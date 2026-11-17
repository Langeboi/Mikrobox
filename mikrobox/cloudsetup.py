"""Put the VM's own network card into the Cloud bridge.

After this, anything linked to the Cloud in a lab sits directly on your network:
it can take an address from your DHCP server or use a static one, and it has
internet. The VM keeps its address, which moves from the card onto the bridge.

    python -m mikrobox.cloudsetup status
    python -m mikrobox.cloudsetup enable            # shows the plan, changes nothing
    python -m mikrobox.cloudsetup enable --apply    # does it, rolls back if the gateway is lost
    python -m mikrobox.cloudsetup disable --apply   # restores the original network config

This rewrites the VM's network configuration (ifupdown or netplan). Have the
Proxmox console at hand in case the VM becomes unreachable.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

BRIDGE = "mbcloud"
STATE_DIR = Path("/var/lib/mikrobox/cloud-setup")
LOG = STATE_DIR / "last-run.log"
MANIFEST = STATE_DIR / "manifest.json"
MOVE_EXCLUDE = {"match", "set-name", "wakeonlan", "mtu", "macaddress", "optional"}


class Unsupported(Exception):
    pass


# ---------------------------------------------------------------- pure

def rewrite_interfaces(text: str, nic: str, mac: str) -> str:
    """ifupdown: turn `iface NIC inet X` into a bridge stanza that owns the address."""
    n = re.escape(nic)
    if not re.search(rf"^\s*iface\s+{n}\s+inet\s+\S+", text, re.M):
        raise Unsupported(f"no 'iface {nic} inet ...' stanza found")
    out = []
    for line in text.splitlines():
        if re.match(rf"^\s*(auto|allow-hotplug)\s+{n}\s*$", line):
            continue
        m = re.match(rf"^\s*iface\s+{n}\s+inet\s+(\S+)\s*$", line)
        if m:
            out += [
                f"iface {nic} inet manual",
                "",
                f"auto {BRIDGE}",
                f"iface {BRIDGE} inet {m.group(1)}",
                f"    bridge_ports {nic}",
                "    bridge_stp off",
                "    bridge_fd 0",
                f"    bridge_hw {mac}",
            ]
            continue
        m6 = re.match(rf"^(\s*)iface\s+{n}\s+inet6\s+(\S+)\s*$", line)
        if m6:
            out.append(f"{m6.group(1)}iface {BRIDGE} inet6 {m6.group(2)}")
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def rewrite_netplan(doc: dict, nic: str, mac: str) -> dict | None:
    """netplan: move the card's addressing onto a bridge. None if this file lacks the card."""
    net = (doc or {}).get("network") or {}
    eths = net.get("ethernets") or {}
    key = None
    for name, cfg in eths.items():
        cfg = cfg or {}
        match = cfg.get("match") or {}
        if name == nic or cfg.get("set-name") == nic or match.get("name") == nic \
                or str(match.get("macaddress", "")).lower() == mac.lower():
            key = name
            break
    if key is None:
        return None
    cfg = eths[key] or {}
    keep = {k: v for k, v in cfg.items() if k in MOVE_EXCLUDE}
    moved = {k: v for k, v in cfg.items() if k not in MOVE_EXCLUDE}
    keep.update({"dhcp4": False, "dhcp6": False})
    eths[key] = keep
    bridge = {"interfaces": [key], "macaddress": mac,
              "parameters": {"stp": False, "forward-delay": 0}, **moved}
    if moved.get("dhcp4") and "dhcp-identifier" not in moved:
        bridge["dhcp-identifier"] = "mac"
    net.setdefault("bridges", {})[BRIDGE] = bridge
    doc["network"] = net
    return doc


# -------------------------------------------------------------- system

def sh(cmd: list[str], check: bool = True) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def default_route() -> tuple[str | None, str | None]:
    try:
        routes = json.loads(sh(["ip", "-j", "-4", "route", "show", "default"]))
    except Exception:
        return None, None
    return (routes[0].get("dev"), routes[0].get("gateway")) if routes else (None, None)


def bridge_ports() -> list[str]:
    try:
        return sorted(p.name for p in Path(f"/sys/class/net/{BRIDGE}/brif").iterdir())
    except OSError:
        return []


def uplink() -> str | None:
    return next((p for p in bridge_ports() if Path(f"/sys/class/net/{p}/device").exists()), None)


