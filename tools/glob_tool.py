"""Glob tool — file pattern matching."""
from __future__ import annotations


from ..core.models import ToolResult
from .base import BaseTool
from .workspace import WorkspacePolicy, WorkspaceViolation


class GlobTool(BaseTool):
    def __init__(self, workspace: WorkspacePolicy | None = None):
        self.workspace = workspace or WorkspacePolicy.unrestricted()

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return "Find files matching a glob pattern. Returns matching file paths sorted by modification time."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.ts')",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: current directory)",
                },
            },
            "required": ["pattern"],
        }

    def _allowed(self, path) -> bool:
        try:
            self.workspace.resolve(path)
            return True
        except WorkspaceViolation:
            return False

    def execute(self, arguments: dict) -> ToolResult:
        pattern = arguments.get("pattern", "")
        try:
            search_path = self.workspace.resolve(arguments.get("path", "."), label="path")
        except WorkspaceViolation as exc:
            return ToolResult(
                name=self.name, content=f"Workspace path denied: {exc}",
                is_error=True, error_code="WORKSPACE_PATH_DENIED",
            )

        try:
            # ``..`` in the pattern is a literal component to pathlib, so an
            # enforced workspace has to be re-checked on the results; lstat()
            # keeps a dangling symlink from failing the whole search.
            matches = [m for m in search_path.glob(pattern) if self._allowed(m)]
            matches.sort(key=lambda p: p.lstat().st_mtime, reverse=True)
            if not matches:
                return ToolResult(
                    tool_call_id="", name=self.name,
                    content=f"No files matching '{pattern}' in {search_path}",
                )
            lines = [str(m) for m in matches[:250]]
            return ToolResult(
                tool_call_id="", name=self.name,
                content="\n".join(lines), display_kind="files",
                metadata={"path": str(search_path), "count": len(matches[:250])},
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="", name=self.name,
                content=f"Error: {e}", is_error=True,
            )
