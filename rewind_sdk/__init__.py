"""Rewind SDK - instrument an agent once, get replayable telemetry for free.

Every step an agent takes produces three things:

1. an OpenTelemetry **span** using the GenAI semantic conventions,
2. a **replay envelope** log record holding the exact inputs of that step,
3. **metrics** for tokens, cost, tool calls, duration and errors.

Because (2) exists, any past run can be re-executed from any point. Replay is
driven by a ReplayPlan: steps before the fork are restored from telemetry,
the step at the fork gets exactly one override, and everything after it runs
live as a brand new trace linked back to its parent.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager

from . import envelope as envelope_mod
from . import llm as llm_mod
from . import mirror, tracing

try:  # Load .env if python-dotenv is installed. Optional by design.
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv()
except Exception:  # pragma: no cover - .env support is a convenience
    pass
from .tracing import GUARDRAIL_LOGGER

__all__ = ["Rewind", "ReplayPlan", "OVERRIDE_TYPES"]

#: Exactly one of these may be changed per replay, so a diff has a single cause.
OVERRIDE_TYPES = ("prompt", "model", "tool_result")


class ReplayBudgetExceeded(RuntimeError):
    pass


class ReplayPlan:
    """What to restore, where to fork, and the single thing to change."""

    def __init__(self, envelopes: dict, fork_index: int, override: dict, parent_trace_id: str = ""):
        self.envelopes = envelopes or {}
        self.fork_index = int(fork_index)
        self.override = override or {}
        self.parent_trace_id = parent_trace_id
        forked = self.envelopes.get(self.fork_index) or {}
        self.forked_from_span_id = forked.get("span_id", "")

    @property
    def override_type(self) -> str:
        return str(self.override.get("type", ""))

    @property
    def override_value(self):
        return self.override.get("value")

    def restored(self, index: int):
        """Envelope to replay from telemetry, or None if this step runs live."""
        if index < self.fork_index:
            return self.envelopes.get(index)
        return None


def _new_trace_id() -> str:
    return uuid.uuid4().hex


def _new_span_id() -> str:
    return uuid.uuid4().hex[:16]


class _Span:
    """A span that works with or without the OpenTelemetry SDK installed."""

    def __init__(self, name: str, trace_id: str = "", parent_span_id: str = "", otel_span=None):
        self.name = name
        self.otel_span = otel_span
        self.attributes: dict = {}
        self.status = "ok"
        self.error = ""
        self.start = time.time()
        self._t0 = time.perf_counter()
        self.duration_ms = 0.0

        if otel_span is not None:
            context = otel_span.get_span_context()
            # An explicit trace_id always wins. Every span of one agent run must
            # land in the same bundle even if OTel handed back a fresh context.
            self.trace_id = trace_id or format(context.trace_id, "032x")
            self.span_id = format(context.span_id, "016x")
        else:
            self.trace_id = trace_id or _new_trace_id()
            self.span_id = _new_span_id()
        self.parent_span_id = parent_span_id

    def set(self, key: str, value) -> None:
        self.attributes[key] = value
        if self.otel_span is not None:
            try:
                self.otel_span.set_attribute(key, value)
            except Exception:
                pass

    def set_many(self, values: dict) -> None:
        for key, value in (values or {}).items():
            if value is not None:
                self.set(key, value)

    def end(self, status: str = "ok", error: str = "") -> None:
        self.status = status
        self.error = error
        self.duration_ms = round((time.perf_counter() - self._t0) * 1000.0, 3)
        self.set("rewind.status", status)
        if error:
            self.set("rewind.error", error)
        if self.otel_span is not None:
            try:
                if status != "ok":
                    from opentelemetry.trace import Status, StatusCode

                    self.otel_span.set_status(Status(StatusCode.ERROR, error))
                self.otel_span.end()
            except Exception:
                pass
        mirror.record_span(
            {
                "trace_id": self.trace_id,
                "span_id": self.span_id,
                "parent_span_id": self.parent_span_id,
                "name": self.name,
                "start_time": self.start,
                "duration_ms": self.duration_ms,
                "status": self.status,
                "error": self.error,
                "attributes": self.attributes,
            }
        )


class RunContext:
    """Live state of one agent run."""

    def __init__(self, span: _Span, run_id: str, question: str, plan: ReplayPlan = None):
        self.span = span
        self.trace_id = span.trace_id
        self.run_id = run_id
        self.question = question
        self.plan = plan
        self.answer = ""
        self.status = "ok"
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self.tool_calls = 0
        self._index = -1
        self._started = time.perf_counter()
        self._max_steps = int(os.getenv("REWIND_MAX_STEPS", "40"))
        self._timeout_s = float(os.getenv("REWIND_REPLAY_TIMEOUT_S", "60"))

    def next_index(self) -> int:
        """Hand out the next step index and enforce the replay budget (REP-5)."""
        self._index += 1
        if self._index >= self._max_steps:
            raise ReplayBudgetExceeded(
                "step budget of " + str(self._max_steps) + " exceeded"
            )
        if time.perf_counter() - self._started > self._timeout_s:
            raise ReplayBudgetExceeded(
                "time budget of " + str(self._timeout_s) + "s exceeded"
            )
        return self._index

    def set_answer(self, answer: str) -> None:
        self.answer = answer

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens


class Rewind:
    """Instrumentation entry point."""

    def __init__(self, service_name: str = "rewind-demo-agent"):
        self.service_name = service_name
        tracing.setup(service_name)
        self.last_trace_id = ""

    # -------------------------------------------------------------- spans
    def _start_span(
        self,
        name: str,
        trace_id: str = "",
        parent_span_id: str = "",
        otel_parent=None,
    ):
        otel_span = None
        tracer = tracing.tracer()
        if tracer is not None:
            try:
                parent_context = None
                if otel_parent is not None:
                    from opentelemetry import trace as _otel_trace

                    parent_context = _otel_trace.set_span_in_context(otel_parent)
                otel_span = tracer.start_span(name, context=parent_context)
            except Exception:
                otel_span = None
        return _Span(name, trace_id=trace_id, parent_span_id=parent_span_id, otel_span=otel_span)

    # ---------------------------------------------------------------- run
    @contextmanager
    def run(self, name: str, input_text: str, plan: ReplayPlan = None):
        span = self._start_span("agent.run " + name)
        run_id = uuid.uuid4().hex[:12]
        context = RunContext(span, run_id, input_text, plan)
        self.last_trace_id = span.trace_id

        span.set_many(
            {
                "agent.name": name,
                "agent.run.id": run_id,
                "agent.input": input_text,
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.system": "rewind",
            }
        )
        if plan is not None:
            span.set_many(
                {
                    "rewind.experiment": True,
                    "rewind.parent_trace_id": plan.parent_trace_id,
                    "rewind.forked_from_span_id": plan.forked_from_span_id,
                    "rewind.override_type": plan.override_type,
                    "rewind.fork_index": plan.fork_index,
                }
            )
            tracing.record("replays", 1, {"override_type": plan.override_type})

        failed = False
        try:
            yield context
        except Exception as exc:
            failed = True
            context.status = "error"
            span.set("agent.error", str(exc))
            raise
        finally:
            span.set_many(
                {
                    "agent.output": context.answer,
                    "agent.run.status": context.status,
                    "agent.total_tokens": context.total_tokens,
                    "agent.cost.usd": round(context.total_cost_usd, 8),
                    "agent.tool.count": context.tool_calls,
                }
            )
            span.end(status="ok" if context.status == "ok" and not failed else "error")
            tracing.record("duration", span.duration_ms, {"agent": name})
            tracing.record("cost", context.total_cost_usd, {"agent": name})
            if context.status != "ok" or failed:
                tracing.record("errors", 1, {"agent": name, "reason": context.status})
            tracing.flush()

    # ---------------------------------------------------------- tool step
    def tool_call(self, run: RunContext, name: str, args: dict, fn):
        index = run.next_index()
        plan = run.plan
        restored = plan.restored(index) if plan else None
        span = self._start_span(
            "tool " + name, run.trace_id, run.span.span_id, otel_parent=run.span.otel_span
        )

        source = "live"
        used_args = dict(args or {})
        try:
            if restored is not None:
                source = "restored"
                tool = restored.get("tool") or {}
                used_args = tool.get("args", used_args)
                result = tool.get("result", "")
            elif plan and index == plan.fork_index and plan.override_type == "tool_result":
                source = "overridden"
                result = plan.override_value
            else:
                result = fn(**used_args)
        except Exception as exc:
            span.set_many(
                {
                    "gen_ai.tool.name": name,
                    "agent.step.index": index,
                    "agent.step.kind": "tool",
                }
            )
            span.end(status="error", error=str(exc))
            tracing.record("errors", 1, {"tool": name})
            raise

        kind = "retrieval" if "retrieve" in name or "search" in name else "tool"
        run.tool_calls += 1
        span.set_many(
            {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
                "agent.run.id": run.run_id,
                "agent.step.index": index,
                "agent.step.kind": kind,
                "agent.tool.args": json.dumps(used_args, default=str)[:1000],
                "rewind.step_source": source,
                "rewind.replayable": True,
            }
        )
        envelope_mod.emit(
            envelope_mod.build(
                trace_id=span.trace_id,
                span_id=span.span_id,
                step_index=index,
                step_kind=kind,
                tool={"name": name, "args": used_args, "result": result},
                output=result,
            )
        )
        span.end()
        tracing.record("tool_calls", 1, {"tool": name, "source": source})
        return result

    # ----------------------------------------------------------- llm step
    def llm(self, run: RunContext, messages: list, model: str = None, params: dict = None):
        index = run.next_index()
        plan = run.plan
        restored = plan.restored(index) if plan else None
        model = model or llm_mod.default_model()
        params = params or {"temperature": 0, "max_tokens": 512, "seed": 42}
        span = self._start_span(
            "chat " + model, run.trace_id, run.span.span_id, otel_parent=run.span.otel_span
        )

        source = "live"
        used_messages = [dict(m) for m in (messages or [])]

        if restored is not None:
            source = "restored"
            used_messages = restored.get("messages") or used_messages
            model = restored.get("model") or model
            usage = (restored.get("params") or {}).get("usage") or {}
            completion = {
                "text": restored.get("output", ""),
                "provider": "restored",
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
                "cost_usd": float(usage.get("cost_usd", 0.0) or 0.0),
            }
        else:
            if plan and index == plan.fork_index:
                if plan.override_type == "prompt":
                    source = "overridden"
                    if used_messages:
                        used_messages[-1] = dict(used_messages[-1])
                        used_messages[-1]["content"] = plan.override_value
                    else:
                        used_messages = [{"role": "user", "content": plan.override_value}]
                elif plan.override_type == "model":
                    source = "overridden"
                    model = str(plan.override_value)
                    span.name = "chat " + model
            completion = llm_mod.complete(used_messages, model=model, params=params)

        run.total_input_tokens += completion["input_tokens"]
        run.total_output_tokens += completion["output_tokens"]
        run.total_cost_usd += completion["cost_usd"]

        span.set_many(
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.system": completion["provider"],
                "gen_ai.request.model": model,
                "gen_ai.request.temperature": params.get("temperature", 0),
                "gen_ai.request.max_tokens": params.get("max_tokens", 512),
                "gen_ai.usage.input_tokens": completion["input_tokens"],
                "gen_ai.usage.output_tokens": completion["output_tokens"],
                "agent.cost.usd": completion["cost_usd"],
                "agent.run.id": run.run_id,
                "agent.step.index": index,
                "agent.step.kind": "llm",
                "rewind.step_source": source,
                "rewind.replayable": True,
            }
        )
        envelope_mod.emit(
            envelope_mod.build(
                trace_id=span.trace_id,
                span_id=span.span_id,
                step_index=index,
                step_kind="llm",
                model=model,
                params={
                    "temperature": params.get("temperature", 0),
                    "max_tokens": params.get("max_tokens", 512),
                    "seed": params.get("seed", 42),
                    "usage": {
                        "input_tokens": completion["input_tokens"],
                        "output_tokens": completion["output_tokens"],
                        "cost_usd": completion["cost_usd"],
                    },
                },
                messages=used_messages,
                output=completion["text"],
            )
        )
        span.end()
        tracing.record(
            "tokens",
            completion["input_tokens"] + completion["output_tokens"],
            {"model": model, "provider": completion["provider"]},
        )
        return completion["text"]

    # ---------------------------------------------------------- guardrail
    def guardrail(self, run: RunContext, name: str, passed: bool, message: str = ""):
        """Ground-truth check. A failure is what SigNoz alerts on."""
        if passed:
            return True
        span = self._start_span(
            "guardrail " + name,
            run.trace_id,
            run.span.span_id,
            otel_parent=run.span.otel_span,
        )
        span.set_many(
            {
                "rewind.kind": "guardrail_failure",
                "agent.run.id": run.run_id,
                "agent.guardrail.name": name,
                "agent.guardrail.message": message,
                "rewind.replayable": False,
            }
        )
        span.end(status="error", error=message)
        run.status = "guardrail_failure"
        run.span.set("agent.run.status", "guardrail_failure")
        logging.getLogger(GUARDRAIL_LOGGER).error(
            json.dumps(
                {
                    "rewind.kind": "guardrail_failure",
                    "guardrail": name,
                    "message": message,
                    "trace_id": run.trace_id,
                    "run_id": run.run_id,
                }
            ),
            extra={
                "rewind.kind": "guardrail_failure",
                "trace_id": run.trace_id,
                "span_id": span.span_id,
            },
        )
        tracing.record("errors", 1, {"reason": "guardrail", "guardrail": name})
        return False
