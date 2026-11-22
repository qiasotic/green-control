"""Приёмочные проверки рабочего места и миграции с v1.4."""
from copy import deepcopy
from datetime import date
from io import BytesIO
import json
from pathlib import Path
import sqlite3

from openpyxl import load_workbook
import pytest
from streamlit.testing.v1 import AppTest

from core import importer, storage
from core.models import load_profile, normalize_case
from core.reports import csv_bytes, xlsx_bytes
from core.rules import analyze_case

ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "cases.sqlite3"
    monkeypatch.setattr(storage, "DB_PATH", path)
    return path


def sample(case_id="ПБ-001", **changes):
    return normalize_case({
        "case_id": case_id, "case_type": "standard", "application_date": "2026-11-16",
        "issued_date": "2026-11-19", "predecision_date": "2026-11-18",
        "documents": ["Заявление", "Акт обследования"], "documents_complete": "yes",
        "accrued_amount": 1500, "paid_amount": 1500, **changes,
    }, PROFILE)


def test_v14_schema_migrates_without_rewriting_original_payload_or_losing_review(isolated_db):
    case = sample(comment="Карточка v1.4")
    original = json.dumps(case, ensure_ascii=False)
    with sqlite3.connect(isolated_db) as conn:
        conn.execute("CREATE TABLE cases(case_id TEXT PRIMARY KEY,payload TEXT,source TEXT,saved_at TEXT)")
        conn.execute("CREATE TABLE reviews(case_id TEXT PRIMARY KEY,decision TEXT,note TEXT,reviewed_at TEXT)")
        conn.execute("INSERT INTO cases VALUES (?,?,?,?)",
                     (case["case_id"], original, "Старый импорт", "2026-11-19T00:00:00+00:00"))
        conn.execute("INSERT INTO reviews VALUES (?,?,?,?)",
                     (case["case_id"], "Сигнал снят", "Сверено", "2026-11-20T00:00:00+00:00"))
    storage.initialize()
    storage.initialize()
    active = storage.all_cases()[0]
    history = storage.history(case["case_id"])
    assert (active["_version"], active["_state"], active["_source"]) == (1, "closed", "Старый импорт")
    assert history["versions"][0]["payload"] == case
    assert history["current_review"]["note"] == "Сверено"
    assert len(history["workflow"]) == 2
    assert len(history["analyses"]) == len(history["reviews"]) == 1
    with sqlite3.connect(isolated_db) as conn:
        assert conn.execute("SELECT payload FROM cases").fetchone()[0] == original
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone() == (1,)
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,), (4,)]
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_edit_reopens_case_and_preserves_review_and_old_analysis():
    storage.initialize()
    original = sample()
    case_id = original["case_id"]
    storage.save_case(original, "Ручной ввод", mode="create")
    storage.record_rule_action(case_id, "R3", storage.ACTIONS[0], "Уточнить журнал",
                               profile=PROFILE, expected_version=1, expected_state="new")
    assert storage.all_cases()[0]["_state"] == "awaiting_info"
    with pytest.raises(ValueError, match="основание"):
        storage.change_workflow(case_id, "closed", "", decision=storage.DECISIONS[0])
    storage.change_workflow(case_id, "closed", "Сверено", decision=storage.DECISIONS[0])
    storage.save_case({**original, "predecision_date": "2026-11-20"}, "Уточнение",
                      mode="edit", expected_version=1)
    active = storage.all_cases()[0]
    assert (active["_version"], active["_state"], active["_source"]) == (2, "in_review", "Ручной ввод")
    assert storage.get_review(case_id) == storage.EMPTY_REVIEW
    history = storage.history(case_id)
    assert [entry["outcomes"][2]["status"] for entry in history["analyses"]] == ["FLAG", "PASS"]
    assert history["reviews"][0]["note"] == "Сверено"
    assert history["actions"][0]["rule"] == "R3"
    assert history["workflow"][0]["from_state"] == "closed"
    with pytest.raises(ValueError, match="Дело изменилось"):
        storage.save_case(original, "Уточнение", mode="edit", expected_version=1)


def test_reopening_keeps_conclusion_historical_when_more_information_is_requested():
    storage.initialize()
    case = sample()
    case_id = case["case_id"]
    storage.save_case(case, "Ручной ввод")
    storage.change_workflow(case_id, "closed", "Результат", decision=storage.DECISIONS[1])
    storage.change_workflow(case_id, "in_review", "Открыто повторно")
    storage.change_workflow(case_id, "awaiting_info", "Нужны сведения")
    assert storage.get_review(case_id) == storage.EMPTY_REVIEW
    assert storage.history(case_id)["reviews"][0]["note"] == "Результат"


