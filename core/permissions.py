"""Tool permission gating."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import PermissionDenial


# Internal callback result used by structured clients that need to distinguish
# an explicit No (which should cancel sibling work and ask for new direction)
# from an ordinary Stop releasing the same approval waiter. Terminal clients
# may continue returning False for a direct denial.
APPROVAL_DENIED_SIGNAL = "__tether_approval_denied__"


@dataclass
class PermissionContext:
    deny_names: frozenset[str] = field(default_factory=frozenset)
    deny_prefixes: tuple[str, ...] = ()
    auto_approve_reads: bool = True
    auto_approve_edits: bool = True
    auto_approve_bash: bool = False
    skills_write_approval: bool = True
    codebase_model_write_approval: bool = True
    session_auto_approve: set[str] = field(default_factory=set)

    def blocks(self, tool_name: str) -> PermissionDenial | None:
        lower = tool_name.lower()
        if lower in self.deny_names:
            return PermissionDenial(tool_name, f"Tool '{tool_name}' is in the deny list")
        for prefix in self.deny_prefixes:
            if lower.startswith(prefix.lower()):
                return PermissionDenial(tool_name, f"Tool '{tool_name}' matches denied prefix '{prefix}'")
        return None

    def needs_approval(self, tool_name: str) -> bool:
        lower = tool_name.lower()
        if lower in self.session_auto_approve:
            return False
        if lower in ("file_read", "glob", "grep", "job_list", "job_output", "lsp"):
            return not self.auto_approve_reads
        if lower == "todo_write":
            return False
        # Loading skill knowledge is a read — never gate it, or progressive
        # disclosure would prompt on every lookup.
        if lower in ("skills_list", "skill_view"):
            return False
        if lower in ("file_edit", "file_write"):
            return not self.auto_approve_edits
        # Authoring/patching skills is a write to ~/.tether/skills; gate it
        # only when the user opted into the write-approval review flow.
        if lower == "skill_manage":
            return self.skills_write_approval
        # Querying / checking the codebase model is a read over ~/.tether; never
        # gate it. Recording a belief/invariant is a write to the model store —
        # gate it only when the user opted into the write-approval flow.
        if lower in ("model_query", "model_check"):
            return False
        if lower == "model_record":
            return self.codebase_model_write_approval
        if lower == "bash":
            return not self.auto_approve_bash
        if lower == "job_kill":
            return False
        if lower == "agent":
            return True
        return True

    def grant_session_auto_approve(self, tool_name: str) -> None:
        self.session_auto_approve.add(tool_name.lower())

    @classmethod
    def from_config(cls, config) -> PermissionContext:
        return cls(
            deny_names=frozenset(n.lower() for n in config.deny_tools),
            deny_prefixes=tuple(config.deny_prefixes),
            auto_approve_reads=config.auto_approve_reads,
            auto_approve_edits=getattr(config, "auto_approve_edits", True),
            auto_approve_bash=config.auto_approve_bash,
            skills_write_approval=getattr(config, "skills_write_approval", True),
            codebase_model_write_approval=getattr(config, "codebase_model_write_approval", True),
        )
