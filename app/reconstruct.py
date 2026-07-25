"""Rebuild a runnable picture of the past from spans + envelope logs.

The backend (SigNoz or the local mirror) hands over a normalised bundle:

    {"trace_id": str,
     "spans": [{span_id, parent_span_id, name, duration_ms, status,
                start_time, attributes}],
     "envelopes": [{span_id, body}]}

This module turns that into Run/Step objects. Steps whose envelope never made
it into logs are marked not replayable rather than dropped (REC-3).
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Run, Step


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except Exception:
        return str(ts or "")


def _kind(attributes: dict, name: str) -> str:
    kind = attributes.get("agent.step.kind")
    if kind:
        return str(kind)
    if attributes.get("rewind.kind") == "guardrail_failure":
        return "guardrail"
    if str(name).startswith("chat"):
        return "llm"
    return "tool"


def build_run(bundle: dict) -> Run:
    spans = list(bundle.get("spans") or [])
    envelopes = {e["span_id"]: e["body"] for e in bundle.get("envelopes") or []}

    root = None
    for span in spans:
        if not span.get("parent_span_id"):
            root = span
            break
    root = root or (spans[0] if spans else {})
    root_attrs = root.get("attributes") or {}

    run = Run(
        trace_id=bundle.get("trace_id") or root.get("trace_id", ""),
        run_id=str(root_attrs.get("agent.run.id", "")),
        agent=str(root_attrs.get("agent.name", "agent")),
        status=str(root_attrs.get("agent.run.status") or root.get("status") or "ok"),
        started_at=_iso(root.get("start_time")),
        duration_ms=float(root.get("duration_ms") or 0.0),
        total_tokens=int(root_attrs.get("agent.total_tokens", 0) or 0),
        total_cost_usd=float(root_attrs.get("agent.cost.usd", 0.0) or 0.0),
        question=str(root_attrs.get("agent.input", "")),
        answer=str(root_attrs.get("agent.output", "")),
        parent_trace_id=str(root_attrs.get("rewind.parent_trace_id", "") or ""),
        forked_from_span_id=str(root_attrs.get("rewind.forked_from_span_id", "") or ""),
        override_type=str(root_attrs.get("rewind.override_type", "") or ""),
        is_experiment=bool(root_attrs.get("rewind.experiment", False)),
    )

    steps = []
    for span in spans:
        if span is root:
            continue
        attrs = span.get("attributes") or {}
        if attrs.get("rewind.kind") == "guardrail_failure":
            run.status = "guardrail_failure"
            steps.append(
                Step(
                    span_id=span.get("span_id", ""),
                    index=9999,
                    kind="guardrail",
                    name=str(attrs.get("agent.guardrail.name") or span.get("name", "guardrail")),
                    status="error",
                    duration_ms=float(span.get("duration_ms") or 0.0),
                    replayable=False,
                    envelope={
                        "output": attrs.get("agent.guardrail.message")
                        or span.get("error", "")
                    },
                )
            )
            continue

        span_id = span.get("span_id", "")
        envelope = envelopes.get(span_id)
        steps.append(
            Step(
                span_id=span_id,
                index=int(attrs.get("agent.step.index", len(steps))),
                kind=_kind(attrs, span.get("name", "")),
                name=str(
                    attrs.get("gen_ai.tool.name")
                    or attrs.get("gen_ai.request.model")
                    or span.get("name", "step")
                ),
                duration_ms=float(span.get("duration_ms") or 0.0),
                input_tokens=int(attrs.get("gen_ai.usage.input_tokens", 0) or 0),
                output_tokens=int(attrs.get("gen_ai.usage.output_tokens", 0) or 0),
                cost_usd=float(attrs.get("agent.cost.usd", 0.0) or 0.0),
                status=str(span.get("status") or "ok"),
                source=str(attrs.get("rewind.step_source", "live")),
                model=str(attrs.get("gen_ai.request.model", "") or ""),
                replayable=envelope is not None,
                envelope=envelope,
            )
        )

    steps.sort(key=lambda s: s.index)
    run.steps = steps

    if not run.total_tokens:
        run.total_tokens = sum(s.tokens for s in steps)
    if not run.total_cost_usd:
        run.total_cost_usd = round(sum(s.cost_usd for s in steps), 8)
    if not run.duration_ms:
        run.duration_ms = round(sum(s.duration_ms for s in steps), 3)
    return run
