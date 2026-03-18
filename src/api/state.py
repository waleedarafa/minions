from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock

import structlog

from src.api.models import RunResult, RunStatus, Task

logger = structlog.get_logger()

_DEFAULT_STATE_PATH = ".minion/state/api_state.json"
_RESTART_FAILURE = "Server restarted before run completed"
_LEASE_EXPIRED_MESSAGE = "Task lease expired; re-queued for another worker"


@dataclass
class ApiStateSnapshot:
    tasks: dict[str, Task]
    run_statuses: dict[str, RunStatus]
    run_results: dict[str, RunResult]
    run_logs: dict[str, list[str]]


def _empty_snapshot() -> ApiStateSnapshot:
    return ApiStateSnapshot(tasks={}, run_statuses={}, run_results={}, run_logs={})


def _build_failure_result(task: Task, status: RunStatus | None, error: str) -> RunResult:
    return RunResult(
        task_id=task.id,
        run_id=status.run_id if status else "",
        source=task.source,
        priority=task.priority,
        created_by=task.created_by,
        success=False,
        tokens_used=status.tokens_used if status else 0,
        hydration_trace=status.hydration_trace if status else [],
        rule_trace=status.rule_trace if status else [],
        rule_scope_targets=status.rule_scope_targets if status else [],
        ci_rounds=status.ci_rounds if status else 0,
        ci_run_id=status.ci_run_id if status else None,
        ci_conclusion=status.ci_conclusion if status else None,
        ci_summary=status.ci_summary if status else "",
        sandbox_mode=status.sandbox_mode if status else "",
        sandbox_backend=status.sandbox_backend if status else "",
        sandbox_network_enabled=status.sandbox_network_enabled if status else False,
        sandbox_host_fallback=status.sandbox_host_fallback if status else False,
        lease_owner=None,
        lease_expires_at=None,
        security_events=status.security_events if status else [],
        duration_seconds=0.0,
        error=error,
    )


def _recover_inflight(snapshot: ApiStateSnapshot) -> ApiStateSnapshot:
    now = datetime.now(timezone.utc)
    for task in snapshot.tasks.values():
        if task.status in ("pending", "running"):
            task.status = "failed"
            snapshot.run_logs.setdefault(task.id, []).append(
                f"[{now.isoformat()}] {_RESTART_FAILURE}"
            )
    for status in snapshot.run_statuses.values():
        if status.status in ("pending", "running"):
            status.status = "failed"
            status.error = _RESTART_FAILURE
            status.lease_owner = None
            status.lease_expires_at = None
    for task_id, task in snapshot.tasks.items():
        if task.status == "failed" and task_id not in snapshot.run_results:
            snapshot.run_results[task_id] = _build_failure_result(
                task,
                snapshot.run_statuses.get(task_id),
                _RESTART_FAILURE,
            )
    return snapshot


