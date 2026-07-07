"""Tests for config loading and FHIR base URL derivation."""

from unittest.mock import MagicMock

import pytest

import order_poller.config as config_mod
from order_poller.config import Settings, derive_fhir_base_url

FULL_ENV = {
    "CANVAS_BASE_URL": "https://jlab-dev.canvasmedical.com",
    "CANVAS_CLIENT_ID": "cid",
    "CANVAS_CLIENT_SECRET": "secret",
    "CANVAS_SCOPE": "system/*.read",
}


class TestDeriveFhirBaseUrl:
    def test_prefixes_fumage(self) -> None:
        assert (
            derive_fhir_base_url("https://jlab-dev.canvasmedical.com")
            == "https://fumage-jlab-dev.canvasmedical.com"
        )

    def test_trailing_slash_host_only(self) -> None:
        assert (
            derive_fhir_base_url("https://example.canvasmedical.com/")
            == "https://fumage-example.canvasmedical.com"
        )

    def test_already_fumage_not_double_prefixed(self) -> None:
        assert (
            derive_fhir_base_url("https://fumage-jlab-dev.canvasmedical.com")
            == "https://fumage-jlab-dev.canvasmedical.com"
        )

    def test_preserves_port(self) -> None:
        assert (
            derive_fhir_base_url("http://localhost:8000")
            == "http://fumage-localhost:8000"
        )

    def test_missing_host_raises(self) -> None:
        with pytest.raises(ValueError):
            derive_fhir_base_url("not-a-url")


class TestSettingsFromEnv:
    def test_full_env(self) -> None:
        settings = Settings.from_env(FULL_ENV)
        assert settings.auth_base_url == "https://jlab-dev.canvasmedical.com"
        assert settings.fhir_base_url == "https://fumage-jlab-dev.canvasmedical.com"
        assert settings.client_id == "cid"
        assert settings.scope == "system/*.read"
        assert settings.lookback_days == 2  # default
        assert settings.poll_interval_seconds == 300  # default

    def test_explicit_fhir_override(self) -> None:
        env = dict(FULL_ENV, CANVAS_FHIR_BASE_URL="https://custom.example.com/")
        settings = Settings.from_env(env)
        assert settings.fhir_base_url == "https://custom.example.com"

    def test_overrides_for_paths_and_intervals(self) -> None:
        env = dict(
            FULL_ENV,
            ORDER_OUTPUT_DIR="D:/orders",
            ORDER_STATE_FILE="D:/state.json",
            ORDER_LOOKBACK_DAYS="5",
            ORDER_POLL_INTERVAL="60",
        )
        settings = Settings.from_env(env)
        assert settings.output_dir == "D:/orders"
        assert settings.state_file == "D:/state.json"
        assert settings.lookback_days == 5
        assert settings.poll_interval_seconds == 60

    def test_missing_scope_is_none(self) -> None:
        env = {k: v for k, v in FULL_ENV.items() if k != "CANVAS_SCOPE"}
        assert Settings.from_env(env).scope is None

    @pytest.mark.parametrize(
        "missing", ["CANVAS_BASE_URL", "CANVAS_CLIENT_ID", "CANVAS_CLIENT_SECRET"]
    )
    def test_missing_required_raises(self, missing: str) -> None:
        env = {k: v for k, v in FULL_ENV.items() if k != missing}
        with pytest.raises(ValueError):
            Settings.from_env(env)


def test_load_settings_loads_dotenv_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub the dotenv helpers imported inside load_settings and the process env.
    import dotenv

    monkeypatch.setattr(dotenv, "find_dotenv", MagicMock(return_value="/repo/.env"))
    monkeypatch.setattr(dotenv, "load_dotenv", MagicMock(return_value=True))
    monkeypatch.setattr(config_mod.os, "environ", dict(FULL_ENV))

    settings = config_mod.load_settings()
    assert settings.client_id == "cid"
    assert settings.fhir_base_url == "https://fumage-jlab-dev.canvasmedical.com"
