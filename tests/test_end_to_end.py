"""Проверки полного цикла без необходимости устанавливать Streamlit."""
from io import BytesIO
from pathlib import Path
import pandas as pd
import pytest

from core.importer import read_cases, empty_template_bytes
from core.models import load_profile
from core.rules import analyze_case
from core import storage

PROFILE = load_profile()


def valid_case(case_id='ПБ-ТЕСТ-001'):
    return {
        'case_id': case_id,
        'case_type': 'standard',
        'application_date': '2026-11-16',
        'issued_date': '2026-11-19',
        'predecision_date': '2026-11-18',
        'documents': 'Заявление|Акт обследования',
        'documents_complete': 'yes',
        'accrued_amount': '1500',
        'paid_amount': '1500',
        'exemption_code': '',
        'exemption_proof': 'unknown',
        'comment': '',
    }


def test_xlsx_template_has_all_columns_and_no_data():
    frame = pd.read_excel(BytesIO(empty_template_bytes()))
    assert len(frame) == 0
    assert 'case_id' in frame.columns
    assert 'exemption_proof' in frame.columns


def test_xlsx_import_and_analyze(tmp_path):
    path = tmp_path / 'cases.xlsx'
    pd.DataFrame([valid_case()]).to_excel(path, index=False)
    cases = read_cases(path, path.name, PROFILE)
    assert len(cases) == 1
    assert [r['status'] for r in analyze_case(cases[0], PROFILE)] == ['PASS','PASS','PASS']


def test_csv_import_and_flag(tmp_path):
    raw = valid_case()
    raw['predecision_date'] = '2026-11-20'
    path = tmp_path / 'cases.csv'
    pd.DataFrame([raw]).to_csv(path, index=False, sep=';', encoding='utf-8-sig')
    cases = read_cases(path, path.name, PROFILE)
    assert [r['status'] for r in analyze_case(cases[0], PROFILE)] == ['PASS','PASS','FLAG']


def test_duplicate_id_rejected(tmp_path):
    path = tmp_path / 'dup.xlsx'
    pd.DataFrame([valid_case(), valid_case()]).to_excel(path, index=False)
    with pytest.raises(ValueError, match='Повторный номер дела'):
        read_cases(path, path.name, PROFILE)


def test_storage_manual_review_and_updated_case(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'checks.sqlite3')
    from core.models import normalize_case
    storage.initialize()
    case = normalize_case(valid_case(), PROFILE)
    storage.save_case(case, 'Ручной ввод')
    assert len(storage.all_cases()) == 1
    storage.save_review(case['case_id'], 'Запросить документы', 'Требуется подтверждение')
    assert storage.get_review(case['case_id'])['decision'] == 'Запросить документы'
    changed = {**case, 'predecision_date': '2026-11-20'}
    storage.save_case(changed, 'Ручной ввод')
    assert storage.get_review(case['case_id'])['decision'] == 'Не проверено'
    assert analyze_case(storage.all_cases()[0], PROFILE)[2]['status'] == 'FLAG'


def test_missing_information_not_treated_as_violation(tmp_path):
    raw = valid_case()
    raw['documents_complete'] = 'unknown'
    raw['paid_amount'] = ''
    raw['predecision_date'] = ''
    path = tmp_path / 'missing.xlsx'
    pd.DataFrame([raw]).to_excel(path, index=False)
    case = read_cases(path, path.name, PROFILE)[0]
    assert [r['status'] for r in analyze_case(case, PROFILE)] == ['NEED_DATA'] * 3
