"""Независимый от интерфейса движок объяснимых индикаторов R1–R3."""
from __future__ import annotations
from datetime import date


def signal(rule: str, status: str, title: str, explanation: str, evidence: str) -> dict:
    return {
        "rule": rule,
        "status": status,
        "title": title,
        "explanation": explanation,
        "evidence": evidence,
    }


def check_r1(case: dict, profile: dict) -> dict:
    """R1: финансовое условие/освобождение по УЧЕБНОМУ профилю."""
    kind = profile["case_types"][case["case_type"]]
    code = case["exemption_code"]

    if kind["payment_mode"] == "exempt":
        return signal("R1", "PASS", "Финансовое условие",
                      "Для этой учебной категории профиль предусматривает освобождение."
                      " Законность реального освобождения здесь НЕ оценивается.",
                      f"case_type={case['case_type']}; payment_mode=exempt")

    if case.get("finance_mode") == "exemption" and not code:
        return signal("R1", "NEED_DATA", "Основание освобождения не указано",
                      "По заявленному освобождению нужно уточнить основание из материалов дела.",
                      "exemption_code=нет")

    if code:
        if code not in kind["allowed_exemptions"]:
            return signal("R1", "FLAG", "Неприменимое основание освобождения",
                          "Указанный код освобождения не разрешён профилем для категории дела."
                          " Требуется правовая проверка.", f"exemption_code={code}")
        if not profile.get("exemptions", {}).get(code, {}).get("requires_document", True):
            return signal("R1", "PASS", "Основание допускается учебным профилем",
                          "Для указанного основания модельный профиль не требует подтверждающего документа.",
                          f"exemption_code={code}")
        proof = case["exemption_proof"]
        if proof == "unknown":
            return signal("R1", "NEED_DATA", "Не подтверждено основание освобождения",
                          "Нет сведений о наличии подтверждающего документа.",
                          f"exemption_code={code}; exemption_proof=unknown")
        if proof == "no":
            return signal("R1", "FLAG", "Нет подтверждения освобождения",
                          "В полном учебном сценарии документ для этого исключения обязателен."
                          " Указано, что он отсутствует.",
                          f"exemption_code={code}; exemption_proof=no")
        return signal("R1", "PASS", "Освобождение подтверждено в учебном профиле",
                      "Код допустим для категории, наличие подтверждения отмечено."
                      " Подлинность документов система не устанавливает.",
                      f"exemption_code={code}; exemption_proof=yes")

    accrued, paid = case["accrued_amount"], case["paid_amount"]
    if accrued is None or paid is None:
        return signal("R1", "NEED_DATA", "Недостаточно финансовых сведений",
                      "Для платной категории нет полного набора начислено/оплачено.",
                      f"accrued_amount={accrued}; paid_amount={paid}")
    if accrued <= 0:
        return signal("R1", "FLAG", "Нулевое начисление без основания",
                      "В учебной платной категории указана нулевая начисленная сумма и отсутствует код освобождения.",
                      f"accrued_amount={accrued}")
    if paid + 0.005 < accrued:
        return signal("R1", "FLAG", "Оплачено меньше начисленного",
                      "Выявлено расхождение между начисленной и уплаченной суммой."
                      " Требуется проверить срок оплаты, отсрочки и статус платежа.",
                      f"accrued_amount={accrued}; paid_amount={paid}")
    return signal("R1", "PASS", "Расхождение по оплате не выявлено",
                  "Оплаченная сумма не меньше начисленной по данным дела."
                  " Правильность самого начисления отдельной проверкой не подтверждена.",
                  f"accrued_amount={accrued}; paid_amount={paid}")


def check_r2(case: dict, profile: dict) -> dict:
    """R2: контроль комплектности документов; неизвестность не равна нарушению."""
    required = set(profile["case_types"][case["case_type"]]["required_documents"])
    available = set(case["documents"])
    if case["documents_complete"] != "yes":
        return signal("R2", "NEED_DATA", "Комплектность дела не подтверждена",
                      "Нельзя делать вывод об отсутствии документа, пока неизвестно,"
                      " все ли документы предоставлены для проверки.",
                      "documents_complete=unknown; available=" + "|".join(sorted(available)))
    missing = sorted(required - available)
    if missing:
        return signal("R2", "FLAG", "Нет обязательных документов",
                      "По учебному профилю в подтверждённо полном деле отсутствуют: " + ", ".join(missing),
                      "required=" + "|".join(sorted(required)) + "; available=" + "|".join(sorted(available)))
    return signal("R2", "PASS", "Документы по перечню присутствуют",
                  "Все обязательные названия документов обнаружены в отмеченном полном деле."
                  " Достоверность и содержание документов не проверялись.",
                  "available=" + "|".join(sorted(available)))


def check_r3(case: dict, profile: dict) -> dict:
    """R3: последовательность дат, если предварительное решение обязательно."""
    kind = profile["case_types"][case["case_type"]]
    if not kind["requires_predecision"]:
        return signal("R3", "NOT_APPLICABLE", "Предварительное решение не требуется",
                      "Для этой учебной категории проверка последовательности не применяется.",
                      f"case_type={case['case_type']}")
    issued, prior = case["issued_date"], case["predecision_date"]
    if not issued or not prior:
        return signal("R3", "NEED_DATA", "Недостаточно дат",
                      "Нужны дата выдачи и дата обязательного предварительного решения.",
                      f"issued_date={issued or 'нет'}; predecision_date={prior or 'нет'}")
    if date.fromisoformat(issued) < date.fromisoformat(prior):
        return signal("R3", "FLAG", "Нарушена последовательность",
                      "Дата выдачи билета раньше обязательного предварительного решения."
                      " Следует проверить исходные документы и корректность дат.",
                      f"issued_date={issued}; predecision_date={prior}")
    return signal("R3", "PASS", "Последовательность дат соблюдена",
                  "Дата выдачи не раньше даты предварительного решения.",
                  f"issued_date={issued}; predecision_date={prior}")


def analyze_case(case: dict, profile: dict) -> list[dict]:
    """Один и тот же вход + один профиль дают одинаковый список результатов."""
    kind = profile["case_types"][case["case_type"]]
    exemptions = [
        profile.get("exemptions", {}).get(code, {}).get("label", code)
        for code in kind["allowed_exemptions"]
    ]
    finance = "Профиль освобождает категорию от оплаты."
    if kind["payment_mode"] != "exempt":
        finance = "Положительное начисление и оплата не ниже начисления либо допустимое освобождение."
        if exemptions:
            finance += " Основания: " + ", ".join(exemptions) + "."
    conditions = [
        finance,
        "При подтверждённой комплектности требуются: " + ", ".join(kind["required_documents"]) + ".",
        ("Дата выдачи не раньше даты обязательного предварительного решения."
         if kind["requires_predecision"] else "Предварительное решение по категории не требуется."),
    ]
    results = [check_r1(case, profile), check_r2(case, profile), check_r3(case, profile)]
    for result, condition in zip(results, conditions):
        indicator = profile.get("indicators", {}).get(result["rule"], {})
        result.update(
            name=indicator.get("name", result["rule"]),
            condition=condition,
            limitation=indicator.get("limitation", profile["description"]),
            profile_id=profile["profile_id"],
            profile_version=str(profile["version"]),
        )
    return results
