#!/usr/bin/env python3
"""
Async (background) delegation registry.

Backs ``delegate_task(background=true)``: the parent agent dispatches a
subagent that runs on a module-level daemon executor and returns a handle
immediately, so the user and the model can keep working while the child runs.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. The
CLI (``cli.py`` process_loop) and gateway (``_run_process_watcher`` /
``completion_queue`` drain) already poll that queue while the agent is idle
and forge a fresh user/internal turn from each event. We deliberately reuse
that rail rather than reaching into a running agent loop:

  - completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - we inherit the queue's de-dup, crash-recovery checkpoint, and the
    existing CLI + gateway drain wiring for free — no new drain loops in the
    two largest files in the repo.

The completion payload carries a RICH, self-contained task-source block (the
original goal, the context the parent supplied, toolsets, model, dispatch
time, status, and the full result summary). When the result re-enters the
conversation the parent may be deep in unrelated context and won't remember
why the subagent existed; the block lets it either use the result or
re-dispatch if the world has moved on.

This module owns ONLY the async lifecycle. The actual child build + run is
delegated back to ``delegate_tool._run_single_child`` via an injected
runner, so all the credential leasing, heartbeat, timeout, and result-shaping
logic stays in one place.
"""

from __future__ import annotations

import logging
import json
import os
from pathlib import Path
import threading
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor variant whose workers do not block process exit.

    Stdlib ``ThreadPoolExecutor`` workers are non-daemon. Background
    delegation is explicitly best-effort detached work, so a long child should
    be interruptible by ``/stop``/shutdown but must not keep a CLI process alive
    after the user exits.
    """

    def _adjust_thread_count(self) -> None:
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
            t = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
                daemon=True,
            )
            t.start()
            self._threads.add(t)


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# A persistent daemon executor (NOT a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat the whole point of async). Workers are daemon
# threads so a hard process exit doesn't hang on an in-flight child.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict. Kept for the lifetime of the run plus a short
# tail after completion so `list_async_delegations()` can show recent results.
_records: Dict[str, Dict[str, Any]] = {}
_lifecycle_observers: List[Callable[[Dict[str, Any], Dict[str, Any], str], Optional[str]]] = []

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# How many completed records to retain for status queries before pruning.
_MAX_RETAINED_COMPLETED = 50
try:
    from gateway.status import get_process_start_time
    _OWNER_START_TIME = get_process_start_time(os.getpid())
except Exception:
    _OWNER_START_TIME = None
_LEDGER_DIR = get_hermes_home() / "async_delegations"
_LEDGER_PATH = _LEDGER_DIR / f"{os.getpid()}-{_OWNER_START_TIME or 'unknown'}.json"


def _serializable_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in record.items() if k not in {"interrupt_fn", "steer_fn"}}


def _write_ledger_locked() -> None:
    """Persist active work so a replacement gateway can reconcile it."""
    active = [
        _serializable_record(r)
        for r in _records.values()
        if r.get("status") in {"running", "pending_delivery"}
    ]
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LEDGER_PATH.with_suffix(f".tmp.{os.getpid()}")
    payload = {
        "owner_pid": os.getpid(),
        "owner_start_time": _OWNER_START_TIME,
        "records": active,
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _LEDGER_PATH)


def reconcile_orphaned_delegations() -> int:
    """Emit interrupted events for work abandoned by a previous process."""
    count = 0
    candidates = set(_LEDGER_DIR.glob("*.json")) if _LEDGER_DIR.exists() else set()
    if _LEDGER_PATH.exists():
        candidates.add(_LEDGER_PATH)
    for path in candidates:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read async delegation ledger %s: %s", path, exc)
            continue
        owner_pid = raw.get("owner_pid") if isinstance(raw, dict) else None
        owner_start = raw.get("owner_start_time") if isinstance(raw, dict) else None
        records = raw.get("records", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
        if not isinstance(records, list):
            logger.error(
                "Skipping malformed async delegation ledger %s: records is not a list",
                path,
            )
            continue
        if owner_pid:
            try:
                from gateway.status import get_process_start_time
                if get_process_start_time(int(owner_pid)) == owner_start:
                    continue
            except Exception:
                continue
        now = time.time()
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("status") == "pending_delivery":
                result = record.get("delivery_result") or {}
                delivery_status = record.get("delivery_status") or result.get("status") or "completed"
            elif record.get("status") == "running":
                record["status"] = "pending_delivery"
                record["completed_at"] = now
                delivery_status = "interrupted"
                result = {
                    "status": "interrupted",
                    "summary": None,
                    "error": "Owning Hermes process exited before this delegated worker reported completion.",
                    "api_calls": 0,
                    "duration_seconds": round(now - float(record.get("dispatched_at") or now), 2),
                    "exit_reason": "owner_process_exit",
                }
                record["delivery_result"] = result
                record["delivery_status"] = delivery_status
            else:
                continue
            record["ledger_path"] = str(path)
            if record.get("is_batch"):
                enqueued = _push_batch_completion_event(record, result, delivery_status)
            else:
                enqueued = _push_completion_event(record, result, delivery_status)
            if enqueued:
                count += 1
        # Keep pending deliveries durable until the gateway acknowledges that
        # their synthetic turn was accepted. A crash may redeliver, but cannot
        # silently discard the result.
        try:
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            payload = {"owner_pid": owner_pid, "owner_start_time": owner_start, "records": records}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.error("Could not persist reconciled delegation ledger %s: %s", path, exc)
    if count:
        logger.warning("Reconciled %d orphaned async delegation(s) after restart", count)
    return count


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow) the shared daemon executor.

    We never shrink — ThreadPoolExecutor can't resize — but if the configured
    cap grows between calls we rebuild a larger pool. Existing in-flight
    futures keep running on the old pool until it's garbage collected.
    """
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            # Daemon threads: thread_name_prefix aids debugging in stack dumps.
            _executor = _DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="async-delegate",
            )
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """Number of async delegations currently running."""
    with _records_lock:
        return sum(1 for r in _records.values() if r.get("status") == "running")