def gateway_reachable(gw: str, seconds: int) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if subprocess.run(["ping", "-c", "1", "-W", "2", gw], capture_output=True).returncode == 0:
            return True
        time.sleep(1)
    return False


def make_plan(nic: str, mac: str) -> dict:
    """Work out which file to rewrite and what it becomes. Changes nothing."""
    netplan_files = sorted(Path("/etc/netplan").glob("*.yaml")) if shutil.which("netplan") else []
    for path in netplan_files:
        import yaml

        doc = yaml.safe_load(path.read_text()) or {}
        new = rewrite_netplan(doc, nic, mac)
        if new is not None:
            return {"kind": "netplan", "path": str(path), "old": path.read_text(),
                    "new": yaml.safe_dump(new, sort_keys=False)}
    candidates = [Path("/etc/network/interfaces")]
    candidates += sorted(Path("/etc/network/interfaces.d").glob("*")) \
        if Path("/etc/network/interfaces.d").is_dir() else []
    for path in candidates:
        try:
            text = path.read_text()
            return {"kind": "ifupdown", "path": str(path), "old": text,
                    "new": rewrite_interfaces(text, nic, mac)}
        except (OSError, Unsupported):
            continue
    raise Unsupported(
        f"Could not find {nic} in /etc/netplan or /etc/network/interfaces. This VM's network is "
        "probably managed by NetworkManager or systemd-networkd directly, which this tool does "
        f"not rewrite. Create a bridge named {BRIDGE} containing {nic} by hand and Mikrobox will use it."
    )


def restart_mikrobox() -> None:
    subprocess.run(["systemctl", "restart", "mikrobox.service"], capture_output=True)


def clear_runtime_bridge() -> list[str]:
    """Remove the private NAT bridge Mikrobox made, remembering the lab ports on it."""
    ports = [p for p in bridge_ports() if not Path(f"/sys/class/net/{p}/device").exists()]
    try:
        pid = int(Path("/run/mikrobox/dnsmasq.pid").read_text().strip())
        os.kill(pid, 15)
    except (OSError, ValueError):
        pass
    if Path(f"/sys/class/net/{BRIDGE}").exists():
        sh(["ip", "link", "del", BRIDGE], check=False)
    return ports


def bring_up(kind: str, nic: str, path: Path, new_text: str) -> None:
    if kind == "netplan":
        path.write_text(new_text)
        sh(["netplan", "apply"])
    else:
        sh(["ifdown", "--force", nic], check=False)  # with the old config, so its DHCP client stops
        sh(["ip", "addr", "flush", "dev", nic], check=False)
        path.write_text(new_text)
        sh(["ifup", BRIDGE])


def restore(kind: str, nic: str, path: Path, old_text: str) -> None:
    if kind == "netplan":
        path.write_text(old_text)
        sh(["netplan", "apply"], check=False)
        sh(["ip", "link", "del", BRIDGE], check=False)
    else:
        sh(["ifdown", "--force", BRIDGE], check=False)
        sh(["ip", "link", "del", BRIDGE], check=False)
        path.write_text(old_text)
        sh(["ifup", "--force", nic], check=False)


def do_enable(nic: str) -> int:
    mac = Path(f"/sys/class/net/{nic}/address").read_text().strip()
    _, gw = default_route()
    plan = make_plan(nic, mac)
    path = Path(plan["path"])
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    backup = STATE_DIR / (path.name + ".orig")
    backup.write_text(plan["old"])
    MANIFEST.write_text(json.dumps({"kind": plan["kind"], "path": plan["path"], "nic": nic,
                                    "backup": str(backup)}))
    log(f"Backed up {path} to {backup}")
    lab_ports = clear_runtime_bridge()
    try:
        bring_up(plan["kind"], nic, path, plan["new"])
        ok = gateway_reachable(gw, 45) if gw else True
    except Exception as e:
        log(f"Applying failed: {e}")
        ok = False
    if not ok:
        log("Gateway not reachable through the bridge. Rolling back.")
        restore(plan["kind"], nic, path, plan["old"])
        MANIFEST.unlink(missing_ok=True)
        restart_mikrobox()
        log("Rolled back to the original network configuration.")
        return 1
    for port in lab_ports:
        sh(["ip", "link", "set", port, "master", BRIDGE], check=False)
    restart_mikrobox()
    log(f"Done. {nic} is now a port of {BRIDGE} and the Cloud is on your network.")
    return 0


