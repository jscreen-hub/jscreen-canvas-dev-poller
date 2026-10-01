from billing_poller.mapper import (
    build_billing_record,
    build_ccda_url,
    claim_queue,
    extract_address,
    extract_claim_diagnoses,
    extract_full_name,
    extract_icd10,
    extract_mrn,
    extract_npi,
    extract_phone,
    extract_procedures,
    extract_test_codes,
    reviewer_reference,
)
from tests.conftest import lab_report, review_encounter

PATIENT = {
    "id": "pat-1",
    "identifier": [
        {"type": {"coding": [{"code": "SS"}]}, "value": "ignored"},
        {"type": {"coding": [{"code": "MR"}]}, "value": "MRN-9"},
    ],
    "name": [{"given": ["Ada", "B"], "family": "Lovelace"}],
    "birthDate": "1980-01-02",
    "gender": "female",
    "address": [
        {"use": "work", "line": ["1 Work Way"], "city": "Nowhere"},
        {
            "use": "home",
            "line": ["12 Main St", "Apt 4"],
            "city": "Boston",
            "state": "MA",
            "postalCode": "02101",
            "country": "US",
        },
    ],
    "telecom": [
        {"system": "email", "value": "a@example.com"},
        {"system": "phone", "value": "555-0100"},
    ],
}

CLAIM = {
    "id": "claim-1",
    "created": "2026-08-26T15:00:00+00:00",
    "extension": [
        {
            "url": "http://schemas.canvasmedical.com/fhir/extensions/claim-queue",
            "valueCoding": {"code": "NeedsCodingReview", "display": "Coding"},
        }
    ],
    "diagnosis": [
        {
            "sequence": 1,
            "diagnosisCodeableConcept": {
                "coding": [{"code": "E1165", "display": "Type 2 diabetes"}]
            },
        }
    ],
    "item": [
        {
            "sequence": 1,
            "diagnosisSequence": [1],
            "productOrService": {
                "coding": [
                    {
                        "system": "http://www.ama-assn.org/go/cpt",
                        "code": "80053",
                        "display": "Comprehensive metabolic panel",
                    }
                ]
            },
            "quantity": {"value": 1},
            "unitPrice": {"value": 47},
            "modifier": [{"coding": [{"code": "90"}]}],
        }
    ],
}

COVERAGE_PRIMARY = {
    "id": "cov-1",
    "status": "active",
    "order": 1,
    "identifier": [{"value": "MEM-1"}],
    "subscriberId": "SUB-1",
    "beneficiary": {"reference": "Patient/pat-1"},
    "relationship": {"coding": [{"code": "self", "display": "Self"}], "text": "18"},
    "payor": [{"reference": "Organization/org-1", "display": "Aetna"}],
    "period": {"start": "2026-01-01"},
    "class": [
        {"type": {"coding": [{"code": "plan"}]}, "value": "PPO Gold"},
        {"type": {"coding": [{"code": "group"}]}, "value": "GRP-7"},
    ],
}


# -- field extractors --------------------------------------------------------


def test_extract_mrn_picks_the_mr_identifier():
    assert extract_mrn(PATIENT) == "MRN-9"


def test_extract_mrn_none_safe():
    assert extract_mrn(None) is None
    assert extract_mrn({"identifier": [{"value": "x"}]}) is None


def test_extract_full_name_joins_given_and_family():
    assert extract_full_name(PATIENT) == "Ada B Lovelace"


def test_extract_full_name_none_safe():
    assert extract_full_name(None) is None
    assert extract_full_name({"name": []}) is None


def test_extract_npi():
    practitioner = {
        "identifier": [
            {"system": "other", "value": "no"},
            {"system": "http://hl7.org/fhir/sid/us-npi", "value": "1234567893"},
        ]
    }
    assert extract_npi(practitioner) == "1234567893"
    assert extract_npi(None) is None


def test_extract_address_prefers_home_over_work():
    address = extract_address(PATIENT)
    assert address["line"] == ["12 Main St", "Apt 4"]
    assert address["postal_code"] == "02101"


def test_extract_address_falls_back_to_the_only_address():
    only = {"address": [{"use": "work", "city": "Nowhere"}]}
    assert extract_address(only)["city"] == "Nowhere"
    assert extract_address({"address": []}) is None
    assert extract_address(None) is None


