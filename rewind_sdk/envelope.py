"""Replay envelopes: the extra thing Rewind records that makes a trace runnable.

An envelope is emitted as an OpenTelemetry *log record* correlated to its span
by trace id and span id. It holds everything needed to re-execute one step:
messages, model, parameters, tool args and tool results.

Secrets are redacted and oversized fields are truncated - and both facts are
recorded in the envelope, so nobody is fooled by a silently shortened prompt.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

from . import mirror
from .tracing import ENVELOPE_LOGGER

DEFAULT_REDACT_KEYS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credit_card",
    "card_number",
    "cvv",
    "ssn",
    "access_key",
    "private_key",
)

REDACTED = "[REDACTED]"


def redact_keys() -> tuple:
    extra = os.getenv("REWIND_REDACT_KEYS", "")
    custom = tuple(k.strip().lower() for k in extra.split(",") if k.strip())
    return DEFAULT_REDACT_KEYS + custom


def max_field_bytes() -> int:
    try:
        return int(os.getenv("REWIND_MAX_FIELD_BYTES", "8000"))
    except ValueError:
        return 8000


def redact(value, path: str, found: list):
    """Walk a structure and mask anything that looks like a credential."""
    keys = redact_keys()
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            child = path + "." + str(key) if path else str(key)
            if str(key).lower() in keys:
                out[key] = REDACTED
                found.append(child)
            else:
                out[key] = redact(item, child, found)
        return out
    if isinstance(value, list):
        return [
            redact(item, path + "[" + str(index) + "]", found)
            for index, item in enumerate(value)
        ]
    return value


def truncate(value, path: str, found: list, limit: int):
    """Clip long strings so one runaway prompt cannot flood the log pipeline."""
    if isinstance(value, str):
        if len(value.encode("utf-8")) > limit:
            clipped = value.encode("utf-8")[:limit].decode("utf-8", "ignore")
            found.append(path)
            return clipped + "... [truncated " + str(len(value)) + " chars]"
        return value
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            child = path + "." + str(key) if path else str(key)
            out[key] = truncate(item, child, found, limit)
        return out
    if isinstance(value, list):
        out = []
        for index, item in enumerate(value):
            child = path + "[" + str(index) + "]"
            out.append(truncate(item, child, found, limit))
        return out
    return value


def build(
    *,
    trace_id: str,
    span_id: str,
    step_index: int,
    step_kind: str,
    model: str = "",
    params: dict = None,
    messages: list = None,
    tool: dict = None,
    output="",
) -> dict:
    """Assemble a redacted, size-bounded, JSON-safe replay envelope."""
    redacted_fields: list = []
    truncated_fields: list = []
    limit = max_field_bytes()

    body = {
        "envelope_id": uuid.uuid4().hex,
        "trace_id": trace_id,
        "span_id": span_id,
        "step_index": step_index,
        "step_kind": step_kind,
        "model": model,
        "params": params or {},
        "messages": messages or [],
        "tool": tool,
        "output": output,
    }

    for key in ("params", "messages", "tool"):
        if body[key]:
            body[key] = redact(body[key], key, redacted_fields)
    for key in ("messages", "tool", "output"):
        if body[key]:
            body[key] = truncate(body[key], key, truncated_fields, limit)

    body["redacted_fields"] = redacted_fields
    body["truncated_fields"] = truncated_fields
    return body


def emit(body: dict) -> dict:
    """Ship the envelope to SigNoz as a log record, and to the local mirror."""
    logging.getLogger(ENVELOPE_LOGGER).info(
        json.dumps(body, default=str),
        extra={
            "rewind.kind": "replay_envelope",
            "rewind.envelope_id": body.get("envelope_id"),
            "rewind.step_index": body.get("step_index"),
            "rewind.step_kind": body.get("step_kind"),
            "trace_id": body.get("trace_id"),
            "span_id": body.get("span_id"),
        },
    )
    mirror.record_envelope(body.get("trace_id", ""), body.get("span_id", ""), body)
    return body
