# Mikrobox

A virtual lab for MikroTik CHR routers, run on a Linux VM and driven from the browser.
Think GNS3 or EVE-NG, but only for RouterOS, with the CHR image installed for you.

Open `http://<vm-ip>:8080/` for the lab workspace: add routers and clients, drag them
around, link ports, start and stop devices, and open consoles. The same things are
available through the API (interactive docs at `/docs`).


> **This is the `experimental/aruba-cx` branch.** It adds Aruba AOS-CX switches next to
> MikroTik. The MikroTik build lives on `ui-workspace`. Install this branch **side by side**
> (below) so it never touches your main install or its labs.

## Installing the test build side by side

A second install gets its own folders, service, port, accounts, labs, images and host
network names, so both run at the same time without seeing each other.

```bash
git clone -b experimental/aruba-cx https://github.com/Langeboi/Mikrobox Mikrobox-test
cd Mikrobox-test
sudo ./scripts/install.sh --name test --port 8081
```

Then open `http://<vm-ip>:8081/` and create an account (it has its own). A purple
**Test build** label in the top bar shows which install you are in.

| | Main install | Test install |
|---|---|---|
| Web | port 8080 | port 8081 |
| Service | `mikrobox` | `mikrobox-test` |
| Program, data | `/opt/mikrobox`, `/var/lib/mikrobox` | `/opt/mikrobox-test`, `/var/lib/mikrobox-test` |
| Host names | `mb...`, bridge `mbcloud`, table `mikrobox` | `mx...`, bridge `mxcloud`, table `mikrobox_mx` |
| Cloud | NAT `10.99.0.0/16`, or bridged to your LAN | NAT `10.98.0.0/16` only |

- **Updating:** `cd Mikrobox-test && git pull && sudo ./scripts/install.sh`. The folder
  remembers that it belongs to the test install, so this never touches the main one.
- **Removing:** `sudo ./scripts/uninstall.sh --name test` stops its devices and deletes
  everything it created. It refuses to remove the main install.
- **Safety net:** running a plain `sudo ./scripts/install.sh` from a folder on an
  experimental branch that is not a named install stops and asks before replacing the
  main install.
- **Lab files:** you can export a lab from the main install and import it into the test
  one. The other way round only works for labs without Aruba switches.
- **Memory sharing (KSM)** is a kernel-wide setting, so the test install switching it on
  helps the main install's devices too.
- Both installs share the VM's CPU and memory, and each counts only its own devices
  towards its 128-device limit.

## Experimental: Aruba AOS-CX switches

Aruba CX switches run the official **AOS-CX Switch Simulator**, the real AOS-CX operating
system as a VM. It does VLANs, LAGs, VSX, OSPF, BGP, VRF and more, and has the same CLI,
web interface and REST API as the hardware. It is not fully feature-identical to the
hardware: check HPE's Feature Navigator (platform "CX Simulator") and the simulator
release notes before relying on a feature. VSF stacking is not supported in the simulator.

The image cannot be shipped with Mikrobox. Download it yourself, free with an HPE account,
from the HPE Networking Support Portal (it comes as an `.ova` inside a `.zip`). Then either:

- in the workspace: **Settings, Images, Aruba AOS-CX, add**, and pick the `.zip`; or
- on the server: `sudo /opt/mikrobox/venv/bin/python -m mikrobox.images add aoscx ArubaOS-CX_<version>_ova.zip`

`.ova`, `.vmdk` and `.qcow2` files are accepted too. The disk is converted to qcow2 and
stored as `/var/lib/mikrobox/images/aoscx.qcow2`. Replacing it later resets every CX switch
to factory defaults.

## Choosing an operating system

**Router** and **Switch** in the toolbar pick the role; the dialog then asks for the
operating system. MikroTik CHR can be a router or a switch; Aruba AOS-CX is a switch only.
The dialog remembers your last choice for each role. Through the API, `kind` is the OS
(`chr`, `aoscx`, `client`) and `role` is `router` or `switch`.

## Sharing memory between devices (KSM)

Mikrobox switches on the kernel's same-page merging when it starts, so devices booted from
the same image share the memory they have in common. The savings show in the top bar
("GB shared") and in Settings, Memory, where it can be switched off. It costs some CPU
while scanning, and savings build up over a few minutes after devices boot. How much it
saves depends on the devices and what they are doing.

How Aruba CX runs:

| | |
|---|---|
| Ports | The first is `mgmt` (out of band); the rest are `1/1/1`, `1/1/2`, ... (up to 16) |
| Resources | 1 vCPU and 2 GB RAM by default, HPE's minimum for labs (HPE's own default, 2 vCPUs and 4 GB, is sized for vSphere). Raise it per switch if a feature needs more |
| Hardware | Needs KVM. Boots in a few minutes |
| Login | `admin` with no password; it asks you to set one |
| QEMU | e1000 NICs (virtio has caused TCP problems with the simulator in other labs), IDE disk |

