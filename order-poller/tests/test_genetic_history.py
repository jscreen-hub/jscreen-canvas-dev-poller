"""Tests for the Finch genetic/family-history enrichment.

Fixtures are trimmed-down real shapes, confirmed live against jlab-dev test
patient 07851fbbbe90498abac0a97597d9da3f.
"""

from __future__ import annotations

import httpx
import pytest

from order_poller.genetic_history import (
    GeneticHistoryClient,
    build_genetic_history,
    extract_assessment_results,
    extract_cancer_history,
    extract_documents,
    extract_genetic_assessment,
)

AUTH = "https://jlab-dev.canvasmedical.com/auth/token/"
FHIR = "https://fumage-jlab-dev.canvasmedical.com"


def _questionnaire_response(display: str, items: list[dict]) -> dict:
    return {
        "resourceType": "QuestionnaireResponse",
        "_questionnaire": {
            "extension": [
                {
                    "url": "http://hl7.org/fhir/StructureDefinition/display",
                    "valueString": display,
                }
            ]
        },
        "item": items,
    }


def _coded_item(link_id, text, code, display):
    return {
        "linkId": link_id,
        "text": text,
        "answer": [
            {
                "valueCoding": {
                    "system": "http://schemas.jlab-dev.canvasmedical.com/fhir/systems/internal",
                    "code": code,
                    "display": display,
                }
            }
        ],
    }


def _string_item(link_id, text, value):
    return {"linkId": link_id, "text": text, "answer": [{"valueString": value}]}


def _unanswered_item(link_id, text):
    return {"linkId": link_id, "text": text}


# -- extract_cancer_history ---------------------------------------------------


def test_extract_cancer_history_flattens_a_personal_occurrence():
    response = _questionnaire_response(
        "Finch Cancer History (v1)",
        [
            _coded_item(
                "a", "Personal or family history",
                "FINCH_GA_CANCER_HISTORY_V1_SCOPE_PERSONAL", "Personal",
            ),
            _coded_item(
                "b", "Reported cancer type (Finch glossary)",
                "FINCH_GA_CANCER_HISTORY_V1_CANCER_TYPE_54", "Ampulla of Vater",
            ),
            _string_item("c", "Occurrence key within this assessment", "personal:0"),
            _unanswered_item("d", "Recorded relationship to the patient"),
        ],
    )

    [occurrence] = extract_cancer_history([response])

    assert occurrence == {
        "personal_or_family_history": "Personal",
        "reported_cancer_type_finch_glossary": "Ampulla of Vater",
        "occurrence_key_within_this_assessment": "personal:0",
    }
    # unanswered items are omitted entirely, not written as null
    assert "recorded_relationship_to_the_patient" not in occurrence


def test_extract_cancer_history_returns_one_entry_per_occurrence():
    responses = [
        _questionnaire_response("Finch Cancer History (v1)", [_string_item("a", "Occurrence key", "personal:0")]),
        _questionnaire_response("Finch Cancer History (v1)", [_string_item("a", "Occurrence key", "family:0")]),
        _questionnaire_response("Finch Genetic Assessment (v1)", [_string_item("a", "x", "y")]),
    ]
    assert len(extract_cancer_history(responses)) == 2


def test_extract_cancer_history_ignores_non_cancer_history_responses():
    responses = [
        _questionnaire_response("Finch Genetic Assessment (v1)", [_string_item("a", "x", "y")]),
        _questionnaire_response("Purchase Flow Questionnaire (v47)", [_string_item("a", "x", "y")]),
    ]
    assert extract_cancer_history(responses) == []


# -- extract_genetic_assessment / extract_assessment_results ------------------


def test_extract_genetic_assessment_matches_only_the_assessment_not_results():
    responses = [
        _questionnaire_response(
            "Finch Genetic Assessment (v1)",
            [_coded_item("a", "Personal history of cancer (patient-reported)",
                         "FINCH_GA_V1_PCH_HAD_CANCER_YES", "Yes")],
        ),
        _questionnaire_response(
            "Finch Genetic Assessment Results (v1) (v53)",
            [_coded_item("b", "NCCN Breast Guidelines eligibility",
                         "FINCH_GA_RESULTS_V1_NCCN_BREAST_ELIGIBLE", "Eligible")],
        ),
    ]

    assessment = extract_genetic_assessment(responses)
    results = extract_assessment_results(responses)

    assert assessment == {"personal_history_of_cancer_patient_reported": "Yes"}
    assert results == {"nccn_breast_guidelines_eligibility": "Eligible"}


def test_extract_genetic_assessment_is_none_when_absent():
    responses = [_questionnaire_response("Finch Cancer History (v1)", [_string_item("a", "x", "y")])]
    assert extract_genetic_assessment(responses) is None
    assert extract_assessment_results(responses) is None


# -- extract_documents ---------------------------------------------------------


def _document(description, content_type="application/pdf", url="https://example/doc"):
    return {
        "description": description,
        "content": [{"attachment": {"contentType": content_type, "url": url}}],
    }


