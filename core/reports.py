"""Экспорт реестра, заключений и истории без привязки к Streamlit."""
from __future__ import annotations

import csv
from io import BytesIO, StringIO
import re

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils import get_column_letter

from core.presentation import (
    FIELD_LABELS, RULE_LABELS, STATUS_LABELS, case_comment, evidence_text, field_value, model_text, profile_label,
    source_label, timestamp,
)

from core.municipal import FIELDS, STAGES


def card_fields(case, profile):
    fields = {label: field_value(field, case_comment(case) if field == "comment" else case.get(field), profile) for field, label in FIELD_LABELS.items()
            if field in case or field not in ("finance_mode", "payment_note")}
    for key, label in FIELDS.items():
        if key in case:
            fields[label] = STAGES.get(case[key], case[key]) if key == "permit_stage" else case[key]
    return fields


def filename_id(case_id):
    cleaned = re.sub(r"[^\w-]", "_", case_id)[:80]
    return cleaned or "дело"


def _csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def csv_bytes(rows, columns):
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(columns)
    writer.writerows([_csv_cell(row.get(column, "")) for column in columns] for row in rows)
    return buffer.getvalue().encode("utf-8-sig")


def xlsx_bytes(sheets):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows, columns in sheets:
        sheet = workbook.create_sheet(name)
        sheet.append(list(columns))
        widths = [len(title) for title in columns]
        for row_index, row in enumerate(rows, start=2):
            for column_index, column in enumerate(columns, start=1):
                value = row.get(column)
                if isinstance(value, str):
                    value = ILLEGAL_CHARACTERS_RE.sub("", value)
                cell = sheet.cell(row_index, column_index, value)
                if isinstance(value, str):
                    cell.data_type = "s"
                widths[column_index - 1] = max(widths[column_index - 1], len(str(value or "")))
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for index, size in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = min(max(size + 3, 15), 72)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


HISTORY_LABELS = {
    "version": "Редакция", "occurred_at": "Время (МСК)", "analyzed_at": "Расчёт (МСК)",
    "reviewed_at": "Заключение (МСК)", "event": "Событие", "actor": "Инициатор",
    "source": "Источник", "is_draft": "Черновик", "from_state": "Прежнее состояние",
    "to_state": "Новое состояние", "note": "Обоснование", "decision": "Заключение",
    "profile_version": "Версия профиля", "profile_status": "Статус профиля",
    "rule": "Проверка", "status": "Результат", "title": "Вывод", "evidence": "Исходные сведения",
    "explanation": "Объяснение", "condition": "Условие", "limitation": "Ограничения",
    "plan": "Рекомендации", "action": "Действие",
}

def _history_values(entry, keys):
    from core.storage import WORKFLOW

    labels = {
        "rule": RULE_LABELS, "status": STATUS_LABELS, "from_state": WORKFLOW,
        "to_state": WORKFLOW, "is_draft": {0: "Нет", 1: "Да"},
    }
    result = {}
    for key in keys:
        value = entry.get(key)
        if key in labels:
            value = labels[key].get(value, "—")
        elif key.endswith("_at"):
            value = timestamp(value)
        elif key == "source":
            value = source_label(value)
        elif key in ("title", "condition", "limitation", "explanation", "plan", "event") and value:
            value = model_text(value)
        result[HISTORY_LABELS[key]] = value if value is not None else "—"
    return result


def history_sheets(history, profile):
    history_fields = {
        "Редакции": ("versions", ("version", "occurred_at", "event", "actor", "source", "is_draft")),
        "Переходы": ("workflow", ("version", "occurred_at", "from_state", "to_state", "note", "actor")),
        "Заключения": ("reviews", ("version", "reviewed_at", "decision", "note", "actor")),
        "Повторные расчёты": ("rechecks", ("version", "profile_version", "occurred_at", "actor")),
        "Действия": ("actions", ("version", "occurred_at", "profile_version", "rule", "action", "note", "actor")),
    }
    sheets = [(title, [_history_values(entry, keys) for entry in history[table]],
               [HISTORY_LABELS[key] for key in keys]) for title, (table, keys) in history_fields.items()]
    by_version = {snapshot["version"]: snapshot["payload"] for snapshot in history["versions"]}
    snapshots = [{"Редакция": version, "Поле": name, "Значение": value}
                 for version, payload in by_version.items()
                 for name, value in card_fields(payload, profile).items()]
    sheets.append(("Снимки карточки", snapshots, ["Редакция", "Поле", "Значение"]))
    calculations = []
    keys = ("version", "profile_version", "profile_status", "analyzed_at", "rule", "status", "title",
            "evidence", "explanation", "condition", "limitation", "plan")
    for calculation in history["analyses"]:
        plans = {item["rule"]: " ".join(item["steps"]) for item in calculation["plan"]}
        for result in calculation["outcomes"]:
            calculations.append(_history_values({
                **calculation, **result, "evidence": evidence_text(result, by_version[calculation["version"]], profile),
                "plan": plans.get(result["rule"], ""),
            }, keys))
    sheets.append(("Расчёты проверок", calculations, [HISTORY_LABELS[key] for key in keys]))
    return sheets


def conclusion_text(case, profile, history):
    from core.guidance import format_review_sheet

    run = case["_analysis"]
    lines = ["ЗЕЛЁНЫЙ КОНТРОЛЬ · ИНДИВИДУАЛЬНОЕ ЗАКЛЮЧЕНИЕ", ""]
    lines += [f"{label}: {value}" for label, value in card_fields(case, profile).items()]
    lines += [f"Редакция: {case['_version']}", f"Источник: {source_label(case['_source'])}", ""]
    if run:
        lines += [f"Расчёт (МСК): {timestamp(run['analyzed_at'])}",
                  format_review_sheet(case, profile, run["outcomes"], include_placeholder=False).rstrip()]
    else:
        lines.append("Анализ по действующему профилю отсутствует.")
    review = history["current_review"]
    lines += ["", "ЗАКЛЮЧЕНИЕ СПЕЦИАЛИСТА", review["decision"], review["note"],
              timestamp(review["reviewed_at"]), "", "ИСТОРИЯ ДЕЛА",
              "Модельный профиль применяется для предварительной проверки. Окончательную оценку даёт специалист."]
    for title, entries, _ in history_sheets(history, profile):
        lines += ["", title]
        lines.extend(" · ".join(f"{key}: {value}" for key, value in entry.items()) for entry in entries)
    return ("\n".join(lines) + "\n").encode("utf-8-sig")
