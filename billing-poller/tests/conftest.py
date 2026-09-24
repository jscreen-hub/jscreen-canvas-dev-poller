"""Shared fixtures/helpers for billing-poller tests."""

from billing_poller.config import Settings


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "auth_base_url": "https://jlab-dev.canvasmedical.com",
        "client_id": "cid",
        "client_secret": "secret",
        "fhir_base_url": "https://fumage-jlab-dev.canvasmedical.com",
        "scope": "system/*.read",
        "output_dir": "out",
        "state_file": "state.json",
        "ledger_file": "ledger.json",
        "lookback_days": 0,
        "poll_interval_seconds": 300,
        "page_size": 100,
        "lookup_url": None,
        "lookup_api_key": None,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def review_encounter(status: str = "finished", **overrides: object) -> dict:
    """A committed Lab Results Review encounter."""
    encounter = {
        "resourceType": "Encounter",
        "id": "enc-1",
        "status": status,
        "type": [{"coding": [{"display": "Lab Results Review"}]}],
        "period": {"start": "2026-08-26T20:52:29+00:00"},
        "participant": [{"individual": {"reference": "Practitioner/rev-1"}}],
        "extension": [
            {
                "url": "http://schemas.canvasmedical.com/fhir/extensions/note-id",
                "valueId": "note-1",
            }
        ],
    }
    encounter.update(overrides)  # type: ignore[arg-type]
    return encounter


def lab_report(**overrides: object) -> dict:
    report = {
        "resourceType": "DiagnosticReport",
        "id": "dr-1",
        "status": "final",
        "code": {"text": "Core Panel"},
        "subject": {"reference": "Patient/pat-1"},
        "encounter": {"reference": "Encounter/enc-1"},
        "effectiveDateTime": "2026-08-26T04:00:00+00:00",
        "issued": "2026-08-26T20:52:29+00:00",
        "presentedForm": [{"url": "https://example/report.pdf"}],
    }
    report.update(overrides)  # type: ignore[arg-type]
    return report
