"""Instant updates: every open workspace keeps one websocket, and changes are pushed to it.

Events are small hints such as {"t": "labs"} ("something changed, fetch again"),
{"t": "presence"} or {"t": "activity", "entry": {...}}. publish() can be called from any
thread: request handlers run in a thread pool and backups run in their own thread.
"""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar

from fastapi import WebSocket, WebSocketDisconnect

# Who is making the current request; set by the sign-in middleware.
CURRENT_USER: ContextVar[str | None] = ContextVar("mikrobox_user", default=None)

QUEUE_SIZE = 200  # a client this far behind is dropped; its browser reconnects and refetches


class Events:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queues: set[asyncio.Queue] = set()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def publish(self, event: dict) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        text = json.dumps(event)
        try:
            loop.call_soon_threadsafe(self._deliver, text)
        except RuntimeError:
            pass  # shutting down

    def _deliver(self, text: str) -> None:
        for q in list(self.queues):
            try:
                q.put_nowait(text)
            except asyncio.QueueFull:
                self.queues.discard(q)
                q.put_nowait_closed = True  # type: ignore[attr-defined]

    async def serve(self, ws: WebSocket) -> None:
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        self.queues.add(q)

        async def writer() -> None:
            await ws.send_text(json.dumps({"t": "hello"}))
            while True:
                text = await q.get()
                await ws.send_text(text)
                if getattr(q, "put_nowait_closed", False) and q.empty():
                    return

        async def reader() -> None:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return

        tasks = [asyncio.create_task(writer()), asyncio.create_task(reader())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            self.queues.discard(q)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await ws.close()
            except Exception:
                pass
