"""Ready-made lab layouts: devices, cabling, areas and a note with a suggested plan.

A template is groundwork, not a solution. Routers boot unconfigured; the note in
each lab suggests addressing and what to build. By convention ether1 is left free
on every router so it can be linked to the Cloud.
"""

from __future__ import annotations

from typing import Callable

from .models import default_chr_ram


class Build:
    def __init__(self, cloud: tuple[float, float] = (90, 80)) -> None:
        self.nodes: list[dict] = []
        self.links: list[dict] = []
        self.annos: list[dict] = []
        self.used: dict[int, int] = {}
        self.cloud_at = cloud

    def router(self, name: str, x: float, y: float, role: str = "router", ports: int = 4, layer: int = 2) -> int:
        nid = len(self.nodes) + 1
        self.nodes.append({"id": nid, "kind": "chr", "role": role, "layer": layer, "name": name,
                           "ports": ports, "x": x, "y": y})
        self.used[nid] = 1  # ether1 stays free for the Cloud
        return nid

    def switch(self, name: str, x: float, y: float, layer: int = 2, ports: int = 8) -> int:
        return self.router(name, x, y, role="switch", ports=ports, layer=layer)

    def pc(self, name: str, x: float, y: float, address: str, gateway: str) -> int:
        nid = len(self.nodes) + 1
        self.nodes.append({"id": nid, "kind": "client", "name": name, "ports": 1, "ram_mb": 128,
                           "x": x, "y": y, "address": address, "gateway": gateway})
        self.used[nid] = 0
        return nid

    def _port(self, nid: int) -> int:
        self.used[nid] += 1
        return self.used[nid]

    def link(self, a: int, b: int) -> None:
        self.links.append({"id": len(self.links) + 1,
                           "a": {"node": a, "port": self._port(a)},
                           "b": {"node": b, "port": self._port(b)}})

    def to_cloud(self, a: int) -> None:
        self.links.append({"id": len(self.links) + 1, "a": {"node": a, "port": 1}, "b": None})

    def area(self, x: float, y: float, w: float, h: float, text: str, color: str = "blue",
             kind: str = "rect", **style) -> None:
        self.annos.append({"id": len(self.annos) + 1, "kind": kind, "x": x, "y": y, "w": w, "h": h,
                           "text": text, "color": color, **style})

    def note(self, x: float, y: float, w: float, h: float, text: str) -> None:
        self.annos.append({"id": len(self.annos) + 1, "kind": "note", "x": x, "y": y, "w": w, "h": h,
                           "text": text, "color": "amber"})

    def data(self, name: str) -> dict:
        for n in self.nodes:
            if n["kind"] == "chr":
                n["ports"] = max(n["ports"], self.used[n["id"]])
                n["ram_mb"] = default_chr_ram(1, n["ports"])
        return {"format": "mikrobox-lab", "name": name, "nodes": self.nodes, "links": self.links,
                "annotations": self.annos, "mgmt_x": self.cloud_at[0], "mgmt_y": self.cloud_at[1]}


def ospf_multi_area() -> Build:
    b = Build(cloud=(150, 110))
    b.area(320, 60, 480, 310, "Area 0 (backbone)", "blue")
    b.area(120, 240, 360, 470, "Area 1", "green", label_pos="bottom-left")
    b.area(640, 240, 360, 470, "Area 2", "purple", label_pos="bottom-left")
    core = b.router("CORE", 560, 140)
    abr1 = b.router("ABR1", 400, 300)
    abr2 = b.router("ABR2", 720, 300)
    a1r1 = b.router("A1-R1", 200, 440)
    a1r2 = b.router("A1-R2", 400, 520)
    a2r1 = b.router("A2-R1", 920, 440)
    a2r2 = b.router("A2-R2", 720, 520)
    for x, y in ((core, abr1), (core, abr2), (abr1, abr2),
                 (abr1, a1r1), (abr1, a1r2), (a1r1, a1r2),
                 (abr2, a2r1), (abr2, a2r2), (a2r1, a2r2)):
        b.link(x, y)
    b.link(a1r1, b.pc("PC1", 200, 620, "192.168.1.10/24", "192.168.1.1"))
    b.link(a2r1, b.pc("PC2", 920, 620, "192.168.2.10/24", "192.168.2.1"))
    b.note(1040, 60, 300, 390,
           "Multi-area OSPF\n\n"
           "ABR1 and ABR2 sit on the border: one foot in area 0, one in their own area.\n\n"
           "Suggested plan\n"
           "Loopbacks 10.255.0.x/32 as router IDs\n"
           "Router links 10.0.x.0/30\n"
           "PC1 LAN 192.168.1.0/24, PC2 LAN 192.168.2.0/24\n\n"
           "Build\n"
           "1. Address the links and loopbacks\n"
           "2. OSPF in area 0, then areas 1 and 2\n"
           "3. PC1 pings PC2\n"
           "4. Make area 1 a stub, then summarise it at ABR1")
    return b


