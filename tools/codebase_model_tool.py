"""Agent-facing tools over the persistent codebase mental model.

Three tools, one per verb the agent needs against the model:

  - ``model_query``  (read)  — recall impact / ownership / allowance, or load the
    small architecture index. The "ask the model what it already knows" surface.
  - ``model_record`` (write) — capture a learned belief, a prescriptive
    invariant, or a rejected-pattern decision so it survives the substrate.
  - ``model_check``  (read)  — run the compiled invariants + rejected-pattern
    detectors against a proposed diff (or the whole graph) and report
    counterexamples, flagging the ones that actually block.

Everything below is a thin shell over :class:`CodebaseModel` (see
``core/codebase_model/service.py``). The shell's only jobs are argument
validation, rendering the structured results into text the model reads, and —
critically — never raising into the engine: an empty/unbuilt model or a bad
argument degrades to a helpful, non-error message. The model is resolved lazily
via :func:`get_model` so a tool instance binds to whatever repo the process is
in; tests inject a model directly.
"""
from __future__ import annotations

from ..core.codebase_model.service import get_model
from ..core.models import ToolResult
from .base import BaseTool


# Tiebreak so a tool always has *a* model to talk to: an injected one (tests),
# else the process-wide per-repo singleton.
class _ModelBoundTool(BaseTool):
    """Shared plumbing: model resolution + uniform empty/error handling."""

    def __init__(self, model=None, model_fn=None):
        # ``model`` pins a specific CodebaseModel (tests); ``model_fn`` overrides
        # how one is fetched; default is the per-repo singleton from get_model.
        self._model = model
        self._model_fn = model_fn or get_model

    def _resolve(self):
        return self._model if self._model is not None else self._model_fn()

    def _ok(self, content: str) -> ToolResult:
        return ToolResult(tool_call_id="", name=self.name, content=content)

    def _err(self, content: str) -> ToolResult:
        return ToolResult(tool_call_id="", name=self.name, content=content, is_error=True)

    @staticmethod
    def _is_empty(model) -> bool:
        """True when nothing has been learned or indexed yet — used to degrade
        gracefully instead of returning confusing empty answers."""
        s = model.store
        return (s.count_nodes() == 0 and s.count_beliefs() == 0
                and not s.all_invariants() and not s.all_decisions(include_archived=True))


# --------------------------------------------------------------------------
# read: recall what the model knows
# --------------------------------------------------------------------------
class ModelQueryTool(_ModelBoundTool):
    """Read side: impact / ownership / allowance / architecture recall."""

    _ACTIONS = ("affects", "owns", "allowed", "architecture")

    @property
    def name(self) -> str:
        return "model_query"

    @property
    def description(self) -> str:
        return (
            "Ask the persistent codebase model what it already knows, without "
            "re-reading files. Actions: 'affects' (blast radius of a symbol or "
            "*.py file — what an edit can reach), 'owns' (which component owns a "
            "topic, from learned beliefs), 'allowed' (is a described change "
            "permitted by recorded invariants / rejected patterns), and "
            "'architecture' (the small load-first index of modules, beliefs and "
            "compiled invariants). 'affects', 'owns' and 'allowed' need `target`."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(self._ACTIONS),
                    "description": "Which question to ask the model.",
                },
                "target": {
                    "type": "string",
                    "description": "The symbol/file (affects), topic (owns), or "
                                   "change description (allowed). Omit for "
                                   "'architecture'.",
                },
            },
            "required": ["action"],
        }

    def execute(self, arguments: dict) -> ToolResult:
        action = (arguments.get("action") or "").strip().lower()
        target = (arguments.get("target") or "").strip()
        if action not in self._ACTIONS:
            return self._err(
                f"Unknown action {action!r}. Use one of: {', '.join(self._ACTIONS)}.")
        try:
            model = self._resolve()
            if self._is_empty(model):
                return self._ok(
                    "The codebase model is empty (not built yet). Build it first, "
                    "or record beliefs/invariants with model_record.")
            if action == "architecture":
                return self._ok(model.architecture_index())
            if not target:
                return self._err(f"Action {action!r} requires a `target`.")
            if action == "affects":
                return self._ok(self._render_affects(model.affects(target)))
            if action == "owns":
                return self._ok(model.owns(target)["answer"])
            # allowed
            return self._ok(self._render_allowed(model.allowed(target)))
        except Exception as exc:  # never raise into the engine
            return self._err(f"model_query failed: {exc}")

    @staticmethod
    def _render_affects(res: dict) -> str:
        files = ", ".join(res["affected_files"]) or "no other files"
        lines = [f"Changing {res['target']} affects {res['count']} symbol(s) "
                 f"across: {files}."]
        for sid in res["affected_symbols"][:25]:
            lines.append(f"  - {sid}")
        more = res["count"] - 25
        if more > 0:
            lines.append(f"  ... and {more} more")
        return "\n".join(lines)

    @staticmethod
    def _render_allowed(res: dict) -> str:
        if res["allowed"]:
            return "Allowed: no blocking invariant or rejected pattern matched."
        lines = ["Not allowed — blocking findings:"]
        for v in res["violations"]:
            mark = "BLOCKING" if v["blocking"] else "soft"
            lines.append(f"  [{mark}] {v['claim']} at {v['location']}"
                         + (f" ({v['detail']})" if v["detail"] else ""))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# write: teach the model something durable
