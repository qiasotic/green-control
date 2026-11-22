"""Агрегация дел и переиспользуемый сводный отчёт."""
from collections import Counter
from datetime import date
from html import escape

from core.presentation import STATUS_LABELS, profile_label, public_row
from core.reports import csv_bytes
from core.storage import WORKFLOW

RULES = ("R1", "R2", "R3")
STATUSES = ("FLAG", "NEED_DATA", "PASS", "NOT_APPLICABLE")
LIMITATION = ("Профиль модельный. Результаты относятся к отдельным условиям предварительной проверки. "
              "Недостаточность сведений указывает на пропуски в исходных данных. Итоговую оценку оформляет специалист.")


def priority(entry):
    case, row = entry
    return (case["_state"] == "closed", case["_draft"],
            -row["Сигналы"], -row["Нехватка сведений"], case["case_id"].casefold())


def summarize(entries):
    metrics = {
        "Всего дел": len(entries),
        "Дела с выявленными отклонениями": sum(row["Сигналы"] > 0 for _, row in entries),
        "Дела, требующие уточнения": sum(row["Нехватка сведений"] > 0 for _, row in entries),
        "Завершены специалистом": sum(case["_state"] == "closed" for case, _ in entries),
    }
    states = Counter(case["_state"] for case, _ in entries)
    frequency = []
    for rule in RULES:
        counts = Counter(row[rule] for _, row in entries)
        frequency.append({"Индикатор": rule, **{status: counts[status] for status in STATUSES},
                          "Без расчёта": len(entries) - sum(counts[status] for status in STATUSES)})
    months = Counter()
    undated = 0
    for case, _ in entries:
        try:
            iso = date.fromisoformat(case.get("application_date", "")).isoformat()
        except (ValueError, TypeError):
            undated += 1
        else:
            months[iso[:7]] += 1
    queue = [
        {"Дело": case["case_id"], "Категория": row["Категория"], "Состояние": row["Состояние дела"],
         "Дата заявления": row["Дата заявления"], "Источник": row["Источник"],
         "FLAG": row["Сигналы"], **{rule: row[rule] for rule in RULES}}
        for case, row in sorted(entries, key=priority)
        if case["_state"] != "closed" and not case["_draft"] and row["Сигналы"]
    ]
    return {
        "metrics": metrics, "states": [{"Состояние": label, "Дел": states[status]}
                                       for status, label in WORKFLOW.items()],
        "rules": frequency, "months": [{"Месяц заявления": month, "Дел": months[month]} for month in sorted(months)],
        "undated": undated, "queue": queue,
        "drafts": sum(case["_draft"] for case, _ in entries),
    }


def report_sheets(summary, filters, profile, generated_at):
    """Все форматы строятся из одинаковых строк и параметров."""
    meta = {
        "Сформирован (МСК)": generated_at,
        "Профиль": profile_label(profile["version"]) + " · модельный",
        **filters,
        "Ограничения": LIMITATION,
        "Без корректной даты заявления": summary["undated"],
        "Из них черновиков в выборке": summary["drafts"],
    }
    return [
        ("Параметры", [{"Показатель": key, "Значение": value} for key, value in meta.items()],
         ("Показатель", "Значение")),
        ("Показатели", [{"Показатель": key, "Дел": value} for key, value in summary["metrics"].items()],
         ("Показатель", "Дел")),
        ("Рабочие состояния", summary["states"], ("Состояние", "Дел")),
        ("Распределение проверок", [public_row(row) for row in summary["rules"]],
         ("Индикатор", *(STATUS_LABELS[key] for key in STATUSES), "Без расчёта")),
        ("По месяцам", [public_row(row) for row in summary["months"]], ("Месяц заявления", "Дел")),
        ("Приоритетная очередь", [public_row(row) for row in summary["queue"]],
         tuple(public_row(dict.fromkeys(("Дело", "Категория", "Состояние", "Дата заявления", "Источник", "FLAG", *RULES), "")))),
    ]


def summary_csv(sheets):
    fields = ("Раздел", "Запись", "Поле", "Значение")
    rows = [
        dict(zip(fields, (section, str(row[columns[0]]), column, row[column])))
        for section, values, columns in sheets for row in values for column in columns[1:]
    ]
    return csv_bytes(rows, fields)


def summary_html(sheets):
    parts = [
        '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Сводный отчёт — Зелёный контроль</title>',
        '<style>body{font:15px system-ui;color:#244562;margin:2rem}table{border-collapse:collapse;width:100%}',
        'td,th{border:1px solid #c6dcef;padding:.4em;text-align:left}th{background:#e7f0f9}thead{display:table-header-group}',
        'h2{break-after:avoid}tr{break-inside:avoid}@media print{body{margin:0;font-size:10pt}}</style>',
        '<h1>Зелёный контроль · сводный отчёт</h1>',
    ]
    for name, rows, columns in sheets:
        parts.append(f"<h2>{escape(name)}</h2><table><thead><tr>")
        parts.extend(f"<th>{escape(column)}</th>" for column in columns)
        parts.append("</tr></thead><tbody>")
        for row in rows:
            parts.append("<tr>" + "".join(f"<td>{escape(str(row[column]))}</td>" for column in columns) + "</tr>")
        parts.append("</tbody></table>")
    return "".join(parts).encode("utf-8")
