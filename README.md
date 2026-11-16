# Mikrobox

A virtual lab for MikroTik CHR routers, run on a Linux VM and driven from the browser.
Think GNS3 or EVE-NG, but only for RouterOS, with the CHR image installed for you.

Open `http://<vm-ip>:8080/` for the lab workspace: add routers and clients, drag them
around, link ports, start and stop devices, and open consoles. The same things are
available through the API (interactive docs at `/docs`).

## What works in this version

- Labs made of CHR routers and lightweight Linux clients, stored in one JSON file.
- Per router: name, number of ports (1-24), vCPUs and RAM.
- A Switch device: a CHR with 24 ports and its own icon, for when you want one as a switch.
- Links between any two ports. Frames pass untouched, so STP, LACP and LLDP/MNDP work.
- A Cloud that gives linked devices internet and makes them reachable from your PC,
  either through the VM (NAT) or directly on your own network (bridged).
- A browser workspace with a topology canvas (pan, zoom, snap), device settings and docked consoles.
- Areas, ellipses, text labels and sticky notes to mark sites, routing areas and tasks.
- Lab export and import as a single JSON file (topology, shapes and notes).
- Router config export (one router, or all as a zip), config import, hostname push and
  factory reset, over SSH on the management network.
- Templates: ready-made layouts for multi-area OSPF, a collapsed core, spine and leaf, an
  ISP with MPLS, and BGP multihoming. Topology and a suggested plan, not router configs.
- Clients take an optional DNS server setting next to address and gateway.
- A live packet view in the browser (Tools menu, or Packets on any running port), with
  capture filters, alongside pcap download for Wireshark.
- Topology export as a PNG image, device duplication, double-click to open a console.
- Automatic config backups per lab (all routers or chosen ones, every 15 minutes to 24
  hours), kept as revisions only when the config changed, with a side-by-side compare.
- Settings: light or dark theme, grid, snapping, port labels, defaults for new routers.
- Browser console for routers (serial port) and clients (shell), over a websocket.
- Live packet capture of any port as a pcap stream you can pipe into Wireshark.
- A hard cap of 128 running devices, and a free-memory check before each router starts.

**Not tested end to end yet.** The API, model, limits and generated host commands are
covered by unit tests, but nothing has booted a real CHR. Expect to fix things on the
first run on a real VM.

## Install

On a Debian 12+ or Ubuntu 22.04+ VM. On Proxmox, set the VM's CPU type to `host` so
nested virtualization (KVM inside the VM) is available.

```bash
git clone https://github.com/Langeboi/Mikrobox && cd Mikrobox
sudo ./scripts/install.sh
```

The installer downloads the current stable CHR image from MikroTik. Pin a version with
`sudo CHR_VERSION=7.x.y ./scripts/install.sh`, or put a raw CHR image at
`/var/lib/mikrobox/images/chr.img` yourself.

To update later: `git pull && sudo ./scripts/install.sh`. Running routers keep running.

## Accounts

The first visit asks you to create an account; after that the workspace and the API need
a sign-in. More accounts are added from the menu with your username at the top right.
Scripts use HTTP Basic auth, e.g. `curl -u yourname http://<vm-ip>:8080/api/labs`.

Locked out? Reset a password on the VM, then restart the service:

```bash
sudo /opt/mikrobox/venv/bin/python -m mikrobox.auth passwd <username>
sudo systemctl restart mikrobox
```

Set `MIKROBOX_AUTH=off` in the service's environment to switch accounts off entirely.

## First lab

```bash
API=http://<vm-ip>:8080/api
J='-u yourname:yourpassword -H content-type:application/json'

curl -s $J $API/labs -d '{"name":"first"}'                                   # lab 1
curl -s $J $API/labs/1/nodes -d '{"name":"R1","ports":4}'                    # node 1
curl -s $J $API/labs/1/nodes -d '{"name":"R2","ports":4}'                    # node 2
curl -s $J $API/labs/1/nodes -d '{"kind":"client","name":"PC1","address":"192.168.10.2/24","gateway":"192.168.10.1"}'  # node 3

curl -s $J $API/labs/1/links -d '{"a":{"node":1,"port":1}}'                           # R1 ether1 -> Cloud
curl -s $J $API/labs/1/links -d '{"a":{"node":2,"port":1}}'                           # R2 ether1 -> Cloud
curl -s $J $API/labs/1/links -d '{"a":{"node":1,"port":2},"b":{"node":2,"port":2}}'   # R1 ether2 <-> R2 ether2
curl -s $J $API/labs/1/links -d '{"a":{"node":1,"port":3},"b":{"node":3,"port":1}}'   # R1 ether3 <-> PC1

curl -s $J -X POST $API/labs/1/start
curl -s $J $API/labs/1
```

