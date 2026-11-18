"""Builds the documentation export: a Markdown write-up, configs and the topology image."""

from __future__ import annotations

import io
import time
import zipfile

from .models import Lab, Node, NodeKind

AREA_KINDS = {"rect": "Area", "ellipse": "Area"}


def port_name(node: Node, port: int) -> str:
    return f"ether{port}" if node.kind == NodeKind.chr else f"eth{port - 1}"


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _rev_time(rev: str) -> str:
    return f"{rev[0:4]}-{rev[4:6]}-{rev[6:8]} {rev[9:11]}:{rev[11:13]} UTC"


def markdown(lab: Lab, rt, backups, has_image: bool) -> str:
    out = [f"# {lab.name}", "",
           f"Exported from Mikrobox on {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}.", ""]
    if has_image:
        out += ["![Topology](topology.png)", ""]

    out += ["## Devices", "",
            "| Name | Type | Ports | vCPUs | RAM (MB) | Address | State |",
            "|---|---|---|---|---|---|---|"]
    for n in lab.nodes:
        kind = (("Linux server" if n.role == "server" else "Linux PC") if n.kind == NodeKind.client else
                ("Layer 3 switch (CHR)" if n.layer == 3 else "Switch (CHR)") if n.role == "switch" else "Router (CHR)")
        address = rt.mgmt_ip(lab, n) or (n.address if n.kind == NodeKind.client else None) or ""
        state = "Running" if rt.node_running(n) else "Stopped"
        out.append(f"| {_cell(n.name)} | {kind} | {n.ports} | {n.cpus} | {n.ram_mb} | {_cell(address)} | {state} |")
    if not lab.nodes:
        out.append("| (none) | | | | | | |")

    out += ["", "## Cabling", "", "| Device | Port | Connected to | Port |", "|---|---|---|---|"]
    for link in lab.links:
        a = lab.node(link.a.node)
        if a is None:
            continue
        if link.b is None:
            out.append(f"| {_cell(a.name)} | {port_name(a, link.a.port)} | Cloud | |")
            continue
        b = lab.node(link.b.node)
        if b is not None:
            out.append(f"| {_cell(a.name)} | {port_name(a, link.a.port)} | {_cell(b.name)} | {port_name(b, link.b.port)} |")
    if not lab.links:
        out.append("| (no links) | | | |")

    clients = [n for n in lab.nodes if n.kind == NodeKind.client
               and (n.address or n.gateway or n.dns or n.addressing == "dhcp" or n.services)]
    if clients:
        names = {"web": "web", "iperf": "iperf3", "dhcp": "DHCP server", "dns": "DNS server"}
        out += ["", "## Clients", "", "| Client | Address | Gateway | DNS | Runs |", "|---|---|---|---|---|"]
        for n in clients:
            dhcp = n.addressing == "dhcp"
            out.append(f"| {_cell(n.name)} | {'DHCP' if dhcp else n.address or ''} | {'' if dhcp else n.gateway or ''} "
                       f"| {'' if dhcp else n.dns or ''} | {', '.join(names[s] for s in n.services)} |")

    areas = [a for a in lab.annotations if a.kind in AREA_KINDS and a.text.strip()]
    if areas:
        out += ["", "## Areas", ""]
        for area in areas:
            inside = [n.name for n in lab.nodes
                      if area.x <= n.x <= area.x + area.w and area.y <= n.y <= area.y + area.h]
            out.append(f"- **{area.text.strip()}**: {', '.join(inside) if inside else 'no devices inside'}")

    notes = [a for a in lab.annotations if a.kind in ("note", "text") and a.text.strip()]
    if notes:
        out += ["", "## Notes", ""]
        for note in notes:
            out += ["> " + line for line in note.text.strip().splitlines()] + [""]

    routers = [n for n in lab.nodes if n.kind == NodeKind.chr]
    if routers:
        out += ["", "## Configurations", "", "| Device | Config | Saved |", "|---|---|---|"]
        for n in routers:
            latest = backups.latest(n.id)
            if latest:
                out.append(f"| {_cell(n.name)} | [configs/{n.name}.txt](configs/{n.name}.txt) | {_rev_time(latest[0])} |")
            else:
                last = (backups.status.get(n.id) or {}).get("message")
                out.append(f"| {_cell(n.name)} | not available | {_cell(last or 'never backed up')} |")
    return "\n".join(out).rstrip() + "\n"


def build_zip(lab: Lab, rt, backups, png: bytes | None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{lab.name}.md", markdown(lab, rt, backups, png is not None))
        if png is not None:
            z.writestr("topology.png", png)
        for n in lab.nodes:
            if n.kind == NodeKind.chr:
                latest = backups.latest(n.id)
                if latest:
                    z.writestr(f"configs/{n.name}.txt", latest[1])
    return buf.getvalue()
