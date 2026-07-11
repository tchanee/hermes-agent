import threading
from types import SimpleNamespace

import pytest
import yaml

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


def _runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_model_overrides = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._evict_cached_agent = lambda key: None
    runner._normalize_source_for_session_key = lambda source: source
    return runner


def _event(args: str):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1003931971445",
        chat_type="group",
        user_id="854344782",
        thread_id="7351",
    )
    return SimpleNamespace(source=source, get_command_args=lambda: args)


@pytest.mark.asyncio
async def test_topic_runtime_hermes_persists_only_exact_topic(tmp_path, monkeypatch):
    session_key = "agent:main:telegram:group:-1003931971445:7351"
    other_key = "agent:main:telegram:group:-1003931971445:2"
    config = {
        "model": {"default": "gpt-5.6-terra", "provider": "openai-codex"},
        "gateway": {
            "session_model_overrides": {
                session_key: {"model": "gpt-5.6-terra"},
                other_key: {"model": "gpt-5.6-sol", "api_mode": "codex_app_server"},
            }
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    runner = _runner()

    reply = await runner._handle_topic_runtime_command(_event("hermes"))

    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["gateway"]["session_model_overrides"][session_key]["api_mode"] == "codex_responses"
    assert saved["gateway"]["session_model_overrides"][other_key]["api_mode"] == "codex_app_server"
    assert runner._session_model_overrides[session_key]["api_mode"] == "codex_responses"
    assert "Other topics and cron are unchanged" in reply


@pytest.mark.asyncio
async def test_topic_runtime_restore_removes_only_api_mode(tmp_path, monkeypatch):
    session_key = "agent:main:telegram:group:-1003931971445:7351"
    config = {
        "gateway": {"session_model_overrides": {
            session_key: {
                "model": "gpt-5.6-terra",
                "provider": "openai-codex",
                "api_mode": "codex_app_server",
            }
        }}
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    runner = _runner()

    await runner._handle_topic_runtime_command(_event("restore"))

    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    override = saved["gateway"]["session_model_overrides"][session_key]
    assert override == {"model": "gpt-5.6-terra", "provider": "openai-codex"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config, expected",
    [
        ({"gateway": []}, "gateway config must be a mapping"),
        ({"gateway": {"session_model_overrides": []}}, "session_model_overrides must be a mapping"),
    ],
)
async def test_topic_runtime_rejects_malformed_config(config, expected, monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    reply = await _runner()._handle_topic_runtime_command(_event("hermes"))
    assert expected in reply


@pytest.mark.asyncio
async def test_topic_runtime_status_checks_live_codex_process(monkeypatch):
    session_key = "agent:main:telegram:group:-1003931971445:7351"
    config = {"gateway": {"session_model_overrides": {
        session_key: {"api_mode": "codex_app_server"}
    }}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    runner = _runner()
    runner._running_agents = {}
    dead_session = SimpleNamespace(_thread_id="thread-dead", is_alive=lambda: False)
    agent = SimpleNamespace(_codex_session=dead_session)
    runner._agent_cache[session_key] = (agent, "sig", 0)
    reply = await runner._handle_topic_runtime_command(_event("status"))
    assert "Codex thread: stopped" in reply
    assert "active" not in reply


@pytest.mark.asyncio
async def test_topic_runtime_status_prefers_control_plane_binding(monkeypatch):
    session_key = "agent:main:telegram:group:-1003931971445:7351"
    config = {"gateway": {"session_model_overrides": {
        session_key: {"api_mode": "codex_app_server"}
    }}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    runner = _runner()
    runner._running_agents = {}
    runner.session_store = SimpleNamespace(
        get_codex_thread_id=lambda _key: "legacy-wrong-thread"
    )
    runner._codex_control_runtime = SimpleNamespace(store=SimpleNamespace(
        get_thread_binding=lambda _key: {
            "thread_id": "control-thread-123", "state": "active"
        }
    ))

    reply = await runner._handle_topic_runtime_command(_event("status"))

    assert "Codex thread: resumable (control-)" in reply
    assert "legacy-wrong-thread" not in reply