def collapsed_core() -> Build:
    b = Build(cloud=(760, 120))
    b.area(290, 210, 460, 150, "Core and distribution (collapsed)", "blue")
    b.area(150, 390, 740, 310, "Access", "green", label_pos="bottom-left")
    edge = b.router("EDGE", 520, 120)
    core1 = b.switch("CORE1", 380, 290, layer=3)
    core2 = b.switch("CORE2", 660, 290, layer=3)
    b.to_cloud(edge)
    b.link(edge, core1)
    b.link(edge, core2)
    b.link(core1, core2)
    b.link(core1, core2)
    for i, x in enumerate((240, 520, 800), start=1):
        sw = b.switch(f"SW{i}", x, 460)
        b.link(sw, core1)
        b.link(sw, core2)
        b.link(sw, b.pc(f"PC{i}", x, 610, f"10.10.{i * 10}.10/24", f"10.10.{i * 10}.1"))
    b.note(960, 200, 300, 400,
           "Collapsed core\n\n"
           "Two layer 3 switches do both core and distribution. Every access switch is dual-homed, "
           "and the two links between the cores can be bonded.\n\n"
           "Suggested plan\n"
           "VLAN 10, 20, 30: one per access switch\n"
           "Gateways 10.10.<vlan>.1, shared by the cores with VRRP\n\n"
           "Build\n"
           "1. Bridge the switch ports and trunk the uplinks\n"
           "2. Bond CORE1 to CORE2\n"
           "3. VLAN interfaces and VRRP on the cores\n"
           "4. Spanning tree: make CORE1 the root\n"
           "5. Default route and NAT out through EDGE")
    return b


def spine_leaf() -> Build:
    b = Build(cloud=(90, 110))
    b.area(290, 70, 500, 150, "Spine", "blue")
    b.area(110, 290, 860, 150, "Leaf", "green")
    # Layer 3 switches, as in a data centre: every link between them is routed.
    spines = [b.switch("SPINE1", 380, 150, layer=3, ports=6), b.switch("SPINE2", 700, 150, layer=3, ports=6)]
    for i, x in enumerate((200, 420, 660, 880), start=1):
        leaf = b.switch(f"LEAF{i}", x, 370, layer=3, ports=4)
        for spine in spines:
            b.link(leaf, spine)
        b.link(leaf, b.pc(f"PC{i}", x, 540, f"10.1.{i}.10/24", f"10.1.{i}.1"))
    b.note(1040, 70, 300, 390,
           "Spine and leaf\n\n"
           "Every leaf connects to every spine and nothing else, so any two leaves are "
           "always two hops apart with equal-cost paths.\n\n"
           "Suggested plan\n"
           "Loopbacks 10.255.0.x/32\n"
           "Leaf to spine links 10.0.<spine>.<n>/31\n"
           "PC LANs 10.1.<leaf>.0/24\n\n"
           "Build\n"
           "1. Underlay: OSPF, or eBGP with one AS per leaf\n"
           "2. Check ECMP: two routes to every remote leaf\n"
           "3. Stop SPINE1 and watch traffic move\n"
           "4. Stretch: a VXLAN overlay between two leaves")
    return b


