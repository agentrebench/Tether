"""Narrow Language Server Protocol navigation tool."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Protocol

from ..core.models import ToolResult
from .base import BaseTool
from .workspace import WorkspacePolicy, WorkspaceViolation


class LspUnavailable(RuntimeError):
    pass


class LspProvider(Protocol):
    def request(
        self,
        *,
        root: Path,
        file_path: Path,
        operation: str,
        line: int,
        character: int,
    ) -> Any: ...


_LANGUAGES: dict[str, tuple[str, list[list[str]]]] = {
    ".ts": ("typescript", [["typescript-language-server", "--stdio"]]),
    ".tsx": ("typescriptreact", [["typescript-language-server", "--stdio"]]),
    ".js": ("javascript", [["typescript-language-server", "--stdio"]]),
    ".jsx": ("javascriptreact", [["typescript-language-server", "--stdio"]]),
    ".mjs": ("javascript", [["typescript-language-server", "--stdio"]]),
    ".cjs": ("javascript", [["typescript-language-server", "--stdio"]]),
    ".py": ("python", [["pyright-langserver", "--stdio"], ["pylsp"]]),
    ".rs": ("rust", [["rust-analyzer"]]),
    ".go": ("go", [["gopls", "serve"]]),
}

_METHODS = {
    "definition": "textDocument/definition",
    "references": "textDocument/references",
    "hover": "textDocument/hover",
    "document_symbols": "textDocument/documentSymbol",
}


class StdioLspProvider:
    """One-request stdio client for installed language servers."""

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    @staticmethod
    def _command(file_path: Path) -> tuple[str, list[str]]:
        language = _LANGUAGES.get(file_path.suffix.lower())
        if language is None:
            raise LspUnavailable(f"No LSP adapter is configured for {file_path.suffix or 'this file'}")
        language_id, candidates = language
        for candidate in candidates:
            executable = shutil.which(candidate[0])
            if executable:
                return language_id, [executable, *candidate[1:]]
        names = ", ".join(candidate[0] for candidate in candidates)
        raise LspUnavailable(f"No language server found. Install one of: {names}")

    @staticmethod
    def _send(proc: subprocess.Popen[bytes], payload: dict) -> None:
        assert proc.stdin is not None
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        proc.stdin.flush()

    @staticmethod
    def _reader(proc: subprocess.Popen[bytes], messages: queue.Queue) -> None:
        assert proc.stdout is not None
        try:
            while True:
                headers: dict[str, str] = {}
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        return
                    if line in (b"\r\n", b"\n"):
                        break
                    key, _, value = line.decode("ascii", errors="replace").partition(":")
                    headers[key.lower().strip()] = value.strip()
                length = int(headers.get("content-length", "0"))
                if length <= 0:
                    continue
                body = proc.stdout.read(length)
                messages.put(json.loads(body.decode("utf-8", errors="replace")))
        except Exception as exc:
            messages.put({"_reader_error": str(exc)})

    def _wait_response(
        self,
        proc: subprocess.Popen[bytes],
        messages: queue.Queue,
        request_id: int,
    ) -> Any:
        while True:
            try:
                message = messages.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise TimeoutError("Language server did not respond in time") from exc
            if "_reader_error" in message:
                raise RuntimeError(message["_reader_error"])
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                return message.get("result")
            # Servers may ask for configuration while initializing. Answer
            # conservatively so a navigation request cannot hang forever.
            if "id" in message and "method" in message:
                result: Any = [] if message["method"] == "workspace/configuration" else None
                self._send(proc, {"jsonrpc": "2.0", "id": message["id"], "result": result})

    def request(
        self,
        *,
        root: Path,
        file_path: Path,
        operation: str,
        line: int,
        character: int,
    ) -> Any:
        language_id, command = self._command(file_path)
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=root,
        )
        messages: queue.Queue = queue.Queue()
        reader = threading.Thread(target=self._reader, args=(proc, messages), daemon=True)
        reader.start()
        try:
            self._send(proc, {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "processId": os.getpid(),
                    "rootUri": root.as_uri(),
                    "capabilities": {},
                    "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
                },
            })
            self._wait_response(proc, messages, 1)
            self._send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
            uri = file_path.as_uri()
            self._send(proc, {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id,
                        "version": 1,
                        "text": file_path.read_text(encoding="utf-8", errors="replace"),
                    }
                },
            })
            params: dict[str, Any] = {"textDocument": {"uri": uri}}
            if operation != "document_symbols":
                params["position"] = {
                    "line": max(0, line - 1),
                    "character": max(0, character - 1),
                }
            if operation == "references":
                params["context"] = {"includeDeclaration": True}
            self._send(proc, {
                "jsonrpc": "2.0", "id": 2,
                "method": _METHODS[operation], "params": params,
            })
            result = self._wait_response(proc, messages, 2)
            self._send(proc, {"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": None})
            return result
        finally:
            try:
                self._send(proc, {"jsonrpc": "2.0", "method": "exit", "params": None})
            except (BrokenPipeError, OSError):
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)


class LspTool(BaseTool):
    def __init__(
        self,
        workspace: WorkspacePolicy | None = None,
        provider: LspProvider | None = None,
    ):
        self.workspace = workspace or WorkspacePolicy.unrestricted()
        self.provider = provider or StdioLspProvider()

    @property
    def name(self) -> str:
        return "lsp"

    @property
    def description(self) -> str:
        return (
            "Use an installed language server for definition, references, hover, or "
            "document symbols in TypeScript/JavaScript, Python, Rust, or Go."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": list(_METHODS)},
                "file_path": {"type": "string"},
                "line": {"type": "integer", "minimum": 1, "default": 1},
                "character": {"type": "integer", "minimum": 1, "default": 1},
            },
            "required": ["operation", "file_path"],
        }

    def execute(self, arguments: dict) -> ToolResult:
        operation = str(arguments.get("operation", ""))
        if operation not in _METHODS:
            return ToolResult(
                name=self.name, content=f"Unsupported LSP operation: {operation}",
                is_error=True, error_code="INVALID_LSP_OPERATION",
            )
        try:
            file_path = self.workspace.resolve(arguments.get("file_path", ""), label="file_path")
        except WorkspaceViolation as exc:
            return ToolResult(
                name=self.name, content=f"Workspace path denied: {exc}",
                is_error=True, error_code="WORKSPACE_PATH_DENIED",
            )
        if not file_path.is_file():
            return ToolResult(
                name=self.name, content=f"File not found: {file_path}",
                is_error=True, error_code="FILE_NOT_FOUND",
            )
        root = self.workspace.root or Path.cwd().resolve()
        try:
            result = self.provider.request(
                root=root,
                file_path=file_path,
                operation=operation,
                line=int(arguments.get("line", 1)),
                character=int(arguments.get("character", 1)),
            )
        except LspUnavailable as exc:
            return ToolResult(
                name=self.name, content=str(exc), is_error=True,
                error_code="LSP_UNAVAILABLE", display_kind="code_navigation",
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            return ToolResult(
                name=self.name, content=f"LSP request failed: {exc}", is_error=True,
                error_code="LSP_REQUEST_FAILED", display_kind="code_navigation",
            )

        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if len(rendered) > 60_000:
            rendered = rendered[:60_000] + "\n... LSP result truncated ..."
        return ToolResult(
            name=self.name,
            content=rendered if result not in (None, [], {}) else "No LSP result.",
            display_kind="code_navigation",
            metadata={"operation": operation, "path": str(file_path)},
        )
