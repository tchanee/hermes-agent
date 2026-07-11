import os
import pytest

from agent.transports import hermes_tools_mcp_server as server


def test_worker_spawn_role_and_importance_are_closed_enums(monkeypatch):
    class Client:
        def call(self, *_args, **_kwargs):
            return {"delegation_id": "deleg_test", "worker_runtime": "codex"}

    monkeypatch.setattr(server, "_control_client_from_env", lambda: Client())
    mcp = server._build_server()
    tool = mcp._tool_manager._tools["hermes_worker_spawn"]
    properties = tool.parameters["properties"]
    assert properties["role"]["enum"] == ["leaf", "orchestrator"]
    assert properties["importance"]["enum"] == ["routine", "important"]
    assert tool.fn("goal", "key") == {
        "delegation_id": "deleg_test", "worker_runtime": "codex",
    }
    assert tool.fn_metadata.output_schema["type"] == "object"


def test_control_client_absent_without_scoped_environment(monkeypatch):
    monkeypatch.delenv("HERMES_CONTROL_SOCKET", raising=False)
    monkeypatch.delenv("HERMES_CONTROL_TOKEN_FILE", raising=False)
    assert server._control_client_from_env() is None


def test_control_client_requires_both_environment_values(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_CONTROL_SOCKET", str(tmp_path / "s"))
    monkeypatch.delenv("HERMES_CONTROL_TOKEN_FILE", raising=False)
    with pytest.raises(RuntimeError, match="both"):
        server._control_client_from_env()


def test_control_token_file_must_be_private(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("id.secret", encoding="utf-8")
    token.chmod(0o644)
    monkeypatch.setenv("HERMES_CONTROL_SOCKET", str(tmp_path / "s"))
    monkeypatch.setenv("HERMES_CONTROL_TOKEN_FILE", str(token))
    with pytest.raises(PermissionError, match="0600"):
        server._control_client_from_env()


def test_control_client_reads_private_token(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("id.secret\n", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setenv("HERMES_CONTROL_SOCKET", str(tmp_path / "s"))
    monkeypatch.setenv("HERMES_CONTROL_TOKEN_FILE", str(token))
    client = server._control_client_from_env()
    assert client.token == "id.secret"
    assert client.socket_path == tmp_path / "s"
