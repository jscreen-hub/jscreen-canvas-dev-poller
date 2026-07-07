"""Thin Canvas FHIR client: OAuth token management + resource reads/searches.

Auth uses the OAuth 2.0 client_credentials grant against
`{auth_base_url}/auth/token/`. FHIR requests go to the `fumage-` base URL. The
access token is cached and reused until shortly before it expires.

Source: docs.canvasmedical.com/api/customer-authentication/,
docs.canvasmedical.com/api/servicerequest/, docs.canvasmedical.com/api/pagination.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from order_poller.config import Settings

# Refresh the token this many seconds before its stated expiry.
_TOKEN_SKEW_SECONDS = 60


class CanvasClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.Client | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._http = http if http is not None else httpx.Client(timeout=30.0)
        self._now = now if now is not None else time.monotonic
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # -- auth ---------------------------------------------------------------

    def _get_token(self) -> str:
        if self._token is not None and self._now() < self._token_expiry:
            return self._token

        data = {
            "grant_type": "client_credentials",
            "client_id": self._settings.client_id,
            "client_secret": self._settings.client_secret,
        }
        if self._settings.scope:
            data["scope"] = self._settings.scope

        response = self._http.post(
            f"{self._settings.auth_base_url}/auth/token/",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        body = response.json()
        token = body["access_token"]
        expires_in = int(body.get("expires_in", 3600))
        self._token = token
        self._token_expiry = self._now() + expires_in - _TOKEN_SKEW_SECONDS
        return token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
        }

    # -- FHIR ---------------------------------------------------------------

    def search_service_requests(
        self, category: str, authored_ge: str | None = None
    ) -> list[dict[str, Any]]:
        """Return all ServiceRequest resources matching category (all pages)."""
        query: dict[str, str] = {"category": category}
        if authored_ge:
            query["authored"] = f"ge{authored_ge}"

        params: dict[str, str] | None = query
        url: str | None = f"{self._settings.fhir_base_url}/ServiceRequest"
        resources: list[dict[str, Any]] = []
        while url:
            response = self._http.get(url, params=params, headers=self._headers())
            response.raise_for_status()
            bundle = response.json()
            for entry in bundle.get("entry", []):
                resource = entry.get("resource")
                if resource:
                    resources.append(resource)
            url = _next_link(bundle)
            params = None  # the next-page URL already carries the query string
        return resources

    def read_reference(self, reference: str | None) -> dict[str, Any] | None:
        """Read a resource from a FHIR reference like 'Patient/abc'. None-safe."""
        if not reference or "/" not in reference:
            return None
        resource_type, _, resource_id = reference.partition("/")
        response = self._http.get(
            f"{self._settings.fhir_base_url}/{resource_type}/{resource_id}",
            headers=self._headers(),
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def close(self) -> None:
        self._http.close()


def _next_link(bundle: dict[str, Any]) -> str | None:
    for link in bundle.get("link", []):
        if link.get("relation") == "next":
            url = link.get("url")
            return str(url) if url else None
    return None
