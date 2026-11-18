"""Client extras: an address by DHCP, and small servers (web, iperf3, DHCP, DNS).

A client is a network namespace on the VM, so these are ordinary programs started
inside it with `ip netns exec`. Each one is tracked with a PID file in the run folder
and stopped together with the client.
"""

from __future__ import annotations

import html
import ipaddress
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .models import Lab, Node, NodeKind
from .network import host_nameservers, pid_alive

SERVICE_NAMES = {"web": "Web server", "iperf": "iperf3 server", "dhcp": "DHCP server", "dns": "DNS server"}

# busybox udhcpc calls this with the lease in its environment. It writes DNS servers to the
# client's own resolv.conf (passed in MIKROBOX_RESOLV), never to /etc/resolv.conf, which
# would be the VM's own file if the client had none yet.
UDHCPC_SCRIPT = """#!/bin/sh
case "$1" in
  deconfig)
    ip -4 addr flush dev "$interface"
    ip link set "$interface" up
    ;;
  bound|renew)
    ip -4 addr flush dev "$interface"
    ip addr add "$ip/${mask:-$subnet}" dev "$interface"
    if [ -n "$router" ]; then
      ip route del default 2>/dev/null
      for r in $router; do ip route add default via "$r" dev "$interface"; break; done
    fi
    if [ -n "$dns" ] && [ -n "$MIKROBOX_RESOLV" ]; then
      : > "$MIKROBOX_RESOLV"
      for d in $dns; do echo "nameserver $d" >> "$MIKROBOX_RESOLV"; done
    fi
    ;;
esac
exit 0
"""