def isp_mpls() -> Build:
    b = Build(cloud=(500, 110))
    b.area(220, 200, 560, 360, "Provider core: MPLS, AS 65000", "blue")
    b.area(40, 60, 160, 270, "Customer A, site 1", "green")
    b.area(800, 60, 160, 270, "Customer A, site 2", "green")
    b.area(40, 440, 160, 270, "Customer B, site 1", "purple")
    b.area(800, 440, 160, 270, "Customer B, site 2", "purple")
    pe1 = b.router("PE1", 300, 380, ports=6)
    pe2 = b.router("PE2", 700, 380, ports=6)
    p1 = b.router("P1", 500, 280)
    p2 = b.router("P2", 500, 480)
    for x, y in ((pe1, p1), (pe1, p2), (p1, p2), (p1, pe2), (p2, pe2)):
        b.link(x, y)
    sites = (("A1", pe1, 120, 260, 120, "10.1.1"), ("A2", pe2, 880, 260, 120, "10.1.2"),
             ("B1", pe1, 120, 500, 640, "10.2.1"), ("B2", pe2, 880, 500, 640, "10.2.2"))
    for tag, pe, x, y, pc_y, net in sites:
        ce = b.router(f"CE-{tag}", x, y)
        b.link(ce, pe)
        b.link(ce, b.pc(f"PC-{tag}", x, pc_y, f"{net}.10/24", f"{net}.1"))
    b.note(1020, 200, 300, 430,
           "ISP with MPLS L3VPN\n\n"
           "P routers only switch labels. PE routers hold one VRF per customer. "
           "Customers A and B never see each other, even if their addresses overlap.\n\n"
           "Suggested plan\n"
           "Core loopbacks 10.255.0.x/32, core links 10.0.x.0/30\n"
           "Customer A 10.1.<site>.0/24, customer B 10.2.<site>.0/24\n\n"
           "Build\n"
           "1. OSPF and LDP in the core\n"
           "2. iBGP between PE1 and PE2 (VPNv4)\n"
           "3. A VRF per customer on each PE\n"
           "4. PE to CE routing: static or eBGP\n"
           "5. PC-A1 pings PC-A2 but not PC-B2")
    return b


def bgp_multihoming() -> Build:
    b = Build(cloud=(520, 60))
    b.area(250, 100, 180, 150, "ISP 1, AS 64501", "amber")
    b.area(610, 100, 180, 150, "ISP 2, AS 64502", "amber")
    b.area(250, 320, 540, 420, "Your network, AS 65010", "green", label_pos="bottom-left")
    isp1 = b.router("ISP1", 340, 180)
    isp2 = b.router("ISP2", 700, 180)
    edge1 = b.router("EDGE1", 340, 400)
    edge2 = b.router("EDGE2", 700, 400)
    core = b.router("CORE", 520, 520)
    for x, y in ((isp1, isp2), (edge1, isp1), (edge2, isp2), (edge1, edge2), (core, edge1), (core, edge2)):
        b.link(x, y)
    b.link(core, b.pc("PC1", 520, 650, "172.16.10.10/24", "172.16.10.1"))
    b.note(860, 300, 300, 400,
           "BGP multihoming\n\n"
           "One network, two upstream providers. The private AS numbers and the "
           "documentation prefixes below are safe to use in a lab.\n\n"
           "Suggested plan\n"
           "Your prefix 203.0.113.0/24\n"
           "ISP links 198.51.100.0/30 and 198.51.100.4/30\n"
           "LAN 172.16.10.0/24\n\n"
           "Build\n"
           "1. eBGP from each EDGE to its ISP\n"
           "2. iBGP between EDGE1 and EDGE2\n"
           "3. Prefer ISP1 outbound with local preference\n"
           "4. Prefer ISP1 inbound with AS path prepending\n"
           "5. Stop ISP1 and check failover")
    return b


TEMPLATES: dict[str, tuple[str, str, Callable[[], Build]]] = {
    "ospf-multi-area": ("Multi-area OSPF", "A backbone with two border routers, each feeding its own area.",
                        ospf_multi_area),
    "collapsed-core": ("Collapsed core", "Two layer 3 core switches, three dual-homed access switches, and an edge "
                       "router to the Cloud.", collapsed_core),
    "spine-leaf": ("Spine and leaf", "Two spines and four leaves, fully meshed between the layers.", spine_leaf),
    "isp-mpls": ("ISP with MPLS", "A provider core with two PE routers and two customers with two sites each.",
                 isp_mpls),
    "bgp-multihoming": ("BGP multihoming", "One network connected to two internet providers.", bgp_multihoming),
}


def describe() -> list[dict]:
    out = []
    for tid, (title, summary, build) in TEMPLATES.items():
        data = build().data(tid)
        routers = [n for n in data["nodes"] if n["kind"] == "chr"]
        out.append({"id": tid, "title": title, "summary": summary, "devices": len(data["nodes"]),
                    "routers": len(routers), "ram_mb": sum(n["ram_mb"] for n in data["nodes"])})
    return out
