"""Генератор учебных файлов для муниципальной сверки (формат docs/MUNICIPAL_DATA.md).

Создаёт четыре файла обмена и отдельный файл ожидаемых результатов:
  01_cases.csv      реестр дел (12 прежних колонок + permit_stage, charge_id, permit_*)
  02_acts.csv       опись актов (одна строка на дело)
  03_payments.csv   накопительная выписка на первую дату
  04_payments.csv   накопительная выписка на вторую дату
  expected.csv      ожидаемые результаты, заданные сценариями ДО запуска проверок

Запуск из корня проекта:
  .\\.venv\\Scripts\\python.exe -m scripts.generate_municipal --count 20 --seed 20261122 --output data/municipal_demo
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from pathlib import Path
import random

PREFIX = "МУН-"  # кириллица, как в учебных примерах
FIRST_STATEMENT = date(2026, 11, 20)
SECOND_STATEMENT = date(2026, 11, 21)
UNKNOWN_CHARGE = "0009999"  # номер начисления, которого нет в реестре

OK, DIFF, CHECK, NONE = "Оплата подтверждена", "Есть расхождение", "Нужна сверка", "нет записи"

CASE_FIELDS = [
    "case_id", "case_type", "application_date", "issued_date", "predecision_date", "documents",
    "documents_complete", "accrued_amount", "paid_amount", "exemption_code", "exemption_proof", "comment",
    "permit_stage", "charge_id", "permit_act_number", "permit_site", "permit_trees",
]
ACT_FIELDS = ["case_id", "act_number", "act_site", "act_trees"]
PAY_FIELDS = ["as_of", "case_id", "charge_id", "paid_total"]
EXPECTED_FIELDS = ["case_id", "scenario", "payment_1", "payment_2", "act_vs_permit"]

# code, title, case_type, stage, accrued, (paid_total на 1-ю дату, на 2-ю дату), act_mode, ожидаемые статусы
# act_mode: same — акт совпадает с проектом, diff — число деревьев различается, none — описи нет
SCENARIOS = (
    ("partial_then_full", "Частичная оплата, затем полная", "standard", "pre_issue", 1500, (700, 1500), "same", (DIFF, OK)),
    ("partial_act_diff", "Недоплата, затем оплата; в акте меньше деревьев", "standard", "pre_issue", 2500, (1200, 2500), "diff", (DIFF, OK)),
    ("paid_in_full", "Выданный билет, оплата полная", "standard", "issued", 1000, (1000, 1000), "same", (OK, OK)),
    ("still_partial", "Недоплата сохраняется на втором срезе", "standard", "issued", 1000, (600, 600), "same", (DIFF, DIFF)),
    ("overpaid", "Второй срез показывает переплату", "standard", "pre_issue", 1000, (1000, 1200), "same", (OK, DIFF)),
    ("no_statement_row", "Строки в выписке нет", "standard", "pre_issue", 700, (None, None), "none", (CHECK, CHECK)),
    ("exempt_category", "Освобождённая категория, оплата не требуется", "emergency", "pre_issue", None, (None, None), "none", (NONE, NONE)),
    ("unknown_accrual", "Начисление неизвестно, платежи есть", "standard", "pre_issue", None, (500, 500), "same", (CHECK, CHECK)),
    ("zero_then_paid", "Явный ноль в первой выписке", "standard", "issued", 800, ("0.00", 800), "diff", (DIFF, OK)),
)


def _money(value):
    return "" if value is None else (value if isinstance(value, str) else f"{value:.2f}")


def build(count: int, seed: int):
    rng = random.Random(seed)
    cases, acts, first, second, expected = [], [], [], [], []
    for index in range(count):
        code, title, case_type, stage, accrued, totals, act_mode, statuses = SCENARIOS[index % len(SCENARIOS)]
        case_id = f"{PREFIX}{index + 1:03d}"
        charge_id = "" if case_type == "emergency" else f"{901 + index:07d}"
        applied = date(2026, 11, 10) + timedelta(days=rng.randint(0, 6))
        decided = applied + timedelta(days=rng.randint(1, 3))
        issued = decided + timedelta(days=rng.randint(1, 2)) if stage == "issued" else None
        trees = rng.randint(3, 30)
        site = f"УЧ-{rng.randint(1, 12):02d}"
        act_number = f"А-{18 + index}"
        cases.append({
            "case_id": case_id, "case_type": case_type, "application_date": applied.isoformat(),
            "issued_date": issued.isoformat() if issued else "",
            "predecision_date": "" if case_type == "emergency" else decided.isoformat(),
            "documents": "Заявление|Акт обследования", "documents_complete": "yes",
            "accrued_amount": _money(accrued), "paid_amount": "", "exemption_code": "",
            "exemption_proof": "unknown", "comment": f"Синтетический сценарий · {title}",
            "permit_stage": stage, "charge_id": charge_id,
            "permit_act_number": act_number if act_mode != "none" else "",
            "permit_site": site if act_mode != "none" else "",
            "permit_trees": trees if act_mode != "none" else "",
        })
        if act_mode != "none":
            acts.append({"case_id": case_id, "act_number": act_number, "act_site": site,
                         "act_trees": trees if act_mode == "same" else trees - rng.randint(1, 3)})
        for rows, total, day in ((first, totals[0], FIRST_STATEMENT), (second, totals[1], SECOND_STATEMENT)):
            if total is None and code not in ("unknown_accrual",):
                continue
            # половина строк связывается только по номеру начисления, остальные ещё и по номеру дела
            rows.append({"as_of": day.isoformat(), "case_id": case_id if index % 2 else "",
                         "charge_id": charge_id, "paid_total": _money(total)})
        expected.append({"case_id": case_id, "scenario": code, "payment_1": statuses[0], "payment_2": statuses[1],
                         "act_vs_permit": {"same": "совпадает", "diff": "расхождение", "none": "нет описи"}[act_mode]})
    # строка выписки по начислению, которого нет в реестре: должна дать задачу на сверку
    for rows, day in ((first, FIRST_STATEMENT), (second, SECOND_STATEMENT)):
        rows.append({"as_of": day.isoformat(), "case_id": "", "charge_id": UNKNOWN_CHARGE, "paid_total": "150.00"})
    expected.append({"case_id": f"(нет дела, начисление {UNKNOWN_CHARGE})", "scenario": "unknown_charge",
                     "payment_1": CHECK, "payment_2": CHECK, "act_vs_permit": "нет описи"})
    return cases, acts, first, second, expected


def _write(path: Path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=20, help="число дел (1-5000)")
    parser.add_argument("--seed", type=int, default=20261122)
    parser.add_argument("--output", type=Path, default=Path("data/municipal_demo"))
    args = parser.parse_args()
    if not 1 <= args.count <= 4999:
        parser.error("Число дел должно быть от 1 до 4999.")
    cases, acts, first, second, expected = build(args.count, args.seed)
    args.output.mkdir(parents=True, exist_ok=True)
    _write(args.output / "01_cases.csv", cases, CASE_FIELDS)
    _write(args.output / "02_acts.csv", acts, ACT_FIELDS)
    _write(args.output / "03_payments.csv", first, PAY_FIELDS)
    _write(args.output / "04_payments.csv", second, PAY_FIELDS)
    _write(args.output / "expected.csv", expected, EXPECTED_FIELDS)
    print(f"Сформировано дел: {len(cases)} · актов: {len(acts)} · строк выписки: {len(first)}+{len(second)} · {args.output}")


if __name__ == "__main__":
    main()
