"""Compare an original run with one of its forks."""

from __future__ import annotations

import difflib

from .models import Run


def _delta(before: float, after: float) -> dict:
    change = round(after - before, 8)
    pct = round((change / before) * 100, 1) if before else None
    if change < 0:
        direction = "down"
    elif change > 0:
        direction = "up"
    else:
        direction = "flat"
    return {
        "before": before,
        "after": after,
        "change": change,
        "pct": pct,
        "direction": direction,
    }


def divergence_index(a: Run, b: Run):
    """First step index where the two runs stop agreeing (DIF-4)."""
    a_steps = [s for s in a.steps if s.kind != "guardrail"]
    b_steps = [s for s in b.steps if s.kind != "guardrail"]
    for i in range(max(len(a_steps), len(b_steps))):
        if i >= len(a_steps) or i >= len(b_steps):
            return i
        left, right = a_steps[i], b_steps[i]
        if left.name != right.name or left.kind != right.kind:
            return i
        if (left.envelope or {}).get("output") != (right.envelope or {}).get("output"):
            return i
    return None


def answer_diff(a: Run, b: Run) -> list:
    """Word-level diff of the two final answers (DIF-5)."""
    left = (a.answer or "").split()
    right = (b.answer or "").split()
    out = []
    matcher = difflib.SequenceMatcher(None, left, right)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.append({"op": "same", "text": " ".join(left[i1:i2])})
        else:
            if i1 != i2:
                out.append({"op": "removed", "text": " ".join(left[i1:i2])})
            if j1 != j2:
                out.append({"op": "added", "text": " ".join(right[j1:j2])})
    return [chunk for chunk in out if chunk["text"]]


def compare(a: Run, b: Run) -> dict:
    return {
        "a": a.to_dict(),
        "b": b.to_dict(),
        "answers": {"a": a.answer, "b": b.answer, "changed": a.answer != b.answer},
        "metrics": {
            "latency_ms": _delta(a.duration_ms, b.duration_ms),
            "tokens": _delta(a.total_tokens, b.total_tokens),
            "cost_usd": _delta(a.total_cost_usd, b.total_cost_usd),
            "steps": _delta(len(a.steps), len(b.steps)),
        },
        "tool_path": {
            "a": a.tool_path,
            "b": b.tool_path,
            "changed": a.tool_path != b.tool_path,
        },
        "status": {"a": a.status, "b": b.status, "improved": a.failed and not b.failed},
        "divergence_index": divergence_index(a, b),
        "answer_diff": answer_diff(a, b),
        "override_type": b.override_type,
    }
