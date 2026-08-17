"""Grep tool — content search using regex."""
from __future__ import annotations

import subprocess

from ..core.models import ToolResult
from .base import BaseTool
from .workspace import WorkspacePolicy, WorkspaceViolation


class GrepTool(BaseTool):
    def __init__(self, workspace: WorkspacePolicy | None = None):
        self.workspace = workspace or WorkspacePolicy.unrestricted()

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return "Search file contents for a regex pattern. Uses ripgrep if available, falls back to Python regex."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search (default: current directory)",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob filter for files (e.g. '*.py')",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case insensitive search",
                    "default": False,
                },
                "before_context": {
                    "type": "integer",
                    "description": "Lines of context before each match (like grep -B)",
                },
                "after_context": {
                    "type": "integer",
                    "description": "Lines of context after each match (like grep -A)",
                },
                "context": {
                    "type": "integer",
                    "description": "Lines of context around each match (like grep -C; overrides before/after)",
                },
            },
            "required": ["pattern"],
        }

    @staticmethod
    def _context_flags(arguments: dict) -> list[str]:
        """-A/-B/-C flags shared by ripgrep and grep."""
        flags: list[str] = []
        ctx = arguments.get("context")
        if ctx:
            return ["-C", str(int(ctx))]
        before = arguments.get("before_context")
        after = arguments.get("after_context")
        if before:
            flags.extend(["-B", str(int(before))])
        if after:
            flags.extend(["-A", str(int(after))])
        return flags

    def execute(self, arguments: dict) -> ToolResult:
        pattern = arguments.get("pattern", "")
        try:
            search_path = str(self.workspace.resolve(arguments.get("path", "."), label="path"))
        except WorkspaceViolation as exc:
            return ToolResult(
                name=self.name, content=f"Workspace path denied: {exc}",
                is_error=True, error_code="WORKSPACE_PATH_DENIED",
            )
        file_glob = arguments.get("glob")
        case_insensitive = arguments.get("case_insensitive", False)
        context_flags = self._context_flags(arguments)

        # Try ripgrep first
        try:
            cmd = ["rg", "--no-heading", "--line-number"]
            if case_insensitive:
                cmd.append("-i")
            if file_glob:
                cmd.extend(["--glob", file_glob])
            cmd.extend(context_flags)
            # ``-e`` keeps patterns like ``-> None`` from being parsed as flags.
            cmd.extend(["--max-count", "250", "-e", pattern, search_path])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return ToolResult(
                    tool_call_id="", name=self.name,
                    content=result.stdout[:50000] or "(no matches)",
                    display_kind="search", metadata={"path": search_path},
                )
            elif result.returncode == 1:
                return ToolResult(
                    tool_call_id="", name=self.name,
                    content="No matches found.",
                )
            elif result.stdout:
                # Exit 2 with output: rg hit an unreadable file/socket
                # somewhere but still matched. Return the matches and note
                # the problem instead of discarding results.
                note = result.stderr.strip().splitlines()
                warning = f"\n\n[search warnings: {len(note)} path(s) could not be read]" if note else ""
                return ToolResult(
                    tool_call_id="", name=self.name,
                    content=result.stdout[:50000] + warning,
                    display_kind="search", metadata={"path": search_path},
                )
            else:
                # Exit 2 with no output = bad regex / bad path; report it
                # rather than silently falling back to grep's different dialect.
                return ToolResult(
                    tool_call_id="", name=self.name,
                    content=f"Search error: {result.stderr.strip() or 'ripgrep failed'}",
                    is_error=True,
                )
        except FileNotFoundError:
            pass  # rg not available, fall back to grep
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_call_id="", name=self.name,
                content="Search timed out after 30s", is_error=True,
            )

        # Fallback: python grep
        try:
            cmd = ["grep", "-rnE"]
            if case_insensitive:
                cmd.append("-i")
            if file_glob:
                cmd.extend(["--include", file_glob])
            cmd.extend(context_flags)
            cmd.extend(["-m", "250", "-e", pattern, search_path])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode not in (0, 1) and not result.stdout:
                return ToolResult(
                    tool_call_id="", name=self.name,
                    content=f"Search error: {result.stderr.strip() or 'grep failed'}",
                    is_error=True,
                )
            return ToolResult(
                tool_call_id="", name=self.name,
                content=result.stdout[:50000] or "No matches found.",
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="", name=self.name,
                content=f"Error: {e}", is_error=True,
            )
