"""The activity log: who did what, and when.

Entries are kept in <data>/activity.jsonl, one JSON object per line. The newest ones are
held in memory for the workspace; the file is trimmed when it grows large.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

from .events import CURRENT_USER, Events

KEEP_IN_MEMORY = 2000
TRIM_AT, TRIM_TO = 20000, 10000


class Activity:
    def __init__(self, path: Path, events: Events) -> None:
        self.path = path
        self.events = events
        self.lock = threading.Lock()
        self.entries: deque[dict] = deque(maxlen=KEEP_IN_MEMORY)
        self.lines = 0
        self.next_id = 1
        try:
            with path.open() as f:
                for line in f:
                    self.lines += 1
                    try:
                        self.entries.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        if self.entries:
            self.next_id = max(e.get("id", 0) for e in self.entries) + 1

    def record(self, text: str, lab=None, kind: str = "info", user: str | None = None) -> dict:
        """kind: info, danger (deletions and resets) or system (done by Mikrobox itself)."""
        who = user or CURRENT_USER.get()
        if who is None:
            who, kind = ("Mikrobox", "system") if kind == "system" else ("someone", kind)
        with self.lock:
            entry = {"id": self.next_id, "ts": round(time.time(), 3), "user": who, "text": text,
                     "kind": kind, "lab": getattr(lab, "id", lab), "lab_name": getattr(lab, "name", None)}
            self.next_id += 1
            self.entries.append(entry)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as f:
                    f.write(json.dumps(entry) + "\n")
                self.lines += 1
                if self.lines > TRIM_AT:
                    self._trim()
            except OSError:
                pass
        self.events.publish({"t": "activity", "entry": entry})
        return entry

    def _trim(self) -> None:
        keep = self.path.read_text().splitlines()[-TRIM_TO:]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(line + "\n" for line in keep))
        tmp.replace(self.path)
        self.lines = len(keep)

    def recent(self, lab: int | None = None, limit: int = 200) -> list[dict]:
        with self.lock:
            items = [e for e in self.entries if lab is None or e.get("lab") in (lab, None)]
        return items[-limit:]
