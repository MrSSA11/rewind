"""Replay envelopes must be safe to ship: redacted and size bounded."""

from rewind_sdk import envelope


def test_secrets_are_redacted():
    body = envelope.build(
        trace_id="t",
        span_id="s",
        step_index=0,
        step_kind="tool",
        tool={
            "name": "login",
            "args": {"user": "ada", "password": "hunter2", "api_key": "sk-live-1"},
            "result": "ok",
        },
    )
    args = body["tool"]["args"]
    assert args["password"] == "[REDACTED]"
    assert args["api_key"] == "[REDACTED]"
    assert args["user"] == "ada"
    assert sorted(body["redacted_fields"]) == ["tool.args.api_key", "tool.args.password"]


def test_custom_redact_keys(monkeypatch):
    monkeypatch.setenv("REWIND_REDACT_KEYS", "customer_email")
    body = envelope.build(
        trace_id="t",
        span_id="s",
        step_index=0,
        step_kind="tool",
        tool={"name": "lookup", "args": {"customer_email": "a@b.c"}, "result": ""},
    )
    assert body["tool"]["args"]["customer_email"] == "[REDACTED]"


def test_long_fields_are_truncated_and_marked(monkeypatch):
    monkeypatch.setenv("REWIND_MAX_FIELD_BYTES", "50")
    body = envelope.build(
        trace_id="t",
        span_id="s",
        step_index=1,
        step_kind="llm",
        messages=[{"role": "user", "content": "x" * 5000}],
        output="y" * 5000,
    )
    assert len(body["messages"][0]["content"]) < 200
    assert "truncated" in body["messages"][0]["content"]
    assert body["truncated_fields"]


def test_envelope_round_trips_through_the_mirror():
    from rewind_sdk import mirror

    body = envelope.build(
        trace_id="t1", span_id="s1", step_index=3, step_kind="llm", output="hello"
    )
    envelope.emit(body)
    records = [r for r in mirror.read_all() if r["type"] == "envelope"]
    assert records[-1]["body"]["output"] == "hello"
    assert records[-1]["body"]["step_index"] == 3
