"""In-memory shapes Rewind rebuilds from telemetry. Rewind owns no database."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Step:
    span_id: str
    index: int
    kind: str  # llm | tool | retrieval | guardrail
    name: str
    duration_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    status: str = "ok"
    source: str = "live"  # live | restored | overridden
    model: str = ""
    replayable: bool = False
    envelope: dict = None

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def summary(self) -> str:
        if not self.envelope:
            return ""
        if self.kind == "llm":
            return str(self.envelope.get("output", ""))[:280]
        tool = self.envelope.get("tool") or {}
        return str(tool.get("result", ""))[:280]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["tokens"] = self.tokens
        return data


@dataclass
class Run:
    trace_id: str
    run_id: str = ""
    agent: str = "agent"
    status: str = "ok"
    started_at: str = ""
    duration_ms: float = 0.0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    question: str = ""
    answer: str = ""
    parent_trace_id: str = ""
    forked_from_span_id: str = ""
    override_type: str = ""
    is_experiment: bool = False
    steps: list = field(default_factory=list)

    @property
    def tool_path(self) -> list:
        return [s.name for s in self.steps if s.kind in ("tool", "retrieval")]

    @property
    def failed(self) -> bool:
        return self.status not in ("ok", "")

    def step_by_span(self, span_id: str):
        for step in self.steps:
            if step.span_id == span_id:
                return step
        return None

    def envelopes_by_index(self) -> dict:
        return {s.index: s.envelope for s in self.steps if s.envelope}

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "agent": self.agent,
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "question": self.question,
            "answer": self.answer,
            "parent_trace_id": self.parent_trace_id,
            "forked_from_span_id": self.forked_from_span_id,
            "override_type": self.override_type,
            "is_experiment": self.is_experiment,
            "tool_path": self.tool_path,
            "steps": [s.to_dict() for s in self.steps],
        }
