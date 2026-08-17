"""File write tool — create or overwrite files."""
from __future__ import annotations


from ..core.models import ToolResult
from .base import BaseTool
from .workspace import WorkspacePolicy, WorkspaceViolation


class FileWriteTool(BaseTool):
    def __init__(self, workspace: WorkspacePolicy | None = None):
        self.workspace = workspace or WorkspacePolicy.unrestricted()

    @property
    def name(self) -> str:
        return "file_write"

    @property
    def description(self) -> str:
        return "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Creates parent directories as needed."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
            },
            "required": ["file_path", "content"],
        }

    def execute(self, arguments: dict) -> ToolResult:
        file_path = arguments.get("file_path", "")
        content = arguments.get("content", "")

        try:
            path = self.workspace.resolve(file_path, label="file_path")
        except WorkspaceViolation as exc:
            return ToolResult(
                name=self.name, content=f"Workspace path denied: {exc}",
                is_error=True, error_code="WORKSPACE_PATH_DENIED",
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            return ToolResult(
                tool_call_id="", name=self.name,
                content=f"Wrote {len(content)} bytes to {path}. (You know the file contents — do not re-read to verify.)",
                display_kind="file",
                metadata={"path": str(path), "bytes": len(content)},
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="", name=self.name,
                content=f"Error writing file: {e}", is_error=True,
            )
