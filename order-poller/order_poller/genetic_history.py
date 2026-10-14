"""Attach a patient's Finch family/genetic-history intake to their lab orders.

JLab's genetic-test purchase flow has patients complete a third-party
("Finch") intake that Canvas stores as `QuestionnaireResponse` resources
(one "Finch Cancer History" response per cancer occurrence reported --
personal or per relative -- plus one "Finch Genetic Assessment" covering
reproductive/surgical history, and one "Finch Genetic Assessment Results"
with NCCN per-guideline eligibility and Tyrer-Cuzick breast-cancer risk
scores) and as three PDF `DocumentReference`s under Medical Documents
("Finch Assessment — Complete Record", "Finch Pedigree", "Finch Clinical
Summary"). Confirmed live on jlab-dev against test patient
07851fbbbe90498abac0a97597d9da3f.

This reads both resource types over FHIR, using the same OAuth credentials
and `fhir_base_url` `Settings` already carries (unused for order data since
the cutover to `lab_billing_lookup` -- see `source.py`) -- no new plugin,
deploy, or credentials needed.

Per-patient, not per-order: a Finch intake is a one-time event per patient,
not tied to any single lab order's note/encounter, and Canvas's
`QuestionnaireResponse` carries a note-id extension pointing at whatever
note it was imported into -- not necessarily the same note as any specific
order. Attaching by patient id (not by matching notes) is simpler and
avoids inventing a correlation Canvas doesn't expose, at the cost of the
same block being duplicated across every order for that patient -- an
accepted tradeoff (see the project's own decision on this).

This is optional, best-effort enrichment layered on top of the core order
feed, the same way `source.LabOrderSource` already is for CPT/ICD-10 via
`lab_billing_lookup`: a fetch failure for one patient must never fail that
order record, let alone the poll cycle. `genetic_history` comes back `None`
(and the failure is logged) rather than raising.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

import httpx

# Match is deliberately substring/case-insensitive, not an exact version
# string -- Finch's own questionnaire display names carry a version suffix
# ("Finch Cancer History (v1)") that will change over time without the
# underlying meaning changing. "Results" must be checked first since
# "Finch Genetic Assessment Results (v1) (v53)" also contains
# "Finch Genetic Assessment".
_CANCER_HISTORY_MARKER = "finch cancer history"
_ASSESSMENT_RESULTS_MARKER = "finch genetic assessment results"
_ASSESSMENT_MARKER = "finch genetic assessment"
_DOCUMENT_DESCRIPTION_PREFIX = "finch"

_TOKEN_SKEW_SECONDS = 60
_MAX_SEARCH_PASSES = 4
_MAX_PAGES = 200


class GeneticHistoryClient:
    """Minimal FHIR client: OAuth token management + paginated search.

    Deliberately scoped to just `search()` -- this module only ever reads
    `QuestionnaireResponse` and `DocumentReference` by patient. The
    offset-based pagination (page by explicit `_offset`, dedupe by id,
    re-walk until the deduplicated count matches `total`) mirrors
    `billing_poller.canvas_client.CanvasClient.search()` exactly: Canvas's
    `next`-link pagination silently drops records under concurrent writes,
    a bug already diagnosed and fixed there -- this reuses that fix rather
    than risking reintroducing the original bug.
    """

    def __init__(
        self,
        auth_base_url: str,
        fhir_base_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        http: httpx.Client | None = None,
        now: Callable[[], float] | None = None,
        page_size: int = 50,
    ) -> None:
        self._auth_base_url = auth_base_url.rstrip("/")
        self._fhir_base_url = fhir_base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._http = http if http is not None else httpx.Client(timeout=60.0)
        self._now = now if now is not None else time.monotonic
        self._page_size = page_size
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def _get_token(self) -> str:
        if self._token is not None and self._now() < self._token_expiry:
            return self._token
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._scope:
            data["scope"] = self._scope
        response = self._http.post(
            f"{self._auth_base_url}/auth/token/",
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

    def search(self, resource_type: str, **params: str) -> list[dict[str, Any]]:
        query = {k: v for k, v in params.items() if v}
        collected: dict[str, dict[str, Any]] = {}
        expected: int | None = None

        for _ in range(_MAX_SEARCH_PASSES):
            offset = 0
            pages = 0
            while pages < _MAX_PAGES:
                pages += 1
                response = self._http.get(
                    f"{self._fhir_base_url}/{resource_type}",
                    params={
                        **query,
                        "_count": str(self._page_size),
                        "_offset": str(offset),
                    },
                    headers={
                        "Authorization": f"Bearer {self._get_token()}",
                        "Accept": "application/json",
                    },
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
                offset += self._page_size
                if expected is not None and offset >= expected + self._page_size:
                    break
            if expected is None or len(collected) >= expected:
                break

        return list(collected.values())

    def close(self) -> None:
        self._http.close()


# -- pure extraction (no I/O; takes already-fetched FHIR resources) ---------


def _questionnaire_display(response: dict[str, Any]) -> str:
    for ext in (response.get("_questionnaire") or {}).get("extension", []):
        if ext.get("url") == "http://hl7.org/fhir/StructureDefinition/display":
            return str(ext.get("valueString") or "")
    return ""


def _slug(text: str) -> str:
    """'Recorded diagnosis age (years)' -> 'recorded_diagnosis_age_years'."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower())
    return slug.strip("_")


