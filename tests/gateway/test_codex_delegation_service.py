import json

import pytest

from gateway.codex_control_store import CodexControlStore, IdempotencyConflict
from gateway.codex_delegation_service import CodexDelegationService


class Parent:
    pass


@pytest.fixture
def service(tmp_path):
    store = CodexControlStore(tmp_path / "control.db")
    workers = CodexDelegationService(store=store)
    yield workers, store
    workers.close()


def principal(**overrides):
    value = {
        "principal_id": "codex:p",
        "profile": "orchestrator",
        "session_key": "telegram:group:topic",
        "session_id": "s1",
        "generation": 2,
    }
    value.update(overrides)
    return value


def spawn_params(**overrides):
    value = {
        "goal": "Inspect and fix the failing tests.",
        "context": "Repository is already checked out.",
        "toolsets": ["terminal"],
        "role": "leaf",
        "importance": "routine",
        "idempotency_key": "turn-7-worker-1",
    }
    value.update(overrides)
    return value


def test_spawn_is_detached_idempotent_and_routine_by_default(service, monkeypatch):
    workers, _store = service
    parent = Parent()
    workers.bind_parent("s1", 2, parent)
    calls = []

    def fake_delegate_task(**kwargs):
        calls.append(kwargs)
        return json.dumps({"status": "dispatched", "delegation_id": kwargs["control_delegation_id"]})

    monkeypatch.setattr("tools.delegate_tool.delegate_task", fake_delegate_task)
    first = workers.spawn(spawn_params(), principal())
    second = workers.spawn(spawn_params(), principal())
    assert first["delegation_id"] == second["delegation_id"]
    assert first["model_policy"] == "Terra/default"
    assert first["worker_runtime"] == "codex"
    assert len(calls) == 1
    assert calls[0]["background"] is True
    assert calls[0]["tier"] == "routine"
    assert calls[0]["worker_runtime"] == "codex"
    assert calls[0]["parent_agent"] is parent

    with pytest.raises(IdempotencyConflict):
        workers.spawn(spawn_params(goal="Different work"), principal())


def test_only_explicit_important_work_uses_important_tier(service, monkeypatch):
    workers, _store = service
    parent = Parent()
    parent._session_messages = [{
        "role": "user", "content": "Perform a rigorous security architecture review."
    }]
    workers.bind_parent("s1", 2, parent)
    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return json.dumps({"status": "dispatched", "delegation_id": kwargs["control_delegation_id"]})

    monkeypatch.setattr("tools.delegate_tool.delegate_task", fake_delegate_task)
    result = workers.spawn(
        spawn_params(goal="Perform a rigorous security architecture review.",
                     importance="important", idempotency_key="important-1"), principal()
    )
    assert captured["tier"] == "important"
    assert result["model_policy"] == "Sol/xhigh"
    assert result["worker_runtime"] == "codex"


def test_hermes_native_toolsets_force_hermes_runtime(service, monkeypatch):
    workers, _store = service
    workers.bind_parent("s1", 2, Parent())
    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return json.dumps({"status": "dispatched", "delegation_id": kwargs["control_delegation_id"]})

    monkeypatch.setattr("tools.delegate_tool.delegate_task", fake_delegate_task)
    result = workers.spawn(
        spawn_params(toolsets=["terminal", "cronjob"], idempotency_key="native-1"),
        principal(),
    )
    assert result["worker_runtime"] == "hermes"
    assert captured["worker_runtime"] == "hermes"


def test_workers_are_generation_scoped_and_steer_cancel_are_targeted(service, monkeypatch):
    workers, store = service
    workers.bind_parent("s1", 2, Parent())
    monkeypatch.setattr(
        "tools.delegate_tool.delegate_task",
        lambda **kwargs: json.dumps({"status": "dispatched", "delegation_id": kwargs["control_delegation_id"]}),
    )
    spawned = workers.spawn(spawn_params(), principal())
    delegation_id = spawned["delegation_id"]
    steered = []
    cancelled = []
    monkeypatch.setattr("tools.async_delegation.steer_delegation", lambda did, msg: steered.append((did, msg)) or True)
    monkeypatch.setattr("tools.async_delegation.interrupt_delegation", lambda did, reason: cancelled.append((did, reason)) or True)

    assert workers.steer({"delegation_id": delegation_id, "message": "Prioritize the regression.",
                          "idempotency_key": "steer-1"}, principal())["accepted"]
    assert workers.cancel({"delegation_id": delegation_id,
                           "idempotency_key": "cancel-1"}, principal())["accepted"]
    assert steered == [(delegation_id, "Prioritize the regression.")]
    assert cancelled[0][0] == delegation_id
    assert store.get_delegation(delegation_id)["state"] == "cancel_requested"

    with pytest.raises(PermissionError):
        workers.steer(
            {"delegation_id": delegation_id, "message": "wrong generation",
             "idempotency_key": "wrong-gen"},
            principal(generation=3),
        )


def test_spawn_fails_closed_without_bound_parent(service):
    workers, _store = service
    with pytest.raises(RuntimeError, match="parent agent is unavailable"):
        workers.spawn(spawn_params(), principal())


def test_frozen_generation_rejects_new_worker_dispatch(service):
    workers, store = service
    workers.bind_parent("s1", 2, Parent())
    workers.freeze_generation("s1", 2, reason="runtime_rollback")
    with pytest.raises(RuntimeError, match="frozen"):
        workers.spawn(spawn_params(), principal())
    assert store.list_audit(session_id="s1")[-1]["event_type"] == "worker_generation_frozen"