def test_import_conflict_rolls_back_and_accepted_replay_is_idempotent():
    storage.initialize()
    case = sample()
    storage.save_cases([case], "Импорт")
    changed = {**case, "paid_amount": 0}
    with pytest.raises(ValueError, match="уже есть"):
        storage.save_cases([sample("НОВОЕ"), changed], "Импорт")
    assert len(storage.all_cases()) == 1
    assert storage.save_cases([changed], "Импорт", update_existing=True) == 1
    assert storage.save_cases([changed], "Импорт") == 0
    assert len(storage.history(case["case_id"])["versions"]) == 2


def test_duplicate_id_and_incomplete_draft():
    storage.initialize()
    partial = sample(application_date="", issued_date="", predecision_date="", paid_amount=None,
                     documents_complete="unknown")
    assert storage.save_case(partial, "Ручной ввод", mode="create", draft=True)
    with pytest.raises(ValueError, match="уже есть"):
        storage.save_case(partial, "Ручной ввод", mode="create", draft=True)
    with pytest.raises(ValueError, match="дату заявления"):
        storage.save_case(partial, "Ручной ввод", mode="edit", expected_version=1)
    with pytest.raises(ValueError, match="черновика"):
        storage.change_workflow(partial["case_id"], "closed", "Проверено", decision=storage.DECISIONS[1])
    assert [r["status"] for r in storage.all_cases()[0]["_analysis"]["outcomes"]] == ["NEED_DATA"] * 3


def test_new_profile_keeps_previous_analysis():
    storage.initialize()
    case = sample()
    storage.save_case(case, "Ручной ввод")
    profile = deepcopy(PROFILE)
    profile["version"] = "2.0"
    profile["case_types"]["standard"]["requires_predecision"] = False
    storage.initialize(profile)
    assert storage.all_cases(profile)[0]["_analysis"]["outcomes"][2]["status"] == "NOT_APPLICABLE"
    assert storage.all_cases(PROFILE)[0]["_analysis"]["outcomes"][2]["status"] == "PASS"
    assert len(storage.history(case["case_id"])["analyses"]) == 2


def test_unknown_exemption_and_exported_cyrillic():
    partial = sample(finance_mode="exemption", exemption_code="", exemption_proof="no",
                     payment_note="Прежнее пояснение")
    assert partial["accrued_amount"] is None and partial["paid_amount"] is None
    assert "payment_note" not in partial
    assert analyze_case(partial, PROFILE)[0]["status"] == "NEED_DATA"
    rows = [{"Дело": "=ТЕСТ()", "Комментарий": "Проверено"}]
    columns = list(rows[0])
    assert csv_bytes(rows, columns).decode("utf-8-sig").splitlines() == [
        "Дело;Комментарий", "'=ТЕСТ();Проверено",
    ]
    sheet = load_workbook(BytesIO(xlsx_bytes([("Реестр", rows, columns)]))).active
    assert sheet["A2"].data_type == "s" and sheet["B2"].value == "Проверено"


def widget(elements, label):
    return next(item for item in elements if item.label == label)


def navigate(app, page):
    app.sidebar.radio[0].set_value(page).run()
    assert not app.exception and app.title[0].value == page


def step(app, label):
    widget(app.radio, "Этап заполнения").set_value(label).run()
    assert not app.exception


def test_folder_sync_is_explicit(tmp_path, monkeypatch):
    inbox = tmp_path / "incoming"
    inbox.mkdir()
    file = inbox / "exchange.csv"
    file.write_bytes((ROOT / "examples" / "TECHNICAL_SMOKE_3_CASES.csv").read_bytes())
    monkeypatch.setattr(importer, "INBOX", inbox)
    monkeypatch.setattr(importer, "PROCESSED", inbox / "processed")
    app = AppTest.from_file(ROOT / "app.py").run()
    navigate(app, "Импорт из ИС")
    app.run()
    assert file.is_file() and not storage.all_cases()
    widget(app.button, "Импортировать из папки обмена").click().run()
    assert not file.exists() and len(storage.all_cases()) == 3
    app.run()
    assert not app.exception and len(storage.all_cases()) == 3


