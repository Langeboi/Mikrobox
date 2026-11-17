"""Turns tcpdump's one-line packet summaries into rows for the browser packet view.

This is a best-effort reading of `tcpdump -l -n -e` output: good enough to scan
traffic at a glance. The raw line is always kept, and a pcap download is there
for real analysis in Wireshark.
"""

from __future__ import annotations

import re

FRAME = re.compile(
    r"^(?P<time>\S+) (?P<smac>\S+) > (?P<dmac>\S+), "
    r"(?:ethertype (?P<etype>[\w.]+) \(0x[0-9a-f]+\)|(?P<llc>802\.3)), "
    r"length (?P<len>\d+): (?P<rest>.*)$"
)
VLAN = re.compile(r"^vlan (?P<vlan>\d+), p \d+, ethertype (?P<etype>[\w.]+) \(0x[0-9a-f]+\), (?P<rest>.*)$")
PAIR = re.compile(r"^(?P<src>\S+) > (?P<dst>\S+): (?P<info>.*)$")
ARP_REQ = re.compile(r"who-has (\S+?),? .*tell (\S+?),")
ARP_REPLY = re.compile(r"Reply (\S+) is-at")

# First match wins; checked against the start of the info text.
BY_PREFIX = [
    ("ICMP6", "ICMPv6"), ("ICMP", "ICMP"), ("OSPFv2", "OSPF"), ("OSPFv3", "OSPF"),
    ("VRRP", "VRRP"), ("PIM", "PIM"), ("igmp", "IGMP"), ("IGMP", "IGMP"), ("GRE", "GRE"),
    ("ESP", "ESP"), ("AH", "AH"), ("BFD", "BFD"), ("RIP", "RIP"), ("BOOTP/DHCP", "DHCP"),
    ("NTP", "NTP"), ("isakmp", "IKE"), ("LLDP", "LLDP"), ("STP", "STP"), ("LACP", "LACP"),
    ("Flags [", "TCP"), ("UDP", "UDP"),
]
BY_PORT = {
    "179": "BGP", "22": "SSH", "23": "Telnet", "53": "DNS", "67": "DHCP", "68": "DHCP",
    "80": "HTTP", "443": "HTTPS", "123": "NTP", "161": "SNMP", "500": "IKE", "4500": "IPsec",
    "520": "RIP", "646": "LDP", "1701": "L2TP", "1812": "RADIUS", "3784": "BFD",
    "5678": "MNDP", "8291": "WinBox", "8728": "API", "4789": "VXLAN", "51820": "WireGuard",
}


def _split_port(addr: str, v6: bool) -> tuple[str, str | None]:
    """tcpdump -n appends the port to the address with a dot."""
    host, _, last = addr.rpartition(".")
    if not host or not last.isdigit():
        return addr, None
    if v6 or host.count(".") == 3:
        return host, last
    return addr, None


def _protocol(etype: str, info: str, ports: list[str | None]) -> str:
    for prefix, name in BY_PREFIX:
        if info.startswith(prefix):
            if name in ("TCP", "UDP"):
                for port in ports:
                    if port in BY_PORT:
                        return BY_PORT[port]
            return name
    for port in ports:
        if port in BY_PORT:
            return BY_PORT[port]
    return etype


def parse_line(line: str) -> dict | None:
    line = line.rstrip("\r\n")
    m = FRAME.match(line)
    if not m:
        return None
    etype = m["etype"] or "802.3"
    rest = m["rest"]
    vlan = None
    v = VLAN.match(rest)
    if v:
        vlan, etype, rest = int(v["vlan"]), v["etype"], v["rest"]
    row = {"time": m["time"], "src": m["smac"], "dst": m["dmac"], "proto": etype,
           "length": int(m["len"]), "info": rest, "vlan": vlan, "raw": line}

    if etype in ("IPv4", "IPv6"):
        p = PAIR.match(rest)
        if p:
            src, sport = _split_port(p["src"], etype == "IPv6")
            dst, dport = _split_port(p["dst"], etype == "IPv6")
            row.update(src=src, dst=dst, info=p["info"],
                       proto=_protocol(etype, p["info"], [dport, sport]))
            if sport and dport and row["proto"] not in ("ICMP", "ICMPv6"):
                row["info"] = f"{sport} → {dport}  {p['info']}"
    elif etype == "ARP":
        req, reply = ARP_REQ.search(rest), ARP_REPLY.search(rest)
        if req:
            row.update(src=req.group(2), dst=req.group(1))
        elif reply:
            row.update(src=reply.group(1))
    else:
        row["proto"] = _protocol(etype, rest, [])
        if row["proto"] == "802.3" and "STP" in rest:
            row["proto"] = "STP"
    if vlan is not None:
        row["info"] = f"VLAN {vlan}  {row['info']}"
    return row


