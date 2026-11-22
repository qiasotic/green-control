"""Регрессии удаления дел и наборов из рабочей базы."""
from pathlib import Path
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

from core import storage
from core.models import load_profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile()
MANUAL = "Ручной ввод"
FILE = "Импорт: cases.csv"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "deletion.sqlite3"
    monkeypatch.setattr(storage, "DB_PATH", path)
    storage.initialize(PROFILE)
    return path


def record(case_id):
    return {"case_id": case_id, "case_type": "standard", "application_date": "2026-11-16"}


def snapshot(source):
    return tuple((case["case_id"], case["_version"], case["_updated_at"])
                 for case in storage.all_cases(PROFILE) if case["_source"] == source)


def test_delete_case_removes_dependents_and_rejects_stale_confirmation(db):
    storage.save_cases([record("A"), record("B")], MANUAL, profile=PROFILE)
    stale = storage.all_cases(PROFILE)[0]
    storage.record_rule_action("A", "R1", storage.ACTIONS[0], "Нужны документы", profile=PROFILE,
                               expected_version=1, expected_state="new")
    storage.change_workflow("A", "closed", "Сведения уточнены", decision=storage.DECISIONS[1])
    storage.recheck_analysis("A", PROFILE, 1)
    with pytest.raises(ValueError, match="изменилось"):
        storage.delete_case("A", expected_version=1, expected_updated_at=stale["_updated_at"])
    assert storage.history("A")["current_review"]["note"] == "Сведения уточнены"

    storage.save_case({**record("A"), "comment": "Следующая редакция"}, MANUAL, profile=PROFILE,
                      mode="edit", expected_version=1)
    current = storage.all_cases(PROFILE)[0]
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO reviews VALUES ('A','Перенос','Запись v1.4','2026-11-18')")

    storage.delete_case("A", expected_version=current["_version"], expected_updated_at=current["_updated_at"])
    assert [case["case_id"] for case in storage.all_cases(PROFILE)] == ["B"]
    assert {item["case_id"] for item in storage.recent_activity()} == {"B"}
    with sqlite3.connect(db) as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        for table in ("case_versions", "case_workflow_events", "analysis_runs", "analysis_rechecks",
                      "rule_actions", "reviews", "review_history"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table} WHERE case_id='A'").fetchone()[0] == 0


def test_delete_current_dataset_preserves_other_sources_and_can_be_reimported(db):
    storage.save_cases([record("A"), record("B")], FILE, profile=PROFILE)
    storage.save_case(record("C"), MANUAL, profile=PROFILE)
    old_members = snapshot(FILE)
    storage.save_case({**record("B"), "comment": "Новая выгрузка"}, "Импорт: other.csv", profile=PROFILE)
    with pytest.raises(ValueError, match="Состав набора изменился"):
        storage.delete_dataset(FILE, old_members)
    with pytest.raises(ValueError, match="источник импорта"):
        storage.delete_dataset(MANUAL, snapshot(MANUAL))
    assert storage.delete_dataset(FILE, snapshot(FILE)) == ("A",)
    assert [case["case_id"] for case in storage.all_cases(PROFILE)] == ["B", "C"]
    assert len(storage.history("B")["versions"]) == 2
    assert storage.save_cases([record("A")], FILE, profile=PROFILE) == 1


def test_delete_github_dataset_allows_same_snapshot_to_be_reimported(db):
    def import_file(path, case_id):
        records = [record(case_id)]
        source = {"repository": "https://github.com/example/project", "path": path, "ref": "main",
                  "received_at": "2026-11-20", "blob_sha": "a" * 40, "digest": "b" * 64}
        plan = storage.preview_github_import(records, PROFILE)
        return storage.save_github_import(records, source, {}, plan, PROFILE)["new"]

    assert import_file("cases.csv", "A") == import_file("other.csv", "B") == 1
    source = "GitHub · https://github.com/example/project · cases.csv"
    assert storage.delete_dataset(source, snapshot(source)) == ("A",)
    assert [entry["path"] for entry in storage.github_import_history()] == ["other.csv"]
    assert import_file("cases.csv", "A") == 1


def test_delete_actions_in_streamlit_require_confirmation(db):
    storage.save_cases([record("A")], MANUAL, profile=PROFILE)
    storage.save_cases([record("B"), record("C")], FILE, profile=PROFILE)
    storage.save_cases([record("D")], "Импорт: other.csv", profile=PROFILE)
    app = AppTest.from_file(ROOT / "app.py").run()
    app.sidebar.radio[0].set_value("Анализ и проверка").run()
    assert app.selectbox(key="case_picker").value == "A"
    assert next(button for button in app.button if button.label == "Удалить дело").disabled
    next(box for box in app.checkbox if box.label == "Подтверждаю удаление дела").check().run()
    next(button for button in app.button if button.label == "Удалить дело").click().run()
    assert not app.exception and app.title[0].value == "Реестр дел"
    assert all("A" != case["case_id"] for case in storage.all_cases(PROFILE))

    app.sidebar.radio[0].set_value("Импорт из ИС").run()
    app.selectbox(key="delete_dataset_source").set_value(FILE).run()
    assert next(button for button in app.button if button.label == "Удалить набор").disabled
    next(box for box in app.checkbox if box.label == "Подтверждаю удаление набора").check().run()
    next(button for button in app.button if button.label == "Удалить набор").click().run()
    assert not app.exception and not app.error
    assert [case["case_id"] for case in storage.all_cases(PROFILE)] == ["D"]
    assert any("Удалено дел — 2" in item.value for item in app.success)
