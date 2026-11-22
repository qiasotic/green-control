from io import BytesIO
from pathlib import Path
import sqlite3

import pandas as pd
import pytest
from openpyxl import load_workbook
from streamlit.testing.v1 import AppTest

from core import acts, payments, storage
from core.importer import read_cases
from core.models import load_profile
from core.municipal import document_check, stage, tasks
from core.reports import xlsx_bytes
from ui.municipal import report_sheets

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "examples" / "municipal"
PROFILE = load_profile()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "work.sqlite3")
    storage.initialize(PROFILE)
    p = FILES / "01_cases.csv"
    storage.save_cases(read_cases(p, p.name, PROFILE), p.name, profile=PROFILE)
    return storage.DB_PATH


def cases():
    return storage.all_cases(PROFILE)


def find(cid="МУН-001"):
    return next(c for c in cases() if c["case_id"] == cid)


def snapshot(name):
    p = FILES / name
    day, records = payments.parse(p, p.name)
    result = payments.reconcile(cases(), PROFILE, day, records)
    return payments.save(day, records, p.name, result, PROFILE), result


def incoming(rows):
    data = "as_of;case_id;charge_id;paid_total\n" + "\n".join("2026-11-21;" + row for row in rows)
    return payments.parse(BytesIO(data.encode()), "file.csv")


def test_cumulative_payments_and_history(db):
    initial, old = snapshot("03_payments.csv")
    batch, new = snapshot("04_payments.csv")
    assert snapshot("04_payments.csv")[0] == batch and len(payments.batches()) == 2
    prior, current = [{r["case_id"]: r for r in group} for group in (old, new)]
    assert prior["МУН-001"]["external"] == 1200 and current["МУН-001"]["external"] == 2500
    assert payments.change_label(current["МУН-001"], prior["МУН-001"]) == "Оплата подтвердилась"
    assert payments.change_label(current["МУН-002"], prior["МУН-002"]) == "Без изменений"
    assert current["МУН-003"]["external"] is None
    assert "МУН-004" not in current and current[None]["status"] == payments.CHECK
    with pytest.raises(ValueError, match="последний"):
        payments.apply(initial, "МУН-001", PROFILE)
    assert payments.apply(batch, "МУН-001", PROFILE)
    c = find()
    assert c["_version"] == 2 and c["paid_amount"] == 2500 and c["_source"] == "01_cases.csv"
    history = storage.history(c["case_id"])
    assert "04_payments.csv" in history["versions"][0]["event"]
    assert history["versions"][1]["payload"]["paid_amount"] is None
    assert [r["status"] for r in c["_analysis"]["outcomes"]] == ["PASS", "PASS", "NEED_DATA"]
    with pytest.raises(ValueError, match="изменилось"):
        payments.apply(batch, "МУН-001", PROFILE)
    storage.delete_case(c["case_id"], expected_version=c["_version"], expected_updated_at=c["_updated_at"])
    assert not any(r["case_id"] == c["case_id"] for r in payments.results(batch))
    with sqlite3.connect(db) as con:
        assert not con.execute("pragma foreign_key_check").fetchall()


@pytest.mark.parametrize("lines,affected", [
    (["МУН-002;0000901;2500"], ["МУН-001", "МУН-002"]),
    ([";0000901;1500", ";0000901;1000"], ["МУН-001"]),
    (["МУН-001;9999;2500"], ["МУН-001"]),
    (["МУН-001;;", ";;300"], ["МУН-001"]),
    (["МУН-004;;100"], ["МУН-004"]),
])
def test_unresolved_rows_never_transfer(db, lines, affected):
    day, records = incoming(lines)
    by_id = {r["case_id"]: r for r in payments.reconcile(cases(), PROFILE, day, records)}
    assert all(by_id[c]["status"] == payments.CHECK and not by_id[c]["transferable"] for c in affected)


def test_fallback_zero_overpayment_and_stale_preview(db):
    day, records = incoming(["МУН-001;;0", "МУН-002;;1100.01"])
    preview = payments.reconcile(cases(), PROFILE, day, records)
    by_id = {r["case_id"]: r for r in preview}
    assert by_id["МУН-001"]["external"] == 0 and by_id["МУН-001"]["status"] == payments.DIFF
    assert "Переплата" in by_id["МУН-002"]["reason"]
    storage.save_case({**find(), "comment": "Уточнение"}, "01_cases.csv", profile=PROFILE)
    with pytest.raises(ValueError, match="изменились"):
        payments.save(day, records, "p.csv", preview, PROFILE)
    assert not payments.batches()


def test_same_charge_in_two_cases_requires_manual_review(db):
    storage.save_case({**find("МУН-002"), "charge_id": "0000901"}, "01_cases.csv", profile=PROFILE)
    day, records = incoming([";0000901;2500"])
    rows = payments.reconcile(cases(), PROFILE, day, records)
    assert all(r["status"] == payments.CHECK for r in rows)
    assert not any(r.get("transferable") for r in rows)


