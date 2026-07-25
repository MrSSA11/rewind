# ⏪ Rewind — a rewind button for AI agents

**Track 01 · AI & Agent Observability — WeMakeDevs × SigNoz Hackathon**

When an AI agent gives a wrong answer, today you get a trace that shows *what*
happened. Rewind lets you change one step in that trace and **run it again** to
find out *why* — and whether your fix actually works.

> Open a bad run → pick the step you suspect → change one thing → replay →
> read a side-by-side diff of the old run and the new one.

---

## The problem

Agents are non-deterministic. When one misbehaves in production, the normal
loop is: read the trace, guess the cause, edit a prompt, re-run the whole thing
by hand with different inputs, and hope. There is no way to ask the obvious
question:

> *If this one step had returned the right thing, would the answer have been correct?*

Rewind answers that question directly from telemetry.

## The idea

Every step an agent takes is recorded as an OpenTelemetry span **plus a replay
envelope** — a log record holding everything needed to run that step again:
messages, model, parameters, tool arguments and results. When you fork a run:

1. Steps **before** your chosen step are *restored* from the envelopes — not
   re-executed, so they cost nothing and cannot drift.
2. Your **one override** is applied at the fork point (a different tool result,
   a different prompt, or a different model).
3. Every step **after** it is genuinely re-executed.
4. The fork is emitted as a **brand new trace**, tagged with
   `rewind.parent_trace_id` and `rewind.experiment=true`.
5. Rewind reads both traces back out of SigNoz and diffs them.

## Rewind has no database

This is the core design decision. **SigNoz is the system of record.** Traces,
logs and metrics are the only storage. There is no Postgres, no Redis, no local
state to keep in sync. If SigNoz can serve it, Rewind can show it — which means
Rewind works on *any* OTel-instrumented agent, not just the demo.

*(A local JSONL mirror is written as a documented fallback cache so the UI still
works while SigNoz is booting, or on a machine too small to run ClickHouse.)*

## The built-in demo bug

A refund-support agent is shipped with a deliberate, realistic bug:

- The retriever returns a **stale policy document** saying the refund window is **90 days** (archived v1).
- The real, current policy (v3) says **14 days**.
- Order `A-10492` is **60 days old**, worth **$249.00**.
- The agent **approves the refund**. A guardrail catches it and the run is marked failed.

Nothing in the trace is obviously broken — every span is green except the last.
In Rewind you fork step 0, paste the correct 14-day policy as the tool result,
replay, and watch the agent deny the refund, skip the `refund_api.approve` call
entirely, and pass the guardrail. **The diff proves the retriever was the cause.**

---

## Architecture

```
  ┌────────────────────┐
  │  Your agent code   │   rewind_sdk: ~20 lines to instrument
  │  + rewind_sdk      │
  └─────────┬──────────┘
            │  OTLP/HTTP :4318
            │  traces · logs (replay envelopes) · metrics
            ▼
  ┌────────────────────────────────────────────┐
  │   SELF-HOSTED SIGNOZ  (Foundry)  :8080     │
  │   the only storage layer                   │
  │   traces │ logs │ metrics │ dashboards     │
  └───┬──────────────────────────────┬─────────┘
      │ query API                    │ alert webhook
      ▼                              ▼
  ┌────────────────────────────────────────────┐
  │   REWIND  (FastAPI + HTMX)  :8000          │
  │   triage queue → run view → step detail    │
  │   → fork + replay → diff                   │
  └────────────────────┬───────────────────────┘
                       │ replays emit a NEW trace
                       └──────────► back into SigNoz
```

| Path | What it does |
| --- | --- |
| `rewind_sdk/` | The instrumentation library: spans, replay envelopes, redaction, metrics, cost |
| `demo_agent/` | The refund agent with the planted bug |
| `app/` | FastAPI web app: reconstruct, replay, diff, SigNoz client |
| `app/templates/` | Mobile-first HTMX UI, no build step |
| `signoz/` | `casting.yaml` for Foundry, dashboard, 4 alert rules |
| `tests/` | Envelope safety, diff engine, and full replay round-trip |

## What gets recorded

**Spans** follow OpenTelemetry GenAI semantic conventions —
`gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.tool.name` — plus
agent-level attributes (`agent.run.id`, `agent.step.index`, `agent.step.kind`,
`agent.cost.usd`) and Rewind's own (`rewind.replayable`, `rewind.envelope_id`,
`rewind.parent_trace_id`, `rewind.override_type`).

