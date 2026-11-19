"""Check this server: what Mikrobox needs from the VM, tested where it can be, with fixes.

The quick checks (GET /api/system/check) read files and settings, and make throwaway
interfaces in a scratch namespace to see that links, bonds and VLANs can be made. The
Docker test (POST /api/system/check/docker) is slower: it starts an engine the way a
server does, downloads hello-world from Docker Hub through Mikrobox, and runs it.

Every result is {group, title, status, detail, fix}: status is ok, warn, fail or info,
and fix, when there is one, is a command or a step to take.
"""

from __future__ import annotations

import os
import re
import platform
import shutil
import subprocess
import threading
import time
import types
from pathlib import Path

from .docker import EngineError, PULL_PORT, demux, pid_alive
from .models import NodeKind

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
INSTALL = "sudo ./scripts/install.sh"

# What runs what. Missing required programs stop devices from starting; missing optional
# ones take away the feature named.
REQUIRED = {
    "qemu-system-x86_64": "routers", "qemu-img": "router disks", "ip": "networking", "tc": "links",
    "nft": "the Cloud's NAT", "dnsmasq": "the Cloud's DHCP", "busybox": "PC and server DHCP",
    "nsenter": "Docker on servers", "unshare": "Docker on servers",
}
OPTIONAL = {
    "tcpdump": "packet capture", "tshark": "the packet view's decoding", "iperf3": "throughput tests",
    "dig": "dig in consoles", "mtr": "mtr in consoles", "nmap": "nmap in consoles", "curl": "curl in consoles",
}


def item(group: str, title: str, status: str, detail: str = "", fix: str | None = None) -> dict:
    return {"group": group, "title": title, "status": status, "detail": detail, "fix": fix}


