"""Подписи для интерфейса и документов. Коды хранения остаются прежними."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re

STATUS_LABELS = {
    "FLAG": "Выявлено отклонение", "NEED_DATA": "Недостаточно сведений",
    "PASS": "Отклонений не выявлено", "NOT_APPLICABLE": "Проверка не требуется",
}
RULE_LABELS = {
    "R1": "Проверка 1. Финансовые условия",
    "R2": "Проверка 2. Комплектность документов",
    "R3": "Проверка 3. Последовательность решений",
}
FIELD_LABELS = {
    "case_id": "Номер дела", "case_type": "Категория", "application_date": "Дата заявления",
    "issued_date": "Дата выдачи разрешения", "predecision_date": "Дата предварительного решения",
    "documents": "Документы", "documents_complete": "Комплектность",
    "accrued_amount": "Начислено, ₽", "paid_amount": "Оплачено, ₽",
    "exemption_code": "Основание освобождения", "exemption_proof": "Подтверждение освобождения",
    "comment": "Комментарий", "payment_note": "Пояснение к оплате", "finance_mode": "Финансовый сценарий",
}
DATE_FIELDS = ("application_date", "issued_date", "predecision_date")
MOSCOW = timezone(timedelta(hours=3))
CONTROL_CASES = {
    "DEMO-OK-001": "Контрольное дело № 001", "DEMO-FLAG-002": "Контрольное дело № 002",
    "DEMO-NODATA-003": "Контрольное дело № 003",
}
ENUM_LABELS = {
    "documents_complete": {"yes": "Комплект полный", "unknown": "Комплектность не установлена"},
    "exemption_proof": {"yes": "Подтверждение имеется", "no": "Отсутствие подтверждено", "unknown": "Не установлено"},
    "finance_mode": {"payment": "Начисление и оплата", "exemption": "Заявлено освобождение", "unknown": "Сведения не получены"},
}

def date_text(value):
    if not value:
        return "—"
    try:
        day = value if isinstance(value, date) else date.fromisoformat(value)
        return f"{day.day:02d}.{day.month:02d}.{day.year:04d}"
    except (TypeError, ValueError):
        return "Дата не распознана"

def timestamp(value):
    if not value:
        return "—"
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M:%S")
    except (TypeError, ValueError):
        return "Дата не распознана"

def month_text(value):
    year, month = value.split("-")
    return f"{month}.{year}"

def case_label(value):
    match = re.fullmatch(r"SYN-(\d+)-(\d+)", value or "")
    if match:
        return f"Набор {match[1]} · дело № {match[2]}"
    return CONTROL_CASES.get(value, value)

def profile_label(version):
    return f"Профиль проверки · версия {version}" if version else "—"

def source_label(value):
    aliases = {
        "Синтетика · набор 200": "Синтетические данные · набор из 200 дел",
        "Синтетика · контрольный сценарий": "Синтетические данные · контрольный сценарий",
        "Синтетика · демонстрационный набор": "Синтетические данные · контрольный набор",
    }
    return aliases.get(value, value)

def model_text(value):
    return str(value).replace(" R1–R3", " проверок").replace(" (учебная)", "").replace(" (учебные)", "").replace(
        "Учебн", "Модельн").replace("учебн", "модельн")

def category_label(value, profile):
    category = profile["case_types"].get(value, {})
    return model_text(category.get("label", "Категория прежнего профиля"))

def field_value(field, value, profile):
    if field in DATE_FIELDS:
        return date_text(value)
    if field == "case_id":
        return case_label(value)
    if field == "case_type":
        return category_label(value, profile)
    if field == "exemption_code" and value:
        exemption = profile.get("exemptions", {}).get(value)
        return model_text(exemption["label"]) if exemption else value
    if field in ENUM_LABELS:
        return ENUM_LABELS[field].get(value, "—")
    if isinstance(value, list):
        return ", ".join(value) or "Не отмечены"
    return "—" if value is None or value == "" else value

def _evidence_fields(result):
    parts = re.split(r"; (?=[a-z_]+=)", result.get("evidence", ""))
    return {key: value for key, separator, value in (part.partition("=") for part in parts) if separator}


def _labeled_evidence(key, value, case, profile):
    if key in FIELD_LABELS:
        stored = case.get(key) if key in case else value
        return FIELD_LABELS[key], field_value(key, stored, profile)
    if key == "payment_mode":
        return "Условие оплаты для категории", "Освобождение предусмотрено профилем"
    if key in ("required", "available"):
        label = "Требуемые документы" if key == "required" else "Отмеченные документы"
        return label, ", ".join(value.split("|")) if value else "Не отмечены"
    return None


def evidence_rows(result, case, profile):
    """Восстанавливает подписи для свидетельств прежних и текущих расчётов."""
    return [row for key, value in _evidence_fields(result).items()
            if (row := _labeled_evidence(key, value, case, profile)) is not None]


def comparison_evidence(result, case, profile):
    """Показывает поля текущей редакции и параметры её профиля проверки."""
    rule = result["rule"]
    kind = profile["case_types"].get(case["case_type"])
    if kind is None:
        return [(label, value, "Сохранённый расчёт") for label, value in evidence_rows(result, case, profile)]

    rows = [(FIELD_LABELS["case_type"], category_label(case["case_type"], profile), "Карточка дела")]
    fields = ()
    if rule == "R1":
        exempt = kind["payment_mode"] == "exempt"
        payment = "Оплата не требуется" if exempt else "Оплата или допустимое освобождение"
        rows.append(("Условие оплаты для категории", payment, "Профиль проверки"))
        code = case.get("exemption_code")
        if not exempt:
            if code or case.get("finance_mode") == "exemption":
                fields = ("exemption_code",)
                if code in kind["allowed_exemptions"]:
                    required = profile.get("exemptions", {}).get(code, {}).get("requires_document", True)
                    rows.append(("Подтверждающий документ обязателен", "Да" if required else "Нет", "Профиль проверки"))
                    if required:
                        fields += ("exemption_proof",)
            else:
                fields = ("accrued_amount", "paid_amount")
    elif rule == "R2":
        required = ", ".join(kind["required_documents"]) or "Нет обязательных документов"
        rows.append(("Требуемые документы", required, "Профиль проверки"))
        composition = "Подтверждён" if case.get("documents_complete") == "yes" else "Не подтверждён"
        rows.append(("Состав материалов", composition, "Карточка дела"))
        fields = ("documents",)
    elif rule == "R3":
        required = result["status"] != "NOT_APPLICABLE"
        rows.append(("Обязательное предварительное решение", "Да" if required else "Нет", "Профиль проверки"))
        if required:
            fields = ("issued_date", "predecision_date")

    for field in fields:
        value = field_value(field, case.get(field), profile)
        rows.append((FIELD_LABELS[field], "Нет сведений" if value == "—" else value, "Карточка дела"))
    return rows


def comparison_text(result, case, profile):
    """Объясняет фактическое сравнение. Статус берётся из сохранённого расчёта."""
    rule = result["rule"]
    status = result["status"]
    kind = profile["case_types"].get(case["case_type"])
    if kind is None:
        return model_text(result["explanation"])
    if rule == "R1":
        if kind["payment_mode"] == "exempt":
            return "Профиль предусматривает освобождение для этой категории. Суммы в сравнении не участвуют."
        code = case.get("exemption_code")
        if code or case.get("finance_mode") == "exemption":
            if not code:
                return "Освобождение заявлено, но сведения об основании отсутствуют."
            if code not in kind["allowed_exemptions"]:
                return "Указанное основание отсутствует в перечне допустимых для этой категории."
            if profile.get("exemptions", {}).get(code, {}).get("requires_document", True):
                return "Основание допустимо для категории. Для него проверяется отметка о подтверждающем документе."
            return "Основание допустимо для категории. Подтверждающий документ по профилю не требуется."
        if status == "NEED_DATA":
            return "Не хватает сведений о начислении или оплате. Суммы нельзя сопоставить."
        if case["accrued_amount"] == 0:
            return "В платной категории зафиксировано нулевое начисление. Освобождение в карточке не указано."
        difference = Decimal(str(case["accrued_amount"])) - Decimal(str(case["paid_amount"]))
        if difference == 0:
            return "Начисление и оплата совпадают."
        amount = f"{abs(difference):,.2f}".replace(",", " ").replace(".", ",")
        relation = "меньше" if difference > 0 else "больше"
        return f"Оплачено на {amount} ₽ {relation} начисленного."

    if rule == "R2":
        if status == "NEED_DATA":
            return "Состав материалов не подтверждён. По неотмеченным документам нельзя установить их отсутствие."
        if status == "FLAG":
            missing = sorted(set(kind["required_documents"]).difference(case["documents"]))
            return "В деле отсутствуют обязательные документы: " + ", ".join(missing) + "."
        return "Все документы из перечня профиля отмечены в составе материалов дела."

    if status == "NOT_APPLICABLE":
        return "Для этой категории предварительное решение не требуется по профилю."
    if status == "NEED_DATA":
        return "Для сопоставления нужны обе даты. Отсутствующие значения указаны в таблице."
    issued = date.fromisoformat(case["issued_date"])
    decision = date.fromisoformat(case["predecision_date"])
    if issued == decision:
        return "Обе даты совпадают. Последовательность в пределах одного дня по этим данным не видна."
    order = "раньше" if issued < decision else "позже"
    return f"Дата выдачи {order} даты предварительного решения. Разница в календарных днях: {abs((issued - decision).days)}."


def evidence_text(result, case, profile):
    return "\n".join(f"{label}: {value}" for label, value in evidence_rows(result, case, profile))

def case_comment(case):
    note = case.get("comment", "")
    return note.replace("Учебный тест:", "Контрольный сценарий:", 1) if case.get("case_id") in CONTROL_CASES else note


def activity_label(value):
    rule, separator, rest = value.partition(" · ")
    return RULE_LABELS[rule] + separator + rest if rule in RULE_LABELS else model_text(value)


def public_row(row):
    """Локализует итоговые строки перед показом или выгрузкой, после фильтрации."""
    converted = {}
    for key, value in row.items():
        if key in RULE_LABELS:
            value = STATUS_LABELS.get(value, value)
        elif key in ("Дело", "Номер дела"):
            value = case_label(value)
        elif key == "Дата заявления":
            value = date_text(value) if value != "—" else value
        elif key == "Месяц заявления":
            value = month_text(value)
        elif key == "Индикатор":
            value = RULE_LABELS.get(value, value)
        elif key == "Источник":
            value = source_label(value)
        converted[RULE_LABELS.get(key, STATUS_LABELS.get(key, key))] = value
    return converted
