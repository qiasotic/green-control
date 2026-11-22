"""Мастер ввода, карточка анализа и действия специалиста."""
from __future__ import annotations

from datetime import date
from sqlite3 import Error as SQLiteError
from uuid import uuid4

import pandas as pd
import streamlit as st

from core import storage
from core.presentation import (
    RULE_LABELS, case_label, category_label, comparison_evidence, comparison_text, evidence_rows,
    model_text, profile_label, source_label,
)
from core.guidance import format_review_sheet
from core.models import normalize_case
from core.reports import card_fields, filename_id
from core.rules import analyze_case
from ui.components import (
    STATUS_LABELS, clear_deleted_case_selections, literal, notify, route, status_badge, timestamp,
)
from ui.training import hint

STEPS = ("1. Общие сведения", "2. Документы", "3. Финансы", "4. Проверка и сохранение")
FINANCE = {
    "unknown": "Сведения ещё не получены",
    "payment": "Начисление и оплата",
    "exemption": "Заявлено освобождение",
}
DOCUMENTS = {"unknown": "Состав не подтверждён", "yes": "Состав полученных материалов подтверждён"}
PROOF = {"unknown": "Не установлено", "yes": "Подтверждение имеется", "no": "Отсутствие подтверждено"}
FINANCE_FIELDS = ("accrued_amount", "paid_amount", "payment_note", "exemption_choice",
                  "exemption_code", "exemption_proof")
OTHER_EXEMPTION = "__other__"


def _date_input(label, value, key):
    parsed = date.fromisoformat(value) if value else None
    selected = st.date_input(
        label, value=parsed, min_value=min(parsed or date.today(), date(1900, 1, 1)),
        max_value=max(parsed or date.today(), date(2100, 12, 31)), format="DD.MM.YYYY", key=key,
        help="Укажите дату в формате ДД.ММ.ГГГГ или выберите её в календаре.",
    )
    return selected.isoformat() if selected else ""


def _finance_mode(case):
    if case.get("finance_mode"):
        return case["finance_mode"]
    if case.get("exemption_code"):
        return "exemption"
    if any(case.get(key) is not None for key in ("accrued_amount", "paid_amount")) or case.get("payment_note"):
        return "payment"
    return "unknown"


def _form_state(case):
    if case:
        values = dict(case)
    else:
        values = {"case_id": f"ПБ-{date.today().year}-{uuid4().hex[:10].upper()}", "documents": []}
    values["finance_mode"] = _finance_mode(values)
    return {"values": values, "step": STEPS[0], "token": uuid4().hex,
            "base_version": case["_version"] if case else None}


def _general(values, profile, prefix, editing):
    hint("form_general")
    left, right = st.columns(2)
    with left:
        shown_id = st.text_input("Номер дела", value=case_label(values["case_id"]) if editing else values["case_id"], max_chars=80,
                                          disabled=editing, key=prefix + "case_id")
        if not editing:
            values["case_id"] = shown_id
        types = list(profile["case_types"])
        current = values.get("case_type")
        values["case_type"] = st.selectbox(
            "Категория дела", types, index=types.index(current) if current in types else None,
            format_func=lambda category: category_label(category, profile),
            placeholder="Выберите категорию", key=prefix + "case_type",
        )
    with right:
        values["application_date"] = _date_input("Дата заявления", values.get("application_date"),
                                                prefix + "application_date")
        values["issued_date"] = _date_input("Дата выдачи", values.get("issued_date"), prefix + "issued_date")
    hint("form_comment")
    values["comment"] = st.text_area("Комментарий к делу", values.get("comment", ""), max_chars=1000,
                                     height=88, key=prefix + "comment")


