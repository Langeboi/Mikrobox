"""Deleted devices, links and shapes, kept for a while so that undo can put them back.

Each delete gets a folder under <data>/trash/<token>: what was removed (items.json) and,
per device, its disk, a PC's Docker data and the router's backup revisions, moved there
rather than erased. Restoring moves them back. Folders older than KEEP_HOURS go.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from pathlib import Path

KEEP_HOURS = 24
TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,40}$")


class Trash:
    def __init__(self, root: Path) -> None:
        self.root = root

    def new(self) -> tuple[str, Path]:
        token = secrets.token_urlsafe(12)
        folder = self.root / token
        folder.mkdir(parents=True)
        return token, folder

    def save(self, token: str, record: dict) -> None:
        (self.root / token / "items.json").write_text(json.dumps(record))

    def load(self, token: str) -> tuple[dict, Path] | None:
        if not TOKEN.match(token):
            return None
        folder = self.root / token
        try:
            return json.loads((folder / "items.json").read_text()), folder
        except (OSError, ValueError):
            return None

    def drop(self, token: str) -> None:
        if TOKEN.match(token):
            shutil.rmtree(self.root / token, ignore_errors=True)

    def purge(self, now: float | None = None) -> None:
        cutoff = (now or time.time()) - KEEP_HOURS * 3600
        try:
            folders = list(self.root.iterdir())
        except OSError:
            return
        for folder in folders:
            try:
                if folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass


def free_name(want: str, taken: set[str]) -> str:
    """want, or the next free name after it in its series: R10 -> R11, EDGE -> EDGE2."""
    if want not in taken:
        return want
    m = re.match(r"^(.*?)(\d+)$", want)
    base = m.group(1) if m and m.group(1) else want
    i = int(m.group(2)) + 1 if m and m.group(1) else 2
    while True:
        name = f"{base[:63 - len(str(i))]}{i}"
        if name not in taken:
            return name
        i += 1
