"""Host networking.

Every node port has one host-side interface: a tap for a router port, the host
end of a veth pair for a client port. A link between two ports is a pair of tc
redirects, so every frame (including STP, LACP and LLDP, which a Linux bridge
would swallow) passes untouched. The management network is a real bridge with
DHCP so routers get a reachable address on first boot.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

from .config import Settings
from .shell import Shell

NFT_RULES = """\
table ip mikrobox
delete table ip mikrobox
table ip mikrobox {{
    chain post {{
        type nat hook postrouting priority 100;
        oifname "{bridge}" masquerade
    }}
}}
"""


def pid_alive(pidfile: Path, must_contain: str) -> int | None:
    """Return the pid in pidfile if that process is alive and looks right."""
    try:
        pid = int(pidfile.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except (OSError, ValueError):
        return None
    return pid if must_contain in cmdline else None


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

    # -- node interfaces -------------------------------------------------

    def create_tap(self, iface: str) -> None:
        self.sh.run(["ip", "tuntap", "add", "dev", iface, "mode", "tap"])
        self._quiet(iface)
        self.sh.run(["ip", "link", "set", iface, "up"])

    def delete(self, iface: str) -> None:
        if self.exists(iface):
            self.sh.run(["ip", "link", "del", iface], check=False)

    def create_client(
        self, ns: str, ifaces: list[str], address: str | None, gateway: str | None
    ) -> None:
        self.sh.run(["ip", "netns", "add", ns])
        self.sh.run(["ip", "-n", ns, "link", "set", "lo", "up"])
        for i, host_if in enumerate(ifaces):
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

    def client_addr(self, ns: str, port: int, cidr: str) -> None:
        self.sh.run(
            ["ip", "-n", ns, "addr", "replace", cidr, "dev", f"eth{port - 1}"]
        )

    # -- links -----------------------------------------------------------

    def connect(self, a: str, b: str) -> None:
        for src, dst in ((a, b), (b, a)):
            self.sh.run(["tc", "qdisc", "del", "dev", src, "clsact"], check=False)
            self.sh.run(["tc", "qdisc", "add", "dev", src, "clsact"])
            self.sh.run(
                ["tc", "filter", "add", "dev", src, "ingress", "matchall",
                 "action", "mirred", "egress", "redirect", "dev", dst]
            )

    def disconnect(self, iface: str) -> None:
        if self.exists(iface):
            self.sh.run(["tc", "qdisc", "del", "dev", iface, "clsact"], check=False)

    def attach_mgmt(self, iface: str) -> None:
        self.sh.run(["ip", "link", "set", iface, "master", self.s.mgmt_bridge])

    def detach_mgmt(self, iface: str) -> None:
        if self.exists(iface):
            self.sh.run(["ip", "link", "set", iface, "nomaster"], check=False)

    # -- management network ---------------------------------------------

    def ensure_mgmt(self) -> None:
        br = self.s.mgmt_bridge
        prefix = self.s.mgmt_net.split("/")[1]
        if not self.exists(br):
            self.sh.run(["ip", "link", "add", br, "type", "bridge"])
            self.sh.run(
                ["ip", "addr", "add", f"{self.s.mgmt_host_ip}/{prefix}", "dev", br]
            )
            self.sh.run(["ip", "link", "set", br, "up"])
        self._write("/proc/sys/net/ipv4/ip_forward", "1")
        # Traffic from your PC is source-NATed onto the bridge, so routers can
        # answer without needing a default route that would pollute the lab.
        self.sh.run(["nft", "-f", "-"], input=NFT_RULES.format(bridge=br), check=False)

    def reload_dhcp(self, hosts: list[str]) -> None:
        """hosts: 'mac,ip,name' lines, one per port on the management bridge."""
        run = self.s.run_dir
        run.mkdir(parents=True, exist_ok=True)
        hostsfile = run / "dhcp-hosts"
        pidfile = run / "dnsmasq.pid"
        hostsfile.write_text("".join(line + "\n" for line in hosts))
        if self.s.dry_run:
            return
        pid = pid_alive(pidfile, "dnsmasq")
        if pid:
            os.kill(pid, signal.SIGHUP)
            return
        import ipaddress

        net = ipaddress.ip_network(self.s.mgmt_net)
        self.sh.run(
            [
                "dnsmasq",
                "--conf-file=/dev/null",
                f"--interface={self.s.mgmt_bridge}",
                "--bind-interfaces",
                "--except-interface=lo",
                "--port=0",  # DHCP only, no DNS
                f"--dhcp-range={net.network_address},static,{net.netmask},12h",
                "--dhcp-option=3",  # hand out no default route
                "--dhcp-authoritative",
                f"--dhcp-hostsfile={hostsfile}",
                f"--dhcp-leasefile={run / 'dnsmasq.leases'}",
                f"--pid-file={pidfile}",
            ],
            check=False,
        )
