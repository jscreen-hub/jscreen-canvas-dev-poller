"""CLI entry point: `python -m billing_poller [--once]`.

Default behavior is a continuous loop polling every BILLING_POLL_INTERVAL
seconds. Pass --once for a single cycle (useful for a Scheduled Task or a
manual run).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone

from billing_poller.canvas_client import CanvasClient
from billing_poller.config import Settings, load_settings
from billing_poller.ledger import BilledLedger
from billing_poller.lookup import BillingLookup
from billing_poller.poller import poll_once
from billing_poller.state import ProcessedStore


def _date_ge(settings: Settings) -> str | None:
    """Lower bound for the report sweep, or None to sweep everything.

    See DEFAULT_LOOKBACK_DAYS in config for why a full sweep is the default.
    """
    if settings.lookback_days <= 0:
        return None
    return (date.today() - timedelta(days=settings.lookback_days)).isoformat()


def run(settings: Settings, once: bool) -> int:
    client = CanvasClient(settings)
    lookup = BillingLookup(settings.lookup_url, settings.lookup_api_key)
    store = ProcessedStore(settings.state_file).load()
    ledger = BilledLedger(settings.ledger_file).load()
    if lookup.enabled:
        print(f"billing lookup enabled: {settings.lookup_url}")
    else:
        print("billing lookup not configured; CPT codes and exact order links unavailable")
    try:
        while True:
            captured_at = datetime.now(timezone.utc).isoformat()
            date_ge = _date_ge(settings)
            window = f" (date>=ge{date_ge})" if date_ge else " (full sweep)"
            print(f"[{captured_at}] polling {settings.fhir_base_url}{window}")
            try:
                poll_once(
                    client,
                    store,
                    settings.output_dir,
                    date_ge,
                    captured_at,
                    lookup=lookup,
                    ledger=ledger,
                )
            except Exception as exc:  # noqa: BLE001 - keep the loop alive across errors
                print(f"poll cycle failed: {exc}", file=sys.stderr)
                if once:
                    return 1
            if once:
                return 0
            client.clear_cache()
            time.sleep(settings.poll_interval_seconds)
    except KeyboardInterrupt:
        print("stopping")
        return 0
    finally:
        client.close()
        lookup.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Poll Canvas for signed-off lab results and export billing data."
    )
    parser.add_argument(
        "--once", action="store_true", help="run a single cycle and exit"
    )
    args = parser.parse_args()
    settings = load_settings()
    return run(settings, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
