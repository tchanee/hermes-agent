"""Durable, session-scoped control of detached Hermes workers."""

from __future__ import annotations

import json
import secrets
import threading
from typing import Any, Optional

from gateway.codex_control_store import CodexControlStore


MAX_GOAL_CHARS = 8000
MAX_CONTEXT_CHARS = 16000
MAX_STEER_CHARS = 4000

_IMPORTANT_SIGNALS = {
    "architecture", "migration", "security", "vulnerability", "incident",
    "trading", "financial", "production", "deploy", "database", "schema",
    "complex", "rigorous", "audit", "review", "refactor", "multi-file",
}

_HERMES_NATIVE_TOOLSETS = frozenset({"cronjob", "kanban", "skills"})


def govern_worker_runtime(toolsets: list[str]) -> tuple[str, str]:
    """Keep native Hermes services in Hermes; run general workers in Codex."""
    native = sorted({item.strip().lower() for item in toolsets} & _HERMES_NATIVE_TOOLSETS)
    if native:
        return "hermes", "requires Hermes-native toolsets: " + ", ".join(native)
    return "codex", "general detached work defaults to Codex"


def govern_importance(
    goal: str, requested: str, toolsets: list[str], user_text: str,
) -> tuple[str, str]:
    """Gateway-owned conservative tier decision; model input is only evidence."""
    words = {word.strip(".,:;()[]{}").lower() for word in user_text.split()}
    signals = sorted(words & _IMPORTANT_SIGNALS)
    complex_shape = len(user_text) >= 240 and len(toolsets) >= 2
    if requested == "important" and (signals or complex_shape):
        reason = "matched consequential signals: " + ", ".join(signals[:5]) if signals else "multi-tool complex task"
        return "important", reason
    if requested == "important":
        return "routine", "important request lacked gateway policy evidence"
    return "routine", "routine is the enforced default"


