"""Сценарий публичного импорта, локализация и обучение."""
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from streamlit.testing.v1 import AppTest

from core import github_connector as github, storage
from core.importer import mapped_rows, read_frame
from core.models import load_profile
from core.presentation import RULE_LABELS, STATUS_LABELS, case_label

ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile()
CASE = "ПБ-ИСТОЧНИК"
PATH = "/repos/sample/data"
MAPPING = {
    "case_id": "Номер", "case_type": "Вид", "issued_date": "Дата выдачи разрешения",
    "predecision_date": "Дата предварительного решения", "application_date": "Дата заявления",
    "accrued_amount": "Начислено, ₽", "paid_amount": "Оплачено, ₽",
}


def fixture(day):
    return ("Номер;Вид;Дата выдачи разрешения;Дата предварительного решения;Дата заявления;Начислено, ₽;Оплачено, ₽\n"
            f"{CASE};Общая процедура;18.11.2026;{day};10.11.2026;2000;1200\n").encode("utf-8-sig")


@pytest.fixture
def remote(monkeypatch):
    current = {"sha": "a" * 40, "content": fixture("20.11.2026")}
    calls = []

    def get(endpoint, *, raw=False):
        calls.append(endpoint)
        if raw:
            assert endpoint == f"{PATH}/git/blobs/{current['sha']}"
            return current["content"]
        if endpoint == PATH:
            return b'{"private": false, "default_branch": "main"}'
        assert endpoint in (PATH + "/git/trees/main?recursive=1", PATH + "/git/trees/main")
        import json
        return json.dumps({"truncated": False, "tree": [
            {"path": "cases.csv", "type": "blob", "mode": "100644", "sha": current["sha"],
             "size": len(current["content"])},
            {"path": "shortcut.csv", "type": "blob", "mode": "120000", "sha": "b" * 40, "size": 30},
        ]}).encode("utf-8")

    monkeypatch.setattr(github, "_get", get)
    return current, calls


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "local.sqlite3")
    storage.initialize()


@pytest.mark.parametrize("address", [
    "http://github.com/sample/data", "https://github.com.evil.test/sample/data",
    "https://github.com@127.0.0.1/test/data", "https://github.com:443/sample/data",
    "https://github.com/sample/data/blob/main/%2e%2e/test.csv",
    "https://github.com/sample/data/blob/main/main.py",
])
def test_rejects_unapproved_addresses(address):
    with pytest.raises(github.GitHubError):
        github.parse_address(address)


def test_lists_regular_files_and_supports_direct_links(remote):
    root = github.parse_address("https://github.com/sample/data")
    files, incomplete = github.list_files(root)
    assert not incomplete and [file.source.path for file in files] == ["cases.csv"]
    for url in ("https://github.com/sample/data/blob/main/cases.csv",
                "https://raw.githubusercontent.com/sample/data/main/cases.csv",
                "https://raw.githubusercontent.com/sample/data/refs/heads/main/cases.csv"):
        assert github.file_info(github.parse_address(url)) == files[0]
    snapshot = github.download(files[0])
    assert snapshot.content == fixture("20.11.2026")
    assert len(snapshot.metadata()["digest"]) == 64
    with pytest.raises(github.GitHubError, match="обычного файла"):
        github.file_info(github.parse_address("https://github.com/sample/data/blob/main/shortcut.csv"))
    with pytest.raises(github.GitHubError, match="перенаправление"):
        github._NoRedirect().redirect_request(Request(github.API), None, 302, "", {}, "http://127.0.0.1/")


@pytest.mark.parametrize(("status", "phrase"), [(403, "отказал"), (404, "не найден"), (429, "ограничил")])
def test_http_failures_are_user_facing(monkeypatch, status, phrase):
    class Client:
        def open(self, request, timeout):
            assert request.full_url == "https://api.github.com/repos/sample/data" and timeout == github.TIMEOUT
            raise HTTPError(request.full_url, status, "", {}, None)
    monkeypatch.setattr(github, "build_opener", lambda *args: Client())
    with pytest.raises(github.GitHubError, match=phrase):
        github.list_files(github.parse_address("https://github.com/sample/data"))


