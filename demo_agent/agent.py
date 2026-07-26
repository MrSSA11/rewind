"""A five-step refund-triage agent with a realistic, planted bug.

Steps:

  0  retrieve_policy_docs   retrieval - returns the STALE 90 day policy
  1  lookup_order           tool      - the order is 60 days old
  2  llm: decide            reads 90 days, wrongly approves
  3  refund_api.approve     tool      - only runs if the model approved
  4  llm: final answer      tells the customer the good news

A ground-truth guardrail then compares the decision against the real 14 day
policy, fails the run, and emits the log record SigNoz alerts on.

Nothing here looks broken in a conventional trace: no exception, no HTTP 500,
every span green. That is exactly why replay is needed.
"""

from __future__ import annotations

from rewind_sdk import Rewind

from . import tools

DEFAULT_QUESTION = (
    "Customer is asking for a refund on order A-10492. Should we approve it?"
)

SYSTEM_PROMPT = (
    "You are a customer support agent. Apply the refund policy exactly as "
    "written in the retrieved document. Reply with DECISION: APPROVE or "
    "DECISION: DENY followed by a one line reason."
)

# Phrases where the word APPROVE appears but the model is refusing. A naive
# substring check reads "I cannot approve this refund" as an approval, which
# silently inverts the whole demo once a real model is answering.
NEGATIONS = (
    "CANNOT APPROVE",
    "CAN NOT APPROVE",
    "CAN'T APPROVE",
    "NOT APPROVE",
    "NOT APPROVED",
    "UNABLE TO APPROVE",
    "DO NOT APPROVE",
    "DON'T APPROVE",
    "WILL NOT APPROVE",
    "WON'T APPROVE",
    "SHOULD NOT APPROVE",
    "NO APPROVAL",
    "WITHOUT APPROVAL",
)

REFUSALS = ("DENY", "DENIED", "DECLINE", "DECLINED", "REJECT", "REJECTED")


def _negated(text: str) -> bool:
    return any(phrase in text for phrase in NEGATIONS)


def parse_decision(text: str) -> bool:
    """True when the model approved the refund.

    Prefers an explicit "DECISION:" line, because that is what the system
    prompt asks for. Falls back to scanning the whole reply, ignoring negated
    forms of "approve". Real models pad their answers with prose, so this has
    to survive markdown bullets, bold markers, and trailing reasoning.
    """
    upper = str(text).upper()

    for raw_line in upper.splitlines():
        line = raw_line.strip().strip("*_#-> ").strip()
        if not line.startswith("DECISION"):
            continue
        _, _, verdict = line.partition(":")
        verdict = verdict.strip() or line
        if any(word in verdict for word in REFUSALS):
            return False
        if "APPROVE" in verdict and not _negated(verdict):
            return True

    if any(word in upper for word in REFUSALS):
        return False
    return "APPROVE" in upper and not _negated(upper)


def run_agent(question: str = DEFAULT_QUESTION, rw: Rewind = None, plan=None) -> dict:
    """Execute the agent once. Pass a ReplayPlan to run it as a fork."""
    rw = rw or Rewind(service_name="rewind-demo-agent")

    with rw.run("refund-triage", question, plan=plan) as run:
        policy = rw.tool_call(
            run,
            "retrieve_policy_docs",
            {"query": "refund window for delivered orders"},
            tools.retrieve_policy_docs,
        )
        order = rw.tool_call(
            run, "lookup_order", {"order_id": "A-10492"}, tools.lookup_order
        )

        decision = rw.llm(
            run,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Policy document:\n" + str(policy) + "\n\n"
                        + "Order record:\n" + str(order) + "\n\n"
                        + "Customer request: " + question + "\n"
                        + "Decide whether to approve the refund."
                    ),
                },
            ],
        )

        approved = parse_decision(decision)
        refund_receipt = ""
        if approved:
            refund_receipt = rw.tool_call(
                run,
                "refund_api.approve",
                {"order_id": "A-10492", "amount_usd": 249.00},
                tools.approve_refund,
            )

        answer = rw.llm(
            run,
            messages=[
                {"role": "system", "content": "Write a short reply to the customer."},
                {
                    "role": "user",
                    "content": (
                        "Policy document:\n" + str(policy) + "\n\n"
                        + "Order record:\n" + str(order) + "\n\n"
                        + "Agent decision:\n" + str(decision) + "\n\n"
                        + ("Refund system response:\n" + str(refund_receipt) + "\n\n" if refund_receipt else "")
                        + "Write the final answer for the customer."
                    ),
                },
            ],
        )
        run.set_answer(answer)

        # Ground truth: the real policy window is 14 days, whatever the
        # retriever believes. This is the check that catches the bug.
        age_days = tools.order_age_days("A-10492")
        rw.guardrail(
            run,
            "refund_policy_window",
            passed=not (approved and age_days > tools.TRUE_REFUND_WINDOW_DAYS),
            message=(
                "Refund approved for an order " + str(age_days) + " days old, but the "
                "current policy window is only " + str(tools.TRUE_REFUND_WINDOW_DAYS)
                + " days. The retrieved policy document was out of date."
            ),
        )

        return {
            "trace_id": run.trace_id,
            "run_id": run.run_id,
            "approved": approved,
            "decision": decision,
            "answer": answer,
            "status": run.status,
            "total_tokens": run.total_tokens,
            "total_cost_usd": run.total_cost_usd,
        }
