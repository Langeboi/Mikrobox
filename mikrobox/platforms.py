"""Virtual machine platforms: how each kind of device is booted and how its ports are named.

MikroTik CHR is the built-in platform. Other platforms are experimental and need
an image the user supplies, because their vendors do not allow redistribution.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .models import Node, NodeKind


@dataclass(frozen=True)
class Platform:
    kind: NodeKind
    label: str
    image_name: str  # file under <data>/images/
    image_format: str  # backing format of that file
    nic: str  # QEMU NIC model
    disk_bus: str  # QEMU drive interface
    default_ports: int
    max_ports: int
    default_cpus: int
    default_ram_mb: int
    min_ram_mb: int
    needs_kvm: bool
    guest_agent: bool
    experimental: bool
    login: str
    image_help: str

    def port_name(self, port: int) -> str:
        if self.kind == NodeKind.aoscx:
            return "mgmt" if port == 1 else f"1/1/{port - 1}"
        return f"ether{port}"

    def image_path(self, settings: Settings) -> Path:
        if self.kind == NodeKind.chr:
            return settings.base_image
        return settings.data_dir / "images" / self.image_name


CHR = Platform(
    kind=NodeKind.chr, label="MikroTik CHR", image_name="chr.img", image_format="raw",
    nic="virtio-net-pci", disk_bus="virtio", default_ports=8, max_ports=24,
    default_cpus=1, default_ram_mb=256, min_ram_mb=128, needs_kvm=False, guest_agent=True,
    experimental=False, login="admin, no password",
    image_help="Installed by scripts/install.sh.",
)

# The AOS-CX Switch Simulator. Its first network card is the out-of-band mgmt port;
# the rest are 1/1/1 onwards. e1000 avoids TCP problems seen with virtio in other labs.
AOSCX = Platform(
    kind=NodeKind.aoscx, label="Aruba AOS-CX", image_name="aoscx.qcow2", image_format="qcow2",
    nic="e1000", disk_bus="ide", default_ports=9, max_ports=17,
    default_cpus=2, default_ram_mb=4096, min_ram_mb=2048, needs_kvm=True, guest_agent=False,
    experimental=True, login="admin, no password (it asks you to set one)",
    image_help=("Download the AOS-CX Switch Simulator (an .ova inside a .zip) from the HPE "
                "Networking Support Portal, free with an HPE account, and upload the .zip, "
                ".ova, .vmdk or a converted .qcow2."),
)

PLATFORMS: dict[NodeKind, Platform] = {CHR.kind: CHR, AOSCX.kind: AOSCX}
VM_KINDS = frozenset(PLATFORMS)


def platform(node_or_kind: Node | NodeKind) -> Platform | None:
    kind = node_or_kind.kind if isinstance(node_or_kind, Node) else node_or_kind
    return PLATFORMS.get(kind)


def is_vm(node: Node) -> bool:
    return node.kind in VM_KINDS


def port_name(node: Node, port: int) -> str:
    p = platform(node)
    return p.port_name(port) if p else f"eth{port - 1}"