def do_disable() -> int:
    if not MANIFEST.exists():
        log("Nothing to undo: the Cloud bridge was not set up by this tool.")
        return 1
    m = json.loads(MANIFEST.read_text())
    restore(m["kind"], m["nic"], Path(m["path"]), Path(m["backup"]).read_text())
    MANIFEST.unlink(missing_ok=True)
    restart_mikrobox()
    log("Original network configuration restored. The Cloud is back to NAT through the VM. "
        "Restart lab devices that were linked to the Cloud.")
    return 0


def detach(args: list[str]) -> int:
    """Run the change in its own systemd unit so a dropped SSH session cannot interrupt it."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("")
    unit = "mikrobox-cloud-setup"
    subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
    sh(["systemd-run", f"--unit={unit}", "--collect", "--quiet",
        sys.executable, "-m", "mikrobox.cloudsetup", *args, "--foreground"])
    print(f"Working. If this session drops, reconnect and read {LOG}")
    seen, deadline = 0, time.time() + 120
    while time.time() < deadline:
        time.sleep(1)
        text = LOG.read_text() if LOG.exists() else ""
        if len(text) > seen:
            print(text[seen:], end="", flush=True)
            seen = len(text)
        if "Done." in text or "Rolled back" in text or "restored" in text or "Nothing to undo" in text:
            return 0 if "Done." in text or "restored" in text else 1
    print(f"Still running after two minutes; check {LOG}")
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mikrobox.cloudsetup", description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["status", "enable", "disable"])
    p.add_argument("--nic", help="network card to use (default: the one with the default route)")
    p.add_argument("--apply", action="store_true", help="make the change instead of only showing it")
    p.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args(argv)

    if os.environ.get("MIKROBOX_TAG", "mb") != "mb":
        print("Putting the Cloud on your own network is only for the main install. "
              "A side-by-side install keeps its Cloud behind the VM (NAT).", file=sys.stderr)
        return 1
    up = uplink()
    if a.command == "status":
        dev, gw = default_route()
        if up:
            print(f"Bridged: {up} is a port of {BRIDGE}. The Cloud is on your network (gateway {gw}).")
        else:
            print(f"NAT: the Cloud is a private network behind this VM. Default route is via {dev}.")
        return 0

    if os.geteuid() != 0:
        print("Run this as root (sudo).", file=sys.stderr)
        return 1

    if a.command == "disable":
        if not a.apply:
            print("This restores the VM's original network configuration. Re-run with --apply.")
            return 0
        return do_disable() if a.foreground else detach(["disable", "--apply"])

    if up:
        print(f"Already bridged: {up} is a port of {BRIDGE}.")
        return 0
    nic = a.nic or default_route()[0]
    if not nic or nic == BRIDGE or not Path(f"/sys/class/net/{nic}/device").exists():
        print("Could not work out which network card to use. Pass --nic <name>.", file=sys.stderr)
        return 1
    mac = Path(f"/sys/class/net/{nic}/address").read_text().strip()
    try:
        plan = make_plan(nic, mac)
    except Unsupported as e:
        print(str(e), file=sys.stderr)
        return 1
    if plan["kind"] == "ifupdown" and not (shutil.which("brctl") or Path("/usr/sbin/brctl").exists()):
        print("Install bridge-utils first: apt-get install bridge-utils", file=sys.stderr)
        return 1
    if not a.apply:
        print(f"Plan: make {nic} a port of a new bridge {BRIDGE} and move the VM's address onto it.")
        print(f"File to change: {plan['path']} ({plan['kind']})\n")
        sys.stdout.writelines(difflib.unified_diff(
            plan["old"].splitlines(True), plan["new"].splitlines(True), "current", "new"))
        print("\nThe network drops for a few seconds. If the gateway cannot be reached afterwards, "
              "the change is rolled back automatically.")
        if plan["kind"] == "netplan":
            print("With DHCP the VM may be given a different address than before.")
        print("If the Proxmox firewall is enabled on this VM's network card, turn off its MAC filter, "
              "or lab devices will get no traffic.")
        print("\nRe-run with --apply to do it.")
        return 0
    return do_enable(nic) if a.foreground else detach(["enable", "--apply", "--nic", nic])


if __name__ == "__main__":
    sys.exit(main())
