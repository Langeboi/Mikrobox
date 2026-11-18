"""Host networking.

Every node port has one host-side interface: a tap for a router port, the host
end of a veth pair for a client port. A link between two ports is a pair of tc
redirects, so every frame (including STP, LACP and LLDP, which a Linux bridge
would swallow) passes untouched.

The Cloud is a real bridge. It runs in one of two modes:

* bridged: the VM's own network card is a port of the bridge (set up once with
  `python -m mikrobox.cloudsetup`). Anything linked to the Cloud sits directly
  on your network and uses its DHCP server and gateway.
* nat: a private network with DHCP, with internet through the VM.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import signal
from pathlib import Path

from .config import Settings
from .shell import CommandError, Shell

OLD_BRIDGE = "mbmgmt"

NFT_RULES = """\
table ip mikrobox
delete table ip mikrobox
table ip mikrobox {{
    chain post {{
        type nat hook postrouting priority 100;
        oifname "{bridge}" masquerade
        ip saddr {net} oifname != "{bridge}" masquerade
    }}
}}
"""
NFT_CLEAR = "table ip mikrobox\ndelete table ip mikrobox\n"


def pid_alive(pidfile: Path, must_contain: str) -> int | None:
    """Return the pid in pidfile if that process is alive and looks right."""
    try:
        pid = int(pidfile.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except (OSError, ValueError):
        return None
    return pid if must_contain in cmdline else None


def host_nameservers(resolv: Path = Path("/etc/resolv.conf")) -> list[str]:
    """Resolvers the VM uses, minus local stubs a router could not reach."""
    servers = []
    for candidate in (Path("/run/systemd/resolve/resolv.conf"), resolv):
        try:
            for line in candidate.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    try:
                        ip = ipaddress.ip_address(parts[1])
                    except ValueError:
                        continue
                    if ip.version == 4 and not ip.is_loopback and str(ip) not in servers:
                        servers.append(str(ip))
        except OSError:
            continue
        if servers:
            break
    return servers[:3] or ["1.1.1.1", "9.9.9.9"]


class Net:
    def __init__(self, settings: Settings, sh: Shell) -> None:
        self.s = settings
        self.sh = sh

    # -- helpers ---------------------------------------------------------

    def exists(self, iface: str) -> bool:
        return (self.s.sys_net / iface).exists()

    def _write(self, path: str, value: str) -> None:
        if self.s.dry_run:
            return
        try:
            Path(path).write_text(value)
        except OSError:
            pass

    def _quiet(self, iface: str) -> None:
        # Stop the host from sending its own IPv6 chatter into a lab link.
        self._write(f"/proc/sys/net/ipv6/conf/{iface}/disable_ipv6", "1")

    def _ports(self, bridge: str) -> list[str]:
        try:
            return sorted(p.name for p in (self.s.sys_net / bridge / "brif").iterdir())
        except OSError:
            return []

    # -- node interfaces -------------------------------------------------

    def create_tap(self, iface: str) -> None:
        self.sh.run(["ip", "tuntap", "add", "dev", iface, "mode", "tap"])
        self._quiet(iface)
        self.sh.run(["ip", "link", "set", iface, "up"])

    def delete(self, iface: str) -> None:
        if self.exists(iface):
            self.sh.run(["ip", "link", "del", iface], check=False)

    def create_client(
        self, ns: str, ifaces: list[str], address: str | None, gateway: str | None,
        dns: str | None = None,
    ) -> None:
        self.sh.run(["ip", "netns", "add", ns])
        # `ip netns exec` shows this file to the client as its /etc/resolv.conf.
        conf = self.s.netns_etc / ns
        try:
            if dns:
                conf.mkdir(parents=True, exist_ok=True)
                (conf / "resolv.conf").write_text(
                    "".join(f"nameserver {server}\n" for server in dns.split(", ")))
            else:
                shutil.rmtree(conf, ignore_errors=True)
        except OSError:
            pass
        self.sh.run(["ip", "-n", ns, "link", "set", "lo", "up"])
        for i, host_if in enumerate(ifaces):
            # A namespace that is still on its way out keeps its old cable; removing
            # our end of it removes both.
            self.delete(host_if)
            self.sh.run(
                ["ip", "link", "add", host_if, "type", "veth",
                 "peer", "name", f"eth{i}", "netns", ns]
            )
            self._quiet(host_if)
            self.sh.run(["ip", "link", "set", host_if, "up"])
            self.sh.run(["ip", "-n", ns, "link", "set", f"eth{i}", "up"])
        if address:
            self.sh.run(["ip", "-n", ns, "addr", "add", address, "dev", "eth0"])
        if gateway:
            self.sh.run(["ip", "-n", ns, "route", "add", "default", "via", gateway])

    def delete_client(self, ns: str) -> None:
        self.sh.run(["ip", "netns", "del", ns], check=False)
        shutil.rmtree(self.s.netns_etc / ns, ignore_errors=True)

    def client_addr(self, ns: str, port: int, cidr: str) -> None:
        self.sh.run(
            ["ip", "-n", ns, "addr", "replace", cidr, "dev", f"eth{port - 1}"]
        )

    # -- links -----------------------------------------------------------

    def connect(self, a: str, b: str) -> None:
        for src, dst in ((a, b), (b, a)):
            self.sh.run(["tc", "qdisc", "del", "dev", src, "clsact"], check=False)
            self.sh.run(["tc", "qdisc", "add", "dev", src, "clsact"])
            redirect = ["action", "mirred", "egress", "redirect", "dev", dst]
            try:
                self.sh.run(["tc", "filter", "add", "dev", src, "ingress", "matchall", *redirect])
            except CommandError:
                # Some kernels are built without the matchall classifier; a u32 rule
                # that matches every frame does the same job.
                self.sh.run(["tc", "filter", "add", "dev", src, "ingress", "protocol", "all",
                             "u32", "match", "u32", "0", "0", *redirect])

    def disconnect(self, iface: str) -> None:
        if self.exists(iface):
            self.sh.run(["tc", "qdisc", "del", "dev", iface, "clsact"], check=False)

    def attach_mgmt(self, iface: str) -> None:
        self.sh.run(["ip", "link", "set", iface, "master", self.s.mgmt_bridge])

    def detach_mgmt(self, iface: str) -> None:
        if self.exists(iface):
            self.sh.run(["ip", "link", "set", iface, "nomaster"], check=False)

    # -- the Cloud -------------------------------------------------------

    def uplink(self) -> str | None:
        """The physical network card inside the Cloud bridge, if there is one."""
        for port in self._ports(self.s.mgmt_bridge):
            if (self.s.sys_net / port / "device").exists():
                return port
        return None

    def bridged(self) -> bool:
        if self.s.cloud_mode != "auto":
            return self.s.cloud_mode == "bridged"
        return self.uplink() is not None

    def cloud_info(self) -> dict:
        br = self.s.mgmt_bridge
        if not self.bridged():
            return {"mode": "nat", "bridge": br, "uplink": None,
                    "network": self.s.mgmt_net, "gateway": self.s.mgmt_host_ip}
        network = gateway = None
        if not self.s.dry_run:
            try:
                addrs = json.loads(self.sh.run(["ip", "-j", "-4", "addr", "show", "dev", br]))
                for info in (addrs[0].get("addr_info", []) if addrs else []):
                    iface = ipaddress.ip_interface(f"{info['local']}/{info['prefixlen']}")
                    network = str(iface.network)
                    break
                routes = json.loads(self.sh.run(["ip", "-j", "-4", "route", "show", "default"]))
                gateway = routes[0].get("gateway") if routes else None
            except Exception:  # display only; never break the API over this
                pass
        return {"mode": "bridged", "bridge": br, "uplink": self.uplink(),
                "network": network, "gateway": gateway}

    def _adopt_old_bridge(self) -> None:
        """Earlier versions called the bridge mbmgmt; move its ports across."""
        if not self.exists(OLD_BRIDGE) or OLD_BRIDGE == self.s.mgmt_bridge:
            return
        for port in self._ports(OLD_BRIDGE):
            self.sh.run(["ip", "link", "set", port, "master", self.s.mgmt_bridge], check=False)
        self.sh.run(["ip", "link", "del", OLD_BRIDGE], check=False)

    def ensure_mgmt(self) -> None:
        br = self.s.mgmt_bridge
        self._stop_dhcp()  # restarted with current options by reload_dhcp
        if self.bridged():
            # Your own network provides addresses and routing; stay out of the way.
            self.sh.run(["nft", "-f", "-"], input=NFT_CLEAR, check=False)
            self._adopt_old_bridge()
            return
        prefix = self.s.mgmt_net.split("/")[1]
        if not self.exists(br):
            self.sh.run(["ip", "link", "add", br, "type", "bridge"])
            self.sh.run(
                ["ip", "addr", "add", f"{self.s.mgmt_host_ip}/{prefix}", "dev", br]
            )
            self.sh.run(["ip", "link", "set", br, "up"])
        self._adopt_old_bridge()
        self._write("/proc/sys/net/ipv4/ip_forward", "1")
        # Lab devices reach the internet through the VM, and your PC reaches them
        # through it, both source-NATed so no return routes are needed.
        self.sh.run(["nft", "-f", "-"],
                    input=NFT_RULES.format(bridge=br, net=self.s.mgmt_net), check=False)

    def _stop_dhcp(self) -> None:
        if self.s.dry_run:
            return
        pid = pid_alive(self.s.run_dir / "dnsmasq.pid", "dnsmasq")
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    def reload_dhcp(self, hosts: list[str]) -> None:
        """hosts: 'mac,ip,name' lines, one per router port on the Cloud (nat mode)."""
        run = self.s.run_dir
        run.mkdir(parents=True, exist_ok=True)
        hostsfile = run / "dhcp-hosts"
        pidfile = run / "dnsmasq.pid"
        if self.bridged():
            hostsfile.unlink(missing_ok=True)
            return
        hostsfile.write_text("".join(line + "\n" for line in hosts))
        if self.s.dry_run:
            return
        pid = pid_alive(pidfile, "dnsmasq")
        if pid:
            os.kill(pid, signal.SIGHUP)
            return
        net = ipaddress.ip_network(self.s.mgmt_net)
        self.sh.run(
            [
                "dnsmasq",
                "--conf-file=/dev/null",
                f"--interface={self.s.mgmt_bridge}",
                "--bind-interfaces",
                "--except-interface=lo",
                "--port=0",  # DHCP only, no DNS server of our own
                f"--dhcp-range={net.network_address},static,{net.netmask},12h",
                f"--dhcp-option=3,{self.s.mgmt_host_ip}",
                "--dhcp-option=6," + ",".join(host_nameservers()),
                "--dhcp-authoritative",
                f"--dhcp-hostsfile={hostsfile}",
                f"--dhcp-leasefile={run / 'dnsmasq.leases'}",
                f"--pid-file={pidfile}",
            ],
            check=False,
        )

    def scan_addresses(self) -> dict[str, str]:
        """MAC -> IPv4 for devices on the Cloud's network (bridged mode)."""
        found: dict[str, str] = {}
        br = self.s.mgmt_bridge
        pattern = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F:]{17})")
        try:
            info = self.cloud_info()
            net = ipaddress.ip_network(info["network"]) if info.get("network") else None
            # A sweep of anything bigger than a /22 would take too long to be useful.
            if net is not None and net.prefixlen >= 22:
                out = self.sh.run(["arp-scan", "-q", "-x", "-I", br, "--localnet"], check=False)
                for line in out.splitlines():
                    m = pattern.match(line.strip())
                    if m:
                        found[m.group(2).lower()] = m.group(1)
            for entry in json.loads(self.sh.run(["ip", "-j", "-4", "neigh", "show", "dev", br],
                                                check=False) or "[]"):
                if entry.get("lladdr") and entry.get("dst"):
                    found.setdefault(entry["lladdr"].lower(), entry["dst"])
        except Exception:
            pass
        return found
