"""Tests for the install-ready handoff and transactional import."""
from pathlib import Path
import shutil

import pytest
import pandas as pd

from core import importer, storage
from core.models import load_profile, normalize_case
from core.rules import analyze_case

ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile()


def test_smoke_file_has_expected_results():
    path = ROOT / 'examples' / 'TECHNICAL_SMOKE_3_CASES.csv'
    cases = importer.read_cases(path, path.name, PROFILE)
    outcomes = {c['case_id']: [r['status'] for r in analyze_case(c, PROFILE)] for c in cases}
    assert outcomes == {
        'DEMO-OK-001': ['PASS', 'PASS', 'PASS'],
        'DEMO-FLAG-002': ['FLAG', 'FLAG', 'FLAG'],
        'DEMO-NODATA-003': ['NEED_DATA', 'NEED_DATA', 'NEED_DATA'],
    }


def test_import_is_atomic_if_a_record_is_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'test.sqlite3')
    storage.initialize()
    good = normalize_case({
        'case_id': 'VALID-001', 'case_type': 'standard',
        'documents_complete':'unknown', 'exemption_proof':'unknown'
    }, PROFILE)
    with pytest.raises(KeyError):
        storage.save_cases([good, {'bad': 'without required ID'}], 'Test')
    assert storage.all_cases() == []


def test_folder_exchange_moves_successfully_imported_file(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'test.sqlite3')
    inbox=tmp_path/'incoming'
    out=inbox/'processed'
    inbox.mkdir();out.mkdir()
    monkeypatch.setattr(importer, 'INBOX', inbox)
    monkeypatch.setattr(importer, 'PROCESSED', out)
    storage.initialize()
    source = ROOT/'examples'/'TECHNICAL_SMOKE_3_CASES.csv'
    shutil.copy(source, inbox/'exchange.csv')
    msgs=importer.sync_folder(PROFILE)
    assert len(msgs)==1 and 'ГОТОВО' in msgs[0]
    assert len(storage.all_cases())==3
    assert not (inbox/'exchange.csv').exists()
    assert len(list(out.glob('exchange_*.csv')))==1
