"""The diff engine is what turns a replay into an answer, so it is tested directly."""

from app.diff import answer_diff, compare, divergence_index
from app.models import Run, Step


def _run(trace_id, answer, status="ok", outputs=("a", "b"), cost=0.01, tokens=100):
    steps = [
        Step(
            span_id=trace_id + str(i),
            index=i,
            kind="tool",
            name="tool_" + str(i),
            envelope={"output": output},
            cost_usd=cost / 2,
            output_tokens=tokens // 2,
        )
        for i, output in enumerate(outputs)
    ]
    return Run(
        trace_id=trace_id,
        answer=answer,
        status=status,
        total_cost_usd=cost,
        total_tokens=tokens,
        duration_ms=1000,
        steps=steps,
    )


def test_reports_a_fix_when_the_fork_passes():
    original = _run("a", "Refund approved.", status="guardrail_failure")
    fork = _run("b", "Refund denied.", status="ok")
    result = compare(original, fork)
    assert result["status"]["improved"] is True
    assert result["answers"]["changed"] is True


def test_metric_deltas_carry_direction():
    original = _run("a", "x", cost=0.02, tokens=200)
    fork = _run("b", "y", cost=0.01, tokens=100)
    metrics = compare(original, fork)["metrics"]
    assert metrics["cost_usd"]["direction"] == "down"
    assert metrics["tokens"]["change"] == -100
    assert metrics["tokens"]["pct"] == -50.0


def test_divergence_index_finds_the_first_disagreement():
    original = _run("a", "x", outputs=("same", "old"))
    fork = _run("b", "y", outputs=("same", "new"))
    assert divergence_index(original, fork) == 1


def test_identical_runs_do_not_diverge():
    original = _run("a", "x", outputs=("same", "same"))
    fork = _run("b", "x", outputs=("same", "same"))
    assert divergence_index(original, fork) is None


def test_answer_diff_marks_added_and_removed_words():
    original = _run("a", "your refund has been approved")
    fork = _run("b", "your refund has been denied")
    ops = {chunk["op"] for chunk in answer_diff(original, fork)}
    assert "removed" in ops and "added" in ops
