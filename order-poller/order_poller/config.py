"""Configuration for the order poller.

Credentials and settings are read from environment variables (loaded from the
repo-root .env by `load_settings`). Nothing is hardcoded. The FHIR base URL is
derived from the auth base URL by inserting the Canvas `fumage-` prefix, unless
CANVAS_FHIR_BASE_URL is set explicitly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

# SNOMED category code that identifies a Laboratory procedure ServiceRequest.
# Source: docs.canvasmedical.com/api/servicerequest/
LAB_CATEGORY = "http://snomed.info/sct|108252007"

# ServiceRequest statuses that represent a signed/committed order. `status` is not
# a server-side search param on Canvas, so this filter is applied client-side.
# `draft` = staged/unsigned; `entered-in-error` = voided.
COMMITTED_STATUSES = ("active", "completed")

DEFAULT_OUTPUT_DIR = r"C:\Projects\canvas-orders\orders"
DEFAULT_STATE_FILE = r"C:\Projects\canvas-orders\order-poller\.state\processed_ids.json"
DEFAULT_LOOKBACK_DAYS = 2
DEFAULT_POLL_INTERVAL = 300


def derive_fhir_base_url(auth_base_url: str) -> str:
    """Insert the Canvas `fumage-` prefix into the host of the auth base URL.

    e.g. https://jlab-dev.canvasmedical.com -> https://fumage-jlab-dev.canvasmedical.com
    """
    parts = urlsplit(auth_base_url)
    if not parts.hostname:
        raise ValueError(f"Cannot derive FHIR base URL from {auth_base_url!r}")
    host = parts.hostname
    if host.startswith("fumage-"):
        new_host = host
    else:
        new_host = f"fumage-{host}"
    if parts.port:
        new_host = f"{new_host}:{parts.port}"
    return urlunsplit((parts.scheme, new_host, "", "", ""))


@dataclass(frozen=True)
class Settings:
    auth_base_url: str
    client_id: str
    client_secret: str
    fhir_base_url: str
    scope: str | None
    output_dir: str
    state_file: str
    lookback_days: int
    poll_interval_seconds: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        auth_base_url = _require(env, "CANVAS_BASE_URL").rstrip("/")
        fhir_base_url = env.get("CANVAS_FHIR_BASE_URL", "").rstrip("/") or (
            derive_fhir_base_url(auth_base_url)
        )
        return cls(
            auth_base_url=auth_base_url,
            client_id=_require(env, "CANVAS_CLIENT_ID"),
            client_secret=_require(env, "CANVAS_CLIENT_SECRET"),
            fhir_base_url=fhir_base_url,
            scope=env.get("CANVAS_SCOPE") or None,
            output_dir=env.get("ORDER_OUTPUT_DIR", DEFAULT_OUTPUT_DIR),
            state_file=env.get("ORDER_STATE_FILE", DEFAULT_STATE_FILE),
            lookback_days=int(env.get("ORDER_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)),
            poll_interval_seconds=int(
                env.get("ORDER_POLL_INTERVAL", DEFAULT_POLL_INTERVAL)
            ),
        )


def _require(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if not value:
        raise ValueError(f"Required environment variable {key} is not set")
    return value


def load_settings() -> Settings:
    """Load .env from the repo root (searching upward) and build Settings."""
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    return Settings.from_env(os.environ)
