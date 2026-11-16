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
