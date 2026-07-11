import os
import tomllib

import pytest

from agent.codex_scoped_home import create_scoped_codex_home


def test_scoped_home_contains_only_control_mcp_and_private_auth(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
    home = create_scoped_codex_home(
        runtime_root=tmp_path / "runtime",
        source_codex_home=source,
        socket_path=tmp_path / "control.sock",
        token="id.secret",
        hermes_home=tmp_path / "hermes",
        python_executable="/venv/python",
    )
    config = tomllib.loads((home.root / "config.toml").read_text())
    assert set(config["mcp_servers"]) == {"hermes-control"}
    assert "plugins" not in config
    assert config["approval_policy"] == "never"
    assert config["sandbox_mode"] == "workspace-write"
    assert config["mcp_servers"]["hermes-control"]["default_tools_approval_mode"] == "approve"
    assert config["mcp_servers"]["hermes-control"]["env"]["HERMES_CONTROL_TOKEN_FILE"] == str(home.token_file)
    assert home.root.stat().st_mode & 0o777 == 0o700
    assert home.token_file.stat().st_mode & 0o777 == 0o600
    assert (home.root / "auth.json").stat().st_mode & 0o777 == 0o600
    home.cleanup()
    assert not home.root.exists()


def test_scoped_home_fails_closed_without_auth(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(RuntimeError, match="auth file not found"):
        create_scoped_codex_home(
            runtime_root=tmp_path / "runtime", source_codex_home=source,
            socket_path=tmp_path / "s", token="x.y",
            hermes_home=tmp_path / "hermes",
        )
