"""Transactional task state and explicit, single-use execution approvals."""
from contextlib import contextmanager
import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

POLICIES = {
    "refresh_evidence": {"codex": 0, "claude": 0, "seconds": 15, "retries": 0},
    "offline_tests": {"codex": 0, "claude": 0, "seconds": 1560, "retries": 0},   # model-free; ~9 min on a fast PC
    "codex_preflight": {"codex": 0, "claude": 0, "seconds": 120, "retries": 0},
    "codex_diagnostic": {"codex": 1, "claude": 0, "seconds": 60, "retries": 0},
    "integration": {"codex": 1, "claude": 1, "seconds": 120, "retries": 0},
    "collaboration": {"codex": 3, "claude": 3, "seconds": 120, "retries": 0, "max_correction_cycles": 2},
    "correction_validation": {"codex": 2, "claude": 2, "seconds": 120, "retries": 0, "max_correction_cycles": 1},
    "correction_preflight": {"codex": 0, "claude": 0, "seconds": 120, "retries": 0},
    "project_scan": {"codex": 0, "claude": 0, "seconds": 60, "retries": 0},
    "project_prepare": {"codex": 0, "claude": 0, "seconds": 120, "retries": 0},
    "project_validation_prepare": {"codex": 0, "claude": 0, "seconds": 120, "retries": 0},
    "project_preflight": {"codex": 0, "claude": 0, "seconds": 120, "retries": 0},
    "project_development": {"codex": 3, "claude": 3, "seconds": 120, "retries": 0, "max_correction_cycles": 2},
    "project_apply": {"codex": 0, "claude": 0, "seconds": 60, "retries": 0},
    "project_recover": {"codex": 0, "claude": 0, "seconds": 60, "retries": 0},
    # Real limits come from the product's approved budget (see approval_scope).
    "product_build": {"codex": 1, "claude": 1, "seconds": 300, "retries": 0},
    # Exact batch count is approval-bound below; no retries and no active-session wall-clock deadline.
    "milestone_review": {"codex": 0, "claude": 3, "seconds": None, "retries": 0},
    # Connect page: one zero-tool reviewer startup (stopped at initialization), then one tiny probe per model.
    "reviewer_profile": {"codex": 0, "claude": 1, "seconds": 15, "retries": 0},
    "claude_model_probe": {"codex": 0, "claude": 1, "seconds": 60, "retries": 0},
}


def needs_approval(kind):
    return bool(POLICIES[kind]['codex'] or POLICIES[kind]['claude'] or kind in ('project_apply', 'project_recover'))


def now():
    return datetime.datetime.now(datetime.UTC).isoformat()


