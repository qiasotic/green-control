"""Общие элементы рабочего места Streamlit."""
import re

import streamlit as st

from core.presentation import STATUS_LABELS, timestamp

SETTINGS = "Настройки"
NAVIGATION = (
    "Новое дело", "Импорт из ИС", "Анализ и проверка",
    "Аналитика", "Реестр дел", "Отчёты", SETTINGS,
)


STATUS_COLORS = {
    "FLAG": "flag", "NEED_DATA": "missing", "PASS": "pass", "NOT_APPLICABLE": "neutral",
}
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def route(page, case_id=None):
    if case_id is not None:
        st.session_state["selected_case"] = case_id
    st.session_state["navigation"] = page


def notify(message):
    st.session_state["notice"] = message


def clear_deleted_case_selections(case_ids):
    state = st.session_state
    for case_id in case_ids:
        state.get("case_forms", {}).pop(case_id, None)
    if state.get("selected_case") in case_ids:
        state.pop("selected_case", None)
    if state.get("editing_case") in case_ids:
        state.pop("editing_case", None)
        state.pop("editing_step", None)
    if state.get("gh_loaded") and state["gh_loaded"][1] in case_ids:
        state.pop("gh_loaded", None)
    state.pop("gh_verified", None)


def literal(value):
    return re.sub(r"([\\\x60*_{}\[\]<>#|!~$])", r"\\\1", str(value))


def status_badge(status):
    st.markdown(
        f'<span class="case-badge {STATUS_COLORS[status]}">{STATUS_LABELS[status]}</span>',
        unsafe_allow_html=True,
    )
