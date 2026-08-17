"""File edit tool with multiple editing modes."""
from __future__ import annotations

from dataclasses import dataclass
from difflib import unified_diff

from ..core.models import ToolResult
from ..session.rationale import compute_entries
from .base import BaseTool
from .workspace import WorkspacePolicy, WorkspaceViolation


@dataclass
class EditOperation:
    mode: str
    summary: str


class FileEditTool(BaseTool):
    def __init__(self, rationale_store=None, workspace: WorkspacePolicy | None = None):
        # Optional RationaleStore (injected by the REPL) for the "why" feature.
        # When absent (headless, tests, sub-agents without it), edits still work;
        # per-line rationale simply isn't persisted for later /why lookups.
        self.rationale_store = rationale_store
        self.workspace = workspace or WorkspacePolicy.unrestricted()

    @property
    def name(self) -> str:
        return "file_edit"

    @property
    def description(self) -> str:
        return (
            "Edit a file using one of several modes. "
            "Supported modes: exact string replacement, line-range replacement, "
            "insert before line, insert after line, append, and a batch `edits` "
            "list of exact replacements applied atomically (all succeed or the "
            "file is untouched). Prefer line-based edits after reading the file "
            "with line numbers, and the edits list for several changes to one file."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to edit",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact string to replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text or text to insert",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all exact-string matches",
                    "default": False,
                },
                "start_line": {
                    "type": "integer",
                    "description": "Start line for a line-range replacement (1-based)",
                },
                "end_line": {
                    "type": "integer",
                    "description": "End line for a line-range replacement (1-based, inclusive)",
                },
                "insert_before_line": {
                    "type": "integer",
                    "description": "Insert new_string before this line number (1-based)",
                },
                "insert_after_line": {
                    "type": "integer",
                    "description": "Insert new_string after this line number (1-based)",
                },
                "append": {
                    "type": "boolean",
                    "description": "Append new_string to the end of the file",
                    "default": False,
                },
                "edits": {
                    "type": "array",
                    "description": (
                        "Batch mode: several exact replacements applied in order, "
                        "atomically — if any old_string is missing or ambiguous, "
                        "nothing is written. Use instead of repeated single calls."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                            "replace_all": {"type": "boolean", "default": False},
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": (
                        "Your honest self-assessment (0.0-1.0) that THIS edit is correct "
                        "and complete. Lower it when guessing at an API you didn't verify, "
                        "touching unfamiliar code, or unsure about edge cases; raise it for "
                        "mechanical changes or edits you confirmed against the file. The diff "
                        "is shaded by this so the user reviews uncertain changes first. "
                        "Optional, but strongly encouraged on every edit. Do not inflate it."
                    ),
                },
                "line_rationale": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Optional map of {added line content, or a distinctive substring of "
                        "it -> one short sentence on WHY that specific line is the way it is: "
                        "what you were doing, what you considered, any assumption or risk}. "
                        "Lets the user ask /why <file>:<line> during review. Annotate only "
                        "non-obvious lines — tricky logic, deliberate trade-offs, workarounds "
                        "— not trivial ones."
                    ),
                },
            },
            "required": ["file_path"],
        }

    def execute(self, arguments: dict) -> ToolResult:
        file_path = arguments.get("file_path", "")
        try:
            path = self.workspace.resolve(file_path, label="file_path")
        except WorkspaceViolation as exc:
            return ToolResult(
                name=self.name, content=f"Workspace path denied: {exc}",
                is_error=True, error_code="WORKSPACE_PATH_DENIED",
            )
        if not path.exists():
            return ToolResult(
                tool_call_id="",
                name=self.name,
                content=f"File not found: {file_path}",
                is_error=True,
            )
        if path.is_dir():
            return ToolResult(
                tool_call_id="",
                name=self.name,
                content=f"Path is a directory, not a file: {file_path}",
                is_error=True,
            )

        try:
            original = path.read_text(encoding="utf-8")
            updated, operation = self._apply_edit(original, arguments)
            if updated == original:
                return ToolResult(
                    tool_call_id="",
                    name=self.name,
                    content=f"No changes made to {file_path}",
                    is_error=True,
                )

            path.write_text(updated, encoding="utf-8")
            diff_preview = self._diff_preview(original, updated, file_path)
            # Lead with a SUMMARY line in a fixed format so:
            #   - truncation can't bury it (we keep the head of the string)
            #   - the REPL can parse it for inline rendering
            #   - downstream prompts can key off "SUMMARY:" to enforce
            #     the post-edit narration rule
            summary_line = (
                f"SUMMARY: edited {file_path} via {operation.mode} "
                f"— {operation.summary}"
            )
            # Optional self-reported confidence. Emitted on its own marker line
            # right after SUMMARY so it survives truncation and the REPL can
            # parse it to shade the diff. Omitted entirely when not provided.
            conf_line = ""
            confidence = arguments.get("confidence")
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
                c = max(0.0, min(1.0, float(confidence)))
                conf_line = f"CONFIDENCE: {c:.2f}\n"

            # Per-line "why" capture. Resolve the model's rationale map onto the
            # lines this edit added, persist them for /why lookups, and emit a
            # WHY_LINES marker so the REPL can hint that annotations exist.
            why_line = ""
            entries = compute_entries(original, updated, arguments.get("line_rationale"))
            if entries:
                if self.rationale_store is not None:
                    self.rationale_store.record(file_path, entries)
                nums = ",".join(str(e["line"]) for e in entries)
                why_line = f"WHY_LINES: {file_path}|{nums}\n"

            return ToolResult(
                tool_call_id="",
                name=self.name,
                content=(
                    f"{summary_line}\n"
                    f"{conf_line}"
                    f"{why_line}"
                    f"{diff_preview}\n"
                    f"(Do not re-read this file to verify — the diff above is the ground truth. "
                    f"In your next message to the user, state one sentence about what changed.)"
                ),
                display_kind="diff",
                metadata={
                    "path": str(path),
                    "operation": operation.mode,
                    "summary": operation.summary,
                },
            )
        except Exception as exc:
            return ToolResult(
                tool_call_id="",
                name=self.name,
                content=f"Error editing file: {exc}",
                is_error=True,
            )

    def _apply_edit(self, content: str, arguments: dict) -> tuple[str, EditOperation]:
        new_string = arguments.get("new_string", "")
        old_string = arguments.get("old_string")
        replace_all = bool(arguments.get("replace_all", False))
        start_line = arguments.get("start_line")
        end_line = arguments.get("end_line")
        insert_before_line = arguments.get("insert_before_line")
        insert_after_line = arguments.get("insert_after_line")
        append = bool(arguments.get("append", False))
        edits = arguments.get("edits")

        modes = [
            old_string is not None,
            start_line is not None or end_line is not None,
            insert_before_line is not None,
            insert_after_line is not None,
            append,
            bool(edits),
        ]
        active_modes = sum(bool(mode) for mode in modes)
        if active_modes == 0:
            raise ValueError(
                "No edit mode provided. Use old_string, start_line/end_line, "
                "insert_before_line, insert_after_line, append=true, or edits=[...]."
            )
        if active_modes > 1:
            raise ValueError("Provide exactly one edit mode per file_edit call.")

        if edits:
            return self._apply_batch(content, edits)
        if old_string is not None:
            return self._replace_string(content, old_string, new_string, replace_all)
        if start_line is not None or end_line is not None:
            return self._replace_lines(content, start_line, end_line, new_string)
        if insert_before_line is not None:
            return self._insert_at_line(content, insert_before_line, new_string, before=True)
        if insert_after_line is not None:
            return self._insert_at_line(content, insert_after_line, new_string, before=False)
        return self._append(content, new_string)

    def _apply_batch(self, content: str, edits: list) -> tuple[str, EditOperation]:
        """Sequential exact replacements over the evolving content. Raising
        before anything is written is what makes the batch atomic — a missing
        or ambiguous old_string aborts the whole call with the file untouched."""
        updated = content
        for i, e in enumerate(edits, 1):
            if not isinstance(e, dict):
                raise ValueError(f"edits[{i}] must be an object with old_string/new_string")
            old = e.get("old_string")
            if not old:
                raise ValueError(f"edits[{i}]: old_string is required (no changes applied)")
            new = e.get("new_string", "")
            replace_all = bool(e.get("replace_all", False))
            count = updated.count(old)
            if count == 0:
                raise ValueError(f"edits[{i}]: old_string not found (no changes applied)")
            if count > 1 and not replace_all:
                raise ValueError(
                    f"edits[{i}]: old_string found {count} times — add context or "
                    f"set replace_all=true (no changes applied)"
                )
            updated = updated.replace(old, new, -1 if replace_all else 1)
        return updated, EditOperation(
            mode="batch_replace",
            summary=f"applied {len(edits)} replacement(s)",
        )

    def _replace_string(
        self,
        content: str,
        old_string: str,
        new_string: str,
        replace_all: bool,
    ) -> tuple[str, EditOperation]:
        count = content.count(old_string)
        if count == 0:
            raise ValueError("old_string not found")
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_string found {count} times. Provide more context or set replace_all=true."
            )

        updated = content.replace(old_string, new_string, -1 if replace_all else 1)
        replaced = count if replace_all else 1
        return updated, EditOperation(
            mode="exact_replace",
            summary=f"replaced {replaced} occurrence(s)",
        )

    def _replace_lines(
        self,
        content: str,
        start_line: int | None,
        end_line: int | None,
        new_string: str,
    ) -> tuple[str, EditOperation]:
        if start_line is None or end_line is None:
            raise ValueError("start_line and end_line are both required for line-range replacement")
        if start_line < 1 or end_line < start_line:
            raise ValueError("Invalid line range")

        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        if end_line > total_lines:
            raise ValueError(f"Line range {start_line}-{end_line} exceeds file length {total_lines}")

        # The inserted block needs a trailing newline whenever lines follow
        # it, regardless of whether the file itself ends with one.
        replacement = self._split_replacement(
            new_string,
            preserve_trailing_newline=end_line < total_lines or content.endswith("\n"),
        )
        updated_lines = lines[: start_line - 1] + replacement + lines[end_line:]
        return "".join(updated_lines), EditOperation(
            mode="line_replace",
            summary=f"replaced lines {start_line}-{end_line}",
        )

    def _insert_at_line(
        self,
        content: str,
        line_number: int,
        new_string: str,
        *,
        before: bool,
    ) -> tuple[str, EditOperation]:
        if line_number < 1:
            raise ValueError("Line numbers must be 1-based")

        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        if total_lines == 0 and line_number != 1:
            raise ValueError("Empty file only supports insert at line 1")
        if total_lines > 0 and before and line_number > total_lines + 1:
            raise ValueError(f"Line number {line_number} exceeds file length {total_lines}")
        if total_lines > 0 and not before and line_number > total_lines:
            raise ValueError(f"Line number {line_number} exceeds file length {total_lines}")

        insert_at = line_number - 1 if before else line_number
        replacement = self._split_replacement(
            new_string,
            preserve_trailing_newline=insert_at < total_lines or content.endswith("\n"),
        )
        # Inserting after a final line that has no newline must not glue the
        # new text onto it.
        if replacement and 0 < insert_at <= total_lines and not lines[insert_at - 1].endswith("\n"):
            lines[insert_at - 1] += "\n"
        updated_lines = lines[:insert_at] + replacement + lines[insert_at:]
        mode = "insert_before_line" if before else "insert_after_line"
        anchor = "before" if before else "after"
        return "".join(updated_lines), EditOperation(
            mode=mode,
            summary=f"inserted text {anchor} line {line_number}",
        )

    def _append(self, content: str, new_string: str) -> tuple[str, EditOperation]:
        if content and not content.endswith("\n") and new_string and not new_string.startswith("\n"):
            updated = content + "\n" + new_string
        else:
            updated = content + new_string
        return updated, EditOperation(
            mode="append",
            summary=f"appended {len(new_string)} characters",
        )

    @staticmethod
    def _split_replacement(new_string: str, preserve_trailing_newline: bool) -> list[str]:
        if not new_string:
            return []
        pieces = new_string.splitlines(keepends=True)
        if new_string and not pieces:
            return [new_string]
        # Preserve the common newline style for line-based edits without forcing
        # a trailing newline into files that intentionally omit one.
        if preserve_trailing_newline and new_string and not new_string.endswith("\n") and pieces:
            if not pieces[-1].endswith("\n"):
                pieces[-1] = pieces[-1] + "\n"
        return pieces

    def _diff_preview(self, original: str, updated: str, file_path: str) -> str:
        diff_lines = list(
            unified_diff(
                original.splitlines(),
                updated.splitlines(),
                fromfile=f"{file_path} (before)",
                tofile=f"{file_path} (after)",
                lineterm="",
                n=2,
            )
        )
        if not diff_lines:
            return "(no diff preview)"
        preview = diff_lines[:80]
        if len(diff_lines) > 80:
            preview.append(f"... ({len(diff_lines) - 80} more diff lines)")
        return "\n".join(preview)
