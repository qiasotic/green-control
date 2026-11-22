"""Зелёный контроль — локальное рабочее место специалиста."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from sqlite3 import Error as SQLiteError

import pandas as pd
import streamlit as st

from core import storage
from core.presentation import (
    RULE_LABELS, activity_label, case_label, category_label, date_text, model_text, profile_label, public_row, source_label,
)
from ui import github
from ui import municipal
from ui.training import hint, sidebar as hints_switch
from core.analytics import LIMITATION, RULES, priority as _priority, report_sheets, summarize, summary_csv, summary_html
from core.importer import INBOX, empty_template_bytes, inspect_cases, read_cases, sync_folder
from core.models import load_profile
from core.municipal import stage
from core.reports import card_fields, conclusion_text, csv_bytes, filename_id, history_sheets, xlsx_bytes
from ui.casework import case_page, editor, selected_case
from ui.components import (
    NAVIGATION, SETTINGS, STATUS_LABELS, XLSX_MIME, clear_deleted_case_selections, literal, notify, route, timestamp,
)

STYLE = """
<style>
:root {color-scheme:light;}
[data-testid="stHeader"] {background:rgba(244,247,251,.96);}
[data-testid="stSidebar"] {background:#e7f0f9;border-right:1px solid #c6dcef;}
[data-testid="stAppDeployButton"], .stAppDeployButton {display:none;}
.block-container {max-width:1440px;padding-top:3.6rem;padding-bottom:2.5rem;}
[data-testid="stSidebar"] .stRadio label {padding:.4rem .18rem;min-height:2.6rem;}
[data-testid="stSidebar"] .stRadio label p {font-size:1.07rem;font-weight:550;line-height:1.25;}
[data-testid="stSidebar"] .stRadio label:hover {color:#294f70;}
.st-key-navigation [data-testid="stRadioOption"] > div > div:first-child {
  box-sizing:border-box;width:1rem;height:1rem;flex:0 0 1rem;padding:0;border-radius:50%;
}
.st-key-navigation [data-testid="stRadioOption"][data-selected="true"] > div > div:first-child {
  background:radial-gradient(circle, #fff 0 .18rem, #426b90 .20rem);
}
.st-key-navigation [data-testid="stRadioOption"][data-selected="true"] > div > div:first-child > div {
  display:none;
}
.st-key-sidebar_brand {--brand-indent:1.375rem;}
.st-key-sidebar_brand [data-testid="stCaptionContainer"] {padding-inline-start:var(--brand-indent);}
.st-key-page_home button {justify-content:flex-start;text-align:left;padding:0;min-height:2.1rem;color:#193954;}
.st-key-page_home button > div {justify-content:flex-start;padding-inline-start:var(--brand-indent);}
.st-key-page_home button p {font-size:1.55rem;line-height:1.15;font-weight:750;text-align:left;}
.st-key-page_home button [data-testid="stMarkdownContainer"] {width:100%;}
.st-key-page_home button:hover p {text-decoration:underline;text-underline-offset:.17em;}
.work-hint {background:#eef4fa;border-left:3px solid #89aac7;border-radius:0 5px 5px 0;
  padding:.38rem .58rem;color:#294a67;font-size:.9rem;line-height:1.42;}
h1,h2,h3 {color:#193954;letter-spacing:-.022em;}
[data-testid="stMetric"] {background:#e7f0f9;border:1px solid #c6dcef;
  border-radius:8px;padding:.8rem .9rem;min-height:112px;}
[data-testid="stMetricValue"] {font-weight:680;}
[data-testid="stExpander"] details {border-color:#c6dcef;}
.case-badge {display:inline-block;padding:.23rem .58rem;border-radius:5px;font-size:.84rem;
  font-weight:650;color:#244562;background:#e7f0f9;border:1px solid #c6dcef;}
.case-badge.flag {background:#f4e7e9;border-color:#dfc1c6;color:#692f3d;}
.case-badge.missing {background:#f6edda;border-color:#e4d2a5;color:#624f28;}
.case-badge.pass {background:#e4eee9;border-color:#c0d8cd;color:#275441;}
.process-chain {display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.65rem;margin:.35rem 0 .4rem;}
.process-chain div {min-width:0;background:#e7f0f9;border:1px solid #c6dcef;
  border-radius:8px;padding:.75rem .85rem;position:relative;}
[data-testid="stDateInput"] input {min-width:8.5rem; font-variant-numeric:tabular-nums;}
[data-testid="stMetricLabel"] p {white-space:normal;overflow-wrap:anywhere;}
.process-chain div + div::before {content:"→";position:absolute;left:-.72rem;top:.85rem;color:#426B90;}
.process-chain strong {color:#193954;} .process-chain p {margin:.35rem 0 0;font-size:.91rem;line-height:1.38;}
@media (max-width:1150px) {.process-chain {grid-template-columns:repeat(2,minmax(0,1fr));}
  .process-chain div + div::before {display:none;}}
@media (max-width:800px) {.block-container {padding-top:3rem;}}
</style>
"""
COLUMNS = ["Дело", "Категория", "Дата заявления", "Источник", "Версия правил", "Сигналы",
           "Нехватка сведений", "Состояние дела", "Черновик", "Последнее изменение (МСК)"]
ROOT = Path(__file__).resolve().parent
DEMO_FILE = ROOT / "examples" / "TECHNICAL_SMOKE_3_CASES.csv"
SYNTHETIC = ROOT / "data" / "synthetic"
DEMO_OPTIONS = {
    "DEMO-OK-001": "Контрольное дело № 001 · отклонений не выявлено",
    "DEMO-FLAG-002": "Контрольное дело № 002 · выявлены отклонения",
    "DEMO-NODATA-003": "Контрольное дело № 003 · недостаточно сведений",
    "all": "Набор данных для проверки · 200 дел",
}
PRESENCE = {"any": "Все", "yes": "Есть", "no": "Нет"}
SORTS = ("По приоритету", "Сначала изменённые", "По номеру дела", "По дате заявления")
FILTER_DEFAULTS = {
    "search": "", "categories": [], "states": [], "sources": [], "from_day": None, "to_day": None,
    "flags": "any", "missing": "any", "rule": "any", "result": "any", "sort": SORTS[0], "size": 20,
}


def _case_entries(cases, profile):
    result = []
    for case in cases:
        analysis = case["_analysis"]
        rule_status = {item["rule"]: item["status"] for item in analysis["outcomes"]} if analysis else {}
        row = {
            "Дело": case["case_id"],
            "Категория": category_label(case["case_type"], profile),
            "Дата заявления": case.get("application_date") or "—",
            "Источник": case["_source"],
            "Версия правил": profile_label(analysis["profile_version"]) if analysis else "—",
            "Сигналы": sum(status == "FLAG" for status in rule_status.values()),
            "Нехватка сведений": sum(status == "NEED_DATA" for status in rule_status.values()),
            "Состояние дела": storage.WORKFLOW[case["_state"]],
            "Черновик": "Да" if case["_draft"] else "Нет",
            "Последнее изменение (МСК)": timestamp(case["_updated_at"]),
            **{rule: rule_status.get(rule, "—") for rule in RULES},
        }
        result.append((case, row))
    return result


def _metrics(values):
    for column, (label, value) in zip(st.columns(4), values.items()):
        column.metric(label, value)


def _empty_registry():
    st.info("В реестре пока нет дел.")
    st.button("Создать дело", type="primary", on_click=route, args=("Новое дело",))


def _home(entries, profile):
    steps = (
        ("Исходные данные", "Введите сведения вручную, загрузите файл или получите данные из GitHub."),
        ("Анализ", "Документы, суммы и даты сопоставляются с условиями применимого профиля."),
        ("Сигнал", "По каждому условию видны результат, исходные значения и причина."),
        ("Рекомендация", "Уточните недостающие сведения, проверьте документы и оформите решение."),
    )
    st.markdown('<div class="process-chain">' + "".join(
        f"<div><strong>{i}. {name}</strong><p>{description}</p></div>"
        for i, (name, description) in enumerate(steps, 1)
    ) + "</div>", unsafe_allow_html=True)
    hint("home_flow")
    create, upload = st.columns(2)
    create.button("Создать разрешительное дело", type="primary", on_click=route, args=("Новое дело",))
    upload.button("Загрузить данные", on_click=route, args=("Импорт из ИС",))
    hint("home_metrics")
    _metrics(summarize(entries)["metrics"])
    municipal.queue(entries, profile)
    st.subheader("Приоритетная очередь")
    st.caption("Выданные билеты и дела с неуточнённым этапом. Подготовка проектов отражена в задачах по выдаче.")
    hint("home_queue")
    queue = [
        (case, row) for case, row in sorted(entries, key=_priority)
        if case["_state"] != "closed" and not case["_draft"] and stage(case) != "pre_issue"
        and (row["Сигналы"] or row["Нехватка сведений"])
    ]
    if not entries:
        st.info("В реестре пока нет дел.")
    elif not queue:
        st.caption("Среди открытых заполненных дел нет отклонений и запросов на уточнение.")
    else:
        with st.container(border=True):
            for title, column in zip(
                ("Дело", "Категория", "Результаты проверок", "Состояние"), st.columns((2, 2.5, 2.4, 1.9)),
            ):
                column.caption(title)
            for case, row in queue[:6]:
                number, kind, results, state = st.columns((2, 2.5, 2.4, 1.9), vertical_alignment="center")
                number.button(literal(case_label(case["case_id"])), key="queue_" + case["case_id"],
                              on_click=route, args=("Анализ и проверка", case["case_id"]))
                kind.write(literal(row["Категория"]))
                missing = row["Нехватка сведений"]
                pending = f"требуют уточнения: {missing}" if missing else "уточнений нет"
                results.write(f"отклонений: {row['Сигналы']} · {pending}")
                state.write(row["Состояние дела"])
        st.button("Открыть реестр", on_click=route, args=("Реестр дел",))
    st.subheader("Последние действия")
    hint("home_activity")
    activity = storage.recent_activity()
    if activity:
        rows = [{
            "Время (МСК)": timestamp(event["occurred_at"]), "Дело": case_label(event["case_id"]),
            "Действие": storage.WORKFLOW.get(event["event"], activity_label(event["event"])),
        } for event in activity]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                     height=min(321, 36 * (len(rows) + 1)))
    else:
        st.caption("История действий пока пуста.")


def _restore_filter_widget(name, filters, scope):
    key = f"{scope}_{name}"
    if key not in st.session_state:
        st.session_state[key] = filters[name]
    return key


def _reset_filters(scope):
    for key in FILTER_DEFAULTS:
        st.session_state.pop(f"{scope}_{key}", None)
    st.session_state[f"{scope}_filters"] = FILTER_DEFAULTS.copy()
    if scope == "registry":
        st.session_state.pop("registry_page", None)


def _filter_controls(entries, scope="registry"):
    values = st.session_state.setdefault(f"{scope}_filters", FILTER_DEFAULTS.copy())

    def key(name):
        return _restore_filter_widget(name, values, scope)

    category_names = {case["case_type"]: row["Категория"] for case, row in entries}
    sources = sorted({row["Источник"] for _, row in entries})
    for name, options in (("categories", category_names), ("sources", sources)):
        widget = f"{scope}_{name}"
        current = st.session_state.get(widget, values[name])
        valid = [value for value in current if value in options]
        if valid != current:
            st.session_state[widget] = valid

    st.text_input("Поиск по номеру или комментарию", placeholder="Введите номер дела или часть комментария",
                  key=key("search"))
    hint("search")
    with st.expander("Фильтры выборки" if scope == "analytics" else "Фильтры реестра",
                     expanded=scope == "analytics"):
        hint("filter_scope")
        first, second, third = st.columns(3)
        first.multiselect("Категория", list(category_names), format_func=lambda value: category_names[value],
                          key=key("categories"), placeholder="Все категории")
        second.multiselect("Состояние дела", list(storage.WORKFLOW), format_func=storage.WORKFLOW.get,
                           key=key("states"), placeholder="Все состояния")
        third.multiselect("Источник", sources, format_func=source_label, key=key("sources"), placeholder="Все источники")
        first.date_input("Дата заявления с", value=None, format="DD.MM.YYYY",
                         min_value=date(1900, 1, 1), max_value=date(2100, 12, 31), key=key("from_day"))
        second.date_input("Дата заявления по", value=None, format="DD.MM.YYYY",
                          min_value=date(1900, 1, 1), max_value=date(2100, 12, 31), key=key("to_day"))
        hint("filter_result")
        first, second, third = st.columns(3)
        first.selectbox("Наличие отклонений", list(PRESENCE), format_func=PRESENCE.get, key=key("flags"))
        second.selectbox("Требуется уточнение", list(PRESENCE), format_func=PRESENCE.get, key=key("missing"))
        third.selectbox("Индикатор", ["any", *RULES],
                        format_func=lambda value: "Все" if value == "any" else RULE_LABELS[value], key=key("rule"))
        third.selectbox("Результат индикатора", ["any", *STATUS_LABELS],
                        format_func=lambda value: "Все" if value == "any" else STATUS_LABELS[value],
                        key=key("result"))
        st.button("Сбросить фильтры", on_click=_reset_filters, args=(scope,))
    if scope == "registry":
        sort_col, size_col = st.columns((2, 1))
        sort_col.selectbox("Сортировка", SORTS, key=key("sort"))
        size_col.selectbox("Дел на странице", [20, 50, 100], key=key("size"))
    updated = {name: st.session_state.get(f"{scope}_{name}", default) for name, default in FILTER_DEFAULTS.items()}
    if scope == "registry" and updated != values:
        st.session_state.pop("registry_page", None)
    st.session_state[f"{scope}_filters"] = updated
    return updated


def _matches_presence(count, wanted):
    return wanted == "any" or bool(count) == (wanted == "yes")


def _filtered(entries, options):
    start, end = options["from_day"], options["to_day"]
    if start and end and start > end:
        return []
    search = options["search"].strip().casefold()
    chosen = []
    for case, row in entries:
        if search and not any(search in (text or "").casefold() for text in (
            case["case_id"], case_label(case["case_id"]), case.get("comment"),
        )):
            continue
        if any(options[filter_name] and value not in options[filter_name]
               for filter_name, value in (("categories", case["case_type"]), ("states", case["_state"]),
                                          ("sources", case["_source"]))):
            continue
        if start or end:
            try:
                date_value = date.fromisoformat(case.get("application_date", ""))
            except (TypeError, ValueError):
                continue
            if (start and date_value < start) or (end and date_value > end):
                continue
        if not (_matches_presence(row["Сигналы"], options["flags"])
                and _matches_presence(row["Нехватка сведений"], options["missing"])):
            continue
        rules = RULES if options["rule"] == "any" else (options["rule"],)
        allowed = STATUS_LABELS if options["result"] == "any" else (options["result"],)
        if (options["rule"] != "any" or options["result"] != "any") and not any(row[rule] in allowed for rule in rules):
            continue
        chosen.append((case, row))
    sorting = options["sort"]
    if sorting == SORTS[1]:
        return sorted(chosen, key=lambda entry: entry[0]["_updated_at"], reverse=True)
    if sorting == SORTS[2]:
        return sorted(chosen, key=lambda entry: entry[0]["case_id"].casefold())
    if sorting == SORTS[3]:
        return sorted(chosen, key=lambda entry: entry[0].get("application_date") or "", reverse=True)
    return sorted(chosen, key=_priority)


def _registry_downloads(rows, key):
    rows = [public_row(row) for row in rows]
    columns = [*COLUMNS, *(RULE_LABELS[rule] for rule in RULES)]
    left, right = st.columns(2)
    left.download_button("Скачать реестр CSV", csv_bytes(rows, columns), "reestr.csv", "text/csv",
                         key=key + "_csv")
    right.download_button("Скачать реестр XLSX", xlsx_bytes([("Реестр", rows, columns)]),
                          "reestr.xlsx", XLSX_MIME, key=key + "_xlsx")


def _registry(entries, profile):
    if not entries:
        _empty_registry()
        return
    filters = _filter_controls(entries)
    start, end = filters["from_day"], filters["to_day"]
    if start and end and start > end:
        st.warning("Начало периода должно быть не позже окончания.")
        return
    chosen = _filtered(entries, filters)
    st.caption(f"Найдено дел: {len(chosen)} из {len(entries)}")
    if not chosen:
        st.info("Под выбранные условия дела не попали.")
        return
    size = filters["size"]
    pages = (len(chosen) + size - 1) // size
    current_page = st.session_state.get("registry_page", 1)
    if current_page > pages:
        st.session_state["registry_page"] = 1
    hint("registry_table")
    page_col, pick_col, open_col = st.columns([1, 3, 1.4], vertical_alignment="bottom")
    page = page_col.selectbox("Страница", range(1, pages + 1), key="registry_page")
    visible = chosen[(page - 1) * size:page * size]
    selection = pick_col.selectbox("Дело на странице", [case["case_id"] for case, _ in visible],
                                   index=None, placeholder="Выберите дело", format_func=case_label,
                                   key=f"registry_pick_{page}")
    open_col.button("Открыть дело", type="primary", on_click=route,
                    args=("Анализ и проверка", selection), disabled=selection is None)
    rows = [public_row(row) for _, row in visible]
    st.dataframe(pd.DataFrame(rows, columns=COLUMNS), hide_index=True, width="stretch",
                 height=min(500, 36 * (len(rows) + 1)))
    st.caption(f"Экспорт включает все {len(chosen)} дел, соответствующих фильтрам.")
    _registry_downloads([row for _, row in chosen], "registry")


def _analytics(entries, profile):
    if not entries:
        _empty_registry()
        return
    filters = _filter_controls(entries, "analytics")
    start, end = filters["from_day"], filters["to_day"]
    if start and end and start > end:
        st.warning("Начало периода должно быть не позже окончания.")
        return
    selected = _filtered(entries, filters)
    summary = summarize(selected)
    st.caption(f"В выборке {len(selected)} из {len(entries)} дел.")
    hint("analytics_metrics")
    _metrics(summary["metrics"])
    if not selected:
        st.info("Под выбранные условия дела не попали.")
        return
    left, right = st.columns(2)
    with left:
        st.subheader("Результаты по индикаторам")
        hint("analytics_rules")
        per_rule = pd.DataFrame(public_row(row) for row in summary["rules"])
        st.bar_chart(per_rule, x="Индикатор", y=[STATUS_LABELS["FLAG"], STATUS_LABELS["NEED_DATA"]], stack=False,
                     x_label="Модельное правило", y_label="Дел", color=["#426B90", "#A07B3F"])
        st.dataframe(per_rule, hide_index=True, width="stretch", height=145)
    with right:
        st.subheader("Заявления по месяцам")
        hint("analytics_months")
        if summary["months"]:
            import altair as alt
            months = pd.DataFrame(public_row(row) for row in summary["months"])
            st.altair_chart(alt.Chart(months).mark_bar(color="#547B9F").encode(
                x=alt.X("Месяц заявления:N", sort=months["Месяц заявления"].tolist()),
                y=alt.Y("Дел:Q"),
            ), width="stretch")
        else:
            st.info("В выборке нет дел с корректной датой заявления.")
        st.caption(f"Дел без корректной даты заявления: {summary['undated']}.")
    st.subheader("Приоритетные проверки")
    hint("analytics_queue")
    queue = summary["queue"]
    if queue:
        st.dataframe(pd.DataFrame(public_row(row) for row in queue), hide_index=True, width="stretch", height=min(300, 36 * (len(queue) + 1)))
        target = st.selectbox("Дело с формальным отклонением", [record["Дело"] for record in queue],
                              key="analytics_case", format_func=case_label)
        st.button("Открыть карточку дела", on_click=route, args=("Анализ и проверка", target))
    else:
        st.caption("В выбранной совокупности нет открытых заполненных дел с выявленными отклонениями.")
    hint("analytics_actions")
    report, recheck = st.columns(2)
    report.button("Сформировать сводный отчёт", on_click=_route_to_summary)
    if recheck.button("Перезапустить анализ выборки",
                      help="Запись в историю добавляется только после проверки всей выбранной совокупности."):
        try:
            storage.recheck_analyses([(case["case_id"], case["_version"]) for case, _ in selected], profile)
        except (ValueError, SQLiteError) as exc:
            st.error(literal(exc))
        else:
            st.success(f"Повторно рассчитано дел: {len(selected)}.")


def _route_to_summary():
    st.session_state["report_scope"] = "Фильтры аналитики"
    route("Отчёты")


def _filter_metadata(options, profile):
    rule_status = options["result"]
    return {
        "Дата заявления с": date_text(options["from_day"]) if options["from_day"] else "Любая",
        "Дата заявления по": date_text(options["to_day"]) if options["to_day"] else "Любая",
        "Поисковый запрос": options["search"] or "Любой",
        "Категория": ", ".join(category_label(value, profile)
                              for value in options["categories"]) or "Все",
        "Рабочее состояние": ", ".join(storage.WORKFLOW[value] for value in options["states"]) or "Все",
        "Источник": ", ".join(map(source_label, options["sources"])) or "Все",
        "Наличие отклонений": PRESENCE[options["flags"]],
        "Требуется уточнение": PRESENCE[options["missing"]],
        "Индикатор": RULE_LABELS.get(options["rule"], "Все"),
        "Результат индикатора": STATUS_LABELS.get(rule_status, "Все"),
    }


def _summary_report(entries, profile):
    st.subheader("Сводный отчёт")
    hint("report_summary")
    scope = st.selectbox("Параметры выборки", ("Весь реестр", "Фильтры аналитики", "Фильтры реестра"),
                         key="report_scope")
    key = {"Фильтры аналитики": "analytics_filters", "Фильтры реестра": "registry_filters"}.get(scope)
    options = st.session_state.get(key, FILTER_DEFAULTS) if key else FILTER_DEFAULTS
    start, end = st.columns(2)
    report_start = start.date_input("Период отчёта с", value=options["from_day"], format="DD.MM.YYYY",
                                    key=f"report_start_{scope}_{options['from_day']}")
    report_end = end.date_input("Период отчёта по", value=options["to_day"], format="DD.MM.YYYY",
                                key=f"report_end_{scope}_{options['to_day']}")
    if report_start and report_end and report_start > report_end:
        st.warning("Начало периода должно быть не позже окончания.")
        return
    options = {**options, "from_day": report_start, "to_day": report_end}
    selected = _filtered(entries, options)
    st.caption(f"В отчёте {len(selected)} из {len(entries)} дел. {LIMITATION}")
    sheets = report_sheets(summarize(selected), _filter_metadata(options, profile),
                           profile, timestamp(datetime.now(timezone.utc).isoformat()))
    csv, xlsx, html = st.columns(3)
    csv.download_button("Сводный отчёт CSV", summary_csv(sheets), "svodny_otchet.csv", "text/csv")
    xlsx.download_button("Сводный отчёт XLSX", xlsx_bytes(sheets), "svodny_otchet.xlsx", XLSX_MIME)
    html.download_button("Сводный отчёт HTML", summary_html(sheets), "svodny_otchet.html", "text/html",
                         help="Откройте файл в браузере, чтобы распечатать или сохранить в PDF.")


def _evaluation_downloads():
    files = [(SYNTHETIC / "evaluation.md", "Отчёт испытаний (Markdown)", "text/markdown"),
             (SYNTHETIC / "evaluation.json", "Метрики испытаний (JSON)", "application/json")]
    if all(path.is_file() for path, _, _ in files):
        with st.expander("Материалы испытаний"):
            hint("report_evaluation")
            st.caption("Испытания выполнены на синтетических данных. В технических файлах используются внутренние обозначения.")
            for column, (path, label, mime) in zip(st.columns(2), files):
                column.download_button(label, path.read_bytes(), path.name, mime)


def _reports(entries, profile):
    if not entries:
        _empty_registry()
        _evaluation_downloads()
        return
    st.subheader("Реестр дел")
    hint("report_registry")
    options = st.session_state.get("registry_filters", FILTER_DEFAULTS)
    has_filters = any(options[key] != FILTER_DEFAULTS[key] for key in FILTER_DEFAULTS if key not in ("sort", "size"))
    filtered = st.checkbox("Применить текущие фильтры реестра", value=has_filters, disabled=not has_filters)
    chosen = _filtered(entries, options) if filtered else entries
    st.caption(f"Выгрузка содержит {len(chosen)} дел.")
    _registry_downloads([row for _, row in chosen], "reports")
    st.divider()
    st.subheader("Индивидуальное заключение")
    hint("report_case")
    case = selected_case([case for case, _ in entries], "report_case")
    data = storage.history(case["case_id"])
    if data["current_version"] != case["_version"]:
        st.warning("Карточка изменилась. Выберите дело повторно для загрузки актуального заключения.")
        return
    run = case["_analysis"]
    st.caption(f"Редакция {case['_version']} · заключение и история рассмотрения")
    st.write(literal(data["current_review"]["decision"]))
    left, right = st.columns(2)
    file_stub = "Заключение_" + filename_id(case_label(case["case_id"]))
    left.download_button("Скачать заключение TXT", conclusion_text(case, profile, data),
                         file_stub + ".txt", "text/plain")
    fields = card_fields(case, profile)
    overview = [{"Поле": key, "Значение": value} for key, value in fields.items()]
    overview.extend({"Поле": key, "Значение": value} for key, value in {
        "Рабочее состояние": storage.WORKFLOW[case["_state"]],
        "Редакция": case["_version"], "Источник": source_label(case["_source"]),
        "Профиль": "Профиль проверки" if run else "",
        "Версия профиля": run["profile_version"] if run else "",
        "Статус профиля": run["profile_status"] if run else "",
        "Расчёт (МСК)": timestamp(run["analyzed_at"]) if run else "",
        "Заключение": data["current_review"]["decision"],
        "Обоснование": data["current_review"]["note"],
        "Дата заключения (МСК)": timestamp(data["current_review"]["reviewed_at"]),
        "Ограничение": "Модельный профиль применяется для предварительной проверки. Итоговую оценку даёт специалист.",
    }.items())
    sheets = [("Карточка и заключение", overview, ["Поле", "Значение"])]
    sheets.extend(history_sheets(data, profile))
    sheets.extend(municipal.report_sheets(case, profile))
    right.download_button("Скачать заключение XLSX", xlsx_bytes(sheets), file_stub + ".xlsx", XLSX_MIME)
    st.divider()
    _summary_report(entries, profile)
    st.divider()
    _evaluation_downloads()


def _method(entries, profile):
    st.subheader("Действующий профиль")
    hint("settings_profile")
    st.write(profile_label(profile["version"]))
    st.caption("Профиль модельный и не утверждён для применения. Контрольный набор имеет синтетическое происхождение.")
    st.write("Правила задают условия предварительной проверки. Для ведомственного применения требуется утверждённый профиль территории.")
    for kind in profile["case_types"].values():
        with st.expander(model_text(kind["label"])):
            st.write("Документы · " + ", ".join(kind["required_documents"]))
            st.write("Финансы · " + ("Профиль предусматривает освобождение"
                                      if kind["payment_mode"] == "exempt" else "Начисление и оплата либо допустимое освобождение"))
            codes = [model_text(profile["exemptions"][code]["label"]) for code in kind["allowed_exemptions"]]
            if codes:
                st.write("Допустимые основания · " + ", ".join(codes))
            st.write("Предварительное решение · " + ("Требуется" if kind["requires_predecision"] else "Не требуется"))
    st.subheader("Значения результатов")
    hint("settings_statuses")
    for status in STATUS_LABELS:
        st.write(STATUS_LABELS[status])
    st.write("Отклонение означает несоответствие модельному условию. Недостаточность сведений указывает на пропуски. "
             "Отсутствие отклонений относится к отдельному условию, а неприменимые проверки учитываются отдельно. "
             "Предварительные результаты не устанавливают законность разрешения, коррупцию или виновность.")
    st.subheader("Данные для внедрения")
    st.write("Для ведомственного пилота нужны утверждённые условия применимости, перечни документов и исключений, "
             "подтверждённые источники дат, начислений и платежей, а также независимая валидация специалистами.")
    st.subheader("Порядок работы")
    hint("settings_workflow")
    st.write("Создайте карточку или импортируйте выгрузку. Просмотрите результаты проверок, внесите уточнения и зафиксируйте заключение. "
             "Запрос сведений регистрируется локально. Используйте технические и обезличенные данные.")
    st.caption("Действия подписываются как «Локальный оператор». Для совместной работы ведомства потребуется "
               "серверная БД, например PostgreSQL, и идентификация сотрудников.")


def _import(entries, profile):
    files, pay, acts, remote, removal = st.tabs(("Файловый импорт", "Платежи", "Акты обследования",
                                               "Подключение к GitHub", "Удаление наборов"),
                                              key="import_tabs", on_change="rerun")
    with files:
        _file_import(entries, profile)
    cases = [c for c, _ in entries]
    with pay:
        municipal.payment_panel(cases, profile)
    with acts:
        municipal.act_panel(cases, profile)
    with remote:
        github.panel(profile)
    with removal:
        _dataset_deletion(entries)


def _dataset_deletion(entries):
    sources = sorted({case["_source"] for case, _ in entries if case["_source"] and case["_source"] != "Ручной ввод"})
    if not sources:
        st.info("В открытой базе нет наборов, загруженных из файлов или других источников.")
        return
    st.caption("Набор объединяет дела с одним источником в их текущих редакциях.")
    source = st.selectbox("Источник набора", sources, index=None, format_func=source_label,
                          placeholder="Выберите источник", key="delete_dataset_source")
    if source is None:
        return
    members = [case for case, _ in entries if case["_source"] == source]
    expected = tuple(sorted((case["case_id"], case["_version"], case["_updated_at"]) for case in members))
    st.write(f"Дел в наборе — {len(members)}.")
    with st.expander("Дела в наборе"):
        rows = [{"Дело": case_label(case["case_id"]), "Состояние": storage.WORKFLOW[case["_state"]]} for case in members]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=245)
    st.warning("Удаление затронет все редакции выбранных дел, включая редакции из других источников, их расчёты и заключения.")
    if source.startswith("GitHub · "):
        st.caption("В открытой базе также будут удалены записи об импорте этого файла из GitHub.")
    scope = f"delete_dataset_{hash((source, expected))}"
    confirmed = st.checkbox("Подтверждаю удаление набора", key=scope + "_confirmed")
    if st.button("Удалить набор", key=scope, disabled=not confirmed):
        try:
            removed = storage.delete_dataset(source, expected)
        except (ValueError, SQLiteError) as exc:
            st.error("Не удалось удалить набор. " + literal(exc))
        else:
            clear_deleted_case_selections(removed)
            notify(f"Набор удалён. Удалено дел — {len(removed)}.")
            st.rerun()


def _file_import(entries, profile):
    st.caption("Файловый обмен CSV/XLSX. Импорт выполняется по команде специалиста.")
    hint("file_template")
    st.download_button("Скачать шаблон XLSX", empty_template_bytes(localized=True), "shablon_vygruzki.xlsx", XLSX_MIME)
    update = st.checkbox("Разрешить создание новых редакций для существующих номеров дел",
                         help="Дела с совпадающими сведениями пропускаются. Отличающиеся дела можно обновить с сохранением предыдущих редакций.")
    st.subheader("Загрузка файла")
    hint("file_upload")
    uploaded = st.file_uploader("Выберите выгрузку", type=["csv", "xlsx"])
    records = []
    errors = []
    if uploaded is not None:
        try:
            records, errors = inspect_cases(uploaded, uploaded.name, profile)
        except (ValueError, OSError) as exc:
            st.error(literal(str(exc)))
        else:
            st.markdown("**Проверка перед импортом**")
            st.caption(f"Строк в файле: {len(records) + len(errors)} · Прошли проверку: {len(records)} · С ошибками: {len(errors)}")
            if errors:
                st.error("Исправьте выгрузку и загрузите её повторно.")
                st.dataframe(pd.DataFrame(errors, columns=["Строка", "Причина"]), hide_index=True)
            if records:
                existing = {case["case_id"] for case, _ in entries}
                st.caption(f"Номеров, уже имеющихся в реестре: {sum(case['case_id'] in existing for case in records)}")
                st.caption(f"Первые {min(len(records), 10)} карточек, прошедших проверку.")
                preview = [{
                    "Номер дела": case_label(case["case_id"]),
                    "Категория": category_label(case["case_type"], profile),
                    "Дата заявления": date_text(case["application_date"]),
                    "Дата выдачи": date_text(case["issued_date"]),
                } for case in records[:10]]
                st.dataframe(pd.DataFrame(preview), hide_index=True)
    if st.button("Импортировать файл", key="file_import_commit", type="primary",
                 disabled=not records or bool(errors)):
        try:
            changed = storage.save_cases(records, f"Импорт: {uploaded.name}", profile=profile, update_existing=update)
        except (ValueError, OSError, SQLiteError) as exc:
            st.error(literal(exc))
        else:
            st.success(f"Сохранено дел: {changed}. Совпадающих пропущено: {len(records) - changed}.")
    st.subheader("Папка обмена")
    hint("file_folder")
    st.code(str(INBOX), language="text")
    if st.button("Импортировать из папки обмена"):
        try:
            messages = sync_folder(profile, update_existing=update)
        except OSError as exc:
            st.error(literal(exc))
            return
        if not messages:
            st.info("В папке обмена нет новых CSV/XLSX.")
        for message in messages:
            show = st.success if message.startswith("ГОТОВО") else st.warning if message.startswith("ВНИМАНИЕ") else st.error
            show(literal(message))
    _demonstration(entries, profile)


def _demonstration(entries, profile):
    st.divider()
    st.subheader("Контрольные сценарии")
    hint("file_examples")
    st.warning("Сведения полностью синтетические. Загрузка добавит их в открытую базу. "
               "Для просмотра контрольных сценариев можно использовать отдельную копию базы.")
    choice = st.selectbox("Контрольный сценарий", list(DEMO_OPTIONS), format_func=DEMO_OPTIONS.get)
    confirm = st.checkbox("Подтверждаю загрузку синтетических дел в открытую базу")
    available = any(case["case_id"] == choice for case, _ in entries)
    if st.button("Загрузить контрольные дела", disabled=not confirm):
        path = SYNTHETIC / "dataset.csv" if choice == "all" else DEMO_FILE
        try:
            cases = read_cases(path, path.name, profile)
            if choice != "all":
                cases = [case for case in cases if case["case_id"] == choice]
                if not cases:
                    raise ValueError("Контрольное дело отсутствует в подготовленной выгрузке.")
            source = "Синтетика · набор 200" if choice == "all" else "Синтетика · контрольный сценарий"
            changed = storage.save_cases(cases, source, profile=profile)
        except (OSError, ValueError, SQLiteError) as exc:
            st.error(literal(exc))
        else:
            available = True
            st.success(f"Добавлено: {changed}. Совпадающих пропущено: {len(cases) - changed}.")
    if choice == "all":
        st.button("Посмотреть аналитику", on_click=route, args=("Аналитика",))
    elif available:
        st.button("Открыть контрольное дело", on_click=route, args=("Анализ и проверка", choice))


def _new_case(entries, profile):
    editor(profile)


SCREENS = {
    None: (_home, "Предварительная проверка разрешительных дел"),
    "Реестр дел": (_registry, "Поиск, фильтрация и выгрузка разрешительных дел"),
    "Новое дело": (_new_case, "Ввод сведений о разрешительном деле"),
    "Анализ и проверка": (None, "Исходные сведения, результаты проверок и решения специалиста"),
    "Аналитика": (_analytics, "Показатели предварительной проверки"),
    "Отчёты": (_reports, "Выгрузка реестра, заключений и истории"),
    SETTINGS: (_method, "Профиль правил и порядок работы"),
    "Импорт из ИС": (_import, "Приём файлов ведомственного обмена"),
}


def main():
    st.set_page_config(page_title="Зелёный контроль", layout="wide", initial_sidebar_state="expanded")
    st.markdown(STYLE, unsafe_allow_html=True)
    try:
        profile = load_profile()
        storage.initialize(profile)
        cases = storage.all_cases(profile)
    except (OSError, ValueError, SQLiteError) as exc:
        st.error("Рабочее место недоступно. Проверьте профиль и локальную базу. " + literal(exc))
        st.stop()
    pending_page = st.session_state.pop("next_page", None)
    if pending_page:
        route(pending_page)
    with st.sidebar:
        with st.container(key="sidebar_brand"):
            st.button("Зелёный контроль", key="page_home", type="tertiary", width="stretch",
                      on_click=route, args=(None,), help="На главный экран")
            st.caption("Цифровой сервис")
        st.divider()
        page = st.radio("Разделы", NAVIGATION, index=None, key="navigation",
                        label_visibility="collapsed")
        st.divider()
        st.caption(profile_label(profile["version"]))
        hints_switch()
    screen, description = SCREENS[page]
    st.title(page or "Рабочее место")
    st.caption(description)
    notice = st.session_state.pop("notice", None)
    error = st.session_state.pop("municipal_error", None)
    if error:
        st.error(literal(error))
    if notice:
        st.success(notice)
    if page == "Анализ и проверка":
        case_page(cases, profile)
    else:
        screen(_case_entries(cases, profile), profile)


if __name__ == "__main__":
    main()
