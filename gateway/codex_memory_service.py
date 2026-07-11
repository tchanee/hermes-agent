"""Governed persistent-memory proposals from scoped Codex sessions."""

from __future__ import annotations

import json
from typing import Any

from gateway.codex_control_store import CodexControlStore


MAX_OPERATIONS = 12
MAX_RATIONALE_CHARS = 1000
MAX_SOURCE_REFS = 12
ALLOWED_SOURCE_KINDS = {"foreground_user"}


class CodexMemoryService:
    def __init__(self, *, store: CodexControlStore, memory_store: Any) -> None:
        self.store = store
        self.memory_store = memory_store

    def methods(self):
        return {"memory.propose": ("memory.propose", self.propose)}

    def propose(self, params: dict[str, Any], principal: dict[str, Any]) -> dict[str, Any]:
        target = str(params.get("target") or "").lower()
        operations = params.get("operations")
        rationale = str(params.get("rationale") or "").strip()
        idempotency_key = str(params.get("idempotency_key") or "").strip()
        source_kind = str(params.get("source_kind") or "foreground_user").strip()
        source_refs = params.get("source_refs") or []
        if target not in {"memory", "user"}:
            raise ValueError("target must be memory or user")
        if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS:
            raise ValueError(f"operations must contain 1-{MAX_OPERATIONS} items")
        if any(not isinstance(op, dict) for op in operations):
            raise ValueError("each operation must be an object")
        if not rationale or len(rationale) > MAX_RATIONALE_CHARS:
            raise ValueError(f"rationale is required and limited to {MAX_RATIONALE_CHARS} characters")
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("idempotency_key is required and limited to 128 characters")
        if source_kind not in ALLOWED_SOURCE_KINDS:
            raise ValueError(
                "shared memory requires foreground_user evidence; retrieved, "
                "worker, tool, and web content must be restated by the user"
            )
        if not isinstance(source_refs, list) or len(source_refs) > MAX_SOURCE_REFS:
            raise ValueError(f"source_refs must be a list of at most {MAX_SOURCE_REFS} items")
        if any(not isinstance(ref, dict) for ref in source_refs):
            raise ValueError("each source reference must be an object")
        if not source_refs or any(not str(ref.get("message_id") or "").strip() for ref in source_refs):
            raise ValueError("foreground_user proposals require source_refs with message_id")

        self.memory_store.load_from_disk()
        expected_revision = self.memory_store.revision(target)
        validation = self.memory_store.apply_batch(
            target, operations, expected_revision=expected_revision, dry_run=True
        )
        if not validation.get("success"):
            raise ValueError(str(validation.get("error") or "invalid memory operations"))

        payload = {
            "target": target,
            "operations": operations,
            "rationale": rationale,
            "source_kind": source_kind,
            "source_refs": source_refs,
        }
        inbox = self.store.accept_request(
            principal_id=principal["principal_id"],
            profile=principal["profile"],
            session_id=principal["session_id"],
            generation=int(principal["generation"]),
            method="memory.propose",
            idempotency_key=idempotency_key,
            payload=payload,
        )
        proposal = self.store.create_memory_proposal(
            inbox_id=int(inbox["id"]), principal=principal, target=target,
            operations=operations, expected_revision=expected_revision,
            expected_result_revision=validation["resulting_revision"],
            rationale=rationale, source_kind=source_kind, source_refs=source_refs,
        )
        return {
            "proposal_id": proposal["proposal_id"],
            "state": proposal["state"],
            "target": target,
            "expected_revision": proposal["expected_revision"],
            "approval_required": True,
        }

    def approve(self, proposal_id: str, *, actor: str) -> dict[str, Any]:
        proposal = self._get(proposal_id)
        if proposal["state"] != "pending":
            return proposal
        operations = json.loads(proposal["operations_json"])
        current_revision = self.memory_store.revision(proposal["target"])
        if current_revision == proposal["expected_result_revision"]:
            return self.store.finish_memory_proposal(
                proposal_id, state="applied",
                result={"success": True, "recovered": True},
                approval_actor=actor, resulting_revision=current_revision,
            )
        result = self.memory_store.apply_batch(
            proposal["target"], operations,
            expected_revision=proposal["expected_revision"],
        )
        if result.get("success"):
            revision = self.memory_store.revision(proposal["target"])
            return self.store.finish_memory_proposal(
                proposal_id, state="applied", result=result,
                approval_actor=actor, resulting_revision=revision,
            )
        state = "stale" if result.get("stale") else "failed"
        return self.store.finish_memory_proposal(
            proposal_id, state=state, result=result, approval_actor=actor,
        )

    def reject(self, proposal_id: str, *, actor: str) -> dict[str, Any]:
        proposal = self._get(proposal_id)
        if proposal["state"] != "pending":
            return proposal
        return self.store.finish_memory_proposal(
            proposal_id, state="rejected", result={"rejected_by": actor},
            approval_actor=actor,
        )

    def pending(self) -> list[dict[str, Any]]:
        return self.store.list_memory_proposals("pending")

    def _get(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.store.get_memory_proposal(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        return proposal
