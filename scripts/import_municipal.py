"""Загрузка учебного набора в SQLite без браузера (то же, что вкладки «Импорт из ИС»).

Порядок как в интерфейсе: дела -> опись актов -> выписка 1 -> выписка 2.
Запуск из корня проекта:
  $env:GREEN_CONTROL_DB = 'data/green_control_municipal_test.sqlite3'
  .\\.venv\\Scripts\\python.exe -m scripts.import_municipal data/municipal_demo
"""
from __future__ import annotations

import argparse
from pathlib import Path
from sqlite3 import Error as SQLiteError

from core import acts, payments, storage
from core.importer import read_cases
from core.models import load_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="папка с 01_cases.csv, 02_acts.csv, 03_payments.csv, 04_payments.csv")
    args = parser.parse_args()
    folder, profile = args.folder, load_profile()
    paths = {name: folder / name for name in ("01_cases.csv", "02_acts.csv", "03_payments.csv", "04_payments.csv")}
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        parser.error("Не найдены файлы: " + ", ".join(missing))
    try:
        print(f"База данных: {storage.DB_PATH}")
        storage.initialize(profile)
        records = read_cases(paths["01_cases.csv"], "01_cases.csv", profile)
        saved = storage.save_cases(records, "Командная строка: 01_cases.csv", profile=profile)
        print(f"1. Дела: в файле {len(records)}, сохранено новых {saved}.")
        plan = acts.preview(paths["02_acts.csv"], "02_acts.csv", storage.all_cases(profile))
        print(f"2. Акты: в описи {len(plan)}, изменено дел {acts.save(plan, '02_acts.csv', profile)}.")
        for step, name in ((3, "03_payments.csv"), (4, "04_payments.csv")):
            day, rows = payments.parse(paths[name], name)
            preview = payments.reconcile(storage.all_cases(profile), profile, day, rows)
            batch = payments.save(day, rows, name, preview, profile)
            print(f"{step}. Выписка {name} на {day}: строк {len(rows)}, результатов сверки {len(preview)}, срез №{batch}.")
    except (ValueError, OSError, SQLiteError) as exc:
        parser.exit(2, f"Ошибка: {exc}\n")
    print("Готово. Откройте приложение и перейдите в «Подготовка и сверка» / «Журнал платёжных сверок».")


if __name__ == "__main__":
    main()
