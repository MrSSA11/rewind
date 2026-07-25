"""Fork execution: restore upstream state, change one input, run forward."""

from __future__ import annotations

from rewind_sdk import OVERRIDE_TYPES, ReplayPlan, Rewind

from .models import Run


class ReplayError(RuntimeError):
    pass


def build_plan(run: Run, span_id: str, override: dict) -> ReplayPlan:
    step = run.step_by_span(span_id)
    if step is None:
        raise ReplayError("step " + str(span_id) + " is not part of run " + run.trace_id)
    if not step.replayable:
        raise ReplayError(
            "step " + step.name + " has no replay envelope, so it cannot be forked"
        )

    override_type = (override or {}).get("type")
    if override_type not in OVERRIDE_TYPES:
        raise ReplayError("override.type must be one of " + ", ".join(OVERRIDE_TYPES))
    if override_type == "tool_result" and step.kind not in ("tool", "retrieval"):
        raise ReplayError("tool_result overrides only apply to tool steps")
    if override_type in ("prompt", "model") and step.kind != "llm":
        raise ReplayError(override_type + " overrides only apply to LLM steps")
    if not str((override or {}).get("value", "")).strip():
        raise ReplayError("override.value cannot be empty")

    return ReplayPlan(
        envelopes=run.envelopes_by_index(),
        fork_index=step.index,
        override=override,
        parent_trace_id=run.trace_id,
    )


def execute(run: Run, span_id: str, override: dict) -> str:
    """Run the fork and return the new trace id."""
    from demo_agent.agent import run_agent

    plan = build_plan(run, span_id, override)
    rw = Rewind(service_name="rewind-demo-agent")
    run_agent(run.question or "", rw=rw, plan=plan)
    if not rw.last_trace_id:
        raise ReplayError("replay produced no trace")
    return rw.last_trace_id


def dry_run(run: Run, span_id: str, override: dict) -> dict:
    """Show what would be replayed without executing anything (REP-6)."""
    plan = build_plan(run, span_id, override)
    return {
        "fork_index": plan.fork_index,
        "forked_from_span_id": plan.forked_from_span_id,
        "restored_steps": sorted(
            index for index in plan.envelopes if index < plan.fork_index
        ),
        "override": plan.override,
        "parent_trace_id": plan.parent_trace_id,
    }
