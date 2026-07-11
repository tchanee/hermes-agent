import os
import socket
import struct
import shutil
import tempfile
from pathlib import Path

import pytest

from gateway.codex_control_rpc import (
    CodexControlRPCClient,
    CodexControlRPCServer,
    RPCError,
)
from gateway.codex_control_store import CodexControlStore


@pytest.fixture
def rpc(tmp_path):
    store = CodexControlStore(tmp_path / "control.db")
    store.cas_thread_binding(
        session_key="topic", session_id="s", expected_generation=None,
        expected_thread_id=None, new_thread_id="thread", policy_revision="r1",
    )
    runtime_dir = Path(tempfile.mkdtemp(prefix="hcrpc-", dir="/tmp"))
    server = CodexControlRPCServer(
        socket_path=runtime_dir / "control.sock",
        store=store,
        gateway_pid=123,
        gateway_start="start",
        methods={
            "context.status": (
                "context.read",
                lambda params, principal: {
                    "profile": principal["profile"], "echo": params.get("echo")
                },
            )
        },
    )
    server.start()
    yield store, server
    server.close()
    shutil.rmtree(runtime_dir, ignore_errors=True)


def issue(store, scopes=("context.read",)):
    return store.issue_capability(
        profile="orchestrator", session_key="topic", session_id="s",
        generation=1, scopes=list(scopes), gateway_pid=123,
        gateway_start="start", ttl_seconds=60,
    )


def test_authenticated_allowlisted_call_and_socket_modes(rpc):
    store, server = rpc
    token, _ = issue(store)
    client = CodexControlRPCClient(socket_path=server.socket_path, token=token)
    assert client.call("context.status", {"echo": "ok"}) == {
        "profile": "orchestrator", "echo": "ok"
    }
    assert server.socket_path.stat().st_mode & 0o777 == 0o600
    assert server.socket_path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("bad", ["", "garbage", "a.b"])
def test_invalid_tokens_fail_closed(rpc, bad):
    _, server = rpc
    client = CodexControlRPCClient(socket_path=server.socket_path, token=bad)
    with pytest.raises(RPCError, match="capability"):
        client.call("context.status")


def test_scope_and_method_are_enforced(rpc):
    store, server = rpc
    token, _ = issue(store, scopes=("memory.write",))
    client = CodexControlRPCClient(socket_path=server.socket_path, token=token)
    with pytest.raises(RPCError, match="scope denied"):
        client.call("context.status")
    good, _ = issue(store)
    client = CodexControlRPCClient(socket_path=server.socket_path, token=good)
    with pytest.raises(RPCError, match="not allowlisted"):
        client.call("tools.dispatch", {"name": "memory"})


def test_oversized_frame_is_rejected_without_handler_execution(rpc):
    _, server = rpc
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(server.socket_path))
        sock.sendall(struct.pack(">I", server.max_frame + 1))
        size = struct.unpack(">I", sock.recv(4))[0]
        response = sock.recv(size).decode()
    assert "invalid frame size" in response