def test_extract_phone_skips_non_phone_telecom():
    assert extract_phone(PATIENT) == "555-0100"
    assert extract_phone({"telecom": [{"system": "email", "value": "a@b.c"}]}) is None


def test_extract_icd10_filters_by_system_and_dedupes():
    conditions = [
        {"code": {"coding": [{"system": "http://snomed.info/sct", "code": "44054006"}]}},
        {
            "code": {
                "coding": [
                    {
                        "system": "http://hl7.org/fhir/sid/icd-10-cm",
                        "code": "K219",
                        "display": "GERD",
                    }
                ]
            }
        },
        {
            "code": {
                "coding": [
                    {
                        "system": "http://hl7.org/fhir/sid/icd-10-cm",
                        "code": "K219",
                        "display": "GERD",
                    }
                ]
            }
        },
    ]
    assert extract_icd10(conditions) == [{"code": "K219", "display": "GERD"}]


def test_extract_test_codes_none_safe():
    assert extract_test_codes(None) == []
    order = {"code": {"coding": [{"system": "s", "code": "c", "display": "d"}]}}
    assert extract_test_codes(order) == [{"system": "s", "code": "c", "display": "d"}]


def test_claim_queue():
    assert claim_queue(CLAIM) == "NeedsCodingReview"
    assert claim_queue({"extension": []}) is None


def test_extract_procedures_reads_cpt_lines():
    procedures = extract_procedures(CLAIM)
    assert len(procedures) == 1
    assert procedures[0]["code"] == "80053"
    assert procedures[0]["is_cpt"] is True
    assert procedures[0]["modifiers"] == ["90"]
    assert procedures[0]["unit_price"] == 47
    assert procedures[0]["diagnosis_pointers"] == [1]


def test_extract_procedures_empty_for_an_uncoded_claim():
    """Canvas returns a claim with no `item` array until it has been coded."""
    assert extract_procedures({"id": "c", "created": "2026-01-01"}) == []
    assert extract_procedures(None) == []


def test_extract_procedures_flags_non_cpt_systems():
    claim = {
        "item": [
            {"productOrService": {"coding": [{"system": "other", "code": "Z1"}]}}
        ]
    }
    assert extract_procedures(claim)[0]["is_cpt"] is False


def test_extract_claim_diagnoses():
    assert extract_claim_diagnoses(CLAIM) == [
        {"code": "E1165", "display": "Type 2 diabetes", "sequence": 1}
    ]
    assert extract_claim_diagnoses(None) == []


def test_reviewer_reference():
    assert reviewer_reference(review_encounter()) == "Practitioner/rev-1"
    assert reviewer_reference(None) is None
    assert reviewer_reference({"participant": [{}]}) is None


def test_build_ccda_url_points_at_the_auth_host_not_fhir_host():
    url = build_ccda_url("https://jlab-dev.canvasmedical.com", "pat-1")
    assert url == (
        "https://jlab-dev.canvasmedical.com/api/data-export/ccda/pat-1"
        "?document=continuity"
    )


def test_build_ccda_url_is_none_without_a_host_or_patient():
    assert build_ccda_url(None, "pat-1") is None
    assert build_ccda_url("https://jlab-dev.canvasmedical.com", None) is None


# -- whole record ------------------------------------------------------------


def build(**overrides):
    kwargs = {
        "report": lab_report(),
        "encounter": review_encounter(),
        "patient": PATIENT,
        "order": {
            "id": "sr-1",
            "status": "active",
            "authoredOn": "2026-08-01",
            "code": {"coding": [{"system": "http://loinc.org", "code": "L1", "display": "Core Panel"}]},
        },
        "order_match": {"method": "name", "confidence": "medium", "candidates_considered": 1},
        "ordering_provider": {
            "id": "prac-1",
            "name": [{"given": ["Ian"], "family": "Lomas"}],
            "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": "111"}],
        },
        "reviewing_provider": {"id": "rev-1", "name": [{"given": ["Gene"], "family": "Counselor"}]},
        "conditions": [
            {"code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "K219", "display": "GERD"}]}}
        ],
        "coverages": [COVERAGE_PRIMARY],
        "payors": {"Organization/org-1": {"id": "org-1", "name": "Aetna"}},
        "claim": CLAIM,
        "captured_at": "2026-09-08T00:00:00+00:00",
        "auth_base_url": "https://jlab-dev.canvasmedical.com",
        "ccda_content": "<ClinicalDocument>fake ccda</ClinicalDocument>",
    }
    kwargs.update(overrides)
    return build_billing_record(**kwargs)


