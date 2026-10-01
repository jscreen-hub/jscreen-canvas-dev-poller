"""Shared fixtures for lab-billing-lookup tests.

The handler queries Canvas SDK Django models, which need a live Canvas database.
These fixtures patch the model managers so the handler's own logic -- the CPT
index, the partner fallback, order grouping and auth -- is tested directly.
"""

from unittest.mock import MagicMock

import pytest

import lab_billing_lookup.handlers.billing_lookup as handler_module


@pytest.fixture
def patch_partner_tests(monkeypatch):
    """Make LabPartnerTest.objects.select_related(...).all() return `tests`."""

    def _patch(tests):
        manager = MagicMock()
        manager.select_related.return_value.all.return_value = tests
        monkeypatch.setattr(
            handler_module.LabPartnerTest, "objects", manager, raising=False
        )
        return manager

    return _patch


@pytest.fixture
def patch_report(monkeypatch):
    """Stub DiagnosticReport.objects.select_related('lab').get(id=...)."""

    def _patch(tests=None, lab_report_id="lab-1", requisition="REQ-1",
               missing=False, no_lab=False):
        manager = MagicMock()
        get = manager.select_related.return_value.get

        if missing:
            class DoesNotExist(Exception):
                pass

            monkeypatch.setattr(
                handler_module.DiagnosticReport, "DoesNotExist", DoesNotExist,
                raising=False,
            )
            get.side_effect = DoesNotExist()
        else:
            diagnostic_report = MagicMock()
            if no_lab:
                diagnostic_report.lab = None
            else:
                lab_report = MagicMock()
                lab_report.id = lab_report_id
                lab_report.requisition_number = requisition
                lab_report.ordered_tests.select_related.return_value = tests or []
                diagnostic_report.lab = lab_report
            get.return_value = diagnostic_report

        monkeypatch.setattr(
            handler_module.DiagnosticReport, "objects", manager, raising=False
        )
        return manager

    return _patch