def test_extract_documents_keeps_only_finch_documents():
    documents = [
        _document("Finch Assessment — Complete Record"),
        _document("Finch Pedigree"),
        _document("Patient Consent"),
        _document("Payer letter"),
    ]

    result = extract_documents(documents)

    assert [d["description"] for d in result] == [
        "Finch Assessment — Complete Record", "Finch Pedigree",
    ]
    assert result[0]["content_type"] == "application/pdf"
    assert result[0]["url"] == "https://example/doc"


def test_extract_documents_is_empty_list_not_none_when_no_finch_docs():
    assert extract_documents([_document("Patient Consent")]) == []


# -- build_genetic_history -----------------------------------------------------


class _FakeClient:
    def __init__(self, responses=None, documents=None, error=None):
        self._responses = responses or []
        self._documents = documents or []
        self._error = error

    def search(self, resource_type, **params):
        if self._error:
            raise self._error
        return self._responses if resource_type == "QuestionnaireResponse" else self._documents


def test_build_genetic_history_assembles_all_four_blocks():
    responses = [
        _questionnaire_response("Finch Cancer History (v1)", [_string_item("a", "Occurrence key", "personal:0")]),
        _questionnaire_response("Finch Genetic Assessment (v1)", [_string_item("a", "x", "y")]),
        _questionnaire_response("Finch Genetic Assessment Results (v1) (v53)", [_string_item("a", "x", "y")]),
    ]
    documents = [_document("Finch Pedigree")]
    client = _FakeClient(responses=responses, documents=documents)

    result = build_genetic_history(client, "pat-1", log=lambda m: None)

    assert result is not None
    assert len(result["cancer_history"]) == 1
    assert result["genetic_assessment"] == {"x": "y"}
    assert result["assessment_results"] == {"x": "y"}
    assert len(result["documents"]) == 1


def test_build_genetic_history_is_none_when_patient_has_no_finch_data():
    client = _FakeClient(responses=[], documents=[_document("Patient Consent")])
    assert build_genetic_history(client, "pat-1", log=lambda m: None) is None


def test_build_genetic_history_fails_safely_and_never_raises():
    client = _FakeClient(error=RuntimeError("fhir down"))
    assert build_genetic_history(client, "pat-1", log=lambda m: None) is None


# -- GeneticHistoryClient (token + pagination) ---------------------------------


def _client_with(handler, **overrides) -> GeneticHistoryClient:
    return GeneticHistoryClient(
        auth_base_url=overrides.get("auth_base_url", "https://jlab-dev.canvasmedical.com"),
        fhir_base_url=overrides.get("fhir_base_url", FHIR),
        client_id="cid",
        client_secret="secret",
        scope=overrides.get("scope"),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        page_size=overrides.get("page_size", 50),
    )


def _token_response(expires_in=3600):
    return httpx.Response(200, json={"access_token": "tok", "expires_in": expires_in})


def _bundle(resources, total=None):
    return httpx.Response(
        200,
        json={
            "resourceType": "Bundle",
            "total": total if total is not None else len(resources),
            "entry": [{"resource": r} for r in resources],
        },
    )


def test_fetches_a_token_and_searches_the_fhir_host():
    seen = {}

    def handler(request):
        if str(request.url) == AUTH:
            return _token_response()
        seen["url"] = str(request.url)
        seen["auth_header"] = request.headers.get("authorization")
        return _bundle([{"id": "qr-1"}])

    client = _client_with(handler)
    results = client.search("QuestionnaireResponse", patient="Patient/pat-1")

    assert results == [{"id": "qr-1"}]
    assert seen["url"].startswith(f"{FHIR}/QuestionnaireResponse")
    assert "patient=Patient%2Fpat-1" in seen["url"]
    assert seen["auth_header"] == "Bearer tok"


def test_token_is_cached_across_searches():
    calls = {"token": 0}

    def handler(request):
        if str(request.url) == AUTH:
            calls["token"] += 1
            return _token_response()
        return _bundle([])

    client = _client_with(handler)
    client.search("QuestionnaireResponse", patient="Patient/pat-1")
    client.search("DocumentReference", patient="Patient/pat-1")

    assert calls["token"] == 1


def test_pagination_dedupes_across_pages():
    """Mirrors billing_poller.CanvasClient.search's own instability fix:
    page by explicit _offset, dedupe by id, re-walk until the deduplicated
    count matches `total`."""

    def handler(request):
        if str(request.url) == AUTH:
            return _token_response()
        offset = int(httpx.QueryParams(request.url.query).get("_offset", "0"))
        if offset == 0:
            # A record repeated across pages (Canvas's own instability) must
            # still only be counted once.
            return _bundle([{"id": "a"}, {"id": "b"}], total=3)
        if offset == 2:
            return _bundle([{"id": "b"}, {"id": "c"}], total=3)
        return _bundle([], total=3)

    client = _client_with(handler, page_size=2)
    results = client.search("QuestionnaireResponse", patient="Patient/pat-1")

    assert {r["id"] for r in results} == {"a", "b", "c"}
