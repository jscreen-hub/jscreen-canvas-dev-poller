"""Thin Canvas FHIR client: OAuth token management + resource reads/searches.

Auth uses the OAuth 2.0 client_credentials grant against
`{auth_base_url}/auth/token/`. FHIR requests go to the `fumage-` base URL. The
access token is cached and reused until shortly before it expires.

Source: docs.canvasmedical.com/api/customer-authentication/,
docs.canvasmedical.com/api/diagnosticreport/, docs.canvasmedical.com/api/pagination.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from billing_poller.config import Settings

# Refresh the token this many seconds before its stated expiry.
_TOKEN_SKEW_SECONDS = 60

# How many times to re-walk when the de-duplicated count falls short of `total`.
_MAX_SEARCH_PASSES = 4

# Hard ceiling on pages per walk, for a server that reports no `total` and keeps
# serving entries.
_MAX_PAGES = 200


class CanvasClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.Client | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._http = http if http is not None else httpx.Client(timeout=60.0)
        self._now = now if now is not None else time.monotonic
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._read_cache: dict[str, dict[str, Any] | None] = {}

    @property
    def auth_base_url(self) -> str:
        """The Canvas auth/instance host -- not the `fumage-` FHIR host.

        Exposed for callers building URLs to non-FHIR endpoints (e.g. the
        C-CDA export), which live on this host rather than `fhir_base_url`.
        """
        return self._settings.auth_base_url

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

    def search(self, resource_type: str, **params: str) -> list[dict[str, Any]]:
        """Return every resource matching the search.

        Canvas's search results are NOT stably ordered across requests, so
        walking `next` links a page at a time returns some records twice and
        never returns others: a 120-result ServiceRequest query paged at the
        default size of 10 yielded only 87 distinct records. This pages by
        explicit offset, de-duplicates by id, and re-walks until it has as many
        distinct records as the bundle `total` promises.
        """
        query = {k: v for k, v in params.items() if v}
        page_size = self._settings.page_size

        collected: dict[str, dict[str, Any]] = {}
        expected: int | None = None

        for _ in range(_MAX_SEARCH_PASSES):
            offset = 0
            pages = 0
            while pages < _MAX_PAGES:
                pages += 1
                response = self._http.get(
                    f"{self._settings.fhir_base_url}/{resource_type}",
                    params={**query, "_count": str(page_size), "_offset": str(offset)},
                    headers=self._headers(),
                )
                response.raise_for_status()
                bundle = response.json()
                if expected is None:
                    total = bundle.get("total")
                    expected = int(total) if isinstance(total, int) else None

                entries = [
                    entry["resource"]
                    for entry in bundle.get("entry", [])
                    if entry.get("resource") and entry["resource"].get("id")
                ]
                if not entries:
                    break
                for resource in entries:
                    collected.setdefault(str(resource["id"]), resource)
                offset += page_size
                if expected is not None and offset >= expected + page_size:
                    break

            if expected is None or len(collected) >= expected:
                break

        return list(collected.values())

    def read_reference(self, reference: str | None) -> dict[str, Any] | None:
        """Read a resource from a FHIR reference like 'Patient/abc'. None-safe.

        Reads are cached for the life of the client. One poll cycle re-reads the
        same patient, practitioner and payor across many reports, and these
        resources do not change mid-cycle.
        """
        if not reference or "/" not in reference:
            return None
        if reference in self._read_cache:
            return self._read_cache[reference]
        resource_type, _, resource_id = reference.partition("/")
        response = self._http.get(
            f"{self._settings.fhir_base_url}/{resource_type}/{resource_id}",
            headers=self._headers(),
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        self._read_cache[reference] = result
        return result

    def clear_cache(self) -> None:
        """Drop cached reads. Called between poll cycles so a long-running loop
        picks up coverage and demographic changes."""
        self._read_cache.clear()

    def close(self) -> None:
        self._http.close()
