"""Resolve which lab order a signed-off result belongs to.

Canvas does NOT populate `DiagnosticReport.basedOn`, so there is no id-level
link from a result back to the ServiceRequest that produced it. The link has to
be inferred from the patient, the test identity and the dates. This module does
that inference and, critically, REPORTS HOW it got there so a downstream biller
can tell an exact match from a guess.

Strategy, best evidence first:

1. `code`  - a coding on the report matches a coding on the order (same system
             and code). Exact test identity. Confidence: high.
2. `name`  - the report's free-text name matches an order's test display name.
             Canvas reports usually carry only `code.text` (e.g. "Core Panel"),
             and a multi-test report concatenates names ("Core Panel, Test 2"),
             so this compares normalized name tokens. Confidence: medium.
3. `sole_open_order` - exactly one committed lab order for that patient predates
             the result. Nothing else it could be. Confidence: medium.
4. `nearest_prior` - several candidates, none matched on code or name; take the
             one authored closest before the result. Confidence: low.
5. `none`  - no committed lab order predates the result. The record is still
             exported, with `order: null`. Confidence: none.

Candidates are always restricted to committed (signed) lab orders for the same
patient authored on or before the result date.
"""

from __future__ import annotations

import re
from typing import Any

from billing_poller.config import CPT_SYSTEM

# Punctuation and filler that differ between a lab's report name and the order's
# catalog name without changing which test is meant.
_NOISE = re.compile(r"[^a-z0-9 ]+")
_STOPWORDS = frozenset({"panel", "test", "screen", "with", "and", "the", "w"})


def _codings(resource: dict[str, Any] | None) -> set[tuple[str, str]]:
    """(system, code) pairs on a resource's `code.coding`."""
    if not resource:
        return set()
    pairs: set[tuple[str, str]] = set()
    for coding in resource.get("code", {}).get("coding", []):
        system, code = coding.get("system"), coding.get("code")
        if system and code:
            pairs.add((str(system), str(code)))
    return pairs


def _name_tokens(text: str | None) -> set[str]:
    """Meaningful lowercase word tokens from a test name."""
    if not text:
        return set()
    words = _NOISE.sub(" ", text.lower()).split()
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def report_names(report: dict[str, Any]) -> list[str]:
    """The test names on a report.

    Canvas joins the names of every test on a multi-test report into one
    `code.text` string, so split it back apart.
    """
    text = report.get("code", {}).get("text")
    if not text:
        return []
    return [part.strip() for part in str(text).split(",") if part.strip()]


def order_names(order: dict[str, Any]) -> list[str]:
    names = [
        coding.get("display")
        for coding in order.get("code", {}).get("coding", [])
        if coding.get("display")
    ]
    text = order.get("code", {}).get("text")
    if text:
        names.append(text)
    return [str(n) for n in names]


def _authored(order: dict[str, Any]) -> str:
    return str(order.get("authoredOn") or "")


def eligible_orders(
    orders: list[dict[str, Any]],
    committed_statuses: tuple[str, ...],
    result_date: str | None,
) -> list[dict[str, Any]]:
    """Committed lab orders for the patient authored on or before the result.

    Comparison is on the date part only: a specimen collected on the same day an
    order was signed is a legitimate match even if the timestamps run backwards.
    """
    cutoff = (result_date or "")[:10]
    eligible = [o for o in orders if o.get("status") in committed_statuses]
    if cutoff:
        eligible = [o for o in eligible if _authored(o)[:10] <= cutoff]
    return sorted(eligible, key=_authored, reverse=True)


def report_order_codes(report: dict[str, Any]) -> list[str]:
    """Lab partner order codes posted on the report.

    Canvas files these under the LOINC system even though they are the lab
    partner's own order codes, so this deliberately does not filter by system --
    it returns every code on `labReport.code.coding` and lets the compendium
    decide which ones it recognises. CPT codings are excluded: those are billing
    codes, not order codes.
    """
    return [
        str(coding["code"])
        for coding in report.get("code", {}).get("coding", [])
        if coding.get("code") and coding.get("system") != CPT_SYSTEM
    ]


def orders_matching_codes(
    order_codes: set[str], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every candidate order sharing a code with the report.

    More than one means the report spans multiple orders, which the single-order
    record shape cannot represent -- the caller flags it rather than silently
    binding to whichever matched first.
    """
    matched: list[dict[str, Any]] = []
    for order in candidates:
        codes = {
            str(coding["code"])
            for coding in order.get("code", {}).get("coding", [])
            if coding.get("code")
        }
        if codes & order_codes:
            matched.append(order)
    return matched


def match_by_order_codes(
    order_codes: set[str],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Match using authoritative order codes from the lab_billing_lookup plugin.

    The plugin resolves the result's order through Canvas's own foreign keys, so
    a candidate whose codings include one of those order codes IS the order --
    no inference involved. Returns (None, meta) when the plugin is unavailable or
    none of the candidates carry a listed code, leaving the caller to fall back
    to `match_order`.
    """
    meta: dict[str, Any] = {
        "method": "none",
        "confidence": "none",
        "candidates_considered": len(candidates),
    }
    if not order_codes or not candidates:
        return None, meta

    for order in candidates:
        codes = {
            str(coding["code"])
            for coding in order.get("code", {}).get("coding", [])
            if coding.get("code")
        }
        shared = codes & order_codes
        if shared:
            meta.update(
                method="lookup_order_code",
                confidence="exact",
                matched_on=sorted(shared),
            )
            return order, meta
    return None, meta


def match_order(
    report: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Pick the order behind a result. Returns (order, match_metadata).

    `candidates` must already be narrowed to eligible orders (see
    `eligible_orders`), most recently authored first.
    """
    meta: dict[str, Any] = {
        "method": "none",
        "confidence": "none",
        "candidates_considered": len(candidates),
    }
    if not candidates:
        return None, meta

    # 1. exact coding match
    report_codes = _codings(report)
    if report_codes:
        for order in candidates:
            shared = report_codes & _codings(order)
            if shared:
                meta.update(
                    method="code",
                    confidence="high",
                    matched_on=sorted(f"{s}|{c}" for s, c in shared),
                )
                return order, meta

    # 2. test-name match
    report_tokens = [_name_tokens(name) for name in report_names(report)]
    report_tokens = [t for t in report_tokens if t]
    if report_tokens:
        for order in candidates:
            for order_name in order_names(order):
                order_tokens = _name_tokens(order_name)
                if order_tokens and any(t == order_tokens for t in report_tokens):
                    meta.update(
                        method="name", confidence="medium", matched_on=[order_name]
                    )
                    return order, meta

    # 3. only one thing it could be
    if len(candidates) == 1:
        meta.update(method="sole_open_order", confidence="medium")
        return candidates[0], meta

    # 4. fall back to the most recent order preceding the result
    meta.update(method="nearest_prior", confidence="low")
    return candidates[0], meta