def _documents(values, profile, prefix):
    hint("form_documents")
    kind = profile["case_types"].get(values.get("case_type"), {})
    required = kind.get("required_documents", [])
    documents = set(values.get("documents", []))
    if not kind:
        st.info("Выберите категорию в общих сведениях, чтобы увидеть перечень документов.")
    if required:
        st.write("Документы по модельному профилю")
    for index, name in enumerate(required):
        checked = st.checkbox(name, value=name in documents, key=f"{prefix}document_{index}_{name}")
        if checked:
            documents.add(name)
        else:
            documents.discard(name)
    extra = documents - set(required)
    if extra:
        st.text("Прочие документы, уже внесённые в дело · " + ", ".join(sorted(extra)))
    values["documents"] = sorted(documents)
    options = list(DOCUMENTS)
    values["documents_complete"] = st.radio(
        "Комплектность материалов", options, index=options.index(values.get("documents_complete", "unknown")),
        format_func=DOCUMENTS.get, key=prefix + "documents_complete",
        help="Подтвердите состав, если видите все материалы дела. Затем сервис сравнит отмеченное с обязательным перечнем.",
    )
    if kind.get("requires_predecision") or values.get("predecision_date"):
        hint("form_predecision")
        values["predecision_date"] = _date_input(
            "Дата предварительного решения", values.get("predecision_date"), prefix + "predecision_date",
        )
    elif kind:
        st.caption("Для этой категории предварительное решение по профилю не требуется.")


def _finance(values, profile, prefix):
    hint("form_finance")
    scenarios = list(FINANCE)
    previous = values["finance_mode"]
    selected = st.radio("Финансовый сценарий", scenarios, index=scenarios.index(previous),
                        format_func=FINANCE.get, horizontal=True, key=prefix + "finance_mode")
    if selected != previous:
        for field in FINANCE_FIELDS:
            values.pop(field, None)
            st.session_state.pop(prefix + field, None)
        values["finance_mode"] = selected
    if selected == "payment":
        hint("form_payment")
        left, right = st.columns(2)
        for column, field, label in ((left, "accrued_amount", "Начислено, ₽"),
                                     (right, "paid_amount", "Оплачено, ₽")):
            with column:
                stored = values.get(field)
                values[field] = st.text_input(
                    label, value="" if stored is None else str(stored), placeholder="Сумма неизвестна",
                    max_chars=32, key=prefix + field,
                )
        st.caption("Пустое поле означает, что сумма неизвестна. Введите 0 для подтверждённой нулевой суммы.")
        values["payment_note"] = st.text_area("Пояснение к оплате", values.get("payment_note", ""),
                                              height=88, max_chars=1000, key=prefix + "payment_note")
    elif selected == "exemption":
        hint("form_exemption")
        kind = profile["case_types"].get(values.get("case_type"), {})
        allowed = list(kind.get("allowed_exemptions", []))
        options = ["", *allowed, OTHER_EXEMPTION]
        current = values.get("exemption_code", "")
        current_option = values.get("exemption_choice", current if current in options else OTHER_EXEMPTION)
        labels = {code: model_text(profile["exemptions"].get(code, {}).get("label", code)) for code in allowed}
        labels.update({"": "Основание неизвестно", OTHER_EXEMPTION: "Другое основание по документам"})
        choice = st.selectbox(
            "Основание освобождения", options, index=options.index(current_option) if current_option in options else 0,
            format_func=labels.get, key=prefix + "exemption_choice",
        )
        if choice != current_option:
            st.session_state.pop(prefix + "exemption_code", None)
        values["exemption_choice"] = choice
        values["exemption_code"] = choice
        if choice == OTHER_EXEMPTION:
            values["exemption_code"] = st.text_input("Код основания из материалов",
                value=current if current not in allowed and current != OTHER_EXEMPTION else "",
                max_chars=80, key=prefix + "exemption_code")
        options = list(PROOF)
        values["exemption_proof"] = st.selectbox(
            "Подтверждение основания", options, index=options.index(values.get("exemption_proof", "unknown")),
            format_func=PROOF.get, key=prefix + "exemption_proof",
        )
    else:
        st.caption("Суммы и основание можно внести после получения материалов.")


def _summary(values, profile):
    hint("form_summary")
    try:
        candidate = normalize_case(values, profile)
    except ValueError as exc:
        st.warning(literal(exc))
        return
    analyses = analyze_case(candidate, profile)
    fields = card_fields(candidate, profile)
    st.dataframe(pd.DataFrame(fields.items(), columns=["Поле", "Значение"]).astype(str),
                 hide_index=True, width="stretch", height=254)
    if not candidate["application_date"]:
        st.warning("Дата заявления не указана. Её можно внести после сохранения черновика.")
    for result in analyses:
        if result["status"] == "NEED_DATA":
            st.caption(f"{RULE_LABELS[result['rule']]} · {model_text(result['title'])}. {model_text(result['explanation'])}")


