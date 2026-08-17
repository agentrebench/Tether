"""skill_manage — let the agent persist its own skills as procedural memory.

After a non-trivial workflow (several tool calls, error recovery, a user
correction), the model can distil what it learned into a reusable SKILL.md so
the next similar request starts from proven steps. Also used by /learn.

Writes land in ~/.tether/skills/<slug>/SKILL.md. When config.skills_write_approval
is on, the call is gated by the normal approval flow before it touches disk.
"""
from __future__ import annotations

from ..core.models import ToolResult
from ..core.skills import SkillStore, default_store, slugify
from .base import BaseTool


class SkillManageTool(BaseTool):
    def __init__(self, store: SkillStore | None = None):
        self._store = store or default_store()

    @property
    def name(self) -> str:
        return "skill_manage"

    @property
    def description(self) -> str:
        return (
            "Create, patch, or delete a reusable skill (procedural memory) stored "
            "as a SKILL.md. Use this after a non-trivial workflow to save the "
            "procedure for next time. Actions: 'create' (name, content, and ideally "
            "description/category), 'patch' (name + new full content), 'delete' "
            "(name). `content` is the markdown body — write it with sections like "
            "'When to Use', 'Procedure', 'Pitfalls', 'Verification'. Do NOT include "
            "YAML frontmatter; pass name/description/category as fields and they are "
            "written for you."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "patch", "delete"]},
                "name": {"type": "string", "description": "Skill name (human-readable)"},
                "description": {"type": "string", "description": "One-line summary used by skills_list"},
                "category": {"type": "string", "description": "e.g. git, testing, debugging"},
                "when_to_use": {
                    "type": "string",
                    "description": "One line on WHEN this skill applies (the trigger, "
                                   "not the topic) — shown in skills_list",
                },
                "argument_hint": {
                    "type": "string",
                    "description": "Expected /command arguments, e.g. '<file> [notes]'",
                },
                "content": {"type": "string", "description": "Markdown body (no frontmatter)"},
                "platforms": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Optional OS restriction, e.g. ['macos','linux']",
                },
            },
            "required": ["action", "name"],
        }

    def execute(self, arguments: dict) -> ToolResult:
        action = (arguments.get("action") or "").strip().lower()
        name = (arguments.get("name") or "").strip()
        if not name:
            return self._err("skill_manage requires a `name`.")
        slug = slugify(name)

        if action == "delete":
            ok = self._store.delete(name)
            if not ok:
                existing = self._store.get(name)
                if existing and not existing.writable:
                    return self._err(f"'{slug}' is a built-in skill and can't be deleted.")
                return self._err(f"No user skill named '{slug}' to delete.")
            return ToolResult(tool_call_id="", name=self.name,
                              content=f"Deleted skill '{slug}'.")

        if action not in ("create", "patch"):
            return self._err(f"Unknown action '{action}'. Use create, patch, or delete.")

        content = arguments.get("content") or ""
        if not content.strip():
            return self._err("`content` (the skill body) is required for create/patch.")

        existing = self._store.get(name)
        if action == "create" and existing is not None:
            return self._err(
                f"Skill '{slug}' already exists — use action 'patch' to update it."
            )
        if action == "patch" and existing is not None and not existing.writable:
            return self._err(f"'{slug}' is a built-in skill and can't be patched. "
                             f"Create a user skill with a different name to override it.")

        # Preserve metadata on patch unless the caller overrides it.
        description = arguments.get("description")
        category = arguments.get("category")
        platforms = arguments.get("platforms")
        when_to_use = arguments.get("when_to_use")
        argument_hint = arguments.get("argument_hint")
        if existing is not None:
            description = description if description is not None else existing.description
            category = category if category is not None else existing.category
            platforms = platforms if platforms is not None else existing.platforms
            when_to_use = when_to_use if when_to_use is not None else existing.when_to_use
            argument_hint = argument_hint if argument_hint is not None else existing.argument_hint

        path = self._store.write(
            name,
            content,
            description=description or "",
            category=category or "general",
            platforms=[str(p) for p in platforms] if platforms else None,
            when_to_use=when_to_use or "",
            argument_hint=argument_hint or "",
        )
        verb = "Updated" if action == "patch" else "Saved"
        return ToolResult(
            tool_call_id="", name=self.name,
            content=f"{verb} skill '{slug}' → {path}. It's now in skills_list and "
                    f"available as the /{slug} command.",
        )

    def _err(self, msg: str) -> ToolResult:
        return ToolResult(tool_call_id="", name=self.name, content=msg, is_error=True)