class CodexDelegationService:
    def __init__(self, *, store: CodexControlStore) -> None:
        self.store = store
        self._parents: dict[tuple[str, int], Any] = {}
        self._frozen_generations: set[tuple[str, int]] = set()
        self._lock = threading.RLock()
        from tools.async_delegation import register_lifecycle_observer
        register_lifecycle_observer(self._on_lifecycle)

    def close(self) -> None:
        from tools.async_delegation import unregister_lifecycle_observer
        unregister_lifecycle_observer(self._on_lifecycle)

    def recover_pending_outbox(self) -> int:
        from tools.process_registry import process_registry
        count = 0
        for row in self.store.list_pending_outbox():
            payload = json.loads(row["payload_json"])
            payload["control_event_id"] = row["event_id"]
            process_registry.completion_queue.put(payload)
            count += 1
        return count

    def _on_lifecycle(
        self, record: dict[str, Any], result: dict[str, Any], status: str
    ) -> Optional[str]:
        if self.store.get_delegation(str(record.get("delegation_id") or "")) is None:
            return None
        durable = self.store.get_delegation(str(record.get("delegation_id") or "")) or {}
        terminal = status if status in {"completed", "error", "interrupted"} else "error"
        payload = {
            "type": "async_delegation",
            "delegation_id": record["delegation_id"],
            "session_key": record.get("session_key", ""),
            "origin_session_id": record.get("origin_session_id"),
            "origin_generation": record.get("origin_generation"),
            "goal": record.get("goal", ""), "goals": record.get("goals"),
            "context": record.get("context"), "toolsets": record.get("toolsets"),
            "role": record.get("role"), "model": result.get("model") or record.get("model"),
            "worker_runtime": durable.get("worker_runtime", "hermes"),
            "status": terminal, "summary": result.get("summary"),
            "error": result.get("error"), "api_calls": result.get("api_calls", 0),
            "duration_seconds": result.get("duration_seconds"),
            "total_duration_seconds": result.get("total_duration_seconds"),
            "results": result.get("results"), "is_batch": bool(record.get("is_batch")),
            "dispatched_at": record.get("dispatched_at"),
            "completed_at": record.get("completed_at"),
        }
        return self.store.complete_delegation_with_outbox(
            delegation_id=record["delegation_id"], state=terminal,
            result=result, payload=payload,
        )

    def methods(self):
        return {
            "workers.spawn": ("workers.spawn", self.spawn),
            "workers.status": ("workers.read", self.status),
            "workers.steer": ("workers.steer", self.steer),
            "workers.cancel": ("workers.cancel", self.cancel),
        }

    def bind_parent(self, session_id: str, generation: int, agent: Any) -> None:
        with self._lock:
            self._parents[(session_id, generation)] = agent

    def freeze_generation(self, session_id: str, generation: int, *, reason: str) -> None:
        key = (session_id, int(generation))
        with self._lock:
            self._frozen_generations.add(key)
        self.store.record_audit(
            event_type="worker_generation_frozen", session_id=session_id,
            generation=int(generation), detail={"reason": reason},
        )

    def unfreeze_generation(self, session_id: str, generation: int, *, reason: str) -> None:
        key = (session_id, int(generation))
        with self._lock:
            self._frozen_generations.discard(key)
        self.store.record_audit(
            event_type="worker_generation_unfrozen", session_id=session_id,
            generation=int(generation), detail={"reason": reason},
        )

    def unbind_parent(self, session_id: str, generation: int, agent: Any) -> None:
        with self._lock:
            key = (session_id, generation)
            if self._parents.get(key) is agent:
                self._parents.pop(key, None)

    def spawn(self, params: dict[str, Any], principal: dict[str, Any]) -> dict[str, Any]:
        goal = str(params.get("goal") or "").strip()
        context = str(params.get("context") or "").strip() or None
        toolsets = params.get("toolsets") or []
        role = str(params.get("role") or "leaf").strip().lower()
        importance = str(params.get("importance") or "routine").strip().lower()
        idempotency_key = str(params.get("idempotency_key") or "").strip()
        if not goal or len(goal) > MAX_GOAL_CHARS:
            raise ValueError(f"goal is required and limited to {MAX_GOAL_CHARS} characters")
        if context and len(context) > MAX_CONTEXT_CHARS:
            raise ValueError(f"context is limited to {MAX_CONTEXT_CHARS} characters")
        if not isinstance(toolsets, list) or any(not isinstance(item, str) for item in toolsets):
            raise ValueError("toolsets must be a list of names")
        if role not in {"leaf", "orchestrator"}:
            raise ValueError("role must be leaf or orchestrator")
        if importance not in {"routine", "important"}:
            raise ValueError("importance must be routine or important")
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("idempotency_key is required and limited to 128 characters")

        generation_key = (principal["session_id"], int(principal["generation"]))
        with self._lock:
            if generation_key in self._frozen_generations:
                raise RuntimeError("worker dispatch is frozen for runtime transition")
            parent = self._parents.get((principal["session_id"], int(principal["generation"])))
        if parent is None:
            raise RuntimeError("responsive parent agent is unavailable for this session generation")
        user_text = self._latest_user_text(parent)
        governed_importance, policy_reason = govern_importance(
            goal, importance, toolsets, user_text
        )
        worker_runtime, runtime_reason = govern_worker_runtime(toolsets)
        if role == "orchestrator" and governed_importance != "important":
            role = "leaf"
            policy_reason += "; orchestrator role downgraded to leaf"
        payload = {"goal": goal, "context": context, "toolsets": toolsets,
                   "role": role, "requested_importance": importance,
                   "governed_importance": governed_importance,
                   "worker_runtime": worker_runtime,
                   "user_evidence_hash": __import__("hashlib").sha256(user_text.encode()).hexdigest()}
        inbox = self.store.accept_request(
            principal_id=principal["principal_id"], profile=principal["profile"],
            session_id=principal["session_id"], generation=principal["generation"],
            method="workers.spawn", idempotency_key=idempotency_key, payload=payload,
        )
        existing = self._delegation_for_inbox(int(inbox["id"]))
        if existing:
            return self._public(existing)
        if not self.store.claim_request(int(inbox["id"])):
            raise RuntimeError("identical worker spawn is already being dispatched")

        delegation_id = "deleg_" + secrets.token_hex(8)
        row = self.store.create_delegation(
            inbox_id=int(inbox["id"]), principal=principal,
            session_key=principal["session_key"], delegation_id=delegation_id,
            goal=goal, context=context, toolsets=toolsets, role=role,
            importance=governed_importance,
            worker_runtime=worker_runtime,
            model_policy=("Sol/xhigh" if governed_importance == "important" else "Terra/default"),
            policy_reason=policy_reason,
        )
        self.store.record_audit(
            event_type="worker_governed", principal_id=principal["principal_id"],
            profile=principal["profile"], session_id=principal["session_id"],
            generation=int(principal["generation"]),
            detail={"delegation_id": delegation_id, "requested": importance,
                    "governed": governed_importance, "reason": policy_reason,
                    "worker_runtime": worker_runtime, "runtime_reason": runtime_reason},
        )

        from tools.delegate_tool import delegate_task
        raw = delegate_task(
            goal=goal, context=context, toolsets=toolsets or None, role=role,
            tier=governed_importance, background=True, parent_agent=parent,
            control_delegation_id=delegation_id, worker_runtime=worker_runtime,
        )
        dispatch = json.loads(raw)
        if dispatch.get("status") != "dispatched":
            self.store.update_delegation_state(delegation_id, state="error", result=dispatch)
            self.store.finish_request(int(inbox["id"]), error=dispatch)
            raise RuntimeError(str(dispatch.get("error") or "worker dispatch failed"))
        if dispatch["delegation_id"] != delegation_id:
            raise RuntimeError("worker registry returned an unexpected delegation id")
        row = self.store.update_delegation_state(delegation_id, state="running")
        self.store.finish_request(int(inbox["id"]), result={"delegation_id": delegation_id})
        return self._public(row)

    def status(self, params: dict[str, Any], principal: dict[str, Any]) -> dict[str, Any]:
        requested = str(params.get("delegation_id") or "").strip()
        self._refresh(principal)
        rows = self.store.list_delegations(
            session_id=principal["session_id"], generation=int(principal["generation"])
        )
        if requested:
            rows = [row for row in rows if row["delegation_id"] == requested]
        return {"workers": [self._public(row) for row in rows]}

    def active_for_session(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.store.list_delegations_for_session(session_id=session_id)
        return [self._public(row) for row in rows if row["state"] in {
            "prepared", "running", "pending_delivery", "cancel_requested"
        }]

    def steer(self, params: dict[str, Any], principal: dict[str, Any]) -> dict[str, Any]:
        delegation_id, row = self._owned(params, principal)
        message = str(params.get("message") or "").strip()
        if not message or len(message) > MAX_STEER_CHARS:
            raise ValueError(f"message is required and limited to {MAX_STEER_CHARS} characters")
        inbox, command = self._command(
            params=params, principal=principal, delegation_id=delegation_id,
            command="steer", payload={"message": message},
        )
        if command["state"] != "accepted":
            return {"delegation_id": delegation_id, "accepted": command["state"] == "applied",
                    "command_id": command["command_id"]}
        from tools.async_delegation import steer_delegation
        applied = steer_delegation(delegation_id, message)
        command = self.store.update_delegation_command(
            command["command_id"], state="applied" if applied else "rejected"
        )
        self.store.finish_request(int(inbox["id"]), result={"applied": applied})
        return {"delegation_id": row["delegation_id"], "accepted": applied,
                "command_id": command["command_id"]}

    def cancel(self, params: dict[str, Any], principal: dict[str, Any]) -> dict[str, Any]:
        delegation_id, row = self._owned(params, principal)
        inbox, command = self._command(
            params=params, principal=principal, delegation_id=delegation_id,
            command="cancel", payload={},
        )
        if command["state"] != "accepted":
            return {"delegation_id": delegation_id, "accepted": command["state"] == "applied",
                    "command_id": command["command_id"]}
        self.store.update_delegation_state(delegation_id, state="cancel_requested")
        from tools.async_delegation import interrupt_delegation
        applied = interrupt_delegation(delegation_id, "Codex parent cancellation")
        command = self.store.update_delegation_command(
            command["command_id"], state="applied" if applied else "rejected"
        )
        self.store.finish_request(int(inbox["id"]), result={"applied": applied})
        return {"delegation_id": row["delegation_id"], "accepted": applied,
                "command_id": command["command_id"]}

    def _refresh(self, principal: dict[str, Any]) -> None:
        from tools.async_delegation import list_async_delegations
        active = {row["delegation_id"]: row for row in list_async_delegations()}
        for durable in self.store.list_delegations(
            session_id=principal["session_id"], generation=int(principal["generation"])
        ):
            live = active.get(durable["delegation_id"])
            if not live:
                continue
            state = live.get("status") or durable["state"]
            if durable["state"] == "cancel_requested" and state == "running":
                continue
            if state not in {"running", "pending_delivery", "completed", "error", "interrupted"}:
                state = "error"
            result = live.get("delivery_result")
            self.store.update_delegation_state(durable["delegation_id"], state=state, result=result)

    def _command(
        self, *, params: dict[str, Any], principal: dict[str, Any],
        delegation_id: str, command: str, payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        idempotency_key = str(params.get("idempotency_key") or "").strip()
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("idempotency_key is required and limited to 128 characters")
        inbox = self.store.accept_request(
            principal_id=principal["principal_id"], profile=principal["profile"],
            session_id=principal["session_id"], generation=principal["generation"],
            method=f"workers.{command}", idempotency_key=idempotency_key,
            payload={"delegation_id": delegation_id, **payload},
        )
        command_row = self.store.record_delegation_command(
            inbox_id=int(inbox["id"]), delegation_id=delegation_id, command=command,
            payload=payload, actor=principal["principal_id"],
        )
        return inbox, command_row

    def _owned(self, params: dict[str, Any], principal: dict[str, Any]):
        delegation_id = str(params.get("delegation_id") or "").strip()
        row = self.store.get_delegation(delegation_id)
        if row is None:
            raise KeyError(delegation_id)
        if row["session_id"] != principal["session_id"] or int(row["generation"]) != int(principal["generation"]):
            raise PermissionError("worker belongs to a different session generation")
        return delegation_id, row

    def _delegation_for_inbox(self, inbox_id: int) -> Optional[dict[str, Any]]:
        conn = self.store._connect()
        try:
            row = conn.execute("SELECT * FROM control_delegations WHERE inbox_id=?", (inbox_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row.get(key) for key in (
            "delegation_id", "goal", "role", "importance", "model_policy",
            "policy_reason", "worker_runtime", "state", "created_at", "updated_at",
        )}

    @staticmethod
    def _latest_user_text(parent: Any) -> str:
        for message in reversed(getattr(parent, "_session_messages", None) or []):
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                return message["content"][-8000:]
        return ""