def editor(profile, case=None):
    edited_id = case["case_id"] if case else None
    forms = st.session_state.setdefault("case_forms", {})
    if edited_id not in forms:
        forms[edited_id] = _form_state(case)
    form = forms[edited_id]
    jump = st.session_state.pop("editing_step", None)
    if jump is not None:
        form["step"] = STEPS[jump]
        st.session_state["edit_" + form["token"] + "_step"] = form["step"]
    stale = case and form["base_version"] != case["_version"]
    if stale:
        st.warning("Пока форма была открыта, появилась новая редакция дела.")
        if st.button("Загрузить актуальную редакцию"):
            forms[edited_id] = _form_state(case)
            st.rerun()
    if case and case["_state"] == "closed":
        st.info("Сохранение изменённых сведений вернёт дело на рассмотрение. Прежнее заключение останется в истории.")
    values = form["values"]
    prefix = "edit_" + form["token"] + "_"
    step_key = prefix + "step"
    hint("form_steps")
    step = st.radio("Этап заполнения", STEPS, index=STEPS.index(form["step"]), horizontal=True,
                    label_visibility="collapsed", key=step_key)
    form["step"] = step
    index = STEPS.index(step)
    st.progress((index + 1) / len(STEPS), text=f"Шаг {index + 1} из {len(STEPS)}")
    with st.container(border=True):
        st.subheader(step.partition(". ")[2])
        if index == 0:
            _general(values, profile, prefix, case is not None)
        elif index == 1:
            _documents(values, profile, prefix)
        elif index == 2:
            _finance(values, profile, prefix)
        else:
            _summary(values, profile)
    controls = st.columns([1, 1, 2])
    if index:
        controls[0].button("Назад", key=prefix + "prev", on_click=st.session_state.update,
                           args=({step_key: STEPS[index - 1]},))
    if index < len(STEPS) - 1:
        controls[2].button("Далее", key=prefix + "next", type="primary",
                           on_click=st.session_state.update, args=({step_key: STEPS[index + 1]},))
    else:
        if not case or case["_draft"]:
            if controls[1].button("Сохранить черновик", key=prefix + "draft", disabled=bool(stale)):
                _save_editor(profile, case, form, draft=True)
        if controls[2].button("Сохранить и проверить", key=prefix + "save", type="primary", disabled=bool(stale)):
            _save_editor(profile, case, form, draft=False)


def _save_editor(profile, case, form, *, draft):
    try:
        changed = storage.save_case(
            form["values"], "Ручной ввод", profile=profile, mode="edit" if case else "create",
            draft=draft, expected_version=form["base_version"],
        )
    except (ValueError, SQLiteError) as exc:
        st.error("Не удалось сохранить дело. " + literal(exc))
        return
    st.session_state["case_forms"].pop(case["case_id"] if case else None, None)
    st.session_state["editing_case"] = None
    st.session_state["selected_case"] = form["values"]["case_id"].strip()
    st.session_state["next_page"] = "Анализ и проверка"
    notify("Черновик сохранён." if draft else
           "Дело сохранено. Результаты проверок рассчитаны." if changed else "Сведения остались без изменений.")
    st.rerun()


def _pick_case(cases):
    ids = [case["case_id"] for case in cases]
    current = st.session_state.get("selected_case")
    # Keep the global selection separate from Streamlit's widget cleanup.
    if current in ids and current != st.session_state.get("case_picker"):
        st.session_state["case_picker"] = current
    selected = st.selectbox("Разрешительное дело", ids, key="case_picker", format_func=case_label,
                            on_change=_select_card)
    st.session_state["selected_case"] = selected
    return next(case for case in cases if case["case_id"] == selected)


def _select_card():
    st.session_state["selected_case"] = st.session_state["case_picker"]
    st.session_state["editing_case"] = None


def selected_case(cases, key):
    ids = [case["case_id"] for case in cases]
    selected = st.session_state.get("selected_case")
    index = ids.index(selected) if selected in ids else 0
    case_id = st.selectbox("Разрешительное дело", ids, index=index, key=key, format_func=case_label)
    return next(case for case in cases if case["case_id"] == case_id)


