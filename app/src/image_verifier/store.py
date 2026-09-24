"""Single-host durable queue. All claims and quota reservations are transactional.

SQLite is intentionally both queue and task store in v1: acceptance cannot commit
without its queue entry. No separate broker/Outbox consistency gap exists here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .config import PREPROCESS_VERSION, PROMPT_VERSION, Settings
from .models import BackendError, BackendResult, ImageInput, ServiceError

TERMINAL = {"succeeded", "failed", "cancelled", "expired"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS resources(
 id TEXT PRIMARY KEY, policy TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready',
 reason TEXT, next_at REAL NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS images(
 digest TEXT PRIMARY KEY, mime TEXT NOT NULL, content BLOB NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS batches(
 id TEXT PRIMARY KEY, idem_key TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
 created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
 position INTEGER NOT NULL, image_digest TEXT NOT NULL REFERENCES images(digest),
 filename TEXT NOT NULL, cache_key TEXT NOT NULL, signature TEXT NOT NULL,
 resource_id TEXT NOT NULL, backend TEXT NOT NULL, model TEXT NOT NULL,
 prompt_version TEXT NOT NULL, preprocess_version TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL, updated REAL NOT NULL,
 deadline REAL NOT NULL, available_at REAL NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL,
 result_json TEXT, error_code TEXT, cache_hit INTEGER NOT NULL DEFAULT 0,
 UNIQUE(batch_id, position)
);
CREATE INDEX IF NOT EXISTS task_queue ON tasks(status, available_at, created);
CREATE INDEX IF NOT EXISTS task_dedup ON tasks(cache_key, status);
CREATE INDEX IF NOT EXISTS task_batch ON tasks(batch_id, position);
CREATE TABLE IF NOT EXISTS attempts(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 resource_id TEXT NOT NULL, started REAL NOT NULL, finished REAL,
 status TEXT NOT NULL DEFAULT 'running', error_code TEXT
);
CREATE INDEX IF NOT EXISTS attempt_budget ON attempts(resource_id, started);
CREATE TABLE IF NOT EXISTS cache(
 cache_key TEXT PRIMARY KEY, result_json TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_heartbeats(
 id TEXT PRIMARY KEY, signature TEXT NOT NULL, seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY, created REAL NOT NULL, action TEXT NOT NULL,
 resource_id TEXT NOT NULL, detail TEXT NOT NULL
);
"""


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings

    def connect(self):
        db = sqlite3.connect(self.settings.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            if db.execute("SELECT version FROM schema_version").fetchone()[0] != 1:
                raise RuntimeError("Unsupported database schema; migration required")
        finally:
            db.close()
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO resources(id,policy) VALUES(?,?)",
                (self.settings.resource_id, self.settings.policy),
            )
            policy = db.execute(
                "SELECT policy FROM resources WHERE id=?", (self.settings.resource_id,)
            ).fetchone()[0]
            if policy != self.settings.policy:
                raise RuntimeError(
                    "Resource policy/model mismatch. Stop all processes and use the "
                    "reconfigure command after draining pending tasks."
                )

    def reconfigure(self):
        """Offline operation only. Retains attempt history and existing cooldowns."""
        with self.transaction() as db:
            pending = db.execute(
                "SELECT count(*) FROM tasks WHERE resource_id=? AND status IN ('queued','running')",
                (self.settings.resource_id,),
            ).fetchone()[0]
            if pending:
                raise ServiceError("pending_tasks", "排空或取消待处理任务后才能修改策略", 409)
            db.execute(
                "UPDATE resources SET policy=? WHERE id=?",
                (self.settings.policy, self.settings.resource_id),
            )

    def submit(
        self,
        images: list[ImageInput],
        idem_key: str,
        deadline_seconds: int,
        now: float | None = None,
    ) -> tuple[str, bool]:
        now = time.time() if now is None else now
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "images": [i.digest for i in images],
                    "signature": self.settings.signature,
                    "deadline_seconds": deadline_seconds,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.transaction() as db:
            previous = db.execute("SELECT * FROM batches WHERE idem_key=?", (idem_key,)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ServiceError("idempotency_conflict", "幂等键已用于其他请求", 409)
                return previous["id"], True
            pending = db.execute(
                "SELECT count(*) FROM tasks WHERE status IN ('queued','running')"
            ).fetchone()[0]
            if pending + len(images) > self.settings.max_pending:
                raise ServiceError("queue_full", "待处理队列已满，请稍后重试", 429)
            resource = db.execute(
                "SELECT state FROM resources WHERE id=?", (self.settings.resource_id,)
            ).fetchone()
            if not resource or resource["state"] != "ready":
                raise ServiceError("backend_paused", "后端已暂停接收任务", 503)
            batch_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO batches VALUES(?,?,?,?)", (batch_id, idem_key, fingerprint, now)
            )
            for position, image in enumerate(images):
                db.execute(
                    "INSERT OR IGNORE INTO images VALUES(?,?,?,?)",
                    (image.digest, image.mime, image.content, now),
                )
                db.execute(
                    """INSERT INTO tasks(
                    id,batch_id,position,image_digest,filename,cache_key,signature,
                    resource_id,backend,model,prompt_version,preprocess_version,
                    created,updated,deadline,available_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        uuid.uuid4().hex,
                        batch_id,
                        position,
                        image.digest,
                        image.filename,
                        f"{self.settings.signature}:{image.digest}",
                        self.settings.signature,
                        self.settings.resource_id,
                        self.settings.backend,
                        self.settings.model,
                        PROMPT_VERSION,
                        PREPROCESS_VERSION,
                        now,
                        now,
                        now + deadline_seconds,
                        now,
                    ),
                )
            return batch_id, False

    @staticmethod
    def _maintenance(db, now):
        # Unknown execution is never blindly retried after a crashed worker.
        lost = db.execute(
            """SELECT id,lease_token,deadline FROM tasks
            WHERE (status='running' AND (lease_until<=? OR deadline<=?))
               OR (status='queued' AND deadline<=?)""",
            (now, now, now),
        ).fetchall()
        for task in lost:
            expired = task["deadline"] <= now
            status = "expired" if expired else "failed"
            code = "deadline_exceeded" if expired else "execution_unknown"
            db.execute(
                """UPDATE tasks SET status=?,error_code=?,updated=?,
                       lease_token=NULL,lease_until=NULL WHERE id=?""",
                (status, code, now, task["id"]),
            )
            if task["lease_token"]:
                db.execute(
                    """UPDATE attempts SET status='unknown',error_code=?,finished=?
                           WHERE id=? AND status='running'""",
                    (code, now, task["lease_token"]),
                )

    def claim(self, worker_id: str, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self.transaction() as db:
            self._maintenance(db, now)
            db.execute(
                "INSERT OR REPLACE INTO worker_heartbeats VALUES(?,?,?)",
                (worker_id, self.settings.signature, now),
            )
            candidates = db.execute(
                """SELECT * FROM tasks t
                WHERE status='queued' AND signature=? AND available_at<=?
                AND NOT EXISTS(SELECT 1 FROM tasks active
                    WHERE active.cache_key=t.cache_key AND active.status='running')
                ORDER BY created,position,id LIMIT 100""",
                (self.settings.signature, now),
            ).fetchall()
            runnable = []
            for task in candidates:
                cached = db.execute(
                    "SELECT result_json FROM cache WHERE cache_key=? AND expires>?",
                    (task["cache_key"], now),
                ).fetchone()
                if cached:
                    db.execute(
                        """UPDATE tasks SET status='succeeded',result_json=?,
                               cache_hit=1,updated=?,error_code=NULL WHERE id=?""",
                        (cached[0], now, task["id"]),
                    )
                else:
                    runnable.append(task)
            resource = db.execute(
                "SELECT * FROM resources WHERE id=?", (self.settings.resource_id,)
            ).fetchone()
            if (
                resource["state"] != "ready"
                or resource["next_at"] > now
                or resource["blocked_until"] > now
            ):
                return None
            active = db.execute(
                "SELECT count(*) FROM tasks WHERE resource_id=? AND status='running'",
                (self.settings.resource_id,),
            ).fetchone()[0]
            counts = db.execute(
                """SELECT count(*) AS hour_count,
                        coalesce(sum(started>?),0) AS minute_count FROM attempts
                        WHERE resource_id=? AND started>?""",
                (now - 60, self.settings.resource_id, now - 3600),
            ).fetchone()
            if (
                active >= self.settings.concurrency
                or counts["hour_count"] >= self.settings.rph
                or counts["minute_count"] >= self.settings.rpm
            ):
                return None
            for task in runnable:
                if db.execute(
                    "SELECT 1 FROM tasks WHERE cache_key=? AND status='running'",
                    (task["cache_key"],),
                ).fetchone():
                    continue
                token = uuid.uuid4().hex
                db.execute(
                    """UPDATE tasks SET status='running',attempts=attempts+1,
                    lease_token=?,lease_until=?,updated=?,error_code=NULL WHERE id=?""",
                    (token, now + self.settings.lease_seconds, now, task["id"]),
                )
                db.execute(
                    "INSERT INTO attempts(id,task_id,resource_id,started) VALUES(?,?,?,?)",
                    (token, task["id"], self.settings.resource_id, now),
                )
                spacing = max(60 / self.settings.rpm, 3600 / self.settings.rph)
                db.execute(
                    "UPDATE resources SET next_at=? WHERE id=?",
                    (now + spacing, self.settings.resource_id),
                )
                result = dict(task)
                result.update(lease_token=token, attempts=task["attempts"] + 1)
                return result
            return None

    def load_image(self, digest: str) -> bytes:
        db = self.connect()
        try:
            return db.execute("SELECT content FROM images WHERE digest=?", (digest,)).fetchone()[0]
        finally:
            db.close()

    def image_mime(self, digest: str) -> str:
        db = self.connect()
        try:
            return db.execute("SELECT mime FROM images WHERE digest=?", (digest,)).fetchone()[0]
        finally:
            db.close()

    def heartbeat(self, task: dict, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.transaction() as db:
            return (
                db.execute(
                    """UPDATE tasks SET lease_until=? WHERE id=? AND
                status='running' AND lease_token=? AND lease_until>? AND deadline>?""",
                    (now + self.settings.lease_seconds, task["id"], task["lease_token"], now, now),
                ).rowcount
                == 1
            )

    def finish(
        self,
        task: dict,
        result: BackendResult | None = None,
        error: BackendError | None = None,
        retry_delay: float = 0,
        now: float | None = None,
    ) -> bool:
        now = time.time() if now is None else now
        with self.transaction() as db:
            self._maintenance(db, now)
            current = db.execute(
                "SELECT * FROM tasks WHERE id=? AND status='running' AND lease_token=?",
                (task["id"], task["lease_token"]),
            ).fetchone()
            if not current:
                return False  # Cancelled, expired or superseded: fencing prevents a stale write.
            if result is not None:
                payload = result.model_dump_json()
                db.execute(
                    """UPDATE tasks SET status='succeeded',result_json=?,updated=?,
                           lease_token=NULL,lease_until=NULL WHERE id=?""",
                    (payload, now, task["id"]),
                )
                db.execute(
                    "INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                    (task["cache_key"], payload, now + self.settings.cache_ttl),
                )
                attempt_status, code = "succeeded", None
            else:
                assert error is not None
                code = error.code
                if error.pause:
                    db.execute(
                        "UPDATE resources SET state='paused',reason=? WHERE id=?",
                        (code, task["resource_id"]),
                    )
                    db.execute(
                        "INSERT INTO audit(created,action,resource_id,detail) VALUES(?,?,?,?)",
                        (now, "auto_pause", task["resource_id"], code),
                    )
                if error.retry_after:
                    db.execute(
                        "UPDATE resources SET blocked_until=max(blocked_until,?) WHERE id=?",
                        (now + error.retry_after, task["resource_id"]),
                    )
                delay = max(retry_delay, error.retry_after)
                retry = (
                    error.retryable
                    and not error.unknown
                    and not error.pause
                    and current["attempts"] < self.settings.max_attempts
                    and now + delay < current["deadline"]
                )
                status = "queued" if retry else "failed"
                db.execute(
                    """UPDATE tasks SET status=?,error_code=?,available_at=?,updated=?,
                    lease_token=NULL,lease_until=NULL WHERE id=?""",
                    (status, code, now + delay, now, task["id"]),
                )
                attempt_status = "unknown" if error.unknown else "failed"
            db.execute(
                "UPDATE attempts SET status=?,error_code=?,finished=? WHERE id=?",
                (attempt_status, code, now, task["lease_token"]),
            )
            return True

    @staticmethod
    def _public_task(row):
        task = dict(row)
        for field in ("lease_token", "lease_until", "cache_key", "signature", "result_json"):
            task.pop(field, None)
        task["result"] = json.loads(row["result_json"]) if row["result_json"] else None
        task["cache_hit"] = bool(task["cache_hit"])
        return task

    def get_task(self, task_id: str):
        with self.transaction() as db:
            self._maintenance(db, time.time())
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ServiceError("not_found", "任务不存在", 404)
            task = self._public_task(row)
            task["attempt_history"] = [
                dict(a)
                for a in db.execute(
                    "SELECT started,finished,status,error_code FROM attempts WHERE task_id=? "
                    "ORDER BY started",
                    (task_id,),
                )
            ]
            return task

    def get_batch(self, batch_id: str):
        with self.transaction() as db:
            self._maintenance(db, time.time())
            batch = db.execute("SELECT id,created FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise ServiceError("not_found", "批次不存在", 404)
            tasks = [
                self._public_task(row)
                for row in db.execute(
                    "SELECT * FROM tasks WHERE batch_id=? ORDER BY position",
                    (batch_id,),
                )
            ]
            return {
                **dict(batch),
                "done": all(t["status"] in TERMINAL for t in tasks),
                "tasks": tasks,
            }

    def cancel(self, task_id: str):
        with self.transaction() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ServiceError("not_found", "任务不存在", 404)
            if task["status"] not in TERMINAL:
                now = time.time()
                db.execute(
                    """UPDATE tasks SET status='cancelled',updated=?,lease_token=NULL,
                           lease_until=NULL WHERE id=?""",
                    (now, task_id),
                )
                db.execute(
                    "UPDATE attempts SET status='unknown',finished=?,error_code=? "
                    "WHERE id=? AND status='running'",
                    (now, "cancelled_in_flight", task["lease_token"]),
                )

    def set_resource_state(self, state: str, reason: str):
        with self.transaction() as db:
            db.execute(
                "UPDATE resources SET state=?,reason=? WHERE id=?",
                (state, reason, self.settings.resource_id),
            )
            db.execute(
                "INSERT INTO audit(created,action,resource_id,detail) VALUES(?,?,?,?)",
                (time.time(), state, self.settings.resource_id, reason),
            )

    def snapshot(self):
        with self.transaction() as db:
            self._maintenance(db, time.time())
            resources = [
                dict(row)
                for row in db.execute("SELECT id,state,reason,next_at,blocked_until FROM resources")
            ]
            statuses = {
                r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM tasks GROUP BY status")
            }
            return {
                "tasks": statuses,
                "resources": resources,
                "attempts": db.execute("SELECT count(*) FROM attempts").fetchone()[0],
                "cache_hits": db.execute("SELECT count(*) FROM tasks WHERE cache_hit=1").fetchone()[
                    0
                ],
                "last_worker_seen": db.execute(
                    "SELECT max(seen) FROM worker_heartbeats WHERE signature=?",
                    (self.settings.signature,),
                ).fetchone()[0],
                "oldest_queued_at": db.execute(
                    "SELECT min(created) FROM tasks WHERE status='queued'"
                ).fetchone()[0],
            }

    def recent_batches(self):
        with self.transaction() as db:
            self._maintenance(db, time.time())
            return [
                dict(row)
                for row in db.execute("""
                SELECT b.id,b.created,count(t.id) AS total,
                sum(t.status IN ('succeeded','failed','cancelled','expired')) AS completed
                FROM batches b JOIN tasks t ON t.batch_id=b.id
                GROUP BY b.id ORDER BY b.created DESC LIMIT 10
            """)
            ]

    def backup(self, destination):
        if destination.resolve() == self.settings.db_path.resolve() or destination.exists():
            raise ValueError("Backup destination must be a new file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()

    def prune(self, older_than_days: int, *, apply: bool = False):
        if older_than_days < 1:
            raise ValueError("Keep at least one day so rolling quota history is preserved")
        now = time.time()
        with self.transaction() as db:
            # Remove whole batches only: an active batch must keep every member.
            batches = [
                row[0]
                for row in db.execute(
                    """
                SELECT b.id FROM batches b WHERE b.created<? AND NOT EXISTS(
                    SELECT 1 FROM tasks t WHERE t.batch_id=b.id AND (
                        t.status IN ('queued','running') OR t.updated>=?
                    )
                ) LIMIT 100
                """,
                    (now - older_than_days * 86400, now - older_than_days * 86400),
                )
            ]
            if apply:
                for batch_id in batches:
                    db.execute(
                        "DELETE FROM attempts WHERE task_id IN "
                        "(SELECT id FROM tasks WHERE batch_id=?)",
                        (batch_id,),
                    )
                    db.execute("DELETE FROM tasks WHERE batch_id=?", (batch_id,))
                    db.execute("DELETE FROM batches WHERE id=?", (batch_id,))
                db.execute("DELETE FROM cache WHERE expires<=?", (now,))
                db.execute(
                    "DELETE FROM images WHERE NOT EXISTS "
                    "(SELECT 1 FROM tasks WHERE tasks.image_digest=images.digest)"
                )
                db.execute("DELETE FROM worker_heartbeats WHERE seen<?", (now - 86400,))
            return {"eligible_batches": len(batches), "applied": apply, "batch_limit": 100}
