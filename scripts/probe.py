"""Ask SigNoz directly why Rewind is not reading from it.

The app falls back to the local mirror whenever a SigNoz read fails or comes
back empty. That keeps the demo running, but it hides the cause. This script
removes the safety net and prints exactly what SigNoz said.

    make probe
"""

from __future__ import annotations

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from app.signoz_client import SigNozClient, _env  # noqa: E402


def line(label: str, value) -> None:
    print("  " + label.ljust(16) + str(value))


def main() -> int:
    print("\n=== configuration ===")
    line("backend mode", _env("REWIND_BACKEND", "auto"))
    line("signoz url", _env("SIGNOZ_URL", "http://localhost:8080"))
    line("api key set", bool(_env("SIGNOZ_API_KEY")))
    line("lookback min", _env("SIGNOZ_LOOKBACK_MINUTES", "720"))
    line("otlp endpoint", os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"))
    line("otel disabled", os.getenv("REWIND_DISABLE_OTEL", "0"))

    client = SigNozClient()

    print("\n=== can we reach signoz at all ===")
    print(json.dumps(client.signoz.health(), indent=2))

    print("\n=== raw query, no fallback ===")
    try:
        bundles = client.signoz.list_trace_bundles(10)
        print("  SigNoz returned " + str(len(bundles)) + " trace bundle(s)")
        for bundle in bundles:
            print(
                "    "
                + str(bundle.get("trace_id"))
                + " -> "
                + str(len(bundle.get("spans") or []))
                + " spans, "
                + str(len(bundle.get("envelopes") or []))
                + " envelopes"
            )
        if not bundles:
            print("  SigNoz answered, but no agent traces matched the time window.")
            print("  Usually that means the run happened before SigNoz was up,")
            print("  or the spans never left the app. Re-run: make clean && make demo")
    except Exception:
        print("  the query raised. full detail:\n")
        traceback.print_exc()
        print("\n  A 401 or 403 here means SigNoz wants an API key. Create one under")
        print("  Settings -> Service Accounts, then add it to .env:")
        print("      SIGNOZ_API_KEY=your-key")
        return 1

    print("\n=== what the mirror holds ===")
    print(json.dumps(client.mirror.health(), indent=2))

    print("\n=== verdict ===")
    through_facade = client.list_trace_bundles(10)
    line("served by", client.last_used)
    line("bundles", len(through_facade))
    line("last error", getattr(client, "last_error", "") or "none")
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
