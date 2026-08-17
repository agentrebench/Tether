"""File read tool."""
from __future__ import annotations


from ..core.cache import get_file_metadata_cached, invalidate_file_metadata
from ..core.models import ToolResult
from .base import BaseTool
from .workspace import WorkspacePolicy, WorkspaceViolation


class FileReadTool(BaseTool):
    def __init__(self, workspace: WorkspacePolicy | None = None):
        self.workspace = workspace or WorkspacePolicy.unrestricted()

    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return "Read a file from the filesystem. Returns file contents with line numbers."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-based)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read (default: 2000)",
                    "default": 2000,
                },
            },
            "required": ["file_path"],
        }

    def execute(self, arguments: dict) -> ToolResult:
        file_path = arguments.get("file_path", "")
        offset = arguments.get("offset", 1)
        limit = arguments.get("limit", 2000)

        try:
            path = self.workspace.resolve(file_path, label="file_path")
        except WorkspaceViolation as exc:
            return ToolResult(
                name=self.name, content=f"Workspace path denied: {exc}",
                is_error=True, error_code="WORKSPACE_PATH_DENIED",
            )
        
        # Use cached metadata for existence and type checks
        metadata = get_file_metadata_cached(str(path))
        if metadata is None:
            return ToolResult(
                tool_call_id="", name=self.name,
                content=f"File not found: {file_path}", is_error=True,
            )
        if not metadata['is_file']:
            return ToolResult(
                tool_call_id="", name=self.name,
                content=f"Path is a directory, not a file: {file_path}", is_error=True,
            )

        try:
            lines = path.read_text(errors="replace").splitlines()
            start = max(0, offset - 1)
            selected = lines[start : start + limit]
            numbered = [f"{start + i + 1}\t{line}" for i, line in enumerate(selected)]
            return ToolResult(
                tool_call_id="", name=self.name,
                content="\n".join(numbered) if numbered else "(empty file)",
                display_kind="file",
                metadata={"path": str(path), "offset": start + 1, "lines": len(selected)},
            )
        except Exception as e:
            # Invalidate cache on error as file might have changed
            invalidate_file_metadata(str(path))
            return ToolResult(
                tool_call_id="", name=self.name,
                content=f"Error reading file: {e}", is_error=True,
            )
