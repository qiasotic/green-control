"""Интерфейс муниципальной сверки, служебных запросов и очереди."""
from sqlite3 import Error as SQLiteError
from collections import Counter

import streamlit as st

from core import acts, payments
from core.municipal import FIELDS, PAIRS, STAGES, document_check, payment_request, stage, tasks
from core.presentation import case_label, date_text
from core.reports import csv_bytes, filename_id, xlsx_bytes
from ui.components import XLSX_MIME, literal, route, notify

TASK_COLUMNS = ("Дело", "Этап", "Кому", "Действие", "Текст запроса")


def _commit(action, args, message):
    try:
        result = action(*args)
        notify(message.format(result=result))
    except (ValueError, OSError, SQLiteError) as exc:
        st.session_state["municipal_error"] = str(exc)


def _current_payment(case, matches):
    row = matches.get(case["case_id"])
    if row and case["_version"] in (row["version"], row.get("applied_version")):
        return row
    if row:
        return {**row, "status": payments.CHECK, "reason": "Карточка изменилась после выписки. Сверьте актуальные реквизиты."}
    return None


def queue(entries, profile):
    st.subheader("Задачи по выдаче билетов")
    matches, batch = payments.latest_by_case()
    if batch:
        st.caption("Платёжная выписка на " + date_text(batch["as_of"]))
    phase = st.selectbox("Этап работы", ["Все этапы", *STAGES.values()], key="municipal_phase")
    rows = []
    for case, _ in entries:
        if case["_state"] == "closed" or (phase != "Все этапы" and STAGES[stage(case)] != phase):
            continue
        rows.extend(tasks(case, profile, _current_payment(case, matches)))
    if not rows:
        st.caption("По открытым делам выбранного этапа задач нет.")
        return
    owner = st.selectbox("Исполнитель задачи", ["Все подразделения", *sorted({r["Кому"] for r in rows})])
    rows = [r for r in rows if owner == "Все подразделения" or r["Кому"] == owner]
    st.dataframe([{k: r[k] for k in TASK_COLUMNS[:-1]} for r in rows], hide_index=True, width="stretch", height=245)
    ids = list(dict.fromkeys(r["Дело"] for r in rows))
    cid = st.selectbox("Дело из очереди", ids, format_func=case_label)
    st.button("Перейти к делу", on_click=route, args=("Анализ и проверка", cid))
    st.download_button("Скачать задачи и тексты запросов", csv_bytes(rows, TASK_COLUMNS),
                       "zadachi.csv", "text/csv")


def _match_key(row):
    if row["case_id"]:
        return ("case", row["case_id"])
    return ("file", row.get("file_case", ""), row["charge_id"])


def _view(r, day, previous=None):
    return {"Номер дела": r["case_id"] or r.get("file_case", ""), "Результат": r["status"],
            "Динамика": payments.change_label(r, previous) if r["case_id"] or previous else "Строка без связи",
            "Основание": r["reason"], "Дата выписки": day,
            "Номер начисления": r["charge_id"], "Строка выписки": r["line"],
            "Этап на момент сверки": r.get("stage", ""), "Редакция": r.get("version"),
            "Начислено, ₽": r.get("due"), "В карточке, ₽": r.get("card_paid"),
            "В выписке, ₽": r["external"]}


def payment_panel(cases, profile):
    st.subheader("Сверка поступлений с начислениями")
    st.caption("Загрузите полный накопительный срез бухгалтерии за одну дату. Одна строка соответствует одному начислению по делу. Идентификаторы в Excel указывайте текстом.")
    st.download_button("Шаблон платёжной выписки CSV", csv_bytes([], payments.COLUMNS), "payments.csv", "text/csv")
    file = st.file_uploader("Выписка бухгалтерии", type=["csv", "xlsx"], key="payment_upload")
    if file is not None:
        try:
            day, records = payments.parse(file, file.name)
            preview = payments.reconcile(cases, profile, day, records)
            st.caption(f"Предварительная сверка на {date_text(day)} · строк файла {len(records)}")
            st.dataframe([_view(r, day) for r in preview], hide_index=True, width="stretch")
            st.button("Сохранить сверку", key="pay_save", on_click=_commit,
                      args=(payments.save, (day, records, file.name, preview, profile),
                            "Сверка сохранена. Итоги доступны в журнале."))
        except (ValueError, OSError, SQLiteError) as exc:
            st.error(literal(exc))
    journal = payments.batches()
    if not journal:
        st.info("После сохранения здесь появятся итоги и список уточнений для бухгалтерии.")
        return
    st.subheader("Журнал платёжных сверок")
    selected = st.selectbox("Дата сохранённой выписки", range(len(journal)),
                            format_func=lambda i: date_text(journal[i]["as_of"]) + " · " + journal[i]["source"])
    batch = journal[selected]
    prior = {}
    previous_batch = selected + 1 < len(journal)
    if previous_batch:
        older = journal[selected + 1]
        previous_rows = payments.results(older["id"])
        counts = Counter(_match_key(r) for r in previous_rows)
        prior = {_match_key(r): r for r in previous_rows if counts[_match_key(r)] == 1}
        st.caption("Сравнение с выпиской на " + date_text(older["as_of"]))
    rows = payments.results(batch["id"])
    counts = Counter(_match_key(r) for r in rows)
    columns = st.columns(3)
    for col, label in zip(columns, (payments.OK, payments.DIFF, payments.CHECK)):
        col.metric(label, sum(r["status"] == label for r in rows))
    status = st.selectbox("Результат сверки", ["Все результаты", payments.OK, payments.DIFF, payments.CHECK])
    changed = st.checkbox("Только изменения к предыдущему срезу", disabled=not previous_batch)
    table, ids = [], []
    for r in rows:
        key = _match_key(r)
        view = _view(r, batch["as_of"], prior.get(key) if counts[key] == 1 else None)
        if status != "Все результаты" and r["status"] != status:
            continue
        if changed and previous_batch and view["Динамика"] == "Без изменений":
            continue
        table.append(view)
        if r["case_id"]:
            ids.append(r["case_id"])
    if table:
        st.dataframe(table, hide_index=True, width="stretch")
        if ids:
            chosen = st.selectbox("Открыть сопоставленное дело", ids, format_func=case_label)
            st.button("К карточке из сверки", on_click=route, args=("Анализ и проверка", chosen))
    else:
        st.caption("По выбранным условиям строк нет.")
    if previous_batch:
        gone = [r for r in previous_rows if _match_key(r) not in counts]
        if gone:
            with st.expander("Строки, которые были только в предыдущем срезе"):
                st.dataframe([_view(r, older["as_of"]) for r in gone], hide_index=True)
    issues = [{**_view(r, batch["as_of"], prior.get(_match_key(r)) if counts[_match_key(r)] == 1 else None),
               "Текст запроса": payment_request(r)} for r in rows if r["status"] != payments.OK]
    if issues:
        st.download_button("Скачать уточнения для бухгалтерии", xlsx_bytes([("Уточнения", issues, list(issues[0]))]),
                           "sverka_" + batch["as_of"] + ".xlsx", XLSX_MIME)