def test_record_shape_is_complete_when_all_data_is_present():
    record = build()
    assert record["event"] == "lab_billing_ready"
    assert record["result"]["diagnostic_report_id"] == "dr-1"
    assert record["result"]["report_pdf_url"] == "https://example/report.pdf"
    assert record["sign_off"]["encounter_type"] == "Lab Results Review"
    assert record["sign_off"]["reviewed_by"]["name"] == "Gene Counselor"
    assert record["order"]["id"] == "sr-1"
    assert record["order"]["ordering_provider"]["npi"] == "111"
    assert record["patient"]["mrn"] == "MRN-9"
    assert record["patient"]["address"]["state"] == "MA"
    assert record["patient"]["ccda_url"] == (
        "https://jlab-dev.canvasmedical.com/api/data-export/ccda/pat-1"
        "?document=continuity"
    )
    assert record["patient"]["ccda_xml"] == "<ClinicalDocument>fake ccda</ClinicalDocument>"
    assert record["diagnoses"] == [{"code": "K219", "display": "GERD"}]
    assert record["procedures"][0]["code"] == "80053"
    assert record["claim"]["queue"] == "NeedsCodingReview"
    assert record["data_gaps"] == []


def test_insurance_block_carries_the_billing_identifiers():
    insurance = build()["insurance"][0]
    assert insurance["rank"] == 1
    assert insurance["payor"]["name"] == "Aetna"
    assert insurance["member_id"] == "MEM-1"
    assert insurance["subscriber_id"] == "SUB-1"
    assert insurance["relationship"]["cms_code"] == "18"
    assert insurance["plan"] == "PPO Gold"
    assert insurance["group"] == "GRP-7"


def test_insurance_sorts_primary_before_secondary():
    secondary = {**COVERAGE_PRIMARY, "id": "cov-2", "order": 2}
    unranked = {**COVERAGE_PRIMARY, "id": "cov-3", "order": None}
    record = build(coverages=[unranked, secondary, COVERAGE_PRIMARY])
    assert [c["coverage_id"] for c in record["insurance"]] == ["cov-1", "cov-2", "cov-3"]


def test_payor_display_is_used_when_the_organization_read_fails():
    record = build(payors={})
    assert record["insurance"][0]["payor"]["name"] == "Aetna"


def test_unmatched_order_still_produces_a_record():
    record = build(order=None, order_match={"method": "none", "confidence": "none", "candidates_considered": 0}, conditions=[])
    assert record["order"] is None
    assert record["result"]["diagnostic_report_id"] == "dr-1"
    assert "no_order_match" in record["data_gaps"]


def test_low_confidence_match_is_flagged():
    record = build(order_match={"method": "nearest_prior", "confidence": "low", "candidates_considered": 3})
    assert "low_confidence_order_match" in record["data_gaps"]


def test_missing_billing_data_is_reported_as_gaps():
    record = build(coverages=[], conditions=[], claim=None, patient={"id": "p"})
    assert set(record["data_gaps"]) >= {
        "no_active_coverage",
        "no_icd10",
        "no_cpt_codes",
        "no_patient_address",
    }
    assert record["claim"] is None
    assert record["procedures"] == []


def test_ccda_url_is_none_when_no_auth_host_is_given():
    record = build(auth_base_url=None)
    assert record["patient"]["ccda_url"] is None


def test_missing_ccda_content_is_flagged_as_a_gap():
    """A patient present but no C-CDA fetched (e.g. the export failed) is a
    named gap, not a silently empty field."""
    record = build(ccda_content=None)
    assert record["patient"]["ccda_xml"] is None
    assert "no_ccda" in record["data_gaps"]


def test_ccda_gap_is_not_raised_without_a_patient():
    record = build(patient=None, ccda_content=None)
    assert record["patient"] is None
    assert "no_ccda" not in record["data_gaps"]


def test_uncoded_claim_counts_as_missing_cpt():
    record = build(claim={"id": "c-9", "created": "2026-08-26"})
    assert "no_cpt_codes" in record["data_gaps"]
    assert record["claim"]["id"] == "c-9"


def test_claim_diagnoses_satisfy_the_icd10_requirement():
    """ICD-10 on the claim counts even when the order carried none."""
    record = build(conditions=[])
    assert record["diagnoses"] == []
    assert "no_icd10" not in record["data_gaps"]


