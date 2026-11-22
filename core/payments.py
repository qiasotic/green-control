"""Сверка одной накопительной выписки с реестром. Денежная арифметика в копейках."""
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
import hashlib
import json
import re

from core import storage
from core.importer import read_frame
from core.models import _date
from core.municipal import stage, STAGES

OK, DIFF, CHECK = "Оплата подтверждена", "Есть расхождение", "Нужна сверка"
COLUMNS = ("as_of", "case_id", "charge_id", "paid_total")


def parse(source, name):
    frame = read_frame(source, name)
    if not {"as_of", "paid_total"} <= set(frame) or not {"case_id", "charge_id"}.intersection(frame):
        raise ValueError("Нужны as_of, paid_total и хотя бы одна колонка case_id или charge_id.")
    dates, rows = set(), []
    for number, item in enumerate(frame.to_dict("records"), 2):
        try:
            day = _date(item["as_of"], "Дата выписки")
            if not day:
                raise ValueError("Укажите дату выписки.")
            keys = [item.get(k, "") for k in ("case_id", "charge_id")]
            if any(not isinstance(k, str) or (k and not k.isprintable()) or len(k.strip()) > 100 for k in keys):
                raise ValueError("Номера храните как текст, включая начальные нули.")
            amount = str(item["paid_total"]).strip().replace(" ", "").replace(",", ".")
            if amount and not re.fullmatch(r"[0-9]{1,12}(\.[0-9]{1,2})?", amount):
                raise ValueError("Укажите накопительный итог в рублях, до двух знаков после запятой.")
            rows.append({"line": number, "case_id": keys[0].strip(), "charge_id": keys[1].strip(),
                         "cents": int(Decimal(amount) * 100) if amount else None})
            dates.add(day)
        except ValueError as exc:
            raise ValueError(f"Строка {number}: {exc}") from exc
    if len(dates) != 1:
        raise ValueError("Все строки одной выписки должны иметь общую дату среза.")
    return dates.pop(), rows


def _requires_payment(case, profile):
    kind = profile["case_types"].get(case.get("case_type"), {})
    return (kind.get("payment_mode") == "required" and not case.get("exemption_code")
            and case.get("finance_mode") != "exemption")


def reconcile(cases, profile, day, records):
    by_id = {c["case_id"]: c for c in cases}
    charges = defaultdict(set)
    for c in cases:
        if c.get("charge_id"):
            charges[c["charge_id"]].add(c["case_id"])
    candidates, problems = defaultdict(list), []
    file_charges = Counter(r["charge_id"] for r in records if r["charge_id"])
    for row in records:
        cid, charge = row["case_id"], row["charge_id"]
        ids = charges.get(charge, set())
        known_case = cid in by_id
        # Fallback by case is valid only when both sources do not carry contradictory keys.
        repeated_charge = charge and file_charges[charge] > 1
        if repeated_charge:
            row = {**row, "conflict": True}
        if len(ids) == 1 and (not cid or cid in ids):
            candidates[next(iter(ids))].append(row)
        elif known_case and not ids and (not charge or not by_id[cid].get("charge_id")):
            candidates[cid].append(row)
        else:
            problems.append({"case_id": None, "file_case": cid, "charge_id": charge, "line": str(row["line"]),
                             "status": CHECK, "reason": "Не удалось однозначно связать номера выписки и реестра.",
                             "external": row["cents"] / 100 if row["cents"] is not None else None})
            for affected in ids | ({cid} if known_case else set()):
                candidates[affected].append({**row, "conflict": True})
    results = []
    for cid in sorted(by_id):
        c = by_id[cid]
        linked = candidates.get(cid, [])
        payable = _requires_payment(c, profile)
        if not linked and (not payable or (c.get("application_date") or "") > day):
            continue
        supplied = linked[0]["cents"] if len(linked) == 1 and not linked[0].get("conflict") else None
        amount = supplied / 100 if supplied is not None else None
        transferable = False
        status = CHECK
        if not linked:
            reason = "В выписке нет строки по делу. Поступившая сумма неизвестна."
        elif len(linked) != 1 or linked[0].get("conflict"):
            reason = "Повторные строки или противоречащие ключи. Уточните соответствие и итог."
        elif not payable:
            reason = "Поступление по делу с освобождением или неизвестной категорией. Уточните основание."
        elif (c.get("application_date") or "") > day:
            reason = "Дата выписки раньше даты заявления. Проверьте принадлежность платежа."
        elif linked[0]["cents"] is None:
            reason = "В выписке не указана сумма."
        else:
            cents = linked[0]["cents"]
            amount = cents / 100
            due = c.get("accrued_amount")
            transferable = True
            if due is None or due <= 0:
                reason = "Уточните начисление в карточке."
            else:
                difference = cents - int(Decimal(str(due)) * 100)
                status = OK if difference == 0 else DIFF
                if difference == 0:
                    reason = "Итог поступлений равен начислению на дату выписки."
                else:
                    label = "Недоплата" if difference < 0 else "Переплата"
                    reason = f"{label} на дату выписки: {abs(difference) / 100:.2f} ₽."
        results.append({"case_id": cid, "version": c["_version"], "stage": STAGES[stage(c)],
                        "charge_id": c.get("charge_id") or (linked[0]["charge_id"] if len(linked) == 1 else ""),
                        "line": ", ".join(str(r["line"]) for r in linked), "status": status,
                        "reason": reason, "due": c.get("accrued_amount"), "card_paid": c.get("paid_amount"),
                        "external": amount, "transferable": transferable})
    return results + problems


