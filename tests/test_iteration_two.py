"""Проверки синтетики, расчётов и отчётности второй итерации."""
from datetime import date
from io import BytesIO
import json
from pathlib import Path

from openpyxl import load_workbook
import pytest
from streamlit.delta_generator import DeltaGenerator
from streamlit.testing.v1 import AppTest

from core import storage
from core.analytics import report_sheets, summarize, summary_csv, summary_html
from core.importer import read_cases
from core.models import load_profile, normalize_case
from core.presentation import case_label
from core.reports import xlsx_bytes
from scripts.evaluate_synthetic import evaluate, read_truth, score
from scripts.generate_synthetic import DEFAULT_SEED, REFERENCE_DATE, generate, write_dataset

ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile()


def test_seed_and_authored_labels_without_rule_engine(monkeypatch):
    monkeypatch.setattr("core.rules.analyze_case", lambda *args: pytest.fail("Разметка зависит от движка"))
    data, labels = generate(DEFAULT_SEED, 200)
    assert generate(DEFAULT_SEED, 200) == (data, labels)
    assert generate(DEFAULT_SEED + 1, 200) != (data, labels)
    assert len({item["case_id"] for item in data}) == 200
    by_scenario = {item["scenario"]: (item["R1"], item["R2"], item["R3"]) for item in labels}
    assert by_scenario["r1_r2"] == ("FLAG", "FLAG", "PASS")
    assert by_scenario["emergency_partial"] == ("PASS", "NEED_DATA", "NOT_APPLICABLE")
    assert by_scenario["all_unknown"] == ("NEED_DATA",) * 3


