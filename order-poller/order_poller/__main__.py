"""CLI entry point: `python -m order_poller [--once] [--seed]`.

Default behavior is a continuous loop polling every ORDER_POLL_INTERVAL seconds.
Pass --once for a single cycle (Windows Task Scheduler / cron / manual runs).

Pass --seed ONCE when cutting over from the old FHIR ServiceRequest source: it
records every currently-signed order as already-processed without writing any
files, so the switch to LabOrder ids does not re-deliver orders downstream that
have already been sent.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from order_poller.config import Settings, load_settings
from order_poller.poller import poll_once, seed
from order_poller.source import LabOrderSource
from order_poller.state import ProcessedStore


def run(settings: Settings, once: bool, do_seed: bool = False) -> int:
    source = LabOrderSource(settings.lookup_url, settings.lookup_api_key)
    store = ProcessedStore(settings.state_file).load()

    if not source.configured:
        print(
            "lab order source is not configured: set BILLING_LOOKUP_URL and "
            "BILLING_LOOKUP_API_KEY in the repo-root .env",
            file=sys.stderr,
        )
        source.close()
        return 1

    try:
        if do_seed:
            seed(source, store)
            return 0

        while True:
            captured_at = datetime.now(timezone.utc).isoformat()
            print(f"[{captured_at}] polling {settings.lookup_url}/lab-orders")
            try:
                poll_once(source, store, settings.output_dir, captured_at)
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
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Poll Canvas for signed lab orders and export them as JSON."
    )
    parser.add_argument(
        "--once", action="store_true", help="run a single cycle and exit"
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help=(
            "mark all currently-signed orders as processed without writing "
            "files (run once when cutting over from the ServiceRequest source)"
        ),
    )
    args = parser.parse_args()
    settings = load_settings()
    return run(settings, once=args.once, do_seed=args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
