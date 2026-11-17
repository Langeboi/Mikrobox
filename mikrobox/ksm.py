"""Kernel Samepage Merging: lets devices booted from the same image share identical memory.

QEMU already marks guest memory as mergeable; the kernel's ksmd only needs switching on.
It costs some CPU while it scans, and savings build up over a few minutes after boot.
The choice is kept in <data>/host.json and applied whenever Mikrobox starts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import Settings


class Ksm:
    def __init__(self, settings: Settings) -> None:
        self.dir = settings.ksm_dir
        self.dry_run = settings.dry_run
        self.file = settings.data_dir / "host.json"

    def _host(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def _read(self, name: str) -> int | None:
        try:
            return int((self.dir / name).read_text().strip())
        except (OSError, ValueError):
            return None

    def available(self) -> bool:
        return (self.dir / "run").exists()

    def wanted(self) -> bool:
        return bool(self._host().get("ksm", True))  # on unless switched off

    def apply(self) -> None:
        if self.dry_run or not self.available():
            return
        try:
            # 0 stops scanning but keeps what is merged, so switching off never
            # suddenly needs more memory than the host has.
            (self.dir / "run").write_text("1" if self.wanted() else "0")
        except OSError:
            pass

    def set(self, enabled: bool) -> dict:
        host = self._host()
        host["ksm"] = bool(enabled)
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(host, indent=2))
        os.replace(tmp, self.file)
        self.apply()
        return self.status()

    def status(self) -> dict:
        sharing = self._read("pages_sharing") or 0
        return {
            "available": self.available(),
            "enabled": self.wanted(),
            "running": self._read("run") == 1,
            # pages_sharing: how many more places use an already shared page, i.e. pages saved.
            "saved_mb": sharing * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024),
        }