def _rule_action(case, profile, rule):
    scope = f"{case['case_id']}_{case['_version']}_{case['_state']}_{rule}"
    note_key = "rule_note_" + scope
    with st.form("rule_action_" + scope, clear_on_submit=False):
        action = st.selectbox("Действие специалиста", storage.ACTIONS, key="rule_kind_" + scope)
        note = st.text_area("Комментарий к действию", max_chars=1000, height=85, key=note_key)
        submitted = st.form_submit_button(f"Зафиксировать действие · проверка {rule[-1]}")
    if submitted:
        try:
            storage.record_rule_action(case["case_id"], rule, action, note, profile=profile,
                                       expected_version=case["_version"], expected_state=case["_state"])
        except (ValueError, SQLiteError) as exc:
            st.error(literal(exc))
        else:
            st.session_state["clear_rule_note"] = note_key
            notify("Действие специалиста сохранено в истории.")
            st.rerun()


def _render_analysis(case, profile, data):
    run = case["_analysis"]
    if not run:
        st.info("Для категории сохранённого дела отсутствует расчёт по действующему профилю.")
        return
    st.caption(f"{profile_label(run['profile_version'])} · расчёт {timestamp(run['analyzed_at'])} МСК")
    recheck = next((item for item in data["rechecks"] if item["version"] == case["_version"]
                    and (item["profile_id"], item["profile_version"]) == (run["profile_id"], run["profile_version"])), None)
    if recheck:
        st.caption(f"Последний повторный расчёт · {timestamp(recheck['occurred_at'])} МСК")
    hint("case_run")
    if st.button("Перезапустить анализ", key=f"recheck_{case['case_id']}_{case['_version']}"):
        try:
            storage.recheck_analysis(case["case_id"], profile, case["_version"])
        except (ValueError, SQLiteError) as exc:
            st.error(literal(exc))
        else:
            notify("Повторный расчёт выполнен. Отметка добавлена в историю.")
            st.rerun()
    plan = {item["rule"]: item for item in run["plan"]}
    selected = next((result for status in ("FLAG", "NEED_DATA") for result in run["outcomes"]
                     if result["status"] == status), run["outcomes"][0])
    facts = " · ".join(f"{label.lower()} — {value}" for label, value, origin in comparison_evidence(selected, case, profile)
                       if origin != "Профиль проверки")
    with st.container(border=True):
        st.markdown("**Как получен результат**")
        st.caption(RULE_LABELS[selected["rule"]])
        st.write("**Исходные данные** · " + literal(facts or "Сведения для проверки отсутствуют."))
        st.caption("Источник карточки · " + literal(source_label(case["_source"])))
        st.write("**Анализ** · " + literal(comparison_text(selected, case, profile)))
        st.write("**Сигнал** · " + STATUS_LABELS[selected["status"]])
        steps = plan.get(selected["rule"], {}).get("steps", ())
        st.write("**Рекомендация** · " + literal(model_text(steps[0]) if steps else "По этому условию дополнительные действия не требуются."))
    for result in run["outcomes"]:
        rule, status = result["rule"], result["status"]
        with st.expander(f"{RULE_LABELS[rule]} · {STATUS_LABELS[status]}", expanded=status == "FLAG"):
            hint("case_rule")
            st.markdown("**1. Что проверяется?**")
            st.write(RULE_LABELS[rule].partition(". ")[2])
            st.markdown("**2. Какие исходные сведения использованы?**")
            evidence = comparison_evidence(result, case, profile)
            st.dataframe(pd.DataFrame(evidence, columns=["Поле", "Значение", "Источник значения"]).astype(str),
                         hide_index=True, width="stretch")
            st.markdown("**3. Какое условие применено?**")
            st.write(literal(model_text(result["condition"])))
            st.write("**Сопоставление** · " + literal(comparison_text(result, case, profile)))
            st.markdown("**4. Что обнаружено?**")
            status_badge(status)
            st.write(literal(model_text(result["title"])))
            st.caption(model_text(result["limitation"]))
            st.markdown("**5. Что рекомендуется сделать?**")
            for step in plan.get(rule, {}).get("steps", ["Дополнительные действия по этому условию не требуются."]):
                st.write("• " + literal(model_text(step)))
            step_index = 2 if rule == "R1" else 0 if rule == "R3" and not case.get("issued_date") else 1
            st.button("Уточнить сведения", key=f"clarify_{case['case_id']}_{rule}",
                      on_click=_clarify, args=(case["case_id"], step_index))
            st.markdown("**6. Какое действие принял специалист?**")
            latest = next((entry for entry in data["actions"] if entry["rule"] == rule
                           and entry["version"] == case["_version"]
                           and (entry["profile_id"], entry["profile_version"])
                           == (run["profile_id"], run["profile_version"])), None)
            if latest:
                st.caption(f"{latest['action']} · {timestamp(latest['occurred_at'])} МСК")
                st.text(latest["note"])
            else:
                st.caption("Действие по этой проверке ещё не зафиксировано.")
            if not case["_draft"] and case["_state"] != "closed":
                hint("case_action")
                _rule_action(case, profile, rule)
    sheet = format_review_sheet(case, profile, run["outcomes"]).encode("utf-8-sig")
    st.download_button("Скачать лист проверки TXT", sheet,
                       "list_proverki_" + filename_id(case_label(case["case_id"])) + ".txt", "text/plain")


