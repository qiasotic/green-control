"""Транзакционное хранение разрешительных дел и истории проверки."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from core.guidance import build_review_plan
from core.models import CONTROL_CHARACTERS, load_profile, normalize_case
from core.rules import analyze_case
from core.municipal import FIELDS
from core.presentation import case_label

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("GREEN_CONTROL_DB", ROOT / "data" / "green_control.sqlite3"))
ACTOR = "Локальный оператор"
WORKFLOW = {
    "new": "Новое",
    "in_review": "На рассмотрении",
    "awaiting_info": "Ожидает сведений",
    "closed": "Завершено",
}
TRANSITIONS = {
    "new": ("in_review", "awaiting_info", "closed"),
    "in_review": ("awaiting_info", "closed"),
    "awaiting_info": ("in_review", "closed"),
    "closed": ("in_review",),
}
ACTIONS = (
    "Запросить сведения", "Отметить рассмотренным",
    "Отклонение подтверждено документально", "Сигнал снят",
)
DECISIONS = ("Отклонение подтверждено документально", "Сигнал снят",
             "Отклонений по итогам проверки не выявлено")
EMPTY_REVIEW = {"decision": "Не проверено", "note": "", "reviewed_at": ""}
LEGACY_TABLES = (
    """CREATE TABLE IF NOT EXISTS cases (
        case_id TEXT PRIMARY KEY, payload TEXT NOT NULL, source TEXT NOT NULL, saved_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS reviews (
        case_id TEXT PRIMARY KEY, decision TEXT NOT NULL, note TEXT NOT NULL, reviewed_at TEXT NOT NULL
    )""",
)
VERSIONED_TABLES = (
    """CREATE TABLE case_versions (
        case_id TEXT NOT NULL REFERENCES cases(case_id), version INTEGER NOT NULL,
        payload TEXT NOT NULL, source TEXT NOT NULL, is_draft INTEGER NOT NULL,
        event TEXT NOT NULL, occurred_at TEXT NOT NULL, actor TEXT NOT NULL,
        PRIMARY KEY (case_id, version)
    )""",
    """CREATE TABLE case_workflow_events (
        id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, version INTEGER NOT NULL,
        from_state TEXT, to_state TEXT NOT NULL, note TEXT NOT NULL, occurred_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        FOREIGN KEY (case_id, version) REFERENCES case_versions(case_id, version)
    )""",
    """CREATE TABLE review_history (
        id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, version INTEGER NOT NULL,
        decision TEXT NOT NULL, note TEXT NOT NULL, reviewed_at TEXT NOT NULL, actor TEXT NOT NULL,
        FOREIGN KEY (case_id, version) REFERENCES case_versions(case_id, version)
    )""",
    """CREATE TABLE analysis_runs (
        case_id TEXT NOT NULL, version INTEGER NOT NULL,
        profile_id TEXT NOT NULL, profile_version TEXT NOT NULL, profile_status TEXT NOT NULL,
        outcomes TEXT NOT NULL, plan TEXT NOT NULL, analyzed_at TEXT NOT NULL,
        PRIMARY KEY (case_id, version, profile_id, profile_version),
        FOREIGN KEY (case_id, version) REFERENCES case_versions(case_id, version)
    )""",
    """CREATE TABLE rule_actions (
        id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, version INTEGER NOT NULL,
        profile_id TEXT NOT NULL, profile_version TEXT NOT NULL, rule TEXT NOT NULL,
        action TEXT NOT NULL, note TEXT NOT NULL, occurred_at TEXT NOT NULL, actor TEXT NOT NULL,
        FOREIGN KEY (case_id, version, profile_id, profile_version)
            REFERENCES analysis_runs(case_id, version, profile_id, profile_version)
    )""",
    "CREATE INDEX workflow_case ON case_workflow_events(case_id, version)",
    "CREATE INDEX review_case ON review_history(case_id, version, id DESC)",
    "CREATE INDEX rule_action_case ON rule_actions(case_id, version)",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False)


@contextmanager
def _database(*, write=False):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
    finally:
        connection.close()


def _migrate(connection):
    for statement in LEGACY_TABLES:
        connection.execute(statement)
    connection.execute("ALTER TABLE cases ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    connection.execute("ALTER TABLE cases ADD COLUMN state TEXT NOT NULL DEFAULT 'new'")
    connection.execute("ALTER TABLE cases ADD COLUMN is_draft INTEGER NOT NULL DEFAULT 0")
    connection.execute("ALTER TABLE cases ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
    connection.execute("UPDATE cases SET updated_at=saved_at")
    for statement in VERSIONED_TABLES:
        connection.execute(statement)
    connection.execute("""
        INSERT INTO case_versions(case_id,version,payload,source,is_draft,event,occurred_at,actor)
        SELECT case_id,version,payload,source,is_draft,'Перенос из v1.4',saved_at,? FROM cases
    """, (ACTOR,))
    connection.execute("""
        INSERT INTO case_workflow_events(case_id,version,from_state,to_state,note,occurred_at,actor)
        SELECT case_id,version,NULL,'new','Перенос из v1.4',saved_at,? FROM cases
    """, (ACTOR,))
    connection.execute("""
        INSERT INTO review_history(case_id,version,decision,note,reviewed_at,actor)
        SELECT case_id,1,decision,note,reviewed_at,? FROM reviews
        WHERE case_id IN (SELECT case_id FROM cases)
    """, (ACTOR,))
    for row in connection.execute("""
        SELECT cases.*, reviews.decision, reviews.note, reviews.reviewed_at
        FROM cases JOIN reviews USING(case_id)
    """).fetchall():
        state = {
            "Запросить документы": "awaiting_info",
            "Подтверждено несоответствие": "closed",
            "Сигнал снят": "closed",
        }.get(row["decision"])
        if state:
            note = row["note"] or "Перенесено решение из v1.4: " + row["decision"]
            _transition(connection, row, state, note, row["reviewed_at"])


def _profile_key(profile):
    return profile["profile_id"], str(profile["version"])


def _store_analysis(connection, case_id, version, payload, profile, now):
    kind = profile["case_types"].get(payload.get("case_type"))
    if kind is None:
        # Старый профиль мог содержать категории, которых больше нет в действующем.
        return
    outcomes = analyze_case(payload, profile)
    plan = build_review_plan(payload, profile, outcomes)
    connection.execute("""
        INSERT OR IGNORE INTO analysis_runs
        (case_id,version,profile_id,profile_version,profile_status,outcomes,plan,analyzed_at)
        VALUES (?,?,?,?,?,?,?,?)
    """, (case_id, version, *_profile_key(profile), profile["status"],
          _json(outcomes), _json(plan), now))


def initialize(profile=None) -> None:
    profile = load_profile() if profile is None else profile
    with _database(write=True) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        )""")
        now = _now()
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone() is None:
            _migrate(connection)
            connection.execute("INSERT INTO schema_migrations VALUES (1,?)", (now,))
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone() is None:
            connection.execute("""CREATE TABLE analysis_rechecks (
                id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, version INTEGER NOT NULL,
                profile_id TEXT NOT NULL, profile_version TEXT NOT NULL,
                occurred_at TEXT NOT NULL, actor TEXT NOT NULL,
                FOREIGN KEY (case_id, version, profile_id, profile_version)
                    REFERENCES analysis_runs(case_id, version, profile_id, profile_version)
            )""")
            connection.execute("CREATE INDEX rechecks_case ON analysis_rechecks(case_id, version)")
            connection.execute("INSERT INTO schema_migrations VALUES (2,?)", (now,))
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone() is None:
            connection.execute("""CREATE TABLE github_imports (
                id INTEGER PRIMARY KEY, repository TEXT NOT NULL, path TEXT NOT NULL, ref TEXT NOT NULL,
                blob_sha TEXT NOT NULL, digest TEXT NOT NULL, received_at TEXT NOT NULL,
                imported_at TEXT NOT NULL, mapping TEXT NOT NULL,
                added INTEGER NOT NULL, updated INTEGER NOT NULL, skipped INTEGER NOT NULL,
                UNIQUE (repository,path,ref,digest)
            )""")
            connection.execute("INSERT INTO schema_migrations VALUES (3,?)", (now,))
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone() is None:
            connection.execute("""CREATE TABLE payment_batches (
                id INTEGER PRIMARY KEY, as_of TEXT NOT NULL UNIQUE, source TEXT NOT NULL,
                digest TEXT NOT NULL, saved_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE payment_results (
                id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES payment_batches(id) ON DELETE CASCADE,
                case_id TEXT REFERENCES cases(case_id) ON DELETE CASCADE, result TEXT NOT NULL,
                UNIQUE(batch_id,case_id))""")
            connection.execute("INSERT INTO schema_migrations VALUES (4,?)", (now,))
        pending = connection.execute("""
            SELECT c.case_id,c.version,c.payload FROM cases c
            LEFT JOIN analysis_runs a ON a.case_id=c.case_id AND a.version=c.version
                AND a.profile_id=? AND a.profile_version=?
            WHERE a.case_id IS NULL
        """, _profile_key(profile)).fetchall()
        for row in pending:
            payload = json.loads(row["payload"])
            if payload.get("case_type") in profile["case_types"]:
                _store_analysis(connection, row["case_id"], row["version"], payload, profile, now)


