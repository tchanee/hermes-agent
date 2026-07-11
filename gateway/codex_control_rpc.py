"""Authenticated local RPC for Codex-to-Hermes control-plane calls."""

from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from gateway.codex_control_store import CodexControlStore, canonical_json


PROTOCOL_VERSION = 1
DEFAULT_MAX_FRAME = 256 * 1024


class RPCError(RuntimeError):
    pass


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("socket closed during frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_frame(sock: socket.socket, max_frame: int) -> dict[str, Any]:
    size = struct.unpack(">I", _recv_exact(sock, 4))[0]
    if size <= 0 or size > max_frame:
        raise RPCError(f"invalid frame size {size}")
    value = json.loads(_recv_exact(sock, size).decode("utf-8"))
    if not isinstance(value, dict):
        raise RPCError("frame must contain a JSON object")
    return value


def _send_frame(sock: socket.socket, value: dict[str, Any], max_frame: int) -> None:
    payload = canonical_json(value).encode("utf-8")
    if len(payload) > max_frame:
        raise RPCError("response exceeds frame limit")
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def _peer_uid(sock: socket.socket) -> Optional[int]:
    getpeereid = getattr(sock, "getpeereid", None)
    if callable(getpeereid):
        return int(getpeereid()[0])
    if hasattr(socket, "SO_PEERCRED"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", raw)
        return int(uid)
    if hasattr(socket, "LOCAL_PEERCRED"):
        # macOS/BSD struct xucred: version, uid, ngroups, padding, groups[16].
        raw = sock.getsockopt(0, socket.LOCAL_PEERCRED, 76)
        _version, uid, _ngroups, *_groups = struct.unpack("IIh2x16I", raw)
        return int(uid)
    return None


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class CodexControlRPCServer:
    def __init__(
        self,
        *,
        socket_path: Path,
        store: CodexControlStore,
        gateway_pid: int,
        gateway_start: str,
        methods: dict[str, tuple[str, Callable[[dict[str, Any], dict[str, Any]], Any]]],
        expected_uid: Optional[int] = None,
        max_frame: int = DEFAULT_MAX_FRAME,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.store = store
        self.gateway_pid = gateway_pid
        self.gateway_start = gateway_start
        self.methods = dict(methods)
        self.expected_uid = os.getuid() if expected_uid is None else expected_uid
        self.max_frame = max_frame
        self._server: Optional[_ThreadingUnixServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._server is not None:
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.socket_path.parent, 0o700)
        self.socket_path.unlink(missing_ok=True)
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                response: dict[str, Any]
                request_id: Any = None
                try:
                    uid = _peer_uid(self.request)
                    if uid is None or uid != owner.expected_uid:
                        raise PermissionError("Unix peer UID mismatch or unavailable")
                    request = _recv_frame(self.request, owner.max_frame)
                    request_id = request.get("request_id")
                    if request.get("version") != PROTOCOL_VERSION:
                        raise RPCError("unsupported protocol version")
                    method = str(request.get("method") or "")
                    method_entry = owner.methods.get(method)
                    if method_entry is None:
                        raise PermissionError("RPC method is not allowlisted")
                    scope, callback = method_entry
                    principal = owner.store.validate_capability(
                        str(request.get("token") or ""),
                        audience="hermes-control",
                        required_scope=scope,
                        gateway_pid=owner.gateway_pid,
                        gateway_start=owner.gateway_start,
                    )
                    owner.store.validate_live_binding(principal)
                    params = request.get("params") or {}
                    if not isinstance(params, dict):
                        raise RPCError("params must be an object")
                    result = callback(params, principal)
                    response = {
                        "version": PROTOCOL_VERSION,
                        "request_id": request_id,
                        "ok": True,
                        "result": result,
                    }
                except Exception as exc:
                    response = {
                        "version": PROTOCOL_VERSION,
                        "request_id": request_id,
                        "ok": False,
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    }
                _send_frame(self.request, response, owner.max_frame)

        self._server = _ThreadingUnixServer(str(self.socket_path), Handler)
        os.chmod(self.socket_path, 0o600)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="codex-control-rpc",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        self.socket_path.unlink(missing_ok=True)


class CodexControlRPCClient:
    def __init__(
        self,
        *,
        socket_path: Path,
        token: str,
        timeout: float = 5.0,
        max_frame: int = DEFAULT_MAX_FRAME,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.token = token
        self.timeout = timeout
        self.max_frame = max_frame

    def call(self, method: str, params: Optional[dict[str, Any]] = None) -> Any:
        request_id = uuid.uuid4().hex
        request = {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "token": self.token,
            "method": method,
            "params": params or {},
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            sock.connect(str(self.socket_path))
            _send_frame(sock, request, self.max_frame)
            response = _recv_frame(sock, self.max_frame)
        if response.get("request_id") != request_id:
            raise RPCError("response request_id mismatch")
        if not response.get("ok"):
            error = response.get("error") or {}
            raise RPCError(f"{error.get('type', 'RPCError')}: {error.get('message', '')}")
        return response.get("result")
