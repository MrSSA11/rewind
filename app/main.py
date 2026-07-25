"""Rewind service: reconstruct, fork, replay, diff - plus a mobile-first UI."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .diff import compare
from .reconstruct import build_run
from .replay import ReplayError, dry_run, execute
from .signoz_client import SigNozClient

app = FastAPI(title="Rewind", description="A rewind button for AI agents", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
client = SigNozClient()

# Alert-driven triage queue (ALT-2). Deliberately in-memory: it is a view of
# SigNoz alerts, not a second source of truth.
QUEUE: list = []


def load_run(trace_id: str):
    run = build_run(client.get_trace_bundle(trace_id))
    if not run.steps:
        raise HTTPException(status_code=404, detail="no telemetry found for " + trace_id)
    return run


def list_runs(limit: int = 25) -> list:
    runs = [build_run(bundle) for bundle in client.list_trace_bundles(limit)]
    runs = [run for run in runs if run.steps]
    runs.sort(key=lambda r: (0 if r.failed else 1, r.started_at), reverse=False)
    return runs


def ctx(request: Request, **kwargs) -> dict:
    base = {
        "request": request,
        "signoz_ui": client.ui_url(),
        "backend": client.last_used,
        "queue": QUEUE,
    }
    base.update(kwargs)
    return base


# ------------------------------------------------------------------- UI
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    runs = list_runs()
    return templates.TemplateResponse("index.html", ctx(request, runs=runs))


@app.get("/runs/{trace_id}", response_class=HTMLResponse)
def run_view(request: Request, trace_id: str):
    run = load_run(trace_id)
    return templates.TemplateResponse(
        "run.html",
        ctx(request, run=run, trace_url=client.trace_url(trace_id)),
    )


@app.get("/runs/{trace_id}/steps/{span_id}", response_class=HTMLResponse)
def step_view(request: Request, trace_id: str, span_id: str):
    run = load_run(trace_id)
    step = run.step_by_span(span_id)
    if step is None:
        raise HTTPException(status_code=404, detail="step not found")
    return templates.TemplateResponse(
        "step.html",
        ctx(request, run=run, step=step, trace_url=client.trace_url(trace_id)),
    )


@app.post("/runs/{trace_id}/fork")
def fork_form(
    trace_id: str,
    span_id: str = Form(...),
    override_type: str = Form(...),
    override_value: str = Form(...),
):
    run = load_run(trace_id)
    try:
        fork_trace_id = execute(
            run, span_id, {"type": override_type, "value": override_value}
        )
    except ReplayError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(
        url="/diff?a=" + trace_id + "&b=" + fork_trace_id, status_code=303
    )


@app.get("/diff", response_class=HTMLResponse)
def diff_view(request: Request, a: str, b: str):
    run_a, run_b = load_run(a), load_run(b)
    return templates.TemplateResponse(
        "diff.html",
        ctx(
            request,
            result=compare(run_a, run_b),
            run_a=run_a,
            run_b=run_b,
            url_a=client.trace_url(a),
            url_b=client.trace_url(b),
        ),
    )


@app.post("/demo/seed")
def demo_seed():
    from demo_agent.seed import seed

    result = seed()
    return RedirectResponse(url="/runs/" + result["trace_id"], status_code=303)


# ------------------------------------------------------------------ API
@app.get("/api/runs")
def api_runs(limit: int = 25, status: str = ""):
    runs = list_runs(limit)
    if status:
        runs = [run for run in runs if run.status == status]
    return {"runs": [run.to_dict() for run in runs]}


@app.get("/api/runs/{trace_id}")
def api_run(trace_id: str):
    return load_run(trace_id).to_dict()


@app.post("/api/runs/{trace_id}/fork")
async def api_fork(trace_id: str, request: Request):
    payload = await request.json()
    run = load_run(trace_id)
    span_id = payload.get("span_id", "")
    override = payload.get("override", {})
    try:
        if payload.get("dry_run"):
            return dry_run(run, span_id, override)
        fork_trace_id = execute(run, span_id, override)
    except ReplayError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {
        "fork_trace_id": fork_trace_id,
        "parent_trace_id": trace_id,
        "diff_url": "/diff?a=" + trace_id + "&b=" + fork_trace_id,
        "signoz_url": client.trace_url(fork_trace_id),
    }


@app.get("/api/diff")
def api_diff(a: str, b: str):
    return compare(load_run(a), load_run(b))


@app.post("/api/webhooks/signoz")
async def signoz_webhook(request: Request):
    """Alert intake (ALT-1). SigNoz payload shapes vary, so dig broadly."""
    payload = await request.json()
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("traceID", "trace_id", "traceId") and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    alert_name = (
        payload.get("alertname")
        or (payload.get("commonLabels") or {}).get("alertname")
        or "SigNoz alert"
    )
    added = 0
    for trace_id in found:
        if any(item["trace_id"] == trace_id for item in QUEUE):
            continue
        QUEUE.insert(0, {"trace_id": trace_id, "alert": alert_name, "resolved": False})
        added += 1
    return {"received": True, "alert": alert_name, "queued": added}


@app.get("/api/queue")
def api_queue():
    return {"queue": [item for item in QUEUE if not item["resolved"]]}


@app.post("/api/queue/{trace_id}/resolve")
def api_queue_resolve(trace_id: str):
    for item in QUEUE:
        if item["trace_id"] == trace_id:
            item["resolved"] = True
    return {"resolved": trace_id}


@app.get("/healthz")
def healthz():
    health = client.health()
    health["fake_llm"] = os.getenv("FAKE_LLM", "1") == "1"
    health["otlp_endpoint"] = os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
    )
    return health
