"""Generate the demo run so Rewind is never a blank screen."""

from __future__ import annotations

import sys

from .agent import DEFAULT_QUESTION, run_agent


def seed(question: str = DEFAULT_QUESTION) -> dict:
    return run_agent(question)


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or DEFAULT_QUESTION
    out = seed(question)
    print("seeded run")
    print("  trace_id : " + out["trace_id"])
    print("  approved : " + str(out["approved"]))
    print("  status   : " + out["status"])
    print("  answer   : " + str(out["answer"])[:160])