# -- CPT from the lab compendium (lab_billing_lookup plugin) -----------------

COMPENDIUM_CPT = [
    {
        "code": "80053", "display": "Core Panel",
        "system": "http://www.ama-assn.org/go/cpt", "is_cpt": True,
        "quantity": 1, "unit_price": None, "modifiers": [],
        "diagnosis_pointers": [], "source": "compendium", "order_code": "100002",
    }
]

LOOKUP = {
    "lab_report_id": "lab-1",
    "requisition_number": "REQ-9",
    "orders": [{"lab_order_id": "order-1", "lab_partner": "Labcorp", "tests": []}],
}


def test_compendium_cpt_fills_in_when_no_claim_carries_lines():
    record = build(claim=None, extra_procedures=COMPENDIUM_CPT)
    assert record["procedures"][0]["code"] == "80053"
    assert record["procedures"][0]["source"] == "compendium"
    assert "no_cpt_codes" not in record["data_gaps"]


def test_uncoded_claim_still_gets_compendium_cpt():
    record = build(claim={"id": "c-9", "created": "2026-08-26"},
                   extra_procedures=COMPENDIUM_CPT)
    assert [p["code"] for p in record["procedures"]] == ["80053"]


def test_a_coded_claim_wins_over_the_compendium():
    """A real claim reflects what was actually billed, so it takes precedence."""
    record = build(extra_procedures=COMPENDIUM_CPT)  # CLAIM has a coded 80053 line
    assert [p["code"] for p in record["procedures"]] == ["80053"]
    assert "source" not in record["procedures"][0]


def test_still_flags_missing_cpt_when_the_compendium_has_none():
    record = build(claim=None, extra_procedures=[])
    assert "no_cpt_codes" in record["data_gaps"]


def test_lab_order_block_records_the_exact_order_identity():
    record = build(lookup=LOOKUP)
    assert record["lab_order"]["lab_order_ids"] == ["order-1"]
    assert record["lab_order"]["requisition_number"] == "REQ-9"
    assert record["lab_order"]["lab_partner"] == "Labcorp"


def test_lab_order_block_is_null_without_the_plugin():
    assert build()["lab_order"] is None


# -- CPT posted on the lab report itself -------------------------------------


def report_with_codings(*codings):
    return lab_report(code={"text": "Core Panel", "coding": list(codings)})


CPT_CODING = {
    "system": "http://www.ama-assn.org/go/cpt",
    "code": "80053",
    "display": "Comprehensive metabolic panel",
}
ORDER_CODING = {"system": "http://loinc.org", "code": "100002", "display": "Core Panel"}


def test_cpt_posted_on_the_report_is_used_when_no_claim_is_coded():
    record = build(report=report_with_codings(CPT_CODING), claim=None)
    assert record["procedures"][0]["code"] == "80053"
    assert record["procedures"][0]["source"] == "lab_report"
    assert "no_cpt_codes" not in record["data_gaps"]


def test_report_cpt_is_read_alongside_an_order_code():
    """The poster can carry both: the order code links the order, the CPT bills it."""
    record = build(report=report_with_codings(ORDER_CODING, CPT_CODING), claim=None)
    assert [p["code"] for p in record["procedures"]] == ["80053"]


def test_report_cpt_outranks_the_compendium():
    record = build(
        report=report_with_codings(CPT_CODING), claim=None,
        extra_procedures=[{**COMPENDIUM_CPT[0], "code": "99999"}],
    )
    assert [p["code"] for p in record["procedures"]] == ["80053"]


def test_a_coded_claim_still_outranks_a_posted_cpt():
    """Claim lines carry no `source` key -- they came from what was billed."""
    record = build(report=report_with_codings(CPT_CODING))  # CLAIM is coded
    assert len(record["procedures"]) == 1
    assert record["procedures"][0].get("source") is None
    assert record["procedures"][0]["unit_price"] == 47  # the claim's line


def test_non_cpt_codings_on_the_report_are_not_treated_as_procedures():
    """An order code in the LOINC slot must not become a CPT line."""
    record = build(report=report_with_codings(ORDER_CODING), claim=None)
    assert record["procedures"] == []
    assert "no_cpt_codes" in record["data_gaps"]


def test_duplicate_report_cpt_codings_collapse():
    record = build(report=report_with_codings(CPT_CODING, dict(CPT_CODING)), claim=None)
    assert len(record["procedures"]) == 1
