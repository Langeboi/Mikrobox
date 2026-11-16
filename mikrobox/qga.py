"""Runs a RouterOS script inside a CHR through the QEMU guest agent.

This goes through the hypervisor, not the network, so it works on routers that
have no link to the Cloud and it needs no RouterOS login. CHR's agent takes the
script as guest-exec input-data and returns what it printed.
"""

from __future__ import annotations

import base64
import json
import random
import socket
import time
from pathlib import Path


class AgentError(Exception):
    pass


class _Channel:
    def __init__(self, path: Path, timeout: float) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(str(path))
        except OSError as e:
            self.sock.close()
            raise AgentError(f"cannot reach the guest agent ({e})")
        self.buf = b""

    def close(self) -> None:
        self.sock.close()

    def call(self, execute: str, arguments: dict | None = None, match=None) -> dict:
        msg = {"execute": execute}
        if arguments is not None:
            msg["arguments"] = arguments
        try:
            self.sock.sendall(json.dumps(msg).encode() + b"\n")
            while True:
                while b"\n" not in self.buf:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        raise AgentError("the guest agent closed the connection")
                    self.buf += chunk
                line, self.buf = self.buf.split(b"\n", 1)
                line = line.strip().lstrip(b"\xff")
                if not line:
                    continue
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue  # leftovers from an earlier, interrupted exchange
                if "error" in reply:
                    raise AgentError(reply["error"].get("desc", "the guest agent refused the request"))
                if match is None or match(reply.get("return")):
                    return reply
        except socket.timeout:
            raise AgentError("the guest agent did not answer (is RouterOS still booting?)")
        except OSError as e:
            raise AgentError(f"guest agent connection failed ({e})")


def write_file(socket_path: Path, name: str, text: str) -> None:
    """Create a file on the router."""
    ch = _Channel(socket_path, timeout=10)
    try:
        token = random.randint(1, 2**31 - 1)
        ch.call("guest-sync", {"id": token}, match=lambda r: r == token)
        handle = ch.call("guest-file-open", {"path": name, "mode": "w"}).get("return")
        if handle is None:
            raise AgentError("the guest agent could not create the file")
        data = text.encode()
        for i in range(0, len(data), 32768):
            ch.call("guest-file-write", {"handle": handle,
                                         "buf-b64": base64.b64encode(data[i:i + 32768]).decode()})
        ch.call("guest-file-close", {"handle": handle})
    finally:
        ch.close()


def run_script(socket_path: Path, script: str, timeout: float = 60, wait: bool = True) -> str:
    """Run a RouterOS script and return its output. wait=False for one that reboots the router."""
    ch = _Channel(socket_path, timeout=10)
    try:
        token = random.randint(1, 2**31 - 1)
        ch.call("guest-sync", {"id": token}, match=lambda r: r == token)
        started = ch.call("guest-exec", {
            "input-data": base64.b64encode(script.encode()).decode(), "capture-output": True})
        pid = (started.get("return") or {}).get("pid")
        if pid is None:
            raise AgentError("the guest agent did not start the script")
        if not wait:
            return ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = ch.call("guest-exec-status", {"pid": pid}).get("return") or {}
            if status.get("exited"):
                out = base64.b64decode(status.get("out-data", "")).decode("utf-8", errors="replace")
                err = base64.b64decode(status.get("err-data", "")).decode("utf-8", errors="replace")
                if status.get("exitcode", 0) != 0 and not out.strip():
                    raise AgentError(err.strip() or f"the script failed (exit code {status.get('exitcode')})")
                return out
            time.sleep(0.3)
        raise AgentError("the script did not finish in time")
    finally:
        ch.close()