# --------------------------------------------------------------------------
class ModelRecordTool(_ModelBoundTool):
    """Write side: capture a belief / invariant / decision into the model."""

    _KINDS = ("belief", "invariant", "decision")

    @property
    def name(self) -> str:
        return "model_record"

    @property
    def description(self) -> str:
        return (
            "Teach the persistent codebase model something durable so it "
            "survives the regenerable substrate. kind='belief' records a "
            "descriptive claim (e.g. who owns what) with optional `citations` "
            "(file @ symbol) and `confidence`; kind='invariant' records a "
            "prescriptive rule with an optional `check` s-expr (e.g. "
            "'(forbidden-edge :kind calls :from plugins :to Renderer)') that "
            "makes it compiled-and-checkable; kind='decision' records a rejected "
            "pattern with a `reason`, an optional `detector` s-expr (e.g. "
            "'(construct Settings)'), and an `accepted_pattern` alternative."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(self._KINDS),
                    "description": "What to record.",
                },
                "claim": {"type": "string",
                          "description": "The belief/invariant statement (belief, invariant)."},
                "reason": {"type": "string",
                           "description": "The durable intent behind a decision (decision)."},
                "confidence": {"type": "number",
                               "description": "Optional 0..1 confidence (belief, invariant)."},
                "citations": {"type": "array", "items": {"type": "string"},
                              "description": "Optional 'file @ symbol' citations (belief)."},
                "check": {"type": "string",
                          "description": "Optional compiled-check s-expr (invariant)."},
                "detector": {"type": "string",
                             "description": "Optional detector s-expr run on a diff (decision)."},
                "accepted_pattern": {"type": "string",
                                     "description": "The constructive alternative (decision)."},
            },
            "required": ["kind"],
        }

    def execute(self, arguments: dict) -> ToolResult:
        kind = (arguments.get("kind") or "").strip().lower()
        if kind not in self._KINDS:
            return self._err(
                f"Unknown kind {kind!r}. Use one of: {', '.join(self._KINDS)}.")
        try:
            model = self._resolve()
            if kind == "belief":
                return self._record_belief(model, arguments)
            if kind == "invariant":
                return self._record_invariant(model, arguments)
            return self._record_decision(model, arguments)
        except Exception as exc:  # never raise into the engine
            return self._err(f"model_record failed: {exc}")

    # -- per-kind handlers -------------------------------------------------
    def _record_belief(self, model, arguments: dict) -> ToolResult:
        claim = (arguments.get("claim") or "").strip()
        if not claim:
            return self._err("Recording a belief requires a `claim`.")
        kw = {}
        conf = self._confidence(arguments)
        if conf is not None:
            kw["confidence"] = conf
        citations = arguments.get("citations")
        if citations:
            kw["justified_by"] = list(citations)
        belief = model.record_belief(claim, source="human", **kw)
        return self._ok(
            f"Recorded belief {belief.id!r} (confidence {belief.confidence:.2f}, "
            f"confirmations {belief.confirmations}).")

    def _record_invariant(self, model, arguments: dict) -> ToolResult:
        claim = (arguments.get("claim") or "").strip()
        if not claim:
            return self._err("Recording an invariant requires a `claim`.")
        kw = {}
        check = (arguments.get("check") or "").strip()
        if check:
            kw["check"] = check
        conf = self._confidence(arguments)
        if conf is not None:
            kw["confidence"] = conf
        inv = model.record_invariant(claim, source="human", **kw)
        how = "compiled-and-checkable" if inv.compiled else "soft (semantic, no check)"
        return self._ok(f"Recorded invariant {inv.id!r} — {how}.")

    def _record_decision(self, model, arguments: dict) -> ToolResult:
        reason = (arguments.get("reason") or "").strip()
        if not reason:
            return self._err("Recording a decision requires a `reason`.")
        dec = model.record_decision(
            reason=reason,
            detector=(arguments.get("detector") or "").strip(),
            accepted_pattern=(arguments.get("accepted_pattern") or "").strip(),
        )
        return self._ok(
            f"Recorded decision {dec.id!r} ({dec.status})"
            + (" with a compiled detector." if dec.detector else "."))

    @staticmethod
    def _confidence(arguments: dict):
        """Parse the optional `confidence`; tolerate the literal 'confirmed'
        (invariants accept it) and bad input (ignored)."""
        raw = arguments.get("confidence")
        if raw is None:
            return None
        if isinstance(raw, str) and raw.strip().lower() == "confirmed":
            return "confirmed"
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None


