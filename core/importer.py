"""Обмен разрешительными делами через локальные CSV/XLSX и папку incoming."""
from __future__ import annotations

import csv
from datetime import datetime
from io import BytesIO
from pathlib import Path
from sqlite3 import Error as SQLiteError
from zipfile import BadZipFile, ZipFile
import shutil
from xml.etree.ElementTree import ParseError

import pandas as pd
from openpyxl.utils.exceptions import InvalidFileException

from core.models import CASE_COLUMNS, OPTIONAL_CASE_COLUMNS, normalize_case
from core.municipal import FIELDS
from core.presentation import ENUM_LABELS, FIELD_LABELS, category_label, case_label
from core.storage import save_cases

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "incoming"
PROCESSED = INBOX / "processed"
MAX_ROWS = 5000


def _csv_sample(source):
    if hasattr(source, "seek"):
        source.seek(0)
        data = source.read(32768)
        source.seek(0)
        return data
    with Path(source).open("rb") as file:
        return file.read(32768)


def _csv_separator(sample):
    # Inspect several lines: a Russian heading may itself contain a comma.
    if len(sample) >= 32768 and "\n" in sample:
        sample = sample.rsplit("\n", 1)[0] + "\n"
    delimiters = ";,\t"
    try:
        return csv.Sniffer().sniff(sample, delimiters=delimiters).delimiter
    except csv.Error:
        first = sample.splitlines()[0] if sample.splitlines() else ""
        return max(delimiters, key=lambda delimiter: len(next(csv.reader([first], delimiter=delimiter))))


def _read_csv(source):
    sample = _csv_sample(source)
    for encoding in ("utf-8-sig", "cp1251"):
        sample_text = sample.decode(encoding, errors="ignore") if isinstance(sample, bytes) else sample
        if hasattr(source, "seek"):
            source.seek(0)
        try:
            return pd.read_csv(source, sep=_csv_separator(sample_text), dtype=str, engine="python",
                               keep_default_na=False, encoding=encoding, nrows=MAX_ROWS + 1)
        except UnicodeDecodeError:
            if encoding == "cp1251":
                raise


def read_frame(source, filename: str):
    """Читает только данные первой страницы. Ограничивает объём и строки."""
    if hasattr(source, "seek"):
        source.seek(0)
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".xlsx":
            with ZipFile(source) as workbook:
                if sum(entry.file_size for entry in workbook.infolist()) > 100 * 1024 * 1024:
                    raise ValueError("Содержимое книги XLSX слишком велико после распаковки.")
            if hasattr(source, "seek"):
                source.seek(0)
            frame = pd.read_excel(source, dtype=object, keep_default_na=False, engine="openpyxl",
                                  nrows=MAX_ROWS + 1)
        elif suffix == ".csv":
            frame = _read_csv(source)
        else:
            raise ValueError("Принимаются только CSV или XLSX.")
    except pd.errors.EmptyDataError as exc:
        raise ValueError("В выгрузке нет строк данных.") from exc
    except (BadZipFile, InvalidFileException, ParseError, pd.errors.ParserError,
            UnicodeError, KeyError, RuntimeError) as exc:
        raise ValueError("Не удалось прочитать файл. Проверьте формат и целостность выгрузки.") from exc

    if frame.empty:
        raise ValueError("В выгрузке нет строк данных.")
    if len(frame) > MAX_ROWS:
        raise ValueError(f"В одной выгрузке допускается до {MAX_ROWS} строк.")
    frame.columns = frame.columns.map(str)
    if frame.columns.has_duplicates:
        raise ValueError("Названия колонок в выгрузке должны быть уникальными.")
    return frame


