"""Проверка локального сохранения и сброса ручной оценки при изменении дела."""
from core import storage
from core.models import load_profile, normalize_case


def test_storage_and_revision_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.sqlite3")
    storage.initialize()
    profile = load_profile()
    raw = {"case_id": "A-1", "case_type": "standard",
           "documents_complete": "unknown", "exemption_proof": "unknown"}
    old_case = normalize_case(raw, profile)
    storage.save_case(old_case, "Тест")
    assert len(storage.all_cases()) == 1
    storage.save_review("A-1", "Сигнал снят", "Проверено")
    assert storage.get_review("A-1")["decision"] == "Сигнал снят"
    storage.save_case(old_case, "Тест")
    assert storage.get_review("A-1")["decision"] == "Сигнал снят"
    new_case = normalize_case({**raw, "issued_date": "2026-11-19"}, profile)
    storage.save_case(new_case, "Тест")
    assert storage.get_review("A-1")["decision"] == "Не проверено"
