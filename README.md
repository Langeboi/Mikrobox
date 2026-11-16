# Mikrobox

A virtual lab for MikroTik CHR routers, run on a Linux VM and driven from the browser.
Think GNS3 or EVE-NG, but only for RouterOS, with the CHR image installed for you.

Open `http://<vm-ip>:8080/` for the lab workspace: add routers and clients, drag them
around, link ports, start and stop devices, and open consoles. The same things are
available through the API (interactive docs at `/docs`).

## What works in this version

- Labs made of CHR routers and lightweight Linux clients, stored in one JSON file.
- Per router: name, number of ports (1-24), vCPUs and RAM.
- Links between any two ports. Frames pass untouched, so STP, LACP and LLDP/MNDP work.
- A management network (`10.99.0.0/16`) so you can SSH or WinBox to routers from your PC.
- A browser workspace with a topology canvas (pan, zoom, snap), device settings and docked consoles.
- Areas, ellipses, text labels and sticky notes to mark sites, routing areas and tasks.
- Lab export and import as a single JSON file (topology, shapes and notes).
- Router config export (one router, or all as a zip), config import, hostname push and
  factory reset, over SSH on the management network.
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

## First lab

```bash
API=http://<vm-ip>:8080/api
J='-H content-type:application/json'

curl -s $J $API/labs -d '{"name":"first"}'                                   # lab 1
curl -s $J $API/labs/1/nodes -d '{"name":"R1","ports":4}'                    # node 1
curl -s $J $API/labs/1/nodes -d '{"name":"R2","ports":4}'                    # node 2
curl -s $J $API/labs/1/nodes -d '{"kind":"client","name":"PC1","address":"192.168.10.2/24","gateway":"192.168.10.1"}'  # node 3

curl -s $J $API/labs/1/links -d '{"a":{"node":1,"port":1}}'                           # R1 ether1 -> management
curl -s $J $API/labs/1/links -d '{"a":{"node":2,"port":1}}'                           # R2 ether1 -> management
curl -s $J $API/labs/1/links -d '{"a":{"node":1,"port":2},"b":{"node":2,"port":2}}'   # R1 ether2 <-> R2 ether2
curl -s $J $API/labs/1/links -d '{"a":{"node":1,"port":3},"b":{"node":3,"port":1}}'   # R1 ether3 <-> PC1

curl -s -X POST $API/labs/1/start
curl -s $API/labs/1
```

Then open `http://<vm-ip>:8080/` to see the lab, or build the same thing there by hand.

## Reaching routers from your PC

A fresh CHR runs a DHCP client on ether1. Plug ether1 into the management bridge and the
router gets a fixed address (`10.99.1.<node id>`, shown as `mgmt_ip` in the API). Add one
route on your PC, pointing the management network at the lab VM:

```
Windows:  route add 10.99.0.0 mask 255.255.0.0 <vm-ip>
Linux:    sudo ip route add 10.99.0.0/16 via <vm-ip>
```

After that `ssh admin@10.99.1.1`, WinBox and MobaXterm sessions work directly. The VM
source-NATs your traffic onto the bridge, so routers need no default route and your lab's
routing tables stay clean.

## Packet capture

```bash
curl -sN http://<vm-ip>:8080/api/labs/1/nodes/1/ports/2/capture | wireshark -k -i -
```

Add `?seconds=30` for a capture that ends by itself; the workspace's Capture links use that.

## How it fits together

| Piece | Implementation |
|---|---|
| Router | One QEMU/KVM process per CHR, on a qcow2 overlay of the shared base image |
| Client | A network namespace with a veth pair; the console is a shell inside it |
| Port | One host interface per port, named `mb<node>p<port>` |
| Link | A pair of `tc` redirects between the two port interfaces |
| Management | Linux bridge `mbmgmt` with dnsmasq handing out fixed leases |
| Console | QEMU serial socket or a pty, relayed over a websocket |
| Capture | `tcpdump` on the port interface, streamed as pcap |

## Known gaps

- **No login.** Anyone who can reach port 8080 controls the lab, and client consoles are
  root shells on the VM. Keep it on a trusted network.
- Config export, import and hostname push are untested against a real router. They need
  the router running, linked to Management, and its RouterOS login.
- A lab file carries the topology only. Export router configs separately.
- Clients share the VM's filesystem; they are namespaces, not full machines.
- Stopping a router kills QEMU rather than shutting RouterOS down cleanly.
- The free CHR license limits each interface to 1 Mbps upload.

## Development

```bash
pip install -e '.[dev]'
pytest
MIKROBOX_DRY_RUN=1 MIKROBOX_DATA=/tmp/mb MIKROBOX_RUN=/tmp/mbrun python -m mikrobox
```

Dry-run mode logs host commands instead of running them, so the API works on any machine.