# --------------------------------------------------------------------------
# read: run the rules against a diff
# --------------------------------------------------------------------------
class ModelCheckTool(_ModelBoundTool):
    """Run compiled invariants + rejected-pattern detectors and report
    counterexamples, flagging blocking ones."""

    @property
    def name(self) -> str:
        return "model_check"

    @property
    def description(self) -> str:
        return (
            "Check a proposed change against the model's recorded rules. Pass "
            "`changed_files` (repo-relative paths) to run only over that diff, or "
            "omit it to check the whole graph. Runs every compiled invariant and "
            "every rejected-pattern detector and lists the counterexamples with "
            "their path:line, marking the ones that hard-block the edit."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repo-relative posix paths of the diff; omit "
                                   "to check the entire graph.",
                },
            },
        }

    def execute(self, arguments: dict) -> ToolResult:
        try:
            model = self._resolve()
            inv = model.invariants
            store = model.store
            if not store.all_invariants() and not store.all_decisions():
                return self._ok(
                    "No invariants or decisions recorded yet — nothing to check. "
                    "Record rules with model_record.")
            changed = arguments.get("changed_files")
            if changed:
                changed = list(changed)
                vios = list(inv.check_diff(changed)) + list(inv.detect_rejected(changed))
                scope = f"diff ({len(changed)} file(s))"
            else:
                vios = (list(inv.check_all())
                        + list(inv.detect_rejected(store.all_files())))
                scope = "whole graph"
            if not vios:
                return self._ok(f"No violations over the {scope}.")
            blocking = [v for v in vios if v.blocking]
            lines = [f"{len(vios)} violation(s) over the {scope} "
                     f"({len(blocking)} blocking):"]
            for v in vios:
                mark = "BLOCKING" if v.blocking else "soft"
                lines.append(f"  [{mark}] {v.claim} at {v.location}"
                             + (f" ({v.detail})" if v.detail else ""))
            return self._ok("\n".join(lines))
        except Exception as exc:  # never raise into the engine
            return self._err(f"model_check failed: {exc}")