**Logs** carry the replay envelopes, correlated to their span by
`trace_id` + `span_id`. This is the trick that makes replay possible without a
database.

**Metrics**: `gen_ai.client.token.usage`, `agent.run.duration`,
`agent.run.cost.usd`, `agent.tool.calls`, `agent.run.errors`, `rewind.replays`.

## Safety

Replay envelopes contain prompts and tool arguments, so before anything leaves
the process:

- Keys like `password`, `api_key`, `token`, `secret`, `authorization` are replaced with `[REDACTED]`, and the paths are listed in `redacted_fields`. Extend with `REWIND_REDACT_KEYS`.
- Fields over `REWIND_MAX_FIELD_BYTES` (8KB) are truncated and listed in `truncated_fields`.
- Replays are bounded by `REWIND_MAX_STEPS` and `REWIND_REPLAY_TIMEOUT_S`.
- Original traces are **never** mutated. A fork is always a new trace. There is a test for this.

---

## Quickstart (60 seconds, no API keys)

```bash
pip install -r requirements.txt
make demo
```

Open **http://localhost:8000**. `FAKE_LLM=1` is the default, so the entire
demo — including replay — runs offline with a deterministic stub model.

To run it for real against self-hosted SigNoz:

```bash
curl -fsSL https://signoz.io/foundry.sh | bash
cd signoz && foundry up          # UI on :8080
cd .. && make demo               # UI on :8000
```

See [`signoz/README.md`](signoz/README.md) for importing the dashboard and the
four alert rules.

## Configuration

Everything has a working default; copy `.env.example` to `.env` to change any of
it. The ones that matter:

| Variable | Default | Meaning |
| --- | --- | --- |
| `FAKE_LLM` | `1` | Deterministic offline model. No API key needed. |
| `GROQ_API_KEY` | – | Set with `FAKE_LLM=0` to use a real model |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | Where telemetry is sent |
| `REWIND_BACKEND` | `auto` | `signoz`, `mirror`, or auto-fallback |
| `SIGNOZ_URL` | `http://localhost:8080` | SigNoz query API |
| `SIGNOZ_API_KEY` | – | From SigNoz → Settings → API Keys |
| `REWIND_MAX_FIELD_BYTES` | `8000` | Envelope field size cap |

## API

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/` | Triage queue + recent runs |
| `GET` | `/runs/{trace_id}` | Step tree for a run |
| `GET` | `/runs/{trace_id}/steps/{span_id}` | Step detail + fork form |
| `POST` | `/runs/{trace_id}/fork` | Fork and replay (form post) |
| `GET` | `/diff?a=&b=` | Side-by-side comparison |
| `GET` | `/api/runs` · `/api/runs/{trace_id}` | JSON run data |
| `POST` | `/api/runs/{trace_id}/fork` | JSON replay (`dry_run` supported) |
| `GET` | `/api/diff?a=&b=` | JSON diff |
| `POST` | `/api/webhooks/signoz` | SigNoz alerts → triage queue |
| `GET` | `/healthz` | Backend + SigNoz reachability |

Fork a run from the command line:

```bash
curl -X POST localhost:8000/api/runs/<trace_id>/fork \
  -H 'content-type: application/json' \
  -d '{
        "span_id": "<span id of step 0>",
        "override_type": "tool_result",
        "override_value": "Refunds Policy (v3, current): the refund window is 14 days."
      }'
```

```json
{
  "fork_trace_id": "9f2c…",
  "parent_trace_id": "ec5f…",
  "diff_url": "/diff?a=ec5f…&b=9f2c…",
  "signoz_url": "http://localhost:8080/trace/9f2c…"
}
```

## Tests

```bash
make test
```

No network, no API keys, no running SigNoz. Covers envelope redaction and
truncation, the diff engine, the demo bug reproducing, forks fixing it,
pre-fork steps being restored rather than re-run, invalid overrides being
rejected, and the original trace staying immutable.

## Built on a phone

This project was built entirely from a mobile device using GitHub Codespaces,
so every screen is designed for a 390px viewport first. The UI is server-
rendered Jinja with HTMX and Tailwind from a CDN — **no Node, no build step.**

## License

MIT — see [LICENSE](LICENSE).
