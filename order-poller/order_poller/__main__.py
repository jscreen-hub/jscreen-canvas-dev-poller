"""CLI entry point: `python -m order_poller [--once]`.

Default behavior is a continuous loop polling every ORDER_POLL_INTERVAL seconds.
Pass --once for a single cycle (useful for cron or manual runs).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone

from order_poller.canvas_client import CanvasClient
from order_poller.config import (
    COMMITTED_STATUSES,
    LAB_CATEGORY,
    Settings,
    load_settings,
)
from order_poller.poller import poll_once
from order_poller.state import ProcessedStore


def _authored_ge(settings: Settings) -> str:
    return (date.today() - timedelta(days=settings.lookback_days)).isoformat()


def run(settings: Settings, once: bool) -> int:
    client = CanvasClient(settings)
    store = ProcessedStore(settings.state_file).load()
    try:
        while True:
            captured_at = datetime.now(timezone.utc).isoformat()
            authored_ge = _authored_ge(settings)
            print(f"[{captured_at}] polling {settings.fhir_base_url} (authored>=ge{authored_ge})")
            try:
                poll_once(
                    client,
                    store,
                    settings.output_dir,
                    LAB_CATEGORY,
                    authored_ge,
                    captured_at,
                    allowed_statuses=COMMITTED_STATUSES,
                )
            except Exception as exc:  # noqa: BLE001 - keep the loop alive across errors
                print(f"poll cycle failed: {exc}", file=sys.stderr)
                if once:
                    return 1
            if once:
                return 0
            time.sleep(settings.poll_interval_seconds)
    except KeyboardInterrupt:
        print("stopping")
        return 0
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll Canvas for committed lab orders.")
    parser.add_argument(
        "--once", action="store_true", help="run a single cycle and exit"
    )
    args = parser.parse_args()
    settings = load_settings()
    return run(settings, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