Add one with **Switch** in the toolbar and choose **Aruba AOS-CX** as the operating system.
A small `CX` badge on the tile marks it on the canvas.

Link `mgmt` to the Cloud to SSH to the switch from your PC; the console works either way.
Config export, automatic backups and restore are MikroTik-only for now.

There is no virtual image for HP ProCurve / ArubaOS-Switch (AOS-S) at all. HP Comware
exists only as the HPE VSR1000 virtual router (routing, few switching features) and the
Windows-only H3C Cloud Lab, so neither is included.

## What works in this version

- Labs made of CHR routers and lightweight Linux clients, stored in one JSON file.
- Per router: name, number of ports (1-24, 5 by default), vCPUs and RAM.
- A Switch device: a CHR with 24 ports and its own icon, for when you want one as a switch.
  Choose layer 2 (the default) or layer 3 when adding it, or later in its settings; a layer 3
  switch has its own icon. Either way it is a full CHR: bridging, VLANs and routing between
  them are set up in RouterOS.
- Links between any two ports. Frames pass untouched, so STP, LACP and LLDP/MNDP work.
  Use the Link tool, or hold Ctrl (⌘ on a Mac) and drag from one device to another.
- A Cloud that gives linked devices internet and makes them reachable from your PC,
  either through the VM (NAT) or directly on your own network (bridged).
- A browser workspace with a topology canvas (pan, zoom, snap), device settings and docked consoles.
- Editing: select several things with Shift+click or by Shift+dragging a box (Ctrl+A for
  all), drag them together, align or space them out. Copy and paste (Ctrl+C, Ctrl+V, or
  Ctrl+D to duplicate) brings the links between the copied devices along, pastes at the
  pointer, and works across labs; Ctrl+Shift+V also copies stopped routers' configurations.
  Undo and redo (Ctrl+Z, Ctrl+Shift+Z) cover moves, adding, linking, pasting and deleting.
  Deleted devices are kept for a day, disks included, so undo brings a router back with its
  configuration. The undo history belongs to the browser tab and starts empty on reload.
- Areas, ellipses, text labels and sticky notes to mark sites, routing areas and tasks.
- Lab export and import as a single JSON file (topology, shapes and notes).
- Router config export (one router, or all as a zip), config import, hostname push and
  factory reset, over SSH on the management network.
- Templates: ready-made layouts for multi-area OSPF, a collapsed core, spine and leaf, an
  ISP with MPLS, and BGP multihoming. Topology and a suggested plan, not router configs.
- Clients take a fixed address, gateway and DNS servers, or ask DHCP for all three. Their
  consoles have ping, traceroute, mtr, dig, nmap, curl, iperf3, netcat and arping.
- Client services, switched on per client: a web server on port 80, an iperf3 server, a
  DHCP server (hands out .100-.199 of its subnet) and a DNS server that answers for every
  device in the lab as `<name>.lab` and forwards everything else.
- Docker on Linux clients, switched on per client: each gets its own Docker engine inside
  the client, so containers sit on the lab network and publish ports on the client's
  address. Add containers from the device panel (nginx, Apache, whoami, Portainer,
  Grafana, Uptime Kuma, PostgreSQL or any image) or with `docker` in its console. Ones
  added from the panel are saved with the lab and start with the client; images are
  downloaded through the VM, and a client that has an image shares it with the others.
