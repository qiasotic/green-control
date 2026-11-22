from pathlib import Path

from streamlit.testing.v1 import AppTest

from core import storage
from core.models import load_profile


ROOT = Path(__file__).resolve().parents[1]


def test_file_preview_and_revision_comparison(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "preview.sqlite3")
    profile = load_profile()
    app = AppTest.from_file(ROOT / "app.py", default_timeout=15).run()
    app.sidebar.radio[0].set_value("Импорт из ИС").run()

    source = ROOT / "examples" / "TECHNICAL_SMOKE_3_CASES.csv"
    data = source.read_bytes()
    duplicate = data.rstrip(b"\r\n") + b"\n" + data.splitlines()[1] + b"\n"
    app.file_uploader[0].set_value(("duplicate.csv", duplicate, "text/csv")).run()
    assert not app.exception
    assert app.button(key="file_import_commit").disabled
    assert any("Повторный номер дела" in str(table.value) for table in app.dataframe)
    assert not storage.all_cases(profile)

    app.file_uploader[0].set_value((source.name, data, "text/csv")).run()
    assert not app.exception and not app.button(key="file_import_commit").disabled
    assert not storage.all_cases(profile)
    app.button(key="file_import_commit").click().run()
    case = next(case for case in storage.all_cases(profile) if case["case_id"] == "DEMO-FLAG-002")
    storage.save_case({**case, "predecision_date": "2026-11-17"}, "Ручной ввод",
                      profile=profile, mode="edit", expected_version=case["_version"])

    app.sidebar.radio[0].set_value("Анализ и проверка").run()
    app.selectbox(key="case_picker").set_value(case["case_id"]).run()
    assert not app.exception
    assert any("20.11.2026" in str(table.value) and "17.11.2026" in str(table.value) for table in app.dataframe)
    results = next(table.value for table in app.dataframe if "Проверка" in table.value.columns)
    assert results["Статус"].tolist() == ["Совпадает", "Совпадает", "Изменился"]
