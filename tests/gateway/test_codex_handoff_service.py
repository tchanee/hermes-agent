import json

import pytest

from gateway.codex_control_store import CodexControlStore
from gateway.codex_handoff_service import CodexHandoffService, MAX_HANDOFF_BYTES


class FakeDB:
    def get_session(self, session_id):
        return {
            "s1": {"id": "s1", "parent_session_id": "s0"},
            "s0": {"id": "s0", "parent_session_id": None},
        }.get(session_id)

    def get_messages(self, session_id, include_inactive=False):
        assert include_inactive is True
        if session_id == "s0":
            return [
                {"id": 10, "role": "user", "content": "Continue lane research."},
                {"id": 11, "role": "assistant", "content": "Two lanes plus control."},
            ]
        assert session_id == "s1"
        return [
            {"id": 1, "role": "user", "content": "Keep the parent chat responsive."},
            {"id": 2, "role": "assistant", "content": "Worker dispatched."},
            {"id": 3, "role": "user", "content": "Token sk-abcdefghijklmnop must not survive."},
        ]


@pytest.fixture
def service(tmp_path):
    store = CodexControlStore(tmp_path / "control.db")
    return CodexHandoffService(store=store, session_db=FakeDB()), store


def test_handoff_is_bounded_redacted_tainted_and_revisioned(service):
    handoffs, store = service
    row = handoffs.build(session_key="topic", session_id="s1", generation=1)
    payload = json.loads(row["payload_json"])
    assert payload["taint"].startswith("untrusted_data")
    assert payload["instructions"] is None
    assert "abcdefghijklmnop" not in row["payload_json"]
    assert payload["recent_user_requests"][-1]["provenance"] == "hermes_transcript"
    assert payload["prior_topic_context"][0]["session_id"] == "s0"
    assert payload["prior_topic_context"][0]["recent_assistant_updates"][0][
        "provenance"
    ] == "hermes_topic_predecessor_transcript"
    assert len(row["payload_json"].encode()) <= MAX_HANDOFF_BYTES
    loaded = handoffs.get(session_key="topic", session_id="s1")
    assert loaded["revision"] == row["revision"]
    assert store.get_handoff(session_key="topic", session_id="s1")["revision"] == row["revision"]


def test_handoff_includes_durable_active_worker_handles(service):
    handoffs, store = service
    inbox = store.accept_request(
        principal_id="p", profile="o", session_id="s1", generation=1,
        method="workers.spawn", idempotency_key="k", payload={"goal": "g"},
    )
    store.create_delegation(
        inbox_id=inbox["id"],
        principal={"principal_id": "p", "profile": "o", "session_id": "s1", "generation": 1},
        session_key="topic", delegation_id="deleg_1", goal="Finish the audit",
        context=None, toolsets=[], role="leaf", importance="important", model_policy="Sol/xhigh",
        policy_reason="audit is consequential",
    )
    row = handoffs.build(session_key="topic", session_id="s1", generation=1)
    active = json.loads(row["payload_json"])["active_workers"]
    assert active == [{
        "delegation_id": "deleg_1", "goal": "Finish the audit",
        "importance": "important", "state": "prepared", "worker_runtime": "hermes",
    }]