- Open in your browser: a link from your own browser to any web page inside the lab, over
  HTTP or HTTPS, fetched the way a chosen client sees it. Pick a container's port or type
  any address (a router's WebFig, say). HTTPS passes through untouched, so you see the
  page's own certificate. Links can be for your computer only or for anyone, are listed
  under Tools › Web access, and close when their client stops or after 8 hours unused.
- Viewer tabs (test build): the same pages in a tab of the bottom panel instead, with an
  address bar, reload and a button to pop it out. The viewer fetches HTTPS pages without
  checking their certificate (the tab says so) and lifts the headers that stop pages from
  being shown in a frame, so router pages and dashboards that refuse frames still open.
- A live packet view in the browser (Tools menu, or Packets on any running port), with
  capture filters, alongside pcap download for Wireshark. Clicking a packet shows the
  full protocol tree from tshark and its bytes: pointing at a field highlights its bytes,
  clicking a byte finds its field. Arrow keys step through packets.
- Topology export as a PNG image, device duplication, double-click to open a console.
- Automatic config backups per lab (all routers or chosen ones, every 15 minutes to 24
  hours), kept as revisions only when the config changed, with a side-by-side compare.
- Shared consoles: everyone who opens a device's console sees the same session and can
  type. Badges show who else is in the lab; hover one to see which device they are on.
- Restore a saved revision to a router, replacing its config or applying it on top.
- Documentation export: a zip with a Markdown write-up (devices, cabling, areas, notes),
  the topology image and each router's latest config.
- Connectivity test from a client (Tools menu): ping then traceroute, or a 5 second
  iperf3 throughput run to a client running the iperf3 server.
- Changes appear for everyone in the lab within a moment (pushed over a websocket), and
  an Activity log (Tools menu) records who did what, per lab or across all labs.
- Short animations when a device starts, stops or is added, when a link is drawn and when
  it comes up. Off when your system asks for reduced motion.
- The bottom panel (consoles, packets, tests) can be made taller by dragging its top edge.
- Nuke (Tools menu): empties the current lab in one go (devices, disks, links, shapes,
  notes and saved revisions) after a warning and typing the lab's name. The lab stays.
- Settings: light or dark theme, grid, snapping, port labels, defaults for new routers.
- Browser console for routers (serial port) and clients (shell), over a websocket.
- Live packet capture of any port as a pcap stream you can pipe into Wireshark.
- A hard cap of 128 running devices, and a free-memory check before each router starts.

**Partly tested on real hardware.** Routers, links, the Cloud and consoles have run on a
real VM. The newer parts listed under Known gaps have only been run against stand-ins;
the API, model, limits and generated host commands are covered by unit tests.

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
Updating this way also installs the packages newer features need (tshark for the packet
decode, iperf3, dig and friends for clients, Docker for clients); stopped clients pick them
up on their next start.

Docker is installed for clients but not started on the VM itself: each client with Docker
turned on runs its own engine. If Docker was already running on the VM, it is left as it
was. Skip Docker with `sudo WITH_DOCKER=0 ./scripts/install.sh`.

Links to lab web pages use ports 20000-20999 on the VM (`MIKROBOX_WEB_PORTS=a-b` in the
service's environment changes the range). If the VM has a firewall, open those too.

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
| Capture | `tcpdump` on the port interface, streamed as pcap, or as lines plus bytes for the packet view |
| Decode | `tshark -T pdml` on one packet at a time, run as `nobody` |
| Client services | `udhcpc`, `dnsmasq`, `python3 -m http.server` and `iperf3 -s`, started inside the client's namespace |
| Docker on a client | `dockerd` started inside the client's namespace with a private mount namespace (its own `/sys`, containerd and `resolv.conf`); data in `/var/lib/mikrobox/docker/n<id>`, socket in `/run/mikrobox/docker/n<id>.sock`, which the client's console uses as `DOCKER_HOST` |
| Web links | A port on the VM per link; each connection is made from inside the client's namespace and passed through byte for byte |
| Live updates | A websocket at `/api/events` that tells open workspaces to reload what changed |

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
- The free CHR license limits each interface to 1 Mbps upload, so iperf3 through a router
  shows about 1 Mbit/s. Between two clients on one link it shows the VM's own speed.
- The packet view decodes each packet on its own, so protocols spread over several TCP
  segments are not reassembled there. Download the pcap for that.
- Client services and DHCP were tested with stand-ins for the real programs; the commands
  they run are covered by tests, but expect to fix things on the first run on a real VM.
- Docker on clients and web links have run for real (engines inside clients, containers
  reached across lab links and from a browser), but only on a cgroup v1 test machine
  without internet, so pulling from Docker Hub was tested with a stand-in. Each engine
  uses about 100 MB of memory while its client runs.
- Web links are plain TCP ports: anyone who can reach the VM can use a link made for
  anyone. "Only this computer" goes by IP address.
- Browsers don't keep cookies apart by port, so a lab page opened through a link is sent
  Mikrobox's sign-in cookie too. Two things make that harmless: Mikrobox refuses changes
  and websockets that come from any page but its own, and a sign-in only works from the
  address it was made from (so you sign in again if your computer's address changes).
- If Docker runs on the VM itself, it may set the firewall to drop forwarded traffic,
  which stops the Cloud's NAT; allow `mbcloud` in Docker's `DOCKER-USER` chain.

## Development

```bash
pip install -e '.[dev]'
pytest
MIKROBOX_DRY_RUN=1 MIKROBOX_AUTH=off MIKROBOX_DATA=/tmp/mb MIKROBOX_RUN=/tmp/mbrun python -m mikrobox
```

Dry-run mode logs host commands instead of running them, so the API works on any machine.
