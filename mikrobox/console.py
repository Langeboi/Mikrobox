"""Shared consoles.

Everyone who opens a device's console joins the same session: they all see the
same output and any of them can type. A router's serial port only accepts one
connection, so the server holds that one and fans it out. New arrivals get the
recent output replayed so they are not looking at a blank screen.

Text frames that start with CONTROL carry JSON for the browser (who is watching);
everything else is terminal data.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import subprocess
import termios
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

CONTROL = "\x01"
SCROLLBACK = 64 * 1024


class Hub:
    keep_when_empty = True

    def __init__(self, key: str, registry: dict) -> None:
        self.key = key
        self.registry = registry
        self.clients: dict[WebSocket, str] = {}
        self.buffer = bytearray()
        self.task: asyncio.Task | None = None
        self.closed = False

    async def open(self) -> None:
        raise NotImplementedError

    async def read(self) -> bytes:
        raise NotImplementedError

    async def write(self, data: bytes) -> None:
        raise NotImplementedError

    def close_backend(self) -> None:
        raise NotImplementedError

    async def run(self) -> None:
        try:
            while True:
                data = await self.read()
                if not data:
                    break
                self.buffer += data
                if len(self.buffer) > SCROLLBACK:
                    del self.buffer[:-SCROLLBACK]
                for ws in list(self.clients):
                    try:
                        await ws.send_bytes(data)
                    except Exception:
                        self.clients.pop(ws, None)
        finally:
            self.closed = True
            if self.registry.get(self.key) is self:
                del self.registry[self.key]
            self.close_backend()
            for ws in list(self.clients):
                try:
                    await ws.send_text("\r\n[console closed]\r\n")
                    await ws.close()
                except Exception:
                    pass

    async def announce(self) -> None:
        msg = CONTROL + json.dumps({"viewers": sorted(self.clients.values())})
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                self.clients.pop(ws, None)

    async def attach(self, ws: WebSocket, user: str | None) -> None:
        self.clients[ws] = user or "someone"
        if self.buffer:
            await ws.send_bytes(bytes(self.buffer))
        await self.announce()
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                data = msg.get("bytes") or (msg.get("text") or "").encode()
                if data and not self.closed:
                    await self.write(data)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        except BaseException:
            self.clients.pop(ws, None)  # cancelled or failed: no goodbyes to send
            raise
        self.clients.pop(ws, None)
        if not self.closed:
            if not self.clients and not self.keep_when_empty and self.task:
                self.task.cancel()
            else:
                await self.announce()


class SerialHub(Hub):
    """A router's serial port. Stays connected so its recent output is kept."""

    def __init__(self, key: str, registry: dict, socket_path: Path) -> None:
        super().__init__(key, registry)
        self.path = socket_path

    async def open(self) -> None:
        self.reader, self.writer = await asyncio.open_unix_connection(str(self.path))

    async def read(self) -> bytes:
        return await self.reader.read(4096)

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    def close_backend(self) -> None:
        try:
            self.writer.close()
        except Exception:
            pass


def _claim_tty() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class ShellHub(Hub):
    """A shell inside a client. Ends when the last person leaves."""

    keep_when_empty = False

    def __init__(self, key: str, registry: dict, netns: str, name: str, env: dict | None = None) -> None:
        super().__init__(key, registry)
        self.netns, self.name = netns, name
        self.env = env or {}  # e.g. DOCKER_HOST for a PC with Docker

    async def open(self) -> None:
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            ["ip", "netns", "exec", self.netns, "env", f"PS1={self.name}# ", "TERM=xterm-256color",
             *[f"{k}={v}" for k, v in self.env.items()], "bash", "--norc", "--noprofile", "-i"],
            stdin=slave, stdout=slave, stderr=slave, close_fds=True, preexec_fn=_claim_tty,
        )
        os.close(slave)
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.loop.add_reader(self.master, self._readable)

    def _readable(self) -> None:
        try:
            data = os.read(self.master, 4096)
        except OSError:
            data = b""
        if not data:
            self.loop.remove_reader(self.master)
        self.queue.put_nowait(data)

    async def read(self) -> bytes:
        return await self.queue.get()

    async def write(self, data: bytes) -> None:
        os.write(self.master, data)

    def close_backend(self) -> None:
        try:
            self.loop.remove_reader(self.master)
        except Exception:
            pass
        try:
            self.proc.terminate()
            os.close(self.master)
        except Exception:
            pass


async def join(registry: dict, key: str, make, ws: WebSocket, user: str | None) -> None:
    """Attach ws to the shared console `key`, creating it with make() if needed."""
    hub = registry.get(key)
    if hub is None or hub.closed:
        hub = make()
        await hub.open()
        registry[key] = hub
        hub.task = asyncio.create_task(hub.run())
    await hub.attach(ws, user)
