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

/// PATH for child processes: desktop apps do not inherit the shell profile,
/// so add the places pipx, python.org, and Homebrew put binaries.
fn desktop_path() -> Option<std::ffi::OsString> {
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
    env::join_paths(path_entries).ok()
}

fn which_on_desktop_path(name: &str) -> Option<PathBuf> {
    let path_value = desktop_path()?;
    find_executable_on_path(name, &path_value)
}

fn command_stdout(program: &Path, args: &[&str]) -> Option<String> {
    let mut command = Command::new(program);
    command.args(args);
    if let Some(joined) = desktop_path() {
        command.env("PATH", joined);
    }
    let output = command.output().ok()?;
    let mut text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if text.is_empty() {
        text = String::from_utf8_lossy(&output.stderr).trim().to_string();
    }
    Some(text)
}

/// Parse "Python 3.12.4" → (3, 12).
fn python_version(path: &Path) -> Option<(u32, u32)> {
    let text = command_stdout(path, &["--version"])?;
    let digits = text.strip_prefix("Python ")?.trim();
    let mut parts = digits.split('.');
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next()?.parse().ok()?;
    Some((major, minor))
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct EnvironmentReport {
    platform: String,
    python_path: Option<String>,
    python_version: Option<String>,
    python_ok: bool,
    pipx: bool,
    homebrew: bool,
    git: bool,
    cmake: bool,
    xcode_clt: bool,
    bwrap: bool,
    tether_path: Option<String>,
    tether_version: Option<String>,
    llama_server: bool,
    /// The whole point: can the app open a project right now?
    ready: bool,
    /// Can we run local GGUF models right now?
    local_ready: bool,
}

/// What is installed on this machine, from the desktop app's point of view.
/// Cheap (a handful of `--version` calls) and side-effect free.
#[tauri::command]
fn check_environment() -> EnvironmentReport {
    // Newest usable interpreter wins; Apple's /usr/bin/python3 may be 3.9.
    let mut python: Option<(PathBuf, (u32, u32))> = None;
    for name in [
        "python3.14",
        "python3.13",
        "python3.12",
        "python3.11",
        "python3.10",
        "python3",
    ] {
        if let Some(path) = which_on_desktop_path(name) {
            if let Some(version) = python_version(&path) {
                let better = match &python {
                    Some((_, current)) => version > *current,
                    None => true,
                };
                if better {
                    python = Some((path, version));
                }
            }
        }
    }
    let python_ok = python
        .as_ref()
        .map(|(_, (major, minor))| *major == 3 && *minor >= 10)
        .unwrap_or(false);
    let tether = locate_tether().ok();
    // The CLI knows its own version and where it built llama-server; ask it
    // (`tether doctor --json`) instead of re-deriving that here.
    let doctor: Option<Value> = tether
        .as_ref()
        .and_then(|path| command_stdout(path, &["doctor", "--json"]))
        .and_then(|text| serde_json::from_str(&text).ok());
    let tether_version = doctor
        .as_ref()
        .and_then(|d| d.get("version"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let llama_server = doctor
        .as_ref()
        .and_then(|d| d.get("llama_server_ok"))
        .and_then(Value::as_bool)
        .unwrap_or(false)
        || which_on_desktop_path("llama-server").is_some();
    let xcode_clt = if cfg!(target_os = "macos") {
        Command::new("/usr/bin/xcode-select")
            .arg("-p")
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false)
    } else {
        true
    };
    let bwrap = if cfg!(target_os = "linux") {
        which_on_desktop_path("bwrap").is_some()
    } else {
        true
    };
    EnvironmentReport {
        platform: env::consts::OS.to_string(),
        python_path: python.as_ref().map(|(path, _)| path.display().to_string()),
        python_version: python
            .as_ref()
            .map(|(_, (major, minor))| format!("{major}.{minor}")),
        python_ok,
        pipx: which_on_desktop_path("pipx").is_some(),
        homebrew: which_on_desktop_path("brew").is_some(),
        git: which_on_desktop_path("git").is_some(),
        cmake: which_on_desktop_path("cmake").is_some(),
        xcode_clt,
        bwrap,
        tether_path: tether.as_ref().map(|path| path.display().to_string()),
        tether_version,
        llama_server,
        ready: tether.is_some(),
        local_ready: tether.is_some() && llama_server,
    }
}

#[derive(Default)]
struct SetupState(Mutex<Option<Child>>);

const TETHER_REPO_URL: &str = "https://github.com/agentrebench/Tether.git";

fn setup_script(step: &str) -> Result<String, String> {
    // Every script is idempotent and re-runnable; output is streamed to the
    // app line by line. Nothing here needs sudo.
    let script = match step {
        "install_cli" => format!(
            r#"set -euo pipefail
echo "==> Checking for pipx"
if ! command -v pipx >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "==> Installing pipx with Homebrew"
    brew install pipx
  else
    echo "==> Installing pipx for the current user"
    PY=""
    for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
      if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
    done
    if [ -z "$PY" ]; then echo "error: Python 3.10+ is required. Install it from https://www.python.org/downloads/ and retry." >&2; exit 2; fi
    "$PY" -m pip install --user --upgrade pipx
    export PATH="$HOME/.local/bin:$HOME/Library/Python/3.14/bin:$HOME/Library/Python/3.13/bin:$HOME/Library/Python/3.12/bin:$HOME/Library/Python/3.11/bin:$HOME/Library/Python/3.10/bin:$PATH"
  fi
fi
pipx ensurepath >/dev/null 2>&1 || true
if pipx list --short 2>/dev/null | grep -q '^tether '; then
  # Keep the existing install's source (a git URL, or a developer's editable
  # checkout) and just bring it up to date.
  echo "==> Tether is already installed with pipx; upgrading in place"
  pipx upgrade tether || pipx reinstall tether
else
  echo "==> Installing the Tether CLI (this clones the repo and builds a wheel)"
  pipx install "git+{repo}"
fi
echo "==> Installed:"
"$HOME/.local/bin/tether" --version 2>/dev/null || tether --version
echo "==> Done"
"#,
            repo = TETHER_REPO_URL
        ),
        "build_llama" => r#"set -euo pipefail
if ! command -v git >/dev/null 2>&1; then echo "error: git is required (macOS: run 'xcode-select --install')." >&2; exit 2; fi
if ! command -v cmake >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "==> Installing cmake with Homebrew"
    brew install cmake
  else
    echo "error: cmake is required to build llama.cpp. Install Homebrew (https://brew.sh) or cmake, then retry." >&2
    exit 2
  fi
fi
TETHER="$(command -v tether || echo "$HOME/.local/bin/tether")"
echo "==> Building llama.cpp with: $TETHER setup"
"$TETHER" setup
echo "==> Done"
"#
        .to_string(),
        other => return Err(format!("Unknown setup step: {other}")),
    };
    Ok(script)
}

/// Run one setup step in a login-less bash and stream its output as
/// `setup-log` events; `setup-done` carries the exit code. One at a time.
#[tauri::command]
fn run_setup_step(
    app: tauri::AppHandle,
    state: State<'_, SetupState>,
    step: String,
    setup_id: String,
) -> Result<(), String> {
    let script = setup_script(&step)?;
    let mut guard = state
        .0
        .lock()
        .map_err(|_| "Setup state poisoned".to_string())?;
    if let Some(existing) = guard.as_mut() {
        if let Ok(None) = existing.try_wait() {
            return Err("A setup step is already running.".to_string());
        }
    }
    let mut command = Command::new("bash");
    command
        .arg("-c")
        .arg(&script)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("NONINTERACTIVE", "1")
        .env("HOMEBREW_NO_AUTO_UPDATE", "1")
        .env("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        .env("GIT_TERMINAL_PROMPT", "0");
    if let Some(joined) = desktop_path() {
        command.env("PATH", joined);
    }
    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start setup: {error}"))?;
    let stdout = child.stdout.take().ok_or("Could not read setup output")?;
    let stderr = child.stderr.take().ok_or("Could not read setup errors")?;

    let emit_lines =
        |app: tauri::AppHandle, id: String, reader: Box<dyn Read + Send>, stream: &'static str| {
            thread::spawn(move || {
                for line in BufReader::new(reader).lines().map_while(Result::ok) {
                    let _ = app.emit(
                        "setup-log",
                        json!({ "setupId": id, "stream": stream, "line": line }),
                    );
                }
            })
        };
    let out_thread = emit_lines(app.clone(), setup_id.clone(), Box::new(stdout), "stdout");
    let err_thread = emit_lines(app.clone(), setup_id.clone(), Box::new(stderr), "stderr");

    // Wait in the background and report the exit code.
    let done_app = app.clone();
    let done_id = setup_id.clone();
    let done_step = step.clone();
    let mut waiter = child;
    thread::spawn(move || {
        let status = waiter.wait().ok();
        let _ = out_thread.join();
        let _ = err_thread.join();
        let code = status.and_then(|s| s.code()).unwrap_or(-1);
        let _ = done_app.emit(
            "setup-done",
            json!({ "setupId": done_id, "step": done_step, "code": code, "ok": code == 0 }),
        );
    });
    *guard = None;
    Ok(())
}

/// `~/.tether/scratch` (or `$TETHER_CONFIG_DIR/scratch`) — the workspace used
/// when no project is selected.
fn scratch_workspace_dir() -> Result<PathBuf, String> {
    if let Ok(configured) = env::var("TETHER_CONFIG_DIR") {
        if !configured.trim().is_empty() {
            return Ok(PathBuf::from(configured).join("scratch"));
        }
    }
    let home = env::var("HOME").map_err(|_| "HOME is not set".to_string())?;
    Ok(PathBuf::from(home).join(".tether").join("scratch"))
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
    // No project chosen: run a general session in a private scratch
    // workspace so the app is usable the moment it opens. Tools are confined
    // there; the user can pick a real project any time.
    let project = if project.trim().is_empty() {
        let scratch = scratch_workspace_dir()?;
        fs::create_dir_all(&scratch)
            .map_err(|error| format!("Could not create the scratch workspace: {error}"))?;
        scratch.display().to_string()
    } else {
        project
    };
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

    if let Some(joined) = desktop_path() {
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
        .manage(SetupState::default())
        .invoke_handler(tauri::generate_handler![
            start_bridge,
            send_bridge,
            stop_bridge,
            read_workspace_entry,
            check_environment,
            run_setup_step
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