def act_panel(cases, profile):
    st.subheader("Сведения из актов обследования")
    st.caption("Загрузите опись с подписанного акта. Код участка должен относиться к тому же справочнику, что и в билете. Сопоставляется один акт на дело.")
    st.download_button("Шаблон описи актов CSV", csv_bytes([], acts.COLUMNS), "acts.csv", "text/csv")
    file = st.file_uploader("Опись актов обследования", type=["csv", "xlsx"], key="act_upload")
    if file is None:
        return
    try:
        plan = acts.preview(file, file.name, cases)
        st.dataframe([{"Дело": cid, "Редакция": version, **{FIELDS[k]: v for k, v in fields.items()}}
                      for cid, version, fields in plan], hide_index=True)
        st.button("Применить сведения из актов", on_click=_commit,
                  args=(acts.save, (plan, file.name, profile), "Сведения из актов сохранены. Изменено дел: {result}."))
    except (ValueError, OSError, SQLiteError) as exc:
        st.error(literal(exc))


def case_panel(case, profile):
    matches, batch = payments.latest_by_case()
    match = matches.get(case["case_id"])
    st.write("Этап выдачи · " + STAGES[stage(case)])
    if match:
        st.markdown("**Сверка с бухгалтерией**")
        st.caption("Срез на " + date_text(batch["as_of"]) + " · " + literal(batch["source"]))
        st.write(match["status"] + ". " + match["reason"])
        amounts = {"Начислено на момент сверки": match.get("due"), "Поступило по выписке": match["external"],
                   "Сейчас в карточке": case.get("paid_amount")}
        for col, (label, value) in zip(st.columns(3), amounts.items()):
            shown = "Нет сведений" if value is None else f"{value:,.2f} ₽".replace(",", " ").replace(".", ",")
            col.metric(label, shown)
        st.caption("Номер начисления · " + literal(match["charge_id"] or "не указан") +
                   " · строка выписки " + (match["line"] or "отсутствует"))
        if match.get("applied_version") == case["_version"]:
            st.caption(f"Сумма этой выписки перенесена в редакцию {case['_version']}.")
        elif match["version"] != case["_version"]:
            st.caption(f"Сверка относится к редакции {match['version']}. Для нового сопоставления загрузите следующий срез.")
        elif match["transferable"] and match["external"] != case.get("paid_amount"):
            st.button("Перенести сумму из выписки в новую редакцию", key="apply_payment", on_click=_commit,
                      args=(payments.apply, (batch["id"], case["case_id"], profile),
                            "Сумма перенесена. Создана редакция, результаты проверок пересчитаны."))
    st.markdown("**Акт обследования и билет**")
    st.dataframe(document_check(case), hide_index=True, width="stretch")
    st.caption("Сравниваются реквизиты одного акта и одного билета. При различии специалист сверяет оригиналы и допустимый объём работ.")
    rows = tasks(case, profile, _current_payment(case, matches))
    if rows:
        st.markdown("**Подготовить служебный запрос**")
        owners = list(dict.fromkeys(r["Кому"] for r in rows))
        owner = st.selectbox("Кому адресован запрос", owners, key="request_owner_" + case["case_id"])
        body = "\n".join(r["Текст запроса"] for r in rows if r["Кому"] == owner)
        st.text(body)
        st.download_button("Скачать проект запроса TXT", owner + "\n" + body,
                           "zapros_" + filename_id(case["case_id"]) + ".txt", "text/plain")
    else:
        st.caption("По заполненным сведениям задач для уточнения нет.")


def document_fields(values, prefix):
    with st.expander("Реквизиты акта и билета"):
        left, right = st.columns(2)
        for _, a, p in PAIRS:
            for column, key in ((left, a), (right, p)):
                stored = values.get(key, "")
                values[key] = column.text_input(FIELDS[key], str(stored), key=prefix + key, max_chars=100)


def report_sheets(case, profile):
    matches, batch = payments.latest_by_case()
    match = matches.get(case["case_id"])
    checks = document_check(case)
    sheets = [("Акт и билет", checks, list(checks[0])),
              ("Рабочие задачи", tasks(case, profile, _current_payment(case, matches)), TASK_COLUMNS)]
    if match:
        row = _view(match, batch["as_of"])
        row["Файл выписки"] = batch["source"]
        sheets.append(("Сверка поступлений", [row], list(row)))
    return sheets

