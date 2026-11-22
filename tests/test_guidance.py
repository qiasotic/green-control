"""Контрольные действия должны соответствовать только фактически установленным статусам."""
from core.guidance import build_review_plan, format_review_sheet
from core.models import load_profile, normalize_case
from core.rules import analyze_case

PROFILE = load_profile()


def case(**kwargs):
    raw = {
        "case_id": "ПБ-ПРОВЕРКА", "case_type": "standard",
        "documents": ["Заявление", "Акт обследования"],
        "documents_complete": "yes", "issued_date": "2026-11-19",
        "predecision_date": "2026-11-18",
        "accrued_amount": 1200, "paid_amount": 1200,
    }
    raw.update(kwargs)
    return normalize_case(raw, PROFILE)


def test_normal_case_has_no_recommendations():
    assert build_review_plan(case(), PROFILE) == []


def test_incomplete_finance_requests_exact_missing_field():
    result = build_review_plan(case(paid_amount=None), PROFILE)
    assert [p['rule'] for p in result] == ['R1']
    assert result[0]['status'] == 'NEED_DATA'
    assert 'оплаченной' in ' '.join(result[0]['steps'])


def test_unknown_document_completeness_is_not_accusation():
    result = build_review_plan(case(documents=['Заявление'], documents_complete='unknown'), PROFILE)
    assert result[0]['rule'] == 'R2'
    assert result[0]['status'] == 'NEED_DATA'
    assert 'не доказанное отсутствие' in ' '.join(result[0]['steps'])


def test_confirmed_missing_doc_creates_action():
    result = build_review_plan(case(documents=['Заявление']), PROFILE)
    assert result[0]['status'] == 'FLAG'
    assert 'Акт обследования' in ' '.join(result[0]['steps'])


def test_bad_date_order_creates_verification_instead_of_guilt():
    result = build_review_plan(case(predecision_date='2026-11-20'), PROFILE)
    assert result[0]['rule'] == 'R3'
    assert 'Сопоставить' in ' '.join(result[0]['steps'])
    assert 'коррупц' not in ' '.join(result[0]['steps']).lower()


def test_flag_precedes_need_data():
    result = build_review_plan(case(predecision_date='2026-11-20', documents_complete='unknown', paid_amount=None), PROFILE)
    assert [p['status'] for p in result] == ['FLAG', 'NEED_DATA', 'NEED_DATA']


def test_no_r3_steps_for_emergency_type():
    result = build_review_plan(case(case_type='emergency', predecision_date=''), PROFILE)
    assert all(p['rule'] != 'R3' for p in result)


def test_review_sheet_is_deterministic_and_contains_rules():
    c = case(predecision_date='2026-11-20')
    a = format_review_sheet(c, PROFILE)
    assert a == format_review_sheet(c, PROFILE)
    assert all(f"Проверка {number}." in a for number in range(1, 4))
    assert "20.11.2026" in a and "2026-11-20" not in a
    assert 'РЕШЕНИЕ' in a.upper()
    assert 'не является доказательством' in a


def test_provided_outcomes_are_reused():
    c = case()
    outcomes = analyze_case(c, PROFILE)
    assert build_review_plan(c, PROFILE, outcomes) == build_review_plan(c, PROFILE)
