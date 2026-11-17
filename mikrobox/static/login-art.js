"use strict";
/* The sign-in page's left half: example labs drawn like the workspace. Each one boots,
   carries traffic and hits the odd failure for a minute, then the next one takes over,
   in random order. Decoration only: the whole panel is aria-hidden.

   A lab is plain data in a 640 x 460 space (the SVG's viewBox):
     nodes  id: { x, y, k: "r" router | "s" switch | "c" client | "cloud", sub, name, late }
     links  [a, b, { label, tunnel, curve, mpls, bond, faint, blocked }]
     flows  traffic: { paths: [first choice, fallback, ...], every: [min, max] s, color, turn, color2 }
     log    lines printed every few seconds: [device, topic, text]
     areas  { x, y, w, h, label, color, at: label corner "tl" (default), "tr", "bl" or "br" }
     prompt the router whose console prompt ends the log (default: the first router)
     events { at: seconds, down | up | block | unblock | show | pulse: "node:X" or "link:A-B",
              sub: { node: text }, labels: { "A-B": text }, log: [[device, topic, text], ...] } */

(() => {
  const SVGNS = "http://www.w3.org/2000/svg";
  const SCENE = 60;   // seconds per lab
  const SPEED = 150;  // packets, viewBox units per second

  const ICONS = {
    r: "M2 12a10 10 0 1 0 20 0a10 10 0 1 0-20 0 M6 12h12 M12 6v12 M6 12l2.6-2.6 M6 12l2.6 2.6 M18 12l-2.6-2.6 M18 12l-2.6 2.6 M12 6l-2.6 2.6 M12 6l2.6 2.6 M12 18l-2.6-2.6 M12 18l2.6-2.6",
    c: "M4 5h16v11H4z M9 20h6 M12 16v4",
    s: "M3 8.5h18 M17 5l4 3.5-4 3.5 M21 15.5H3 M7 12l-4 3.5 4 3.5",
  };
  const CLOUD = "M-34 26a20 20 0 0 1-5-39a27 27 0 0 1 51-9a23 23 0 0 1 27 48z";

  const LABS = [
    {
      id: "ospf", name: "Multi-area OSPF",
      blurb: "A backbone area with two areas hanging off it through area border routers.",
      areas: [
        { x: 150, y: 8, w: 340, h: 178, label: "Area 0 · backbone", color: "blue" },
        { x: 16, y: 216, w: 290, h: 238, label: "Area 1", color: "green", at: "bl" },
        { x: 334, y: 216, w: 290, h: 238, label: "Area 2 · stub", color: "purple", at: "br" },
      ],
      nodes: {
        CORE: { x: 320, y: 88, k: "r", sub: "1.1.1.1" }, ABR1: { x: 210, y: 196, k: "r", sub: "2.2.2.2" },
        ABR2: { x: 430, y: 196, k: "r", sub: "3.3.3.3" }, R1: { x: 96, y: 318, k: "r", sub: "4.4.4.4" },
        PC1: { x: 232, y: 384, k: "c", sub: "10.1.0.10" }, R2: { x: 544, y: 318, k: "r", sub: "5.5.5.5" },
        PC2: { x: 408, y: 384, k: "c", sub: "10.2.0.10" },
      },
      links: [["CORE", "ABR1"], ["CORE", "ABR2"], ["ABR1", "ABR2"], ["ABR1", "R1"], ["R1", "PC1"], ["ABR2", "R2"], ["R2", "PC2"]],
      flows: [
        { paths: [["PC1", "R1", "ABR1", "ABR2", "R2", "PC2"], ["PC1", "R1", "ABR1", "CORE", "ABR2", "R2", "PC2"]] },
        { paths: [["PC2", "R2", "ABR2", "ABR1", "R1", "PC1"], ["PC2", "R2", "ABR2", "CORE", "ABR1", "R1", "PC1"]] },
      ],
      log: [
        ["CORE", "ospf", "neighbor 2.2.2.2 on ether1 changed to Full"],
        ["ABR1", "ospf", "summary-LSA 10.1.0.0/16 originated into area 0"],
        ["R2", "ospf", "area 0.0.0.2 is a stub, default route from 3.3.3.3"],
        ["CORE", "route", "10.2.0.0/16 via 10.0.0.6, ospf inter-area, cost 20"],
        ["ABR2", "ospf", "summary-LSA 10.2.0.0/16 originated into area 0"],
      ],
      events: [
        { at: 29, down: "link:ABR1-ABR2", log: [["ABR1", "ospf", "neighbor 3.3.3.3 on ether3 changed to Down"], ["ABR1", "route", "10.2.0.0/16 now via CORE, cost 30"]] },
        { at: 43, up: "link:ABR1-ABR2", log: [["ABR1", "ospf", "neighbor 3.3.3.3 on ether3 changed to Full"]] },
      ],
    },
    {
      id: "spine-leaf", name: "Spine and leaf",
      blurb: "Every leaf connects to every spine. eBGP underlay, traffic spread over both spines.",
      areas: [{ x: 22, y: 8, w: 596, h: 282, label: "Fabric · eBGP, ECMP", color: "blue" }],
      nodes: {
        SPINE1: { x: 214, y: 76, k: "r", sub: "AS 65000" }, SPINE2: { x: 426, y: 76, k: "r", sub: "AS 65000" },
        LEAF1: { x: 92, y: 214, k: "s", sub: "AS 65001" }, LEAF2: { x: 241, y: 214, k: "s", sub: "AS 65002" },
        LEAF3: { x: 399, y: 214, k: "s", sub: "AS 65003" }, LEAF4: { x: 548, y: 214, k: "s", sub: "AS 65004" },
        SRV1: { x: 92, y: 372, k: "c", sub: "10.1.1.10" }, SRV2: { x: 241, y: 372, k: "c", sub: "10.1.2.10" },
        SRV3: { x: 399, y: 372, k: "c", sub: "10.1.3.10" }, SRV4: { x: 548, y: 372, k: "c", sub: "10.1.4.10" },
      },
      links: [
        ["SPINE1", "LEAF1"], ["SPINE1", "LEAF2"], ["SPINE1", "LEAF3"], ["SPINE1", "LEAF4"],
        ["SPINE2", "LEAF1"], ["SPINE2", "LEAF2"], ["SPINE2", "LEAF3"], ["SPINE2", "LEAF4"],
        ["LEAF1", "SRV1"], ["LEAF2", "SRV2"], ["LEAF3", "SRV3"], ["LEAF4", "SRV4"],
      ],
      flows: [
        { paths: [["SRV1", "LEAF1", "SPINE1", "LEAF3", "SRV3"], ["SRV1", "LEAF1", "SPINE2", "LEAF3", "SRV3"]] },
        { paths: [["SRV2", "LEAF2", "SPINE2", "LEAF4", "SRV4"], ["SRV2", "LEAF2", "SPINE1", "LEAF4", "SRV4"]] },
        { paths: [["SRV4", "LEAF4", "SPINE1", "LEAF1", "SRV1"], ["SRV4", "LEAF4", "SPINE2", "LEAF1", "SRV1"]] },
        { paths: [["SRV3", "LEAF3", "SPINE2", "LEAF2", "SRV2"], ["SRV3", "LEAF3", "SPINE1", "LEAF2", "SRV2"]] },
      ],
      log: [
        ["SPINE1", "bgp", "session to LEAF1 (AS 65001) Established"],
        ["LEAF2", "bgp", "10.1.4.0/24 received from 2 peers"],
        ["LEAF3", "route", "10.1.1.0/24 via SPINE1 and SPINE2 (ECMP)"],
        ["SPINE2", "bgp", "4 sessions Established"],
        ["LEAF4", "bfd", "session to SPINE2 up, 300 ms"],
      ],
      events: [
        { at: 31, down: "node:SPINE1", log: [["LEAF1", "bgp", "session to SPINE1 Idle, hold timer expired"], ["LEAF3", "route", "ECMP 2 → 1 paths to 10.1.1.0/24"]] },
        { at: 45, up: "node:SPINE1", log: [["SPINE1", "bgp", "4 sessions Established"], ["LEAF3", "route", "ECMP 1 → 2 paths to 10.1.1.0/24"]] },
      ],
    },
    {
      id: "collapsed-core", name: "Collapsed core",
      blurb: "Core and distribution in one pair of switches, sharing the gateway with VRRP.",
      areas: [{ x: 100, y: 104, w: 440, h: 128, label: "Collapsed core · VRRP, LACP", color: "blue" }],
      nodes: {
        EDGE: { x: 320, y: 50, k: "r", sub: "10.0.0.1" },
        CORE1: { x: 206, y: 160, k: "s", sub: "VRRP master" }, CORE2: { x: 434, y: 160, k: "s", sub: "VRRP backup" },
        ACC1: { x: 92, y: 284, k: "s" }, ACC2: { x: 241, y: 284, k: "s" }, ACC3: { x: 399, y: 284, k: "s" }, ACC4: { x: 548, y: 284, k: "s" },
        PC1: { x: 92, y: 392, k: "c", sub: "10.10.1.21" }, PC4: { x: 548, y: 392, k: "c", sub: "10.10.4.33" },
      },
      links: [
        ["EDGE", "CORE1"], ["EDGE", "CORE2"], ["CORE1", "CORE2", { bond: true, label: "LACP" }],
        ["ACC1", "CORE1"], ["ACC1", "CORE2"], ["ACC2", "CORE1"], ["ACC2", "CORE2"],
        ["ACC3", "CORE1"], ["ACC3", "CORE2"], ["ACC4", "CORE1"], ["ACC4", "CORE2"],
        ["ACC1", "PC1"], ["ACC4", "PC4"],
      ],
      flows: [
        { paths: [["PC1", "ACC1", "CORE1", "EDGE"], ["PC1", "ACC1", "CORE2", "EDGE"]] },
        { paths: [["EDGE", "CORE1", "ACC4", "PC4"], ["EDGE", "CORE2", "ACC4", "PC4"]] },
        { paths: [["PC4", "ACC4", "CORE1", "ACC1", "PC1"], ["PC4", "ACC4", "CORE2", "ACC1", "PC1"]] },
      ],
      log: [
        ["CORE1", "vrrp", "vrrp1 MASTER for 10.10.0.1, priority 200"],
        ["CORE1", "bonding", "bond1 (ether7, ether8) LACP active, 2 links up"],
        ["EDGE", "route", "10.10.0.0/16 via 10.0.0.2 (CORE1)"],
        ["CORE2", "vrrp", "vrrp1 BACKUP, master 10.10.0.2 alive"],
        ["ACC2", "bridge", "uplinks ether1, ether2 forwarding"],
      ],
      events: [
        { at: 30, down: "node:CORE1", sub: { CORE1: "failed", CORE2: "VRRP master" }, log: [["CORE2", "vrrp", "vrrp1 is now MASTER for 10.10.0.1"], ["ACC1", "bridge", "ether1 down, traffic moves to ether2"]] },
        { at: 44, up: "node:CORE1", sub: { CORE1: "VRRP master", CORE2: "VRRP backup" }, log: [["CORE1", "vrrp", "vrrp1 preempts, MASTER for 10.10.0.1"]] },
      ],
    },
    {
      id: "ipsec", name: "IPsec site to site",
      blurb: "Two offices joined over the internet by an encrypted tunnel between their routers.",
      areas: [
        { x: 20, y: 140, w: 240, h: 314, label: "Site A · 10.1.0.0/24", color: "green" },
        { x: 380, y: 140, w: 240, h: 314, label: "Site B · 10.2.0.0/24", color: "purple" },
      ],
      nodes: {
        NET: { x: 320, y: 70, k: "cloud", name: "Internet" },
        RA: { x: 130, y: 196, k: "r", sub: "203.0.113.10" }, RB: { x: 510, y: 196, k: "r", sub: "198.51.100.20" },
        SWA: { x: 130, y: 292, k: "s" }, SWB: { x: 510, y: 292, k: "s" },
        PA1: { x: 72, y: 390, k: "c", sub: "10.1.0.11" }, PA2: { x: 190, y: 390, k: "c", sub: "10.1.0.12" },
        PB1: { x: 450, y: 390, k: "c", sub: "10.2.0.11" }, PB2: { x: 570, y: 390, k: "c", sub: "10.2.0.12" },
      },
      links: [
        ["NET", "RA"], ["NET", "RB"], ["RA", "SWA"], ["SWA", "PA1"], ["SWA", "PA2"], ["RB", "SWB"], ["SWB", "PB1"], ["SWB", "PB2"],
        ["RA", "RB", { tunnel: true, label: "IPsec tunnel" }],
      ],
      flows: [
        { paths: [["PA1", "SWA", "RA", "RB", "SWB", "PB2"]] },
        { paths: [["PB1", "SWB", "RB", "RA", "SWA", "PA2"]] },
        { paths: [["PA2", "SWA", "RA", "NET"]], every: [5, 8] },
      ],
      log: [
        ["RA", "ipsec", "phase 1 established with 198.51.100.20 (IKEv2)"],
        ["RA", "ipsec", "SA installed 10.1.0.0/24 ⇄ 10.2.0.0/24, aes-256-gcm"],
        ["RB", "firewall", "accepted ipsec-esp from 203.0.113.10"],
        ["RA", "ipsec", "dead peer detection: 198.51.100.20 alive"],
        ["RB", "route", "10.1.0.0/24 via ipsec policy"],
      ],
      events: [
        { at: 34, pulse: "link:RA-RB", log: [["RA", "ipsec", "phase 2 rekeyed, new SA spi 0x5e1f02c7"]] },
      ],
    },
    {
      id: "office", name: "Small office",
      blurb: "One router for internet, NAT and firewall, a switch, a file server and a guest VLAN.",
      areas: [
        { x: 36, y: 214, w: 428, h: 240, label: "Office · VLAN 10", color: "blue" },
        { x: 476, y: 214, w: 128, h: 126, label: "Guest · VLAN 20", color: "amber" },
      ],
      nodes: {
        NET: { x: 320, y: 58, k: "cloud", name: "Internet" },
        GW: { x: 320, y: 158, k: "r", sub: "NAT · firewall" }, SW1: { x: 320, y: 270, k: "s" },
        NAS: { x: 112, y: 270, k: "c", sub: "files · DHCP · DNS" }, GUEST: { x: 540, y: 270, k: "c" },
        PC1: { x: 180, y: 388, k: "c", sub: "192.168.10.21" }, PC2: { x: 320, y: 388, k: "c", sub: "192.168.10.23" },
        PC3: { x: 460, y: 388, k: "c", sub: "192.168.10.31", late: 26 },
      },
      links: [["NET", "GW"], ["GW", "SW1"], ["SW1", "NAS"], ["SW1", "GUEST"], ["SW1", "PC1"], ["SW1", "PC2"], ["SW1", "PC3"]],
      flows: [
        { paths: [["PC1", "SW1", "GW", "NET"]] },
        { paths: [["NET", "GW", "SW1", "PC2"]] },
        { paths: [["PC2", "SW1", "NAS"]], every: [4, 6] },
        { paths: [["GUEST", "SW1", "GW", "NET"]], color: "amber" },
        { paths: [["PC3", "SW1", "NAS"]] },
      ],
      log: [
        ["GW", "dhcp", "leased 192.168.10.23 to PC2"],
        ["NAS", "dns", "nas.office.lan answered with 192.168.10.5"],
        ["GW", "nat", "masquerade 192.168.10.0/24 to 198.51.100.7"],
        ["GW", "dhcp", "guest pool leased 192.168.20.14 to GUEST"],
        ["NAS", "smb", "PC1 connected to share projects"],
      ],
      events: [
        { at: 26, show: "node:PC3", log: [["GW", "dhcp", "leased 192.168.10.31 to PC3, a new device"]] },
        { at: 39, log: [["GW", "firewall", "dropped guest → office 192.168.10.5:445"]] },
      ],
    },
    {
      id: "mpls", name: "ISP core with MPLS", prompt: "PE1",
      blurb: "A provider core switching labels, carrying one customer's two sites in a VRF.",
      areas: [{ x: 116, y: 12, w: 408, h: 330, label: "MPLS core · AS 65100", color: "amber" }],
      nodes: {
        CE1: { x: 58, y: 166, k: "r", sub: "customer A" }, PE1: { x: 190, y: 166, k: "r", sub: "PE" },
        P1: { x: 320, y: 86, k: "r", sub: "P" }, P2: { x: 320, y: 254, k: "r", sub: "P" },
        PE2: { x: 450, y: 166, k: "r", sub: "PE" }, CE2: { x: 582, y: 166, k: "r", sub: "customer A" },
        PC1: { x: 58, y: 380, k: "c", sub: "10.10.0.10" }, PC2: { x: 582, y: 380, k: "c", sub: "10.20.0.10" },
      },
      links: [
        ["CE1", "PE1"], ["PE1", "P1", { mpls: true }], ["PE1", "P2", { mpls: true }], ["P1", "P2", { mpls: true }],
        ["P1", "PE2", { mpls: true }], ["P2", "PE2", { mpls: true }], ["PE2", "CE2"], ["CE1", "PC1"], ["CE2", "PC2"],
      ],
      flows: [
        { paths: [["PC1", "CE1", "PE1", "P1", "PE2", "CE2", "PC2"], ["PC1", "CE1", "PE1", "P2", "PE2", "CE2", "PC2"]] },
        { paths: [["PC2", "CE2", "PE2", "P2", "PE1", "CE1", "PC1"], ["PC2", "CE2", "PE2", "P1", "PE1", "CE1", "PC1"]] },
      ],
      log: [
        ["PE1", "ldp", "neighbor 10.255.0.2 operational, 12 labels"],
        ["PE1", "bgp", "vpnv4 10.20.0.0/24 RD 65100:1 from 10.255.0.4"],
        ["P1", "mpls", "label 21 swapped to 34 toward PE2"],
        ["PE2", "vrf", "CUST-A installed 10.10.0.0/24 via 10.255.0.1"],
        ["P2", "ospf", "neighbor 10.255.0.1 on ether1 Full"],
      ],
      events: [
        { at: 30, down: "node:P1", log: [["PE1", "ldp", "session with 10.255.0.2 (P1) down"], ["PE1", "mpls", "LSP to 10.255.0.4 rerouted via P2"]] },
        { at: 44, up: "node:P1", log: [["PE1", "ldp", "session with 10.255.0.2 operational"]] },
      ],
    },
    {
      id: "multihoming", name: "BGP multihoming", prompt: "EDGE",
      blurb: "One network, two internet providers. Traffic prefers the primary and fails over.",
      areas: [{ x: 108, y: 214, w: 424, h: 240, label: "Your network · AS 65010", color: "blue" }],
      nodes: {
        NET: { x: 320, y: 52, k: "cloud", name: "Internet" },
        ISPA: { x: 150, y: 138, k: "r", name: "ISP-A", sub: "AS 64500" }, ISPB: { x: 490, y: 138, k: "r", name: "ISP-B", sub: "AS 64510" },
        EDGE: { x: 320, y: 262, k: "r" }, SW1: { x: 320, y: 352, k: "s" },
        PC1: { x: 190, y: 400, k: "c" }, PC2: { x: 450, y: 400, k: "c" },
      },
      links: [
        ["NET", "ISPA"], ["NET", "ISPB"], ["ISPA", "EDGE", { label: "primary" }], ["ISPB", "EDGE", { label: "backup" }],
        ["EDGE", "SW1"], ["SW1", "PC1"], ["SW1", "PC2"],
      ],
      flows: [
        { paths: [["PC1", "SW1", "EDGE", "ISPA", "NET"], ["PC1", "SW1", "EDGE", "ISPB", "NET"]] },
        { paths: [["NET", "ISPA", "EDGE", "SW1", "PC2"], ["NET", "ISPB", "EDGE", "SW1", "PC2"]] },
      ],
      log: [
        ["EDGE", "bgp", "advertising 203.0.113.0/24 to AS 64500 and AS 64510"],
        ["EDGE", "bgp", "local-pref 200 on routes from AS 64500"],
        ["ISP-B", "bgp", "203.0.113.0/24 as-path 65010 65010 65010"],
        ["EDGE", "route", "0.0.0.0/0 via 192.0.2.1 (AS 64500)"],
        ["EDGE", "bfd", "session to 192.0.2.1 up"],
      ],
      events: [
        { at: 30, down: "link:ISPA-EDGE", log: [["EDGE", "bgp", "session to AS 64500 Idle, link down"], ["EDGE", "route", "0.0.0.0/0 now via AS 64510"]] },
        { at: 44, up: "link:ISPA-EDGE", log: [["EDGE", "bgp", "session to AS 64500 Established"]] },
      ],
    },
    {
      id: "hub-spoke", name: "Hub and spoke VPN",
      blurb: "Branches tunnel to headquarters with WireGuard. Branch to branch goes through the hub.",
      nodes: {
        HQ: { x: 320, y: 66, k: "r", sub: "hub · 10.0.0.1" }, SRV: { x: 500, y: 66, k: "c", sub: "intranet" },
        NET: { x: 320, y: 226, k: "cloud", name: "Internet" },
        BR1: { x: 96, y: 352, k: "r", sub: "branch 1" }, BR2: { x: 320, y: 392, k: "r", sub: "branch 2", late: 24 },
        BR3: { x: 544, y: 352, k: "r", sub: "branch 3" },
      },
      links: [
        ["HQ", "NET", { faint: true }], ["BR1", "NET", { faint: true }], ["BR2", "NET", { faint: true }], ["BR3", "NET", { faint: true }],
        ["HQ", "SRV"],
        ["HQ", "BR1", { tunnel: true, curve: 64, label: "wg" }], ["HQ", "BR3", { tunnel: true, curve: -64, label: "wg" }],
        ["HQ", "BR2", { tunnel: true, curve: 112, label: "wg" }],
      ],
      flows: [
        { paths: [["BR1", "HQ", "SRV"]] },
        { paths: [["SRV", "HQ", "BR3"]] },
        { paths: [["BR2", "HQ", "BR1"]], every: [4, 6] },
        { paths: [["BR3", "HQ", "SRV"]], every: [4, 7] },
      ],
      log: [
        ["HQ", "wireguard", "peer BR1 handshake completed"],
        ["HQ", "wireguard", "peer BR3 latest handshake 14s ago"],
        ["BR1", "route", "10.0.0.0/8 via wg-hq"],
        ["HQ", "wireguard", "rx 1.2 MiB from BR1, tx 3.4 MiB"],
        ["BR3", "wireguard", "keepalive to 203.0.113.1:13231"],
      ],
      events: [
        { at: 24, show: "node:BR2", log: [["HQ", "wireguard", "new peer BR2 (10.0.0.12), handshake completed"]] },
      ],
    },
    {
      id: "vlans", name: "VLANs, router on a stick",
      blurb: "Two VLANs share the switches. Traffic between them goes up the trunk to the router and back.",
      areas: [
        { x: 36, y: 334, w: 268, h: 120, label: "VLAN 10 · staff", color: "green" },
        { x: 336, y: 334, w: 268, h: 120, label: "VLAN 20 · lab", color: "purple" },
      ],
      nodes: {
        R1: { x: 320, y: 54, k: "r" }, SW1: { x: 320, y: 176, k: "s" },
        SW2: { x: 170, y: 262, k: "s" }, SW3: { x: 470, y: 262, k: "s" },
        PC1: { x: 100, y: 392, k: "c", sub: "192.168.10.11" }, PC2: { x: 240, y: 392, k: "c", sub: "192.168.10.12" },
        PC3: { x: 400, y: 392, k: "c", sub: "192.168.20.13" }, PC4: { x: 540, y: 392, k: "c", sub: "192.168.20.14" },
      },
      links: [
        ["R1", "SW1", { label: "trunk 10,20" }], ["SW1", "SW2", { label: "trunk" }], ["SW1", "SW3", { label: "trunk" }],
        ["SW2", "PC1"], ["SW2", "PC2"], ["SW3", "PC3"], ["SW3", "PC4"],
      ],
      flows: [
        { paths: [["PC1", "SW2", "SW1", "R1", "SW1", "SW3", "PC4"]], color: "ok", turn: "R1", color2: "purple" },
        { paths: [["PC3", "SW3", "SW1", "R1", "SW1", "SW2", "PC2"]], color: "purple", turn: "R1", color2: "ok" },
        { paths: [["PC1", "SW2", "PC2"]], every: [5, 7] },
      ],
      log: [
        ["SW1", "bridge", "vlan 10,20 tagged on ether1, trunk to R1"],
        ["R1", "route", "192.168.10.11 → 192.168.20.14, vlan10 to vlan20"],
        ["SW2", "bridge", "learned 52:54:00:05:01:01 on ether2, vlan 10"],
        ["R1", "dhcp", "vlan20 leased 192.168.20.14 to PC4"],
        ["SW3", "bridge", "ether3 untagged member of vlan 20"],
      ],
      events: [
        { at: 33, pulse: "link:R1-SW1", log: [["R1", "firewall", "vlan20 → vlan10 ssh dropped by rule 4"]] },
      ],
    },
    {
      id: "ring", name: "Switch ring with RSTP",
      blurb: "A loop of switches. RSTP blocks one link, and opens it when another one breaks.",
      areas: [{ x: 108, y: 14, w: 424, h: 440, label: "RSTP ring", color: "teal" }],
      nodes: {
        SW1: { x: 320, y: 78, k: "s", sub: "root bridge" }, SW2: { x: 460, y: 180, k: "s" }, SW3: { x: 406, y: 340, k: "s" },
        SW4: { x: 234, y: 340, k: "s" }, SW5: { x: 180, y: 180, k: "s" },
        PCA: { x: 588, y: 270, k: "c", sub: "10.30.0.21" }, PCB: { x: 52, y: 270, k: "c", sub: "10.30.0.22" },
      },
      links: [
        ["SW1", "SW2"], ["SW2", "SW3"], ["SW3", "SW4", { blocked: true, label: "discarding" }], ["SW4", "SW5"], ["SW5", "SW1"],
        ["SW2", "PCA"], ["SW5", "PCB"],
      ],
      flows: [
        { paths: [["PCA", "SW2", "SW1", "SW5", "PCB"], ["PCA", "SW2", "SW3", "SW4", "SW5", "PCB"]] },
        { paths: [["PCB", "SW5", "SW1", "SW2", "PCA"], ["PCB", "SW5", "SW4", "SW3", "SW2", "PCA"]] },
      ],
      log: [
        ["SW1", "rstp", "root bridge, priority 0x1000"],
        ["SW3", "rstp", "ether2 alternate port, discarding"],
        ["SW4", "rstp", "ether1 root port, path cost 20000"],
        ["SW2", "bridge", "learned 52:54:00:06:01:01 on ether3"],
      ],
      events: [
        { at: 28, down: "link:SW1-SW2", unblock: "link:SW3-SW4", labels: { "SW3-SW4": "forwarding" },
          log: [["SW2", "rstp", "ether1 (root port) down"], ["SW3", "rstp", "ether2 discarding → forwarding"], ["SW2", "rstp", "topology change, fdb flushed"]] },
        { at: 43, up: "link:SW1-SW2", block: "link:SW3-SW4", labels: { "SW3-SW4": "discarding" },
          log: [["SW2", "rstp", "ether1 forwarding, root port again"], ["SW3", "rstp", "ether2 back to alternate, discarding"]] },
      ],
    },
  ];

  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined && v !== false) el.setAttribute(k, v);
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
    return el;
  }
  const rand = (a, b) => a + Math.random() * (b - a);
  const pairKey = (a, b) => `${a}-${b}`;

  // Points along a link, for the line and for packets: straight, or a gentle curve (tunnels).
  function geometry(A, B, curve) {
    if (!curve) return { d: `M${A.x} ${A.y}L${B.x} ${B.y}`, pts: [A, B], mid: { x: (A.x + B.x) / 2, y: (A.y + B.y) / 2 } };
    const len = Math.hypot(B.x - A.x, B.y - A.y);
    const px = -(B.y - A.y) / len, py = (B.x - A.x) / len;
    const C = { x: (A.x + B.x) / 2 + px * curve * 2, y: (A.y + B.y) / 2 + py * curve * 2 };
    const pts = [];
    for (let i = 0; i <= 24; i++) {
      const t = i / 24, u = 1 - t;
      pts.push({ x: u * u * A.x + 2 * u * t * C.x + t * t * B.x, y: u * u * A.y + 2 * u * t * C.y + t * t * B.y });
    }
    return { d: `M${A.x} ${A.y}Q${C.x} ${C.y} ${B.x} ${B.y}`, pts, mid: pts[12] };
  }

  // ------------------------------------------------------------ one lab on screen
  function Scene(lab, ui, still) {
    const nodes = new Map(), links = new Map(), packets = [];
    const root = s("g", { class: "scene" });
    const gAreas = s("g"), gLinks = s("g"), gLabels = s("g"), gPkts = s("g"), gNodes = s("g");
    root.append(gAreas, gLinks, gLabels, gPkts, gNodes);
    let t = 0;
    const timeline = [];
    const at = (when, fn) => timeline.push({ when, fn });
    const flash = (el, cls, ms) => { el.classList.remove(cls); void el.getBoundingClientRect(); el.classList.add(cls); setTimeout(() => el.classList.remove(cls), ms); };

    for (const a of lab.areas || []) {
      // The label goes in a corner: top left unless at says "bl", "br" or "tr".
      const pos = a.at || "tl";
      const right = pos[1] === "r";
      gAreas.append(s("g", { class: `area c-${a.color}` },
        s("rect", { x: a.x, y: a.y, width: a.w, height: a.h, rx: 14 }),
        s("text", { x: right ? a.x + a.w - 12 : a.x + 12, y: pos[0] === "b" ? a.y + a.h - 9 : a.y + 19,
          style: right ? "text-anchor:end" : null }, a.label)));
    }

    for (const [id, n] of Object.entries(lab.nodes)) {
      const name = n.name || id;
      const el = s("g", { class: "n", transform: `translate(${n.x},${n.y})` });
      let sub = null;
      if (n.k === "cloud") {
        el.classList.add("cloudn");
        el.append(s("path", { class: "a-cloud", d: CLOUD }), s("text", { y: 9 }, name));
      } else {
        sub = s("text", { class: "a-sub", y: 62 }, n.sub || "");
        el.append(
          s("g", { class: "a-tile" },
            s("rect", { class: "a-box", x: -30, y: -30, width: 60, height: 60, rx: 14 }),
            s("path", { class: "a-glyph", d: ICONS[n.k], transform: "translate(-19.2,-19.2) scale(1.6)" }),
            s("circle", { class: "a-rip", cx: 26, cy: -26, r: 6.5 }),
            s("circle", { class: "a-dot", cx: 26, cy: -26, r: 6.5 })),
          s("text", { y: 48 }, name), sub);
      }
      gNodes.append(el);
      nodes.set(id, { id, name, ...n, el, sub, shown: !n.late, on: n.k === "cloud", failed: false });
    }

    for (const [a, b, o = {}] of lab.links) {
      const A = nodes.get(a), B = nodes.get(b);
      const g = geometry(A, B, o.curve);
      const cls = "l" + (o.tunnel ? " tunnel" : "") + (o.faint ? " faint" : "") + (o.bond ? " bond" : "");
      // Its length drives the draw-in animation (see l-draw in login.html).
      const length = g.pts.slice(1).reduce((sum, pt, i) => sum + Math.hypot(pt.x - g.pts[i].x, pt.y - g.pts[i].y), 0).toFixed(1);
      const el = s("path", { class: cls, d: g.d, style: `--len:${length}` });
      let bondEl = null;
      if (o.bond) {
        // A bond is drawn as two thin lines side by side.
        const len = Math.hypot(B.x - A.x, B.y - A.y), px = -(B.y - A.y) / len * 3, py = (B.x - A.x) / len * 3;
        el.setAttribute("d", `M${A.x + px} ${A.y + py}L${B.x + px} ${B.y + py}`);
        bondEl = s("path", { class: cls, d: `M${A.x - px} ${A.y - py}L${B.x - px} ${B.y - py}`, style: `--len:${length}` });
      }
      gLinks.append(el);
      if (bondEl) gLinks.append(bondEl);
      let label = null;
      if (o.label) {
        label = s("text", { class: "a-port", x: g.mid.x.toFixed(1), y: (g.mid.y + 4).toFixed(1) }, o.label);
        gLabels.append(label);
      }
      const link = { a, b, o, g, els: bondEl ? [el, bondEl] : [el], label, down: false, blocked: !!o.blocked };
      links.set(pairKey(a, b), link);
      links.set(pairKey(b, a), link);
    }
    const allLinks = [...new Set(links.values())];

    const nodeUp = (n) => n.shown && n.on && !n.failed;
    const linkShown = (l) => nodes.get(l.a).shown && nodes.get(l.b).shown;
    const linkUp = (l) => linkShown(l) && !l.down && !l.blocked && nodeUp(nodes.get(l.a)) && nodeUp(nodes.get(l.b));

    function paint() {
      for (const n of nodes.values()) {
        n.el.classList.toggle("hide", !n.shown);
        n.el.classList.toggle("on", n.on && !n.failed);
        n.el.classList.toggle("failed", n.failed);
      }
      for (const l of allLinks) {
        for (const el of l.els) {
          el.classList.toggle("hide", !linkShown(l));
          el.classList.toggle("up", linkUp(l));
          el.classList.toggle("down", l.down);
          el.classList.toggle("blocked", l.blocked && !l.down);
        }
        if (l.label) l.label.classList.toggle("hide", !linkShown(l));
      }
    }

    // -- the minute, planned up front
    const order = [...nodes.values()].filter((n) => n.k !== "cloud" && !n.late)
      .sort((x, y) => "rsc".indexOf(x.k) - "rsc".indexOf(y.k));
    const step = Math.min(0.32, 3.4 / Math.max(1, order.length));
    const boot = (n) => {
      n.on = true;
      if (!still) { flash(n.el, "boot", 1500); }
      paint();
    };
    if (still) {
      for (const n of order) n.on = true;
    } else {
      [...nodes.values()].filter((n) => n.shown).forEach((n, i) => { n.el.style.setProperty("--in", `${(i * 0.035).toFixed(3)}s`); flash(n.el, "enter", 900); });
      allLinks.filter(linkShown).forEach((l, i) => { for (const el of l.els) { el.style.setProperty("--in", `${(0.15 + i * 0.03).toFixed(3)}s`); flash(el, "enter", 1600); } });
      order.forEach((n, i) => at(0.9 + i * step, () => boot(n)));
    }
    for (const n of nodes.values()) {
      if (!n.late) continue;
      at(n.late, () => {
        n.shown = true;
        if (!still) {
          n.el.style.setProperty("--in", "0s");
          flash(n.el, "enter", 900);
          for (const l of allLinks) if ((l.a === n.id || l.b === n.id) && linkShown(l)) for (const el of l.els) { el.style.setProperty("--in", "0s"); flash(el, "enter", 1400); }
        }
        paint();
      });
      at(n.late + 0.6, () => boot(n));
    }

    const target = (ref) => {
      const [kind, key] = ref.split(":");
      return kind === "node" ? { node: nodes.get(key) } : { link: links.get(key) };
    };
    for (const ev of lab.events || []) {
      at(ev.at, () => {
        if (ev.down) { const x = target(ev.down); if (x.node) { x.node.failed = true; if (!still) flash(x.node.el, "stop", 900); } else x.link.down = true; }
        if (ev.up) { const x = target(ev.up); if (x.node) { x.node.failed = false; if (!still) flash(x.node.el, "boot", 1500); } else x.link.down = false; }
        if (ev.block) target(ev.block).link.blocked = true;
        if (ev.unblock) target(ev.unblock).link.blocked = false;
        if (ev.pulse && !still) for (const el of target(ev.pulse).link.els) flash(el, "pulse", 1300);
        for (const [id, text] of Object.entries(ev.sub || {})) nodes.get(id).sub.textContent = text;
        for (const [key, text] of Object.entries(ev.labels || {})) links.get(key).label.textContent = text;
        for (const line of ev.log || []) ui.log(...line);
        paint();
      });
    }

    // Ambient log lines, cycling through the lab's list.
    let li = 0;
    for (let when = 2.4; when < SCENE - 4; when += rand(5.5, 7.5)) {
      at(when, () => { if (lab.log.length) ui.log(...lab.log[li++ % lab.log.length]); });
    }

    // Shut down at the end of the minute: everything stops, last started first.
    const stopAt = SCENE - 2.6;
    [...nodes.values()].filter((n) => n.k !== "cloud").reverse().forEach((n, i) => at(stopAt + i * 0.06, () => {
      if (!n.on) return;
      n.on = false;
      if (!still) flash(n.el, "stop", 900);
      paint();
    }));
    at(SCENE - 1, () => { if (!still) root.classList.add("out"); });
    timeline.sort((x, y) => x.when - y.when);

    // -- traffic
    const flows = (lab.flows || []).map((f) => ({ ...f, next: rand(4.6, 7.2) }));
    function launch(flow) {
      const path = flow.paths.find((p) => p.every((id) => nodeUp(nodes.get(id))) &&
        p.slice(1).every((id, i) => { const l = links.get(pairKey(p[i], id)); return l && linkUp(l); }));
      if (!path) return;
      const hops = [];
      let total = 0, turned = false;
      for (let i = 0; i < path.length - 1; i++) {
        const l = links.get(pairKey(path[i], path[i + 1]));
        let pts = l.g.pts;
        if (l.a !== path[i]) pts = [...pts].reverse();
        if (flow.turn && path[i] === flow.turn) turned = true;
        const color = l.o.tunnel ? "accent" : l.o.mpls ? "amber" : turned && flow.color2 ? flow.color2 : flow.color || "ok";
        for (let j = 0; j < pts.length - 1; j++) {
          const len = Math.hypot(pts[j + 1].x - pts[j].x, pts[j + 1].y - pts[j].y);
          hops.push({ from: pts[j], to: pts[j + 1], start: total, len, color });
          total += len;
        }
      }
      const el = s("circle", { class: `pkt p-${hops[0].color}`, r: 3.4 });
      gPkts.append(el);
      packets.push({ el, hops, total, t0: t, color: hops[0].color });
    }

    function tick(dt) {
      t += dt;
      while (timeline.length && timeline[0].when <= t) timeline.shift().fn();
      if (!still && t < SCENE - 3) {
        for (const f of flows) {
          if (t >= f.next) {
            launch(f);
            const [lo, hi] = f.every || [2.6, 4.2];
            f.next = t + rand(lo, hi);
          }
        }
      }
      for (let i = packets.length - 1; i >= 0; i--) {
        const p = packets[i];
        const d = (t - p.t0) * SPEED;
        if (d >= p.total) { p.el.remove(); packets.splice(i, 1); continue; }
        const h = p.hops.find((x) => d < x.start + x.len) || p.hops[p.hops.length - 1];
        const k = h.len ? (d - h.start) / h.len : 0;
        p.el.setAttribute("cx", (h.from.x + (h.to.x - h.from.x) * k).toFixed(1));
        p.el.setAttribute("cy", (h.from.y + (h.to.y - h.from.y) * k).toFixed(1));
        if (h.color !== p.color) { p.el.classList.replace(`p-${p.color}`, `p-${h.color}`); p.color = h.color; }
      }
      return t < SCENE;
    }

    paint();
    // The log ends at a prompt on the lab's own router: the one named, or the first router.
    const first = nodes.get(lab.prompt) || [...nodes.values()].find((n) => n.k === "r") || [...nodes.values()].find((n) => n.k !== "cloud");
    return { root, tick, prompt: first ? first.name : "lab" };
  }

  // ------------------------------------------------------------ the rotation
  function start({ svg, log, name, blurb, bar, head }) {
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const hidden = matchMedia("(max-width: 880px)");
    let bag = [], last = null, scene = null;

    const logUi = {
      log(who, topic, text) {
        const line = `${who.padEnd(7)}${topic.padEnd(10)}${text}`;
        const el = document.createElement("div");
        el.className = "a-line";
        el.style.setProperty("--n", line.length);
        const b = document.createElement("b");
        b.textContent = who.padEnd(7);
        el.append(b, topic.padEnd(10) + text);
        el.addEventListener("animationend", () => el.classList.add("typed"), { once: true });
        log.insertBefore(el, log.lastElementChild);
        while (log.children.length > 5) log.firstElementChild.remove();
      },
    };

    function next() {
      // Every lab once, in random order, then again; never the same one twice in a row.
      if (!bag.length) {
        bag = LABS.map((_, i) => i);
        for (let i = bag.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [bag[i], bag[j]] = [bag[j], bag[i]]; }
        if (bag[bag.length - 1] === last && bag.length > 1) [bag[0], bag[bag.length - 1]] = [bag[bag.length - 1], bag[0]];
      }
      show(bag.pop());
    }

    function show(index) {
      last = index;
      const lab = LABS[index];
      if (scene) scene.root.remove();
      log.replaceChildren(document.createElement("div"));
      scene = Scene(lab, logUi, still);
      svg.append(scene.root);
      log.lastElementChild.append(Object.assign(document.createElement("b"), { textContent: `[admin@${scene.prompt}] >` }), " ",
        Object.assign(document.createElement("span"), { className: "a-caret" }));
      name.textContent = lab.name;
      blurb.textContent = lab.blurb;
      head.classList.remove("out");
      const fresh = bar.cloneNode(false);  // restarts the progress animation
      bar.replaceWith(fresh);
      bar = fresh;
    }

    let prev = performance.now();
    function frame(now) {
      const dt = Math.min(0.1, (now - prev) / 1000);  // a background tab resumes where it was
      prev = now;
      if (!hidden.matches && scene) {
        if (!scene.tick(dt)) next();
        else if (scene && !still && scene.root.classList.contains("out")) head.classList.add("out");
      }
      requestAnimationFrame(frame);
    }
    next();
    requestAnimationFrame(frame);

    // For trying labs out from the browser console:
    //   MikroboxArt.current.show("ipsec")   MikroboxArt.current.skip(30)   (seconds ahead)
    const handle = {
      labs: LABS.map((l) => l.id),
      show: (id) => show(Math.max(0, LABS.findIndex((l) => l.id === id))),
      skip: (seconds) => { for (let i = 0; i < seconds * 20; i++) if (scene && !scene.tick(0.05)) next(); },
    };
    window.MikroboxArt.current = handle;
    return handle;
  }

  window.MikroboxArt = { start, LABS, SCENE };
})();
