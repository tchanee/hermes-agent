"""Durable state primitives for the scoped Codex control plane.

This module contains no network or model code. It establishes the transaction
boundaries required before stateful MCP methods can be exposed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class IdempotencyConflict(RuntimeError):
    pass


class CodexControlStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _insert_audit(
        conn: sqlite3.Connection, *, event_type: str,
        principal_id: Optional[str] = None, profile: Optional[str] = None,
        session_id: Optional[str] = None, generation: Optional[int] = None,
        detail: Any = None,
    ) -> None:
        conn.execute(
            """INSERT INTO control_audit
            (event_type,principal_id,profile,session_id,generation,detail_json,created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (
                event_type, principal_id, profile, session_id, generation,
                canonical_json(detail if detail is not None else {}), time.time(),
            ),
        )

    def record_audit(self, *, event_type: str, principal_id: Optional[str] = None,
                     profile: Optional[str] = None, session_id: Optional[str] = None,
                     generation: Optional[int] = None, detail: Any = None) -> None:
        with self.transaction() as conn:
            self._insert_audit(
                conn, event_type=event_type, principal_id=principal_id,
                profile=profile, session_id=session_id, generation=generation,
                detail=detail,
            )

    def list_audit(self, *, session_id: Optional[str] = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if session_id is None:
                rows = conn.execute("SELECT * FROM control_audit ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM control_audit WHERE session_id=? ORDER BY id",
                    (session_id,),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_inbox (
                    id INTEGER PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    method TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('accepted','in_progress','succeeded','failed')),
                    result_json TEXT,
                    error_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(principal_id, profile, session_id, generation, method, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS control_outbox (
                    event_id TEXT PRIMARY KEY,
                    inbox_id INTEGER NOT NULL UNIQUE REFERENCES control_inbox(id),
                    destination_session_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','accepted','processed','sent','platform_confirmed')),
                    lease_token TEXT,
                    lease_expires_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS codex_thread_bindings (
                    session_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    thread_id TEXT NOT NULL,
                    previous_thread_id TEXT,
                    policy_revision TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active','fenced','retired')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control_audit (
                    id INTEGER PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    principal_id TEXT,
                    profile TEXT,
                    session_id TEXT,
                    generation INTEGER,
                    detail_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control_capabilities (
                    token_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    audience TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    gateway_pid INTEGER NOT NULL,
                    gateway_start TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active','revoked','expired')),
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    inbox_id INTEGER NOT NULL REFERENCES control_inbox(id),
                    principal_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    target TEXT NOT NULL CHECK(target IN ('memory','user')),
                    operations_json TEXT NOT NULL,
                    operation_hash TEXT NOT NULL,
                    expected_revision TEXT NOT NULL,
                    expected_result_revision TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','approved','rejected','applied','stale','failed')),
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_provenance (
                    id INTEGER PRIMARY KEY,
                    proposal_id TEXT NOT NULL REFERENCES memory_proposals(proposal_id),
                    target TEXT NOT NULL,
                    operation_hash TEXT NOT NULL,
                    resulting_revision TEXT,
                    approval_actor TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_proposals_inbox
                    ON memory_proposals(inbox_id);
                CREATE TABLE IF NOT EXISTS control_delegations (
                    delegation_id TEXT PRIMARY KEY,
                    inbox_id INTEGER NOT NULL UNIQUE REFERENCES control_inbox(id),
                    principal_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    goal TEXT NOT NULL,
                    context TEXT,
                    toolsets_json TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('leaf','orchestrator')),
                    importance TEXT NOT NULL CHECK(importance IN ('routine','important')),
                    model_policy TEXT NOT NULL,
                    policy_reason TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('prepared','running','pending_delivery','completed','error','interrupted','cancel_requested')),
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control_delegation_commands (
                    command_id TEXT PRIMARY KEY,
                    inbox_id INTEGER NOT NULL UNIQUE REFERENCES control_inbox(id),
                    delegation_id TEXT NOT NULL REFERENCES control_delegations(delegation_id),
                    command TEXT NOT NULL CHECK(command IN ('steer','cancel')),
                    payload_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('accepted','applied','rejected')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS thread_handoffs (
                    session_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    revision TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_message_max_id INTEGER,
                    state TEXT NOT NULL CHECK(state IN ('verified','superseded')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(session_key, session_id, generation)
                );
                CREATE TABLE IF NOT EXISTS codex_thread_lineage (
                    id INTEGER PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    from_generation INTEGER NOT NULL,
                    to_generation INTEGER NOT NULL,
                    previous_thread_id TEXT NOT NULL,
                    new_thread_id TEXT,
                    reason TEXT NOT NULL,
                    policy_revision TEXT NOT NULL,
                    memory_revision TEXT NOT NULL,
                    handoff_revision TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('prepared','bound','rolled_back','failed')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(session_key, session_id, to_generation)
                );
                """
            )
        finally:
            conn.close()

    def issue_capability(
        self,
        *,
        profile: str,
        session_key: str,
        session_id: str,
        generation: int,
        scopes: list[str],
        gateway_pid: int,
        gateway_start: str,
        audience: str = "hermes-control",
        ttl_seconds: float = 3600,
    ) -> tuple[str, dict[str, Any]]:
        if not scopes or any(not isinstance(scope, str) or not scope for scope in scopes):
            raise ValueError("at least one non-empty scope is required")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        token_id = secrets.token_hex(16)
        secret = secrets.token_urlsafe(32)
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        principal_id = "codex:" + secrets.token_hex(16)
        now = time.time()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO control_capabilities
                (token_id,token_hash,principal_id,profile,session_key,session_id,
                 generation,audience,scopes_json,gateway_pid,gateway_start,state,
                 expires_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)""",
                (
                    token_id, digest, principal_id, profile, session_key,
                    session_id, generation, audience,
                    canonical_json(sorted(set(scopes))), gateway_pid,
                    gateway_start, now + ttl_seconds, now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM control_capabilities WHERE token_id=?", (token_id,)
            ).fetchone()
            self._insert_audit(
                conn, event_type="capability_issued", principal_id=principal_id,
                profile=profile, session_id=session_id, generation=generation,
                detail={"token_id": token_id, "scopes": sorted(set(scopes))},
            )
        return f"{token_id}.{secret}", dict(row)

    def validate_capability(
        self,
        token: str,
        *,
        audience: str,
        required_scope: str,
        gateway_pid: int,
        gateway_start: str,
        expected_session_id: Optional[str] = None,
        expected_generation: Optional[int] = None,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        token_id, sep, secret = str(token or "").partition(".")
        if not sep or not token_id or not secret:
            raise PermissionError("malformed capability")
        current_time = time.time() if now is None else now
        supplied_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        expired = False
        result: Optional[dict[str, Any]] = None
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM control_capabilities WHERE token_id=?", (token_id,)
            ).fetchone()
            if row is None or not secrets.compare_digest(row["token_hash"], supplied_hash):
                raise PermissionError("invalid capability")
            if row["state"] != "active":
                raise PermissionError(f"capability is {row['state']}")
            if row["expires_at"] <= current_time:
                conn.execute(
                    "UPDATE control_capabilities SET state='expired',updated_at=? WHERE token_id=?",
                    (current_time, token_id),
                )
                expired = True
            elif row["audience"] != audience:
                raise PermissionError("capability audience mismatch")
            elif row["gateway_pid"] != gateway_pid or row["gateway_start"] != gateway_start:
                raise PermissionError("capability gateway identity mismatch")
            elif expected_session_id is not None and row["session_id"] != expected_session_id:
                raise PermissionError("capability session mismatch")
            elif expected_generation is not None and row["generation"] != expected_generation:
                raise PermissionError("capability generation mismatch")
            else:
                scopes = json.loads(row["scopes_json"])
                if required_scope not in scopes:
                    raise PermissionError("capability scope denied")
                result = dict(row)
                result["scopes"] = scopes
        if expired:
            raise PermissionError("capability expired")
        assert result is not None
        return result

    def validate_live_binding(self, principal: dict[str, Any]) -> None:
        binding = self.get_thread_binding(str(principal["session_key"]))
        if (
            binding is None
            or binding["state"] != "active"
            or binding["session_id"] != principal["session_id"]
            or int(binding["generation"]) != int(principal["generation"])
        ):
            raise PermissionError("capability is not bound to the active Codex generation")

    def revoke_capability(self, token_id: str) -> bool:
        now = time.time()
        with self.transaction() as conn:
            cur = conn.execute(
                """UPDATE control_capabilities SET state='revoked',updated_at=?
                WHERE token_id=? AND state='active'""",
                (now, token_id),
            )
            if cur.rowcount == 1:
                self._insert_audit(
                    conn, event_type="capability_revoked",
                    detail={"token_id": token_id},
                )
            return cur.rowcount == 1

    def accept_request(
        self,
        *,
        principal_id: str,
        profile: str,
        session_id: str,
        generation: int,
        method: str,
        idempotency_key: str,
        payload: Any,
    ) -> dict[str, Any]:
        digest = request_hash(payload)
        now = time.time()
        key = (principal_id, profile, session_id, generation, method, idempotency_key)
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT * FROM control_inbox WHERE
                principal_id=? AND profile=? AND session_id=? AND generation=?
                AND method=? AND idempotency_key=?""",
                key,
            ).fetchone()
            if row is not None:
                if row["request_hash"] != digest:
                    raise IdempotencyConflict("idempotency key reused with different request")
                return dict(row)
            cur = conn.execute(
                """INSERT INTO control_inbox
                (principal_id,profile,session_id,generation,method,idempotency_key,
                 request_hash,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'accepted',?,?)""",
                (*key, digest, now, now),
            )
            self._insert_audit(
                conn, event_type="request_accepted", principal_id=principal_id,
                profile=profile, session_id=session_id, generation=generation,
                detail={"inbox_id": cur.lastrowid, "method": method,
                        "request_hash": digest},
            )
            return dict(conn.execute("SELECT * FROM control_inbox WHERE id=?", (cur.lastrowid,)).fetchone())

    def claim_request(self, inbox_id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE control_inbox SET state='in_progress',updated_at=? WHERE id=? AND state='accepted'",
                (time.time(), inbox_id),
            )
            return cur.rowcount == 1

    def finish_request(self, inbox_id: int, *, result: Any = None, error: Any = None) -> dict[str, Any]:
        state = "failed" if error is not None else "succeeded"
        with self.transaction() as conn:
            conn.execute(
                """UPDATE control_inbox SET state=?,result_json=?,error_json=?,updated_at=?
                WHERE id=? AND state IN ('accepted','in_progress')""",
                (state, canonical_json(result) if result is not None else None,
                 canonical_json(error) if error is not None else None, time.time(), inbox_id),
            )
            row = conn.execute("SELECT * FROM control_inbox WHERE id=?", (inbox_id,)).fetchone()
            if row is None:
                raise KeyError(inbox_id)
            return dict(row)

    def complete_with_outbox(
        self,
        *,
        inbox_id: int,
        result: Any,
        event_type: str,
        stable_action_id: str,
        destination_session_id: str,
        payload: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        payload_json = canonical_json(payload)
        payload_digest = request_hash(payload)
        event_id = request_hash(
            {
                "event_type": event_type,
                "action_id": stable_action_id,
                "destination": destination_session_id,
                "payload_hash": payload_digest,
            }
        )
        now = time.time()
        with self.transaction() as conn:
            inbox = conn.execute("SELECT * FROM control_inbox WHERE id=?", (inbox_id,)).fetchone()
            if inbox is None:
                raise KeyError(inbox_id)
            if inbox["state"] == "succeeded":
                outbox = conn.execute("SELECT * FROM control_outbox WHERE inbox_id=?", (inbox_id,)).fetchone()
                return dict(inbox), dict(outbox) if outbox else {}
            conn.execute(
                "UPDATE control_inbox SET state='succeeded', result_json=?, updated_at=? WHERE id=?",
                (canonical_json(result), now, inbox_id),
            )
            conn.execute(
                """INSERT OR IGNORE INTO control_outbox
                (event_id,inbox_id,destination_session_id,payload_hash,payload_json,state,created_at,updated_at)
                VALUES (?,?,?,?,?,'pending',?,?)""",
                (event_id, inbox_id, destination_session_id, payload_digest, payload_json, now, now),
            )
            return (
                dict(conn.execute("SELECT * FROM control_inbox WHERE id=?", (inbox_id,)).fetchone()),
                dict(conn.execute("SELECT * FROM control_outbox WHERE event_id=?", (event_id,)).fetchone()),
            )

    def cas_thread_binding(
        self,
        *,
        session_key: str,
        session_id: str,
        expected_generation: Optional[int],
        expected_thread_id: Optional[str],
        new_thread_id: str,
        policy_revision: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM codex_thread_bindings WHERE session_key=?", (session_key,)
            ).fetchone()
            if current is None:
                if expected_generation not in (None, 0) or expected_thread_id is not None:
                    raise IdempotencyConflict("thread binding expectation does not match empty state")
                generation = 1
                conn.execute(
                    """INSERT INTO codex_thread_bindings
                    (session_key,session_id,generation,thread_id,previous_thread_id,
                     policy_revision,state,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,'active',?,?)""",
                    (session_key, session_id, generation, new_thread_id, None, policy_revision, now, now),
                )
            else:
                if current["generation"] != expected_generation or current["thread_id"] != expected_thread_id:
                    raise IdempotencyConflict("stale thread binding compare-and-swap")
                session_changed = current["session_id"] != session_id
                generation = 1 if session_changed else int(current["generation"]) + 1
                conn.execute(
                    """UPDATE codex_thread_bindings SET session_id=?, generation=?, thread_id=?,
                    previous_thread_id=?, policy_revision=?, state='active', updated_at=?
                    WHERE session_key=?""",
                    (session_id, generation, new_thread_id, current["thread_id"], policy_revision, now, session_key),
                )
            return dict(conn.execute(
                "SELECT * FROM codex_thread_bindings WHERE session_key=?", (session_key,)
            ).fetchone())

    def get_thread_binding(self, session_key: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM codex_thread_bindings WHERE session_key=?",
                (session_key,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def fence_thread_binding(
        self, *, session_key: str, session_id: str, generation: int, thread_id: str,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            cur = conn.execute(
                """UPDATE codex_thread_bindings SET state='fenced',updated_at=?
                WHERE session_key=? AND session_id=? AND generation=?
                AND thread_id=? AND state='active'""",
                (time.time(), session_key, session_id, generation, thread_id),
            )
            if cur.rowcount != 1:
                raise IdempotencyConflict("thread binding changed before reseed fencing")
            return dict(conn.execute(
                "SELECT * FROM codex_thread_bindings WHERE session_key=?", (session_key,)
            ).fetchone())

    def revoke_gateway_capabilities(self, gateway_pid: int, gateway_start: str) -> int:
        now = time.time()
        with self.transaction() as conn:
            cur = conn.execute(
                """UPDATE control_capabilities SET state='revoked',updated_at=?
                WHERE gateway_pid=? AND gateway_start=? AND state='active'""",
                (now, gateway_pid, gateway_start),
            )
            return int(cur.rowcount)

    def create_memory_proposal(
        self,
        *,
        inbox_id: int,
        principal: dict[str, Any],
        target: str,
        operations: list[dict[str, Any]],
        expected_revision: str,
        expected_result_revision: str,
        rationale: str,
        source_kind: str,
        source_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if target not in {"memory", "user"}:
            raise ValueError("invalid memory target")
        proposal_id = "cp_mem_" + secrets.token_hex(8)
        operation_digest = request_hash({"target": target, "operations": operations})
        now = time.time()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM memory_proposals WHERE inbox_id=?", (inbox_id,)
            ).fetchone()
            if existing is not None:
                if existing["operation_hash"] != operation_digest:
                    raise IdempotencyConflict("inbox already owns a different memory proposal")
                return dict(existing)
            conn.execute(
                """INSERT INTO memory_proposals
                (proposal_id,inbox_id,principal_id,profile,session_id,generation,
                 target,operations_json,operation_hash,expected_revision,expected_result_revision,rationale,
                 source_kind,source_refs_json,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                (
                    proposal_id, inbox_id, principal["principal_id"],
                    principal["profile"], principal["session_id"],
                    principal["generation"], target, canonical_json(operations),
                    operation_digest, expected_revision, expected_result_revision,
                    rationale, source_kind,
                    canonical_json(source_refs), now, now,
                ),
            )
            return dict(conn.execute(
                "SELECT * FROM memory_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone())

    def get_memory_proposal(self, proposal_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM memory_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_memory_proposals(self, state: str = "pending") -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM memory_proposals WHERE state=? ORDER BY created_at", (state,)
            ).fetchall()]
        finally:
            conn.close()

    def list_memory_provenance(self, proposal_id: Optional[str] = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if proposal_id is None:
                rows = conn.execute(
                    "SELECT * FROM memory_provenance ORDER BY created_at"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_provenance WHERE proposal_id=? ORDER BY created_at",
                    (proposal_id,),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def finish_memory_proposal(
        self,
        proposal_id: str,
        *,
        state: str,
        result: Any,
        approval_actor: str,
        resulting_revision: Optional[str] = None,
    ) -> dict[str, Any]:
        if state not in {"applied", "stale", "failed", "rejected"}:
            raise ValueError("invalid terminal proposal state")
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM memory_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["state"] != "pending":
                return dict(row)
            conn.execute(
                "UPDATE memory_proposals SET state=?,result_json=?,updated_at=? WHERE proposal_id=?",
                (state, canonical_json(result), now, proposal_id),
            )
            if state == "applied":
                conn.execute(
                    """INSERT INTO memory_provenance
                    (proposal_id,target,operation_hash,resulting_revision,
                     approval_actor,source_kind,source_refs_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        proposal_id, row["target"], row["operation_hash"],
                        resulting_revision, approval_actor, row["source_kind"],
                        row["source_refs_json"], now,
                    ),
                )
            return dict(conn.execute(
                "SELECT * FROM memory_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone())

    def create_delegation(
        self, *, inbox_id: int, principal: dict[str, Any], session_key: str,
        delegation_id: str, goal: str, context: Optional[str], toolsets: list[str],
        role: str, importance: str, model_policy: str, policy_reason: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM control_delegations WHERE inbox_id=?", (inbox_id,)
            ).fetchone()
            if existing is not None:
                return dict(existing)
            conn.execute(
                """INSERT INTO control_delegations
                (delegation_id,inbox_id,principal_id,profile,session_key,session_id,
                 generation,goal,context,toolsets_json,role,importance,model_policy,
                 policy_reason,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?)""",
                (delegation_id, inbox_id, principal["principal_id"], principal["profile"],
                 session_key, principal["session_id"], principal["generation"], goal,
                 context, canonical_json(toolsets), role, importance, model_policy,
                 policy_reason, now, now),
            )
            return dict(conn.execute(
                "SELECT * FROM control_delegations WHERE delegation_id=?", (delegation_id,)
            ).fetchone())

    def get_delegation(self, delegation_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM control_delegations WHERE delegation_id=?", (delegation_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_delegations(self, *, session_id: str, generation: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(
                """SELECT * FROM control_delegations WHERE session_id=? AND generation=?
                ORDER BY created_at DESC LIMIT 50""", (session_id, generation)
            ).fetchall()]
        finally:
            conn.close()

    def list_delegations_for_session(self, *, session_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(
                """SELECT * FROM control_delegations WHERE session_id=?
                ORDER BY created_at DESC LIMIT 50""", (session_id,)
            ).fetchall()]
        finally:
            conn.close()

    def update_delegation_state(
        self, delegation_id: str, *, state: str, result: Any = None,
    ) -> dict[str, Any]:
        allowed = {"prepared", "running", "pending_delivery", "completed", "error", "interrupted", "cancel_requested"}
        if state not in allowed:
            raise ValueError("invalid delegation state")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM control_delegations WHERE delegation_id=?", (delegation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(delegation_id)
            conn.execute(
                "UPDATE control_delegations SET state=?,result_json=?,updated_at=? WHERE delegation_id=?",
                (state, canonical_json(result) if result is not None else row["result_json"], time.time(), delegation_id),
            )
            return dict(conn.execute(
                "SELECT * FROM control_delegations WHERE delegation_id=?", (delegation_id,)
            ).fetchone())

    def record_delegation_command(
        self, *, inbox_id: int, delegation_id: str, command: str, payload: Any,
        actor: str, state: str = "accepted",
    ) -> dict[str, Any]:
        if command not in {"steer", "cancel"} or state not in {"accepted", "applied", "rejected"}:
            raise ValueError("invalid delegation command")
        command_id = "cp_cmd_" + secrets.token_hex(8)
        now = time.time()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM control_delegation_commands WHERE inbox_id=?", (inbox_id,)
            ).fetchone()
            if existing is not None:
                return dict(existing)
            conn.execute(
                """INSERT INTO control_delegation_commands
                (command_id,inbox_id,delegation_id,command,payload_json,actor,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (command_id, inbox_id, delegation_id, command, canonical_json(payload), actor, state, now, now),
            )
            return dict(conn.execute(
                "SELECT * FROM control_delegation_commands WHERE command_id=?", (command_id,)
            ).fetchone())

    def update_delegation_command(self, command_id: str, *, state: str) -> dict[str, Any]:
        if state not in {"applied", "rejected"}:
            raise ValueError("invalid command terminal state")
        with self.transaction() as conn:
            conn.execute(
                """UPDATE control_delegation_commands SET state=?,updated_at=?
                WHERE command_id=? AND state='accepted'""",
                (state, time.time(), command_id),
            )
            row = conn.execute(
                "SELECT * FROM control_delegation_commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None:
                raise KeyError(command_id)
            return dict(row)

    def complete_delegation_with_outbox(
        self, *, delegation_id: str, state: str, result: Any, payload: dict[str, Any],
    ) -> Optional[str]:
        if state not in {"completed", "error", "interrupted"}:
            state = "error"
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM control_delegations WHERE delegation_id=?", (delegation_id,)
            ).fetchone()
            if row is None:
                return None
            payload_digest = request_hash(payload)
            event_id = request_hash({
                "event_type": "delegation.completed", "action_id": delegation_id,
                "destination": row["session_id"], "payload_hash": payload_digest,
            })
            conn.execute(
                """UPDATE control_delegations SET state='pending_delivery',result_json=?,updated_at=?
                WHERE delegation_id=? AND state NOT IN ('completed','error','interrupted')""",
                (canonical_json({"terminal_state": state, "result": result}), now, delegation_id),
            )
            conn.execute(
                """INSERT OR IGNORE INTO control_outbox
                (event_id,inbox_id,destination_session_id,payload_hash,payload_json,state,created_at,updated_at)
                VALUES (?,?,?,?,?,'pending',?,?)""",
                (event_id, row["inbox_id"], row["session_id"], payload_digest,
                 canonical_json(payload), now, now),
            )
            return event_id

    def repair_interrupted_batch_states(self) -> int:
        """Repair legacy all-interrupted batches misclassified as errors."""
        repaired = 0
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM control_delegations WHERE state='error' AND result_json IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    result = json.loads(row["result_json"])
                    children = (result.get("result") or {}).get("results") or []
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not children or not all(
                    isinstance(child, dict)
                    and child.get("status") == "interrupted"
                    for child in children
                ):
                    continue
                result["terminal_state"] = "interrupted"
                conn.execute(
                    "UPDATE control_delegations SET state='interrupted',result_json=?,updated_at=? WHERE delegation_id=? AND state='error'",
                    (canonical_json(result), time.time(), row["delegation_id"]),
                )
                self._insert_audit(
                    conn,
                    event_type="delegation_terminal_repaired",
                    principal_id=row["principal_id"],
                    profile=row["profile"],
                    session_id=row["session_id"],
                    generation=int(row["generation"]),
                    detail={
                        "delegation_id": row["delegation_id"],
                        "from": "error",
                        "to": "interrupted",
                    },
                )
                repaired += 1
        return repaired

    def list_pending_outbox(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM control_outbox WHERE state IN ('pending','accepted') ORDER BY created_at"
            ).fetchall()]
        finally:
            conn.close()

    def acknowledge_outbox(self, event_id: str) -> bool:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM control_outbox WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE control_outbox SET state='processed',updated_at=? WHERE event_id=?",
                (time.time(), event_id),
            )
            payload = json.loads(row["payload_json"])
            delegation_id = payload.get("delegation_id")
            terminal_state = payload.get("status")
            if delegation_id and terminal_state in {"completed", "error", "interrupted"}:
                conn.execute(
                    "UPDATE control_delegations SET state=?,updated_at=? WHERE delegation_id=?",
                    (terminal_state, time.time(), delegation_id),
                )
            return True

    def put_handoff(
        self, *, session_key: str, session_id: str, generation: int,
        payload: Any, source_message_max_id: Optional[int],
    ) -> dict[str, Any]:
        revision = request_hash(payload)
        now = time.time()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO thread_handoffs
                (session_key,session_id,generation,revision,payload_json,
                 source_message_max_id,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'verified',?,?)
                ON CONFLICT(session_key,session_id,generation) DO UPDATE SET
                revision=excluded.revision,payload_json=excluded.payload_json,
                source_message_max_id=excluded.source_message_max_id,
                state='verified',updated_at=excluded.updated_at
                WHERE COALESCE(thread_handoffs.source_message_max_id, 0)
                    <= COALESCE(excluded.source_message_max_id, 0)""",
                (session_key, session_id, generation, revision, canonical_json(payload),
                 source_message_max_id, now, now),
            )
            return dict(conn.execute(
                """SELECT * FROM thread_handoffs
                WHERE session_key=? AND session_id=? AND generation=?""",
                (session_key, session_id, generation),
            ).fetchone())

    def get_handoff(
        self, *, session_key: str, session_id: str, generation: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            if generation is None:
                row = conn.execute(
                    """SELECT * FROM thread_handoffs WHERE session_key=? AND session_id=?
                    AND state='verified' ORDER BY generation DESC LIMIT 1""",
                    (session_key, session_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT * FROM thread_handoffs WHERE session_key=? AND session_id=?
                    AND generation=? AND state='verified'""",
                    (session_key, session_id, generation),
                ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def prepare_thread_lineage(
        self, *, session_key: str, session_id: str, from_generation: int,
        previous_thread_id: str, reason: str, policy_revision: str,
        memory_revision: str, handoff_revision: str,
    ) -> dict[str, Any]:
        now = time.time()
        to_generation = from_generation + 1
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO codex_thread_lineage
                (session_key,session_id,from_generation,to_generation,previous_thread_id,
                 reason,policy_revision,memory_revision,handoff_revision,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'prepared',?,?)
                ON CONFLICT(session_key,session_id,to_generation) DO NOTHING""",
                (session_key, session_id, from_generation, to_generation,
                 previous_thread_id, reason, policy_revision, memory_revision,
                 handoff_revision, now, now),
            )
            return dict(conn.execute(
                """SELECT * FROM codex_thread_lineage
                WHERE session_key=? AND session_id=? AND to_generation=?""",
                (session_key, session_id, to_generation),
            ).fetchone())

    def bind_thread_lineage(
        self, *, session_key: str, session_id: str, generation: int, thread_id: str,
    ) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                """UPDATE codex_thread_lineage SET new_thread_id=?,state='bound',updated_at=?
                WHERE session_key=? AND session_id=? AND to_generation=? AND state='prepared'""",
                (thread_id, time.time(), session_key, session_id, generation),
            )
            return cur.rowcount == 1

    def get_thread_lineage(self, *, session_key: str, session_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(
                """SELECT * FROM codex_thread_lineage WHERE session_key=? AND session_id=?
                ORDER BY to_generation""", (session_key, session_id)
            ).fetchall()]
        finally:
            conn.close()
