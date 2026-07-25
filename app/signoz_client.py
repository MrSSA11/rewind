"""The one file that knows how to read data back out of SigNoz.

The query API shape is the biggest external unknown in this project, so every
assumption about it is isolated here (PRD 13.4). Two backends implement the
same interface:

  SigNozBackend  - the real thing, over the SigNoz query API
  MirrorBackend  - the local JSONL mirror, used offline and in tests

REWIND_BACKEND=auto (default) prefers SigNoz and silently falls back to the
mirror when SigNoz is unreachable, so the UI is never dead on a fresh box.
"""

from __future__ import annotations

import os
from collections import OrderedDict

from rewind_sdk import mirror


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


class MirrorBackend:
    """Reads the append-only JSONL mirror written by the SDK."""

    name = "mirror"

    def health(self) -> dict:
        records = mirror.read_all()
        return {
            "backend": self.name,
            "ok": True,
            "detail": str(len(records)) + " telemetry records at " + str(mirror.path()),
        }

    def _bundles(self) -> "OrderedDict":
        bundles: OrderedDict = OrderedDict()
        for record in mirror.read_all():
            trace_id = record.get("trace_id")
            if not trace_id:
                continue
            bundle = bundles.setdefault(
                trace_id, {"trace_id": trace_id, "spans": [], "envelopes": []}
            )
            if record.get("type") == "span":
                bundle["spans"].append(record)
            elif record.get("type") == "envelope":
                bundle["envelopes"].append(
                    {"span_id": record.get("span_id", ""), "body": record.get("body", {})}
                )
        return bundles

    def list_trace_bundles(self, limit: int = 25) -> list:
        bundles = list(self._bundles().values())
        bundles.reverse()  # newest first
        return bundles[:limit]

    def get_trace_bundle(self, trace_id: str) -> dict:
        return self._bundles().get(
            trace_id, {"trace_id": trace_id, "spans": [], "envelopes": []}
        )


class SigNozBackend:
    """Reads traces and envelope logs from a running SigNoz instance."""

    name = "signoz"

    def __init__(self):
        self.base = _env("SIGNOZ_URL", "http://localhost:8080").rstrip("/")
        self.api_key = _env("SIGNOZ_API_KEY")
        self.timeout = float(_env("SIGNOZ_TIMEOUT_S", "10"))
        self.lookback_minutes = int(_env("SIGNOZ_LOOKBACK_MINUTES", "720"))

    # ------------------------------------------------------------ plumbing
    def _client(self):
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["SIGNOZ-API-KEY"] = self.api_key
        return httpx.Client(base_url=self.base, headers=headers, timeout=self.timeout)

    def _get(self, path: str, params: dict = None):
        with self._client() as client:
            response = client.get(path, params=params or {})
            response.raise_for_status()
            return response.json()

    def _post(self, path: str, payload: dict):
        with self._client() as client:
            response = client.post(path, json=payload)
            response.raise_for_status()
            return response.json()

    # -------------------------------------------------------------- public
    def health(self) -> dict:
        try:
            self._get(_env("SIGNOZ_HEALTH_PATH", "/api/v1/health"))
            return {"backend": self.name, "ok": True, "detail": self.base}
        except Exception as exc:
            return {
                "backend": self.name,
                "ok": False,
                "detail": self.base + " unreachable: " + str(exc),
            }

    def _window_ms(self) -> tuple:
        import time

        end = int(time.time() * 1000)
        return end - self.lookback_minutes * 60 * 1000, end

    def _envelopes_for(self, trace_id: str) -> list:
        """Envelope log records carry their own trace_id and span_id."""
        start, end = self._window_ms()
        payload = {
            "start": start,
            "end": end,
            "query": {
                "queryType": "builder",
                "panelType": "list",
                "builderQueries": {
                    "A": {
                        "dataSource": "logs",
                        "queryName": "A",
                        "expression": "A",
                        "filters": {
                            "op": "AND",
                            "items": [
                                {
                                    "key": {"key": "trace_id", "type": "tag"},
                                    "op": "=",
                                    "value": trace_id,
                                },
                                {
                                    "key": {"key": "rewind.kind", "type": "tag"},
                                    "op": "=",
                                    "value": "replay_envelope",
                                },
                            ],
                        },
                        "limit": 200,
                    }
                },
            },
        }
        data = self._post(_env("SIGNOZ_QUERY_PATH", "/api/v4/query_range"), payload)
        return _parse_envelope_logs(data)

    def get_trace_bundle(self, trace_id: str) -> dict:
        path = _env("SIGNOZ_TRACE_PATH", "/api/v1/traces/{trace_id}").replace(
            "{trace_id}", trace_id
        )
        spans = _parse_spans(self._get(path), trace_id)
        try:
            envelopes = self._envelopes_for(trace_id)
        except Exception:
            envelopes = []  # REC-3: render read-only rather than fail
        return {"trace_id": trace_id, "spans": spans, "envelopes": envelopes}

    def list_trace_bundles(self, limit: int = 25) -> list:
        start, end = self._window_ms()
        payload = {
            "start": start,
            "end": end,
            "query": {
                "queryType": "builder",
                "panelType": "list",
                "builderQueries": {
                    "A": {
                        "dataSource": "traces",
                        "queryName": "A",
                        "expression": "A",
                        "filters": {
                            "op": "AND",
                            "items": [
                                {
                                    "key": {"key": "agent.run.id", "type": "tag"},
                                    "op": "exists",
                                    "value": "",
                                }
                            ],
                        },
                        "limit": limit,
                    }
                },
            },
        }
        data = self._post(_env("SIGNOZ_QUERY_PATH", "/api/v4/query_range"), payload)
        trace_ids = _parse_trace_ids(data)[:limit]
        return [self.get_trace_bundle(trace_id) for trace_id in trace_ids]


