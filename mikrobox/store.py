"""Labs are kept in one JSON file, rewritten atomically on every change."""

from __future__ import annotations

import os
from pathlib import Path

from .models import State


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.on_save = None  # called after every save, e.g. to push the change to browsers
        if path.exists():
            self.state = State.model_validate_json(path.read_text())
        else:
            self.state = State()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(self.state.model_dump_json(indent=2))
        os.replace(tmp, self.path)
        if self.on_save:
            self.on_save()
