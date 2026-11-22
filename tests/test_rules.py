"""Тесты поведения правил: запуск python -m pytest -q."""
from datetime import date
from io import BytesIO

import pandas as pd
import pytest

from core.importer import read_cases
from core.models import CASE_COLUMNS, load_profile, normalize_case
from core.rules import analyze_case

PROFILE = load_profile()


def sample(**overrides):
    raw = {
        "case_id": "ПБ-TEST-001", "case_type": "standard",
        "application_date": "2026-11-17", "issued_date": "2026-11-19",
        "predecision_date": "2026-11-18", "documents": ["Заявление", "Акт обследования"],
        "documents_complete": "yes", "accrued_amount": "1500",
        "paid_amount": "1500", "exemption_code": "", "exemption_proof": "unknown",
        "comment": "",
    }
    raw.update(overrides)
    return normalize_case(raw, PROFILE)


def statuses(case):
    return {row["rule"]: row["status"] for row in analyze_case(case, PROFILE)}


def test_normal_case_passes():
    assert statuses(sample()) == {"R1": "PASS", "R2": "PASS", "R3": "PASS"}


def test_underpayment_flag():
    assert statuses(sample(paid_amount="900"))["R1"] == "FLAG"


def test_missing_amount_is_unknown_not_zero():
    assert statuses(sample(paid_amount=""))["R1"] == "NEED_DATA"


def test_valid_exemption():
    assert statuses(sample(exemption_code="DEMO_PUBLIC_USE", exemption_proof="yes",
                           accrued_amount="", paid_amount=""))["R1"] == "PASS"


def test_unknown_exemption_document():
    assert statuses(sample(exemption_code="DEMO_PUBLIC_USE", exemption_proof="unknown"))["R1"] == "NEED_DATA"


def test_invalid_exemption():
    assert statuses(sample(exemption_code="WRONG"))["R1"] == "FLAG"


def test_missing_document_with_complete_file():
    assert statuses(sample(documents=["Заявление"]))["R2"] == "FLAG"


def test_unconfirmed_completeness_is_unknown():
    assert statuses(sample(documents=["Заявление"], documents_complete="unknown"))["R2"] == "NEED_DATA"


def test_wrong_date_order_flag():
    assert statuses(sample(predecision_date="2026-11-20"))["R3"] == "FLAG"


def test_emergency_does_not_need_prior_decision():
    assert statuses(sample(case_type="emergency", predecision_date=""))["R3"] == "NOT_APPLICABLE"


def test_repeated_analysis_is_identical():
    case = sample()
    assert analyze_case(case, PROFILE) == analyze_case(case, PROFILE)


def test_bad_date_is_rejected():
    with pytest.raises(ValueError):
        sample(issued_date="31.11.2026")


def test_russian_dates_normalize_without_changing_the_result():
    assert sample(issued_date="19.11.2026") == sample(issued_date="2026-11-19")


def test_negative_amount_is_rejected():
    with pytest.raises(ValueError):
        sample(paid_amount="-1")


def test_excel_import_roundtrip():
    record = sample()
    row = {key: ("|".join(record[key]) if key == "documents" else record[key])
           for key in CASE_COLUMNS}
    content = BytesIO()
    pd.DataFrame([row], columns=CASE_COLUMNS).to_excel(content, index=False)
    content.seek(0)
    restored = read_cases(content, "cases.xlsx", PROFILE)
    assert len(restored) == 1
    assert statuses(restored[0]) == {"R1": "PASS", "R2": "PASS", "R3": "PASS"}
