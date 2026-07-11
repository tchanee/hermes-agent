"""Generate an exclusive Codex home for one scoped Hermes session."""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ScopedCodexHome:
    root: Path
    token_file: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def create_scoped_codex_home(
    *,
    runtime_root: Path,
    source_codex_home: Path,
    socket_path: Path,
    token: str,
    hermes_home: Path,
    python_executable: str | None = None,
) -> ScopedCodexHome:
    runtime_root = Path(runtime_root)
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime_root, 0o700)
    root = runtime_root / ("codex-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    token_file = root / "control.token"
    token_file.write_text(token, encoding="utf-8")
    os.chmod(token_file, 0o600)

    source_auth = Path(source_codex_home) / "auth.json"
    if not source_auth.is_file():
        shutil.rmtree(root, ignore_errors=True)
        raise RuntimeError(f"Codex auth file not found: {source_auth}")
    shutil.copyfile(source_auth, root / "auth.json")
    os.chmod(root / "auth.json", 0o600)

    executable = python_executable or sys.executable
    repo_root = Path(__file__).resolve().parent.parent
    launcher = root / "hermes_control_mcp.py"
    launcher.write_text(
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "runpy.run_module('agent.transports.hermes_tools_mcp_server', run_name='__main__')\n",
        encoding="utf-8",
    )
    os.chmod(launcher, 0o500)
    config = f'''default_permissions = ":workspace"
approval_policy = "never"
sandbox_mode = "workspace-write"

[mcp_servers.hermes-control]
command = {_toml_string(executable)}
args = ["-I", {_toml_string(str(launcher))}]
default_tools_approval_mode = "approve"
env = {{ HERMES_HOME = {_toml_string(str(hermes_home))}, HERMES_QUIET = "1", HERMES_REDACT_SECRETS = "true", HERMES_GATEWAY_SESSION = "1", HERMES_CONTROL_SOCKET = {_toml_string(str(socket_path))}, HERMES_CONTROL_TOKEN_FILE = {_toml_string(str(token_file))} }}
startup_timeout_sec = 30.0
tool_timeout_sec = 600.0
'''
    (root / "config.toml").write_text(config, encoding="utf-8")
    os.chmod(root / "config.toml", 0o600)
    return ScopedCodexHome(root=root, token_file=token_file)
