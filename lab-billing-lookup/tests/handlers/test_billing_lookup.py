"""Tests for the billing lookup API handler.

The Canvas SDK models are not importable outside a Canvas runtime, so the module
under test is imported with its SDK dependencies stubbed (see conftest).
"""

import json
from http import HTTPStatus
from unittest.mock import MagicMock

import pytest

from lab_billing_lookup.handlers.billing_lookup import (
    BillingLookupAPI,
    _cpt_index,
    _lookup_cpt,
)


def body_of(response):
    """JSONResponse serializes to bytes; decode it back for assertions."""
    return json.loads(response.content)


def make_partner_test(order_code, cpt_code, partner="Labcorp", order_name="Panel"):
    test = MagicMock()
    test.order_code = order_code
    test.cpt_code = cpt_code
    test.order_name = order_name
    test.lab_partner = MagicMock(name=partner)
    test.lab_partner.name = partner
    return test


# -- compendium index --------------------------------------------------------


def test_cpt_index_keys_on_partner_and_code(patch_partner_tests):
    patch_partner_tests([make_partner_test("100002", "80053")])
    index = _cpt_index()
    assert index[("Labcorp", "100002")]["cpt_code"] == "80053"
    # also resolvable without the partner
    assert index[("", "100002")]["cpt_code"] == "80053"


def test_cpt_index_skips_tests_without_an_order_code(patch_partner_tests):
    patch_partner_tests([make_partner_test("", "80053"), make_partner_test("100002", "1")])
    assert len(_cpt_index()) == 2  # one entry, indexed two ways


def test_cpt_index_keeps_partners_distinct(patch_partner_tests):
    """Two partners can reuse an order code for different tests."""
    patch_partner_tests(
        [
            make_partner_test("100002", "80053", partner="Labcorp"),
            make_partner_test("100002", "84443", partner="Quest"),
        ]
    )
    index = _cpt_index()
    assert index[("Labcorp", "100002")]["cpt_code"] == "80053"
    assert index[("Quest", "100002")]["cpt_code"] == "84443"


def test_cpt_index_preserves_null_cpt(patch_partner_tests):
    patch_partner_tests([make_partner_test("100002", "")])
    assert _cpt_index()[("Labcorp", "100002")]["cpt_code"] is None


def test_lookup_falls_back_to_a_partnerless_match(patch_partner_tests):
    patch_partner_tests([make_partner_test("100002", "80053", partner="Labcorp")])
    index = _cpt_index()
    # The order's partner string doesn't match the compendium's exactly.
    assert _lookup_cpt(index, "LABCORP INC", "100002")["cpt_code"] == "80053"


def test_lookup_returns_none_for_unknown_or_missing_code(patch_partner_tests):
    patch_partner_tests([make_partner_test("100002", "80053")])
    index = _cpt_index()
    assert _lookup_cpt(index, "Labcorp", "999999") is None
    assert _lookup_cpt(index, "Labcorp", None) is None


# -- authentication ----------------------------------------------------------


def make_handler(secret="s3cret"):
    handler = BillingLookupAPI.__new__(BillingLookupAPI)
    handler.secrets = {"BILLING_LOOKUP_API_KEY": secret} if secret else {}
    return handler


def test_authenticate_accepts_the_configured_key():
    assert make_handler().authenticate(MagicMock(key="s3cret")) is True


def test_authenticate_rejects_a_wrong_key():
    assert make_handler().authenticate(MagicMock(key="wrong")) is False


def test_authenticate_rejects_when_no_key_is_configured():
    """An unset secret must fail closed, not allow everything through."""
    assert make_handler(secret=None).authenticate(MagicMock(key="anything")) is False


def test_authenticate_rejects_an_empty_key():
    assert make_handler().authenticate(MagicMock(key="")) is False


# -- /compendium -------------------------------------------------------------


def test_compendium_returns_every_coded_test(patch_partner_tests):
    patch_partner_tests(
        [
            make_partner_test("100002", "80053", order_name="Core Panel"),
            make_partner_test("100833", None, order_name="Carrier Screen"),
            make_partner_test("", "99999"),  # skipped: no order code
        ]
    )
    body = body_of(make_handler().compendium()[0])
    assert body["count"] == 2
    assert {t["order_code"] for t in body["tests"]} == {"100002", "100833"}
    assert body["tests"][1]["cpt_code"] is None


# -- /report/<id> ------------------------------------------------------------


def make_lab_test(code, name, order):
    test = MagicMock()
    test.ontology_test_code = code
    test.ontology_test_name = name
    test.order = order
    return test


def make_order(order_id="order-1", partner="Labcorp", req="REQ-9"):
    order = MagicMock()
    order.id = order_id
    order.requisition_number = req
    order.ontology_lab_partner = partner
    order.date_ordered = None
    order.is_patient_bill = False
    return order


def test_report_resolves_the_exact_order_with_cpt(patch_partner_tests, patch_report):
    patch_partner_tests([make_partner_test("100002", "80053", order_name="Core Panel")])
    order = make_order()
    patch_report(lab_report_id="lab-1", requisition="REQ-9",
                 tests=[make_lab_test("100002", "Core Panel", order)])

    handler = make_handler()
    handler.request = MagicMock(path_params={"report_id": "dr-1"})
    body = body_of(handler.report()[0])

    assert body["diagnostic_report_id"] == "dr-1"
    assert len(body["orders"]) == 1
    assert body["orders"][0]["lab_order_id"] == "order-1"
    assert body["orders"][0]["tests"][0]["cpt_code"] == "80053"


def test_report_groups_tests_under_their_own_orders(patch_partner_tests, patch_report):
    """One review can cover results from more than one order."""
    patch_partner_tests(
        [make_partner_test("100002", "80053"), make_partner_test("100833", "81443")]
    )
    order_a, order_b = make_order("order-a"), make_order("order-b")
    patch_report(tests=[
        make_lab_test("100002", "Core Panel", order_a),
        make_lab_test("100833", "Carrier Screen", order_b),
    ])

    handler = make_handler()
    handler.request = MagicMock(path_params={"report_id": "dr-1"})
    body = body_of(handler.report()[0])

    assert {o["lab_order_id"] for o in body["orders"]} == {"order-a", "order-b"}
    assert all(len(o["tests"]) == 1 for o in body["orders"])


def test_report_reports_a_test_with_no_cpt_in_the_compendium(patch_partner_tests, patch_report):
    patch_partner_tests([])
    patch_report(tests=[make_lab_test("999999", "Mystery", make_order())])

    handler = make_handler()
    handler.request = MagicMock(path_params={"report_id": "dr-1"})
    body = body_of(handler.report()[0])

    assert body["orders"][0]["tests"][0]["cpt_code"] is None


def test_report_skips_tests_with_no_order(patch_partner_tests, patch_report):
    patch_partner_tests([])
    patch_report(tests=[make_lab_test("100002", "Orphan", None)])

    handler = make_handler()
    handler.request = MagicMock(path_params={"report_id": "dr-1"})
    assert body_of(handler.report()[0])["orders"] == []


def test_report_404s_for_an_unknown_id(patch_report):
    patch_report(missing=True)
    handler = make_handler()
    handler.request = MagicMock(path_params={"report_id": "nope"})
    response = handler.report()[0]
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_report_handles_a_diagnostic_report_with_no_lab_report(patch_report):
    patch_report(no_lab=True)
    handler = make_handler()
    handler.request = MagicMock(path_params={"report_id": "dr-1"})
    body = body_of(handler.report()[0])
    assert body["orders"] == []
    assert "note" in body