def register_lifecycle_observer(callback) -> None:
    with _records_lock:
        if callback not in _lifecycle_observers:
            _lifecycle_observers.append(callback)


def unregister_lifecycle_observer(callback) -> None:
    with _records_lock:
        if callback in _lifecycle_observers:
            _lifecycle_observers.remove(callback)


def _notify_lifecycle(record: Dict[str, Any], result: Dict[str, Any], status: str) -> Optional[str]:
    with _records_lock:
        observers = list(_lifecycle_observers)
    event_id = None
    for callback in observers:
        try:
            event_id = callback(record, result, status) or event_id
        except Exception:
            logger.exception("Async delegation lifecycle observer failed")
    return event_id


def acknowledge_delivery(delegation_id: str, ledger_path: Optional[str]) -> bool:
    """Remove one durably queued completion after gateway acceptance."""
    if not delegation_id or not ledger_path:
        return False
    path = Path(ledger_path)
    with _records_lock:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            records = raw.get("records", []) if isinstance(raw, dict) else raw
            remaining = [
                record for record in records
                if not isinstance(record, dict)
                or record.get("delegation_id") != delegation_id
            ]
            if len(remaining) == len(records):
                return False
            if remaining:
                payload = dict(raw) if isinstance(raw, dict) else {"records": remaining}
                payload["records"] = remaining
                tmp = path.with_suffix(f".tmp.{os.getpid()}")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, path)
            else:
                path.unlink(missing_ok=True)
            local = _records.get(delegation_id)
            if local and local.get("status") == "pending_delivery":
                local["status"] = local.get("delivery_status") or "completed"
                local.pop("delivery_result", None)
                local.pop("delivery_status", None)
                local.pop("ledger_path", None)
                _prune_completed_locked()
            return True
        except FileNotFoundError:
            return False
        except Exception as exc:
            logger.error("Could not acknowledge async delegation %s: %s", delegation_id, exc)
            return False


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the retention cap.

    Caller must hold ``_records_lock``.
    """
    completed = [
        (rid, r)
        for rid, r in _records.items()
        if r.get("status") != "running"
    ]
    if len(completed) <= _MAX_RETAINED_COMPLETED:
        return
    # Oldest-first by completion time (fall back to dispatch time).
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: len(completed) - _MAX_RETAINED_COMPLETED]:
        _records.pop(rid, None)


def dispatch_async_delegation(
    *,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    runner: Callable[[], Dict[str, Any]],
    interrupt_fn: Optional[Callable[[], None]] = None,
    steer_fn: Optional[Callable[[str], bool]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.

    Parameters
    ----------
    goal, context, toolsets, role, model
        The dispatch-time task spec, captured verbatim for the rich
        completion block.
    session_key
        The gateway session_key (from ``tools.approval.get_current_session_key``)
        captured on the parent thread BEFORE dispatch, because the daemon
        worker thread won't carry the contextvar. Used to route the
        completion back to the originating session.
    runner
        Zero-arg callable that builds + runs the child and returns the same
        result dict ``_run_single_child`` produces. Runs on the worker thread.
    interrupt_fn
        Optional callable to signal the child to stop (used on shutdown /
        explicit cancel).
    max_async_children
        Concurrency cap. When at capacity the dispatch is REJECTED (the caller
        should fall back to sync or tell the user) rather than queued, so a
        runaway model can't pile up unbounded background work.

    Returns
    -------
    dict
        ``{"status": "dispatched", "delegation_id": ...}`` on success, or
        ``{"status": "rejected", "error": ...}`` when at capacity.
    """
    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "steer_fn": steer_fn,
    }
    # Capacity check and record insert under ONE lock hold — checking
    # active_count() separately would let two concurrent dispatches (e.g.
    # from different gateway sessions) both pass the check and exceed the cap.
    with _records_lock:
        if delegation_id in _records:
            return {"status": "rejected", "error": "delegation id already exists"}
        running = sum(
            1 for r in _records.values() if r.get("status") == "running"
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or run this task synchronously "
                    f"(background=false). Raise delegation.max_async_children in "
                    f"config.yaml to allow more concurrent background subagents."
                ),
            }
        _records[delegation_id] = record
        try:
            _write_ledger_locked()
        except Exception as exc:
            _records.pop(delegation_id, None)
            return {"status": "rejected", "error": f"Could not persist async delegation: {exc}"}

    executor = _get_executor(max_async_children)

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        try:
            result = runner() or {}
            status = result.get("status") or "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation %s crashed", delegation_id)
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize(delegation_id, result, status)

    try:
        executor.submit(_worker)
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        with _records_lock:
            _records.pop(delegation_id, None)
            _write_ledger_locked()
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation: {exc}",
        }

    logger.info(
        "Dispatched async delegation %s (session_key=%s): %s",
        delegation_id, session_key or "<cli>", (goal or "")[:80],
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize(delegation_id: str, result: Dict[str, Any], status: str) -> None:
    """Mark a record complete and push the completion event onto the queue."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = "pending_delivery"
        record["completed_at"] = time.time()
        record["delivery_result"] = result
        record["delivery_status"] = status
        record["ledger_path"] = str(_LEDGER_PATH)
        record["interrupt_fn"] = None  # drop the closure; child is done
        record["steer_fn"] = None
        # Snapshot fields needed for the event while holding the lock.
        event_record = dict(record)
    control_event_id = _notify_lifecycle(event_record, result, status)
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        if control_event_id:
            record["control_event_id"] = control_event_id
            event_record["control_event_id"] = control_event_id
        _write_ledger_locked()

    enqueued = _push_completion_event(event_record, result, status)
    if not enqueued:
        logger.error("Async delegation %s remains pending durable delivery", delegation_id)


def _push_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> bool:
    """Push a type='async_delegation' event onto the shared completion queue.

    Best-effort: a failure here must not crash the worker, but it WOULD mean a
    silently-lost result, so we log loudly.
    """
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s finished but process_registry import failed; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )
        return False

    summary = result.get("summary")
    error = result.get("error")
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()

    evt = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        # session_key routes the completion back to the originating gateway
        # session; empty string => CLI (single-session) path.
        "session_key": record.get("session_key", ""),
        "goal": record.get("goal", ""),
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role"),
        "model": result.get("model") or record.get("model"),
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": result.get("api_calls", 0),
        "duration_seconds": result.get(
            "duration_seconds", round(completed_at - dispatched_at, 2)
        ),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "exit_reason": result.get("exit_reason"),
        "_async_ledger_path": record.get("ledger_path"),
        "control_event_id": record.get("control_event_id"),
    }
    try:
        process_registry.completion_queue.put(evt)
        return True
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s: failed to enqueue completion event; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )
        return False


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    runner: Callable[[], Dict[str, Any]],
    interrupt_fn: Optional[Callable[[], None]] = None,
    steer_fn: Optional[Callable[[str], bool]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    delegation_id: Optional[str] = None,
    origin_session_id: Optional[str] = None,
    origin_generation: Optional[int] = None,
) -> Dict[str, Any]:
    """Dispatch a WHOLE fan-out batch as ONE background unit.

    Unlike ``dispatch_async_delegation`` (which backs a single subagent),
    ``runner`` here runs the entire batch — it builds and joins on every child
    in parallel and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict that the synchronous path would have
    returned. We occupy ONE async slot for the whole batch (the in-batch
    parallelism is bounded separately by ``max_concurrent_children``), so a
    single ``delegate_task`` fan-out never exhausts the async pool by itself.

    When the batch finishes, a SINGLE completion event is pushed onto the
    shared ``process_registry.completion_queue`` carrying the full per-task
    ``results`` list, so the consolidated summaries re-enter the conversation
    as one message once every child is done — the chat is never blocked while
    they run.

    Returns ``{"status": "dispatched", "delegation_id": ...}`` on success or
    ``{"status": "rejected", "error": ...}`` when the async pool is at
    capacity.
    """
    delegation_id = delegation_id or _new_delegation_id()
    dispatched_at = time.time()
    n = len(goals)
    # A combined goal label for status listings / the completion header.
    combined_goal = (
        goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in goals)
    )
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": combined_goal,
        "goals": list(goals),
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "steer_fn": steer_fn,
        "is_batch": True,
        "origin_session_id": origin_session_id,
        "origin_generation": origin_generation,
    }
    with _records_lock:
        if delegation_id in _records:
            return {"status": "rejected", "error": "delegation id already exists"}
        running = sum(
            1 for r in _records.values() if r.get("status") == "running"
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or raise delegation.max_async_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        _records[delegation_id] = record
        try:
            _write_ledger_locked()
        except Exception as exc:
            _records.pop(delegation_id, None)
            return {"status": "rejected", "error": f"Could not persist async delegation batch: {exc}"}

    executor = _get_executor(max_async_children)

    def _worker() -> None:
        combined: Dict[str, Any] = {}
        status = "error"
        try:
            combined = runner() or {}
            # Batch status: completed unless every child errored/was interrupted.
            child_results = combined.get("results") or []
            if child_results and all(
                (r.get("status") not in ("completed", "success"))
                for r in child_results
            ):
                status = "error"
            else:
                status = "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation batch %s crashed", delegation_id)
            combined = {
                "results": [],
                "error": f"{type(exc).__name__}: {exc}",
                "total_duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize_batch(delegation_id, combined, status)

    try:
        executor.submit(_worker)
    except Exception as exc:  # pragma: no cover
        with _records_lock:
            _records.pop(delegation_id, None)
            _write_ledger_locked()
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation batch: {exc}",
        }

    logger.info(
        "Dispatched async delegation batch %s (%d task(s), session_key=%s)",
        delegation_id, n, session_key or "<cli>",
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """Mark a batch record complete and push ONE combined completion event."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = "pending_delivery"
        record["completed_at"] = time.time()
        record["delivery_result"] = combined
        record["delivery_status"] = status
        record["ledger_path"] = str(_LEDGER_PATH)
        record["interrupt_fn"] = None
        record["steer_fn"] = None
        event_record = dict(record)
    control_event_id = _notify_lifecycle(event_record, combined, status)
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        if control_event_id:
            record["control_event_id"] = control_event_id
            event_record["control_event_id"] = control_event_id
        _write_ledger_locked()

    enqueued = _push_batch_completion_event(event_record, combined, status)
    if not enqueued:
        logger.error("Async delegation batch %s remains pending durable delivery", delegation_id)


def _push_batch_completion_event(
    event_record: Dict[str, Any], combined: Dict[str, Any], status: str
) -> bool:
    """Push a batch completion, including restart-interrupted batches."""
    delegation_id = event_record.get("delegation_id", "unknown")
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s finished but process_registry import "
            "failed; result lost: %s",
            delegation_id, exc,
        )
        return False

    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": event_record.get("session_key", ""),
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "model": event_record.get("model"),
        "status": status,
        "is_batch": True,
        # The full per-task results list — the formatter renders a
        # consolidated multi-task block from this.
        "results": combined.get("results") or [],
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "_async_ledger_path": event_record.get("ledger_path"),
        "origin_session_id": event_record.get("origin_session_id"),
        "origin_generation": event_record.get("origin_generation"),
        "control_event_id": event_record.get("control_event_id"),
    }
    try:
        process_registry.completion_queue.put(evt)
        return True
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s: failed to enqueue completion event; "
            "result lost: %s",
            delegation_id, exc,
        )
        return False


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed).

    Safe to call from any thread. Excludes the non-serialisable interrupt_fn.
    """
    with _records_lock:
        return [
            {k: v for k, v in r.items() if k not in {"interrupt_fn", "steer_fn"}}
            for r in _records.values()
        ]


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop. Returns how many.

    Used on ``/stop`` and gateway shutdown so a dangling background subagent
    can't keep burning tokens with no one listening. The child still emits a
    completion event (status='interrupted') via the normal finalize path.
    """
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values() if r.get("status") == "running"
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_all: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info("Interrupted %d async delegation(s) (%s)", count, reason)
    return count


def interrupt_delegation(delegation_id: str, reason: str = "cancelled") -> bool:
    """Signal one detached delegation to stop."""
    with _records_lock:
        record = _records.get(delegation_id)
        fn = record.get("interrupt_fn") if record and record.get("status") == "running" else None
    if not callable(fn):
        return False
    fn()
    logger.info("Interrupted async delegation %s (%s)", delegation_id, reason)
    return True


def steer_delegation(delegation_id: str, message: str) -> bool:
    """Inject a user steering note into one running detached delegation."""
    if not str(message or "").strip():
        return False
    with _records_lock:
        record = _records.get(delegation_id)
        fn = record.get("steer_fn") if record and record.get("status") == "running" else None
    return bool(fn(str(message).strip())) if callable(fn) else False


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor."""
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    with _records_lock:
        _records.clear()
        try:
            _LEDGER_PATH.unlink(missing_ok=True)
        except OSError:
            pass