def mapped_rows(frame, mapping, profile):
    """Возвращает исходные сопоставленные поля и ошибки по номерам строк."""
    if (not {"case_id", "case_type"}.issubset(mapping)
            or len(set(mapping.values())) != len(mapping)
            or not set(mapping.values()).issubset(frame.columns)):
        raise ValueError("Сопоставьте разные колонки с номером и категорией дела.")
    categories = {name.casefold(): key for key in profile["case_types"]
                  for name in (key, category_label(key, profile), profile["case_types"][key]["label"])}
    values = {
        field: {label.casefold(): value for value, label in options.items()}
        for field, options in ENUM_LABELS.items()
    }
    values["documents_complete"]["да"] = "yes"
    values["exemption_proof"].update({"да": "yes", "нет": "no"})
    values["exemption_code"] = {definition["label"].casefold(): key
                                for key, definition in profile.get("exemptions", {}).items()}
    values["case_type"] = categories
    records, errors = [], []
    seen = set()
    for number, row in enumerate(frame.to_dict("records"), 2):
        raw = {field: row[column] for field, column in mapping.items()}
        for field, options in values.items():
            if field in raw and isinstance(raw[field], str):
                raw[field] = options.get(raw[field].strip().casefold(), raw[field])
        try:
            case = normalize_case(raw, profile)
            case_id = case["case_id"]
            if case_id in seen:
                raise ValueError(f"Повторный номер дела «{case_label(case_id)}» в выгрузке.")
            seen.add(case_id)
            records.append({**raw, "case_id": case_id})
        except ValueError as exc:
            errors.append((number, str(exc)))
    return records, errors


def inspect_cases(source, filename: str, profile: dict):
    """Импорт стандартной выгрузки с сохранением совместимости колонок."""
    frame = read_frame(source, filename)
    mapping = {}
    labels = {**FIELD_LABELS, **FIELDS}
    for column in [*CASE_COLUMNS, *OPTIONAL_CASE_COLUMNS, *FIELDS]:
        if column in frame.columns:
            mapping[column] = column
        elif labels[column] in frame.columns:
            mapping[column] = labels[column]
    missing = [column for column in CASE_COLUMNS if column not in mapping]
    if missing:
        raise ValueError("В выгрузке нет колонок: " + ", ".join(FIELD_LABELS[column] for column in missing))
    records, errors = mapped_rows(frame, mapping, profile)
    return [normalize_case(row, profile) for row in records], errors


def read_cases(source, filename: str, profile: dict) -> list[dict]:
    """Импорт стандартной выгрузки с сохранением совместимости колонок."""
    records, errors = inspect_cases(source, filename, profile)
    if errors:
        number, message = errors[0]
        raise ValueError(f"Строка {number}: {message}")
    return records


def empty_template_bytes(*, localized=False) -> bytes:
    labels = {**FIELD_LABELS, **FIELDS}
    columns = [*CASE_COLUMNS, *OPTIONAL_CASE_COLUMNS, *FIELDS]
    buffer = BytesIO()
    if not localized:
        pd.DataFrame(columns=columns).to_excel(buffer, index=False, engine="openpyxl")
    else:
        with pd.ExcelWriter(buffer, engine="openpyxl") as book:
            pd.DataFrame(columns=[labels[key] for key in columns]).to_excel(
                book, sheet_name="Разрешительные дела", index=False)
            pd.DataFrame([
                ("Категория", "Общая процедура или Аварийные работы"),
                ("Даты", "ДД.ММ.ГГГГ"),
                ("Документы", "Разделяйте названия вертикальной чертой"),
                ("Комплектность", "Комплект полный или Комплектность не установлена"),
                ("Финансовый сценарий", "Начисление и оплата, Заявлено освобождение или Сведения не получены"),
                ("Денежные суммы", "Оставьте пустым, если сумма неизвестна"),
                ("Подтверждение освобождения", "Подтверждение имеется, Отсутствие подтверждено или Не установлено"),
            ], columns=["Поле", "Заполнение"]).to_excel(book, sheet_name="Подсказки", index=False)
    return buffer.getvalue()


def sync_folder(profile: dict, *, update_existing=False) -> list[str]:
    """По явной команде сохраняет каждый файл одной транзакцией и архивирует его."""
    INBOX.mkdir(parents=True, exist_ok=True)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    messages = []
    for path in sorted(INBOX.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".csv", ".xlsx"):
            continue
        try:
            records = read_cases(path, path.name, profile)
            changed = save_cases(records, source=f"Папка обмена: {path.name}", profile=profile,
                                 update_existing=update_existing)
        except (OSError, ValueError, SQLiteError) as exc:
            messages.append(f"ОШИБКА: {path.name} — {exc}")
            continue
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        destination = PROCESSED / f"{path.stem}_{stamp}{path.suffix}"
        try:
            shutil.move(str(path), str(destination))
        except OSError:
            messages.append(f"ВНИМАНИЕ: {path.name} — изменения сохранены, файл не перемещён в архив.")
        else:
            messages.append(f"ГОТОВО: {path.name} — сохранено дел: {changed}, "
                            f"совпадающих пропущено: {len(records) - changed}.")
    return messages
