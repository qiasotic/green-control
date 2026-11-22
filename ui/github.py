"""Проверка, подтверждение и повторная загрузка данных GitHub."""
from io import BytesIO
import json
from sqlite3 import Error as SQLiteError

import pandas as pd
import streamlit as st

from core import github_connector as connector, storage
from core.importer import mapped_rows, read_frame
from core.presentation import FIELD_LABELS, case_label, field_value, timestamp
from ui.components import literal, route
from ui.training import hint


def _receive(file, previous_mapping=None):
    received = connector.download(file)
    frame = read_frame(BytesIO(received.content), file.source.path)
    st.session_state["gh_dataset"] = (received, frame, previous_mapping or {})
    st.session_state["gh_revision"] = st.session_state.get("gh_revision", 0) + 1
    st.session_state.pop("gh_verified", None)
    st.session_state.pop("gh_loaded", None)


def _mapping(frame, saved, revision):
    columns = list(frame.columns)
    folded = {str(column).strip().casefold(): column for column in columns}
    selected = {}
    with st.expander("Сопоставление колонок", expanded=False):
        hint("github_mapping")
        st.caption("Номер и категория обязательны. При обновлении дела несопоставленные поля сохраняются. "
                   "Значения должны соответствовать выбранным полям.")
        panels = st.columns(2)
        for index, (field, title) in enumerate(FIELD_LABELS.items()):
            best = saved.get(field) if saved.get(field) in columns else folded.get(field, folded.get(title.casefold()))
            value = panels[index % 2].selectbox(
                title + (" *" if field in ("case_id", "case_type") else ""),
                [None, *columns], index=columns.index(best) + 1 if best in columns else 0,
                format_func=lambda name: "Не сопоставлено" if name is None else str(name),
                key=f"gh_map_{revision}_{field}",
            )
            if value is not None:
                selected[field] = value
    # Column labels from pandas are strings or numbers. Record a serializable label for the source history.
    return selected


def _sample(frame, mapping, profile):
    view = frame.head(10).copy()
    for field, column in mapping.items():
        if column in view and field in ("case_id", "application_date", "issued_date", "predecision_date"):
            # An unfamiliar raw value stays visible during mapping.
            def present(value):
                if field in ("application_date", "issued_date", "predecision_date") and isinstance(value, str):
                    if len(value) == 10 and value[2] == "." and value[5] == ".":
                        return value
                shown = field_value(field, value, profile)
                return value if shown == "Дата не распознана" else shown
            view[column] = view[column].map(present)
    view = view.rename(columns={column: FIELD_LABELS[field] for field, column in mapping.items()})
    return view.loc[:, ~view.columns.duplicated()].astype(str)


def _preview(profile, history):
    dataset = st.session_state.get("gh_dataset")
    if dataset is None:
        return
    download, frame, saved = dataset
    path = download.file.source.path
    metadata = download.metadata()
    revision = st.session_state["gh_revision"]
    st.subheader("3. Предварительная проверка")
    hint("github_preview")
    st.text(path)
    st.caption(f"Размер: {download.file.size:,} байт · Записей: {len(frame)} · Колонок: {len(frame.columns)}")
    mapping = _mapping(frame, saved, revision)
    st.write("Первые 10 записей")
    st.dataframe(_sample(frame, mapping, profile), hide_index=True, width="stretch")
    checked = st.session_state.get("gh_verified")
    signature = (revision, tuple(mapping.items()))
    already = any(entry["repository"] == metadata["repository"] and entry["path"] == metadata["path"]
                  and entry["ref"] == metadata["ref"] and entry["digest"] == metadata["digest"] for entry in history)
    if already:
        st.info("Эта версия файла уже была обработана. Повторный импорт не нужен.")
    if st.button("Проверить данные", key="gh_check"):
        try:
            records, errors = mapped_rows(frame, mapping, profile)
            plan = storage.preview_github_import(records, profile) if records else ()
            checked = (signature, records, errors, plan)
            st.session_state["gh_verified"] = checked
        except (ValueError, SQLiteError) as exc:
            st.session_state.pop("gh_verified", None)
            checked = None
            st.error(literal(exc))
    if checked and checked[0] == signature:
        _, records, errors, plan = checked
        new = sum(kind == "new" for _, _, kind, _ in plan)
        same = sum(kind == "same" for _, _, kind, _ in plan)
        update = sum(kind == "update" for _, _, kind, _ in plan)
        st.write(f"Новых: {new} · Совпадают: {same} · Требуют обновления: {update} · Ошибочных: {len(errors)}.")
        if errors:
            st.error("Исправьте ошибочные строки и получите файл повторно. Этот пакет не сохранён.")
            st.dataframe(pd.DataFrame(errors, columns=["Строка", "Причина"]), hide_index=True, width="stretch")
        differences = [{"Дело": case_label(case_id), "Итог": "Добавить" if kind == "new" else "Новая редакция",
                        "Поля": ", ".join(FIELD_LABELS[field] for field in fields) or "Новое дело"}
                       for case_id, _, kind, fields in plan if kind != "same"]
        if differences:
            with st.expander("Что будет добавлено или изменено"):
                st.dataframe(pd.DataFrame(differences), hide_index=True, width="stretch", height=250)
        st.subheader("4. Импорт")
        hint("github_confirm")
        confirm = (
            st.checkbox("Подтверждаю обновление существующих дел с сохранением прежних редакций",
                        key=f"gh_confirm_{revision}_{hash(signature)}")
            if update and not errors and not already else False
        )
        if st.button("Импортировать данные", type="primary",
                     disabled=not records or bool(errors) or already or bool(update and not confirm), key="gh_save"):
            try:
                result = storage.save_github_import(records, metadata, mapping, plan, profile, update_existing=confirm)
            except (ValueError, SQLiteError) as exc:
                st.session_state.pop("gh_verified", None)
                st.error(literal(exc))
            else:
                first_id = next((row[0] for row in plan if row[2] != "same"), records[0]["case_id"])
                st.session_state["gh_loaded"] = (result, first_id, len(records))
                st.rerun()
    loaded = st.session_state.get("gh_loaded")
    if loaded:
        counts, first_id, total = loaded
        st.success(f"Получено: {total} · Сохранено: {counts['new'] + counts['update']} "
                   f"· Пропущено: {counts['same']} · Требуют исправления: 0.")
        st.subheader("5. Анализ")
        st.button("Перейти к анализу", on_click=route, args=("Анализ и проверка", first_id))
        st.button("Открыть реестр дел", on_click=route, args=("Реестр дел",))