# --------------------------------------------------------------- parsing
def _rows(data) -> list:
    """SigNoz wraps results differently by version; be forgiving."""
    if not isinstance(data, dict):
        return []
    payload = data.get("data", data)
    if isinstance(payload, list):
        return payload
    for key in ("result", "newResult", "results"):
        block = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(block, dict):
            block = block.get("data", block)
        if isinstance(block, list):
            rows = []
            for entry in block:
                if isinstance(entry, dict) and isinstance(entry.get("list"), list):
                    rows.extend(entry["list"])
                elif isinstance(entry, dict):
                    rows.append(entry)
            if rows:
                return rows
    return []


def _parse_trace_ids(data) -> list:
    seen = []
    for row in _rows(data):
        body = row.get("data", row) if isinstance(row, dict) else {}
        trace_id = body.get("traceID") or body.get("trace_id")
        if trace_id and trace_id not in seen:
            seen.append(trace_id)
    return seen


def _parse_spans(data, trace_id: str) -> list:
    spans = []
    for row in _rows(data):
        body = row.get("data", row) if isinstance(row, dict) else {}
        span_id = body.get("spanID") or body.get("span_id")
        if not span_id:
            continue
        attributes = (
            body.get("attributes")
            or body.get("tagMap")
            or body.get("stringTagMap")
            or {}
        )
        for extra_key in ("numberTagMap", "boolTagMap"):
            extra = body.get(extra_key)
            if isinstance(extra, dict):
                attributes = dict(attributes)
                attributes.update(extra)
        duration = body.get("durationNano")
        spans.append(
            {
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_span_id": body.get("parentSpanID")
                or body.get("parent_span_id")
                or "",
                "name": body.get("name") or body.get("spanName") or "span",
                "start_time": (body.get("timestamp") or 0),
                "duration_ms": (float(duration) / 1_000_000.0)
                if duration
                else float(body.get("durationMs") or 0.0),
                "status": "error" if body.get("hasError") else "ok",
                "attributes": attributes,
            }
        )
    return spans


def _parse_envelope_logs(data) -> list:
    import json

    envelopes = []
    for row in _rows(data):
        body = row.get("data", row) if isinstance(row, dict) else {}
        raw = body.get("body") or body.get("message") or ""
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        if not isinstance(parsed, dict) or "step_index" not in parsed:
            continue
        envelopes.append(
            {"span_id": parsed.get("span_id") or body.get("span_id", ""), "body": parsed}
        )
    return envelopes


class SigNozClient:
    """Facade the rest of the app talks to. Picks a backend and remembers it."""

    def __init__(self):
        self.mode = _env("REWIND_BACKEND", "auto").lower()
        self.mirror = MirrorBackend()
        self.signoz = SigNozBackend()
        self.last_used = "mirror"

    def ui_url(self) -> str:
        return _env("SIGNOZ_UI_URL", _env("SIGNOZ_URL", "http://localhost:8080")).rstrip("/")

    def trace_url(self, trace_id: str) -> str:
        return self.ui_url() + "/trace/" + str(trace_id)

    def health(self) -> dict:
        signoz_health = (
            {"backend": "signoz", "ok": False, "detail": "disabled by REWIND_BACKEND"}
            if self.mode == "mirror"
            else self.signoz.health()
        )
        return {
            "mode": self.mode,
            "signoz": signoz_health,
            "mirror": self.mirror.health(),
            "serving": self.last_used,
        }

    def _try(self, method: str, *args):
        if self.mode != "mirror":
            try:
                result = getattr(self.signoz, method)(*args)
                if result and (not isinstance(result, dict) or result.get("spans")):
                    self.last_used = "signoz"
                    return result
                if self.mode == "signoz":
                    self.last_used = "signoz"
                    return result
            except Exception:
                if self.mode == "signoz":
                    raise
        self.last_used = "mirror"
        return getattr(self.mirror, method)(*args)

    def list_trace_bundles(self, limit: int = 25) -> list:
        return self._try("list_trace_bundles", limit)

    def get_trace_bundle(self, trace_id: str) -> dict:
        return self._try("get_trace_bundle", trace_id)
