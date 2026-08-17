use serde::Serialize;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::env;
use std::ffi::OsStr;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};
use tauri::{Emitter, State};

struct ManagedBridge {
    child: Child,
    stdin: ChildStdin,
    workspace_root: PathBuf,
}

#[derive(Default)]
struct BridgeState(Mutex<Option<ManagedBridge>>);

const MAX_PREVIEW_BYTES: u64 = 512 * 1024;
const MAX_DIRECTORY_ENTRIES: usize = 500;
const MAX_PATH_SEARCH_ENTRIES: usize = 20_000;
const BRIDGE_SHUTDOWN_GRACE: Duration = Duration::from_secs(8);
const BRIDGE_SHUTDOWN_POLL: Duration = Duration::from_millis(50);

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct WorkspaceEntry {
    name: String,
    path: String,
    is_directory: bool,
    size_bytes: u64,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct WorkspacePreview {
    path: String,
    kind: String,
    content: String,
    entries: Vec<WorkspaceEntry>,
    size_bytes: u64,
    truncated: bool,
}

fn workspace_relative_path(root: &Path, path: &Path) -> String {
    let relative = path.strip_prefix(root).unwrap_or(path);
    let display = relative.to_string_lossy().replace('\\', "/");
    if display.is_empty() {
        ".".to_string()
    } else {
        display
    }
}

fn canonical_workspace_candidate(root: &Path, candidate: &Path) -> Result<PathBuf, String> {
    let resolved = candidate
        .canonicalize()
        .map_err(|error| format!("Workspace path is unavailable: {error}"))?;
    if !resolved.starts_with(root) {
        return Err("That path is outside the selected workspace.".to_string());
    }
    Ok(resolved)
}

fn skip_search_directory(name: &str) -> bool {
    matches!(
        name,
        ".git" | "node_modules" | "target" | "dist" | ".venv" | "venv" | "__pycache__"
    )
}

fn find_unique_workspace_suffix(root: &Path, suffix: &Path) -> Result<Option<PathBuf>, String> {
    let mut queue = VecDeque::from([root.to_path_buf()]);
    let mut matches = Vec::new();
    let mut inspected = 0usize;

    while let Some(directory) = queue.pop_front() {
        let entries = match fs::read_dir(&directory) {
            Ok(entries) => entries,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            inspected += 1;
            if inspected > MAX_PATH_SEARCH_ENTRIES {
                return Err(
                    "That shortened path is too broad to resolve safely; use a longer workspace-relative path."
                        .to_string(),
                );
            }
            let file_type = match entry.file_type() {
                Ok(file_type) => file_type,
                Err(_) => continue,
            };
            if file_type.is_symlink() {
                continue;
            }
            let path = entry.path();
            let relative = match path.strip_prefix(root) {
                Ok(relative) => relative,
                Err(_) => continue,
            };
            if relative.ends_with(suffix) {
                matches.push(path.clone());
                if matches.len() > 1 {
                    return Err(format!(
                        "The shortened path '{}' is ambiguous; use a longer workspace-relative path.",
                        suffix.display()
                    ));
                }
            }
            if file_type.is_dir() && !skip_search_directory(&entry.file_name().to_string_lossy()) {
                queue.push_back(path);
            }
        }
    }
    Ok(matches.pop())
}

fn resolve_workspace_path(root: &Path, requested: &str) -> Result<PathBuf, String> {
    let requested = requested.trim();
    if requested.is_empty() {
        return Err("Choose a file or directory inside the workspace.".to_string());
    }
    let requested_path = Path::new(requested);
    if requested_path.is_absolute() {
        return canonical_workspace_candidate(root, requested_path);
    }

    let normalized = requested_path.strip_prefix("./").unwrap_or(requested_path);
    let direct = root.join(normalized);
    if direct.exists() {
        return canonical_workspace_candidate(root, &direct);
    }

    if let Some(root_name) = root.file_name() {
        let mut components = normalized.components();
        if components.next().map(|part| part.as_os_str()) == Some(root_name) {
            let without_root_name = components.as_path();
            let candidate = root.join(without_root_name);
            if candidate.exists() {
                return canonical_workspace_candidate(root, &candidate);
            }
        }
    }

    if let Some(candidate) = find_unique_workspace_suffix(root, normalized)? {
        return canonical_workspace_candidate(root, &candidate);
    }

    Err(format!(
        "No workspace file or directory matches '{}'.",
        requested
    ))
}

fn read_workspace_path(root: &Path, requested: &str) -> Result<WorkspacePreview, String> {
    let path = resolve_workspace_path(root, requested)?;
    let metadata = fs::metadata(&path)
        .map_err(|error| format!("Could not inspect workspace path: {error}"))?;
    let display_path = workspace_relative_path(root, &path);

    if metadata.is_dir() {
        let mut entries = fs::read_dir(&path)
            .map_err(|error| format!("Could not list directory: {error}"))?
            .filter_map(Result::ok)
            .filter_map(|entry| {
                let resolved = entry.path().canonicalize().ok()?;
                if !resolved.starts_with(root) {
                    return None;
                }
                let metadata = fs::metadata(&resolved).ok()?;
                Some(WorkspaceEntry {
                    name: entry.file_name().to_string_lossy().into_owned(),
                    path: workspace_relative_path(root, &resolved),
                    is_directory: metadata.is_dir(),
                    size_bytes: if metadata.is_file() {
                        metadata.len()
                    } else {
                        0
                    },
                })
            })
            .collect::<Vec<_>>();
        entries.sort_by(|left, right| {
            right
                .is_directory
                .cmp(&left.is_directory)
                .then_with(|| left.name.to_lowercase().cmp(&right.name.to_lowercase()))
        });
        let truncated = entries.len() > MAX_DIRECTORY_ENTRIES;
        entries.truncate(MAX_DIRECTORY_ENTRIES);
        return Ok(WorkspacePreview {
            path: display_path,
            kind: "directory".to_string(),
            content: String::new(),
            entries,
            size_bytes: 0,
            truncated,
        });
    }

    if !metadata.is_file() {
        return Err("That workspace entry is not a regular file or directory.".to_string());
    }

    let mut bytes = Vec::new();
    fs::File::open(&path)
        .map_err(|error| format!("Could not open file: {error}"))?
        .take(MAX_PREVIEW_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("Could not read file: {error}"))?;
    if bytes.contains(&0) {
        return Err("Binary files cannot be shown in the code preview.".to_string());
    }
    let truncated = bytes.len() as u64 > MAX_PREVIEW_BYTES;
    if truncated {
        bytes.truncate(MAX_PREVIEW_BYTES as usize);
    }

    Ok(WorkspacePreview {
        path: display_path,
        kind: "file".to_string(),
        content: String::from_utf8_lossy(&bytes).into_owned(),
        entries: Vec::new(),
        size_bytes: metadata.len(),
        truncated,
    })
}

fn executable_file(path: &Path) -> bool {
    let Ok(metadata) = fs::metadata(path) else {
        return false;
    };
    if !metadata.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    true
}

fn find_executable_on_path(name: &str, path_value: &OsStr) -> Option<PathBuf> {
    env::split_paths(path_value)
        .map(|directory| directory.join(name))
        .find(|candidate| executable_file(candidate))
}

fn locate_tether() -> Result<PathBuf, String> {
    if let Ok(configured) = env::var("TETHER_CLI") {
        let path = PathBuf::from(configured);
        if executable_file(&path) {
            return Ok(path);
        }
    }

    if let Some(path_value) = env::var_os("PATH") {
        if let Some(path) = find_executable_on_path("tether", &path_value) {
            return Ok(path);
        }
    }

    let mut candidates = Vec::new();
    if let Ok(home) = env::var("HOME") {
        let home = PathBuf::from(home);
        candidates.push(home.join(".local/bin/tether"));
        if cfg!(target_os = "macos") {
            for version in ["3.14", "3.13", "3.12", "3.11", "3.10"] {
                candidates.push(
                    home.join("Library")
                        .join("Python")
                        .join(version)
                        .join("bin/tether"),
                );
            }
        }
    }
    candidates.push(PathBuf::from("/usr/local/bin/tether"));
    candidates.push(PathBuf::from("/usr/bin/tether"));
    if cfg!(target_os = "macos") {
        candidates.push(PathBuf::from("/opt/homebrew/bin/tether"));
    }

    candidates
        .into_iter()
        .find(|path| executable_file(path))
        .ok_or_else(|| {
            "The Tether CLI was not found. Install it with pipx or set TETHER_CLI to its absolute path."
                .to_string()
        })
}

fn bridge_envelope(bridge_id: &str, payload: Value) -> Value {
    json!({
        "bridgeId": bridge_id,
        "payload": payload,
    })
}

fn stop_managed_bridge(bridge: ManagedBridge) {
    let ManagedBridge {
        mut child,
        mut stdin,
        workspace_root: _,
    } = bridge;
    let _ = writeln!(stdin, "{}", json!({ "type": "shutdown" }));
    let _ = stdin.flush();
    drop(stdin);
    thread::spawn(move || {
        let deadline = Instant::now() + BRIDGE_SHUTDOWN_GRACE;
        loop {
            match child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) if Instant::now() < deadline => thread::sleep(BRIDGE_SHUTDOWN_POLL),
                Ok(None) | Err(_) => break,
            }
        }
        let _ = child.kill();
        let _ = child.wait();
    });
}

