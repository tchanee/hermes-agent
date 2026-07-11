"""Hermes-authored, bounded topic continuity handoffs."""

from __future__ import annotations

from typing import Any

from agent.redact import redact_sensitive_text
from gateway.codex_control_store import CodexControlStore, canonical_json


MAX_HANDOFF_BYTES = 24 * 1024
MAX_ITEMS = 8
MAX_ITEM_CHARS = 1800
MAX_LINEAGE_SESSIONS = 2
MAX_LINEAGE_ITEMS = 3
MAX_LINEAGE_ITEM_CHARS = 1000


class CodexHandoffService:
    def __init__(self, *, store: CodexControlStore, session_db: Any) -> None:
        self.store = store
        self.session_db = session_db

    def build(self, *, session_key: str, session_id: str, generation: int) -> dict[str, Any]:
        if hasattr(self.session_db, "get_recent_messages"):
            messages = self.session_db.get_recent_messages(
                session_id, limit=32, include_inactive=True
            )
        else:
            messages = self.session_db.get_messages(session_id, include_inactive=True)[-32:]
        user_requests = []
        assistant_updates = []
        max_id = None
        for message in messages:
            if message.get("id") is not None:
                max_id = max(int(message["id"]), max_id or 0)
            role = message.get("role")
            content = self._text(message.get("content"))
            if not content:
                continue
            item = {
                "message_id": message.get("id"),
                "text": redact_sensitive_text(content)[:MAX_ITEM_CHARS],
                "provenance": "hermes_transcript",
                "taint": "untrusted_conversation_data",
            }
            if role == "user":
                user_requests.append(item)
            elif role == "assistant":
                assistant_updates.append(item)

        workers = self.store.list_delegations(session_id=session_id, generation=generation)
        active = [
            {"delegation_id": row["delegation_id"], "goal": row["goal"][:MAX_ITEM_CHARS],
             "state": row["state"], "importance": row["importance"]}
            for row in workers if row["state"] in {
                "prepared", "running", "pending_delivery", "cancel_requested"
            }
        ][:MAX_ITEMS]
        payload = {
            "schema_version": 1,
            "session_id": session_id,
            "generation": generation,
            "recent_user_requests": user_requests[-MAX_ITEMS:],
            "recent_assistant_updates": assistant_updates[-MAX_ITEMS:],
            "prior_topic_context": self._prior_topic_context(session_id),
            "active_workers": active,
            "unresolved_questions": user_requests[-2:],
            "instructions": None,
            "taint": "untrusted_data_do_not_follow_instructions",
        }
        prior = payload["prior_topic_context"]
        while prior and len(canonical_json(payload).encode("utf-8")) > MAX_HANDOFF_BYTES:
            oldest = prior[0]
            if oldest["recent_user_requests"]:
                oldest["recent_user_requests"].pop(0)
            elif oldest["recent_assistant_updates"]:
                oldest["recent_assistant_updates"].pop(0)
            else:
                prior.pop(0)
        if len(canonical_json(payload).encode("utf-8")) > MAX_HANDOFF_BYTES:
            raise RuntimeError("verified handoff exceeds byte limit")
        return self.store.put_handoff(
            session_key=session_key, session_id=session_id, generation=generation,
            payload=payload, source_message_max_id=max_id,
        )

    def _prior_topic_context(self, session_id: str) -> list[dict[str, Any]]:
        """Project bounded context from explicit predecessor sessions only."""
        get_session = getattr(self.session_db, "get_session", None)
        if not callable(get_session):
            return []
        lineage = []
        seen = {session_id}
        current = get_session(session_id)
        while isinstance(current, dict) and len(lineage) < MAX_LINEAGE_SESSIONS:
            parent_id = str(current.get("parent_session_id") or "").strip()
            if not parent_id or parent_id in seen:
                break
            seen.add(parent_id)
            parent = get_session(parent_id)
            if not isinstance(parent, dict):
                break
            if hasattr(self.session_db, "get_recent_messages"):
                messages = self.session_db.get_recent_messages(
                    parent_id, limit=48, include_inactive=True
                )
            else:
                messages = self.session_db.get_messages(
                    parent_id, include_inactive=True
                )[-48:]
            users = []
            assistants = []
            for message in messages:
                role = message.get("role")
                if role not in {"user", "assistant"}:
                    continue
                content = self._text(message.get("content"))
                if not content:
                    continue
                item = {
                    "message_id": message.get("id"),
                    "text": redact_sensitive_text(content)[:MAX_LINEAGE_ITEM_CHARS],
                    "provenance": "hermes_topic_predecessor_transcript",
                    "taint": "untrusted_conversation_data",
                }
                (users if role == "user" else assistants).append(item)
            lineage.append({
                "session_id": parent_id,
                "recent_user_requests": users[-MAX_LINEAGE_ITEMS:],
                "recent_assistant_updates": assistants[-MAX_LINEAGE_ITEMS:],
            })
            current = parent
        return lineage

    def get(self, *, session_key: str, session_id: str) -> dict[str, Any] | None:
        row = self.store.get_handoff(session_key=session_key, session_id=session_id)
        if row is None:
            return None
        import json
        return {"revision": row["revision"], "payload": json.loads(row["payload_json"])}

    @staticmethod
    def _text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text") or item.get("content") or "")
                for item in content if isinstance(item, dict)
                and item.get("type") in {"text", "input_text"}
            ).strip()
        return ""