class _BaseStateBackend:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = RLock()

    def load(self, recover_inflight: bool = True) -> ApiStateSnapshot:
        raise NotImplementedError

    def persist(self, snapshot: ApiStateSnapshot) -> None:
        raise NotImplementedError

    def claim_pending_tasks(self, *, worker_id: str, max_count: int, lease_seconds: int) -> list[str]:
        raise NotImplementedError

    def renew_task_lease(self, *, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        raise NotImplementedError

    def requeue_expired_leases(self) -> list[str]:
        raise NotImplementedError


class _JsonStateBackend(_BaseStateBackend):
    def load(self, recover_inflight: bool = True) -> ApiStateSnapshot:
        with self._lock:
            if not os.path.isfile(self.path):
                snapshot = _empty_snapshot()
            else:
                try:
                    with open(self.path) as fh:
                        payload = json.load(fh)
                except Exception as exc:
                    logger.warning("api_state_load_error", path=self.path, error=str(exc))
                    snapshot = _empty_snapshot()
                else:
                    snapshot = ApiStateSnapshot(
                        tasks={item["id"]: Task.model_validate(item) for item in payload.get("tasks", [])},
                        run_statuses={item["task_id"]: RunStatus.model_validate(item) for item in payload.get("run_statuses", [])},
                        run_results={item["task_id"]: RunResult.model_validate(item) for item in payload.get("run_results", [])},
                        run_logs={str(task_id): [str(line) for line in lines] for task_id, lines in payload.get("run_logs", {}).items()},
                    )
            if recover_inflight:
                snapshot = _recover_inflight(snapshot)
            logger.info(
                "api_state_loaded",
                path=self.path,
                tasks=len(snapshot.tasks),
                statuses=len(snapshot.run_statuses),
                results=len(snapshot.run_results),
                backend="json",
            )
            return snapshot

    def persist(self, snapshot: ApiStateSnapshot) -> None:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                payload = {
                    "tasks": [task.model_dump(mode="json") for task in snapshot.tasks.values()],
                    "run_statuses": [status.model_dump(mode="json") for status in snapshot.run_statuses.values()],
                    "run_results": [result.model_dump(mode="json") for result in snapshot.run_results.values()],
                    "run_logs": snapshot.run_logs,
                }
                fd, temp_path = tempfile.mkstemp(
                    prefix="api-state-",
                    suffix=".json",
                    dir=os.path.dirname(self.path) or ".",
                )
                with os.fdopen(fd, "w") as fh:
                    json.dump(payload, fh, indent=2)
                os.replace(temp_path, self.path)
            except Exception as exc:
                logger.warning("api_state_persist_error", path=self.path, error=str(exc), backend="json")

    def claim_pending_tasks(self, *, worker_id: str, max_count: int, lease_seconds: int) -> list[str]:
        with self._lock:
            snapshot = self.load(recover_inflight=False)
            now = datetime.now(timezone.utc)
            pending: list[tuple[int, datetime, str]] = []
            for task_id, task in snapshot.tasks.items():
                if task.status != "pending":
                    continue
                status = snapshot.run_statuses.get(task_id)
                if status is not None and status.lease_expires_at and status.lease_expires_at > now:
                    continue
                pending.append(({"high": 0, "normal": 1, "low": 2}.get(task.priority.lower(), 1), task.created_at, task_id))
            claimed_ids: list[str] = []
            for _, _, task_id in sorted(pending)[:max_count]:
                task = snapshot.tasks[task_id]
                status = snapshot.run_statuses.get(task_id)
                if status is None:
                    continue
                task.status = "running"
                status.status = "running"
                status.current_step = "claimed"
                status.lease_owner = worker_id
                status.lease_expires_at = now + timedelta(seconds=lease_seconds)
                claimed_ids.append(task_id)
            if claimed_ids:
                self.persist(snapshot)
            return claimed_ids

    def renew_task_lease(self, *, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        with self._lock:
            snapshot = self.load(recover_inflight=False)
            task = snapshot.tasks.get(task_id)
            status = snapshot.run_statuses.get(task_id)
            if task is None or status is None or task.status != "running" or status.lease_owner != worker_id:
                return False
            status.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            self.persist(snapshot)
            return True

    def requeue_expired_leases(self) -> list[str]:
        with self._lock:
            snapshot = self.load(recover_inflight=False)
            now = datetime.now(timezone.utc)
            requeued: list[str] = []
            for task_id, task in snapshot.tasks.items():
                if task.status != "running":
                    continue
                status = snapshot.run_statuses.get(task_id)
                if status is None or status.lease_expires_at is None or status.lease_expires_at > now:
                    continue
                task.status = "pending"
                status.status = "pending"
                status.current_step = "queued"
                status.error = None
                status.lease_owner = None
                status.lease_expires_at = None
                snapshot.run_logs.setdefault(task_id, []).append(
                    f"[{now.isoformat()}] {_LEASE_EXPIRED_MESSAGE}"
                )
                requeued.append(task_id)
            if requeued:
                self.persist(snapshot)
            return requeued


class _SqliteStateBackend(_BaseStateBackend):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS run_statuses (task_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS run_results (task_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS run_logs (task_id TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def _load_snapshot(self, conn: sqlite3.Connection) -> ApiStateSnapshot:
        tasks = {
            row["id"]: Task.model_validate(json.loads(row["data"]))
            for row in conn.execute("SELECT id, data FROM tasks")
        }
        run_statuses = {
            row["task_id"]: RunStatus.model_validate(json.loads(row["data"]))
            for row in conn.execute("SELECT task_id, data FROM run_statuses")
        }
        run_results = {
            row["task_id"]: RunResult.model_validate(json.loads(row["data"]))
            for row in conn.execute("SELECT task_id, data FROM run_results")
        }
        run_logs = {
            row["task_id"]: json.loads(row["data"])
            for row in conn.execute("SELECT task_id, data FROM run_logs")
        }
        return ApiStateSnapshot(tasks=tasks, run_statuses=run_statuses, run_results=run_results, run_logs=run_logs)

    @staticmethod
    def _load_task_row(row: sqlite3.Row | None) -> Task | None:
        if row is None:
            return None
        return Task.model_validate(json.loads(row["data"]))

    @staticmethod
    def _load_status_row(row: sqlite3.Row | None) -> RunStatus | None:
        if row is None:
            return None
        return RunStatus.model_validate(json.loads(row["data"]))

    @staticmethod
    def _load_logs_data(data: str | None) -> list[str]:
        if data is None:
            return []
        return [str(line) for line in json.loads(data)]

    def _persist_snapshot(self, conn: sqlite3.Connection, snapshot: ApiStateSnapshot) -> None:
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM run_statuses")
        conn.execute("DELETE FROM run_results")
        conn.execute("DELETE FROM run_logs")
        conn.executemany(
            "INSERT INTO tasks (id, data) VALUES (?, ?)",
            [(task.id, json.dumps(task.model_dump(mode="json"))) for task in snapshot.tasks.values()],
        )
        conn.executemany(
            "INSERT INTO run_statuses (task_id, data) VALUES (?, ?)",
            [(status.task_id, json.dumps(status.model_dump(mode="json"))) for status in snapshot.run_statuses.values()],
        )
        conn.executemany(
            "INSERT INTO run_results (task_id, data) VALUES (?, ?)",
            [(result.task_id, json.dumps(result.model_dump(mode="json"))) for result in snapshot.run_results.values()],
        )
        conn.executemany(
            "INSERT INTO run_logs (task_id, data) VALUES (?, ?)",
            [(task_id, json.dumps(lines)) for task_id, lines in snapshot.run_logs.items()],
        )

    def load(self, recover_inflight: bool = True) -> ApiStateSnapshot:
        with self._lock, self._connect() as conn:
            snapshot = self._load_snapshot(conn)
            if recover_inflight:
                snapshot = _recover_inflight(snapshot)
            logger.info(
                "api_state_loaded",
                path=self.path,
                tasks=len(snapshot.tasks),
                statuses=len(snapshot.run_statuses),
                results=len(snapshot.run_results),
                backend="sqlite",
            )
            return snapshot

    def persist(self, snapshot: ApiStateSnapshot) -> None:
        with self._lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._persist_snapshot(conn, snapshot)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                logger.warning("api_state_persist_error", path=self.path, error=str(exc), backend="sqlite")

    def claim_pending_tasks(self, *, worker_id: str, max_count: int, lease_seconds: int) -> list[str]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc)
            pending: list[tuple[int, datetime, str, Task, RunStatus]] = []
            rows = conn.execute(
                """
                SELECT t.id AS task_id, t.data AS task_data, s.data AS status_data
                FROM tasks t
                JOIN run_statuses s ON s.task_id = t.id
                """
            ).fetchall()
            for row in rows:
                task = Task.model_validate(json.loads(row["task_data"]))
                status = RunStatus.model_validate(json.loads(row["status_data"]))
                if task.status != "pending":
                    continue
                if status.lease_expires_at and status.lease_expires_at > now:
                    continue
                pending.append((
                    {"high": 0, "normal": 1, "low": 2}.get(task.priority.lower(), 1),
                    task.created_at,
                    task.id,
                    task,
                    status,
                ))
            claimed_ids: list[str] = []
            for _, _, task_id, task, status in sorted(pending)[:max_count]:
                task.status = "running"
                status.status = "running"
                status.current_step = "claimed"
                status.lease_owner = worker_id
                status.lease_expires_at = now + timedelta(seconds=lease_seconds)
                conn.execute(
                    "UPDATE tasks SET data = ? WHERE id = ?",
                    (json.dumps(task.model_dump(mode="json")), task_id),
                )
                conn.execute(
                    "UPDATE run_statuses SET data = ? WHERE task_id = ?",
                    (json.dumps(status.model_dump(mode="json")), task_id),
                )
                claimed_ids.append(task_id)
            conn.commit()
            return claimed_ids

    def renew_task_lease(self, *, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = self._load_task_row(conn.execute("SELECT data FROM tasks WHERE id = ?", (task_id,)).fetchone())
            status = self._load_status_row(conn.execute("SELECT data FROM run_statuses WHERE task_id = ?", (task_id,)).fetchone())
            if task is None or status is None or task.status != "running" or status.lease_owner != worker_id:
                conn.rollback()
                return False
            status.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            conn.execute(
                "UPDATE run_statuses SET data = ? WHERE task_id = ?",
                (json.dumps(status.model_dump(mode="json")), task_id),
            )
            conn.commit()
            return True

    def requeue_expired_leases(self) -> list[str]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc)
            requeued: list[str] = []
            rows = conn.execute(
                """
                SELECT t.id AS task_id, t.data AS task_data, s.data AS status_data, l.data AS log_data
                FROM tasks t
                JOIN run_statuses s ON s.task_id = t.id
                LEFT JOIN run_logs l ON l.task_id = t.id
                """
            ).fetchall()
            for row in rows:
                task = Task.model_validate(json.loads(row["task_data"]))
                status = RunStatus.model_validate(json.loads(row["status_data"]))
                if task.status != "running" or status.lease_expires_at is None or status.lease_expires_at > now:
                    continue
                task.status = "pending"
                status.status = "pending"
                status.current_step = "queued"
                status.error = None
                status.lease_owner = None
                status.lease_expires_at = None
                logs = self._load_logs_data(row["log_data"])
                logs.append(
                    f"[{now.isoformat()}] {_LEASE_EXPIRED_MESSAGE}"
                )
                conn.execute(
                    "UPDATE tasks SET data = ? WHERE id = ?",
                    (json.dumps(task.model_dump(mode="json")), task.id),
                )
                conn.execute(
                    "UPDATE run_statuses SET data = ? WHERE task_id = ?",
                    (json.dumps(status.model_dump(mode="json")), task.id),
                )
                conn.execute(
                    """
                    INSERT INTO run_logs (task_id, data) VALUES (?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET data = excluded.data
                    """,
                    (task.id, json.dumps(logs)),
                )
                requeued.append(task.id)
            conn.commit()
            return requeued


class ApiStateStore:
    def __init__(self, path: str | None = None) -> None:
        resolved = path or os.environ.get("MINIONS_API_STATE_PATH", _DEFAULT_STATE_PATH)
        backend_name = os.environ.get("MINIONS_API_STATE_BACKEND", "").lower()
        use_sqlite = backend_name == "sqlite" or resolved.endswith(".db")
        self._backend: _BaseStateBackend = _SqliteStateBackend(resolved) if use_sqlite else _JsonStateBackend(resolved)

    @property
    def path(self) -> str:
        return self._backend.path

    def set_path(self, path: str) -> None:
        self.__init__(path)

    def load(self, recover_inflight: bool = True) -> ApiStateSnapshot:
        return self._backend.load(recover_inflight=recover_inflight)

    def persist(self, snapshot: ApiStateSnapshot) -> None:
        self._backend.persist(snapshot)

    def claim_pending_tasks(self, *, worker_id: str, max_count: int, lease_seconds: int) -> list[str]:
        return self._backend.claim_pending_tasks(worker_id=worker_id, max_count=max_count, lease_seconds=lease_seconds)

    def renew_task_lease(self, *, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        return self._backend.renew_task_lease(task_id=task_id, worker_id=worker_id, lease_seconds=lease_seconds)

    def requeue_expired_leases(self) -> list[str]:
        return self._backend.requeue_expired_leases()
