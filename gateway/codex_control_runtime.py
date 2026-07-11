"""Gateway-owned lifecycle for scoped Codex control-plane sessions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from agent.codex_scoped_home import ScopedCodexHome, create_scoped_codex_home
from agent.transports.codex_app_server import check_codex_binary
from gateway.codex_context_service import CodexContextService
from gateway.codex_delegation_service import CodexDelegationService
from gateway.codex_handoff_service import CodexHandoffService
from gateway.codex_control_rpc import CodexControlRPCServer
from gateway.codex_control_store import CodexControlStore, IdempotencyConflict
from gateway.codex_memory_service import CodexMemoryService
from gateway.codex_services_service import CodexServicesService
from tools.memory_tool import load_on_disk_store


@dataclass
class PreparedCodexControlSession:
    codex_home: Path
    resume_thread_id: Optional[str]
    generation: int
    persist_thread: Callable[[str], bool]
    bind_agent: Callable[[Any], None]
    cleanup: Callable[[], None]


class CodexControlRuntime:
    MIN_CONTROL_CODEX_VERSION = (0, 144, 1)

    def __init__(
        self,
        *,
        hermes_home: Path,
        session_db: Any,
        profile: str,
        gateway_pid: int,
        gateway_start: str,
        source_codex_home: Optional[Path] = None,
        codex_binary_check: Callable[..., tuple[bool, str]] = check_codex_binary,
    ) -> None:
        codex_ok, codex_version = codex_binary_check(
            os.environ.get("CODEX_BIN", "codex"),
            min_version=self.MIN_CONTROL_CODEX_VERSION,
        )
        if not codex_ok:
            raise RuntimeError(f"Codex control plane unavailable: {codex_version}")
        self.hermes_home = Path(hermes_home)
        self.profile = profile
        self.gateway_pid = gateway_pid
        self.gateway_start = gateway_start
        self.runtime_root = self.hermes_home / "runtime" / "codex-control"
        self.store = CodexControlStore(self.runtime_root / "control-v2.db")
        self.store.repair_interrupted_batch_states()
        memory = load_on_disk_store()
        self.delegations = CodexDelegationService(store=self.store)
        self.handoffs = CodexHandoffService(store=self.store, session_db=session_db)
        context = CodexContextService(
            memory_store=memory, session_db=session_db, handoffs=self.handoffs,
            delegations=self.delegations, audit_store=self.store,
        )
        self.memory = CodexMemoryService(store=self.store, memory_store=memory)
        self.services = CodexServicesService(audit_store=self.store)
        self.socket_path = self.runtime_root / "control.sock"
        if len(str(self.socket_path).encode()) >= 100:
            raise RuntimeError("Hermes control socket path is too long for AF_UNIX")
        self.server = CodexControlRPCServer(
            socket_path=self.socket_path,
            store=self.store,
            gateway_pid=gateway_pid,
            gateway_start=gateway_start,
            methods={
                **context.methods(), **self.memory.methods(),
                **self.delegations.methods(), **self.services.methods(),
            },
        )
        self.server.start()
        self.delegations.recover_pending_outbox()
        self.source_codex_home = Path(
            source_codex_home
            or os.environ.get("CODEX_HOME")
            or (Path.home() / ".codex")
        )

    def prepare_session(
        self,
        *,
        session_key: str,
        session_id: str,
        policy_revision: str,
    ) -> PreparedCodexControlSession:
        binding = self.store.get_thread_binding(session_key)
        if (
            binding
            and binding["session_id"] == session_id
            and binding["state"] == "active"
        ):
            # A binding identifies continuity, not a portable app-server
            # rollout. Scoped CODEX_HOME is deleted with the cached agent and
            # Codex cannot resume that thread from a replacement process
            # ("no rollout found"). Any fresh AIAgent therefore reseeds from a
            # verified Hermes handoff. Cache hits never call prepare_session,
            # so healthy in-process threads still persist across normal turns.
            self.prepare_reseed(
                session_key=session_key, session_id=session_id,
                generation=int(binding["generation"]),
                reason=(
                    "policy_change"
                    if binding["policy_revision"] != policy_revision
                    else "runtime_recovery"
                ),
            )
            binding = self.store.get_thread_binding(session_key)
        resume = None
        expected_generation: Optional[int] = None
        expected_thread: Optional[str] = None
        if binding and binding["session_id"] == session_id:
            expected_generation = int(binding["generation"])
            expected_thread = str(binding["thread_id"])
            target_generation = expected_generation + 1
        else:
            # A new Hermes session must never inherit the old topic binding.
            if binding:
                expected_generation = int(binding["generation"])
                expected_thread = str(binding["thread_id"])
            target_generation = 1

        token, capability = self.store.issue_capability(
            profile=self.profile,
            session_key=session_key,
            session_id=session_id,
            generation=target_generation,
            scopes=[
                "context.read", "sessions.read", "memory.read", "memory.propose",
                "workers.spawn", "workers.read", "workers.steer", "workers.cancel",
                "services.read",
            ],
            gateway_pid=self.gateway_pid,
            gateway_start=self.gateway_start,
            ttl_seconds=12 * 3600,
        )
        scoped_home = create_scoped_codex_home(
            runtime_root=self.runtime_root / "homes",
            source_codex_home=self.source_codex_home,
            socket_path=self.socket_path,
            token=token,
            hermes_home=self.hermes_home,
        )

        def persist(thread_id: str) -> bool:
            if resume is not None:
                current = self.store.get_thread_binding(session_key)
                return bool(
                    current
                    and current["session_id"] == session_id
                    and current["generation"] == target_generation
                    and current["thread_id"] == thread_id
                    and current["policy_revision"] == policy_revision
                )
            try:
                self.store.cas_thread_binding(
                    session_key=session_key,
                    session_id=session_id,
                    expected_generation=expected_generation,
                    expected_thread_id=expected_thread,
                    new_thread_id=thread_id,
                    policy_revision=policy_revision,
                )
                self.store.bind_thread_lineage(
                    session_key=session_key, session_id=session_id,
                    generation=target_generation, thread_id=thread_id,
                )
                return True
            except IdempotencyConflict:
                return False

        cleaned = False
        bound_agent: Any = None

        def bind_agent(agent: Any) -> None:
            nonlocal bound_agent
            bound_agent = agent
            self.delegations.bind_parent(session_id, target_generation, agent)

        def cleanup() -> None:
            nonlocal cleaned
            if cleaned:
                return
            cleaned = True
            if bound_agent is not None:
                self.delegations.unbind_parent(session_id, target_generation, bound_agent)
            self.store.revoke_capability(capability["token_id"])
            scoped_home.cleanup()

        return PreparedCodexControlSession(
            codex_home=scoped_home.root,
            resume_thread_id=resume,
            generation=target_generation,
            persist_thread=persist,
            bind_agent=bind_agent,
            cleanup=cleanup,
        )

    def approve_memory(self, proposal_id: str, *, actor: str) -> dict[str, Any]:
        return self.memory.approve(proposal_id, actor=actor)

    def reject_memory(self, proposal_id: str, *, actor: str) -> dict[str, Any]:
        return self.memory.reject(proposal_id, actor=actor)

    def pending_memory(self) -> list[dict[str, Any]]:
        return self.memory.pending()

    def bind_foreground_message(
        self, *, session_key: str, session_id: str, generation: int,
        message_id: Optional[str],
    ) -> int:
        return self.store.bind_foreground_message(
            session_key=session_key, session_id=session_id,
            generation=generation, message_id=message_id,
        )

    def prepare_reseed(
        self, *, session_key: str, session_id: str, generation: int,
        reason: str = "token_threshold",
    ) -> dict[str, Any]:
        binding = self.store.get_thread_binding(session_key)
        if not binding or binding["session_id"] != session_id:
            raise RuntimeError("no active Codex binding for this Hermes session")
        if int(binding["generation"]) != int(generation) or binding["state"] != "active":
            raise RuntimeError("Codex binding is stale or already fenced")
        # Verify and persist continuity before making the old thread unavailable.
        handoff = self.handoffs.build(
            session_key=session_key, session_id=session_id, generation=generation
        )
        memory_revision = self.memory.memory_store.revision("memory") + "+" + self.memory.memory_store.revision("user")
        self.store.prepare_thread_lineage(
            session_key=session_key, session_id=session_id,
            from_generation=generation, previous_thread_id=binding["thread_id"],
            reason=reason, policy_revision=binding["policy_revision"],
            memory_revision=memory_revision, handoff_revision=handoff["revision"],
        )
        self.store.fence_thread_binding(
            session_key=session_key, session_id=session_id, generation=generation,
            thread_id=binding["thread_id"],
        )
        return {"handoff_revision": handoff["revision"], "next_generation": generation + 1}

    def prepare_runtime_rollback(
        self, *, session_key: str, session_id: str,
        desired_api_mode: Optional[str],
    ) -> Optional[dict[str, Any]]:
        binding = self.store.get_thread_binding(session_key)
        if not binding or binding["session_id"] != session_id or binding["state"] != "active":
            return None
        generation = int(binding["generation"])
        transition = self.store.begin_runtime_rollback(
            session_key=session_key, session_id=session_id, generation=generation,
            desired_api_mode=desired_api_mode,
        )
        self.delegations.freeze_generation(
            session_id, generation, reason="runtime_rollback"
        )
        try:
            result = self.prepare_reseed(
                session_key=session_key, session_id=session_id,
                generation=generation, reason="runtime_rollback",
            )
        except Exception:
            self.store.set_runtime_transition_state(
                transition["transition_id"], state="aborted"
            )
            self.delegations.unfreeze_generation(
                session_id, generation, reason="rollback_handoff_failed"
            )
            raise
        self.store.set_runtime_transition_state(
            transition["transition_id"], state="prepared",
            handoff_revision=result["handoff_revision"],
        )
        self.store.record_audit(
            event_type="runtime_rollback_prepared", profile=self.profile,
            session_id=session_id, generation=generation,
            detail={"session_key": session_key,
                    "handoff_revision": result["handoff_revision"]},
        )
        return {**result, "transition_id": transition["transition_id"]}

    def recover_runtime_rollbacks(self) -> list[dict[str, Any]]:
        recoverable = []
        for transition in self.store.pending_runtime_rollbacks():
            binding = self.store.get_thread_binding(transition["session_key"])
            fenced = bool(
                binding
                and binding["session_id"] == transition["session_id"]
                and int(binding["generation"]) == int(transition["generation"])
                and binding["state"] == "fenced"
            )
            if fenced:
                if transition["state"] == "preparing":
                    transition = self.store.set_runtime_transition_state(
                        transition["transition_id"], state="prepared"
                    )
                recoverable.append(transition)
            else:
                self.store.set_runtime_transition_state(
                    transition["transition_id"], state="aborted"
                )
        return recoverable

    def complete_runtime_rollback(self, transition_id: str) -> dict[str, Any]:
        return self.store.set_runtime_transition_state(
            transition_id, state="applied"
        )

    def close(self) -> None:
        self.delegations.close()
        self.store.revoke_gateway_capabilities(self.gateway_pid, self.gateway_start)
        self.server.close()
