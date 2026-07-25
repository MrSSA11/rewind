# Self-hosted SigNoz for Rewind

SigNoz is not a side car for this project. It is the database. Rewind stores no
run data of its own: every run you see in the UI is reconstructed by querying
spans and log records back out of SigNoz.

## Start it

```bash
curl -fsSL https://signoz.io/foundry.sh | bash
export PATH="$HOME/.local/bin:$PATH"

cd signoz
foundryctl cast -f casting.yaml
```

The installer drops the binary at `~/.local/bin/foundryctl`. If your shell says
`command not found`, that folder is not on your `PATH` yet - run the `export`
line above, and add it to `~/.bashrc` to make it stick.

`cast` validates your tooling, generates the Docker Compose files into `pours/`,
and starts the stack. First run pulls several images and takes a few minutes.

From the repo root you can also just run:

```bash
make signoz
```

## Check it

```bash
docker ps
```

Every container should read `Up`. Then open <http://localhost:8080> and create
an account. It is local to your machine.

## Ports

| Port | What |
| --- | --- |
| 8080 | SigNoz UI and query API |
| 4317 | OTLP gRPC |
| 4318 | OTLP HTTP - `rewind_sdk` exports here |
| 8000 | Rewind itself, **not** SigNoz |

The SigNoz MCP server is intentionally left disabled in `casting.yaml`. It binds
port 8000, which would collide with the Rewind web app.

## Point Rewind at it

`.env` in the repo root:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
SIGNOZ_URL=http://localhost:8080
REWIND_BACKEND=auto
```

On GitHub Codespaces also set `SIGNOZ_UI_URL` to the public forwarded URL of
port 8080, so the "Open this trace in SigNoz" links resolve from your browser
rather than from inside the container.

Confirm which backend is serving:

```bash
make health
```

`"serving": "signoz"` means reads are coming from SigNoz. `"mirror"` means
SigNoz was unreachable and Rewind fell back to the local JSONL cache so the demo
still runs. The fallback is a convenience, not the design.

## Dashboard

`dashboards/agent-health.json` - import via **Dashboards -> New dashboard ->
Import JSON**. Eight panels: runs per minute, error rate, run duration p50/p95,
tokens by model, cost per hour, tool calls, top failing spans, and replays.

## Alerts

Import each file under `alerts/` via **Alerts -> New alert -> Import JSON**.

| Alert | Fires when |
| --- | --- |
| `guardrail-failure.json` | any log record with `rewind.kind = guardrail_failure` |
| `error-rate.json` | more than 10% of runs error |
| `latency-p95.json` | p95 run duration goes over 20s |
| `cost-spike.json` | spend passes $5 in an hour |

All four notify a channel named `rewind`. Create it under **Settings -> Alert
Channels -> New channel**, type **Webhook**:

```
http://host.docker.internal:8000/api/webhooks/signoz
```

That closes the loop. A bad run trips an alert, the alert lands in Rewind's
triage queue, and you rewind it from there.

## Lock file

Commit whatever lock or generated output `foundryctl` writes next to
`casting.yaml` so the deployment is reproducible.

```bash
git add signoz/
git commit -m "Add Foundry generated output"
```

## Retention

Defaults are fine for a demo. To change them, use **Settings -> Retention** in
the SigNoz UI rather than hand-editing generated files.