def _answer_value(item: dict[str, Any]) -> Any:
    """One item's answer(s) as plain value(s) -- a single scalar, or a list
    for a multi-select (multiple `answer` entries)."""
    values = []
    for answer in item.get("answer", []):
        if "valueString" in answer:
            values.append(answer["valueString"])
        elif "valueDecimal" in answer:
            values.append(answer["valueDecimal"])
        elif "valueCoding" in answer:
            coding = answer["valueCoding"]
            values.append(coding.get("display") or coding.get("code"))
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _flatten_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    """`QuestionnaireResponse.item` -> `{slugified question text: answer}`.

    Generic rather than a hardcoded field-by-field mapping on purpose: the
    three Finch questionnaires carry on the order of 15-40 questions each,
    and a hand-maintained mapping would silently drop any question Finch
    adds or renames in a future version. An item with no `answer` at all
    (common -- e.g. "Recorded relationship to the patient" is blank for a
    personal, non-family occurrence) is omitted rather than written as
    `null`, so a sparse record reads as "not applicable here" rather than
    "fetched but empty".
    """
    flattened: dict[str, Any] = {}
    for item in items:
        if not item.get("answer"):
            continue
        text = item.get("text")
        if not text:
            continue
        flattened[_slug(text)] = _answer_value(item)
    return flattened


def extract_cancer_history(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One dict per reported cancer occurrence (personal or per relative)."""
    return [
        _flatten_items(r.get("item", []))
        for r in responses
        if _CANCER_HISTORY_MARKER in _questionnaire_display(r).lower()
    ]


def extract_genetic_assessment(responses: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one-per-patient reproductive/surgical/genetic-testing intake."""
    for r in responses:
        display = _questionnaire_display(r).lower()
        if _ASSESSMENT_MARKER in display and _ASSESSMENT_RESULTS_MARKER not in display:
            return _flatten_items(r.get("item", []))
    return None


def extract_assessment_results(responses: list[dict[str, Any]]) -> dict[str, Any] | None:
    """NCCN per-guideline eligibility + Tyrer-Cuzick breast-cancer risk."""
    for r in responses:
        if _ASSESSMENT_RESULTS_MARKER in _questionnaire_display(r).lower():
            return _flatten_items(r.get("item", []))
    return None


def extract_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The Finch PDFs under Medical Documents (complete record, pedigree,
    clinical summary) -- link only; never fetched/embedded here (PHI
    footprint, same reasoning as billing-poller's `ccda_url`)."""
    results = []
    for doc in documents:
        description = str(doc.get("description") or "")
        if not description.lower().startswith(_DOCUMENT_DESCRIPTION_PREFIX):
            continue
        attachment = ((doc.get("content") or [{}])[0]).get("attachment", {})
        results.append(
            {
                "description": description,
                "content_type": attachment.get("contentType"),
                "url": attachment.get("url"),
            }
        )
    return results


def build_genetic_history(
    client: GeneticHistoryClient,
    patient_id: str,
    log: Callable[[str], None] = print,
) -> dict[str, Any] | None:
    """Fetch and assemble one patient's `genetic_history` block.

    Returns `None` (logged, never raised) on any fetch failure, or if the
    patient simply has no Finch data at all -- both read the same to a
    downstream consumer: there is nothing to attach.
    """
    try:
        responses = client.search(
            "QuestionnaireResponse", patient=f"Patient/{patient_id}"
        )
        documents = client.search("DocumentReference", patient=f"Patient/{patient_id}")
    except Exception as exc:  # noqa: BLE001 - optional enrichment, never fatal
        log(
            f"genetic history fetch failed for patient {patient_id} ({exc}); "
            "continuing without it"
        )
        return None

    cancer_history = extract_cancer_history(responses)
    genetic_assessment = extract_genetic_assessment(responses)
    assessment_results = extract_assessment_results(responses)
    finch_documents = extract_documents(documents)

    if not (cancer_history or genetic_assessment or assessment_results or finch_documents):
        return None

    return {
        "cancer_history": cancer_history,
        "genetic_assessment": genetic_assessment,
        "assessment_results": assessment_results,
        "documents": finch_documents,
    }
