"""Local stub tools for the demo agent.

Every tool is a pure local function with no real side effect, so a replay can
never charge a customer or mutate anything outside this process.

The planted bug lives here: the retriever's index was never re-embedded after
the refund policy changed, so it happily returns the archived 90-day policy.
"""

from __future__ import annotations

# The real, current policy. The retriever does not know about it yet.
TRUE_REFUND_WINDOW_DAYS = 14

STALE_POLICY = (
    "Refunds Policy (v1, archived 2023): customers may request a refund for any "
    "reason. The refund window is 90 days from the delivery date. Approvals are "
    "automatic for orders under $500."
)

CURRENT_POLICY = (
    "Refunds Policy (v3, current): the refund window is 14 days from the delivery "
    "date. Outside that window agents may offer store credit or a repair."
)

ORDERS = {
    "A-10492": {
        "order_id": "A-10492",
        "age_days": 60,
        "total_usd": 249.00,
        "item": "Aurora wireless headphones",
        "status": "delivered",
    }
}


def retrieve_policy_docs(query: str = "refund window") -> str:
    """Vector search over the policy knowledge base.

    BUG: the index still holds the archived v1 document. Nothing here errors,
    nothing looks wrong in a log line - the agent is simply told the wrong rule.
    """
    return STALE_POLICY


def lookup_order(order_id: str = "A-10492") -> str:
    order = ORDERS.get(order_id)
    if not order:
        return "No order found with id " + str(order_id) + "."
    return (
        "Order " + order["order_id"] + ": " + order["item"] + ", $"
        + str(order["total_usd"]) + ", status " + order["status"] + ". The order is "
        + str(order["age_days"]) + " days old."
    )


def approve_refund(order_id: str = "A-10492", amount_usd: float = 249.00) -> str:
    """Stub payment call - records the intent, moves no money."""
    return (
        "Refund of $" + str(amount_usd) + " approved for order " + str(order_id)
        + ". Reference RF-" + str(order_id).replace("-", "") + "."
    )


def order_age_days(order_id: str = "A-10492") -> int:
    order = ORDERS.get(order_id) or {}
    return int(order.get("age_days", 0))
