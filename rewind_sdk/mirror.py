"""Append-only JSONL mirror of what we send to SigNoz.

This is a fallback cache, never the system of record. It exists so that a fresh
Codespace, an offline laptop, or the test suite can exercise the whole replay
loop before SigNoz has finished booting.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_LOCK = threading.Lock()


def path() -> Path:
    return Path(os.getenv("REWIND_MIRROR_PATH", ".rewind/telemetry.jsonl"))


def _append(record: dict) -> None:
    target = path()
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")


def record_span(span: dict) -> None:
    payload = dict(span)
    payload["type"] = "span"
    _append(payload)


def record_envelope(trace_id: str, span_id: str, body: dict) -> None:
    _append(
        {"type": "envelope", "trace_id": trace_id, "span_id": span_id, "body": body}
    )


def read_all() -> list:
    target = path()
    if not target.exists():
        return []
    records = []
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def clear() -> None:
    target = path()
    if target.exists():
        target.unlink()
