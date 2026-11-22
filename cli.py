"""Локальная проверка и импорт ведомственных выгрузок из командной строки."""
from __future__ import annotations

import argparse
from pathlib import Path
from sqlite3 import Error as SQLiteError

from core.importer import read_cases
from core.models import load_profile
from core.rules import analyze_case
from core.storage import initialize, save_cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Зелёный контроль: проверка CSV/XLSX без браузера")
    parser.add_argument("action", choices=("validate", "import"))
    parser.add_argument("file", type=Path)
    parser.add_argument("--update-existing", action="store_true",
                        help="разрешить обновление существующих дел при импорте")
    args = parser.parse_args()
    if not args.file.is_file():
        parser.error(f"Файл не найден: {args.file}")
    profile = load_profile()
    try:
        records = read_cases(args.file, args.file.name, profile)
        if args.action == "import":
            initialize(profile)
            changed = save_cases(records, source=f"Командная строка: {args.file.name}", profile=profile,
                                 update_existing=args.update_existing)
            print(f"Сохранено дел: {changed}. Совпадающих пропущено: {len(records) - changed}.")
    except (OSError, ValueError, SQLiteError) as exc:
        parser.exit(2, f"Ошибка: {exc}\n")
    totals = {name: 0 for name in ("FLAG", "NEED_DATA", "PASS", "NOT_APPLICABLE")}
    for case in records:
        results = analyze_case(case, profile)
        print(f"{case['case_id']}: " + ", ".join(f"{r['rule']}={r['status']}" for r in results))
        for result in results:
            totals[result["status"]] += 1
    print(f"Дел: {len(records)}; итоги проверок: " + ", ".join(f"{k}={v}" for k, v in totals.items()))
    if args.action == "validate":
        print("Проверка выполнена без сохранения в базу.")
    print("Использован модельный профиль предварительной проверки.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