def _run(argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _first_line(text: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")


def cgroup_version() -> str:
    try:
        out = _run(["stat", "-f", "-c", "%T", "/sys/fs/cgroup"]).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return "v2" if out == "cgroup2fs" else "v1" if out == "tmpfs" else out or "unknown"


def ufw_web_ports(status: str, first: int, last: int) -> bool | None:
    """From `ufw status`: None if ufw is off, else whether the link ports are allowed."""
    if "Status: active" not in status:
        return None
    return f"{first}:{last}/tcp" in status or f"{first}:{last}" in status


class SelfCheck:
    def __init__(self, rt, web_ports: tuple[int, int]) -> None:
        self.rt = rt
        self.s = rt.s
        self.web_ports = web_ports
        self.docker_lock = threading.Lock()
        self.gateway = lambda: (None, "", "")  # (port, error, domain): links through a domain

    # -- the quick checks ----------------------------------------------------

    def run(self) -> list[dict]:
        if self.s.dry_run:
            return [item("This VM", "Dry-run mode", INFO,
                         "Mikrobox is only pretending here, so nothing on this machine was checked.")]
        out: list[dict] = []
        for part in (self._vm, self._images, self._programs, self._network, self._probes, self._docker, self._firewall,
                     self._gateway):
            try:
                out += part()
            except Exception as e:  # a check that breaks says so instead of hiding the others
                out.append(item("This VM", f"A check failed to run ({part.__name__.strip('_')})", WARN, str(e)))
        return out

    def _vm(self) -> list[dict]:
        g = "This VM"
        out = []
        kvm = self.s.kvm_device
        if kvm.exists() and os.access(kvm, os.R_OK | os.W_OK):
            out.append(item(g, "Hardware acceleration (KVM)", OK, "Routers run at full speed."))
        else:
            out.append(item(g, "Hardware acceleration (KVM)", FAIL,
                            f"{kvm} is missing, so routers run in software and are very slow.",
                            "Turn on nested virtualisation for this VM: on Proxmox set its CPU type to host; "
                            "on VMware tick 'Expose hardware assisted virtualization to the guest OS'."))
        mem = self.rt.mem_available_mb()
        if mem is not None:
            room = max(0, (mem - self.s.mem_reserve_mb) // 256)
            status = WARN if room < 4 else OK
            out.append(item(g, "Free memory", status,
                            f"{mem / 1024:.1f} GB free: room for about {room} more routers at 256 MB each "
                            f"({self.s.mem_reserve_mb} MB is kept for the VM itself)."))
        try:
            self.s.data_dir.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.s.data_dir).free / 1024 ** 3
            status = FAIL if free < 2 else WARN if free < 10 else OK
            out.append(item(g, "Free disk space", status, f"{free:.1f} GB free for router disks, images and Docker in {self.s.data_dir}.",
                            None if status == OK else "Give the VM a bigger disk, or delete labs and Docker images you no longer need."))
        except OSError as e:
            out.append(item(g, "Free disk space", WARN, str(e)))
        out.append(item(g, "System", INFO, f"Linux {platform.release()}, cgroup {cgroup_version()}."))
        return out

    def _images(self) -> list[dict]:
        g = "Routers and switches"
        if self.s.base_image.exists():
            version = self.rt._chr_version()
            out = [item(g, "RouterOS CHR image", OK, f"Version {version}." if version else "Installed.")]
        else:
            out = [item(g, "RouterOS CHR image", FAIL, f"{self.s.base_image} is missing, so routers can't start.", INSTALL)]
        # The test build's other operating systems: optional, added by hand.
        from .platforms import PLATFORMS
        for p in PLATFORMS.values():
            if p.kind == NodeKind.chr:
                continue
            info = self.rt.platform_info(p)
            if info["image"]:
                out.append(item(g, f"{p.label} image", OK, f"Version {info['version']}." if info["version"] else "Added."))
            else:
                out.append(item(g, f"{p.label} image", INFO, "Not added. Settings › Images takes the file from HPE."))
            if p.needs_kvm and not self.rt.kvm_available():
                out.append(item(g, f"{p.label} needs KVM", WARN, f"{p.label} devices won't start without hardware acceleration."))
        return out

    def _programs(self) -> list[dict]:
        g = "Programs"
        missing = [f"{p} ({why})" for p, why in REQUIRED.items() if not shutil.which(p)]
        gone = [f"{p} ({why})" for p, why in OPTIONAL.items() if not shutil.which(p)]
        out = [item(g, "Required programs", FAIL if missing else OK,
                    ("Missing: " + ", ".join(missing) + ".") if missing else "All there.", INSTALL if missing else None)]
        if gone:
            out.append(item(g, "Optional programs", WARN, "Missing: " + ", ".join(gone) + ".", INSTALL))
        return out

    def _network(self) -> list[dict]:
        g = "Network"
        out = []
        bridge = Path("/sys/class/net") / self.s.mgmt_bridge
        out.append(item(g, "The Cloud", OK if bridge.exists() else FAIL,
                        f"Bridge {self.s.mgmt_bridge}, {self.rt.net.cloud_info().get('network')}."
                        if bridge.exists() else f"The bridge {self.s.mgmt_bridge} is missing.",
                        None if bridge.exists() else "sudo systemctl restart mikrobox"))
        try:
            forwarding = Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() == "1"
        except OSError:
            forwarding = False
        if not self.rt.net.bridged():
            out.append(item(g, "IP forwarding", OK if forwarding else FAIL,
                            "On: the Cloud's NAT can pass traffic." if forwarding
                            else "Off: devices on the Cloud can't reach the internet.",
                            None if forwarding else "sudo sysctl -w net.ipv4.ip_forward=1"))
        # Docker on the VM itself sets the firewall to drop forwarded traffic, the Cloud's included.
        if shutil.which("iptables"):
            try:
                rules = _run(["iptables", "-S", "FORWARD"]).stdout
            except (OSError, subprocess.SubprocessError):
                rules = ""
            if "-P FORWARD DROP" in rules and not any(self.s.mgmt_bridge in line and "ACCEPT" in line for line in rules.splitlines()):
                out.append(item(g, "Forwarded traffic", WARN,
                                "The VM's firewall drops forwarded traffic (Docker on the VM does this), "
                                "which stops the Cloud's internet.",
                                f"sudo iptables -I FORWARD -i {self.s.mgmt_bridge} -j ACCEPT && "
                                f"sudo iptables -I FORWARD -o {self.s.mgmt_bridge} -j ACCEPT"))
        if Path("/sys/class/net/bond0").exists():
            out.append(item(g, "A bond0 on the VM itself", INFO,
                            "Made when the bonding module was loaded without the installer's setting. Harmless, "
                            "and gone after a reboot.", "sudo ip link del bond0"))
        return out

    def _probes(self) -> list[dict]:
        """Make the kinds of interfaces devices use, in a scratch namespace, and see what works."""
        g = "Network"
        ns = f"mbcheck{os.getpid()}"
        out = []
        try:
            _run(["ip", "netns", "add", ns])
            made = _run(["ip", "-n", ns, "link", "add", "c0", "type", "veth", "peer", "name", "c1"])
            if made.returncode:
                return [item(g, "Network namespaces", FAIL, _first_line(made.stderr), INSTALL)]
            _run(["tc", "-n", ns, "qdisc", "add", "dev", "c0", "ingress"])
            redirect = ["action", "mirred", "egress", "redirect", "dev", "c1"]
            if _run(["tc", "-n", ns, "filter", "add", "dev", "c0", "ingress", "matchall", *redirect]).returncode == 0:
                out.append(item(g, "Links between devices", OK, "Made with tc matchall and mirred."))
            elif _run(["tc", "-n", ns, "filter", "add", "dev", "c0", "ingress", "protocol", "all", "u32",
                       "match", "u32", "0", "0", *redirect]).returncode == 0:
                out.append(item(g, "Links between devices", OK, "Made with tc u32 and mirred (this kernel has no matchall)."))
            else:
                out.append(item(g, "Links between devices", FAIL,
                                "tc can't redirect traffic between interfaces, so links won't pass anything.",
                                "Install the full kernel modules: sudo apt install linux-modules-extra-$(uname -r)"))
            bond = _run(["ip", "-n", ns, "link", "add", "b0", "type", "bond", "mode", "802.3ad"])
            out.append(item(g, "Bonding (servers)", OK if bond.returncode == 0 else FAIL,
                            "Servers can bond their NICs." if bond.returncode == 0
                            else f"Servers can't bond their NICs ({_first_line(bond.stderr)}). Running the installer "
                                 "again sets this up for good; this loads it now:",
                            None if bond.returncode == 0 else "sudo modprobe bonding max_bonds=0"))
            vlan = _run(["ip", "-n", ns, "link", "add", "link", "c0", "name", "c0.10", "type", "vlan", "id", "10"])
            out.append(item(g, "VLANs (PCs and servers)", OK if vlan.returncode == 0 else FAIL,
                            "PCs and servers can tag VLANs." if vlan.returncode == 0
                            else f"PCs and servers can't tag VLANs ({_first_line(vlan.stderr)}). Running the installer "
                                 "again sets this up for good; this loads it now:",
                            None if vlan.returncode == 0 else "sudo modprobe 8021q"))
        finally:
            _run(["ip", "netns", "del", ns])
        return out

    def _docker(self) -> list[dict]:
        g = "Docker on servers"
        if not shutil.which("dockerd"):
            return [item(g, "Docker", WARN, "Not installed, so servers can't run containers.", INSTALL)]
        try:
            version = _first_line(_run(["dockerd", "--version"]).stdout)
        except (OSError, subprocess.SubprocessError):
            version = "installed"
        missing = [p for p in ("containerd", "runc", "docker") if not shutil.which(p)]
        if missing:
            return [item(g, "Docker", FAIL, f"{version}, but {', '.join(missing)} is missing.", INSTALL)]
        running = _run(["systemctl", "is-active", "docker"]).stdout.strip() == "active" if shutil.which("systemctl") else False
        out = [item(g, "Docker", OK, f"{version}. Run the Docker test to see a server start an engine, "
                                     "download an image and run it.")]
        if running:
            out.append(item(g, "Docker on the VM itself", INFO,
                            "A Docker engine also runs on the VM. Mikrobox doesn't need it; servers run their own."))
        return out

    def _firewall(self) -> list[dict]:
        g = "Network"
        first, last = self.web_ports
        if not shutil.which("ufw"):
            return []
        try:
            status = _run(["ufw", "status"]).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        allowed = ufw_web_ports(status, first, last)
        if allowed is None:
            return []
        return [item(g, "Firewall (ufw) and web links", OK if allowed else WARN,
                     f"Ports {first}-{last}, used by links to lab web pages, are "
                     + ("allowed." if allowed else "blocked, so those links won't open."),
                     None if allowed else f"sudo ufw allow {first}:{last}/tcp")]

    def _gateway(self) -> list[dict]:
        """Links through your domain: the port the reverse proxy sends *.<domain> to."""
        port, error, domain = self.gateway()
        if not domain:
            return []
        g, title = "Network", f"Links through {domain}"
        if port is None:
            return [item(g, title, FAIL, error or "The gateway port is off (MIKROBOX_GATEWAY_PORT).",
                         "Free the port, or set another with MIKROBOX_GATEWAY_PORT, and restart Mikrobox.")]
        out = [item(g, title, OK, f"Port {port} takes *.{domain} from your reverse proxy.")]
        if shutil.which("ufw"):
            try:
                status = _run(["ufw", "status"]).stdout
            except (OSError, subprocess.SubprocessError):
                status = ""
            if "Status: active" in status and not re.search(rf"(?m)^{port}(/tcp)?\s+ALLOW", status):
                out.append(item(g, f"Firewall (ufw) and port {port}", WARN,
                                f"Your reverse proxy can't reach port {port}, so links through {domain} won't open.",
                                f"sudo ufw allow {port}/tcp"))
        return out

    # -- the Docker test -----------------------------------------------------

    def docker_test(self, image: str = "hello-world") -> dict:
        """Start an engine like a server does, pull hello-world and run it. Takes up to a
        couple of minutes; one at a time."""
        if self.s.dry_run:
            return {"ok": False, "steps": [item("Docker test", "Dry-run mode", INFO, "Nothing is started here.")]}
        d = self.rt.docker
        if not d.available:
            return {"ok": False, "steps": [item("Docker test", "Docker", FAIL, "Docker isn't installed.", INSTALL)]}
        if not self.docker_lock.acquire(blocking=False):
            return {"ok": False, "steps": [item("Docker test", "Already running", INFO, "A Docker test is running already.")]}
        node = types.SimpleNamespace(id=0, kind=NodeKind.client, docker=True, name="check")
        ns = self.rt.netns(node.id)
        steps: list[dict] = []
        g = "Docker test"
        try:
            self._clean_test(node, ns)
            _run(["ip", "netns", "add", ns])
            _run(["ip", "-n", ns, "link", "set", "lo", "up"])
            d.root().mkdir(parents=True, exist_ok=True)
            d.data_root(node).mkdir(parents=True, exist_ok=True)
            d._open_proxy(node)
            env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
            env.update(HTTPS_PROXY=f"http://127.0.0.1:{PULL_PORT}", HTTP_PROXY=f"http://127.0.0.1:{PULL_PORT}")
            log_path = d.root() / "n0.log"
            with log_path.open("wb") as log:
                subprocess.Popen(d.command(node), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 env=env, start_new_session=True, close_fds=True)
            eng = d.engine(node)
            t0 = time.time()
            while time.time() - t0 < 60 and not eng.ping():
                time.sleep(0.5)
            if not eng.ping():
                tail = [line for line in log_path.read_text(errors="replace").splitlines() if line.strip()][-4:]
                steps.append(item(g, "Start an engine inside a server", FAIL,
                                  "The engine did not start. " + " ".join(tail)[-500:],
                                  "Send this to whoever helps you with Mikrobox; it names what Docker missed."))
                return {"ok": False, "steps": steps}
            version = (eng.call("GET", "/version") or {}).get("Version", "")
            steps.append(item(g, "Start an engine inside a server", OK, f"Docker {version} started in {time.time() - t0:.0f} s."))
            t0 = time.time()
            try:
                d.pull(node, image, ("check", image))
            except EngineError as e:
                steps.append(item(g, f"Download {image} from Docker Hub", FAIL, str(e),
                                  "Check that the VM itself can reach registry-1.docker.io on port 443."))
                return {"ok": False, "steps": steps}
            finally:
                d.progress.pop(("check", image), None)
            steps.append(item(g, f"Download {image} from Docker Hub", OK, f"Took {time.time() - t0:.0f} s, through Mikrobox."))
            steps.append(run_hello(eng, image))
            return {"ok": all(s["status"] == OK for s in steps), "steps": steps}
        except (OSError, subprocess.SubprocessError, EngineError) as e:
            steps.append(item(g, "The test itself", FAIL, str(e)))
            return {"ok": False, "steps": steps}
        finally:
            self._clean_test(node, ns)
            self.docker_lock.release()

    def _clean_test(self, node, ns: str) -> None:
        d = self.rt.docker
        if pid_alive(d.pidfile(node), "dockerd") or d.sock(node).exists():
            d.stop(node)
        d._close_proxy(node)
        shutil.rmtree(d.data_root(node), ignore_errors=True)
        shutil.rmtree(d.root() / "n0", ignore_errors=True)
        try:
            (d.root() / "n0.cg").rmdir()  # where the engine's launcher parked cgroups
        except OSError:
            pass
        _run(["ip", "netns", "del", ns])


def run_hello(eng, image: str) -> dict:
    """Run the image to the end and check what it said."""
    g = "Docker test"
    title = f"Run {image}"
    try:
        made = eng.call("POST", "/containers/create", {"Image": image})
        cid = made["Id"]
        eng.call("POST", f"/containers/{cid}/start")
        state = {}
        for _ in range(60):
            state = (eng.call("GET", f"/containers/{cid}/json") or {}).get("State", {})
            if not state.get("Running"):
                break
            time.sleep(0.5)
        raw = eng.call("GET", f"/containers/{cid}/logs?stdout=1&stderr=1")
        text = demux(raw) if isinstance(raw, bytes) else str(raw or "")
        eng.call("DELETE", f"/containers/{cid}?force=1")
    except (EngineError, KeyError, OSError) as e:
        return item(g, title, FAIL, str(e))
    if state.get("ExitCode") == 0 and ("Hello from Docker" in text or image != "hello-world"):
        return item(g, title, OK, "It ran and said hello: containers work on this VM.")
    detail = state.get("Error") or _first_line(text) or f"It stopped with code {state.get('ExitCode')}."
    return item(g, title, FAIL, detail, "Send this to whoever helps you with Mikrobox; cgroups are the usual suspect.")
