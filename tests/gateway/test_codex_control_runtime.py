from pathlib import Path

import pytest

from gateway.codex_control_runtime import CodexControlRuntime


class FakeDB:
    def search_messages(self, **kwargs):
        return []

    def get_messages(self, session_id, include_inactive=False):
        return [
            {"id": 1, "role": "user", "content": "Preserve this request."},
            {"id": 2, "role": "assistant", "content": "Work is in progress."},
        ]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    source = tmp_path / "source-codex"
    source.mkdir()
    (source / "auth.json").write_text("{}", encoding="utf-8")
    # Keep AF_UNIX path below macOS's limit.
    short = Path("/tmp") / ("hcrt-" + tmp_path.name[-8:])
    rt = CodexControlRuntime(
        hermes_home=short, session_db=FakeDB(), profile="orchestrator",
        gateway_pid=99, gateway_start="start", source_codex_home=source,
        codex_binary_check=lambda *_args, **_kwargs: (True, "0.144.1"),
    )
    yield rt
    rt.close()
    import shutil
    shutil.rmtree(short, ignore_errors=True)


def test_old_codex_version_fails_before_runtime_start(tmp_path):
    with pytest.raises(RuntimeError, match="older than required"):
        CodexControlRuntime(
            hermes_home=tmp_path, session_db=FakeDB(), profile="orchestrator",
            gateway_pid=99, gateway_start="start",
            codex_binary_check=lambda *_args, **_kwargs: (
                False, "codex 0.143.0 is older than required 0.144.1"
            ),
        )


def test_first_session_starts_fresh_and_binds_once(runtime):
    prepared = runtime.prepare_session(
        session_key="topic", session_id="s1", policy_revision="r1"
    )
    assert prepared.resume_thread_id is None
    assert prepared.generation == 1
    assert prepared.persist_thread("thread-1")
    prepared.bind_agent(object())
    assert not prepared.persist_thread("thread-stale")
    prepared.cleanup()
    assert not prepared.codex_home.exists()


def test_same_policy_resumes_but_policy_change_reseeds(runtime):
    first = runtime.prepare_session(
        session_key="topic", session_id="s1", policy_revision="r1"
    )
    assert first.persist_thread("thread-1")
    first.cleanup()
    resumed = runtime.prepare_session(
        session_key="topic", session_id="s1", policy_revision="r1"
    )
    assert resumed.resume_thread_id == "thread-1"
    assert resumed.persist_thread("thread-1")
    resumed.cleanup()
    reseed = runtime.prepare_session(
        session_key="topic", session_id="s1", policy_revision="r2"
    )
    assert reseed.resume_thread_id is None
    assert reseed.generation == 2
    assert reseed.persist_thread("thread-2")
    reseed.cleanup()


def test_new_hermes_session_does_not_inherit_topic_thread(runtime):
    first = runtime.prepare_session(
        session_key="topic", session_id="s1", policy_revision="r1"
    )
    assert first.persist_thread("thread-1")
    first.cleanup()
    fresh = runtime.prepare_session(
        session_key="topic", session_id="s2", policy_revision="r1"
    )
    assert fresh.resume_thread_id is None
    assert fresh.generation == 1
    assert fresh.persist_thread("thread-new-session")
    fresh.cleanup()


def test_verified_handoff_is_persisted_before_reseed_fences_old_thread(runtime):
    first = runtime.prepare_session(
        session_key="topic", session_id="s1", policy_revision="r1"
    )
    assert first.persist_thread("thread-1")
    prepared = runtime.prepare_reseed(session_key="topic", session_id="s1", generation=1)
    assert prepared["next_generation"] == 2
    binding = runtime.store.get_thread_binding("topic")
    assert binding["state"] == "fenced"
    assert runtime.store.get_handoff(session_key="topic", session_id="s1", generation=1)

    next_session = runtime.prepare_session(
        session_key="topic", session_id="s1", policy_revision="r1"
    )
    assert next_session.resume_thread_id is None
    assert next_session.generation == 2
    assert next_session.persist_thread("thread-2")


def test_reseed_failure_keeps_old_binding_active(runtime, monkeypatch):
    first = runtime.prepare_session(
        session_key="topic", session_id="s1", policy_revision="r1"
    )
    assert first.persist_thread("thread-1")
    monkeypatch.setattr(runtime.handoffs, "build", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("summary failed")))
    with pytest.raises(RuntimeError, match="summary failed"):
        runtime.prepare_reseed(session_key="topic", session_id="s1", generation=1)
    assert runtime.store.get_thread_binding("topic")["state"] == "active"
