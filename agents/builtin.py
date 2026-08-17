"""Built-in agent definitions for Tether."""
from __future__ import annotations

import os

from .base import AgentDefinition

EXPLORE_AGENT = AgentDefinition(
    name="explore",
    description="Fast agent for exploring codebases. Use for finding files, searching code, and answering questions about project structure.",
    system_prompt=f"""You are an Explore agent for Tether. Your job is to quickly explore a codebase and return findings.

You have access to file reading, glob, and grep tools. Use them efficiently:
- Start with glob to find files by pattern
- Use grep to search content
- Use file_read to examine specific files

Be thorough but fast. Keep reasoning private and return a concise summary of what you found.

Working directory: {os.getcwd()}""",
    allowed_tools=["file_read", "glob", "grep"],
    max_turns=15,
)

PLAN_AGENT = AgentDefinition(
    name="plan",
    description="Architecture agent for designing implementation plans. Returns step-by-step plans with file identification.",
    system_prompt=f"""You are a Plan agent for Tether. Your job is to analyze a task and produce an implementation plan.

You can read files and search the codebase to understand the current state, then produce:
1. A list of files that need to be created or modified
2. Step-by-step implementation plan
3. Key architectural decisions and trade-offs
4. Potential risks or blockers

Do NOT make changes. Only read and plan.
Keep reasoning private. Return only the plan and concrete findings.

Working directory: {os.getcwd()}""",
    allowed_tools=["file_read", "glob", "grep"],
    max_turns=15,
)

GENERAL_AGENT = AgentDefinition(
    name="general",
    description="General-purpose agent for complex multi-step tasks. Has full tool access.",
    system_prompt=f"""You are a General-purpose sub-agent for Tether. You handle complex tasks that require multiple steps.

You have full tool access. Complete the task autonomously and return the result.
Keep reasoning private. Return concise progress and results, not chain-of-thought.

Working directory: {os.getcwd()}""",
    allowed_tools=[],  # all tools
    max_turns=30,
)

BUILTIN_AGENTS = {
    "explore": EXPLORE_AGENT,
    "plan": PLAN_AGENT,
    "general": GENERAL_AGENT,
}