#[tauri::command]
fn start_bridge(
    app: tauri::AppHandle,
    state: State<'_, BridgeState>,
    project: String,
    bridge_id: String,
) -> Result<String, String> {
    let project_path = PathBuf::from(&project)
        .canonicalize()
        .map_err(|error| format!("Project directory is unavailable: {error}"))?;
    if !project_path.is_dir() {
        return Err(format!("Project path is not a directory: {project}"));
    }

    if let Some(existing) = state
        .0
        .lock()
        .map_err(|_| "Bridge state lock was poisoned".to_string())?
        .take()
    {
        stop_managed_bridge(existing);
    }

    let executable = locate_tether()?;
    let mut command = Command::new(&executable);
    command
        .args(["app-bridge", "--project"])
        .arg(&project_path)
        .current_dir(&project_path)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut path_entries: Vec<PathBuf> = Vec::new();
    if let Ok(home) = env::var("HOME") {
        path_entries.push(PathBuf::from(&home).join(".local/bin"));
        if cfg!(target_os = "macos") {
            for version in ["3.14", "3.13", "3.12", "3.11", "3.10"] {
                path_entries.push(
                    PathBuf::from(&home)
                        .join("Library")
                        .join("Python")
                        .join(version)
                        .join("bin"),
                );
            }
            path_entries.push(PathBuf::from("/opt/homebrew/bin"));
        }
    }
    path_entries.push(PathBuf::from("/usr/local/bin"));
    path_entries.push(PathBuf::from("/usr/bin"));
    path_entries.push(PathBuf::from("/bin"));
    if let Some(inherited) = env::var_os("PATH") {
        path_entries.extend(env::split_paths(&inherited));
    }
    if let Ok(joined) = env::join_paths(path_entries) {
        command.env("PATH", joined);
    }

    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not launch {}: {error}", executable.display()))?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| "Could not open bridge stdin".to_string())?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Could not open bridge stdout".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "Could not open bridge stderr".to_string())?;

    let stdout_app = app.clone();
    let stdout_id = bridge_id.clone();
    thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            match line {
                Ok(line) if !line.trim().is_empty() => {
                    let payload = serde_json::from_str::<Value>(&line).unwrap_or_else(|error| {
                        json!({
                            "type": "bridge_log",
                            "message": format!("Invalid bridge event: {error}"),
                        })
                    });
                    let _ = stdout_app.emit("bridge-event", bridge_envelope(&stdout_id, payload));
                }
                Ok(_) => {}
                Err(error) => {
                    let _ = stdout_app.emit(
                        "bridge-event",
                        bridge_envelope(
                            &stdout_id,
                            json!({ "type": "bridge_log", "message": error.to_string() }),
                        ),
                    );
                    break;
                }
            }
        }
        let _ = stdout_app.emit(
            "bridge-event",
            bridge_envelope(
                &stdout_id,
                json!({ "type": "bridge_stopped", "message": "The Tether engine stopped." }),
            ),
        );
    });

    let stderr_app = app;
    let stderr_id = bridge_id;
    thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            if !line.trim().is_empty() {
                let _ = stderr_app.emit(
                    "bridge-event",
                    bridge_envelope(&stderr_id, json!({ "type": "bridge_log", "message": line })),
                );
            }
        }
    });

    *state
        .0
        .lock()
        .map_err(|_| "Bridge state lock was poisoned".to_string())? = Some(ManagedBridge {
        child,
        stdin,
        workspace_root: project_path,
    });

    Ok(executable.to_string_lossy().into_owned())
}

