"""Configuration for the billing poller.

Credentials and settings are read from environment variables (loaded from the
repo-root .env by `load_settings`). Nothing is hardcoded. The FHIR base URL is
derived from the auth base URL by inserting the Canvas `fumage-` prefix, unless
CANVAS_FHIR_BASE_URL is set explicitly.

This mirrors ../order-poller/order_poller/config.py so the two pollers share
one .env and one set of conventions.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

# --- what counts as a signed-off lab result ---------------------------------

# Canvas DiagnosticReport statuses. `entered-in-error` is a voided report.
FINAL_REPORT_STATUS = "final"

# A Canvas lab report only gains an `encounter` reference once a Lab Results
# Review (or POC Lab Test) command has been COMMITTED in a note on the patient's
# chart. That committed review is the provider sign-off this poller waits for.
# Source: docs.canvasmedical.com/api/diagnosticreport/ (encounter attribute).
LAB_REVIEW_ENCOUNTER_TYPES = ("Lab Results Review",)

# Review notes that were deleted/voided. Their encounter still hangs off the
# report, so they must be filtered out or voided reviews would be billed.
VOID_ENCOUNTER_STATUSES = ("cancelled", "entered-in-error")

# --- what counts as the order behind the result -----------------------------

# SNOMED category code that identifies a Laboratory procedure ServiceRequest.
# Source: docs.canvasmedical.com/api/servicerequest/
LAB_CATEGORY = "http://snomed.info/sct|108252007"

# ServiceRequest statuses that represent a signed/committed order. `status` is
# not a server-side search param on Canvas, so this filter is applied
# client-side. `draft` = staged/unsigned; `entered-in-error` = voided.
COMMITTED_ORDER_STATUSES = ("active", "completed")

# --- coding systems ---------------------------------------------------------

ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"
CPT_SYSTEM = "http://www.ama-assn.org/go/cpt"
NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"
LOINC_SYSTEM = "http://loinc.org"
MRN_TYPE_CODE = "MR"
CLAIM_QUEUE_EXTENSION = "http://schemas.canvasmedical.com/fhir/extensions/claim-queue"
NOTE_ID_EXTENSION = "http://schemas.canvasmedical.com/fhir/extensions/note-id"

# --- patient chart export ----------------------------------------------------

# Canvas's only whole-chart summary export: a continuity-of-care C-CDA (XML)
# covering problems, meds, allergies, results, procedures and encounters for
# the whole patient -- not scoped to the single order this record is about.
# It is not a PDF, and it lives on the auth host, not the `fumage-` FHIR host.
# A billing record carries a link to it, never the fetched document, so a
# per-result record doesn't balloon into a full chart dump by default.
# Source: docs.canvasmedical.com/api/ccda/
CCDA_PATH = "/api/data-export/ccda/{patient_key}"
CCDA_DOCUMENT_TYPE = "continuity"

# --- defaults ---------------------------------------------------------------

DEFAULT_OUTPUT_DIR = r"C:\Projects\canvas-orders\billing"
DEFAULT_STATE_FILE = (
    r"C:\Projects\canvas-orders\billing-poller\.state\processed_ids.json"
)
DEFAULT_LEDGER_FILE = (
    r"C:\Projects\canvas-orders\billing-poller\.state\billed_cpt.json"
)
DEFAULT_POLL_INTERVAL = 300
DEFAULT_PAGE_SIZE = 100

# Path to the optional `lab_billing_lookup` plugin, which supplies CPT codes and
# the exact result->order link. Derived from CANVAS_BASE_URL unless overridden.
LOOKUP_PATH = "/plugin-io/api/lab_billing_lookup/billing"

# 0 = no date filter, sweep every report every cycle. This is the default on
# purpose: Canvas ignores `_lastUpdated` and `_sort` on DiagnosticReport (they
# return the full set unchanged), so there is no server-side cursor. The only
# date param that works is `date`, which filters effectiveDateTime -- the date
# the specimen was collected, NOT the date the provider signed off. A result
# collected in January and reviewed in March would fall outside any short
# window. A full sweep plus the id dedupe store is what guarantees a signed-off
# result is never missed. Set BILLING_LOOKBACK_DAYS only if the report volume
# makes the full sweep too slow, and set it generously.
DEFAULT_LOOKBACK_DAYS = 0


def derive_fhir_base_url(auth_base_url: str) -> str:
    """Insert the Canvas `fumage-` prefix into the host of the auth base URL.

    e.g. https://jlab-dev.canvasmedical.com -> https://fumage-jlab-dev.canvasmedical.com
    """
    parts = urlsplit(auth_base_url)
    if not parts.hostname:
        raise ValueError(f"Cannot derive FHIR base URL from {auth_base_url!r}")
    host = parts.hostname
    new_host = host if host.startswith("fumage-") else f"fumage-{host}"
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
    ledger_file: str
    lookback_days: int
    poll_interval_seconds: int
    page_size: int
    lookup_url: str | None
    lookup_api_key: str | None

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
            output_dir=env.get("BILLING_OUTPUT_DIR", DEFAULT_OUTPUT_DIR),
            state_file=env.get("BILLING_STATE_FILE", DEFAULT_STATE_FILE),
            ledger_file=env.get("BILLING_LEDGER_FILE", DEFAULT_LEDGER_FILE),
            lookback_days=int(env.get("BILLING_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)),
            poll_interval_seconds=int(
                env.get("BILLING_POLL_INTERVAL", DEFAULT_POLL_INTERVAL)
            ),
            page_size=int(env.get("BILLING_PAGE_SIZE", DEFAULT_PAGE_SIZE)),
            lookup_url=(
                env.get("BILLING_LOOKUP_URL", "").rstrip("/")
                or f"{auth_base_url}{LOOKUP_PATH}"
            ),
            lookup_api_key=env.get("BILLING_LOOKUP_API_KEY") or None,
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
