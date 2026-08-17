"""Base tool interface for Tether."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.models import ToolDefinition, ToolResult


class BaseTool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def parameters(self) -> dict: ...

    @abstractmethod
    def execute(self, arguments: dict) -> ToolResult: ...

    def execute_with_cancel(self, arguments: dict, cancel_event=None) -> ToolResult:
        """Execute honoring a threading.Event for user interruption.

        Default ignores the event — only tools that can block for a long time
        (bash, agent) override this. Callers should prefer this entrypoint so
        cancellation reaches tools that support it.
        """
        return self.execute(arguments)

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


@dataclass
class ToolRegistry:
    tools: dict[str, BaseTool]
    close_callbacks: list[Callable[[], None]] | None = None

    def get(self, name: str) -> BaseTool | None:
        return self.tools.get(name) or self.tools.get(name.lower())

    def definitions(self) -> list[ToolDefinition]:
        return [t.to_definition() for t in self.tools.values()]

    def names(self) -> list[str]:
        return list(self.tools.keys())

    def close(self) -> None:
        for callback in self.close_callbacks or []:
            callback()

    @classmethod
    def build_default(
        cls,
        include_codebase_model: bool = False,
        *,
        workspace_root: str | Path | None = None,
        enforce_workspace: bool = False,
    ) -> ToolRegistry:
        from .bash import BashTool
        from .file_read import FileReadTool
        from .file_edit import FileEditTool
        from .file_write import FileWriteTool
        from .glob_tool import GlobTool
        from .grep_tool import GrepTool
        from .web_fetch import WebFetchTool
        from .jobs import JobKillTool, JobListTool, JobOutputTool, JobRegistry
        from .todo import TodoWriteTool
        from .lsp import LspTool
        from .workspace import WorkspacePolicy

        workspace = WorkspacePolicy(
            Path(workspace_root) if workspace_root is not None else None,
            enforced=enforce_workspace,
        )
        jobs = JobRegistry()

        tools_list = [
            BashTool(workspace, jobs),
            FileReadTool(workspace),
            FileEditTool(workspace=workspace),
            FileWriteTool(workspace),
            GlobTool(workspace),
            GrepTool(workspace),
            WebFetchTool(),
            JobListTool(jobs),
            JobOutputTool(jobs),
            JobKillTool(jobs),
            TodoWriteTool(),
            LspTool(workspace),
        ]
        registry = cls(tools={t.name: t for t in tools_list}, close_callbacks=[jobs.close])

        # Skills: progressive-disclosure knowledge + procedural memory. Added
        # here so every engine (REPL, CLI, sub-agents) can load and author
        # skills. They share the process-wide store, so writes are seen live.
        from .skills_tool import SkillsListTool, SkillViewTool
        from .skill_manage import SkillManageTool
        registry.tools["skills_list"] = SkillsListTool(
            toolsets_fn=lambda: set(registry.names())
        )
        registry.tools["skill_view"] = SkillViewTool()
        registry.tools["skill_manage"] = SkillManageTool()

        # Persistent codebase mental model (opt-in via config). Registered only
        # when enabled so a default session pays nothing and never creates a
        # model store it won't use. The tools resolve their per-repo model lazily.
        if include_codebase_model:
            from .codebase_model_tool import (
                ModelCheckTool, ModelQueryTool, ModelRecordTool)
            registry.tools["model_query"] = ModelQueryTool()
            registry.tools["model_record"] = ModelRecordTool()
            registry.tools["model_check"] = ModelCheckTool()
        return registry