def test_network_timeout_is_visible(monkeypatch):
    class Client:
        def open(self, request, timeout):
            raise URLError(TimeoutError())

    monkeypatch.setattr(github, "build_opener", lambda *args: Client())
    with pytest.raises(github.GitHubError, match="Не удалось соединиться"):
        github.list_files(github.parse_address("https://github.com/sample/data"))


def test_bad_file_and_duplicate_rows_are_reported():
    with pytest.raises(ValueError, match="прочитать"):
        read_frame(BytesIO(b"not an xlsx file"), "wrong.xlsx")
    data = fixture("20.11.2026").decode("utf-8-sig")
    frame = read_frame(BytesIO((data + data.splitlines()[1] + "\n").encode("utf-8")), "cases.csv")
    records, errors = mapped_rows(frame, MAPPING, PROFILE)
    assert len(records) == 1 and errors[0][0] == 3 and "Повторный" in errors[0][1]


def test_storage_atomic_refresh_and_unmapped_fields(remote, db):
    current, _ = remote
    source = github.parse_address("https://github.com/sample/data/blob/main/cases.csv")
    snap = github.download(github.file_info(source))
    records, errors = mapped_rows(read_frame(BytesIO(snap.content), "cases.csv"), MAPPING, PROFILE)
    assert not errors
    plan = storage.preview_github_import(records, PROFILE)
    assert storage.save_github_import(records, snap.metadata(), MAPPING, plan, PROFILE)["new"] == 1
    assert storage.save_github_import(records, snap.metadata(), MAPPING, plan, PROFILE)["seen"]
    original = storage.all_cases()[0]
    storage.save_case({**original, "comment": "Пометка специалиста"}, "Уточнение", mode="edit",
                      expected_version=original["_version"])
    storage.change_workflow(CASE, "closed", "Проверено", decision=storage.DECISIONS[0])
    changes = [{**records[0], "predecision_date": "17.11.2026"}]
    update = dict(snap.metadata(), digest="c" * 64, blob_sha="d" * 40)
    plan = storage.preview_github_import(changes, PROFILE)
    assert plan[0][2:] == ("update", ("predecision_date",))
    with pytest.raises(ValueError, match="подтверждение"):
        storage.save_github_import(changes, update, MAPPING, plan, PROFILE)
    assert len(storage.github_import_history()) == 1
    assert storage.save_github_import(changes, update, MAPPING, plan, PROFILE, update_existing=True)["update"] == 1
    case = storage.all_cases()[0]
    assert case["comment"] == "Пометка специалиста" and case["_state"] == "in_review"
    data = storage.history(CASE)
    assert data["reviews"][0]["note"] == "Проверено"
    assert [run["outcomes"][2]["status"] for run in data["analyses"]] == ["PASS", "FLAG", "FLAG"]
    latest = storage.github_import_history()[0]
    assert latest["repository"] == source.address and latest["blob_sha"] == "d" * 40


def widget(elements, label):
    return next(element for element in elements if element.label == label)