def dhcp_range(address: str) -> tuple[str, str, str]:
    """The pool a client's DHCP server hands out: (first, last, netmask)."""
    iface = ipaddress.ip_interface(address)
    net = iface.network
    if net.num_addresses < 8:
        raise ValueError("A DHCP server needs a subnet of at least /29.")
    hosts = list(net.hosts()) if net.num_addresses <= 256 else None
    if hosts is not None:
        pool = hosts[len(hosts) // 2:] if len(hosts) < 200 else hosts[99:199]
        pool = [h for h in pool if h != iface.ip] or [h for h in hosts if h != iface.ip]
        return str(pool[0]), str(pool[-1]), str(net.netmask)
    first = net.network_address + 100
    return str(first), str(first + 99), str(net.netmask)


class ClientServices:
    def __init__(self, rt) -> None:
        self.rt = rt
        self.s = rt.s
        self.lease_cache: dict[int, tuple[float, str | None]] = {}

    # -- files -----------------------------------------------------------

    def _pidfile(self, node: Node, service: str) -> Path:
        return self.s.run_dir / f"n{node.id}.svc-{service}.pid"

    def script_path(self) -> Path:
        # udhcpc runs this file itself, so it can't live on /run, which is often
        # mounted noexec (Ubuntu, Debian).
        return self.s.data_dir / "bin" / "udhcpc.sh"

    def resolv_path(self, node: Node) -> Path:
        return self.s.netns_etc / self.rt.netns(node.id) / "resolv.conf"

    def install_script(self) -> None:
        path = self.script_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(UDHCPC_SCRIPT)
        os.chmod(path, 0o755)

    # -- the programs ----------------------------------------------------

    def commands(self, lab: Lab, node: Node) -> list[tuple[str, list[str], dict]]:
        """(service, argv, extra environment) for everything this client runs."""
        out: list[tuple[str, list[str], dict]] = []
        run = self.s.run_dir
        if node.addressing == "dhcp":
            out.append(("udhcpc", ["busybox", "udhcpc", "-f", "-i", "eth0", "-s", str(self.script_path()),
                                   "-x", f"hostname:{node.name}"],
                        {"MIKROBOX_RESOLV": str(self.resolv_path(node))}))
        if "web" in node.services:
            out.append(("web", [sys.executable, "-m", "http.server", "80", "--bind", "0.0.0.0",
                                "--directory", str(run / f"n{node.id}.www")], {}))
        if "iperf" in node.services:
            out.append(("iperf", ["iperf3", "-s"], {}))
        if "dhcp" in node.services or "dns" in node.services:
            out.append(("dnsmasq", self._dnsmasq(lab, node), {}))
        return out

    def _dnsmasq(self, lab: Lab, node: Node) -> list[str]:
        run = self.s.run_dir
        own = str(ipaddress.ip_interface(node.address).ip) if node.address else None
        argv = ["dnsmasq", "--keep-in-foreground", "--conf-file=/dev/null", "--interface=eth0",
                "--bind-interfaces", f"--pid-file={run / f'n{node.id}.dnsmasq-own.pid'}"]
        if "dns" in node.services:
            upstream = [d for d in (node.dns.split(", ") if node.dns else host_nameservers()) if d != own]
            argv += ["--port=53", "--no-resolv", "--no-hosts", "--domain=lab", "--expand-hosts",
                     "--local=/lab/", f"--addn-hosts={run / f'n{node.id}.hosts'}"]
            argv += [f"--server={d}" for d in upstream]
        else:
            argv.append("--port=0")
        if "dhcp" in node.services:
            first, last, mask = dhcp_range(node.address)
            argv += [f"--dhcp-range={first},{last},{mask},1h", "--dhcp-authoritative",
                     f"--dhcp-leasefile={run / f'n{node.id}.leases'}"]
            argv.append(f"--dhcp-option=3,{node.gateway}" if node.gateway else "--dhcp-option=3")
            if "dns" in node.services and own:
                argv.append(f"--dhcp-option=6,{own}")
            elif node.dns:
                argv.append(f"--dhcp-option=6,{','.join(node.dns.split(', '))}")
        return argv

    def _write_files(self, lab: Lab, node: Node) -> None:
        run = self.s.run_dir
        if "web" in node.services:
            www = run / f"n{node.id}.www"
            www.mkdir(parents=True, exist_ok=True)
            where = html.escape(node.address or "an address from DHCP")
            (www / "index.html").write_text(
                f"<!doctype html><meta charset=utf-8><title>{html.escape(node.name)}</title>"
                f"<h1>Hello from {html.escape(node.name)}</h1><p>Served by Mikrobox from {where}.</p>\n")
        if "dns" in node.services:
            lines = []
            for other in lab.nodes:
                ip = None
                if other.kind == NodeKind.client and other.address and other.addressing == "static":
                    ip = str(ipaddress.ip_interface(other.address).ip)
                elif other.kind != NodeKind.client:
                    ip = self.rt.mgmt_ip(lab, other)
                if ip:
                    lines.append(f"{ip} {other.name.lower()}")
            (run / f"n{node.id}.hosts").write_text("".join(line + "\n" for line in lines))
        if node.addressing == "dhcp":
            resolv = self.resolv_path(node)
            resolv.parent.mkdir(parents=True, exist_ok=True)
            if not resolv.exists():
                resolv.write_text("")  # must exist before anything runs, see UDHCPC_SCRIPT

    def start(self, lab: Lab, node: Node) -> None:
        cmds = self.commands(lab, node)
        if not cmds:
            return
        if node.addressing == "dhcp":
            self.install_script()
        self._write_files(lab, node)
        ns = self.rt.netns(node.id)
        for service, argv, extra in cmds:
            full = ["ip", "netns", "exec", ns, *argv]
            if self.s.dry_run:
                self.rt.sh.history.append(full)
                continue
            log = (self.s.run_dir / f"n{node.id}.svc-{service}.log").open("ab")
            proc = subprocess.Popen(full, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    env={**os.environ, **extra}, start_new_session=True, close_fds=True)
            log.close()
            self._pidfile(node, service).write_text(f"{proc.pid}\n")

    def stop(self, node: Node) -> None:
        for pidfile in self.s.run_dir.glob(f"n{node.id}.svc-*.pid"):
            service = pidfile.name.split(".svc-")[1].removesuffix(".pid")
            marker = {"udhcpc": "udhcpc", "web": "http.server", "iperf": "iperf3", "dnsmasq": "dnsmasq"}.get(service)
            pid = pid_alive(pidfile, marker) if marker else None
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            pidfile.unlink(missing_ok=True)
        self.lease_cache.pop(node.id, None)

    # -- what DHCP gave it -----------------------------------------------

    def leased(self, node: Node) -> str | None:
        """The address a DHCP client currently has, checked at most every few seconds."""
        if self.s.dry_run or node.addressing != "dhcp":
            return None
        now = time.time()
        cached = self.lease_cache.get(node.id)
        if cached and now - cached[0] < 3:
            return cached[1]
        value = None
        try:
            import json

            out = subprocess.run(["ip", "-n", self.rt.netns(node.id), "-j", "-4", "addr", "show", "dev", "eth0"],
                                 capture_output=True, text=True, timeout=3).stdout
            info = json.loads(out or "[]")
            for a in (info[0].get("addr_info", []) if info else []):
                value = f"{a['local']}/{a['prefixlen']}"
                break
        except Exception:
            pass
        self.lease_cache[node.id] = (now, value)
        return value