def test_streamlit_wizard_registry_review_and_reports():
    app = AppTest.from_file(ROOT / "app.py").run()
    navigate(app, "Новое дело")
    widget(app.text_input, "Номер дела").set_value("ПБ-ИНТЕРФЕЙС")
    widget(app.selectbox, "Категория дела").set_value("standard")
    widget(app.text_area, "Комментарий к делу").set_value("Сценарий ручного ввода")
    app.run()
    widget(app.button, "Далее").click().run()
    widget(app.checkbox, "Заявление").check().run()
    step(app, "3. Финансы")
    finance = "Финансовый сценарий"
    widget(app.radio, finance).set_value("payment").run()
    widget(app.text_input, "Начислено, ₽").set_value("999").run()
    widget(app.radio, finance).set_value("exemption").run()
    assert not any(item.label == "Начислено, ₽" for item in app.text_input)
    widget(app.selectbox, "Основание освобождения").set_value("DEMO_PUBLIC_USE").run()
    widget(app.radio, finance).set_value("payment").run()
    assert widget(app.text_input, "Начислено, ₽").value == ""
    widget(app.text_input, "Начислено, ₽").set_value("1500").run()
    step(app, "4. Проверка и сохранение")
    widget(app.button, "Сохранить черновик").click().run()
    assert not app.exception and app.title[0].value == "Анализ и проверка"
    draft = storage.all_cases()[0]
    assert draft["comment"] == "Сценарий ручного ввода" and draft["documents"] == ["Заявление"]
    assert draft["_draft"] and draft["accrued_amount"] == 1500 and draft["paid_amount"] is None

    widget(app.button, "Продолжить заполнение").click().run()
    widget(app.date_input, "Дата заявления").set_value(date(2026, 11, 16))
    widget(app.date_input, "Дата выдачи").set_value(date(2026, 11, 19))
    app.run()
    step(app, "2. Документы")
    assert widget(app.checkbox, "Заявление").value
    widget(app.checkbox, "Акт обследования").check()
    widget(app.radio, "Комплектность материалов").set_value("yes")
    widget(app.date_input, "Дата предварительного решения").set_value(date(2026, 11, 18))
    app.run()
    step(app, "3. Финансы")
    widget(app.text_input, "Оплачено, ₽").set_value("1500").run()
    step(app, "4. Проверка и сохранение")
    widget(app.button, "Сохранить и проверить").click().run()
    assert not app.exception
    case = storage.all_cases()[0]
    assert not case["_draft"]
    assert [row["status"] for row in case["_analysis"]["outcomes"]] == ["PASS"] * 3

    navigate(app, "Реестр дел")
    search = "Поиск по номеру или комментарию"
    widget(app.text_input, search).set_value("сценарий").run()
    widget(app.selectbox, "Дело на странице").set_value(case["case_id"]).run()
    widget(app.button, "Открыть дело").click().run()
    assert app.title[0].value == "Анализ и проверка"
    widget(app.selectbox, "Следующее состояние").set_value("closed").run()
    widget(app.selectbox, "Заключение специалиста").set_value(storage.DECISIONS[2])
    widget(app.text_area, "Основание или комментарий (обязательно)").set_value("Сверено с материалами")
    widget(app.button, "Зафиксировать заключение").click().run()
    assert not app.exception
    assert storage.history(case["case_id"])["reviews"][0]["note"] == "Сверено с материалами"
    navigate(app, "Реестр дел")
    assert widget(app.text_input, search).value == "сценарий"
    navigate(app, "Отчёты")
    assert {item.proto.label for item in app.get("download_button")} >= {
        "Скачать реестр CSV", "Скачать реестр XLSX", "Скачать заключение TXT", "Скачать заключение XLSX",
    }


def test_streamlit_overview_opens_flagged_case_and_registers_rule_action():
    storage.initialize()
    path = ROOT / "examples" / "TECHNICAL_SMOKE_3_CASES.csv"
    storage.save_cases(importer.read_cases(path, path.name, PROFILE), "Проверочная выгрузка")

    app = AppTest.from_file(ROOT / "app.py").run()
    assert [metric.value for metric in app.metric] == ["3", "1", "1", "0"]
    widget(app.button, "Контрольное дело № 002").click().run()
    assert not app.exception and app.title[0].value == "Анализ и проверка"
    assert app.selectbox(key="case_picker").value == "DEMO-FLAG-002"
    assert all(any(item.label.startswith(f"Проверка {number}.") and "Выявлено отклонение" in item.label
                   for item in app.expander) for number in range(1, 4))
    assert "Профиль проверки · версия 1.0" in " ".join(caption.value for caption in app.caption)
    note = next(item for item in app.text_area if item.key.endswith("_R3"))
    note.set_value("Сверить даты с журналом регистрации")
    widget(app.button, "Зафиксировать действие · проверка 3").click().run()
    assert not app.exception
    assert storage.history("DEMO-FLAG-002")["actions"][0]["note"] == "Сверить даты с журналом регистрации"
    assert next(case for case in storage.all_cases() if case["case_id"] == "DEMO-FLAG-002")["_state"] == "awaiting_info"