def _updates(history):
    if not history:
        return
    with st.expander("История источников и обновления"):
        hint("github_history")
        recent = {}
        for row in history:
            recent.setdefault((row["repository"], row["path"], row["ref"]), row)
        selection = st.selectbox(
            "Ранее подключённый файл", list(recent),
            format_func=lambda item: f"{item[0]} · {item[1]} · {item[2]}",
        )
        entry = recent[selection]
        st.dataframe(pd.DataFrame([
            {"Получен (МСК)": timestamp(item["received_at"]), "Импортирован (МСК)": timestamp(item["imported_at"]),
             "Версия файла": item["blob_sha"], "Контрольная сумма": item["digest"],
             "Добавлено": item["added"], "Обновлено": item["updated"], "Пропущено": item["skipped"]}
            for item in history if (item["repository"], item["path"], item["ref"]) == selection
        ]), hide_index=True, width="stretch")
        if st.button("Проверить обновления"):
            try:
                root = connector.parse_address(entry["repository"])
                file = connector.file_info(connector.Source(root.owner, root.repository, entry["ref"], entry["path"]))
                if file.sha == entry["blob_sha"]:
                    st.info("Файл не изменился. Новых редакций нет.")
                    return
                st.session_state.pop("gh_listing", None)
                _receive(file, json.loads(entry["mapping"]))
                st.session_state["gh_notice"] = "Найдена новая версия. Проверьте изменения перед импортом."
                st.rerun()
            except (ValueError, OSError) as exc:
                st.error(literal(exc))


def panel(profile):
    st.subheader("1. Подключение")
    hint("github_connect")
    st.caption("Подключение → Выбор данных → Предварительная проверка → Импорт → Анализ")
    address = st.text_input("Адрес репозитория", placeholder="https://github.com/владелец/репозиторий",
                            help="Можно вставить прямую HTTPS-ссылку на CSV или XLSX. Если имя ветки содержит косую черту, используйте постоянную ссылку на коммит.")
    if st.button("Проверить подключение", key="gh_connect", disabled=not address):
        for key in ("gh_listing", "gh_dataset", "gh_loaded", "gh_verified"):
            st.session_state.pop(key, None)
        try:
            source = connector.parse_address(address)
            with st.spinner("Проверяем доступность GitHub…"):
                files, incomplete = connector.list_files(source)
            st.session_state["gh_listing"] = (address.strip(), files, incomplete)
        except (ValueError, OSError) as exc:
            st.error(literal(exc))
    listing = st.session_state.get("gh_listing")
    file = None
    if listing and listing[0] == address.strip():
        _, files, incomplete = listing
        st.subheader("2. Выбор данных")
        hint("github_select")
        if incomplete:
            st.warning("GitHub вернул неполный список. Нужный файл можно открыть по прямой ссылке.")
        if files:
            st.caption(f"Найдено файлов: {len(files)}.")
        if len(files) > 50:
            search = st.text_input("Поиск по пути файла", key="gh_file_search")
            files = [candidate for candidate in files if search.casefold().strip() in candidate.source.path.casefold()]
        if files:
            file = st.selectbox("Файл данных", files, format_func=lambda item: f"{item.source.path} · {item.size:,} байт",
                                key="gh_file")
            if st.button("Предпросмотр выбранного файла"):
                try:
                    with st.spinner("Читаем файл…"):
                        _receive(file)
                except (ValueError, OSError) as exc:
                    st.error(literal(exc))
        else:
            st.info("Под условия поиска не попали доступные файлы CSV или XLSX.")
    notice = st.session_state.pop("gh_notice", None)
    if notice:
        st.info(notice)
    history = storage.github_import_history()
    current = st.session_state.get("gh_dataset")
    if not listing or (file is not None and current and current[0].file == file):
        _preview(profile, history)
    _updates(history)
