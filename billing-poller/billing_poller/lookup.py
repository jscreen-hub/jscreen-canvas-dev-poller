"""Client for the `lab_billing_lookup` Canvas plugin.

The plugin exposes two things FHIR does not: CPT codes from the lab partner
compendium, and the exact `DiagnosticReport -> LabOrder` link. Both are ordinary
SDK model reads that simply require running inside Canvas.

This client is **optional**. When the plugin is not installed or no API key is
configured, `BillingLookup.enabled` is False and the poller falls back to its
FHIR-only behavior: heuristic order matching and no CPT codes. A lookup failure
mid-cycle is logged and degrades the same way rather than failing the record --
a billing feed that stops entirely because an optional enrichment is down is
worse than one that keeps flowing with a gap flag.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx


class BillingLookup:
    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        http: httpx.Client | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._http = http if http is not None else httpx.Client(timeout=30.0)
        self._log = log
        self._compendium: dict[str, dict[str, Any]] | None = None
        self._failed = False

    @property
    def enabled(self) -> bool:
        """True only when both the endpoint and a key are configured."""
        return bool(self._base_url and self._api_key) and not self._failed

    def _get(self, path: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            response = self._http.get(
                f"{self._base_url}{path}",
                headers={"Authorization": self._api_key, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            self._log(f"billing lookup unreachable ({exc}); continuing without it")
            self._failed = True
            return None
        if response.status_code == 404:
            return None
        if response.status_code in (401, 403):
            # A bad key will not fix itself mid-run; stop asking.
            self._log(
                f"billing lookup rejected the API key ({response.status_code}); "
                "continuing without it"
            )
            self._failed = True
            return None
        if response.status_code >= 400:
            self._log(f"billing lookup error {response.status_code} on {path}")
            return None
        result: dict[str, Any] = response.json()
        return result

    def compendium(self) -> dict[str, dict[str, Any]]:
        """order_code -> compendium entry, fetched once and cached."""
        if self._compendium is None:
            body = self._get("/compendium")
            entries = (body or {}).get("tests", [])
            self._compendium = {
                str(entry["order_code"]): entry
                for entry in entries
                if entry.get("order_code")
            }
            if self._compendium:
                with_cpt = sum(1 for e in self._compendium.values() if e.get("cpt_code"))
                self._log(
                    f"loaded {len(self._compendium)} compendium test(s), "
                    f"{with_cpt} with a CPT code"
                )
        return self._compendium

    def report(self, report_id: str) -> dict[str, Any] | None:
        """The exact order(s) behind a diagnostic report, with CPT per test."""
        return self._get(f"/report/{report_id}")

    def close(self) -> None:
        self._http.close()


def procedures_from_lookup(lookup_report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten the lookup's per-test CPT codes into procedure lines.

    Shaped to match `mapper.extract_procedures` so a record's `procedures` block
    looks the same whether the codes came from a coded Claim or the compendium.
    """
    if not lookup_report:
        return []
    procedures: list[dict[str, Any]] = []
    seen: set[str] = set()
    for order in lookup_report.get("orders", []):
        for test in order.get("tests", []):
            cpt = test.get("cpt_code")
            if not cpt or cpt in seen:
                continue
            seen.add(str(cpt))
            procedures.append(
                {
                    "code": str(cpt),
                    "display": test.get("compendium_name") or test.get("name"),
                    "system": "http://www.ama-assn.org/go/cpt",
                    "is_cpt": True,
                    "quantity": 1,
                    "unit_price": None,
                    "modifiers": [],
                    "diagnosis_pointers": [],
                    "source": "compendium",
                    "order_code": test.get("order_code"),
                }
            )
    return procedures


def order_codes_from_lookup(lookup_report: dict[str, Any] | None) -> set[str]:
    """Every lab partner order code the lookup attributes to this result.

    These are authoritative, so matching them against a candidate
    ServiceRequest's codings turns a guess into an exact link.
    """
    if not lookup_report:
        return set()
    return {
        str(test["order_code"])
        for order in lookup_report.get("orders", [])
        for test in order.get("tests", [])
        if test.get("order_code")
    }


def procedures_from_compendium(
    order_codes: list[str], compendium: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Resolve CPT lines for order codes posted on the lab report.

    This is the path used when the result poster puts the order's test codes on
    `labReport.code.coding` and leaves CPT to us: each code is looked up in the
    cached compendium. One report can carry many test codes, so this returns one
    line per distinct CPT.

    A code the compendium does not know, or knows with no CPT configured, yields
    no line -- the caller reports that as a missing-CPT gap rather than inventing
    a code.
    """
    procedures: list[dict[str, Any]] = []
    seen: set[str] = set()
    for order_code in order_codes:
        entry = compendium.get(str(order_code))
        if not entry:
            continue
        cpt = entry.get("cpt_code")
        if not cpt or str(cpt) in seen:
            continue
        seen.add(str(cpt))
        procedures.append(
            {
                "code": str(cpt),
                "display": entry.get("order_name"),
                "system": "http://www.ama-assn.org/go/cpt",
                "is_cpt": True,
                "quantity": 1,
                "unit_price": None,
                "modifiers": [],
                "diagnosis_pointers": [],
                "source": "compendium",
                "order_code": str(order_code),
            }
        )
    return procedures


def unresolved_order_codes(
    order_codes: list[str], compendium: dict[str, dict[str, Any]]
) -> list[str]:
    """Posted order codes that produced no CPT, so the gap is nameable.

    Distinguishes "the compendium has never heard of this code" from "the code
    exists but has no CPT configured in Canvas" -- different fixes.
    """
    unresolved: list[str] = []
    for order_code in order_codes:
        entry = compendium.get(str(order_code))
        if not entry or not entry.get("cpt_code"):
            unresolved.append(str(order_code))
    return unresolved
