"""Workspace path validation and layered shell sandboxing."""
from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


class WorkspaceViolation(ValueError):
    """Raised when a tool path resolves outside the selected workspace."""


class SandboxUnavailable(RuntimeError):
    """Raised when an enforced workspace cannot safely launch a shell."""


_HOME_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<prefix>~|\$HOME|\$\{HOME\})(?=$|/)"
    r"(?P<suffix>/[^\s'\"`|&;()<>{}]*)?"
)
_USER_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<path>/Users(?:/[^\s'\"`|&;()<>{}]*)?)"
)
_SHELL_FRAGMENT_SPLIT_RE = re.compile(r"[=,:;(){}\[\]'\"`]+")


@dataclass(frozen=True)
class WorkspacePolicy:
    """Resolve tool paths and reject explicit shell escapes from one project.

    The terminal client remains unrestricted by default.  The desktop bridge
    creates an enforced policy for its selected project and shares it with all
    tools, including sub-agents.
    """

    root: Path | None = None
    enforced: bool = False

    def __post_init__(self) -> None:
        if self.enforced and self.root is None:
            raise ValueError("An enforced workspace policy requires a root path")
        if self.root is not None:
            resolved = Path(self.root).expanduser().resolve()
            if not resolved.is_dir():
                raise ValueError(f"Workspace is not a directory: {resolved}")
            object.__setattr__(self, "root", resolved)

    @classmethod
    def unrestricted(cls) -> "WorkspacePolicy":
        return cls()

    def resolve(self, value: str | os.PathLike[str], *, label: str = "path") -> Path:
        """Return one canonical path, rejecting escapes for enforced policies."""
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            base = self.root if self.root is not None else Path.cwd()
            raw = base / raw
        resolved = raw.resolve(strict=False)
        if self.enforced and self.root is not None:
            try:
                resolved.relative_to(self.root)
            except ValueError as exc:
                raise WorkspaceViolation(
                    f"{label} resolves outside the selected workspace: {value}"
                ) from exc
        return resolved

    def _shell_allowed_roots(self) -> tuple[Path, ...]:
        assert self.root is not None
        candidates = (
            self.root,
            Path("/tmp"),
            Path("/private/tmp"),
            Path(tempfile.gettempdir()),
        )
        roots: list[Path] = []
        for candidate in candidates:
            resolved = candidate.expanduser().resolve(strict=False)
            if resolved not in roots:
                roots.append(resolved)
        return tuple(roots)

    @staticmethod
    def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _shell_tokens(script: str) -> list[str]:
        """Return shell-like words while preserving paths inside quoted code.

        This is deliberately a lexical safety check, not a shell parser.  It
        catches explicit filesystem references before Bash can expand them,
        including references embedded in a ``python -c`` argument.  The OS
        sandbox remains the second line of defense for writes.
        """
        try:
            lexer = shlex.shlex(
                script,
                posix=True,
                punctuation_chars="|&;()<>",
            )
            lexer.whitespace_split = True
            lexer.commenters = "#"
            return list(lexer)
        except ValueError:
            # Unbalanced quotes are routine in legitimate scripts (heredoc
            # bodies with apostrophes). Fall back to whitespace tokens so the
            # path checks below still run instead of rejecting the command.
            return script.split()

    def validate_shell_script(
        self,
        script: str,
        cwd: str | os.PathLike[str],
    ) -> None:
        """Reject explicit shell paths that escape an enforced workspace.

        Bash needs access to system executables and libraries, so the desktop
        cannot simply deny every absolute path.  Before launch, reject the
        user-data escape forms models commonly emit: home expansions, absolute
        ``/Users`` paths, and relative parent traversal.  Workspace paths and
        temporary directories remain available.
        """
        if not self.enforced:
            return

        resolved_cwd = self.resolve(cwd, label="working directory")
        roots = self._shell_allowed_roots()
        home = Path(os.environ.get("HOME") or Path.home()).expanduser().resolve(
            strict=False
        )

        def require_allowed(raw: str, path: Path) -> None:
            resolved = path.expanduser().resolve(strict=False)
            if not self._is_within(resolved, roots):
                raise WorkspaceViolation(
                    "shell command references a path outside the selected "
                    f"workspace or temporary directories: {raw}"
                )

        for token in self._shell_tokens(script):
            for match in _HOME_PATH_RE.finditer(token):
                suffix = match.group("suffix") or ""
                require_allowed(match.group(0), home / suffix.lstrip("/"))

            for match in _USER_PATH_RE.finditer(token):
                require_allowed(match.group("path"), Path(match.group("path")))

            # Inspect path-like fragments for ``..`` components. Splitting on
            # shell/code punctuation catches forms such as ``--file=../x`` and
            # ``open('../x')`` without treating ordinary prose as a path.
            for fragment in _SHELL_FRAGMENT_SPLIT_RE.split(token):
                candidate = fragment.strip()
                if not candidate or ".." not in Path(candidate).parts:
                    continue
                path = Path(candidate)
                if not path.is_absolute():
                    path = resolved_cwd / path
                require_allowed(candidate, path)

    def shell_argv(self, script: str, cwd: str | os.PathLike[str]) -> list[str]:
        """Build a platform-specific, workspace-confined command."""
        resolved_cwd = self.resolve(cwd, label="working directory")
        if not self.enforced:
            return ["bash", "-c", script]

        assert self.root is not None
        self.validate_shell_script(script, resolved_cwd)
        system = platform.system()
        if system == "Darwin":
            executable = shutil.which("sandbox-exec")
            if not executable:
                raise SandboxUnavailable(
                    "macOS sandbox-exec is unavailable; refusing an unrestricted shell"
                )
            profile = self._macos_profile()
            return [executable, "-p", profile, "bash", "-c", script]

        if system == "Linux":
            executable = shutil.which("bwrap")
            if not executable:
                raise SandboxUnavailable(
                    "bubblewrap (bwrap) is required for project-scoped shell commands on Linux"
                )
            args = [
                executable,
                "--die-with-parent",
                "--new-session",
                "--ro-bind", "/", "/",
                "--bind", str(self.root), str(self.root),
            ]
            writable = [Path("/tmp"), Path(tempfile.gettempdir()).resolve()]
            seen = {self.root}
            for path in writable:
                if path.exists() and path not in seen:
                    args.extend(["--bind", str(path), str(path)])
                    seen.add(path)
            args.extend([
                "--proc", "/proc",
                "--dev-bind", "/dev", "/dev",
                "--chdir", str(resolved_cwd),
                "bash", "-c", script,
            ])
            return args

        raise SandboxUnavailable(
            f"No project-scoped shell sandbox is implemented for {system or 'this platform'}"
        )

    @staticmethod
    def _scheme_string(path: Path) -> str:
        return str(path).replace("\\", "\\\\").replace('"', '\\"')

    def _macos_profile(self) -> str:
        """Seatbelt profile: normal reads/network/processes, scoped writes."""
        assert self.root is not None
        writable = {
            self.root,
            Path("/tmp"),
            Path("/private/tmp"),
            Path(tempfile.gettempdir()).resolve(),
        }
        exclusions = "\n".join(
            f'(require-not (subpath "{self._scheme_string(path)}"))'
            for path in sorted(writable, key=str)
            if path.exists()
        )
        return (
            "(version 1)\n"
            "(allow default)\n"
            "(deny file-write* (require-all\n"
            f"{exclusions}\n"
            '(require-not (literal "/dev/null"))))\n'
        )
