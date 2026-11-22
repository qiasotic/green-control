"""Приём структурированной описи обследований без повторного ввода дела."""
import json

from core import storage
from core.importer import read_frame
from core.municipal import clean_field

COLUMNS = ("case_id", "act_number", "act_site", "act_trees")


def preview(source, filename, cases):
    frame = read_frame(source, filename)
    if not set(COLUMNS).issubset(frame.columns):
        raise ValueError("Нужны колонки " + ", ".join(COLUMNS) + ".")
    known = {c["case_id"]: c for c in cases}
    seen, plan = set(), []
    for line, row in enumerate(frame.to_dict("records"), 2):
        cid = row["case_id"]
        if not isinstance(cid, str) or cid.strip() not in known:
            raise ValueError(f"Строка {line}: сначала загрузите дело с таким текстовым номером.")
        cid = cid.strip()
        if cid in seen:
            raise ValueError(f"Строка {line}: на дело допускается один сопоставляемый акт.")
        seen.add(cid)
        fields = {k: clean_field(k, row[k]) for k in COLUMNS[1:]}
        if any(v == "" for v in fields.values()):
            raise ValueError(f"Строка {line}: заполните номер акта, код участка и количество деревьев.")
        plan.append((cid, known[cid]["_version"], fields))
    return plan


def save(plan, filename, profile):
    changed = 0
    with storage._database(write=True) as con:
        for cid, version, fields in plan:
            row = storage._case_row(con, cid, version)
            payload = {**json.loads(row["payload"]), **fields}
            changed += storage._write_case(con, payload, row["source"], profile, storage._now(), mode="edit",
                                           draft=bool(row["is_draft"]), expected_version=version,
                                           revision_event="Сверка акта обследования. " + filename)
    return changed

