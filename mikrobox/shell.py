"""Runs host commands. In dry-run mode it only records them."""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("mikrobox.shell")


class CommandError(RuntimeError):
    pass


class Shell:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.history: list[list[str]] = []  # filled in dry-run mode only

    def run(self, cmd: list[str], check: bool = True, input: str | None = None) -> str:
        cmd = [str(c) for c in cmd]
        log.debug("run: %s", " ".join(cmd))
        if self.dry_run:
            self.history.append(cmd)
            return ""
        proc = subprocess.run(cmd, capture_output=True, text=True, input=input)
        if check and proc.returncode != 0:
            raise CommandError(
                f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout
