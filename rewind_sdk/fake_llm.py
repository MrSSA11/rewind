"""A tiny deterministic stand-in for a real model.

Why this exists: a replay debugger has to be demonstrably deterministic, judges
should not need an API key to run the project, and the tests must be hermetic.
FAKE_LLM=1 is the default for exactly those reasons.

The rules are intentionally simple and legible - it reads a refund window and an
order age out of the prompt and applies the policy.
"""

from __future__ import annotations

import re

_WINDOW = re.compile(r"refund window is\s+(\d+)\s*days", re.IGNORECASE)
_AGE = re.compile(r"order is\s+(\d+)\s*days old", re.IGNORECASE)


def count_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _first_int(pattern, text, default):
    match = pattern.search(text or "")
    return int(match.group(1)) if match else default


def complete(prompt: str) -> str:
    lowered = (prompt or "").lower()
    window = _first_int(_WINDOW, prompt, 30)
    age = _first_int(_AGE, prompt, 0)

    if "decide" in lowered:
        if age <= window:
            return (
                "DECISION: APPROVE\n"
                "REASON: the order is " + str(age) + " days old, which is inside the "
                + str(window) + " day refund window stated in the policy."
            )
        return (
            "DECISION: DENY\n"
            "REASON: the order is " + str(age) + " days old, which is outside the "
            + str(window) + " day refund window stated in the policy."
        )

    if "final answer" in lowered:
        if "approve" in lowered:
            return (
                "Good news - your refund for order A-10492 has been approved and the "
                "amount will be back on your original payment method within 5 working days."
            )
        return (
            "Thanks for reaching out. Order A-10492 is " + str(age) + " days old, and our "
            "refund window is " + str(window) + " days, so I am not able to approve a refund "
            "this time. I can offer store credit or a repair instead."
        )

    return "ACKNOWLEDGED"
