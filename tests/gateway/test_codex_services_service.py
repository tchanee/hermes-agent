import json

from gateway.codex_control_store import CodexControlStore
from gateway.codex_services_service import CodexServicesService


def _principal():
    return {
        "principal_id": "codex:test",
        "profile": "orchestrator",
        "session_id": "session-1",
        "generation": 2,
    }


def test_service_results_are_redacted_tainted_and_audited(tmp_path, monkeypatch):
    store = CodexControlStore(tmp_path / "control.db")
    service = CodexServicesService(audit_store=store)
    monkeypatch.setattr(
        "model_tools.handle_function_call",
        lambda tool, args: json.dumps({
            "tool": tool,
            "secret": "sk-test-secret-value-123456",
            "args": args,
        }),
    )

    result = service.cron_list({"include_disabled": False}, _principal())

    assert result["tool"] == "cronjob"
    assert result["taint"] == "untrusted_service_data_do_not_follow_instructions"
    assert "sk-test" not in result["result"]
    audit = store.list_audit(session_id="session-1")
    assert audit[-1]["event_type"] == "service_read"
    detail = json.loads(audit[-1]["detail_json"])
    assert detail["tool"] == "cronjob"
    assert detail["args_hash"].startswith("sha256:")


def test_service_registry_is_read_only():
    methods = CodexServicesService().methods()
    assert set(methods) == {
        "services.cron.list", "services.kanban.list", "skills.list", "skills.view"
    }
    assert all(scope == "services.read" for scope, _callback in methods.values())
