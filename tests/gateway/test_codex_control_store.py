import threading
import json

import pytest

from gateway.codex_control_store import CodexControlStore, IdempotencyConflict


def test_repair_only_reclassifies_proven_all_interrupted_batches(tmp_path):
    store = CodexControlStore(tmp_path / "control.db")
    inbox = store.accept_request(
        principal_id="p", profile="o", session_id="s", generation=1,
        method="workers.spawn", idempotency_key="k", payload={"goal": "g"},
    )
    row = store.create_delegation(
        inbox_id=inbox["id"],
        principal={"principal_id": "p", "profile": "o", "session_id": "s", "generation": 1},
        session_key="topic", delegation_id="deleg_interrupted", goal="g",
        context=None, toolsets=[], role="leaf", importance="routine",
        model_policy="Terra/default", policy_reason="routine",
    )
    store.update_delegation_state(
        row["delegation_id"], state="error",
        result={
            "terminal_state": "error",
            "result": {"results": [{"status": "interrupted"}]},
        },
    )

    assert store.repair_interrupted_batch_states() == 1
    repaired = store.get_delegation(row["delegation_id"])
    assert repaired["state"] == "interrupted"
    assert json.loads(repaired["result_json"])["terminal_state"] == "interrupted"
    assert store.repair_interrupted_batch_states() == 0
    assert store.list_audit(session_id="s")[-1]["event_type"] == "delegation_terminal_repaired"


def test_request_idempotency_and_hash_conflict(tmp_path):
    store = CodexControlStore(tmp_path / "control.db")
    args = dict(principal_id="p", profile="orchestrator", session_id="s",
                generation=1, method="delegate.spawn", idempotency_key="k")
    first = store.accept_request(**args, payload={"goal": "a"})
    assert store.accept_request(**args, payload={"goal": "a"})["id"] == first["id"]
    with pytest.raises(IdempotencyConflict):
        store.accept_request(**args, payload={"goal": "b"})
    audit = store.list_audit(session_id="s")
    assert [row["event_type"] for row in audit] == ["request_accepted"]


def test_concurrent_duplicates_create_one_inbox_row(tmp_path):
    store = CodexControlStore(tmp_path / "control.db")
    ids = []
    errors = []
    def run():
        try:
            row = store.accept_request(
                principal_id="p", profile="o", session_id="s", generation=1,
                method="m", idempotency_key="same", payload={"x": 1}
            )
            ids.append(row["id"])
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert not errors
    assert len(set(ids)) == 1


def test_terminal_result_and_outbox_commit_together(tmp_path):
    store = CodexControlStore(tmp_path / "control.db")
    inbox = store.accept_request(
        principal_id="p", profile="o", session_id="s", generation=1,
        method="spawn", idempotency_key="k", payload={"goal": "g"}
    )
    done, event = store.complete_with_outbox(
        inbox_id=inbox["id"], result={"delegation_id": "d1"},
        event_type="delegation.completed", stable_action_id="d1",
        destination_session_id="s", payload={"summary": "ok"}
    )
    assert done["state"] == "succeeded"
    assert event["state"] == "pending"
    again_done, again_event = store.complete_with_outbox(
        inbox_id=inbox["id"], result={"delegation_id": "d1"},
        event_type="delegation.completed", stable_action_id="d1",
        destination_session_id="s", payload={"summary": "ok"}
    )
    assert again_done["id"] == done["id"]
    assert again_event["event_id"] == event["event_id"]


def test_thread_binding_rejects_stale_same_session_writer(tmp_path):
    store = CodexControlStore(tmp_path / "control.db")
    first = store.cas_thread_binding(
        session_key="topic", session_id="s", expected_generation=None,
        expected_thread_id=None, new_thread_id="t1", policy_revision="r1"
    )
    second = store.cas_thread_binding(
        session_key="topic", session_id="s", expected_generation=first["generation"],
        expected_thread_id="t1", new_thread_id="t2", policy_revision="r2"
    )
    assert second["generation"] == 2
    assert second["previous_thread_id"] == "t1"
    with pytest.raises(IdempotencyConflict):
        store.cas_thread_binding(
            session_key="topic", session_id="s", expected_generation=1,
            expected_thread_id="t1", new_thread_id="stale", policy_revision="r1"
        )


def test_capability_is_scoped_bound_and_revocable(tmp_path):
    store = CodexControlStore(tmp_path / "control.db")
    token, row = store.issue_capability(
        profile="orchestrator", session_key="topic", session_id="s",
        generation=3, scopes=["context.read"], gateway_pid=42,
        gateway_start="start", ttl_seconds=60,
    )
    valid = store.validate_capability(
        token, audience="hermes-control", required_scope="context.read",
        gateway_pid=42, gateway_start="start", expected_session_id="s",
        expected_generation=3,
    )
    assert valid["principal_id"] == row["principal_id"]
    assert valid["foreground_message_id"] is None
    assert store.bind_foreground_message(
        session_key="topic", session_id="s", generation=3, message_id="99"
    ) == 1
    rebound = store.validate_capability(
        token, audience="hermes-control", required_scope="context.read",
        gateway_pid=42, gateway_start="start",
    )
    assert rebound["foreground_message_id"] == "99"
    store.bind_foreground_message(
        session_key="topic", session_id="s", generation=3, message_id=None
    )
    assert store.validate_capability(
        token, audience="hermes-control", required_scope="context.read",
        gateway_pid=42, gateway_start="start",
    )["foreground_message_id"] is None
    for overrides in (
        {"required_scope": "memory.write"},
        {"gateway_pid": 43},
        {"expected_session_id": "other"},
        {"expected_generation": 4},
        {"audience": "other"},
    ):
        kwargs = dict(
            audience="hermes-control", required_scope="context.read",
            gateway_pid=42, gateway_start="start", expected_session_id="s",
            expected_generation=3,
        )
        kwargs.update(overrides)
        with pytest.raises(PermissionError):
            store.validate_capability(token, **kwargs)
    assert store.revoke_capability(row["token_id"])
    assert [event["event_type"] for event in store.list_audit()] == [
        "capability_issued", "capability_revoked"
    ]
    with pytest.raises(PermissionError, match="revoked"):
        store.validate_capability(
            token, audience="hermes-control", required_scope="context.read",
            gateway_pid=42, gateway_start="start",
        )


def test_capability_expiry_is_persisted(tmp_path):
    store = CodexControlStore(tmp_path / "control.db")
    token, row = store.issue_capability(
        profile="o", session_key="k", session_id="s", generation=1,
        scopes=["context.read"], gateway_pid=1, gateway_start="x",
        ttl_seconds=1,
    )
    with pytest.raises(PermissionError, match="expired"):
        store.validate_capability(
            token, audience="hermes-control", required_scope="context.read",
            gateway_pid=1, gateway_start="x", now=row["expires_at"] + 1,
        )
    with pytest.raises(PermissionError, match="expired"):
        store.validate_capability(
            token, audience="hermes-control", required_scope="context.read",
            gateway_pid=1, gateway_start="x",
        )
