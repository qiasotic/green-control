"""Объяснимый план последующей ручной проверки после индикаторов R1–R3.

Не делает юридических выводов. Не считает отсутствие данных нарушением.
Должен оставаться независимым от Streamlit и файлового хранилища.
"""
from __future__ import annotations

from core.rules import analyze_case
from core.presentation import RULE_LABELS, STATUS_LABELS, case_label, category_label, evidence_text, model_text, profile_label


def build_review_plan(case: dict, profile: dict, outcomes: list[dict] | None = None) -> list[dict]:
    """Возвращает предложения специалисту по всем FLAG и NEED_DATA.

    Источник каждого предложения — конкретное сработавшее правило и поля дела.
    Ни одно предложение не означает, что коррупция доказана.
    """
    if outcomes is None:
        outcomes = analyze_case(case, profile)
    by_rule = {outcome["rule"]: outcome for outcome in outcomes}
    plan: list[dict] = []

    def add(rule: str, steps: list[str]) -> None:
        outcome = by_rule[rule]
        if outcome["status"] not in ("FLAG", "NEED_DATA"):
            return
        plan.append({
            "rule": rule,
            "status": outcome["status"],
            "title": outcome["title"],
            "why": outcome["explanation"],
            "steps": steps,
        })

    r1 = by_rule["R1"]
    if r1["status"] == "NEED_DATA":
        if case.get("finance_mode") == "exemption" and not case["exemption_code"]:
            steps = ["Уточнить заявленное основание освобождения по материалам дела."]
        elif case["exemption_code"]:
            steps = [
                "Запросить подтверждающий документ по заявленному основанию освобождения.",
                "Проверить применимость основания по действовавшему на дату решения регламенту.",
            ]
        else:
            fields = []
            if case["accrued_amount"] is None:
                fields.append("начисленной сумме")
            if case["paid_amount"] is None:
                fields.append("фактически оплаченной сумме")
            steps = [
                "Получить сведения о " + " и ".join(fields) + "." if fields else
                "Уточнить финансовые сведения по разрешительному делу.",
                "Уточнить применимость платы и допустимые исключения по локальному регламенту.",
            ]
        add("R1", steps)
    elif r1["status"] == "FLAG":
        if case["exemption_code"]:
            steps = [
                "Сверить основание освобождения с действовавшим регламентом и документами дела.",
                "Запросить у профильного подразделения письменное обоснование применения исключения.",
            ]
        else:
            steps = [
                "Сверить начисление и поступление с первичными платёжными документами.",
                "Проверить срок оплаты, допустимые отсрочки, корректировки и законные исключения.",
            ]
        add("R1", steps)

    r2 = by_rule["R2"]
    required = set(profile["case_types"][case["case_type"]]["required_documents"])
    present = set(case["documents"])
    missing_docs = sorted(required - present)
    if r2["status"] == "NEED_DATA":
        steps = [
            "Установить, является ли полученный комплект материалов полным.",
            "При необходимости запросить опись и недостающие материалы разрешительного дела.",
        ]
        if missing_docs:
            steps.append("В полученных сведениях не отмечены: " + ", ".join(missing_docs) + ". Пока это не доказанное отсутствие документов.")
        add("R2", steps)
    elif r2["status"] == "FLAG":
        add("R2", [
            "Сверить перечень обязательных документов с нормативными требованиями на дату обращения.",
            "Запросить оригиналы или подтверждённые копии: " + ", ".join(missing_docs) + ".",
            "Уточнить, предусмотрено ли для конкретной категории работ законное исключение.",
        ])

    r3 = by_rule["R3"]
    if r3["status"] == "NEED_DATA":
        required_dates = []
        if not case["issued_date"]:
            required_dates.append("дату выдачи разрешения")
        if not case["predecision_date"]:
            required_dates.append("дату обязательного предварительного решения")
        add("R3", [
            "Получить " + " и ".join(required_dates) + " из первичных документов.",
            "Сопоставить даты с журналом регистрации и действовавшим порядком выдачи.",
        ])
    elif r3["status"] == "FLAG":
        add("R3", [
            "Сопоставить исходные документы, подписи и регистрационные отметки по обеим датам.",
            "Проверить, что предварительное решение действительно являлось обязательным для данного случая.",
            "Запросить пояснение ответственного подразделения по расхождению дат.",
        ])

    # Сначала формальные отклонения, затем недостаточность данных — не оценка виновности.
    return sorted(plan, key=lambda item: (0 if item["status"] == "FLAG" else 1, item["rule"]))


def format_review_sheet(case: dict, profile: dict, outcomes: list[dict] | None = None,
                        *, include_placeholder: bool = True) -> str:
    """Текстовый контрольный лист; одинаковые входные данные дают одинаковый текст."""
    if outcomes is None:
        outcomes = analyze_case(case, profile)
    plan = build_review_plan(case, profile, outcomes)
    lines = [
        "ЗЕЛЁНЫЙ КОНТРОЛЬ — ЛИСТ ПРЕДВАРИТЕЛЬНОЙ ПРОВЕРКИ",
        "Дело: " + case_label(case["case_id"]),
        "Категория: " + category_label(case["case_type"], profile),
        profile_label(profile["version"]) + " · " + profile["status"],
        "",
        "РЕЗУЛЬТАТЫ ПРАВИЛ",
    ]
    for outcome in outcomes:
        lines.extend([
            f"{RULE_LABELS[outcome['rule']]} — {STATUS_LABELS[outcome['status']]}: {model_text(outcome['title'])}",
            "Основание сигнала: " + model_text(outcome["explanation"]),
            "Исходные сведения:\n" + evidence_text(outcome, case, profile),
            "Условие профиля: " + model_text(outcome["condition"]),
            "Ограничение: " + model_text(outcome["limitation"]),
            "",
        ])
    lines.append("ДЕЙСТВИЯ СПЕЦИАЛИСТА")
    if not plan:
        lines.append("По применённым модельным правилам сигналы и недостаточность данных не выявлены.")
    for item in plan:
        lines.append(f"{RULE_LABELS[item['rule']]} — {STATUS_LABELS[item['status']]}")
        for number, step in enumerate(item["steps"], start=1):
            lines.append(f"  {number}. {model_text(step)}")
        lines.append("")
    if include_placeholder:
        lines.extend([
            "ВЫВОД И ОТМЕТКА СПЕЦИАЛИСТА",
            "Решение: ______________________________________________",
            "Обоснование: ___________________________________________",
            "",
        ])
    lines.extend([
        "ВНИМАНИЕ: правила являются модельными, не заменяют нормативный акт и юридическое заключение.",
        "Выявленный сигнал не является доказательством коррупции или виновности лица.",
    ])
    return "\n".join(lines) + "\n"
