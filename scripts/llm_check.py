"""Check the real model before you rely on it.

Answers two questions:

  1. Is the model actually reachable, and what does it cost per call?
  2. Is the demo deterministic? The whole story depends on the model
     approving the refund when it reads the STALE policy and refusing when it
     reads the CURRENT one. A real model has to do that every single time or
     the demo is a coin flip.

Run:

  python scripts/llm_check.py
  LLM_CHECK_ROUNDS=5 python scripts/llm_check.py

Exits non-zero if the model is unreachable or the decision is not stable.
No telemetry is written, so this is safe to run as often as you like.
"""

from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from demo_agent import agent as demo  # noqa: E402
from demo_agent import tools  # noqa: E402
from rewind_sdk import llm  # noqa: E402


def line(label: str, value) -> None:
    print(str(label).ljust(20) + str(value))


def decide_messages(policy: str) -> list:
    """The exact prompt the agent's decide step sends."""
    order = tools.lookup_order(order_id="A-10492")
    return [
        {"role": "system", "content": demo.SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Policy document:\n" + str(policy) + "\n\n"
                + "Order record:\n" + str(order) + "\n\n"
                + "Customer request: " + demo.DEFAULT_QUESTION + "\n"
                + "Decide whether to approve the refund."
            ),
        },
    ]


def main() -> int:
    key = llm.api_key()
    which = llm.provider()

    print("=== configuration ===")
    line("provider", which)
    line("model", llm.default_model())
    line("api key set", "yes, " + str(len(key)) + " chars" if key else "no")
    line("fake_llm env", os.getenv("FAKE_LLM", "(unset, defaults to 1)"))
    line("timeout", os.getenv("REWIND_LLM_TIMEOUT", "30"))

    if which == "fake":
        print("")
        print("Running the offline model. Nothing to check against a real API.")
        print("To use Google AI Studio, put this in .env:")
        print("  FAKE_LLM=0")
        print("  GEMINI_API_KEY=your-key")
        print("  REWIND_MODEL=gemma-4-31b-it")
        return 0

    print("")
    print("=== one real call ===")
    try:
        result = llm.complete([{"role": "user", "content": "Reply with the word OK."}])
    except Exception as exc:
        print("FAILED: " + type(exc).__name__ + ": " + str(exc)[:500])
        print("")
        traceback.print_exc()
        print("")
        print("401 or 403  -> the key is wrong. Make a new one at")
        print("               https://aistudio.google.com/apikey")
        print("404         -> the model name is wrong for this provider.")
        print("429         -> free tier rate limit. Wait a minute and retry.")
        return 1

    line("provider used", result["provider"])
    line("reply", str(result["text"])[:80])
    line("input tokens", result["input_tokens"])
    line("output tokens", result["output_tokens"])
    line("cost usd", result["cost_usd"])

    rounds = max(1, int(os.getenv("LLM_CHECK_ROUNDS", "3")))
    print("")
    print("=== is the demo deterministic? " + str(rounds) + " rounds each ===")

    cases = (
        ("stale policy (90 days)", tools.STALE_POLICY, True),
        ("current policy (14 days)", tools.CURRENT_POLICY, False),
    )

    stable = True
    for label, policy, expected in cases:
        outcomes = []
        for _ in range(rounds):
            try:
                reply = llm.complete(decide_messages(policy))["text"]
            except Exception as exc:
                print(label + ": call failed: " + str(exc)[:200])
                stable = False
                break
            outcomes.append(demo.parse_decision(reply))
        else:
            approvals = sum(1 for value in outcomes if value)
            wanted = "approve" if expected else "refuse"
            got = str(approvals) + "/" + str(rounds) + " approved"
            ok = all(value is expected for value in outcomes)
            stable = stable and ok
            print(("PASS  " if ok else "FLAKY ") + label.ljust(26)
                  + "expected " + wanted + ", got " + got)

    print("")
    print("=== verdict ===")
    if stable:
        print("The real model is reliable for this demo. Record with it.")
        return 0

    print("The real model is NOT reliable for this demo.")
    print("Record with the offline model instead:")
    print("  sed -i 's/^FAKE_LLM=0/FAKE_LLM=1/' .env && make clean && make demo")
    print("Everything else stays real: the traces, the replay, and the diff.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