Then open `http://<vm-ip>:8080/` to see the lab, or build the same thing there by hand.

## The Cloud: internet and access from your PC

Every lab has a Cloud. Link a port to it and that device gets internet and becomes
reachable from your PC. It works in one of two modes.

**NAT (default).** The Cloud is a private network, `10.99.0.0/16`, behind the VM. A fresh
CHR runs a DHCP client on ether1, so it gets `10.99.1.<node id>` plus a default route and
DNS. To reach it from your PC, add one route pointing that range at the VM:

```
Windows:  route add 10.99.0.0 mask 255.255.0.0 <vm-ip>
Linux:    sudo ip route add 10.99.0.0/16 via <vm-ip>
```

**Bridged.** The VM's own network card becomes a port of the Cloud bridge, so linked
devices sit directly on your network: DHCP from your own router, or a static address in
the same range, with no route needed on your PC.

```bash
sudo /opt/mikrobox/venv/bin/python -m mikrobox.cloudsetup enable            # show the plan
sudo /opt/mikrobox/venv/bin/python -m mikrobox.cloudsetup enable --apply    # do it
sudo /opt/mikrobox/venv/bin/python -m mikrobox.cloudsetup disable --apply   # undo
```

This rewrites the VM's network configuration (ifupdown or netplan), moving its address
from the card onto a bridge named `mbcloud`. The original file is backed up, and if the
gateway cannot be reached afterwards the change is rolled back by itself. Keep the
hypervisor console at hand anyway. Notes:

- On Ubuntu with DHCP, the VM may be given a different address afterwards.
- The hypervisor must allow several MAC addresses on the VM's network card. On Proxmox
  that means the firewall's MAC filter must be off for that card.
- In bridged mode Mikrobox learns each device's address by scanning the network, which
  takes a few seconds and is skipped on networks larger than a /22. You can always type
  the address into the config dialogs.

## Packet capture

```bash
curl -sN -u yourname http://<vm-ip>:8080/api/labs/1/nodes/1/ports/2/capture | wireshark -k -i -
```

Add `?seconds=30` for a capture that ends by itself; the workspace's Capture links use that.

## How it fits together

| Piece | Implementation |
|---|---|
| Router | One QEMU/KVM process per CHR, on a qcow2 overlay of the shared base image |
| Client | A network namespace with a veth pair; the console is a shell inside it |
| Port | One host interface per port, named `mb<node>p<port>` |
| Link | A pair of `tc` redirects between the two port interfaces |
| Cloud | Linux bridge `mbcloud`: NAT with dnsmasq, or bridged to the VM's network card |
| Console | QEMU serial socket or a pty, relayed over a websocket |
| Capture | `tcpdump` on the port interface, streamed as pcap |

## Known gaps

- Sign-in is by username and password over plain HTTP, so keep the server on a network
  you trust. Anyone with an account controls every lab, and client consoles are root
  shells on the VM.
- Config export, import and hostname push are untested against a real router. They need
  the router running, linked to the Cloud, and its RouterOS login.
- `cloudsetup` (bridged mode) is untested on a real VM.
- Backups read the config through the QEMU guest agent, so routers need no Cloud link.
  This is untested on a real CHR; if RouterOS refuses it, routers on the Cloud fall back
  to SSH and the others report why they were skipped. Routers started before this was
  added need one stop and start.
- Automatic backups store the RouterOS login on the server in `backup.json` (readable by
  root only), because they run unattended.
- A lab file carries the topology only. Export router configs separately.
- Clients share the VM's filesystem; they are namespaces, not full machines.
- Stopping a router kills QEMU rather than shutting RouterOS down cleanly.
- The free CHR license limits each interface to 1 Mbps upload.

## Development

```bash
pip install -e '.[dev]'
pytest
MIKROBOX_DRY_RUN=1 MIKROBOX_AUTH=off MIKROBOX_DATA=/tmp/mb MIKROBOX_RUN=/tmp/mbrun python -m mikrobox
```

Dry-run mode logs host commands instead of running them, so the API works on any machine.
