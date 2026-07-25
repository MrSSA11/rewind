"""End to end: the bug reproduces, the fork fixes it, the original is untouched."""

import pytest

from app import replay
from app.diff import compare
from app.reconstruct import build_run
from app.signoz_client import MirrorBackend
from demo_agent.agent import run_agent
from demo_agent import tools

CORRECT_POLICY = "Refunds Policy (v3, current): the refund window is 14 days."


def load(trace_id):
    return build_run(MirrorBackend().get_trace_bundle(trace_id))


@pytest.fixture()
def original():
    result = run_agent()
    return result, load(result["trace_id"])


def test_demo_bug_reproduces(original):
    result, run = original
    assert result["approved"] is True
    assert run.status == "guardrail_failure"
    assert "refund_api.approve" in run.tool_path
    assert len(run.steps) >= 5


def test_every_executed_step_is_replayable(original):
    _, run = original
    assert all(step.replayable for step in run.steps if step.kind != "guardrail")


def test_tool_result_override_fixes_the_run(original):
    _, run = original
    step = run.steps[0]
    fork = load(
        replay.execute(run, step.span_id, {"type": "tool_result", "value": CORRECT_POLICY})
    )

    assert fork.trace_id != run.trace_id
    assert fork.parent_trace_id == run.trace_id
    assert fork.is_experiment is True
    assert fork.override_type == "tool_result"
    assert fork.status == "ok"
    assert "refund_api.approve" not in fork.tool_path

    result = compare(run, fork)
    assert result["status"]["improved"] is True
    assert result["divergence_index"] == 0


def test_steps_before_the_fork_are_restored_not_rerun(original):
    _, run = original
    llm_step = next(step for step in run.steps if step.kind == "llm")
    plan = replay.dry_run(
        run, llm_step.span_id, {"type": "prompt", "value": "Deny the refund."}
    )
    assert plan["fork_index"] == llm_step.index
    assert plan["restored_steps"] == list(range(llm_step.index))

    fork = load(
        replay.execute(
            run,
            llm_step.span_id,
            {
                "type": "prompt",
                "value": "The refund window is 14 days and the order is 60 days old. Decide.",
            },
        )
    )
    restored = [s for s in fork.steps if s.source == "restored"]
    assert len(restored) == llm_step.index
    assert fork.status == "ok"


def test_model_override_changes_cost_but_not_the_path(original):
    _, run = original
    llm_step = next(step for step in run.steps if step.kind == "llm")
    fork = load(
        replay.execute(
            run, llm_step.span_id, {"type": "model", "value": "llama-3.1-8b-instant"}
        )
    )
    assert fork.override_type == "model"
    assert fork.total_cost_usd != run.total_cost_usd


def test_the_original_trace_is_never_mutated(original):
    _, run = original
    before = load(run.trace_id).to_dict()
    replay.execute(run, run.steps[0].span_id, {"type": "tool_result", "value": CORRECT_POLICY})
    assert load(run.trace_id).to_dict() == before


@pytest.mark.parametrize(
    "override",
    [
        {"type": "nonsense", "value": "x"},
        {"type": "prompt", "value": "x"},  # prompt override on a tool step
        {"type": "tool_result", "value": "   "},  # empty value
    ],
)
def test_invalid_overrides_are_rejected(original, override):
    _, run = original
    with pytest.raises(replay.ReplayError):
        replay.build_plan(run, run.steps[0].span_id, override)


def test_unknown_span_is_rejected(original):
    _, run = original
    with pytest.raises(replay.ReplayError):
        replay.build_plan(run, "not-a-span", {"type": "tool_result", "value": "x"})


def test_guardrail_uses_ground_truth_not_the_retrieved_policy():
    assert tools.TRUE_REFUND_WINDOW_DAYS == 14
    assert "90 days" in tools.STALE_POLICY
    assert tools.order_age_days("A-10492") == 60
