"""The lab model: labs hold nodes (routers, clients) and links between ports."""

from __future__ import annotations

import ipaddress
import re
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"


class NodeKind(str, Enum):
    chr = "chr"
    client = "client"
    aoscx = "aoscx"  # experimental: Aruba AOS-CX Switch Simulator, image supplied by the user


class Node(BaseModel):
    id: int
    kind: NodeKind
    # What the device is used as. kind says which operating system runs it; role says
    # whether it is drawn and sized as a router or a switch. A CHR can be either.
    role: Literal["router", "switch"] = "router"
    name: str = Field(pattern=NAME_PATTERN)
    ports: int = Field(ge=1)
    cpus: int = Field(default=1, ge=1, le=8)
    ram_mb: int = Field(default=256, ge=128, le=16384)
    # Clients only: applied to eth0 when the client starts.
    address: Optional[str] = None
    gateway: Optional[str] = None
    dns: Optional[str] = None  # one to three resolver addresses, comma separated
    # Position on the topology canvas.
    x: float = 0
    y: float = 0

    @model_validator(mode="after")
    def _fixed_roles(self) -> "Node":
        if self.kind == NodeKind.aoscx:
            self.role = "switch"  # AOS-CX only comes as a switch
        return self

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