def _case_row(connection, case_id, expected_version=None, expected_state=None):
    row = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        raise ValueError("Дело не найдено.")
    if ((expected_version is not None and row["version"] != expected_version) or
            (expected_state is not None and row["state"] != expected_state)):
        raise ValueError("Дело изменилось. Откройте актуальную карточку и повторите действие.")
    return row


def _transition(connection, row, state, note, now):
    connection.execute(
        "UPDATE cases SET state=?,updated_at=MAX(updated_at,?) WHERE case_id=?",
        (state, now, row["case_id"]),
    )
    connection.execute("""
        INSERT INTO case_workflow_events
        (case_id,version,from_state,to_state,note,occurred_at,actor) VALUES (?,?,?,?,?,?,?)
    """, (row["case_id"], row["version"], row["state"], state, note, now, ACTOR))


def _write_case(connection, raw, source, profile, now, *, mode, draft=False, expected_version=None,
                revision_event=None):
    case_id = str(raw["case_id"]).strip()
    prior = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if mode == "edit":
        prior = _case_row(connection, case_id, expected_version)
    if mode == "create" and prior is not None:
        raise ValueError(f"Дело с номером «{case_label(case_id)}» уже есть. Откройте его для редактирования.")
    candidate = normalize_case(raw, profile)
    needs_completion = mode == "create" or (mode == "edit" and prior["is_draft"])
    if needs_completion and not draft and not candidate["application_date"]:
        raise ValueError("Укажите дату заявления для завершения ввода или сохраните черновик.")
    if prior is not None:
        original = json.loads(prior["payload"])
        # An older file can update a case without supplying the municipal fields.
        for key in FIELDS:
            if key not in raw and key in original:
                candidate[key] = original[key]
        if candidate.get("permit_stage") and candidate.get("issued_date"):
            candidate["permit_stage"] = "issued"
        try:
            comparable = normalize_case(original, profile)
        except ValueError:
            comparable = original
        if (comparable == candidate
                and (mode == "import" or bool(prior["is_draft"]) == draft)):
            return False
        if mode == "import":
            raise ValueError(f"Дело «{case_label(case_id)}» уже есть и содержит другие сведения. "
                             "Для замены подтвердите обновление существующих дел.")
        from_original = {key: value for key, value in original.items()
                         if key not in ("finance_mode", "payment_note") and key not in FIELDS}
        candidate = {**from_original, **candidate}
        if mode == "edit":
            source = prior["source"]
    payload = _json(candidate)
    if prior is None:
        version, state = 1, "new"
        connection.execute("""
            INSERT INTO cases(case_id,payload,source,saved_at,updated_at,is_draft) VALUES (?,?,?,?,?,?)
        """, (case_id, payload, source, now, now, int(draft)))
        event = "Создание черновика" if draft else "Создание дела"
    else:
        version, state = prior["version"] + 1, prior["state"]
        connection.execute("""
            UPDATE cases SET payload=?,source=?,saved_at=?,updated_at=?,version=?,is_draft=?
            WHERE case_id=?
        """, (payload, source, now, now, version, int(draft), case_id))
        event = "Изменение карточки"
        if prior["is_draft"] and not draft:
            event = "Завершение ввода"
    connection.execute("""
        INSERT INTO case_versions(case_id,version,payload,source,is_draft,event,occurred_at,actor)
        VALUES (?,?,?,?,?,?,?,?)
    """, (case_id, version, payload, source, int(draft), revision_event or event, now, ACTOR))
    if prior is None or state in ("awaiting_info", "closed"):
        connection.execute("""
            INSERT INTO case_workflow_events
            (case_id,version,from_state,to_state,note,occurred_at,actor) VALUES (?,?,?,?,?,?,?)
        """, (case_id, version, state if prior is not None else None,
              "in_review" if prior is not None else "new",
              "Изменены сведения. Требуется повторная проверка." if prior is not None else event,
              now, ACTOR))
        if prior is not None:
            connection.execute("UPDATE cases SET state='in_review' WHERE case_id=?", (case_id,))
    _store_analysis(connection, case_id, version, candidate, profile, now)
    return True