def _clarify(case_id, step):
    st.session_state["editing_case"] = case_id
    st.session_state["editing_step"] = step


def _workflow(case, current):
    hint("case_review")
    state = case["_state"]
    if current["reviewed_at"]:
        st.write("**" + literal(current["decision"]) + "**")
        st.text(current["note"])
        st.caption(f"Зафиксировано {timestamp(current['reviewed_at'])} МСК")
    allowed = [next_state for next_state in storage.TRANSITIONS[state]
               if not (next_state == "closed" and case["_draft"])]
    scope = f"{case['case_id']}_{case['_version']}_{state}"
    target = st.selectbox("Следующее состояние", allowed, index=None, placeholder="Выберите действие",
                          format_func=storage.WORKFLOW.get, key="workflow_to_" + scope)
    if target is None:
        return
    with st.form("workflow_" + scope + target):
        decision = None
        if target == "closed":
            decision = st.selectbox("Заключение специалиста", storage.DECISIONS, index=None,
                                    placeholder="Выберите заключение")
        label = "Основание или комментарий (обязательно)" if target in ("awaiting_info", "closed") else "Комментарий"
        note = st.text_area(label, max_chars=1000, height=85)
        submitted = st.form_submit_button("Зафиксировать заключение" if target == "closed" else "Сохранить переход",
                                           type="primary")
    if submitted:
        try:
            storage.change_workflow(case["case_id"], target, note, decision=decision,
                                    expected_version=case["_version"], expected_state=state)
        except (ValueError, SQLiteError) as exc:
            st.error(literal(exc))
        else:
            notify("Заключение зафиксировано." if target == "closed" else "Рабочее состояние изменено.")
            st.rerun()


def _history(case, data, profile):
    hint("case_history")
    columns = ["Время (МСК)", "Ревизия", "Событие", "Комментарий", "Инициатор"]
    events = []
    for name, time_key, event_key, note_key in (
        ("versions", "occurred_at", "event", None),
        ("workflow", "occurred_at", "to_state", "note"),
        ("reviews", "reviewed_at", "decision", "note"),
        ("actions", "occurred_at", "action", "note"),
        ("rechecks", "occurred_at", "event", None),
    ):
        for entry in data[name]:
            event = model_text(entry[event_key])
            if name == "workflow":
                label = storage.WORKFLOW[event]
                prior = storage.WORKFLOW.get(entry["from_state"])
                event = f"{prior} → {label}" if prior else label
            elif name == "actions":
                event = f"{RULE_LABELS[entry['rule']]} · {event}"
            elif name == "reviews" and entry["reviewed_at"] != data["current_review"]["reviewed_at"]:
                event = f"{event} · историческое"
            events.append((entry[time_key], entry["version"], event, entry.get(note_key, ""), entry["actor"]))
    rows = [(timestamp(time), version, event, note, actor)
            for time, version, event, note, actor in sorted(events, reverse=True)]
    st.dataframe(pd.DataFrame(rows, columns=columns), hide_index=True, width="stretch", height=240)
    scope = f"{case['case_id']}_{case['_version']}"
    versions = {entry["version"]: entry for entry in data["versions"]}
    version = st.selectbox("Снимок редакции", list(versions), key="history_version_" + scope)
    snapshot = versions[version]
    fields = card_fields(snapshot["payload"], profile)
    st.dataframe(pd.DataFrame(fields.items(), columns=["Поле", "Значение"]).astype(str),
                 hide_index=True, width="stretch", height=240)
    for run in data["analyses"]:
        if run["version"] != version:
            continue
        current = case["_analysis"]
        active = current and (run["version"], run["profile_id"], run["profile_version"]) == (
            current["version"], current["profile_id"], current["profile_version"])
        label = "Текущий" if active else "Исторический"
        with st.expander(f"{label} расчёт · {profile_label(run['profile_version'])} · {timestamp(run['analyzed_at'])}"):
            for result in run["outcomes"]:
                st.write(f"**{RULE_LABELS[result['rule']]} · {STATUS_LABELS[result['status']]}**")
                st.write(literal(model_text(result["explanation"])))
                for name, value in evidence_rows(result, snapshot["payload"], profile):
                    st.write(f"{name}: {literal(value)}")
                st.caption(model_text(result["condition"] + " " + result["limitation"]))
    st.button("Открыть экспорт этого дела", on_click=route, args=("Отчёты", case["case_id"]))


