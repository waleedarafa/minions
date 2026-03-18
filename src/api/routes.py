from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from src.api.models import RunResult, RunStatus, Task, TaskCreate
from src.api.state import ApiStateSnapshot, ApiStateStore
from src.monitoring.metrics import MetricsCollector
from src.orchestration import (
    TaskRunRequest,
    TaskRunner,
    get_blueprint,
    list_blueprints as load_blueprints,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/api")

# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------
_tasks: dict[str, Task] = {}
_run_statuses: dict[str, RunStatus] = {}
_run_results: dict[str, RunResult] = {}
_run_logs: dict[str, list[str]] = {}
_background_tasks: dict[str, asyncio.Task[Any]] = {}
_state_store = ApiStateStore()
_metrics = MetricsCollector()
_dispatcher_task: asyncio.Task[Any] | None = None
_dispatcher_stop: asyncio.Event | None = None
_max_concurrent_tasks = 2
_worker_id = f"api-worker-{uuid.uuid4()}"
_lease_seconds = 300
_lease_renew_interval_seconds = 30
_server_role = "all"


def _snapshot() -> ApiStateSnapshot:
    return ApiStateSnapshot(
        tasks=_tasks,
        run_statuses=_run_statuses,
        run_results=_run_results,
        run_logs=_run_logs,
    )


def _persist_runtime_state() -> None:
    _state_store.persist(_snapshot())


def configure_runtime_state(state_path: str | None = None, metrics_path: str | None = None) -> None:
    global _state_store, _metrics, _max_concurrent_tasks, _worker_id, _lease_seconds, _lease_renew_interval_seconds, _server_role
    if state_path is not None:
        _state_store = ApiStateStore(state_path)
    if metrics_path is not None:
        _metrics = MetricsCollector(metrics_path)
    _max_concurrent_tasks = max(1, int(os.environ.get("MINIONS_MAX_CONCURRENT_TASKS", "2")))
    _lease_seconds = max(30, int(os.environ.get("MINIONS_TASK_LEASE_SECONDS", "300")))
    _lease_renew_interval_seconds = max(1, min(_lease_seconds // 2, int(os.environ.get("MINIONS_LEASE_RENEW_INTERVAL_SECONDS", "30"))))
    _worker_id = os.environ.get("MINIONS_WORKER_ID", _worker_id)
    _server_role = os.environ.get("MINIONS_SERVER_ROLE", "all").lower()


def get_server_role() -> str:
    return _server_role


def dispatcher_enabled() -> bool:
    return _server_role in {"all", "worker"}


def dispatcher_running() -> bool:
    return _dispatcher_task is not None and not _dispatcher_task.done()


def worker_status() -> dict[str, Any]:
    leased = [
        status
        for status in _run_statuses.values()
        if status.lease_owner == _worker_id and status.status == "running"
    ]
    return {
        "worker_id": _worker_id,
        "server_role": get_server_role(),
        "dispatcher_enabled": dispatcher_enabled(),
        "dispatcher_running": dispatcher_running(),
        "lease_seconds": _lease_seconds,
        "lease_renew_interval_seconds": _lease_renew_interval_seconds,
        "active_leases": len(leased),
        "leased_task_ids": [status.task_id for status in leased],
    }


def load_runtime_state(recover_inflight: bool = True) -> None:
    snapshot = _state_store.load(recover_inflight=recover_inflight)
    _tasks.clear()
    _tasks.update(snapshot.tasks)
    _run_statuses.clear()
    _run_statuses.update(snapshot.run_statuses)
    _run_results.clear()
    _run_results.update(snapshot.run_results)
    _run_logs.clear()
    _run_logs.update(snapshot.run_logs)
    _background_tasks.clear()


async def _dispatch_pending_tasks() -> None:
    requeued = _state_store.requeue_expired_leases()
    if requeued:
        load_runtime_state()
    available_slots = max(0, _max_concurrent_tasks - len(_background_tasks))
    if available_slots == 0:
        return
    claimed_ids = _state_store.claim_pending_tasks(
        worker_id=_worker_id,
        max_count=available_slots,
        lease_seconds=_lease_seconds,
    )
    if not claimed_ids:
        return
    load_runtime_state(recover_inflight=False)
    for task_id in claimed_ids:
        task = _tasks.get(task_id)
        if task is None or task.id in _background_tasks:
            continue
        _background_tasks[task.id] = asyncio.create_task(_execute_task(task))


async def _task_dispatcher_loop() -> None:
    assert _dispatcher_stop is not None
    while not _dispatcher_stop.is_set():
        await _dispatch_pending_tasks()
        try:
            await asyncio.wait_for(_dispatcher_stop.wait(), timeout=0.05)
        except TimeoutError:
            continue


async def start_task_dispatcher() -> None:
    global _dispatcher_task, _dispatcher_stop
    if not dispatcher_enabled():
        return
    if _dispatcher_task is not None and not _dispatcher_task.done():
        return
    _dispatcher_stop = asyncio.Event()
    _dispatcher_task = asyncio.create_task(_task_dispatcher_loop())


async def stop_task_dispatcher() -> None:
    global _dispatcher_task, _dispatcher_stop
    if _dispatcher_stop is not None:
        _dispatcher_stop.set()
    if _dispatcher_task is not None:
        await _dispatcher_task
    _dispatcher_task = None
    _dispatcher_stop = None


def _append_run_log(task_id: str, message: str) -> None:
    _run_logs.setdefault(task_id, []).append(message)
    _persist_runtime_state()


def _record_run_metrics(result: RunResult) -> None:
    _metrics.record_run(
        run_id=result.run_id,
        success=result.success,
        tokens=result.tokens_used,
        duration=result.duration_seconds,
        ci_rounds=result.ci_rounds,
    )


async def _lease_renewer(task_id: str, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_lease_renew_interval_seconds)
            break
        except TimeoutError:
            renewed = _state_store.renew_task_lease(
                task_id=task_id,
                worker_id=_worker_id,
                lease_seconds=_lease_seconds,
            )
            if not renewed:
                break
            load_runtime_state(recover_inflight=False)


def _sorted_results() -> list[RunResult]:
    return sorted(
        _run_results.values(),
        key=lambda result: _tasks.get(result.task_id).created_at if result.task_id in _tasks else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Background task execution
# ---------------------------------------------------------------------------
async def _execute_task(task: Task) -> None:
    """Execute a task in the background using the shared task runner."""
    existing_status = _run_statuses.get(task.id)
    run_id = existing_status.run_id if existing_status else str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    blueprint = get_blueprint(task.blueprint)
    if blueprint is None:
        task.status = "failed"
        _tasks[task.id] = task
        result = RunResult(
            task_id=task.id,
            run_id=run_id,
            source=task.source,
            priority=task.priority,
            created_by=task.created_by,
            success=False,
            hydration_trace=[],
            rule_trace=[],
            rule_scope_targets=[],
            ci_rounds=0,
            ci_run_id=None,
            ci_conclusion=None,
            ci_summary="",
            sandbox_mode="",
            sandbox_backend="",
            sandbox_network_enabled=False,
            sandbox_host_fallback=False,
            security_events=[],
            duration_seconds=0.0,
            error=f"Blueprint '{task.blueprint}' not found",
        )
        _run_results[task.id] = result
        _record_run_metrics(result)
        _persist_runtime_state()
        return

    steps = [step.name for step in blueprint.steps]

    status = existing_status or RunStatus(
        task_id=task.id,
        run_id=run_id,
        source=task.source,
        priority=task.priority,
        created_by=task.created_by,
        status="pending",
        current_step="queued",
        steps_completed=0,
        total_steps=len(steps),
        started_at=started,
    )
    status.status = "running"
    status.current_step = steps[0] if steps else "init"
    status.started_at = started
    status.total_steps = len(steps)
    status.lease_owner = _worker_id
    status.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=_lease_seconds)
    _run_statuses[task.id] = status
    _run_logs.setdefault(task.id, [])

    task.status = "running"
    _tasks[task.id] = task
    _persist_runtime_state()

    completed_steps: list[dict[str, Any]] = []
    lease_stop = asyncio.Event()
    lease_task = asyncio.create_task(_lease_renewer(task.id, lease_stop))

    try:
        request = TaskRunRequest(
            description=task.description,
            repo=task.repo,
            blueprint=task.blueprint,
            branch_base=task.branch_base,
            context_links=task.context_links,
            tools=task.tools,
            rules_override=task.rules_override,
            priority=task.priority,
            created_by=task.created_by,
            source="api",
        )
        runner = TaskRunner(status_callback=lambda msg: _append_run_log(task.id, msg))
        bp_result = await runner.run(request)

        completed_steps = [
            {"step": step.name, "status": step.status, "error": step.error}
            for step in bp_result.steps
        ]

        status.status = bp_result.status.value
        status.steps_completed = sum(1 for step in bp_result.steps if step.status == "completed")
        if bp_result.steps:
            status.current_step = bp_result.steps[-1].name
        status.tokens_used = bp_result.total_tokens
        if bp_result.error:
            status.error = bp_result.error
        status.hydration_trace = bp_result.context.extra.get("hydration_trace", [])
        status.rule_trace = bp_result.context.extra.get("rule_trace", [])
        status.rule_scope_targets = bp_result.context.extra.get("rule_scope_targets", [])
        status.ci_rounds = bp_result.context.ci_rounds
        status.ci_run_id = bp_result.context.extra.get("ci_run_id")
        status.ci_conclusion = bp_result.context.extra.get("ci_conclusion")
        status.ci_summary = bp_result.context.extra.get("ci_summary", "")
        status.sandbox_mode = bp_result.context.extra.get("sandbox_mode", "")
        status.sandbox_backend = bp_result.context.extra.get("sandbox_backend", "")
        status.sandbox_network_enabled = bp_result.context.extra.get("sandbox_network_enabled", False)
        status.sandbox_host_fallback = bp_result.context.extra.get("sandbox_host_fallback", False)
        status.security_events = bp_result.context.extra.get("security_events", [])
        status.lease_owner = None
        status.lease_expires_at = None

        task.status = "completed" if bp_result.status.value in ("completed", "partial") else "failed"

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        result = RunResult(
            task_id=task.id,
            run_id=run_id,
            source=task.source,
            priority=task.priority,
            created_by=task.created_by,
            success=bp_result.status.value in ("completed", "partial"),
            pr_url=bp_result.pr_url or None,
            steps=completed_steps,
            tokens_used=status.tokens_used,
            hydration_trace=bp_result.context.extra.get("hydration_trace", []),
            rule_trace=bp_result.context.extra.get("rule_trace", []),
            rule_scope_targets=bp_result.context.extra.get("rule_scope_targets", []),
            ci_rounds=bp_result.context.ci_rounds,
            ci_run_id=bp_result.context.extra.get("ci_run_id"),
            ci_conclusion=bp_result.context.extra.get("ci_conclusion"),
            ci_summary=bp_result.context.extra.get("ci_summary", ""),
            sandbox_mode=bp_result.context.extra.get("sandbox_mode", ""),
            sandbox_backend=bp_result.context.extra.get("sandbox_backend", ""),
            sandbox_network_enabled=bp_result.context.extra.get("sandbox_network_enabled", False),
            sandbox_host_fallback=bp_result.context.extra.get("sandbox_host_fallback", False),
            security_events=bp_result.context.extra.get("security_events", []),
            duration_seconds=elapsed,
            error=bp_result.error,
        )
        _run_results[task.id] = result
        _record_run_metrics(result)

    except asyncio.CancelledError:
        status.status = "cancelled"
        status.lease_owner = None
        status.lease_expires_at = None
        task.status = "failed"
        _append_run_log(
            task.id,
            f"[{datetime.now(timezone.utc).isoformat()}] Task cancelled"
        )

    except Exception as exc:
        logger.error("task_execution_error", task_id=task.id, error=str(exc))
        status.status = "failed"
        status.error = str(exc)
        status.lease_owner = None
        status.lease_expires_at = None
        task.status = "failed"

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        result = RunResult(
            task_id=task.id,
            run_id=run_id,
            source=task.source,
            priority=task.priority,
            created_by=task.created_by,
            success=False,
            steps=completed_steps,
            tokens_used=status.tokens_used,
            hydration_trace=status.hydration_trace,
            rule_trace=status.rule_trace,
            rule_scope_targets=status.rule_scope_targets,
            ci_rounds=status.ci_rounds,
            ci_run_id=status.ci_run_id,
            ci_conclusion=status.ci_conclusion,
            ci_summary=status.ci_summary,
            sandbox_mode=status.sandbox_mode,
            sandbox_backend=status.sandbox_backend,
            sandbox_network_enabled=status.sandbox_network_enabled,
            sandbox_host_fallback=status.sandbox_host_fallback,
            security_events=status.security_events,
            duration_seconds=elapsed,
            error=str(exc),
        )
        _run_results[task.id] = result
        _record_run_metrics(result)

    finally:
        lease_stop.set()
        await lease_task
        _tasks[task.id] = task
        _background_tasks.pop(task.id, None)
        _persist_runtime_state()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/tasks", response_model=Task, status_code=201)
async def create_task(payload: TaskCreate) -> Task:
    if get_blueprint(payload.blueprint) is None:
        raise HTTPException(status_code=400, detail=f"Blueprint '{payload.blueprint}' not found")
    task = Task(**payload.model_dump(), source="api")
    _tasks[task.id] = task
    _run_statuses[task.id] = RunStatus(
        task_id=task.id,
        run_id=str(uuid.uuid4()),
        source=task.source,
        priority=task.priority,
        created_by=task.created_by,
        status="pending",
        current_step="queued",
        steps_completed=0,
        total_steps=0,
        started_at=datetime.now(timezone.utc),
    )
    _persist_runtime_state()
    logger.info("task_created", task_id=task.id, description=task.description[:80])
    return task.model_copy(deep=True)


@router.get("/tasks", response_model=list[Task])
async def list_tasks(status: str | None = Query(None)) -> list[Task]:
    tasks = list(_tasks.values())
    if status:
        tasks = [t for t in tasks if t.status == status]
    return sorted(tasks, key=lambda t: t.created_at, reverse=True)


@router.get("/runs", response_model=list[RunResult])
async def list_runs(
    success: bool | None = Query(None),
    status: str | None = Query(None),
    priority: str | None = Query(None),
    source: str | None = Query(None),
    ci_conclusion: str | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> list[RunResult]:
    query = (q or "").strip().lower()
    results = _sorted_results()
    filtered: list[RunResult] = []
    for result in results:
        task = _tasks.get(result.task_id)
        if success is not None and result.success != success:
            continue
        if status and task and task.status != status:
            continue
        if priority and result.priority != priority:
            continue
        if source and result.source != source:
            continue
        if ci_conclusion and (result.ci_conclusion or "") != ci_conclusion:
            continue
        if query:
            haystacks = [
                result.task_id,
                result.run_id,
                result.source,
                result.created_by,
                result.pr_url or "",
                task.description if task else "",
            ]
            if not any(query in value.lower() for value in haystacks):
                continue
        filtered.append(result)
        if len(filtered) >= limit:
            break
    return filtered


@router.get("/runs/summary")
async def get_run_summary() -> dict[str, Any]:
    results = _sorted_results()
    tasks = _tasks
    statuses: dict[str, int] = {}
    priorities: dict[str, int] = {}
    sources: dict[str, int] = {}
    ci_conclusions: dict[str, int] = {}
    total_duration = 0.0
    total_tokens = 0
    partial_successes = 0

    for result in results:
        task = tasks.get(result.task_id)
        status_value = task.status if task else ("completed" if result.success else "failed")
        statuses[status_value] = statuses.get(status_value, 0) + 1
        priorities[result.priority] = priorities.get(result.priority, 0) + 1
        sources[result.source] = sources.get(result.source, 0) + 1
        if result.ci_conclusion:
            ci_conclusions[result.ci_conclusion] = ci_conclusions.get(result.ci_conclusion, 0) + 1
        total_duration += result.duration_seconds
        total_tokens += result.tokens_used
        if result.success and task and task.status != "completed":
            partial_successes += 1

    count = len(results)
    recent_failures = []
    for result in results:
        task = tasks.get(result.task_id)
        if task and task.status == "failed":
            recent_failures.append(
                {
                    "task_id": result.task_id,
                    "run_id": result.run_id,
                    "description": task.description,
                    "error": result.error or "",
                    "ci_conclusion": result.ci_conclusion,
                }
            )
        if len(recent_failures) >= 10:
            break

    return {
        "total_runs": count,
        "statuses": statuses,
        "priorities": priorities,
        "sources": sources,
        "ci_conclusions": ci_conclusions,
        "avg_duration": round(total_duration / count, 2) if count else 0.0,
        "avg_tokens": (total_tokens // count) if count else 0,
        "partial_successes": partial_successes,
        "recent_failures": recent_failures,
    }


@router.get("/tasks/{task_id}/logs/history")
async def get_task_logs_history(
    task_id: str,
    limit: int = Query(200, ge=1, le=2000),
    contains: str | None = Query(None),
) -> dict[str, Any]:
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    lines = list(_run_logs.get(task_id, []))
    if contains:
        needle = contains.lower()
        lines = [line for line in lines if needle in line.lower()]
    lines = lines[-limit:]
    return {
        "task_id": task_id,
        "line_count": len(lines),
        "lines": lines,
    }


@router.get("/logs/search")
async def search_logs(
    q: str = Query(..., min_length=1),
    task_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    needle = q.lower()
    matches: list[dict[str, str]] = []
    task_ids = [task_id] if task_id else list(_run_logs.keys())
    for candidate_task_id in task_ids:
        lines = _run_logs.get(candidate_task_id, [])
        for line in lines:
            if needle not in line.lower():
                continue
            matches.append({"task_id": candidate_task_id, "line": line})
            if len(matches) >= limit:
                return {"query": q, "count": len(matches), "matches": matches}
    return {"query": q, "count": len(matches), "matches": matches}


@router.get("/tasks/{task_id}", response_model=Task)
async def get_task(task_id: str) -> Task:
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/tasks/{task_id}/status", response_model=RunStatus)
async def get_task_status(task_id: str) -> RunStatus:
    status = _run_statuses.get(task_id)
    if not status:
        raise HTTPException(status_code=404, detail="No run status for this task")
    return status


@router.get("/tasks/{task_id}/result", response_model=RunResult)
async def get_task_result(task_id: str) -> RunResult:
    result = _run_results.get(task_id)
    if not result:
        raise HTTPException(status_code=404, detail="No run result for this task")
    return result


@router.get("/tasks/{task_id}/logs")
async def get_task_logs(task_id: str) -> StreamingResponse:
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    async def _stream() -> Any:
        sent = 0
        while True:
            logs = _run_logs.get(task_id, [])
            for line in logs[sent:]:
                yield line + "\n"
                sent_local = sent  # noqa: F841 – intentional
            sent = len(logs)

            # Stop streaming once the task is terminal
            task = _tasks.get(task_id)
            if task and task.status in ("completed", "failed"):
                # flush remaining
                remaining = _run_logs.get(task_id, [])[sent:]
                for line in remaining:
                    yield line + "\n"
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(_stream(), media_type="text/plain")


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict[str, str]:
    bg = _background_tasks.get(task_id)
    if not bg:
        raise HTTPException(status_code=404, detail="No running task to cancel")
    bg.cancel()
    return {"status": "cancelling", "task_id": task_id}


@router.get("/blueprints")
async def list_blueprints() -> list[dict[str, Any]]:
    return [
        {
            "name": bp.name,
            "description": bp.description,
            "steps": [step.name for step in bp.steps],
        }
        for bp in load_blueprints()
    ]


@router.get("/worker/health")
async def worker_health() -> dict[str, Any]:
    status = worker_status()
    status["status"] = "ok"
    status["tasks_pending"] = sum(1 for task in _tasks.values() if task.status == "pending")
    return status


@router.get("/health")
async def health_check() -> dict[str, Any]:
    status = worker_status()
    return {
        "status": "ok",
        **status,
        "tasks_total": len(_tasks),
        "tasks_running": sum(1 for t in _tasks.values() if t.status == "running"),
        "tasks_pending": sum(1 for t in _tasks.values() if t.status == "pending"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
