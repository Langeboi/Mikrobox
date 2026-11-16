"""Websocket consoles: a router's serial port, or a shell inside a client."""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import subprocess
import termios
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect


async def _pump(ws: WebSocket, read_chunk, write_chunk) -> None:
    """Relay bytes both ways until either side closes."""

    async def outbound() -> None:
        while True:
            data = await read_chunk()
            if not data:
                return
            await ws.send_bytes(data)

    async def inbound() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            if data:
                await write_chunk(data)

    tasks = [asyncio.create_task(outbound()), asyncio.create_task(inbound())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def serial_console(ws: WebSocket, socket_path: Path) -> None:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))

    async def write(data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    try:
        await _pump(ws, lambda: reader.read(4096), write)
    except WebSocketDisconnect:
        pass
    finally:
        writer.close()


def _claim_tty() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


async def netns_console(ws: WebSocket, netns: str, name: str) -> None:
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["ip", "netns", "exec", netns, "env", f"PS1={name}# ", "TERM=xterm-256color",
         "bash", "--norc", "--noprofile", "-i"],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True, preexec_fn=_claim_tty,
    )
    os.close(slave)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def on_readable() -> None:
        try:
            data = os.read(master, 4096)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(master)
        queue.put_nowait(data)

    loop.add_reader(master, on_readable)

    async def write(data: bytes) -> None:
        os.write(master, data)

    try:
        await _pump(ws, queue.get, write)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            loop.remove_reader(master)
        except (ValueError, OSError):
            pass
        proc.terminate()
        os.close(master)