def _case_deletion(case):
    case_id = case["case_id"]
    version, updated_at = case["_version"], case["_updated_at"]
    scope = f"delete_case_{hash((case_id, version, updated_at))}"
    with st.expander("Удаление дела"):
        st.warning("Из открытой базы будут удалены дело, его редакции, результаты проверок и заключения.")
        confirmed = st.checkbox("Подтверждаю удаление дела", key=scope + "_confirmed")
        if st.button("Удалить дело", key=scope, disabled=not confirmed):
            try:
                storage.delete_case(case_id, expected_version=version, expected_updated_at=updated_at)
            except (ValueError, SQLiteError) as exc:
                st.error("Не удалось удалить дело. " + literal(exc))
            else:
                clear_deleted_case_selections((case_id,))
                notify(f"Дело «{case_label(case_id)}» удалено.")
                st.session_state["next_page"] = "Реестр дел"
                st.rerun()


def case_page(cases, profile):
    if not cases:
        st.info("Карточки появятся после создания дела или импорта выгрузки.")
        st.button("Создать дело", type="primary", on_click=route, args=("Новое дело",))
        return
    hint("case_pick")
    case = _pick_case(cases)
    key_to_clear = st.session_state.pop("clear_rule_note", None)
    if key_to_clear:
        st.session_state.pop(key_to_clear, None)
    editing = st.session_state.get("editing_case") == case["case_id"]
    heading, controls = st.columns([3, 1.5], vertical_alignment="center")
    with heading:
        st.subheader(literal(case_label(case["case_id"])))
        line = f"{storage.WORKFLOW[case['_state']]} · Редакция {case['_version']}"
        if case["_draft"]:
            line += " · Черновик"
        st.caption(line)
    with controls:
        if st.button("К карточке" if editing else "Продолжить заполнение" if case["_draft"] else "Редактировать дело",
                     width="stretch", key=f"edit_toggle_{case['case_id']}"):
            st.session_state["editing_case"] = None if editing else case["case_id"]
            st.rerun()
    if editing:
        editor(profile, case)
        return
    _case_deletion(case)
    try:
        data = storage.history(case["case_id"])
    except (ValueError, SQLiteError) as exc:
        st.error("Не удалось открыть дело. " + literal(exc))
        st.button("Вернуться в реестр", on_click=route, args=("Реестр дел",))
        return
    if not data["current_review"]["reviewed_at"] and (case["_prior_review"] or data["reviews"]):
        st.info("Требуется повторная проверка. Прежнее заключение доступно в истории.")
    analysis, review, history, inputs = st.tabs(("Результаты проверок", "Рассмотрение и заключение", "История", "Сведения дела"))
    with analysis:
        _render_analysis(case, profile, data)
    with review:
        _workflow(case, data["current_review"])
    with history:
        _history(case, data, profile)
    with inputs:
        hint("case_input")
        fields = card_fields(case, profile)
        st.dataframe(pd.DataFrame(fields.items(), columns=["Поле", "Значение"]).astype(str),
                     hide_index=True, width="stretch")
