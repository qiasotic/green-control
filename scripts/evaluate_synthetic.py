"""Измерение качества R1–R3 на независимо размеченной синтетике."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
from statistics import median
from time import perf_counter

from core.importer import read_cases
from core.models import load_profile
from core.rules import analyze_case
from scripts.generate_synthetic import DATA_DIR

RULES = ("R1", "R2", "R3")
STATUSES = ("FLAG", "PASS", "NEED_DATA", "NOT_APPLICABLE")
BINARY = STATUSES[:2]
LIMIT = ("Измерение проверяет согласованность модельных правил с заданными сценариями. "
         "Эффективность в реальной ведомственной эксплуатации здесь не оценивалась.")


def divide(numerator, denominator):
    return numerator / denominator if denominator else None


def read_truth(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, delimiter=";")
        if not {"case_id", "scenario", *RULES} <= set(reader.fieldnames or ()):
            raise ValueError("Разметка должна содержать case_id, scenario и R1–R3.")
        labels = {}
        for row in reader:
            case_id = row["case_id"]
            if not case_id or case_id in labels or any(row[rule] not in STATUSES for rule in RULES):
                raise ValueError(f"Некорректная разметка дела {case_id!r}.")
            labels[case_id] = row
    return labels


def _predict(records, profile):
    return {case["case_id"]: analyze_case(case, profile) for case in records}


def score(expected, observed):
    matrix = Counter(zip(expected, observed, strict=True))
    tp, fp = matrix["FLAG", "FLAG"], matrix["PASS", "FLAG"]
    fn, tn = matrix["FLAG", "PASS"], matrix["PASS", "PASS"]
    applicable = sum(count for (truth, _), count in matrix.items() if truth != "NOT_APPLICABLE")
    covered = sum(count for (truth, found), count in matrix.items()
                  if truth != "NOT_APPLICABLE" and found in BINARY)
    count = matrix.total()
    match = sum(matrix[status, status] for status in STATUSES)
    return {
        "TP": tp, "FP": fp, "FN": fn, "TN": tn,
        "precision": divide(tp, tp + fp), "recall": divide(tp, tp + fn),
        "applicable": applicable, "covered": covered, "coverage": divide(covered, applicable),
        "binary_pairs": tp + fp + fn + tn, "nonbinary_pairs": count - tp - fp - fn - tn,
        "four_status_matches": match, "four_status_agreement": divide(match, count),
        "reference_counts": {status: sum(matrix[status, found] for found in STATUSES) for status in STATUSES},
        "prediction_counts": {status: sum(matrix[truth, status] for truth in STATUSES) for status in STATUSES},
        "matrix": {truth: {found: matrix[truth, found] for found in STATUSES} for truth in STATUSES},
    }


def evaluate(records, labels, profile):
    ids = [case["case_id"] for case in records]
    if len(ids) != len(set(ids)) or set(ids) != set(labels):
        raise ValueError("Списки уникальных номеров выгрузки и эталона должны совпадать.")
    runs = _predict(records, profile)
    deterministic = runs == _predict(records, profile)
    by_id = {case_id: {outcome["rule"]: outcome["status"] for outcome in outcomes}
             for case_id, outcomes in runs.items()}
    metrics = {rule: score([labels[case_id][rule] for case_id in ids],
                           [by_id[case_id][rule] for case_id in ids]) for rule in RULES}
    mismatches = [{"case_id": case_id, "scenario": labels[case_id]["scenario"], "rule": rule,
                   "expected": labels[case_id][rule], "observed": by_id[case_id][rule]}
                  for case_id in ids for rule in RULES if labels[case_id][rule] != by_id[case_id][rule]]
    return {"count": len(ids), "deterministic": deterministic, "rules": metrics, "mismatches": mismatches}


def benchmark(records, profile, repeats=7):
    times = {}
    for size in (1, 100, 200):
        durations = []
        if len(records) >= size:
            batch = records[:size]
            _predict(batch, profile)
            for _ in range(repeats):
                start = perf_counter()
                _predict(batch, profile)
                durations.append((perf_counter() - start) * 1000)
        times[str(size)] = median(durations) if durations else None
    return {"median_ms": times, "repeats": repeats,
            "machine": {"os": platform.platform(), "cpu": platform.processor() or platform.machine(),
                        "logical_cpus": os.cpu_count(), "python": platform.python_version()}}


def markdown(report):
    def percent(value):
        return "не определено" if value is None else f"{value:.1%}"

    def seconds(value):
        return "не измерялось" if value is None else f"{value:.3f}"

    lines = [
        "# Испытания R1–R3", "", LIMIT, "",
        f"Дел: {report['count']}. Профиль: {report['profile_id']} v{report['profile_version']}. "
        f"Расчёт: {report['measured_at']}.", "",
        "## Результаты", "",
        "| Правило | TP | FP | FN | TN | Precision | Recall | Coverage | Совпадения 4 статусов |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for rule, metrics in report["rules"].items():
        lines.append(f"| {rule} | {metrics['TP']} | {metrics['FP']} | {metrics['FN']} | {metrics['TN']} | "
                     f"{percent(metrics['precision'])} | {percent(metrics['recall'])} | "
                     f"{metrics['covered']}/{metrics['applicable']} ({percent(metrics['coverage'])}) | "
                     f"{metrics['four_status_matches']}/{report['count']} |")
    lines += ["", "В TP/FP/FN/TN входят пары, где и эталон, и результат — FLAG или PASS. "
              "Coverage использует применимость по эталону. NEED_DATA считается применимой проверкой. "
              "Неприменимость и воздержание показаны отдельно.", "",
              "| Правило | NEED_DATA эталон / расчёт | NOT_APPLICABLE эталон / расчёт | Пар вне бинарной оценки |",
              "| --- | ---: | ---: | ---: |"]
    for rule, metrics in report["rules"].items():
        ref, pred = metrics["reference_counts"], metrics["prediction_counts"]
        lines.append(f"| {rule} | {ref['NEED_DATA']} / {pred['NEED_DATA']} | "
                     f"{ref['NOT_APPLICABLE']} / {pred['NOT_APPLICABLE']} | {metrics['nonbinary_pairs']} |")
    benchmark_info = report["benchmark"]
    machine = benchmark_info["machine"]
    lines += ["", "## Воспроизводимость и время", "",
              f"Два структурированных запуска совпали: {'да' if report['deterministic'] else 'нет'}.",
              f"Расхождений по четырём статусам: {len(report['mismatches'])}. "
              "Матрицы и детали расхождений доступны в JSON.", "",
              f"Среда: {machine['os']}, {machine['cpu']}, логических CPU: {machine['logical_cpus']}, "
              f"Python {machine['python']}. Медиана из {benchmark_info['repeats']} повторов "
              "после прогрева, только движок правил.", "",
              "| Дел | Время, мс |", "| ---: | ---: |"]
    lines += [f"| {size} | {seconds(timing)} |" for size, timing in benchmark_info["median_ms"].items()]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    parser.add_argument("--output", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    try:
        profile = load_profile()
        manifest = json.loads((args.data / "generation_manifest.json").read_text(encoding="utf-8"))
        if manifest["reference_profile"] != {"id": profile["profile_id"], "version": str(profile["version"])}:
            raise ValueError("Эталон подготовлен для другой версии профиля.")
        records = read_cases(args.data / "dataset.csv", "dataset.csv", profile)
        if len(records) != manifest["count"]:
            raise ValueError("Число дел в наборе отличается от манифеста.")
        report = evaluate(records, read_truth(args.data / "ground_truth.csv"), profile)
        report.update(profile_id=profile["profile_id"], profile_version=profile["version"],
                      seed=manifest["seed"], measured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      benchmark=benchmark(records, profile), limitation=LIMIT)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                                    encoding="utf-8")
        (args.output / "evaluation.md").write_text(markdown(report), encoding="utf-8")
        print(f"Проверено {report['count']} дел. Расхождений: {len(report['mismatches'])}.")
    except (KeyError, OSError, ValueError) as error:
        parser.exit(2, f"Ошибка: {error}\n")


if __name__ == "__main__":
    main()