# -- full decode --------------------------------------------------------------
#
# The live view runs `tcpdump -xx`, which prints each packet's bytes as hex lines
# under its summary line. The server keeps the bytes of recent packets, and when
# someone opens one it is handed to tshark on its own, so the browser can show the
# same protocol tree and byte view as Wireshark. Each packet is decoded alone: TCP
# streams are not reassembled across packets (download the pcap for that).

import asyncio
import os
import shutil
import struct
import tempfile
import xml.etree.ElementTree as ET


class Assembler:
    """Joins tcpdump -xx output (a summary line, then indented hex lines) into packets."""

    def __init__(self) -> None:
        self.row: dict | None = None
        self.hex: list[str] = []

    def feed(self, line: str) -> list[tuple[dict, bytes]]:
        if line[:1] in (" ", "\t"):
            body = line.strip()
            if self.row is not None and body.startswith("0x") and ":" in body:
                self.hex.append("".join(body.split(":", 1)[1].split()))
            return []
        done = self.flush()
        self.row = parse_line(line)
        return done

    def flush(self) -> list[tuple[dict, bytes]]:
        row, hexes = self.row, self.hex
        self.row, self.hex = None, []
        if row is None:
            return []
        try:
            return [(row, bytes.fromhex("".join(hexes)))]
        except ValueError:
            return [(row, b"")]


def pcap_of(data: bytes, ts: float, length: int | None = None) -> bytes:
    """A one-packet pcap file (Ethernet link type)."""
    head = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1)
    sec = int(ts)
    rec = struct.pack("<IIII", sec, int((ts - sec) * 1_000_000), len(data), max(length or 0, len(data)))
    return head + rec + data


MAX_TREE = 4000  # fields; a normal packet has well under a few hundred


def parse_pdml(text: str) -> list[dict]:
    """tshark -T pdml for one packet -> [{"t": text, "p": byte offset, "s": size, "c": [...]}, ...]"""
    root = ET.fromstring(text)
    packet = root.find("packet")
    if packet is None:
        return []
    count = 0

    def node(el) -> dict | None:
        nonlocal count
        if el.get("hide") == "yes" or count >= MAX_TREE:
            return None
        name, show, showname = el.get("name") or "", el.get("show"), el.get("showname")
        text = showname or (f"{name}: {show}" if name and show is not None else show or name)
        kids = nodes(el)
        if not text:
            return None
        count += 1
        out = {"t": text}
        if name and not name.startswith("_ws."):
            out["n"] = name  # lets the page remember which parts were expanded
        try:
            pos, size = int(el.get("pos", "")), int(el.get("size", ""))
            if size > 0:
                out["p"], out["s"] = pos, size
        except ValueError:
            pass
        if kids:
            out["c"] = kids
        return out

    def nodes(el) -> list[dict]:
        out = []
        for child in el:
            if child.tag not in ("proto", "field"):
                continue
            if child.get("name") == "geninfo":
                continue
            if child.get("name") == "fake-field-wrapper":
                out += nodes(child)
                continue
            n = node(child)
            if n:
                out.append(n)
        return out

    return nodes(packet)


def tshark_argv() -> list[str]:
    argv = ["tshark", "-r", "-", "-n", "-T", "pdml"]
    # Dissectors parse whatever is on the wire; never do that as root.
    if os.geteuid() == 0 and shutil.which("setpriv"):
        argv = ["setpriv", "--reuid=nobody", "--regid=nogroup", "--clear-groups", "--", *argv]
    return argv


async def decode(data: bytes, ts: float, length: int | None = None) -> dict:
    if not data:
        return {"error": "No bytes were captured for this packet."}
    if not shutil.which("tshark"):
        return {"error": "Full decode needs tshark. Run the installer again, or: apt install tshark"}
    # tshark only reads a capture from stdin when it is a pipe or a file, and some event
    # loops (uvloop, under uvicorn) give subprocesses socket pairs. An unlinked temp file
    # works everywhere, and the unprivileged tshark reads it through the inherited descriptor.
    with tempfile.TemporaryFile() as f:
        f.write(pcap_of(data, ts, length))
        f.flush()
        f.seek(0)
        proc = await asyncio.create_subprocess_exec(
            *tshark_argv(), stdin=f, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={**os.environ, "HOME": "/tmp"})
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 15)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": "tshark took too long on this packet."}
    try:
        tree = parse_pdml(out.decode(errors="replace"))
    except ET.ParseError:
        tree = []
    if not tree:
        msg = [l for l in err.decode(errors="replace").splitlines() if l and "dangerous" not in l]
        return {"error": msg[-1] if msg else "tshark could not decode this packet."}
    return {"tree": tree, "hex": data.hex(), "length": len(data)}
