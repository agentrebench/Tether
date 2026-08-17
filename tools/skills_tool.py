"""Progressive-disclosure skill tools.

Three levels, three cheap reads:
  - skills_list()            -> name/description/category for every available skill
  - skill_view(name)         -> the full SKILL.md body (+ a list of its bundled files)
  - skill_view(name, path)   -> one bundled reference file

The model only pays for the full skill when it decides it needs it.
"""
from __future__ import annotations


from ..core.models import ToolResult
from ..core.skills import SkillStore, default_store
from .base import BaseTool


class SkillsListTool(BaseTool):
    def __init__(self, store: SkillStore | None = None, toolsets_fn=None):
        self._store = store or default_store()
        # Returns the set of tool names currently wired up — used to honour a
        # skill's requires_toolsets / fallback_for_toolsets gating.
        self._toolsets_fn = toolsets_fn or (lambda: set())

    @property
    def name(self) -> str:
        return "skills_list"

    @property
    def description(self) -> str:
        return (
            "List the reusable skills (procedural knowledge) available to you. "
            "Returns only name, category, and description — call skill_view(name) "
            "to load a skill's full instructions when you decide one applies."
        )

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict) -> ToolResult:
        skills = self._store.available(self._toolsets_fn())
        if not skills:
            return ToolResult(
                tool_call_id="", name=self.name,
                content="No skills installed. The user can author one with /learn "
                        "or you can save a reusable procedure with skill_manage.",
            )
        by_cat: dict[str, list] = {}
        for s in sorted(skills, key=lambda s: (s.category, s.slug)):
            by_cat.setdefault(s.category, []).append(s)
        lines = [f"{len(skills)} skill(s) available "
                 f"(call skill_view(\"<name>\") to load one):", ""]
        for cat, items in sorted(by_cat.items()):
            lines.append(f"[{cat}]")
            for s in items:
                entry = f"  - {s.slug}: {s.description or '(no description)'}"
                if s.when_to_use:
                    entry += f" | use when: {s.when_to_use}"
                lines.append(entry)
            lines.append("")
        return ToolResult(tool_call_id="", name=self.name, content="\n".join(lines).strip())


class SkillViewTool(BaseTool):
    def __init__(self, store: SkillStore | None = None):
        self._store = store or default_store()

    @property
    def name(self) -> str:
        return "skill_view"

    @property
    def description(self) -> str:
        return (
            "Load a skill's full instructions by name. Pass `path` as well to read "
            "one of the skill's bundled reference files instead of the main body. "
            "Call skills_list first to see what's available."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name or slug"},
                "path": {
                    "type": "string",
                    "description": "Optional: a reference file inside the skill "
                                   "directory to read instead of the main body",
                },
            },
            "required": ["name"],
        }

    def execute(self, arguments: dict) -> ToolResult:
        name = (arguments.get("name") or "").strip()
        path = (arguments.get("path") or "").strip()
        if not name:
            return ToolResult(tool_call_id="", name=self.name,
                              content="skill_view requires a `name`.", is_error=True)

        if path:
            content, err = self._store.read_reference(name, path)
            if content is None:
                return ToolResult(tool_call_id="", name=self.name, content=err, is_error=True)
            return ToolResult(tool_call_id="", name=self.name,
                              content=f"# {name}/{path}\n\n{content}")

        skill = self._store.get(name)
        if skill is None:
            avail = ", ".join(s.slug for s in self._store.all()) or "(none)"
            return ToolResult(
                tool_call_id="", name=self.name,
                content=f"No such skill: {name}. Available: {avail}", is_error=True,
            )

        header = f"# Skill: {skill.name}"
        if skill.version:
            header += f"  (v{skill.version})"
        meta = []
        if skill.required_environment_variables:
            meta.append("Requires env vars: " + ", ".join(skill.required_environment_variables))
        # Surface bundled reference files so the model knows it can drill in.
        refs = []
        if skill.directory and skill.directory.is_dir():
            for f in sorted(skill.directory.iterdir()):
                if f.is_file() and f.name != "SKILL.md":
                    refs.append(f.name)
        parts = [header]
        if meta:
            parts.append("\n".join(meta))
        parts.append(skill.body)
        if refs:
            parts.append("Bundled files (read with skill_view(name, path=...)): "
                         + ", ".join(refs))
        return ToolResult(tool_call_id="", name=self.name, content="\n\n".join(parts))