def test_reproducible_outputs_and_roundtrip(tmp_path):
    write_dataset(tmp_path, DEFAULT_SEED, 200, REFERENCE_DATE)
    csv_path = tmp_path / "dataset.csv"
    xlsx_path = tmp_path / "dataset.xlsx"
    labels = read_truth(tmp_path / "ground_truth.csv")
    records = read_cases(csv_path, csv_path.name, PROFILE)
    assert records == read_cases(xlsx_path, xlsx_path.name, PROFILE)
    duplicate_path = tmp_path / "invalid_duplicate_id.csv"
    with pytest.raises(ValueError, match="Повторный"):
        read_cases(duplicate_path, duplicate_path.name, PROFILE)
    actual = evaluate(records, labels, PROFILE)
    assert actual["deterministic"] and actual["mismatches"] == []
    assert [(rule["covered"], rule["applicable"]) for rule in actual["rules"].values()] == [
        (160, 200), (170, 200), (160, 180),
    ]
    broken = dict(labels)
    broken.pop(records[0]["case_id"])
    with pytest.raises(ValueError, match="должны совпадать"):
        evaluate(records, broken, PROFILE)
    manifest = json.loads((tmp_path / "generation_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["scenarios"]) == 20 and sum(manifest["scenarios"].values()) == len(records)
    snapshot = csv_path.read_bytes(), (tmp_path / "ground_truth.csv").read_bytes()
    write_dataset(tmp_path, DEFAULT_SEED, 200, REFERENCE_DATE)
    assert snapshot == (csv_path.read_bytes(), (tmp_path / "ground_truth.csv").read_bytes())


def test_scoring_counts_abstentions_and_undefined_precision():
    metrics = score(["FLAG", "PASS", "NEED_DATA", "NOT_APPLICABLE", "FLAG"],
                    ["PASS", "FLAG", "NEED_DATA", "FLAG", "NEED_DATA"])
    assert [metrics[key] for key in ("TP", "FP", "FN", "TN")] == [0, 1, 1, 0]
    assert metrics["nonbinary_pairs"] == 3 and metrics["coverage"] == .5
    empty = score(["NOT_APPLICABLE"], ["NOT_APPLICABLE"])
    assert empty["precision"] is None and empty["recall"] is None and empty["coverage"] is None


def test_summary_counts_case_once_and_renders_all_export_formats_safely():
    valid = ({"case_id": "<script>тест</script>", "application_date": "2026-11-20",
              "_state": "new", "_draft": False},
             {"Сигналы": 2, "Нехватка сведений": 1, "R1": "FLAG", "R2": "FLAG", "R3": "NEED_DATA",
              "Категория": "Общая процедура", "Состояние дела": "Новое",
              "Дата заявления": "2026-11-20", "Источник": "Ручной ввод"})
    undated = ({"case_id": "ВТОРОЕ", "application_date": "неизвестно", "_state": "closed", "_draft": False},
               {"Сигналы": 0, "Нехватка сведений": 0, "R1": "PASS", "R2": "PASS", "R3": "NOT_APPLICABLE"})
    result = summarize([valid, undated])
    assert list(result["metrics"].values()) == [2, 1, 1, 1]
    assert result["undated"] == 1 and result["months"] == [{"Месяц заявления": "2026-11", "Дел": 1}]
    sheets = report_sheets(result, {"Дата заявления с": "2026-11-01"}, PROFILE, "20.11.2026")
    assert "Приоритетная очередь" in summary_csv(sheets).decode("utf-8-sig")
    workbook = load_workbook(BytesIO(xlsx_bytes(sheets)), read_only=True)
    assert workbook["Показатели"]["B2"].value == 2
    assert workbook["Распределение проверок"]["A2"].value == "Проверка 1. Финансовые условия"
    html = summary_html(sheets).decode("utf-8")
    assert "&lt;script&gt;тест&lt;/script&gt;" in html and "<script>" not in html


def item(widgets, label):
    return next(widget for widget in widgets if widget.label == label)


def navigate(app, page):
    app.sidebar.radio[0].set_value(page).run()
    assert app.title[0].value == page and not app.exception


def test_load_filter_recheck_close_and_export_from_streamlit(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "v2.sqlite3")
    downloads = {}
    download_button = DeltaGenerator.download_button

    def capture_download(self, label, data, *args, **kwargs):
        downloads[label] = data
        return download_button(self, label, data, *args, **kwargs)

    monkeypatch.setattr(DeltaGenerator, "download_button", capture_download)
    app = AppTest.from_file(ROOT / "app.py", default_timeout=12).run()
    navigate(app, "Импорт из ИС")
    choice = item(app.selectbox, "Контрольный сценарий")
    choice.set_value("all").run()
    assert item(app.button, "Загрузить контрольные дела").disabled
    item(app.checkbox, "Подтверждаю загрузку синтетических дел в открытую базу").check().run()
    item(app.button, "Загрузить контрольные дела").click().run()
    assert len(storage.all_cases()) == 200 and not app.exception
    item(app.button, "Загрузить контрольные дела").click().run()
    assert "Добавлено: 0" in app.success[0].value

    navigate(app, "Аналитика")
    assert [metric.value for metric in app.metric] == ["200", "80", "70", "0"]
    app.selectbox(key="analytics_rule").set_value("R3")
    app.selectbox(key="analytics_result").set_value("FLAG")
    app.multiselect(key="analytics_sources").set_value(["Синтетика · набор 200"]).run()
    assert [metric.value for metric in app.metric] == ["30", "30", "0", "0"]
    case_id = item(app.selectbox, "Дело с формальным отклонением").value
    item(app.button, "Открыть карточку дела").click().run()
    assert app.selectbox(key="case_picker").value == case_id and not app.exception
    item(app.button, "Перезапустить анализ").click().run()
    history = storage.history(case_id)
    assert len(history["rechecks"]) == len(history["analyses"]) == 1
    assert history["analyses"][0]["outcomes"][2]["status"] == "FLAG"

    app.selectbox(key=next(widget.key for widget in app.selectbox if widget.label == "Следующее состояние")).set_value("closed").run()
    item(app.selectbox, "Заключение специалиста").set_value(storage.DECISIONS[0])
    item(app.text_area, "Основание или комментарий (обязательно)").set_value("Сверено с модельными материалами")
    item(app.button, "Зафиксировать заключение").click().run()
    assert storage.history(case_id)["current_review"]["note"] == "Сверено с модельными материалами"

    navigate(app, "Аналитика")
    assert [metric.value for metric in app.metric] == ["30", "30", "0", "1"]
    item(app.button, "Сформировать сводный отчёт").click().run()
    assert app.title[0].value == "Отчёты" and not app.exception
    assert app.selectbox(key="report_scope").value == "Фильтры аналитики"
    exports = {element.proto.label for element in app.get("download_button")}
    assert exports >= {"Сводный отчёт CSV", "Сводный отчёт XLSX", "Сводный отчёт HTML",
                       "Скачать заключение XLSX", "Метрики испытаний (JSON)"}
    with BytesIO(downloads["Скачать заключение XLSX"]) as content:
        workbook = load_workbook(content, read_only=True)
        fields = dict(workbook["Карточка и заключение"].values)
        assert fields["Номер дела"] == case_label(case_id)
        assert fields["Обоснование"] == "Сверено с модельными материалами"
        workbook.close()
    with BytesIO(downloads["Сводный отчёт XLSX"]) as content:
        workbook = load_workbook(content, read_only=True)
        assert workbook["Показатели"]["B2"].value == 30
        workbook.close()
    item(app.date_input, "Период отчёта с").set_value(date(2027, 1, 1)).run()
    assert any("В отчёте 0 из 200 дел" in caption.value for caption in app.caption)

    current = next(case for case in storage.all_cases() if case["case_id"] == case_id)
    candidate = normalize_case({**current, "predecision_date": current["issued_date"]}, PROFILE)
    storage.save_case(candidate, "Тест", mode="edit", expected_version=current["_version"])
    history = storage.history(case_id)
    assert [run["outcomes"][2]["status"] for run in history["analyses"]] == ["PASS", "FLAG"]
    assert len(history["rechecks"]) == 1 and len(history["reviews"]) == 1
    navigate(app, "Аналитика")
    assert [metric.value for metric in app.metric] == ["29", "29", "0", "0"]
    item(app.button, "Перезапустить анализ выборки").click().run()
    assert any("Повторно рассчитано дел: 29" in message.value for message in app.success)


def test_batch_rechecks_rollback_when_any_revision_has_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "rechecks.sqlite3")
    storage.initialize()
    rows, _ = generate(DEFAULT_SEED, 2)
    storage.save_cases(rows, "Синтетика")
    first, second = [row["case_id"] for row in rows]
    with pytest.raises(ValueError, match="Дело изменилось"):
        storage.recheck_analyses([(first, 1), (second, 2)], PROFILE)
    assert storage.history(first)["rechecks"] == []
    storage.recheck_analyses([(first, 1), (second, 1)], PROFILE)
    assert len(storage.history(first)["rechecks"]) == len(storage.history(second)["rechecks"]) == 1
