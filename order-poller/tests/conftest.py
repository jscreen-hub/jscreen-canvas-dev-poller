"""Shared fixtures/helpers for order-poller tests."""

from order_poller.config import Settings


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "auth_base_url": "https://jlab-dev.canvasmedical.com",
        "client_id": "cid",
        "client_secret": "secret",
        "fhir_base_url": "https://fumage-jlab-dev.canvasmedical.com",
        "scope": "system/*.read",
        "output_dir": "out",
        "state_file": "state.json",
        "lookback_days": 2,
        "poll_interval_seconds": 300,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]