def save_case(case: dict, source: str, *, profile=None, mode="upsert", draft=False,
              expected_version=None) -> bool:
    profile = load_profile() if profile is None else profile
    with _database(write=True) as connection:
        return _write_case(connection, case, source, profile, _now(), mode=mode, draft=draft,
                           expected_version=expected_version)


def save_cases(cases: list[dict], source: str, *, profile=None, update_existing=False) -> int:
    """Одна транзакция на файл. Совпадающие карточки пропускаются."""
    profile = load_profile() if profile is None else profile
    ids = [str(case["case_id"]).strip() for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("В выгрузке повторяется номер дела.")
    mode = "upsert" if update_existing else "import"
    with _database(write=True) as connection:
        now = _now()
        return sum(_write_case(connection, case, source, profile, now, mode=mode) for case in cases)


def _delete_cases(connection, case_ids):
    # Delete dependents before their parent records while foreign keys remain enforced.
    arguments = [(case_id,) for case_id in case_ids]
    for table in ("analysis_rechecks", "rule_actions", "analysis_runs", "review_history",
                  "case_workflow_events", "case_versions", "reviews", "cases"):
        connection.executemany(f"DELETE FROM {table} WHERE case_id=?", arguments)


def delete_case(case_id, *, expected_version, expected_updated_at):
    """Удаляет подтверждённую карточку и все связанные записи в одной транзакции."""
    with _database(write=True) as connection:
        row = _case_row(connection, case_id, expected_version)
        if row["updated_at"] != expected_updated_at:
            raise ValueError("Дело изменилось. Проверьте актуальную карточку перед удалением.")
        _delete_cases(connection, (case_id,))


def delete_dataset(source, expected):
    """Удаляет только дела с выбранным источником в текущей редакции."""
    if not source or source == "Ручной ввод":
        raise ValueError("Для удаления набора выберите источник импорта.")
    with _database(write=True) as connection:
        rows = connection.execute("""
            SELECT case_id, version, updated_at FROM cases WHERE source=? ORDER BY case_id
        """, (source,)).fetchall()
        if not rows or tuple(map(tuple, rows)) != expected:
            raise ValueError("Состав набора изменился. Сверьте список дел и подтвердите удаление повторно.")
        case_ids = tuple(row["case_id"] for row in rows)
        _delete_cases(connection, case_ids)
        if source.startswith("GitHub · "):
            connection.execute("""
                DELETE FROM github_imports WHERE 'GitHub · ' || repository || ' · ' || path = ?
            """, (source,))
        return case_ids


def _github_plan(connection, records, profile):
    if not records:
        raise ValueError("В файле нет пригодных к импорту записей.")
    plan = []
    seen = set()
    for raw in records:
        normalized = normalize_case(raw, profile)
        case_id = normalized["case_id"]
        if case_id in seen:
            raise ValueError("В выгрузке повторяется номер дела.")
        seen.add(case_id)
        prior = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if prior is None:
            plan.append((case_id, None, "new", (), normalized, False))
            continue
        original = json.loads(prior["payload"])
        comparable = normalize_case(original, profile)
        candidate = normalize_case({**original, **raw}, profile)
        differences = tuple(key for key in comparable.keys() | candidate.keys()
                            if comparable.get(key) != candidate.get(key))
        plan.append((case_id, prior["version"], "update" if differences else "same", tuple(sorted(differences)),
                     candidate, bool(prior["is_draft"])))
    return plan


def _plan_signature(plan):
    return tuple((case_id, version, kind, fields) for case_id, version, kind, fields, _, _ in plan)


def preview_github_import(records, profile):
    with _database() as connection:
        return _plan_signature(_github_plan(connection, records, profile))


def github_import_history():
    with _database() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM github_imports ORDER BY id DESC")]


def save_github_import(records, metadata, mapping, preview, profile, *, update_existing=False):
    """Атомарно сохраняет подтверждённый снимок файла и изменённые карточки."""
    source_fields = ("repository", "path", "ref", "blob_sha", "digest", "received_at")
    values = tuple(metadata[key] for key in source_fields)
    with _database(write=True) as connection:
        duplicate = connection.execute("""
            SELECT 1 FROM github_imports WHERE repository=? AND path=? AND ref=? AND digest=?
        """, (*values[:3], metadata["digest"])).fetchone()
        if duplicate:
            return {"new": 0, "update": 0, "same": len(records), "seen": True}
        plan = _github_plan(connection, records, profile)
        if _plan_signature(plan) != preview:
            raise ValueError("Дела изменились после проверки. Выполните предварительную проверку повторно.")
        if not update_existing and any(row[2] == "update" for row in plan):
            raise ValueError("Для обновления существующих дел нужно отдельное подтверждение.")
        counts = {"new": 0, "update": 0, "same": 0, "seen": False}
        now = _now()
        source = f"GitHub · {metadata['repository']} · {metadata['path']}"
        for _, _, kind, _, candidate, draft in plan:
            if kind != "same":
                _write_case(connection, candidate, source, profile, now, mode="upsert", draft=draft)
            counts[kind] += 1
        connection.execute("""
            INSERT INTO github_imports(repository,path,ref,blob_sha,digest,received_at,imported_at,mapping,added,updated,skipped)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (*values, now, _json(mapping), counts["new"], counts["update"], counts["same"]))
    return counts


def _analysis(row):
    run = dict(row)
    run["outcomes"] = json.loads(run["outcomes"])
    run["plan"] = json.loads(run["plan"])
    return run


def all_cases(profile=None) -> list[dict]:
    profile = load_profile() if profile is None else profile
    with _database() as connection:
        runs = {row["case_id"]: _analysis(row) for row in connection.execute("""
            SELECT a.* FROM analysis_runs a JOIN cases c USING(case_id, version)
            WHERE a.profile_id=? AND a.profile_version=?
        """, _profile_key(profile))}
        rows = connection.execute("""
            SELECT c.*, EXISTS(
                SELECT 1 FROM review_history old WHERE old.case_id=c.case_id AND old.version<c.version
            ) AS prior_review
            FROM cases c ORDER BY c.case_id
        """).fetchall()
        return [
            {**json.loads(row["payload"]), "_source": row["source"], "_saved_at": row["saved_at"],
             "_updated_at": row["updated_at"], "_version": row["version"], "_state": row["state"],
             "_draft": bool(row["is_draft"]), "_prior_review": bool(row["prior_review"]),
             "_analysis": runs.get(row["case_id"])}
            for row in rows
        ]


def _recheck(connection, case_id, expected_version, profile, now):
    row = _case_row(connection, case_id, expected_version)
    payload = json.loads(row["payload"])
    if payload.get("case_type") not in profile["case_types"]:
        raise ValueError("Категория отсутствует в действующем профиле.")
    key = (case_id, row["version"], *_profile_key(profile))
    current = connection.execute("""
        SELECT * FROM analysis_runs WHERE case_id=? AND version=? AND profile_id=? AND profile_version=?
    """, key).fetchone()
    if current is None:
        _store_analysis(connection, *key[:2], payload, profile, now)
    else:
        saved = _analysis(current)
        outcomes = analyze_case(payload, profile)
        if (outcomes != saved["outcomes"] or profile["status"] != saved["profile_status"]
                or build_review_plan(payload, profile, outcomes) != saved["plan"]):
            raise ValueError("Повторный расчёт отличается. Проверьте код и версию модельного профиля.")
    connection.execute("""
        INSERT INTO analysis_rechecks(case_id,version,profile_id,profile_version,occurred_at,actor)
        VALUES (?,?,?,?,?,?)
    """, (*key, now, ACTOR))


def recheck_analyses(selection, profile):
    """Пакет повторных расчётов и отметок сохраняется одной транзакцией."""
    with _database(write=True) as connection:
        now = _now()
        for case_id, version in selection:
            _recheck(connection, case_id, version, profile, now)


def recheck_analysis(case_id, profile, expected_version):
    recheck_analyses([(case_id, expected_version)], profile)

def _note(note, required=False):
    note = note.strip()
    if required and not note:
        raise ValueError("Укажите основание или комментарий к действию.")
    if CONTROL_CHARACTERS.search(note):
        raise ValueError("В комментарии есть недопустимые управляющие символы.")
    if len(note) > 1000:
        raise ValueError("Комментарий должен содержать до 1000 символов.")
    return note


def _write_review(connection, row, decision, note, now):
    connection.execute("""
        INSERT INTO review_history(case_id,version,decision,note,reviewed_at,actor)
        VALUES (?,?,?,?,?,?)
    """, (row["case_id"], row["version"], decision, note, now, ACTOR))
    connection.execute("UPDATE cases SET updated_at=? WHERE case_id=?", (now, row["case_id"]))


def save_review(case_id: str, decision: str, note: str) -> None:
    """Сохраняет самостоятельную отметку прежнего интерфейса в общем журнале."""
    if not decision.strip() or len(decision) > 160:
        raise ValueError("Укажите решение длиной до 160 символов.")
    with _database(write=True) as connection:
        row = _case_row(connection, case_id)
        _write_review(connection, row, decision, _note(note), _now())


def _current_review(connection, case_id):
    row = connection.execute("""
        SELECT r.decision,r.note,r.reviewed_at FROM review_history r JOIN cases c USING(case_id,version)
        WHERE r.case_id=? AND r.reviewed_at >= COALESCE((
            SELECT MAX(e.occurred_at) FROM case_workflow_events e
            WHERE e.case_id=c.case_id AND e.version=c.version AND e.from_state='closed'
        ), '') ORDER BY r.id DESC LIMIT 1
    """, (case_id,)).fetchone()
    return dict(row) if row is not None else EMPTY_REVIEW.copy()


def get_review(case_id: str) -> dict:
    with _database() as connection:
        return _current_review(connection, case_id)


def change_workflow(case_id, state, note, *, decision=None, expected_version=None, expected_state=None):
    with _database(write=True) as connection:
        row = _case_row(connection, case_id, expected_version, expected_state)
        if state not in TRANSITIONS[row["state"]]:
            raise ValueError("Этот переход недоступен для текущего состояния дела.")
        if state == "closed" and row["is_draft"]:
            raise ValueError("Завершите ввод черновика перед оформлением заключения.")
        note = _note(note, required=state in ("awaiting_info", "closed"))
        now = _now()
        if state == "closed":
            if decision not in DECISIONS:
                raise ValueError("Выберите заключение специалиста.")
            _write_review(connection, row, decision, note, now)
        _transition(connection, row, state, note, now)


def record_rule_action(case_id, rule, action, note, *, profile, expected_version, expected_state):
    if action not in ACTIONS:
        raise ValueError("Выберите действие по правилу.")
    note = _note(note, required=True)
    with _database(write=True) as connection:
        row = _case_row(connection, case_id, expected_version, expected_state)
        if row["state"] == "closed" or row["is_draft"]:
            raise ValueError("Для действий по индикаторам откройте рассмотрение заполненного дела.")
        found = connection.execute("""
            SELECT outcomes FROM analysis_runs WHERE case_id=? AND version=?
            AND profile_id=? AND profile_version=?
        """, (case_id, row["version"], *_profile_key(profile))).fetchone()
        if found is None or rule not in {result["rule"] for result in json.loads(found["outcomes"])}:
            raise ValueError("Актуальный результат по этому правилу отсутствует.")
        now = _now()
        connection.execute("""
            INSERT INTO rule_actions(case_id,version,profile_id,profile_version,rule,action,note,occurred_at,actor)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (case_id, row["version"], *_profile_key(profile), rule, action, note, now, ACTOR))
        connection.execute("UPDATE cases SET updated_at=? WHERE case_id=?", (now, case_id))
        state = row["state"]
        if action == ACTIONS[0] and state != "awaiting_info":
            _transition(connection, row, "awaiting_info", rule + " · " + note, now)
        elif state == "new":
            _transition(connection, row, "in_review", rule + " · " + note, now)


def history(case_id: str) -> dict:
    with _database() as connection:
        current = _case_row(connection, case_id)
        review = _current_review(connection, case_id)
        versions = [dict(row) for row in connection.execute("""
            SELECT * FROM case_versions WHERE case_id=? ORDER BY version DESC
        """, (case_id,))]
        for entry in versions:
            entry["payload"] = json.loads(entry["payload"])
        return {
            "current_version": current["version"],
            "current_review": review,
            "versions": versions,
            "analyses": [_analysis(row) for row in connection.execute("""
                SELECT * FROM analysis_runs WHERE case_id=? ORDER BY version DESC,analyzed_at DESC
            """, (case_id,))],
            "workflow": [dict(row) for row in connection.execute("""
                SELECT * FROM case_workflow_events WHERE case_id=? ORDER BY id DESC
            """, (case_id,))],
            "reviews": [dict(row) for row in connection.execute("""
                SELECT * FROM review_history WHERE case_id=? ORDER BY id DESC
            """, (case_id,))],
            "rechecks": [dict(row) for row in connection.execute("""
                SELECT *, 'Повторный расчёт R1–R3' AS event
                FROM analysis_rechecks WHERE case_id=? ORDER BY id DESC
            """, (case_id,))],
            "actions": [dict(row) for row in connection.execute("""
                SELECT * FROM rule_actions WHERE case_id=? ORDER BY id DESC
            """, (case_id,))],
        }


def recent_activity(limit=8) -> list[dict]:
    with _database() as connection:
        return [dict(row) for row in connection.execute("""
            SELECT case_id, occurred_at, event FROM case_versions
            UNION ALL
            SELECT case_id, occurred_at, to_state FROM case_workflow_events
                WHERE from_state IS NOT NULL
            UNION ALL
            SELECT case_id, reviewed_at, 'Заключение: ' || decision FROM review_history
            UNION ALL
            SELECT case_id, occurred_at, rule || ' · ' || action FROM rule_actions
            UNION ALL
            SELECT case_id, occurred_at, 'Повторный расчёт R1–R3' FROM analysis_rechecks
            ORDER BY occurred_at DESC LIMIT ?
        """, (limit,))]
