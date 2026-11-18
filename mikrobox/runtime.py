"""Starts and stops nodes, applies links, and enforces the device and memory limits."""

from __future__ import annotations

import ipaddress
import os
import shutil
import signal
import threading
import time
from pathlib import Path

from . import __version__
from .config import Settings
from .models import Endpoint, Lab, Link, Node, NodeKind
from .clients import ClientServices
from .docker import Docker
from .network import Net, pid_alive
from .shell import CommandError, Shell
from .store import Store


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Invalid(Exception):
    pass


def iface_name(node_id: int, port: int) -> str:
    return f"mb{node_id}p{port}"


def netns_name(node_id: int) -> str:
    return f"mbc{node_id}"


def mac_for(node_id: int, port: int) -> str:
    return f"52:54:00:{(node_id >> 8) & 0xFF:02x}:{node_id & 0xFF:02x}:{port & 0xFF:02x}"


class Runtime:
    def __init__(self, settings: Settings, store: Store, sh: Shell | None = None) -> None:
        self.s = settings
        self.store = store
        self.sh = sh or Shell(dry_run=settings.dry_run)
        self.net = Net(settings, self.sh)
        self.clients = ClientServices(self)
        self.docker = Docker(self)
        self.stop_hooks: list = []  # called with the node after it stops (web links close)
        self.lock = threading.RLock()
        self.cloud_ips: dict[str, str] = {}  # MAC -> address, discovered in bridged mode
        self._scanner: threading.Thread | None = None

    # -- host names ------------------------------------------------------

    def iface(self, node_id: int, port: int) -> str:
        return iface_name(node_id, port)

    def netns(self, node_id: int) -> str:
        return netns_name(node_id)

    # -- paths -----------------------------------------------------------

    def _run(self, node: Node, suffix: str) -> Path:
        return self.s.run_dir / f"n{node.id}.{suffix}"

    def serial_socket(self, node: Node) -> Path:
        return self._run(node, "serial")

    def agent_socket(self, node: Node) -> Path:
        """QEMU guest agent channel; present for routers started since it was added."""
        return self._run(node, "qga")

    def disk_path(self, node: Node) -> Path:
        return self.s.data_dir / "disks" / f"n{node.id}.qcow2"

    # -- facts -----------------------------------------------------------

    def kvm_available(self) -> bool:
        return self.s.kvm_device.exists()

    def mem_available_mb(self) -> int | None:
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
        except OSError:
            pass
        return None

    def node_running(self, node: Node) -> bool:
        if node.kind == NodeKind.chr:
            return pid_alive(self._run(node, "pid"), "qemu") is not None
        return (self.s.netns_dir / netns_name(node.id)).exists()

    def running_count(self) -> int:
        return sum(1 for n in self.store.state.all_nodes() if self.node_running(n))

    def mgmt_ip(self, lab: Lab, node: Node) -> str | None:
        """Address the node gets on the management bridge, if it is plugged in."""
        port = self.cloud_port(lab, node)
        if port is None:
            return None
        if self.net.bridged():
            # Your own network hands out the address; we only know it once it is seen.
            return self.cloud_ips.get(mac_for(node.id, port))
        return str(ipaddress.ip_address(self.s.mgmt_first_ip) + node.id)

    def cloud_port(self, lab: Lab, node: Node) -> int | None:
        """The node's port that is linked to the Cloud, if any."""
        return next((l.a.port for l in lab.links if l.b is None and l.a.node == node.id), None)

    def system_info(self) -> dict:
        return {
            "version": __version__,
            "max_devices": self.s.max_devices,
            "running_devices": self.running_count(),
            "kvm": self.kvm_available(),
            "chr_image": self.s.base_image.exists(),
            "mem_available_mb": self.mem_available_mb(),
            "cloud": (cloud := self.net.cloud_info()),
            "mgmt_network": cloud["network"],
            "mgmt_host_ip": cloud["gateway"],
            "dry_run": self.s.dry_run,
            "chr_version": self._chr_version(),
            "max_chr_ports": self.s.max_chr_ports,
            "docker": self.docker.available,
        }

    def _chr_version(self) -> str | None:
        try:
            return (self.s.base_image.parent / "chr.version").read_text().strip() or None
        except OSError:
            return None

    # -- host setup ------------------------------------------------------

    def ensure_host(self) -> None:
        self.s.run_dir.mkdir(parents=True, exist_ok=True)
        (self.s.data_dir / "disks").mkdir(parents=True, exist_ok=True)
        self.net.ensure_mgmt()
        self.sync_dhcp()
        if self.net.bridged() and not self.s.dry_run and self._scanner is None:
            self._scanner = threading.Thread(target=self._scan_loop, daemon=True)
            self._scanner.start()

    def _scan_loop(self) -> None:
        """Bridged mode: learn which address each Cloud-linked device ended up with."""
        while True:
            try:
                wanted = any(
                    self.cloud_port(lab, n) is not None and self.node_running(n)
                    for lab in self.store.state.labs for n in lab.nodes
                )
                if wanted:
                    self.cloud_ips = self.net.scan_addresses()
            except Exception:
                pass
            time.sleep(15)

    def sync_dhcp(self) -> None:
        hosts = []
        for lab in self.store.state.labs:
            for link in lab.links:
                if link.b is not None:
                    continue
                node = lab.node(link.a.node)
                if node is None or node.kind != NodeKind.chr:
                    continue
                hosts.append(
                    f"{mac_for(node.id, link.a.port)},{self.mgmt_ip(lab, node)},{node.name}"
                )
        self.net.reload_dhcp(hosts)

    # -- qemu ------------------------------------------------------------

    def qemu_command(self, node: Node) -> list[str]:
        kvm = self.kvm_available()
        cmd = [
            self.s.qemu_binary,
            "-name", f"mikrobox-{node.id}-{node.name}",
            "-machine", "accel=kvm" if kvm else "accel=tcg",
            "-cpu", "host" if kvm else "max",
            "-smp", str(node.cpus),
            "-m", str(node.ram_mb),
            "-display", "none",
            "-daemonize",
            "-pidfile", str(self._run(node, "pid")),
            "-drive", f"file={self.disk_path(node)},if=virtio,format=qcow2",
            "-serial", f"unix:{self.serial_socket(node)},server=on,wait=off",
            "-monitor", f"unix:{self._run(node, 'monitor')},server=on,wait=off",
            # Guest agent: lets Mikrobox read the config without any network path.
            "-device", "virtio-serial-pci",
            "-chardev", f"socket,id=qga0,path={self.agent_socket(node)},server=on,wait=off",
            "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
        ]
        for port in range(1, node.ports + 1):
            cmd += [
                "-netdev",
                f"tap,id=p{port},ifname={iface_name(node.id, port)},script=no,downscript=no",
                "-device",
                f"virtio-net-pci,netdev=p{port},mac={mac_for(node.id, port)}",
            ]
        return cmd

    def _ensure_disk(self, node: Node) -> None:
        disk = self.disk_path(node)
        if disk.exists():
            return
        disk.parent.mkdir(parents=True, exist_ok=True)
        self.sh.run(
            ["qemu-img", "create", "-f", "qcow2", "-F", "raw",
             "-b", str(self.s.base_image), str(disk)]
        )

    # -- start / stop ----------------------------------------------------

    def start_node(self, lab: Lab, node: Node) -> None:
        with self.lock:
            if self.node_running(node):
                return
            if self.running_count() >= self.s.max_devices:
                raise Conflict(f"Device limit reached ({self.s.max_devices} running).")
            if node.kind == NodeKind.chr:
                self._start_chr(node)
            else:
                self.clients.stop(node)  # leftovers from a crash
                self.net.delete_client(netns_name(node.id))
                static = node.addressing == "static"
                self.net.create_client(
                    netns_name(node.id),
                    [iface_name(node.id, p) for p in range(1, node.ports + 1)],
                    node.address if static else None,
                    node.gateway if static else None,
                    node.dns if static else None,
                )
                self.clients.start(lab, node)
                self.docker.start(lab, node)
            for link in lab.links:
                if link.touches(node.id):
                    self.apply_link(lab, link)

    def _start_chr(self, node: Node) -> None:
        if not self.s.dry_run and not self.s.base_image.exists():
            raise Conflict(f"CHR image not found at {self.s.base_image}. Run scripts/install.sh.")
        avail = self.mem_available_mb()
        if avail is not None and avail < node.ram_mb + self.s.mem_reserve_mb:
            raise Conflict(
                f"Not enough free memory: {node.name} needs {node.ram_mb} MB, "
                f"{avail} MB available and {self.s.mem_reserve_mb} MB is kept in reserve."
            )
        self._ensure_disk(node)
        ifaces = [iface_name(node.id, p) for p in range(1, node.ports + 1)]
        for iface in ifaces:
            self.net.delete(iface)  # leftovers from a crash
            self.net.create_tap(iface)
        self.serial_socket(node).unlink(missing_ok=True)
        self.agent_socket(node).unlink(missing_ok=True)
        try:
            self.sh.run(self.qemu_command(node))
        except CommandError:
            for iface in ifaces:
                self.net.delete(iface)
            raise

    def stop_node(self, lab: Lab, node: Node) -> None:
        with self.lock:
            for link in lab.links:
                if link.touches(node.id) and link.b is not None:
                    other = link.b if link.a.node == node.id else link.a
                    self.net.disconnect(iface_name(other.node, other.port))
            if node.kind == NodeKind.chr:
                self._kill(self._run(node, "pid"))
                for suffix in ("pid", "serial", "monitor", "qga"):
                    self._run(node, suffix).unlink(missing_ok=True)
                for port in range(1, node.ports + 1):
                    self.net.delete(iface_name(node.id, port))
            else:
                self.docker.stop(node)  # stops its containers first
                self.clients.stop(node)
                self.net.delete_client(netns_name(node.id))
        for hook in self.stop_hooks:
            hook(node)

    def _kill(self, pidfile: Path) -> None:
        pid = pid_alive(pidfile, "qemu")
        if not pid:
            return
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if pid_alive(pidfile, "qemu") is None:
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def start_lab(self, lab: Lab) -> None:
        for node in lab.nodes:
            self.start_node(lab, node)

    def stop_lab(self, lab: Lab) -> None:
        for node in lab.nodes:
            self.stop_node(lab, node)

    # -- links -----------------------------------------------------------

    def apply_link(self, lab: Lab, link: Link) -> None:
        a = iface_name(link.a.node, link.a.port)
        if link.b is None:
            if not self.net.exists(a):
                return
            self.net.attach_mgmt(a)
            node = lab.node(link.a.node)
            if node is not None and node.kind == NodeKind.client and not self.net.bridged():
                prefix = self.s.mgmt_net.split("/")[1]
                self.net.client_addr(
                    netns_name(node.id), link.a.port, f"{self.mgmt_ip(lab, node)}/{prefix}"
                )
            return
        b = iface_name(link.b.node, link.b.port)
        if self.net.exists(a) and self.net.exists(b):
            self.net.connect(a, b)

    def remove_link(self, link: Link) -> None:
        a = iface_name(link.a.node, link.a.port)
        if link.b is None:
            self.net.detach_mgmt(a)
            return
        self.net.disconnect(a)
        self.net.disconnect(iface_name(link.b.node, link.b.port))

    def check_endpoint(self, lab: Lab, ep: Endpoint) -> None:
        node = lab.node(ep.node)
        if node is None:
            raise NotFound(f"Node {ep.node} is not in this lab.")
        if ep.port > node.ports:
            raise Invalid(f"{node.name} has {node.ports} ports; port {ep.port} does not exist.")
        for link in lab.links:
            if any(e.node == ep.node and e.port == ep.port for e in link.endpoints()):
                raise Conflict(f"{node.name} port {ep.port} is already connected.")

    def copy_disk(self, src: Node, dst: Node) -> bool:
        """Give dst a copy of src's disk (its saved configuration). False if src has none."""
        if not self.disk_path(src).exists():
            return False
        shutil.copyfile(self.disk_path(src), self.disk_path(dst))
        return True

    def delete_disk(self, node: Node) -> None:
        """A device is deleted, reset or replaced: its disk goes, and a PC's Docker data too."""
        self.disk_path(node).unlink(missing_ok=True)
        if node.kind == NodeKind.client:
            self.docker.forget(node)

    def resume(self) -> None:
        """After Mikrobox itself restarts: reconnect to what kept running."""
        for lab in self.store.state.labs:
            for node in lab.nodes:
                if node.kind == NodeKind.client and node.docker and self.node_running(node):
                    self.docker.resume(lab, node)
