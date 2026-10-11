"""Minimal obs-websocket (v5) client, standard library only: connect, authenticate and
make requests. Used to grab frames of an OBS source for the scanner.

The password is read from OBS's own config file on this PC when not given."""

import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import threading
from pathlib import Path


class ObsError(Exception):
    pass


def obs_config():
    """OBS's websocket settings ({port, password, enabled}) from this PC, or None."""
    path = Path(os.environ.get("APPDATA", "")) / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {"port": int(cfg.get("server_port", 4455)),
            "password": cfg.get("server_password") if cfg.get("auth_required") else None,
            "enabled": bool(cfg.get("server_enabled"))}


class ObsClient:
    def __init__(self, host="127.0.0.1", port=None, password=None, timeout=5.0):
        cfg = obs_config() or {}
        self.host = host
        self.port = port or cfg.get("port", 4455)
        self.password = password if password is not None else cfg.get("password")
        self.timeout = timeout
        self.sock = None
        self._buf = b""
        self._lock = threading.Lock()

    # ---- connection
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall((
            f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n").encode())
        head = self._read_until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ObsError("OBS refused the websocket connection")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if accept not in head:
            raise ObsError("bad websocket handshake from OBS")
        hello = self._recv_json()
        if hello.get("op") != 0:
            raise ObsError("OBS did not say hello")
        identify = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = hello["d"].get("authentication")
        if auth:
            if not self.password:
                raise ObsError("OBS needs a websocket password and none was found")
            secret = base64.b64encode(hashlib.sha256((self.password + auth["salt"]).encode()).digest()).decode()
            identify["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        self._send_json({"op": 1, "d": identify})
        reply = self._recv_json()
        if reply.get("op") != 2:
            raise ObsError("OBS rejected the login (wrong websocket password?)")
        return self

    def close(self):
        if self.sock:
            try:
                self._send_frame(0x8, b"")
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # ---- requests
    def request(self, request_type, data=None):
        with self._lock:
            rid = secrets.token_hex(4)
            self._send_json({"op": 6, "d": {"requestType": request_type, "requestId": rid, "requestData": data or {}}})
            while True:
                msg = self._recv_json()
                if msg.get("op") == 7 and msg["d"].get("requestId") == rid:
                    status = msg["d"].get("requestStatus", {})
                    if not status.get("result"):
                        raise ObsError(f"{request_type} failed: {status.get('comment') or status.get('code')}")
                    return msg["d"].get("responseData") or {}

    # ---- websocket framing
    def _read_until(self, marker):
        while marker not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ObsError("OBS closed the connection")
            self._buf += chunk
        head, self._buf = self._buf.split(marker, 1)
        return head

    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise ObsError("OBS closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode, payload):
        mask = secrets.token_bytes(4)
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if n < 4096 else _mask_fast(payload, mask)
        self.sock.sendall(header + mask + masked)

    def _send_json(self, obj):
        self._send_frame(0x1, json.dumps(obj).encode())

    def _recv_json(self):
        parts = []
        while True:
            b1, b2 = self._read_exact(2)
            opcode, fin, n = b1 & 0x0F, b1 & 0x80, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if b2 & 0x80 else None
            payload = self._read_exact(n)
            if mask:
                payload = _mask_fast(payload, mask)
            if opcode == 0x8:
                raise ObsError("OBS closed the connection")
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if fin:
                return json.loads(b"".join(parts).decode("utf-8"))


def _mask_fast(data, mask):
    n = len(data)
    full = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(full, "big")).to_bytes(n, "big")