#[tauri::command]
fn send_bridge(state: State<'_, BridgeState>, message: Value) -> Result<(), String> {
    let mut guard = state
        .0
        .lock()
        .map_err(|_| "Bridge state lock was poisoned".to_string())?;
    let bridge = guard
        .as_mut()
        .ok_or_else(|| "The Tether engine is not running".to_string())?;
    let encoded = serde_json::to_string(&message).map_err(|error| error.to_string())?;
    writeln!(bridge.stdin, "{encoded}").map_err(|error| error.to_string())?;
    bridge.stdin.flush().map_err(|error| error.to_string())
}

#[tauri::command]
fn stop_bridge(state: State<'_, BridgeState>) -> Result<(), String> {
    if let Some(bridge) = state
        .0
        .lock()
        .map_err(|_| "Bridge state lock was poisoned".to_string())?
        .take()
    {
        stop_managed_bridge(bridge);
    }
    Ok(())
}

#[tauri::command]
fn read_workspace_entry(
    state: State<'_, BridgeState>,
    path: String,
) -> Result<WorkspacePreview, String> {
    let workspace_root = state
        .0
        .lock()
        .map_err(|_| "Bridge state lock was poisoned".to_string())?
        .as_ref()
        .map(|bridge| bridge.workspace_root.clone())
        .ok_or_else(|| "Choose a project before opening files.".to_string())?;
    read_workspace_path(&workspace_root, &path)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(BridgeState::default())
        .invoke_handler(tauri::generate_handler![
            start_bridge,
            send_bridge,
            stop_bridge,
            read_workspace_entry
        ])
        .run(tauri::generate_context!())
        .expect("error while running Tether desktop");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::File;

    #[test]
    fn bridge_events_are_scoped_to_the_launch_id() {
        let envelope = bridge_envelope("bridge-42", json!({ "type": "hello" }));
        assert_eq!(envelope["bridgeId"], "bridge-42");
        assert_eq!(envelope["payload"]["type"], "hello");
    }

    #[test]
    fn executable_check_rejects_directories() {
        let fixture = env::temp_dir().join(format!("tether-desktop-test-{}", std::process::id()));
        fs::create_dir_all(&fixture).expect("create fixture directory");
        let file = fixture.join("tether");
        File::create(&file).expect("create fixture file");

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = fs::metadata(&file).expect("file metadata").permissions();
            permissions.set_mode(0o755);
            fs::set_permissions(&file, permissions).expect("mark fixture executable");
        }

        assert!(executable_file(&file));
        assert!(!executable_file(&fixture));

        let path_value = env::join_paths([fixture.clone()]).expect("join fixture PATH");
        assert_eq!(
            find_executable_on_path("tether", &path_value),
            Some(file.clone())
        );

        fs::remove_dir_all(&fixture).expect("remove fixture directory");
    }

    #[test]
    fn workspace_preview_reads_files_and_rejects_escapes() {
        let suffix = format!(
            "{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock after epoch")
                .as_nanos()
        );
        let fixture = env::temp_dir().join(format!("tether-preview-{suffix}"));
        let outside = env::temp_dir().join(format!("tether-outside-{suffix}.txt"));
        fs::create_dir_all(fixture.join("desktop/src")).expect("create preview fixture");
        fs::write(
            fixture.join("desktop/src/app.ts"),
            "export const ready = true;\n",
        )
        .expect("write preview file");
        fs::write(&outside, "outside\n").expect("write outside file");

        let root = fixture.canonicalize().expect("canonical fixture");
        let preview = read_workspace_path(&root, "src/app.ts").expect("resolve shortened file");
        assert_eq!(preview.kind, "file");
        assert_eq!(preview.path, "desktop/src/app.ts");
        assert!(preview.content.contains("ready = true"));

        let directory = read_workspace_path(&root, "src/").expect("resolve shortened directory");
        assert_eq!(directory.kind, "directory");
        assert_eq!(directory.entries.len(), 1);
        assert_eq!(directory.entries[0].path, "desktop/src/app.ts");

        let prefixed = format!(
            "{}/desktop/src/app.ts",
            root.file_name()
                .expect("fixture basename")
                .to_string_lossy()
        );
        assert_eq!(
            read_workspace_path(&root, &prefixed)
                .expect("strip workspace prefix")
                .path,
            "desktop/src/app.ts"
        );

        assert!(read_workspace_path(&root, outside.to_string_lossy().as_ref()).is_err());

        fs::remove_dir_all(&fixture).expect("remove preview fixture");
        fs::remove_file(&outside).expect("remove outside fixture");
    }
}
