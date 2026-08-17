"""Reverse clarifying-questions tool.

Lets the model fire 2-3 quick multiple-choice questions at the user BEFORE it
commits to an approach, instead of guessing wrong and burning a full run. The
actual terminal I/O is performed by an injected `ask_fn` callback (the REPL
supplies it); when no callback is available — headless runs, tests, sub-agents —
the tool degrades to telling the model to proceed with sensible defaults rather
than blocking forever on stdin.
"""
from __future__ import annotations

from ..core.models import ToolResult
from .base import BaseTool


class AskUserTool(BaseTool):
    def __init__(self, ask_fn=None):
        # ask_fn(questions) -> answers | None
        #   questions: list[{"question": str, "options": list[str]}]
        #   answers:   list[{"question": str, "answer": str}]
        self.ask_fn = ask_fn

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return (
            "Ask the user 1-3 quick multiple-choice questions to resolve ambiguity "
            "BEFORE you start working. Use this the moment a request is underspecified "
            "in a way that would change your approach — e.g. monorepo or standalone, "
            "mock the API or hit it for real, TypeScript or JavaScript, which of two "
            "files they meant. Cheap to ask, expensive to guess wrong: prefer this over "
            "picking an interpretation and running a whole task on it. Returns the user's "
            "selected answers. Do NOT use it for trivial choices you can reasonably "
            "default, for open-ended design discussion, or after work is already underway."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "Between 1 and 3 multiple-choice questions.",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The question. Keep it short and concrete.",
                            },
                            "options": {
                                "type": "array",
                                "description": (
                                    "2-4 mutually-exclusive choices. The user can also type "
                                    "a custom answer instead of picking one."
                                ),
                                "items": {"type": "string"},
                                "minItems": 2,
                                "maxItems": 4,
                            },
                        },
                        "required": ["question", "options"],
                    },
                },
            },
            "required": ["questions"],
        }

    @staticmethod
    def _normalize(raw_questions) -> list[dict]:
        """Coerce model-supplied questions into clean {question, options} dicts,
        dropping anything malformed and capping at 3."""
        normalized: list[dict] = []
        for q in (raw_questions or [])[:3]:
            if not isinstance(q, dict):
                continue
            text = str(q.get("question", "")).strip()
            options = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()]
            if text and options:
                normalized.append({"question": text, "options": options})
        return normalized

    def execute(self, arguments: dict) -> ToolResult:
        normalized = self._normalize(arguments.get("questions"))

        if not normalized:
            return ToolResult(
                name=self.name,
                content=(
                    "No valid questions were provided (each needs a question and at least "
                    "two options). Proceed using your best judgment and reasonable defaults."
                ),
                is_error=True,
            )

        # The user is reachable only during the primary turn. Sub-agents run in
        # fresh executor threads that don't carry the interactive marker, so they
        # degrade instead of trying to prompt (and colliding with the main loop).
        from ..engine.turn_controller import is_interactive_turn
        interactive = self.ask_fn is not None and is_interactive_turn()

        # No interactive user (headless, sub-agent, test). Don't hang on stdin.
        if not interactive:
            lines = [
                "[non-interactive: no user is available to answer right now] "
                "Proceed with sensible defaults for the following, and state the "
                "assumptions you made:"
            ]
            for q in normalized:
                lines.append(f"- {q['question']} (options: {', '.join(q['options'])})")
            return ToolResult(name=self.name, content="\n".join(lines), display_kind="question")

        try:
            answers = self.ask_fn(normalized)
        except (EOFError, KeyboardInterrupt):
            return ToolResult(
                name=self.name,
                content=(
                    "The user dismissed the questions without answering. Proceed with "
                    "sensible defaults and state the assumptions you made."
                ),
            )

        if not answers:
            return ToolResult(
                name=self.name,
                content=(
                    "The user did not answer. Proceed with sensible defaults and state "
                    "your assumptions."
                ),
            )

        lines = ["The user answered your clarifying questions:"]
        for a in answers:
            lines.append(f"Q: {a.get('question', '')}")
            lines.append(f"A: {a.get('answer', '')}")
        lines.append("")
        lines.append("Proceed based on these answers.")
        return ToolResult(
            name=self.name, content="\n".join(lines), display_kind="question",
            metadata={"answers": answers},
        )
