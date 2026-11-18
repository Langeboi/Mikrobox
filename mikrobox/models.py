"""The lab model: labs hold nodes (routers, clients) and links between ports."""

from __future__ import annotations

import ipaddress
import re
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"


class NodeKind(str, Enum):
    chr = "chr"
    client = "client"


class PortMap(BaseModel):
    """A published port: host is the port on the PC, container the one inside."""

    host: int = Field(ge=1, le=65535)
    container: int = Field(ge=1, le=65535)
    proto: Literal["tcp", "udp"] = "tcp"


class ContainerSpec(BaseModel):
    """A container saved with the lab, recreated on its PC when missing (see docker.py)."""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
    image: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
    ports: list[PortMap] = Field(default_factory=list, max_length=16)
    env: dict[str, str] = Field(default_factory=dict)
    restart: bool = True  # start again with the PC
    docker_socket: bool = False  # mount the PC's own Docker socket (Portainer and the like)

    @field_validator("env")
    @classmethod
    def _check_env(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key):
                raise ValueError(f"{key!r} is not a valid variable name")
        return v

    @field_validator("ports")
    @classmethod
    def _check_ports(cls, v: list[PortMap]) -> list[PortMap]:
        seen = set()
        for p in v:
            if (p.host, p.proto) in seen:
                raise ValueError(f"port {p.host}/{p.proto} is published twice")
            seen.add((p.host, p.proto))
        return v


class Node(BaseModel):
    id: int
    kind: NodeKind
    # How a CHR is presented. A switch is still a full CHR, just drawn and sized as one.
    role: Literal["router", "switch"] = "router"
    layer: Literal[2, 3] = 2  # switches: layer 2, or layer 3 (routes between its VLANs)
    name: str = Field(pattern=NAME_PATTERN)
    ports: int = Field(ge=1)
    cpus: int = Field(default=1, ge=1, le=8)
    ram_mb: int = Field(default=256, ge=128, le=16384)
    # Clients only: applied to eth0 when the client starts.
    address: Optional[str] = None
    gateway: Optional[str] = None
    dns: Optional[str] = None  # one to three resolver addresses, comma separated
    addressing: Literal["static", "dhcp"] = "static"  # clients: fixed address, or ask DHCP
    services: list[Literal["web", "iperf", "dhcp", "dns"]] = Field(default_factory=list)  # clients only
    docker: bool = False  # clients only: run a Docker engine on this PC
    containers: list[ContainerSpec] = Field(default_factory=list, max_length=32)  # saved with the lab
    # Position on the topology canvas.
    x: float = 0
    y: float = 0

    @field_validator("address")
    @classmethod
    def _check_address(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            ipaddress.ip_interface(v)
            if "/" not in v:
                raise ValueError("address needs a prefix length, e.g. 192.168.10.2/24")
        return v

    @field_validator("dns")
    @classmethod
    def _check_dns(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        servers = [str(ipaddress.ip_address(part)) for part in re.split(r"[,\s]+", v.strip()) if part]
        if len(servers) > 3:
            raise ValueError("at most three DNS servers")
        return ", ".join(servers)

    @field_validator("gateway")
    @classmethod
    def _check_gateway(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            ipaddress.ip_address(v)
        return v


class Endpoint(BaseModel):
    node: int
    port: int = Field(ge=1)


class Link(BaseModel):
    id: int
    a: Endpoint
    # None means "a" is plugged into the management bridge.
    b: Optional[Endpoint] = None

    def touches(self, node_id: int) -> bool:
        return self.a.node == node_id or (self.b is not None and self.b.node == node_id)

    def endpoints(self) -> list[Endpoint]:
        return [self.a] if self.b is None else [self.a, self.b]


class Annotation(BaseModel):
    """A drawing on the canvas: an area shape, a line of text, or a sticky note."""

    id: int
    kind: Literal["rect", "ellipse", "text", "note"]
    x: float
    y: float
    w: float = Field(gt=0, le=20000)
    h: float = Field(gt=0, le=20000)
    text: str = Field(default="", max_length=2000)
    color: Literal["gray", "blue", "green", "amber", "red", "purple"] = "blue"
    # Look of the shape. The defaults are the original appearance.
    fill: Literal["none", "light", "medium", "strong"] = "light"
    border: Literal["dashed", "solid", "dotted", "none"] = "dashed"
    border_width: float = Field(default=1.5, ge=0.5, le=8)
    radius: float = Field(default=12, ge=0, le=80)
    label_pos: Literal["auto", "top-left", "top", "center", "bottom-left", "bottom"] = "auto"
    label_size: Literal["s", "m", "l", "xl"] = "m"


class Lab(BaseModel):
    id: int
    name: str = Field(pattern=NAME_PATTERN)
    nodes: list[Node] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    # Where the management network sits on the canvas.
    mgmt_x: float = 90
    mgmt_y: float = 80

    def node(self, node_id: int) -> Optional[Node]:
        return next((n for n in self.nodes if n.id == node_id), None)


class State(BaseModel):
    labs: list[Lab] = Field(default_factory=list)

    def lab(self, lab_id: int) -> Optional[Lab]:
        return next((lab for lab in self.labs if lab.id == lab_id), None)

    def all_nodes(self) -> list[Node]:
        return [n for lab in self.labs for n in lab.nodes]

    def all_links(self) -> list[Link]:
        return [link for lab in self.labs for link in lab.links]


def free_id(used: set[int]) -> int:
    """Lowest positive integer not in use, so ids (and interface names) stay short."""
    i = 1
    while i in used:
        i += 1
    return i


def default_chr_ram(cpus: int, ports: int) -> int:
    """MikroTik's RouterOS v7 sizing formula, using its lower published baseline."""
    return 256 + 8 * cpus * (ports - 1)
