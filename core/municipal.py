"""Рабочие сведения о выдаче и сверка двух документов."""
import re

FIELDS = {
    "charge_id": "Номер начисления",
    "permit_stage": "Этап выдачи",
    "act_number": "Номер акта обследования",
    "act_site": "Код участка в акте",
    "act_trees": "Деревьев в акте",
    "permit_act_number": "Номер акта в билете",
    "permit_site": "Код участка в билете",
    "permit_trees": "Деревьев в билете",
}
STAGES = {"pre_issue": "До выдачи", "issued": "После выдачи", "unknown": "Этап не уточнён"}
PAIRS = [
    ("Номер акта", "act_number", "permit_act_number"),
    ("Код участка", "act_site", "permit_site"),
    ("Количество деревьев", "act_trees", "permit_trees"),
]


def clean_field(key, value):
    if value is None or value == "":
        return ""
    if key in ("act_trees", "permit_trees"):
        if not re.fullmatch(r"[0-9]{1,7}", str(value).strip()):
            raise ValueError(f"{FIELDS[key]}: укажите целое неотрицательное число до 7 цифр.")
        return int(str(value).strip())
    if not isinstance(value, str) or len(value.strip()) > 100 or not value.isprintable():
        raise ValueError(f"{FIELDS[key]}: используйте однострочное текстовое поле до 100 символов.")
    text = value.strip()
    if key == "permit_stage":
        text = next((k for k, label in STAGES.items() if label == text), text)
        if text not in STAGES:
            raise ValueError("Этап выдачи: допустимы pre_issue, issued или unknown.")
    return text


def stage(case):
    if case.get("issued_date"):
        return "issued"
    return case.get("permit_stage") or "unknown"


def document_check(case):
    rows = []
    for label, act_key, permit_key in PAIRS:
        act, permit = case.get(act_key), case.get(permit_key)
        if act in (None, "") or permit in (None, ""):
            status = "Нужны сведения"
        else:
            status = "Совпадает" if act == permit else "Есть расхождение"
        rows.append({"Реквизит": label, "Акт обследования": str(act) if act not in (None, "") else "—",
                     "Билет": str(permit) if permit not in (None, "") else "—", "Результат": status})
    return rows


def tasks(case, profile, payment=None):
    """Каждая строка задаёт конкретное действие и текст для соответствующего подразделения."""
    phase = stage(case)
    actions = []
    def add(owner, action, request):
        actions.append({"Дело": case["case_id"], "Этап": STAGES[phase], "Кому": owner,
                        "Действие": action, "Текст запроса": f"По делу {case['case_id']} {request}"})
    kind = profile["case_types"].get(case["case_type"])
    if not kind:
        add("Специалист", "Уточнить применимый профиль", "уточните категорию и применимый перечень условий.")
        return actions
    if phase == "unknown":
        add("Специалист", "Уточнить этап выдачи", "уточните, выдан ли билет, и внесите этап либо дату выдачи.")
    if phase == "issued" and not case.get("issued_date"):
        add("Делопроизводство", "Уточнить дату выдачи", "сообщите дату выдачи билета по журналу регистрации.")
    outcomes = {r["rule"]: r for r in (case.get("_analysis") or {}).get("outcomes", [])}
    r1 = outcomes.get("R1", {})
    if payment and payment["status"] != "Оплата подтверждена":
        action = {"pre_issue": "Уточнить поступление до выдачи",
                  "issued": "Проверить расчёты по выданному билету",
                  "unknown": "Сверить оплату по выписке"}[phase]
        add("Бухгалтерия", action, payment_request(payment))
    elif payment and payment.get("external") is not None and case.get("paid_amount") != payment["external"]:
        add("Специалист", "Перенести оплату из сверки", "проверьте сопоставленную сумму и подтвердите перенос в карточку.")
    elif r1.get("status") in ("NEED_DATA", "FLAG"):
        if case.get("exemption_code") or case.get("finance_mode") == "exemption":
            add("Специалист", "Проверить основание освобождения", "приложите основание освобождения и его подтверждение.")
        elif phase == "pre_issue":
            add("Бухгалтерия", "Уточнить расчёт до выдачи", "подтвердите начисление и поступившую сумму на дату подготовки билета.")
        else:
            add("Бухгалтерия", "Сверить финансовые условия", "предоставьте расчёт и подтверждение суммы оплаты по делу.")
    r2 = outcomes.get("R2", {})
    if r2.get("status") in ("FLAG", "NEED_DATA"):
        missing = sorted(set(kind["required_documents"]) - set(case.get("documents", [])))
        request = ("предоставьте документы по полной описи: " + ", ".join(missing) + "."
                   if r2["status"] == "FLAG" else "подтвердите полноту описи и перечень переданных документов.")
        add("Ответственный за комплект дела", "Проверить опись", request)
    if kind["requires_predecision"]:
        if not case.get("predecision_date"):
            add("Делопроизводство", "Получить реквизиты решения", "укажите дату и реквизиты обязательного решения.")
        elif phase == "issued" and outcomes.get("R3", {}).get("status") == "FLAG":
            add("Делопроизводство", "Сверить хронологию выдачи",
                f"сверьте даты решения ({case['predecision_date']}) и выдачи ({case['issued_date']}) с журналом.")
    # Сравнение актуально, когда в деле начат ввод хотя бы одного документа.
    if any(case.get(k) not in ("", None) for _, a, p in PAIRS for k in (a, p)):
        for row in document_check(case):
            if row["Результат"] != "Совпадает":
                add("Благоустройство", "Сверить документы · " + row["Реквизит"],
                    f"сверьте реквизит «{row['Реквизит']}». Акт: {row['Акт обследования']}. Билет: {row['Билет']}.")
    return actions


def payment_request(row):
    charge = row.get("charge_id") or "не указан"
    return (f"уточните расчёты по начислению {charge}. {row['reason']} "
            "Подтвердите номер начисления, сумму поступлений и дату выписки.")