def batches():
    with storage._database() as con:
        return [dict(r) for r in con.execute("SELECT * FROM payment_batches ORDER BY as_of DESC")]


def results(batch_id):
    with storage._database() as con:
        return [json.loads(r[0]) for r in con.execute(
            "SELECT result FROM payment_results WHERE batch_id=? ORDER BY id", (batch_id,))]


def latest_by_case():
    history = batches()
    if not history:
        return {}, None
    latest = history[0]
    return {r["case_id"]: r for r in results(latest["id"]) if r["case_id"]}, latest


def _cases(con):
    return [{**json.loads(r["payload"]), "_version": r["version"]}
            for r in con.execute("SELECT payload,version FROM cases")]


def save(day, records, name, preview, profile):
    date.fromisoformat(day)
    digest = hashlib.sha256(storage._json(records).encode()).hexdigest()
    with storage._database(write=True) as con:
        existing = con.execute("SELECT * FROM payment_batches WHERE as_of=?", (day,)).fetchone()
        if existing and existing["digest"] == digest:
            return existing["id"]
        latest = con.execute("SELECT MAX(as_of) FROM payment_batches").fetchone()[0]
        if latest and day <= latest:
            raise ValueError("Для следующей выписки нужна более поздняя дата полного среза.")
        verified = reconcile(_cases(con), profile, day, records)
        if verified != preview:
            raise ValueError("Дела изменились. Обновите предварительную сверку.")
        cursor = con.execute("INSERT INTO payment_batches(as_of,source,digest,saved_at) VALUES (?,?,?,?)",
                             (day, name, digest, storage._now()))
        batch = cursor.lastrowid
        con.executemany("INSERT INTO payment_results(batch_id,case_id,result) VALUES (?,?,?)",
                        [(batch, r["case_id"], storage._json(r)) for r in verified])
        return batch


def apply(batch_id, case_id, profile):
    with storage._database(write=True) as con:
        latest = con.execute("SELECT * FROM payment_batches ORDER BY as_of DESC LIMIT 1").fetchone()
        if not latest or latest["id"] != batch_id:
            raise ValueError("Откройте последний срез сверки.")
        record = con.execute("SELECT result FROM payment_results WHERE batch_id=? AND case_id=?",
                             (batch_id, case_id)).fetchone()
        if not record:
            raise ValueError("Сопоставление отсутствует.")
        match = json.loads(record[0])
        if not match.get("transferable"):
            raise ValueError("Требуется ручная сверка.")
        row = storage._case_row(con, case_id, match["version"])
        case = json.loads(row["payload"])
        if match["charge_id"]:
            others = con.execute("SELECT 1 FROM cases WHERE case_id<>? AND json_extract(payload,'$.charge_id')=?",
                                 (case_id, match["charge_id"])).fetchone()
            if others:
                raise ValueError("Номер начисления теперь встречается в другом деле. Повторите сверку.")
        if case.get("paid_amount") == match["external"]:
            return False
        case["paid_amount"] = match["external"]
        case["finance_mode"] = "payment"
        note = f"Выписка {latest['source']} на {latest['as_of']}, строка {match['line']}."
        changed = storage._write_case(con, case, row["source"], profile, storage._now(), mode="edit",
                                      draft=bool(row["is_draft"]), expected_version=row["version"],
                                      revision_event="Перенос оплаты. " + note)
        if changed:
            match["applied_version"] = row["version"] + 1
            con.execute("UPDATE payment_results SET result=? WHERE batch_id=? AND case_id=?",
                        (storage._json(match), batch_id, case_id))
        return changed


def change_label(current, previous):
    if not previous:
        return "Впервые в сверке"
    if current["status"] == OK and previous["status"] != OK:
        return "Оплата подтвердилась"
    if current["status"] != previous["status"] or any(current.get(k) != previous.get(k)
            for k in ("external", "due", "reason", "charge_id")):
        return "Результат изменился"
    return "Без изменений"

