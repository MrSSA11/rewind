# SigNoz setup for Rewind

Rewind treats SigNoz as its **only** system of record. There is no application
database: every run you see in the UI is reconstructed from traces and logs
that SigNoz already stores.

## 1. Start SigNoz

```bash
cd signoz
foundry up
```

This reads `casting.yaml` and brings up SigNoz, ClickHouse, and the OTel
collector. Foundry generates `casting.yaml.lock` pinning exact versions -
commit it.

When it finishes:

- SigNoz UI: <http://localhost:8080>
- OTLP HTTP (what the SDK uses): <http://localhost:4318>
- OTLP gRPC: `localhost:4317`

## 2. Point the app at it

```bash
cp .env.example .env
```

The defaults already target `http://localhost:4318`. In GitHub Codespaces,
also set `SIGNOZ_UI_URL` to your forwarded 8080 URL so "Open in SigNoz" links
resolve from your browser.

## 3. Import the dashboard

In SigNoz: **Dashboards -> New dashboard -> Import JSON**, then paste
`dashboards/agent-health.json`.

Eight panels: runs per minute, error rate, run duration percentiles, tokens by
model, cost per hour, tool call volume, top failing spans, and replay count.

## 4. Import the alerts

In SigNoz: **Alerts -> New alert -> Import JSON**, once per file in `alerts/`:

| File | Fires when |
| --- | --- |
| `guardrail-failure.json` | any agent run trips a guardrail |
| `error-rate.json` | more than 10% of runs fail |
| `latency-p95.json` | p95 run duration goes above 20s |
| `cost-spike.json` | spend passes $5/hour |

Each alert notifies the `rewind` webhook receiver defined in `casting.yaml`.
Rewind receives it at `POST /api/webhooks/signoz`, pulls the trace ids out of
the payload, and puts those runs at the top of the triage queue on the home
screen. That is the full loop: **alert -> the exact broken run -> rewind it**.

## What Rewind writes to SigNoz

| Signal | Contents |
| --- | --- |
| Traces | One span per agent step, using OpenTelemetry GenAI semantic conventions (`gen_ai.*`) plus `agent.*` and `rewind.*` attributes |
| Logs | One replay envelope per step, correlated by `trace_id` + `span_id`, carrying the exact inputs needed to re-run that step |
| Metrics | `gen_ai.client.token.usage`, `agent.run.duration`, `agent.run.cost.usd`, `agent.tool.calls`, `agent.run.errors`, `rewind.replays` |

Forked runs are new traces tagged `rewind.parent_trace_id`,
`rewind.forked_from_span_id`, and `rewind.experiment=true`, so you can filter
experiments out of production dashboards with a single clause.

## If SigNoz will not start

The SDK also mirrors everything to `.rewind/telemetry.jsonl`. Set
`REWIND_BACKEND=mirror` and the entire app - reconstruction, replay, diff -
keeps working offline. The default `auto` mode falls back automatically.
