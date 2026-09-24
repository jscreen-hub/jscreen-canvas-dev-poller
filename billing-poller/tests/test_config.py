import pytest

from billing_poller.config import DEFAULT_OUTPUT_DIR, Settings, derive_fhir_base_url


def test_derive_fhir_base_url_adds_prefix():
    assert (
        derive_fhir_base_url("https://jlab-dev.canvasmedical.com")
        == "https://fumage-jlab-dev.canvasmedical.com"
    )


def test_derive_fhir_base_url_is_idempotent():
    url = "https://fumage-jlab-dev.canvasmedical.com"
    assert derive_fhir_base_url(url) == url


def test_derive_fhir_base_url_keeps_port():
    assert (
        derive_fhir_base_url("https://local.test:8443")
        == "https://fumage-local.test:8443"
    )


def test_derive_fhir_base_url_rejects_hostless():
    with pytest.raises(ValueError):
        derive_fhir_base_url("not-a-url")


def test_from_env_defaults():
    settings = Settings.from_env(
        {
            "CANVAS_BASE_URL": "https://jlab-dev.canvasmedical.com/",
            "CANVAS_CLIENT_ID": "cid",
            "CANVAS_CLIENT_SECRET": "secret",
        }
    )
    assert settings.fhir_base_url == "https://fumage-jlab-dev.canvasmedical.com"
    assert settings.output_dir == DEFAULT_OUTPUT_DIR
    assert settings.scope is None
    # A full sweep by default: Canvas gives no server-side cursor for new reports.
    assert settings.lookback_days == 0


def test_from_env_overrides():
    settings = Settings.from_env(
        {
            "CANVAS_BASE_URL": "https://jlab-dev.canvasmedical.com",
            "CANVAS_CLIENT_ID": "cid",
            "CANVAS_CLIENT_SECRET": "secret",
            "CANVAS_FHIR_BASE_URL": "https://custom.example.com/",
            "CANVAS_SCOPE": "system/*.read",
            "BILLING_OUTPUT_DIR": "D:/billing",
            "BILLING_LOOKBACK_DAYS": "30",
            "BILLING_POLL_INTERVAL": "60",
            "BILLING_PAGE_SIZE": "50",
        }
    )
    assert settings.fhir_base_url == "https://custom.example.com"
    assert settings.output_dir == "D:/billing"
    assert settings.lookback_days == 30
    assert settings.poll_interval_seconds == 60
    assert settings.page_size == 50


@pytest.mark.parametrize(
    "missing", ["CANVAS_BASE_URL", "CANVAS_CLIENT_ID", "CANVAS_CLIENT_SECRET"]
)
def test_from_env_requires_credentials(missing):
    env = {
        "CANVAS_BASE_URL": "https://jlab-dev.canvasmedical.com",
        "CANVAS_CLIENT_ID": "cid",
        "CANVAS_CLIENT_SECRET": "secret",
    }
    del env[missing]
    with pytest.raises(ValueError, match=missing):
        Settings.from_env(env)