def approval_scope(task):
    payload = task["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    value = {"id": task["id"], "kind": task["kind"], "objective": task["objective"], "payload": payload}
    limits = POLICIES[task['kind']]
    if task['kind'] == 'project_development':
        settings = payload['settings']
        limits = {'codex': settings['max_codex_sessions'], 'claude': settings['max_claude_sessions'],
            'seconds': settings['seconds_per_session'], 'max_correction_cycles': settings['max_correction_cycles'], 'retries': 0}
    if task['kind'] == 'product_build':
        limits = payload['limits']  # products.limits(budget); the worker re-derives and compares it
    if task['kind'] == 'milestone_review':
        batches = payload.get('review_batch_count', 1)
        if type(batches) is not int or not 1 <= batches <= POLICIES['milestone_review']['claude']:
            raise ValueError('INVALID_REVIEW_BATCH_ALLOWANCE')
        limits = {**limits, 'claude': batches}
    return {"limits": limits,
        "task_sha256": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()}


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 2:
                raise RuntimeError('STATE_DATABASE_NEWER_THAN_APPLICATION')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, objective TEXT NOT NULL,
                    state TEXT NOT NULL, payload TEXT NOT NULL, result TEXT,
                    created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    state TEXT NOT NULL, started TEXT NOT NULL, ended TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS approvals(
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id), fingerprint TEXT NOT NULL,
                    policy TEXT NOT NULL, approved TEXT NOT NULL, consumed TEXT);
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, occurred TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evidence(
                    path TEXT PRIMARY KEY, digest TEXT NOT NULL, summary TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS projects(
                    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, summary TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            expected = {'tasks': ('id', 'kind', 'objective', 'state', 'payload', 'result', 'created', 'updated'),
                'runs': ('id', 'task_id', 'state', 'started', 'ended', 'result'),
                'approvals': ('task_id', 'fingerprint', 'policy', 'approved', 'consumed'),
                'events': ('seq', 'task_id', 'kind', 'payload', 'occurred'),
                'evidence': ('path', 'digest', 'summary'), 'projects': ('id', 'path', 'summary', 'updated'),
                'settings': ('key', 'value')}
            for table, columns in expected.items():
                if tuple(r[1] for r in db.execute('PRAGMA table_info(' + table + ')')) != columns:
                    raise RuntimeError('STATE_DATABASE_SCHEMA_REQUIRES_MIGRATION')
            db.execute('PRAGMA user_version=2')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, task_id, kind, payload):
        db.execute("INSERT INTO events(task_id,kind,payload,occurred) VALUES(?,?,?,?)",
            (task_id, kind, json.dumps(payload), now()))

    def add(self, kind, objective, payload=None):
        if kind not in POLICIES:
            raise ValueError("UNKNOWN_WORKFLOW")
        if type(objective) is not str or not objective.strip() or len(objective) > 4000:
            raise ValueError('TASK_TEXT_MUST_BE_1_TO_4000_CHARACTERS')
        task_id = uuid.uuid4().hex
        state = "awaiting_approval" if needs_approval(kind) else "queued"
        with self.connection() as db:
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)",
                (task_id, kind, objective.strip(), state, json.dumps(payload or {}), None, now(), now()))
            self.event(db, task_id, "created", {"kind": kind, "policy": POLICIES[kind]})
        return task_id

    def get(self, task_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("TASK_NOT_FOUND")
            return self.decode(row)

    @staticmethod
    def decode(row):
        value = dict(row)
        for name in ("payload", "result"):
            if value.get(name):
                value[name] = json.loads(value[name])
        return value

    def tasks(self):
        with self.connection() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM tasks ORDER BY created DESC")]

    def approve(self, task_id, fingerprint):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["state"] != "awaiting_approval":
                raise ValueError("TASK_NOT_AWAITING_APPROVAL")
            previous = db.execute("SELECT consumed FROM approvals WHERE task_id=?", (task_id,)).fetchone()
            if previous and previous["consumed"]:
                raise ValueError("APPROVAL_ALREADY_CONSUMED_CREATE_NEW_TASK")
            db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,NULL)",
                (task_id, fingerprint, json.dumps(approval_scope(row)), now()))
            db.execute("UPDATE tasks SET state='queued',updated=? WHERE id=?", (now(), task_id))
            self.event(db, task_id, "explicit_human_approval", {"fingerprint": fingerprint, "policy": approval_scope(row)})

    def claim(self, fingerprint, allowed_kinds=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM tasks WHERE state='running'").fetchone():
                return None
            for row in db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created").fetchall():
                if allowed_kinds is not None and row["kind"] not in allowed_kinds:
                    continue
                if needs_approval(row['kind']):
                    approval = db.execute("SELECT * FROM approvals WHERE task_id=?", (row["id"],)).fetchone()
                    if (not approval or approval["consumed"] or approval["fingerprint"] != fingerprint
                            or json.loads(approval["policy"]) != approval_scope(row)):
                        db.execute("UPDATE tasks SET state='awaiting_approval',updated=? WHERE id=?", (now(), row["id"]))
                        self.event(db, row["id"], "approval_invalidated", {"reason": "POLICY_OR_CODE_CHANGED"})
                        continue
                    db.execute("UPDATE approvals SET consumed=? WHERE task_id=?", (now(), row["id"]))
                run_id = uuid.uuid4().hex
                db.execute("UPDATE tasks SET state='running',updated=? WHERE id=?", (now(), row["id"]))
                db.execute("INSERT INTO runs VALUES(?,?,?, ?,NULL,NULL)", (run_id, row["id"], "running", now()))
                self.event(db, row["id"], "started", {"run_id": run_id, "fingerprint": fingerprint})
                task = self.decode(row)
                task.update(state="running", run_id=run_id, fingerprint=fingerprint)
                return task
        return None

    def finish(self, task_id, run_id, state, result):
        if state not in ("completed", "failed", "blocked", "cancelled", "interrupted", "awaiting_acceptance", "paused_quota"):
            raise ValueError("INVALID_FINAL_STATE")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["state"] != "running":
                raise ValueError("TASK_NOT_RUNNING")
            if not db.execute("SELECT 1 FROM runs WHERE id=? AND task_id=? AND state='running'", (run_id, task_id)).fetchone():
                raise ValueError("RUN_NOT_ACTIVE_FOR_TASK")
            db.execute("UPDATE tasks SET state=?,result=?,updated=? WHERE id=?",
                (state, json.dumps(result), now(), task_id))
            db.execute("UPDATE runs SET state=?,result=?,ended=? WHERE id=? AND task_id=?",
                (state, json.dumps(result), now(), run_id, task_id))
            self.event(db, task_id, state, result)

    def record(self, task_id, kind, payload):
        with self.connection() as db:
            self.event(db, task_id, kind, payload)

    def decide(self, task_id, sha, accepted, *, independently_tested=False):
        """Human disposition of one exact reviewed revision; never merges or deploys."""
        if type(accepted) is not bool or type(independently_tested) is not bool:
            raise ValueError("INVALID_DECISION")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["state"] != "awaiting_acceptance":
                raise ValueError("TASK_NOT_AWAITING_ACCEPTANCE")
            result = json.loads(row["result"])
            if result.get("candidate_sha") != sha or result.get("verdict") != "PASS":
                raise ValueError("REVIEWED_REVISION_MISMATCH")
            state = "accepted" if accepted else "rejected"
            result["human_decision"] = {"state": state, "candidate_sha": sha, "occurred": now(),
                "independently_tested_by_human": independently_tested}
            db.execute("UPDATE tasks SET state=?,result=?,updated=? WHERE id=?", (state, json.dumps(result), now(), task_id))
            self.event(db, task_id, "human_" + state, result["human_decision"])

    def cancel_pending(self, task_id, *, reason="HUMAN_CANCELLED_BEFORE_EXECUTION"):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["state"] not in ("queued", "awaiting_approval"):
                raise ValueError("TASK_NOT_PENDING")
            db.execute("UPDATE tasks SET state='cancelled',updated=? WHERE id=?", (now(), task_id))
            self.event(db, task_id, "cancelled", {"reason": reason})

    def recover(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id FROM tasks WHERE state='running'").fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET state='interrupted',updated=? WHERE id=?", (now(), row["id"]))
                db.execute("UPDATE runs SET state='interrupted',ended=? WHERE task_id=? AND state='running'", (now(), row["id"]))
                self.event(db, row["id"], "interrupted", {"reason": "COORDINATOR_RESTART_NO_AUTOMATIC_MODEL_REPLAY"})
            return len(rows)

    def history(self, task_id):
        with self.connection() as db:
            return [{**dict(row), "payload": json.loads(row["payload"])}
                for row in db.execute("SELECT * FROM events WHERE task_id=? ORDER BY seq", (task_id,))]

    def import_evidence(self, path, digest, summary):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO evidence VALUES(?,?,?)", (str(path), digest, json.dumps(summary)))

    def evidence(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT summary FROM evidence ORDER BY path DESC")]

    def save_project(self, summary):
        with self.connection() as db:
            row = db.execute('SELECT id FROM projects WHERE path=?', (summary['path'],)).fetchone()
            project_id = row[0] if row else uuid.uuid4().hex
            db.execute('INSERT OR REPLACE INTO projects VALUES(?,?,?,?)',
                (project_id, summary['path'], json.dumps(summary), now()))
            self.event(db, None, 'project_scanned', {'project_id': project_id, 'git': summary['git'], 'eligible': summary['eligible']})
            return project_id

    def projects(self):
        with self.connection() as db:
            return [{'id': row['id'], 'summary': json.loads(row['summary'])} for row in db.execute('SELECT * FROM projects ORDER BY updated DESC')]

    def setting(self, key, default=None):
        with self.connection() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def save_setting(self, key, value):
        if key != 'model_settings':
            raise ValueError('UNKNOWN_SETTINGS_KEY')
        from .model_settings import validate
        value = validate(value)
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))
            self.event(db, None, 'settings_saved', {'key': key})
