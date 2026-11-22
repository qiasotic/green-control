"""Воспроизводимый набор заранее размеченных модельных сценариев."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, timedelta
import json
from pathlib import Path
import random
from collections import Counter

from core.models import CASE_COLUMNS, OPTIONAL_CASE_COLUMNS
from core.reports import xlsx_bytes

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "synthetic"
GENERATOR_VERSION = "1.0"
REFERENCE_PROFILE = {"id": "DEMO-2026-01", "version": "1.0"}
DEFAULT_SEED = 20261120
REFERENCE_DATE = date(2026, 11, 20)
P, F, U, N = "PASS", "FLAG", "NEED_DATA", "NOT_APPLICABLE"
FIELDS = [*CASE_COLUMNS, *OPTIONAL_CASE_COLUMNS]


@dataclass(frozen=True)
class Scenario:
    code: str
    title: str
    expected: tuple[str, str, str]
    changes: dict


# The expectations are authored before any rule execution and travel with the scenario definitions.
SCENARIOS = (
    Scenario("paid", "Полная оплата", (P, P, P), {}),
    Scenario("exempt_valid", "Освобождение подтверждено", (P, P, P),
             {"finance_mode": "exemption", "exemption_code": "DEMO_PUBLIC_USE", "exemption_proof": "yes"}),
    Scenario("underpaid", "Неполная оплата", (F, P, P), {"paid_amount": 100}),
    Scenario("exempt_no_proof", "Подтверждено отсутствие документа об освобождении", (F, P, P),
             {"finance_mode": "exemption", "exemption_code": "DEMO_PUBLIC_USE", "exemption_proof": "no"}),
    Scenario("exempt_unknown", "Наличие подтверждения неизвестно", (U, P, P),
             {"finance_mode": "exemption", "exemption_code": "DEMO_PUBLIC_USE"}),
    Scenario("missing_document", "В полном деле отсутствует акт", (P, F, P), {"documents": "Заявление"}),
    Scenario("unknown_documents", "Получена часть материалов", (P, U, P),
             {"documents": "Заявление", "documents_complete": "unknown"}),
    Scenario("wrong_order", "Разрешение выдано до предварительного решения", (P, P, F),
             {"predecision_offset": 5}),
    Scenario("emergency", "Аварийные работы с модельным исключением", (P, P, N),
             {"case_type": "emergency", "finance_mode": "unknown", "predecision_offset": None}),
    Scenario("r1_r2", "Неполная оплата и отсутствие акта", (F, F, P),
             {"paid_amount": 100, "documents": "Заявление"}),
    Scenario("r2_r3", "Отсутствие акта и расхождение дат", (P, F, F),
             {"documents": "Заявление", "predecision_offset": 5}),
    Scenario("r1_r3", "Неполная оплата и расхождение дат", (F, P, F),
             {"paid_amount": 100, "predecision_offset": 5}),
    Scenario("missing_payment", "Начисление известно, оплата неизвестна", (U, P, P), {"paid_amount": None}),
    Scenario("same_day", "Выдача и решение в один день", (P, P, P), {"predecision_offset": 4}),
    Scenario("overpayment", "Оплата выше начисления", (P, P, P), {"paid_amount": 3000}),
    Scenario("missing_date", "Дата предварительного решения неизвестна", (P, P, U),
             {"predecision_offset": None}),
    Scenario("all_unknown", "Финансы, комплектность и дата решения неизвестны", (U, U, U),
             {"finance_mode": "unknown", "documents": "", "documents_complete": "unknown",
              "predecision_offset": None}),
    Scenario("unknown_exemption_code", "Заявлено освобождение без кода", (U, P, P),
             {"finance_mode": "exemption"}),
    Scenario("confirmed_zero", "Подтверждённое нулевое начисление без освобождения", (F, P, P),
             {"accrued_amount": 0, "paid_amount": 0}),
    Scenario("emergency_partial", "Аварийные работы, комплектность неизвестна", (P, U, N),
             {"case_type": "emergency", "finance_mode": "unknown", "documents_complete": "unknown",
              "predecision_offset": None}),
)


def generate(seed: int, count: int, reference_date: date = REFERENCE_DATE):
    if not 1 <= count <= 5000 or seed < 0 or seed > 10**12:
        raise ValueError("Количество дел от 1 до 5000, seed от 0 до 1000000000000.")
    if reference_date < date(2, 1, 1):
        raise ValueError("Дата среза слишком ранняя.")
    rng = random.Random(seed)
    records, labels = [], []
    for index in range(count):
        scenario = SCENARIOS[index % len(SCENARIOS)]
        case_id = f"SYN-{seed}-{index + 1:04d}"
        start = reference_date - timedelta(days=rng.randrange(7, 245))
        amount = rng.randrange(65, 240) * 10
        changes = scenario.changes.copy()
        prior = changes.pop("predecision_offset", 3)
        row = {
            "case_id": case_id, "case_type": "standard", "application_date": start.isoformat(),
            "issued_date": (start + timedelta(days=4)).isoformat(),
            "predecision_date": (start + timedelta(days=prior)).isoformat() if prior is not None else "",
            "documents": "Заявление|Акт обследования", "documents_complete": "yes",
            "accrued_amount": amount, "paid_amount": amount, "exemption_code": "",
            "exemption_proof": "unknown", "finance_mode": "payment", "payment_note": "",
            "comment": "Синтетический сценарий · " + scenario.title,
            **changes,
        }
        if row["finance_mode"] != "payment":
            row.update(accrued_amount=None, paid_amount=None)
        records.append(row)
        labels.append({"case_id": case_id, "scenario": scenario.code,
                       **dict(zip(("R1", "R2", "R3"), scenario.expected))})
    return records, labels


def _write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def write_dataset(directory, seed, count, reference_date):
    records, labels = generate(seed, count, reference_date)
    directory.mkdir(parents=True, exist_ok=True)
    _write_csv(directory / "dataset.csv", records, FIELDS)
    (directory / "dataset.xlsx").write_bytes(xlsx_bytes([("Дела", records, FIELDS)]))
    _write_csv(directory / "ground_truth.csv", labels, ["case_id", "scenario", "R1", "R2", "R3"])
    # Negative fixture is deliberately excluded from the main cohort and reference.
    _write_csv(directory / "invalid_duplicate_id.csv", [records[0], records[0]], FIELDS)
    manifest = {
        "generator_version": GENERATOR_VERSION, "seed": seed, "count": count,
        "reference_date": reference_date.isoformat(), "reference_profile": REFERENCE_PROFILE,
        "source": "Полностью синтетические данные",
        "scenarios": dict(Counter(row["scenario"] for row in labels)),
    }
    (directory / "generation_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def create_demo_database(directory):
    from core import storage
    from core.importer import read_cases
    from core.models import load_profile

    path = directory / "green_control_demo.sqlite3"
    if path.resolve() == storage.DB_PATH.resolve():
        raise ValueError("Укажите отдельный каталог для демонстрационной базы.")
    # Refuse to touch any pre-existing database, including a previously used demo.
    with path.open("xb"):
        pass
    previous_path = storage.DB_PATH
    try:
        storage.DB_PATH = path
        profile = load_profile()
        records = read_cases(directory / "dataset.csv", "dataset.csv", profile)
        storage.initialize(profile)
        storage.save_cases(records, "Синтетика · набор 200" if len(records) == 200
                           else "Синтетика · демонстрационный набор", profile=profile)
    except Exception:
        path.unlink()
        raise
    finally:
        storage.DB_PATH = previous_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--reference-date", type=date.fromisoformat, default=REFERENCE_DATE)
    parser.add_argument("--output", type=Path, default=DATA_DIR)
    parser.add_argument("--sqlite", action="store_true", help="создать отдельную демонстрационную SQLite")
    args = parser.parse_args()
    if args.sqlite and (args.output / "green_control_demo.sqlite3").exists():
        parser.error("Демонстрационная база уже существует. Выберите новый каталог.")
    try:
        write_dataset(args.output, args.seed, args.count, args.reference_date)
        if args.sqlite:
            create_demo_database(args.output)
    except (OSError, ValueError) as error:
        parser.exit(2, f"Ошибка: {error}\n")
    print(f"Сформировано {args.count} синтетических дел · seed {args.seed} · {args.output}")


if __name__ == "__main__":
    main()
