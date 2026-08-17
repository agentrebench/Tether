"""LLM-backed belief verification.

``BeliefManager.reverify`` refetches a stale belief's cited slices and, when a
verifier is present, asks it whether the code still supports the claim. Without
one, any belief whose citations merely *resolve* is reinforced — so a
plausible-but-wrong claim gets stronger every time it's consulted. This module
provides the real check: a single cheap yes/no completion against the refetched
source.

The verifier fails *open* (returns True) on infra errors or when no source
could be refetched — that matches the no-verifier default, so a flaky backend
degrades to today's behavior instead of demoting healthy beliefs.
"""
from __future__ import annotations

from ..models import Message

_SYSTEM = (
    "You are a code-claim verifier. You are given a claim about a codebase and "
    "the current source of the code the claim cites. Answer with exactly one "
    "word: YES if the source still supports the claim, NO if the source "
    "contradicts it or no longer matches it. Do not explain."
)


def llm_verifier(backend, *, max_slices: int = 4, max_slice_chars: int = 1500):
    """Build a ``Verifier`` (``(Belief, list[RefetchResult]) -> bool``) over an
    :class:`~tether.engine.backend.InferenceBackend`."""

    def verify(belief, results) -> bool:
        slices: list[str] = []
        for res in results[:max_slices]:
            source = (res.source or "").strip()
            if not source:
                continue
            if len(source) > max_slice_chars:
                source = source[:max_slice_chars] + "\n... (truncated)"
            where = res.citation.file
            if res.citation.symbol:
                where += f" @ {res.citation.symbol}"
            slices.append(f"--- {where} ---\n{source}")
        if not slices:
            # citations resolved but nothing refetchable to judge against
            return True

        prompt = (
            f"Claim about this codebase:\n{belief.claim}\n\n"
            f"Current source of the cited code:\n\n" + "\n\n".join(slices) +
            "\n\nDoes the source still support the claim? Answer YES or NO."
        )
        try:
            msg, _ = backend.chat_completion(
                messages=[
                    Message(role="system", content=_SYSTEM),
                    Message(role="user", content=prompt),
                ],
                max_tokens=16,
            )
            answer = (msg.content or "").strip().upper()
        except Exception:
            return True  # infra failure: fail open, never demote on a hiccup
        if answer.startswith("NO"):
            return False
        if answer.startswith("YES"):
            return True
        # Unparseable answer — treat like no verdict rather than punishing.
        return True

    return verify