def test_snapshot_date_order_and_mixed_dates(db):
    snapshot("04_payments.csv")
    with pytest.raises(ValueError, match="поздняя"):
        snapshot("03_payments.csv")
    with pytest.raises(ValueError, match="общую дату"):
        payments.parse(BytesIO(b"as_of;case_id;paid_total\n2026-11-21;a;1\n2026-11-22;b;2"), "p.csv")
    assert len(payments.batches()) == 1


def test_payment_before_and_after_issue_generate_different_work(db):
    _, rows = snapshot("03_payments.csv")
    matches = {r["case_id"]: r for r in rows}
    before, after = [tasks(find(cid), PROFILE, matches[cid]) for cid in ("МУН-001", "МУН-002")]
    assert any(t["Действие"] == "Уточнить поступление до выдачи" for t in before)
    assert any(t["Действие"] == "Проверить расчёты по выданному билету" for t in after)
    assert any(t["Действие"] == "Сверить хронологию выдачи" for t in after)
    assert not any(t["Действие"] == "Сверить хронологию выдачи" for t in before)


def test_xlsx_keeps_identifiers_and_rejects_numeric_keys(db):
    for key, valid in [("0000901", True), (901, False)]:
        buffer = BytesIO()
        pd.DataFrame([{"as_of": "2026-11-21", "charge_id": key, "paid_total": 12.34}]).to_excel(buffer, index=False)
        if valid:
            _, rows = payments.parse(buffer, "p.xlsx")
            assert rows[0]["charge_id"] == key and rows[0]["cents"] == 1234
        else:
            with pytest.raises(ValueError, match="текст"):
                payments.parse(buffer, "p.xlsx")
    with pytest.raises(ValueError, match="итог"):
        incoming(["МУН-001;;12.345"])


def test_acts_requests_and_individual_report(db):
    p = FILES / "02_acts.csv"
    plan = acts.preview(p, p.name, cases())
    assert acts.save(plan, p.name, PROFILE) == 2
    assert acts.save(acts.preview(p, p.name, cases()), p.name, PROFILE) == 0
    c = find()
    assert [r["Результат"] for r in document_check(c)] == ["Совпадает", "Совпадает", "Есть расхождение"]
    actions = tasks(c, PROFILE)
    assert any(r["Кому"] == "Благоустройство" and "8" in r["Текст запроса"] and "12" in r["Текст запроса"] for r in actions)
    assert not any("дату выдачи" in r["Текст запроса"] for r in actions)
    assert stage(find("МУН-003")) == "unknown"
    snapshot("03_payments.csv")
    report = load_workbook(BytesIO(xlsx_bytes(report_sheets(c, PROFILE))), read_only=True)
    assert {"Акт и билет", "Рабочие задачи", "Сверка поступлений"} <= set(report.sheetnames)
    report.close()
    assert "02_acts.csv" in storage.history("МУН-001")["versions"][0]["event"]


def test_atomic_act_import_and_legacy_update(db):
    p = FILES / "02_acts.csv"
    plan = acts.preview(p, p.name, cases())
    legacy = {k: v for k, v in find("МУН-002").items() if k in ("case_id", "case_type", "application_date")}
    storage.save_cases([{**legacy, "comment": "Изменено"}], "old.csv", update_existing=True)
    assert find("МУН-002")["charge_id"] == "0000902"
    with pytest.raises(ValueError, match="изменилось"):
        acts.save(plan, p.name, PROFILE)
    assert "act_number" not in find()


def test_ui_flow_upload_compare_transfer_and_report(db):
    app = AppTest.from_file(ROOT / "app.py").run()
    app.sidebar.radio[0].set_value("Импорт из ИС").run()
    def upload(key, name):
        app.file_uploader(key=key).set_value((name, (FILES / name).read_bytes(), "text/csv")).run()
        assert not app.exception and not app.error
    upload("act_upload", "02_acts.csv")
    next(b for b in app.button if b.label == "Применить сведения из актов").click().run()
    for name in ("03_payments.csv", "04_payments.csv"):
        upload("payment_upload", name)
        app.button(key="pay_save").click().run()
        assert not app.exception and not app.error
    next(b for b in app.button if b.label == "К карточке из сверки").click().run()
    assert app.selectbox(key="case_picker").value == "МУН-001"
    app.button(key="apply_payment").click().run()
    assert find()["paid_amount"] == 2500
    app.sidebar.radio[0].set_value("Отчёты").run()
    assert any(w.proto.label == "Скачать заключение XLSX" for w in app.get("download_button"))
    assert not app.error and not app.exception

