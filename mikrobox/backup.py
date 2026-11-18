"""Automatic config backups and their revision history.

A background loop runs /export on every included router that is running and
keeps the result as a new revision only when the config actually changed. The
config is read through the QEMU guest agent, which needs no network path or
login; routers on the Cloud fall back to SSH with the login stored per lab.

Revisions: <data>/backups/n<node id>/<UTC time>.rsc
Settings:  <data>/backup.json (holds the RouterOS password, so it is mode 600)
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from . import qga, routeros
from .models import Lab, Node, NodeKind

log = logging.getLogger("mikrobox.backup")

REV_ID = re.compile(r"^\d{8}T\d{6}Z$")
# RouterOS stamps each export with the time it was made; that line is not a change.
STAMP = re.compile(r"^# .* by RouterOS")
KEEP = 200
INTERVALS = (15, 60, 360, 1440)
DEFAULTS = {"enabled": False, "scope": "all", "nodes": [], "interval": 60,
            "username": "admin", "password": "", "sensitive": False, "last_run": 0}


def _meaningful(text: str) -> list[str]:
    return [line for line in text.splitlines() if not STAMP.match(line)]


class Backups:
    def __init__(self, rt) -> None:
        self.rt = rt
        self.activity = None  # set by the API: automatic runs are logged there
        self.events = None
        self.dir = rt.s.data_dir / "backups"
        self.file = rt.s.data_dir / "backup.json"
        self.lock = threading.RLock()
        self.status: dict[int, dict] = {}  # node id -> last attempt
        try:
            self.cfg: dict[str, dict] = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.cfg = {}

    # -- settings --------------------------------------------------------

    def settings(self, lab_id: int) -> dict:
        return {**DEFAULTS, **self.cfg.get(str(lab_id), {})}

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.file)

    def update(self, lab_id: int, changes: dict) -> None:
        with self.lock:
            s = self.settings(lab_id)
            s.update({k: v for k, v in changes.items() if k in DEFAULTS and v is not None})
            self.cfg[str(lab_id)] = s
            self._save()

    def set_node(self, lab_id: int, node_id: int, included: bool) -> None:
        with self.lock:
            s = self.settings(lab_id)
            nodes = set(s["nodes"])
            (nodes.add if included else nodes.discard)(node_id)
            s["nodes"] = sorted(nodes)
            self.cfg[str(lab_id)] = s
            self._save()

    def included(self, lab: Lab, node: Node) -> bool:
        s = self.settings(lab.id)
        return node.kind == NodeKind.chr and (s["scope"] == "all" or node.id in s["nodes"])

    def summary(self, lab: Lab) -> dict:
        """What the workspace needs; never the password."""
        s = self.settings(lab.id)
        return {"enabled": s["enabled"], "scope": s["scope"], "interval": s["interval"],
                "username": s["username"], "has_password": bool(s["password"]),
                "sensitive": s["sensitive"], "last_run": s["last_run"]}

    def node_summary(self, lab: Lab, node: Node) -> dict | None:
        if node.kind != NodeKind.chr:
            return None
        return {"included": self.included(lab, node), "revisions": len(self._files(node.id)),
                "last": self.status.get(node.id)}

    # -- revisions -------------------------------------------------------

    def _files(self, node_id: int) -> list[Path]:
        try:
            return sorted(p for p in (self.dir / f"n{node_id}").iterdir() if REV_ID.match(p.stem))
        except OSError:
            return []

    def revisions(self, node_id: int) -> list[dict]:
        out = []
        for p in reversed(self._files(node_id)):
            text = p.read_text(errors="replace")
            out.append({"id": p.stem, "size": len(text), "lines": len(_meaningful(text))})
        return out

    def read(self, node_id: int, rev: str) -> str:
        if not REV_ID.match(rev):
            raise KeyError(rev)
        try:
            return (self.dir / f"n{node_id}" / f"{rev}.rsc").read_text(errors="replace")
        except OSError:
            raise KeyError(rev)

    def diff(self, node_id: int, a: str, b: str) -> dict:
        old, new = _meaningful(self.read(node_id, a)), _meaningful(self.read(node_id, b))
        lines, added, removed = [], 0, 0
        for line in list(difflib.unified_diff(old, new, lineterm="", n=3))[2:]:
            kind = line[:1]
            if kind == "@":
                lines.append({"t": "@", "s": line})
                continue
            added += kind == "+"
            removed += kind == "-"
            lines.append({"t": kind if kind in "+-" else " ", "s": line[1:]})
        return {"a": a, "b": b, "added": added, "removed": removed, "lines": lines}

    def store(self, node_id: int, text: str) -> bool:
        """Keep text as a new revision unless it matches the latest one. True if kept."""
        files = self._files(node_id)
        if files and _meaningful(files[-1].read_text(errors="replace")) == _meaningful(text):
            return False
        folder = self.dir / f"n{node_id}"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        while True:  # never overwrite a revision saved in the same second
            target = folder / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(stamp)) + ".rsc")
            if not target.exists():
                break
            stamp += 1
        target.write_text(text)
        for old in self._files(node_id)[:-KEEP]:
            old.unlink(missing_ok=True)
        return True

    def is_included(self, lab_id: int, node_id: int) -> bool:
        return node_id in self.settings(lab_id)["nodes"]

    def stash_node(self, node_id: int, dest: Path) -> None:
        """Revisions of a device going to the trash."""
        src = self.dir / f"n{node_id}"
        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dest)
        self.status.pop(node_id, None)

    def unstash_node(self, node_id: int, src: Path) -> None:
        if src.exists():
            shutil.rmtree(self.dir / f"n{node_id}", ignore_errors=True)
            self.dir.mkdir(parents=True, exist_ok=True)
            shutil.move(src, self.dir / f"n{node_id}")

    def forget_node(self, node_id: int) -> None:
        shutil.rmtree(self.dir / f"n{node_id}", ignore_errors=True)
        self.status.pop(node_id, None)

    def forget_lab(self, lab: Lab) -> None:
        for node in lab.nodes:
            self.forget_node(node.id)
        with self.lock:
            if self.cfg.pop(str(lab.id), None) is not None:
                self._save()

    # -- running ---------------------------------------------------------

    def backup_node(self, lab: Lab, node: Node) -> dict:
        s = self.settings(lab.id)
        result = {"node": node.id, "name": node.name, "ok": False, "changed": False,
                  "time": int(time.time()), "message": ""}
        if not self.rt.node_running(node):
            result["message"] = "Not running"
            self.status[node.id] = result
            return result

        text, problems = None, []
        # First through the hypervisor: no Cloud link and no login needed.
        agent = self.rt.agent_socket(node)
        if agent.exists():
            try:
                out = qga.run_script(agent, "/export show-sensitive;" if s["sensitive"] else "/export;")
                if out.strip():
                    text, result["via"] = out, "agent"
                else:
                    problems.append("the guest agent returned nothing")
            except qga.AgentError as e:
                problems.append(f"guest agent: {e}")
        # Otherwise over SSH, for routers that are on the Cloud.
        if text is None and self.rt.cloud_port(lab, node) is not None:
            host = self.rt.mgmt_ip(lab, node)
            if host is None:
                problems.append("its Cloud address is not known yet")
            else:
                try:
                    out = routeros.export_config(host, s["username"], s["password"], s["sensitive"])
                    if out.strip():
                        text, result["via"] = out, "ssh"
                    else:
                        problems.append("the router returned an empty config over SSH")
                except routeros.RouterError as e:
                    problems.append(str(e))

        if text is not None:
            result.update(ok=True, changed=self.store(node.id, text))
            result["message"] = "New revision saved" if result["changed"] else "No changes"
        elif not problems:
            result["message"] = "Stop and start this router once, then it can be backed up without a Cloud link"
        else:
            result["message"] = "; ".join(problems)
        self.status[node.id] = result
        return result

    def restore(self, lab: Lab, node: Node, rev: str, replace: bool) -> dict:
        """Load a saved revision back into the router. Saves the current config first."""
        text = self.read(node.id, rev)
        if not self.rt.node_running(node):
            return {"ok": False, "message": f"Start {node.name} first."}
        self.backup_node(lab, node)  # so this restore can itself be undone
        s = self.settings(lab.id)
        problems = []
        agent = self.rt.agent_socket(node)
        if agent.exists():
            try:
                qga.write_file(agent, routeros.RESTORE_FILE, text)
                qga.run_script(agent, routeros.RESET_COMMAND if replace else routeros.MERGE_COMMAND,
                               timeout=180, wait=not replace)
                return {"ok": True, "via": "agent", "message": self._restored(node, replace)}
            except qga.AgentError as e:
                problems.append(f"guest agent: {e}")
        if self.rt.cloud_port(lab, node) is not None:
            host = self.rt.mgmt_ip(lab, node)
            if host is None:
                problems.append("its Cloud address is not known yet")
            else:
                try:
                    routeros.restore_config(host, s["username"], s["password"], text, replace)
                    return {"ok": True, "via": "ssh", "message": self._restored(node, replace)}
                except routeros.RouterError as e:
                    problems.append(str(e))
        return {"ok": False, "message": "; ".join(problems) or
                f"Stop and start {node.name} once, then it can be restored without a Cloud link"}

    @staticmethod
    def _restored(node: Node, replace: bool) -> str:
        return (f"{node.name} is resetting and will load the revision when it comes back up."
                if replace else f"The revision was applied on top of {node.name}'s current config.")

    def latest(self, node_id: int) -> tuple[str, str] | None:
        files = self._files(node_id)
        return (files[-1].stem, files[-1].read_text(errors="replace")) if files else None

    def run(self, lab: Lab, only: int | None = None) -> list[dict]:
        nodes = [n for n in lab.nodes if n.kind == NodeKind.chr
                 and (n.id == only if only is not None else self.included(lab, n))]
        results = [self.backup_node(lab, n) for n in nodes]
        if only is None:
            with self.lock:
                s = self.settings(lab.id)
                s["last_run"] = int(time.time())
                self.cfg[str(lab.id)] = s
                self._save()
        return results

    def loop(self) -> None:
        """Runs forever in a background thread."""
        while True:
            time.sleep(30)
            try:
                for lab in list(self.rt.store.state.labs):
                    s = self.settings(lab.id)
                    if s["enabled"] and time.time() - s["last_run"] >= s["interval"] * 60:
                        results = self.run(lab)
                        log.info("backup of lab %s: %s", lab.name,
                                 ", ".join(f"{r['name']}: {r['message']}" for r in results) or "no routers")
                        changed = [r["name"] for r in results if r.get("changed")]
                        if changed and self.activity:
                            self.activity.record(f"Automatic backup saved new revisions of {', '.join(changed)}",
                                                 lab, kind="system")
                        if self.events:
                            self.events.publish({"t": "labs"})
            except Exception:
                log.exception("automatic backup failed")