def test_streamlit_github_explanation_and_return_to_fields(remote, db):
    app = AppTest.from_file(ROOT / "app.py", default_timeout=15).run()
    app.sidebar.radio[0].set_value("Импорт из ИС").run()
    widget(app.text_input, "Адрес репозитория").set_value("https://github.com/sample/data").run()
    app.button(key="gh_connect").click().run()
    widget(app.button, "Предпросмотр выбранного файла").click().run()
    widget(app.selectbox, "Номер дела *").set_value("Номер")
    widget(app.selectbox, "Категория *").set_value("Вид")
    app.run()
    app.button(key="gh_check").click().run()
    assert not app.button(key="gh_save").disabled and not app.exception
    app.button(key="gh_save").click().run()
    widget(app.button, "Проверить обновления").click().run()
    assert any("Файл не изменился" in item.value for item in app.info)
    widget(app.button, "Перейти к анализу").click().run()
    assert not app.exception and app.title[0].value == "Анализ и проверка"
    assert app.selectbox(key="case_picker").value == CASE
    assert any(el.label == RULE_LABELS["R3"] + " · " + STATUS_LABELS["FLAG"] for el in app.expander)
    assert any("20.11.2026" in str(table.value) for table in app.dataframe)
    evidence = [table.value for table in app.dataframe if "Источник значения" in table.value.columns]
    assert any("Профиль проверки" in table["Источник значения"].tolist() for table in evidence)
    assert any("Разница в календарных днях: 2" in text.value for text in app.markdown)
    assert not any("2026-11-20" in str(table.value) for table in app.dataframe)
    app.button(key=f"clarify_{CASE}_R3").click().run()
    assert not app.exception
    assert widget(app.radio, "Этап заполнения").value == "2. Документы"
    widget(app.button, "К карточке").click().run()
    assert any("Сопоставить исходные документы" in element.value for element in app.markdown)
    widget(app.selectbox, "Следующее состояние").set_value("closed").run()
    widget(app.selectbox, "Заключение специалиста").set_value(storage.DECISIONS[0])
    widget(app.text_area, "Основание или комментарий (обязательно)").set_value("Даты сверены с документами")
    widget(app.button, "Зафиксировать заключение").click().run()
    assert not app.exception
    assert storage.history(CASE)["current_review"]["note"] == "Даты сверены с документами"


def test_sidebar_menu_and_home_button(db):
    app = AppTest.from_file(ROOT / "app.py").run()
    assert not app.exception and app.title[0].value == "Рабочее место"
    assert app.sidebar.radio[0].value is None
    assert app.sidebar.radio[0].options == [
        "Новое дело", "Импорт из ИС", "Анализ и проверка",
        "Аналитика", "Реестр дел", "Отчёты", "Настройки",
    ]
    assert "Обзор" not in " ".join(el.value for el in [*app.title, *app.markdown, *app.caption])
    app.sidebar.radio[0].set_value("Настройки").run()
    assert not app.exception and app.title[0].value == "Настройки"
    app.button(key="page_home").click().run()
    assert not app.exception and app.title[0].value == "Рабочее место"
    assert app.sidebar.radio[0].value is None


def test_context_hints_preserve_unsaved_values(db):
    app = AppTest.from_file(ROOT / "app.py").run()
    app.sidebar.radio[0].set_value("Новое дело").run()
    widget(app.text_input, "Номер дела").set_value("ПБ-ЧЕРНОВИК").run()
    assert not any("work-hint" in el.value and "<aside" in el.value for el in app.markdown)
    assert all(button.label != "Начать обучение" for button in app.button)
    app.toggle(key="work_hints").set_value(True).run()
    assert app.title[0].value == "Новое дело"
    assert any("категория задаёт применимые условия" in el.value for el in app.markdown)
    widget(app.radio, "Этап заполнения").set_value("2. Документы").run()
    assert any("Отметьте только документы" in el.value for el in app.markdown)
    app.sidebar.radio[0].set_value("Импорт из ИС").run()
    assert any("Проверка подключения ничего не записывает" in el.value for el in app.markdown)
    app.sidebar.radio[0].set_value("Новое дело").run()
    widget(app.radio, "Этап заполнения").set_value("1. Общие сведения").run()
    assert widget(app.text_input, "Номер дела").value == "ПБ-ЧЕРНОВИК"
    app.toggle(key="work_hints").set_value(False).run()
    assert not any("work-hint" in el.value and "<aside" in el.value for el in app.markdown)
    assert widget(app.text_input, "Номер дела").value == "ПБ-ЧЕРНОВИК"
    assert not app.exception and not storage.all_cases()
