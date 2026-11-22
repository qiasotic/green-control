"""Единая структура разрешительного дела и проверка вводимых значений."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import json
import math
import re

from core.presentation import case_label
from core.municipal import FIELDS, clean_field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = PROJECT_ROOT / "config" / "rules_demo.json"

CASE_COLUMNS = [
    "case_id", "case_type", "application_date", "issued_date",
    "predecision_date", "documents", "documents_complete",
    "accrued_amount", "paid_amount", "exemption_code",
    "exemption_proof", "comment",
]

OPTIONAL_CASE_COLUMNS = ("finance_mode", "payment_note")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def load_profile(path: Path = PROFILE_PATH) -> dict:
    """Загружает нормативный профиль. Текущий профиль исключительно учебный."""
    with path.open("r", encoding="utf-8") as handle:
        profile = json.load(handle)
    if not profile.get("case_types"):
        raise ValueError("В профиле отсутствует раздел case_types")
    if not all(profile.get(key) for key in ("profile_id", "version", "status", "description")):
        raise ValueError("В профиле не заполнены идентификатор, версия, статус или описание")
    return profile


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    text = str(value).strip()
    if CONTROL_CHARACTERS.search(text):
        raise ValueError("В полях обнаружены недопустимые управляющие символы")
    return text


def _date(value: object, name: str) -> str:
    result = _text(value)
    if result:
        try:
            if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", result):
                return datetime.strptime(result, "%d.%m.%Y").date().isoformat()
            return date.fromisoformat(result).isoformat()
        except ValueError as exc:
            raise ValueError(f"{name}: укажите корректную дату в формате ДД.ММ.ГГГГ") from exc
    return result


def _amount(value: object, name: str) -> float | None:
    raw = _text(value).replace(" ", "").replace(",", ".")
    if not raw:
        return None
    try:
        amount = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name}: требуется число, например 1250.50") from exc
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"{name}: сумма должна быть неотрицательным конечным числом")
    return round(amount, 2)


def normalize_case(raw: dict, profile: dict) -> dict:
    """Нормализует одну запись; исключает подмену отсутствующих данных нулём."""
    case_id = _text(raw.get("case_id"))
    if not case_id:
        raise ValueError("Укажите номер дела")
    if any(character in case_id for character in ("\r", "\n", "\t")):
        raise ValueError("Укажите номер дела одной строкой")
    if len(case_id) > 80:
        raise ValueError("Номер дела должен содержать до 80 символов")

    case_type = _text(raw.get("case_type"))
    if case_type not in profile["case_types"]:
        raise ValueError(f"Дело {case_label(case_id)}: указана неизвестная категория")

    docs_raw = raw.get("documents", "")
    if isinstance(docs_raw, (list, tuple, set)):
        documents = [_text(item) for item in docs_raw]
    else:
        documents = [item.strip() for item in _text(docs_raw).split("|") if item.strip()]
    documents = sorted(set(filter(None, documents)))

    completeness = _text(raw.get("documents_complete", "unknown")).lower() or "unknown"
    proof = _text(raw.get("exemption_proof", "unknown")).lower() or "unknown"
    if completeness not in ("yes", "unknown"):
        raise ValueError(f"Дело {case_label(case_id)}: укажите, подтверждена ли комплектность материалов")
    if proof not in ("yes", "no", "unknown"):
        raise ValueError(f"Дело {case_label(case_id)}: укажите наличие, отсутствие или неизвестность подтверждения освобождения")

    finance_mode = _text(raw.get("finance_mode")).lower()
    if finance_mode not in ("", "unknown", "payment", "exemption"):
        raise ValueError(f"Дело {case_label(case_id)}: финансовый сценарий не распознан")
    comments = {key: _text(raw.get(key)) for key in ("comment", "payment_note")}
    if any(len(value) > 1000 for value in comments.values()):
        raise ValueError("Комментарий и пояснение к оплате должны содержать до 1000 символов")

    result = {
        "case_id": case_id,
        "case_type": case_type,
        "application_date": _date(raw.get("application_date"), "Дата заявления"),
        "issued_date": _date(raw.get("issued_date"), "Дата выдачи разрешения"),
        "predecision_date": _date(raw.get("predecision_date"), "Дата предварительного решения"),
        "documents": documents,
        "documents_complete": completeness,
        "accrued_amount": _amount(raw.get("accrued_amount"), "Начислено"),
        "paid_amount": _amount(raw.get("paid_amount"), "Оплачено"),
        "exemption_code": _text(raw.get("exemption_code")),
        "exemption_proof": proof,
        "comment": comments["comment"],
    }
    if finance_mode in ("unknown", "exemption"):
        result.update(accrued_amount=None, paid_amount=None)
    if finance_mode in ("unknown", "payment"):
        result.update(exemption_code="", exemption_proof="unknown")
    if ((finance_mode == "exemption" and not result["exemption_code"]) or
            (finance_mode == "payment" and result["accrued_amount"] is None and result["paid_amount"] is None)):
        result["finance_mode"] = finance_mode
    if comments["payment_note"] and finance_mode in ("", "payment"):
        result["payment_note"] = comments["payment_note"]
    for key in FIELDS:
        value = clean_field(key, raw.get(key))
        if value != "":
            result[key] = value
    if result.get("permit_stage") and result["issued_date"]:
        result["permit_stage"] = "issued"
    return result
