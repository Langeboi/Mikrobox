"""Tests that run without root, KVM or a CHR image (dry-run mode)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mikrobox import __version__
from mikrobox.api import create_app
from mikrobox.config import Settings


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        run_dir=tmp_path / "run",
        dry_run=True,
        sys_net=tmp_path / "sysnet",
        netns_dir=tmp_path / "netns",
        netns_etc=tmp_path / "etcns",
        kvm_device=tmp_path / "kvm",
        web_ports=(42000, 42049),
    )
    (tmp_path / "sysnet").mkdir()
    (tmp_path / "netns").mkdir()
    app = create_app(settings)
    with TestClient(app) as c:
        c.rt = app.state.rt
        c.tmp = tmp_path
        assert c.post("/api/auth/setup", json={"username": "tester", "password": "correct-horse"}).status_code == 200
        c.rt.sh.history[:] = [cmd for cmd in c.rt.sh.history]
        yield c


def make_lab(client) -> int:
    r = client.post("/api/labs", json={"name": "first"})
    assert r.status_code == 201
    return r.json()["id"]


def add_node(client, lab, **body) -> dict:
    r = client.post(f"/api/labs/{lab}/nodes", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_host_setup_creates_management_bridge(client):
    cmds = [" ".join(c) for c in client.rt.sh.history]
    assert "ip link add mbcloud type bridge" in cmds
    assert "ip addr add 10.99.0.1/16 dev mbcloud" in cmds


def test_node_defaults_and_validation(client):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1-core")
    assert r1["kind"] == "chr" and r1["ports"] == 8
    assert r1["ram_mb"] == 256 + 8 * 7
    pc = add_node(client, lab, kind="client", name="PC1", address="192.168.10.2/24",
                  gateway="192.168.10.1")
    assert pc["ports"] == 1

    assert client.post(f"/api/labs/{lab}/nodes", json={"name": "R1-core"}).status_code == 409
    assert client.post(f"/api/labs/{lab}/nodes", json={"name": "bad name"}).status_code == 422
    assert client.post(f"/api/labs/{lab}/nodes",
                       json={"name": "big", "ports": 25}).status_code == 400
    assert client.post(f"/api/labs/{lab}/nodes",
                       json={"kind": "client", "name": "PC2",
                             "address": "192.168.10.2"}).status_code == 400


def test_links_and_management_address(client):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    r2 = add_node(client, lab, name="R2")
    link = {"a": {"node": r1["id"], "port": 2}, "b": {"node": r2["id"], "port": 1}}
    assert client.post(f"/api/labs/{lab}/links", json=link).status_code == 201
    assert client.post(f"/api/labs/{lab}/links", json=link).status_code == 409
    assert client.post(f"/api/labs/{lab}/links",
                       json={"a": {"node": r1["id"], "port": 9}}).status_code == 400

    assert client.post(f"/api/labs/{lab}/links",
                       json={"a": {"node": r1["id"], "port": 1}}).status_code == 201
    view = client.get(f"/api/labs/{lab}").json()
    node = next(n for n in view["nodes"] if n["id"] == r1["id"])
    assert node["mgmt_ip"] == "10.99.1.1"
    assert node["ssh"] == "ssh admin@10.99.1.1"
    hosts = (client.tmp / "run" / "dhcp-hosts").read_text()
    assert hosts == "52:54:00:00:01:01,10.99.1.1,R1\n"

    # Shrinking below a connected port is refused.
    r = client.patch(f"/api/labs/{lab}/nodes/{r1['id']}", json={"ports": 1})
    assert r.status_code == 409


def test_start_router_builds_taps_and_qemu(client):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1", ports=2)
    client.rt.sh.history.clear()
    assert client.post(f"/api/labs/{lab}/nodes/{r1['id']}/start").status_code == 200
    cmds = client.rt.sh.history
    flat = [" ".join(c) for c in cmds]
    assert "ip tuntap add dev mb1p1 mode tap" in flat
    assert "ip tuntap add dev mb1p2 mode tap" in flat
    qemu = next(c for c in cmds if c[0] == "qemu-system-x86_64")
    assert "accel=tcg" in qemu  # no /dev/kvm in the test
    assert qemu[qemu.index("-m") + 1] == "264"
    assert "tap,id=p2,ifname=mb1p2,script=no,downscript=no" in qemu
    assert "virtio-net-pci,netdev=p1,mac=52:54:00:00:01:01" in qemu
    assert flat.index("ip tuntap add dev mb1p2 mode tap") < cmds.index(qemu)


def test_link_between_live_ports_uses_tc_redirect(client, monkeypatch):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    pc = add_node(client, lab, kind="client", name="PC1")
    monkeypatch.setattr(client.rt.net, "exists", lambda iface: True)
    client.rt.sh.history.clear()
    link = {"a": {"node": r1["id"], "port": 3}, "b": {"node": pc["id"], "port": 1}}
    r = client.post(f"/api/labs/{lab}/links", json=link)
    assert r.status_code == 201
    flat = [" ".join(c) for c in client.rt.sh.history]
    assert ("tc filter add dev mb1p3 ingress matchall action mirred egress redirect "
            "dev mb2p1") in flat
    assert ("tc filter add dev mb2p1 ingress matchall action mirred egress redirect "
            "dev mb1p3") in flat

    client.rt.sh.history.clear()
    assert client.delete(f"/api/labs/{lab}/links/{r.json()['id']}").status_code == 204
    flat = [" ".join(c) for c in client.rt.sh.history]
    assert "tc qdisc del dev mb1p3 clsact" in flat


def test_device_limit(client, monkeypatch):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    monkeypatch.setattr(client.rt, "running_count", lambda: 128)
    r = client.post(f"/api/labs/{lab}/nodes/{r1['id']}/start")
    assert r.status_code == 409
    assert "128" in r.json()["detail"]


def test_memory_check(client, monkeypatch):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    monkeypatch.setattr(client.rt, "mem_available_mb", lambda: 300)
    r = client.post(f"/api/labs/{lab}/nodes/{r1['id']}/start")
    assert r.status_code == 409
    assert "memory" in r.json()["detail"]


def test_state_survives_restart(client):
    lab = make_lab(client)
    add_node(client, lab, name="R1")
    from mikrobox.store import Store

    again = Store(client.tmp / "data" / "state.json")
    assert again.state.labs[0].nodes[0].name == "R1"


def test_delete_node_removes_its_links(client):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    r2 = add_node(client, lab, name="R2")
    client.post(f"/api/labs/{lab}/links",
                json={"a": {"node": r1["id"], "port": 1}, "b": {"node": r2["id"], "port": 1}})
    assert client.delete(f"/api/labs/{lab}/nodes/{r2['id']}").status_code == 204
    view = client.get(f"/api/labs/{lab}").json()
    assert view["links"] == [] and len(view["nodes"]) == 1


def test_ui_is_served_and_positions_are_saved(client):
    assert "Mikrobox" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/vendor/xterm.js").status_code == 200
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1", x=300, y=200)
    assert (r1["x"], r1["y"]) == (300, 200)
    r = client.patch(f"/api/labs/{lab}/nodes/{r1['id']}", json={"x": 420.0, "y": 110.0})
    assert r.status_code == 200 and r.json()["x"] == 420
    r = client.patch(f"/api/labs/{lab}", json={"mgmt_x": 150, "name": "renamed"})
    assert r.json()["mgmt_x"] == 150 and r.json()["name"] == "renamed"


def test_running_node_can_move_but_not_change(client, monkeypatch):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    monkeypatch.setattr(client.rt, "node_running", lambda node: True)
    url = f"/api/labs/{lab}/nodes/{r1['id']}"
    assert client.patch(url, json={"x": 10, "y": 20}).status_code == 200
    assert client.patch(url, json={"ports": 4}).status_code == 409


def test_annotations(client):
    lab = make_lab(client)
    r = client.post(f"/api/labs/{lab}/annotations",
                    json={"kind": "rect", "x": 10, "y": 20, "w": 300, "h": 200, "text": "Area 0"})
    assert r.status_code == 201
    aid = r.json()["id"]
    r = client.patch(f"/api/labs/{lab}/annotations/{aid}", json={"color": "green", "w": 400})
    assert r.json()["color"] == "green" and r.json()["w"] == 400 and r.json()["text"] == "Area 0"
    assert client.post(f"/api/labs/{lab}/annotations",
                       json={"kind": "blob", "x": 0, "y": 0, "w": 1, "h": 1}).status_code == 422
    assert len(client.get(f"/api/labs/{lab}").json()["annotations"]) == 1
    assert client.delete(f"/api/labs/{lab}/annotations/{aid}").status_code == 204
    assert client.get(f"/api/labs/{lab}").json()["annotations"] == []


def test_lab_export_import_roundtrip(client):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1", x=100, y=100)
    pc = add_node(client, lab, kind="client", name="PC1", address="10.0.0.2/24")
    client.post(f"/api/labs/{lab}/links",
                json={"a": {"node": r1["id"], "port": 2}, "b": {"node": pc["id"], "port": 1}})
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": r1["id"], "port": 1}})
    client.post(f"/api/labs/{lab}/annotations",
                json={"kind": "note", "x": 0, "y": 0, "w": 100, "h": 80, "text": "hi"})
    exported = client.get(f"/api/labs/{lab}/export")
    assert "first.mikrobox.json" in exported.headers["content-disposition"]
    r = client.post("/api/labs/import", json=exported.json())
    assert r.status_code == 201, r.text
    copy = r.json()
    assert copy["name"] == "first-2" and copy["id"] != lab
    assert [n["name"] for n in copy["nodes"]] == ["R1", "PC1"]
    assert {n["id"] for n in copy["nodes"]}.isdisjoint({r1["id"], pc["id"]})
    assert len(copy["links"]) == 2 and copy["annotations"][0]["text"] == "hi"
    new_r1 = copy["nodes"][0]
    assert copy["links"][0]["a"]["node"] == new_r1["id"]
    assert new_r1["mgmt_ip"] is not None
    bad = exported.json()
    bad["links"][0]["a"]["node"] = 999
    assert client.post("/api/labs/import", json=bad).status_code == 400


def test_config_export_and_import(client, monkeypatch):
    from mikrobox import routeros

    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    url = f"/api/labs/{lab}/nodes/{r1['id']}"
    assert client.post(url + "/config/export", json={}).status_code == 409  # stopped
    monkeypatch.setattr(client.rt, "node_running", lambda node: True)
    assert client.post(url + "/config/export", json={}).status_code == 409  # no management
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": r1["id"], "port": 1}})

    calls = []
    monkeypatch.setattr(routeros, "export_config",
                        lambda host, u, p, s=False: calls.append((host, u, p, s)) or "/ip address\n")
    monkeypatch.setattr(routeros, "import_config",
                        lambda host, u, p, cfg: calls.append((host, cfg)) or "ok")
    monkeypatch.setattr(routeros, "set_identity",
                        lambda host, u, p, name: calls.append((host, name)) or "")
    r = client.post(url + "/config/export", json={"password": "x", "sensitive": True})
    assert r.status_code == 200 and r.text == "/ip address\n"
    assert calls[-1] == ("10.99.1.1", "admin", "x", True)
    assert "R1.txt" in r.headers["content-disposition"]
    r = client.post(url + "/config/import", json={"config": "/system identity set name=x"})
    assert r.json() == {"output": "ok"}
    assert client.post(url + "/identity", json={}).status_code == 200
    assert calls[-1] == ("10.99.1.1", "R1")

    add_node(client, lab, name="R2")  # not on management: skipped in the zip
    r = client.post(f"/api/labs/{lab}/config/export", json={})
    assert r.status_code == 200 and r.headers["x-mikrobox-skipped"] == "1"
    import io, zipfile
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert names == ["R1.txt", "SKIPPED.txt"]

    def boom(*a, **k):
        raise routeros.RouterError("Login refused")
    monkeypatch.setattr(routeros, "export_config", boom)
    assert client.post(url + "/config/export", json={}).status_code == 502


def test_factory_reset_needs_stopped_router(client, monkeypatch):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    url = f"/api/labs/{lab}/nodes/{r1['id']}/reset"
    assert client.post(url).status_code == 200
    monkeypatch.setattr(client.rt, "node_running", lambda node: True)
    assert client.post(url).status_code == 409


def test_cloud_nat_mode_reports_private_network(client):
    info = client.get("/api/system").json()["cloud"]
    assert info == {"mode": "nat", "bridge": "mbcloud", "uplink": None,
                    "network": "10.99.0.0/16", "gateway": "10.99.0.1"}
    nft = [c for c in client.rt.sh.history if c[0] == "nft"]
    assert nft, "NAT rules are loaded"


def test_cloud_bridged_mode(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", run_dir=tmp_path / "r", dry_run=True,
                        sys_net=tmp_path / "sys", netns_dir=tmp_path / "ns",
                        kvm_device=tmp_path / "kvm", cloud_mode="bridged", auth_enabled=False)
    (tmp_path / "sys").mkdir()
    app = create_app(settings)
    with TestClient(app) as c:
        rt = app.state.rt
        assert not any("add" in cmd and "bridge" in cmd for cmd in rt.sh.history)
        assert c.get("/api/system").json()["cloud"]["mode"] == "bridged"
        lab = c.post("/api/labs", json={"name": "b"}).json()["id"]
        r1 = c.post(f"/api/labs/{lab}/nodes", json={"name": "R1"}).json()
        c.post(f"/api/labs/{lab}/links", json={"a": {"node": r1["id"], "port": 1}})
        node = c.get(f"/api/labs/{lab}").json()["nodes"][0]
        assert node["cloud_port"] == 1 and node["mgmt_ip"] is None
        assert not (tmp_path / "r" / "dhcp-hosts").exists()
        rt.cloud_ips = {"52:54:00:00:01:01": "192.168.1.57"}
        node = c.get(f"/api/labs/{lab}").json()["nodes"][0]
        assert node["mgmt_ip"] == "192.168.1.57" and node["ssh"] == "ssh admin@192.168.1.57"


def test_config_tools_accept_a_typed_address(client, monkeypatch):
    from mikrobox import routeros

    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    monkeypatch.setattr(client.rt, "node_running", lambda node: True)
    seen = []
    monkeypatch.setattr(routeros, "export_config",
                        lambda host, u, p, s=False: seen.append(host) or "cfg")
    url = f"/api/labs/{lab}/nodes/{r1['id']}/config/export"
    assert client.post(url, json={"host": "192.168.1.80"}).status_code == 200
    assert seen == ["192.168.1.80"]
    assert client.post(url, json={"host": "not-an-ip"}).status_code == 400


def test_cloudsetup_rewrites_ifupdown():
    from mikrobox.cloudsetup import Unsupported, rewrite_interfaces

    old = """source /etc/network/interfaces.d/*

auto lo
iface lo inet loopback

allow-hotplug ens18
iface ens18 inet static
    address 192.168.1.50/24
    gateway 192.168.1.1
iface ens18 inet6 auto
"""
    new = rewrite_interfaces(old, "ens18", "bc:24:11:aa:bb:cc")
    assert "allow-hotplug ens18" not in new
    assert "iface ens18 inet manual" in new
    block = new.split("auto mbcloud\n")[1]
    assert block.startswith("iface mbcloud inet static\n    bridge_ports ens18\n")
    assert "bridge_hw bc:24:11:aa:bb:cc" in block
    # The address lines still follow, so they now belong to the bridge.
    assert block.index("bridge_hw") < block.index("address 192.168.1.50/24") < block.index("gateway")
    assert "iface mbcloud inet6 auto" in new and "iface lo inet loopback" in new
    with pytest.raises(Unsupported):
        rewrite_interfaces(old, "eth9", "00:00:00:00:00:00")


def test_cloudsetup_rewrites_netplan():
    from mikrobox.cloudsetup import rewrite_netplan

    doc = {"network": {"version": 2, "ethernets": {"ens18": {
        "dhcp4": True, "match": {"macaddress": "bc:24:11:aa:bb:cc"}, "set-name": "ens18"}}}}
    new = rewrite_netplan(doc, "ens18", "bc:24:11:aa:bb:cc")
    eth = new["network"]["ethernets"]["ens18"]
    assert eth == {"match": {"macaddress": "bc:24:11:aa:bb:cc"}, "set-name": "ens18",
                   "dhcp4": False, "dhcp6": False}
    br = new["network"]["bridges"]["mbcloud"]
    assert br["interfaces"] == ["ens18"] and br["dhcp4"] is True
    assert br["macaddress"] == "bc:24:11:aa:bb:cc" and br["dhcp-identifier"] == "mac"
    assert br["parameters"] == {"stp": False, "forward-delay": 0}

    static = {"network": {"ethernets": {"eth0": {
        "addresses": ["10.0.0.5/24"], "routes": [{"to": "default", "via": "10.0.0.1"}],
        "nameservers": {"addresses": ["10.0.0.1"]}}}}}
    br = rewrite_netplan(static, "eth0", "aa:aa:aa:aa:aa:aa")["network"]["bridges"]["mbcloud"]
    assert br["addresses"] == ["10.0.0.5/24"] and br["routes"][0]["via"] == "10.0.0.1"
    assert "dhcp-identifier" not in br
    assert rewrite_netplan({"network": {"ethernets": {"eth1": {}}}}, "eth0", "aa:aa:aa:aa:aa:aa") is None


def test_area_style_defaults_and_changes(client):
    lab = make_lab(client)
    a = client.post(f"/api/labs/{lab}/annotations",
                    json={"kind": "rect", "x": 0, "y": 0, "w": 200, "h": 100}).json()
    assert (a["fill"], a["border"], a["border_width"], a["radius"], a["label_pos"], a["label_size"]) \
        == ("light", "dashed", 1.5, 12, "auto", "m")
    r = client.patch(f"/api/labs/{lab}/annotations/{a['id']}",
                     json={"fill": "strong", "border": "solid", "border_width": 3, "radius": 0,
                           "label_pos": "center", "label_size": "l"})
    assert r.status_code == 200 and r.json()["fill"] == "strong" and r.json()["radius"] == 0
    assert client.patch(f"/api/labs/{lab}/annotations/{a['id']}",
                        json={"border": "wavy"}).status_code == 422
    assert client.patch(f"/api/labs/{lab}/annotations/{a['id']}",
                        json={"border_width": 50}).status_code == 400


def test_duplicate_node(client, monkeypatch):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1", ports=4, cpus=2, x=100, y=100)
    r2 = add_node(client, lab, name="R2")
    client.post(f"/api/labs/{lab}/links",
                json={"a": {"node": r1["id"], "port": 1}, "b": {"node": r2["id"], "port": 1}})
    url = f"/api/labs/{lab}/nodes/{r1['id']}/duplicate"
    assert client.post(url, json={"name": "R2"}).status_code == 409
    r = client.post(url, json={"name": "R3"})
    assert r.status_code == 201
    copy = r.json()
    assert (copy["ports"], copy["cpus"], copy["ram_mb"]) == (4, 2, r1["ram_mb"])
    assert (copy["x"], copy["y"]) == (160, 160) and copy["id"] not in (r1["id"], r2["id"])
    assert copy["config_copied"] is False
    view = client.get(f"/api/labs/{lab}").json()
    assert len(view["links"]) == 1 and not any(
        e and e["node"] == copy["id"] for l in view["links"] for e in (l["a"], l["b"]))

    disk = client.rt.disk_path(client.rt.store.state.lab(lab).node(r1["id"]))
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(b"router-config")
    r = client.post(url, json={"name": "R4", "copy_config": True})
    assert r.json()["config_copied"] is True
    new = client.rt.store.state.lab(lab).node(r.json()["id"])
    assert client.rt.disk_path(new).read_bytes() == b"router-config"
    monkeypatch.setattr(client.rt, "node_running", lambda node: True)
    assert client.post(url, json={"name": "R5", "copy_config": True}).status_code == 409

    pc = add_node(client, lab, kind="client", name="PC1", address="10.0.0.2/24", gateway="10.0.0.1")
    dup = client.post(f"/api/labs/{lab}/nodes/{pc['id']}/duplicate", json={"name": "PC2"}).json()
    assert dup["address"] is None and dup["gateway"] == "10.0.0.1"


def test_packet_lines_are_parsed():
    from mikrobox.packets import parse_line

    icmp = parse_line("22:43:01.123456 52:54:00:00:01:02 > 52:54:00:00:02:01, ethertype IPv4 "
                      "(0x0800), length 98: 10.0.12.1 > 10.0.12.2: ICMP echo request, id 1, seq 1, length 64")
    assert (icmp["src"], icmp["dst"], icmp["proto"], icmp["length"]) == ("10.0.12.1", "10.0.12.2", "ICMP", 98)
    assert icmp["info"].startswith("ICMP echo request") and icmp["time"] == "22:43:01.123456"

    bgp = parse_line("22:43:02.000001 52:54:00:00:01:02 > 52:54:00:00:02:01, ethertype IPv4 (0x0800), "
                     "length 85: 10.0.12.1.179 > 10.0.12.2.41234: Flags [P.], seq 1:20, ack 1, win 501, length 19: BGP")
    assert (bgp["src"], bgp["dst"], bgp["proto"]) == ("10.0.12.1", "10.0.12.2", "BGP")
    assert bgp["info"].startswith("179 → 41234")

    ospf = parse_line("22:43:03.5 52:54:00:00:01:02 > 01:00:5e:00:00:05, ethertype IPv4 (0x0800), "
                      "length 78: 10.0.12.1 > 224.0.0.5: OSPFv2, Hello, length 44")
    assert ospf["proto"] == "OSPF" and ospf["dst"] == "224.0.0.5"

    arp = parse_line("22:43:04.1 52:54:00:00:01:02 > ff:ff:ff:ff:ff:ff, ethertype ARP (0x0806), "
                     "length 42: Request who-has 10.0.12.2 tell 10.0.12.1, length 28")
    assert (arp["proto"], arp["src"], arp["dst"]) == ("ARP", "10.0.12.1", "10.0.12.2")

    mndp = parse_line("22:43:05.1 52:54:00:00:01:02 > ff:ff:ff:ff:ff:ff, ethertype IPv4 (0x0800), "
                      "length 150: 10.0.12.1.5678 > 255.255.255.255.5678: UDP, length 108")
    assert mndp["proto"] == "MNDP"

    tagged = parse_line("22:43:06.1 52:54:00:00:01:02 > 52:54:00:00:02:01, ethertype 802.1Q (0x8100), length 102: "
                        "vlan 10, p 0, ethertype IPv4 (0x0800), 192.168.10.1 > 192.168.10.2: ICMP echo reply, id 1, seq 1, length 64")
    assert tagged["vlan"] == 10 and tagged["proto"] == "ICMP" and tagged["info"].startswith("VLAN 10")

    v6 = parse_line("22:43:07.1 52:54:00:00:01:02 > 33:33:00:00:00:05, ethertype IPv6 (0x86dd), "
                    "length 90: fe80::1 > ff02::5: OSPFv3, Hello, length 36")
    assert (v6["src"], v6["dst"], v6["proto"]) == ("fe80::1", "ff02::5", "OSPF")

    stp = parse_line("22:43:08.1 52:54:00:00:01:02 > 01:80:c2:00:00:00, 802.3, length 53: LLC, dsap STP (0x42) "
                     "Individual, ssap STP (0x42) Command, ctrl 0x03: STP 802.1w, Rapid STP, Flags [Learn]")
    assert stp["proto"] == "STP" and stp["src"] == "52:54:00:00:01:02"
    assert parse_line("tcpdump: listening on mb1p1") is None


def test_packet_view_refuses_a_port_that_is_down(client):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    with client.websocket_connect(f"/api/labs/{lab}/nodes/{r1['id']}/ports/1/packets") as ws:
        assert "not up" in ws.receive_json()["error"]


def test_switch_is_a_chr_with_24_ports(client):
    lab = make_lab(client)
    sw = add_node(client, lab, name="SW1", role="switch")
    assert (sw["kind"], sw["role"], sw["ports"], sw["ram_mb"]) == ("chr", "switch", 24, 256 + 8 * 23)
    assert add_node(client, lab, name="R1")["role"] == "router"
    assert client.post(f"/api/labs/{lab}/nodes",
                       json={"kind": "client", "role": "switch", "name": "PC1"}).status_code == 400
    client.rt.sh.history.clear()
    assert client.post(f"/api/labs/{lab}/nodes/{sw['id']}/start").status_code == 200
    qemu = next(c for c in client.rt.sh.history if c[0] == "qemu-system-x86_64")
    assert sum(1 for arg in qemu if arg.startswith("virtio-net-pci")) == 24
    dup = client.post(f"/api/labs/{lab}/nodes/{sw['id']}/duplicate", json={"name": "SW2"}).json()
    assert dup["role"] == "switch" and dup["ports"] == 24


def test_client_dns(client):
    lab = make_lab(client)
    pc = add_node(client, lab, kind="client", name="PC1", address="10.0.0.2/24",
                  gateway="10.0.0.1", dns="1.1.1.1 9.9.9.9")
    assert pc["dns"] == "1.1.1.1, 9.9.9.9"
    assert client.post(f"/api/labs/{lab}/nodes",
                       json={"kind": "client", "name": "PC2", "dns": "example.com"}).status_code == 400
    assert client.post(f"/api/labs/{lab}/nodes", json={"name": "R1", "dns": "1.1.1.1"}).status_code == 400
    assert client.post(f"/api/labs/{lab}/nodes/{pc['id']}/start").status_code == 200
    conf = client.tmp / "etcns" / f"mbc{pc['id']}" / "resolv.conf"
    assert conf.read_text() == "nameserver 1.1.1.1\nnameserver 9.9.9.9\n"
    r = client.patch(f"/api/labs/{lab}/nodes/{pc['id']}", json={"dns": None})
    assert r.json()["dns"] is None
    client.post(f"/api/labs/{lab}/nodes/{pc['id']}/stop")
    assert not conf.exists()


def test_templates_build_valid_labs(client):
    listed = client.get("/api/templates").json()
    assert [t["id"] for t in listed] == ["ospf-multi-area", "collapsed-core", "spine-leaf",
                                          "isp-mpls", "bgp-multihoming"]
    for t in listed:
        r = client.post(f"/api/templates/{t['id']}", json={})
        assert r.status_code == 201, r.text
        lab = r.json()
        assert lab["name"] == t["id"] and len(lab["nodes"]) == t["devices"]
        names = [n["name"] for n in lab["nodes"]]
        assert len(set(names)) == len(names)
        ports = {n["id"]: n["ports"] for n in lab["nodes"]}
        seen = set()
        for link in lab["links"]:
            for end in (link["a"], link["b"]):
                if end:
                    assert 1 <= end["port"] <= ports[end["node"]]
                    assert (end["node"], end["port"]) not in seen
                    seen.add((end["node"], end["port"]))
        # ether1 is kept for the Cloud on every router.
        chr_ids = {n["id"] for n in lab["nodes"] if n["kind"] == "chr"}
        assert not any(l["b"] and e["node"] in chr_ids and e["port"] == 1
                       for l in lab["links"] for e in (l["a"], l["b"]))
        assert lab["annotations"] and any(a["kind"] == "note" for a in lab["annotations"])
    # Using one twice gives a second lab with a new name.
    assert client.post("/api/templates/spine-leaf", json={}).json()["name"] == "spine-leaf-2"
    assert client.post("/api/templates/spine-leaf", json={"name": "dc1"}).json()["name"] == "dc1"
    assert client.post("/api/templates/nope", json={}).status_code == 404
    core = next(l for l in client.get("/api/labs").json() if l["name"] == "collapsed-core")
    assert sum(1 for n in core["nodes"] if n["role"] == "switch") == 3
    assert any(l["b"] is None for l in core["links"])


def test_everything_needs_an_account(client):
    anon = TestClient(client.app)
    assert anon.get("/api/labs").status_code == 401
    assert anon.get("/api/system").status_code == 401
    assert anon.post("/api/labs", json={"name": "x"}).status_code == 401
    assert anon.get("/static/app.js").status_code == 200
    assert anon.get("/login").status_code == 200
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert anon.get("/docs", follow_redirects=False).status_code == 302
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect("/api/labs/1/nodes/1/console"):
            pass
    assert anon.get("/api/auth/status").json() == {"enabled": True, "setup_needed": False, "user": None,
                                                   "version": __version__, "min_password": 8}


def test_login_logout_and_accounts(client):
    assert client.get("/api/auth/status").json()["user"] == "tester"
    # The first-account route closes once an account exists.
    assert client.post("/api/auth/setup", json={"username": "eve", "password": "long-enough"}).status_code == 409

    anon = TestClient(client.app)
    assert anon.post("/api/auth/login", json={"username": "tester", "password": "nope"}).status_code == 401
    assert anon.post("/api/auth/login", json={"username": "ghost", "password": "correct-horse"}).status_code == 401
    r = anon.post("/api/auth/login", json={"username": "TESTER", "password": "correct-horse"})
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    assert anon.get("/api/labs").status_code == 200
    assert anon.post("/api/auth/logout").status_code == 200
    assert anon.get("/api/labs").status_code == 401

    assert client.post("/api/users", json={"username": "anna", "password": "short"}).status_code == 400
    assert client.post("/api/users", json={"username": "bad name", "password": "long-enough"}).status_code == 400
    assert client.post("/api/users", json={"username": "anna", "password": "long-enough"}).status_code == 201
    assert client.post("/api/users", json={"username": "Anna", "password": "long-enough"}).status_code == 400
    assert client.get("/api/users").json() == {"users": ["anna", "tester"], "me": "tester"}
    assert client.delete("/api/users/tester").status_code == 409
    assert client.delete("/api/users/anna").status_code == 204

    assert client.post("/api/account/password", json={"current": "wrong", "new": "another-one"}).status_code == 400
    assert client.post("/api/account/password", json={"current": "correct-horse", "new": "another-one"}).status_code == 200
    assert anon.post("/api/auth/login", json={"username": "tester", "password": "another-one"}).status_code == 200
    # Passwords are not stored in the clear.
    stored = (client.tmp / "data" / "users.json").read_text()
    assert "another-one" not in stored and "correct-horse" not in stored


def test_basic_auth_for_scripts_and_throttling(client):
    anon = TestClient(client.app)
    assert anon.get("/api/labs", auth=("tester", "correct-horse")).status_code == 200
    assert anon.get("/api/labs", auth=("tester", "wrong")).status_code == 401
    for _ in range(8):
        anon.post("/api/auth/login", json={"username": "tester", "password": "wrong"})
    r = anon.post("/api/auth/login", json={"username": "tester", "password": "correct-horse"})
    assert r.status_code == 400 and "Too many" in r.json()["detail"]


def test_automatic_backups_and_revisions(client, monkeypatch):
    from mikrobox import routeros

    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    r2 = add_node(client, lab, name="R2")
    pc = add_node(client, lab, kind="client", name="PC1")
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": r1["id"], "port": 1}})
    base = f"/api/labs/{lab}"

    view = client.get(base).json()
    assert view["backup"]["enabled"] is False and view["backup"]["interval"] == 60
    assert view["nodes"][0]["backup"] == {"included": True, "revisions": 0, "last": None}
    assert view["nodes"][2]["backup"] is None  # clients have no config

    r = client.put(base + "/backup", json={"enabled": True, "username": "admin", "password": "s3cret-pw"})
    assert r.json()["has_password"] is True and "password" not in r.json()
    assert "s3cret-pw" not in client.get(base).text
    assert client.put(base + "/backup", json={"interval": 7}).status_code == 400
    assert oct((client.tmp / "data" / "backup.json").stat().st_mode)[-3:] == "600"

    config = {"text": "# 2026-11-17 00:30:00 by RouterOS 7.20\n/ip address\nadd address=10.0.0.1/24 interface=ether2\n"}
    seen = []
    monkeypatch.setattr(routeros, "export_config",
                        lambda host, u, p, s=False: seen.append((host, u, p)) or config["text"])
    monkeypatch.setattr(client.rt, "node_running", lambda node: True)

    results = client.post(base + "/backup/run", json={}).json()["results"]
    by_name = {r["name"]: r for r in results}
    assert set(by_name) == {"R1", "R2"}
    assert by_name["R1"]["ok"] and by_name["R1"]["changed"] and seen == [("10.99.1.1", "admin", "s3cret-pw")]
    assert not by_name["R2"]["ok"] and "Stop and start this router once" in by_name["R2"]["message"]

    # Same config with a new timestamp is not a new revision.
    import time as _time
    config["text"] = config["text"].replace("00:30:00", "01:30:00")
    again = client.post(base + "/backup/run", json={"node": r1["id"]}).json()["results"][0]
    assert again["ok"] and not again["changed"] and again["message"] == "No changes"
    revs = client.get(f"{base}/nodes/{r1['id']}/revisions").json()["revisions"]
    assert len(revs) == 1

    _time.sleep(1.1)
    config["text"] += "/ip route\nadd gateway=10.0.0.254\n"
    config["text"] = config["text"].replace("10.0.0.1/24", "10.0.0.2/24")
    assert client.post(base + "/backup/run", json={"node": r1["id"]}).json()["results"][0]["changed"]
    revs = client.get(f"{base}/nodes/{r1['id']}/revisions").json()["revisions"]
    assert len(revs) == 2 and revs[0]["id"] > revs[1]["id"]  # newest first

    d = client.get(f"{base}/nodes/{r1['id']}/revisions/diff", params={"a": revs[1]["id"], "b": revs[0]["id"]}).json()
    assert (d["added"], d["removed"]) == (3, 1)
    assert {"t": "-", "s": "add address=10.0.0.1/24 interface=ether2"} in d["lines"]
    assert {"t": "+", "s": "add gateway=10.0.0.254"} in d["lines"]
    assert not any("by RouterOS" in line["s"] for line in d["lines"])

    r = client.get(f"{base}/nodes/{r1['id']}/revisions/{revs[0]['id']}")
    assert "10.0.0.254" in r.text and f"R1-{revs[0]['id']}.txt" in r.headers["content-disposition"]
    assert client.get(f"{base}/nodes/{r1['id']}/revisions/..%2Fbackup").status_code == 404
    assert client.get(f"{base}/nodes/{r1['id']}/revisions/diff", params={"a": "x", "b": "y"}).status_code == 404

    # Only chosen routers.
    client.put(base + "/backup", json={"scope": "selected"})
    assert client.post(base + "/backup/run", json={}).json()["results"] == []
    client.put(f"{base}/nodes/{r1['id']}/backup", json={"included": True})
    assert [r["name"] for r in client.post(base + "/backup/run", json={}).json()["results"]] == ["R1"]
    assert client.post(base + "/backup/run", json={"node": pc["id"]}).status_code == 400

    def refuse(*a, **k):
        raise routeros.RouterError("Login to 10.99.1.1 as admin was refused. Check the password.")
    monkeypatch.setattr(routeros, "export_config", refuse)
    failed = client.post(base + "/backup/run", json={"node": r1["id"]}).json()["results"][0]
    assert not failed["ok"] and "refused" in failed["message"]
    assert client.get(base).json()["nodes"][0]["backup"]["last"]["ok"] is False

    monkeypatch.undo()
    assert client.delete(f"{base}/nodes/{r1['id']}").status_code == 204
    assert not (client.tmp / "data" / "backups" / f"n{r1['id']}").exists()


def _fake_agent(path, output, refuse=False):
    """A stand-in for CHR's guest agent on a unix socket."""
    import base64, json, socket, threading

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)
    seen = []

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            f = conn.makefile("rwb")
            polls = 0
            for line in f:
                msg = json.loads(line.strip().lstrip(b"\xff"))
                cmd, args = msg["execute"], msg.get("arguments", {})
                if cmd == "guest-sync":
                    reply = {"return": args["id"]}
                elif cmd == "guest-exec":
                    seen.append(base64.b64decode(args["input-data"]).decode())
                    reply = {"error": {"class": "CommandDisabled", "desc": "guest-exec is disabled"}} if refuse \
                        else {"return": {"pid": 7}}
                else:
                    polls += 1
                    reply = {"return": {"exited": False}} if polls < 2 else \
                        {"return": {"exited": True, "exitcode": 0,
                                    "out-data": base64.b64encode(output.encode()).decode()}}
                f.write(json.dumps(reply).encode() + b"\n")
                f.flush()
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return server, seen


def test_backup_without_cloud_link_uses_the_guest_agent(client, monkeypatch):
    from mikrobox import routeros

    lab = make_lab(client)
    leaf = add_node(client, lab, name="LEAF1")  # no link to the Cloud at all
    node = client.rt.store.state.lab(lab).node(leaf["id"])
    qemu = client.rt.qemu_command(node)
    assert "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0" in qemu
    monkeypatch.setattr(client.rt, "node_running", lambda n: True)
    monkeypatch.setattr(routeros, "export_config", lambda *a, **k: pytest.fail("SSH must not be used"))

    client.rt.s.run_dir.mkdir(parents=True, exist_ok=True)
    config = "# 2026-11-17 00:40:00 by RouterOS 7.20\n/system identity\nset name=LEAF1\n"
    server, seen = _fake_agent(client.rt.agent_socket(node), config)
    try:
        r = client.post(f"/api/labs/{lab}/backup/run", json={"node": leaf["id"]}).json()["results"][0]
        assert r["ok"] and r["changed"] and r["via"] == "agent", r
        assert seen == ["/export;"]
        rev = client.get(f"/api/labs/{lab}/nodes/{leaf['id']}/revisions").json()["revisions"][0]
        assert "set name=LEAF1" in client.get(f"/api/labs/{lab}/nodes/{leaf['id']}/revisions/{rev['id']}").text
        client.put(f"/api/labs/{lab}/backup", json={"sensitive": True})
        client.post(f"/api/labs/{lab}/backup/run", json={"node": leaf["id"]})
        assert seen[-1] == "/export show-sensitive;"
    finally:
        server.close()
        client.rt.agent_socket(node).unlink()


def test_backup_falls_back_to_ssh_when_the_agent_refuses(client, monkeypatch):
    from mikrobox import routeros

    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    r2 = add_node(client, lab, name="R2")
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": r1["id"], "port": 1}})
    monkeypatch.setattr(client.rt, "node_running", lambda n: True)
    monkeypatch.setattr(routeros, "export_config", lambda *a, **k: "/ip address\n")
    client.rt.s.run_dir.mkdir(parents=True, exist_ok=True)
    servers = []
    for n in (r1, r2):
        node = client.rt.store.state.lab(lab).node(n["id"])
        servers.append((_fake_agent(client.rt.agent_socket(node), "", refuse=True)[0], client.rt.agent_socket(node)))
    try:
        results = {r["name"]: r for r in client.post(f"/api/labs/{lab}/backup/run", json={}).json()["results"]}
        assert results["R1"]["ok"] and results["R1"]["via"] == "ssh"
        assert not results["R2"]["ok"] and "guest-exec is disabled" in results["R2"]["message"]
    finally:
        for server, path in servers:
            server.close()
            path.unlink()


def test_presence_lists_other_people(client):
    a = client.post("/api/presence", json={"client": "tab-aaaa", "lab": 1, "node": 3}).json()
    assert a == {"others": []}
    client.post("/api/users", json={"username": "anna", "password": "long-enough"})
    anna = TestClient(client.app)
    anna.post("/api/auth/login", json={"username": "anna", "password": "long-enough"})
    b = anna.post("/api/presence", json={"client": "tab-bbbb", "lab": 1}).json()
    assert b["others"] == [{"user": "tester", "lab": 1, "node": 3}]
    a = client.post("/api/presence", json={"client": "tab-aaaa", "lab": 1}).json()
    assert a["others"] == [{"user": "anna", "lab": 1, "node": None}]
    assert anna.post("/api/presence/leave", json={"client": "tab-bbbb"}).status_code == 204
    assert client.post("/api/presence", json={"client": "tab-aaaa", "lab": 1}).json()["others"] == []


def test_console_is_shared_between_viewers(client, monkeypatch):
    """Two people on one router console see the same output, and both can type."""
    import socket, threading

    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    node = client.rt.store.state.lab(lab).node(r1["id"])
    monkeypatch.setattr(client.rt, "node_running", lambda n: True)
    client.rt.s.run_dir.mkdir(parents=True, exist_ok=True)
    path = client.rt.serial_socket(node)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    accepted, typed = [], []

    def router():
        conn, _ = server.accept()
        accepted.append(conn)
        conn.sendall(b"MikroTik Login: ")
        while True:
            data = conn.recv(100)
            if not data:
                return
            typed.append(data)
            conn.sendall(b"echo:" + data)

    threading.Thread(target=router, daemon=True).start()
    url = f"/api/labs/{lab}/nodes/{r1['id']}/console"

    def read_until(ws, want_bytes=None, want_viewers=None):
        got = b""
        for _ in range(20):
            msg = ws.receive()
            if msg.get("bytes"):
                got += msg["bytes"]
                if want_bytes and want_bytes in got:
                    return got
            elif msg.get("text", "").startswith("\x01"):
                import json
                viewers = json.loads(msg["text"][1:])["viewers"]
                if want_viewers and viewers == want_viewers:
                    return viewers
        raise AssertionError(f"did not see {want_bytes or want_viewers}; got {got!r}")

    try:
        with client.websocket_connect(url) as first:
            read_until(first, want_bytes=b"MikroTik Login: ")
            with client.websocket_connect(url) as second:
                # The second viewer gets the earlier output replayed, over the same single connection.
                read_until(second, want_bytes=b"MikroTik Login: ")
                read_until(first, want_viewers=["tester", "tester"])
                second.send_bytes(b"admin")
                read_until(first, want_bytes=b"echo:admin")
                read_until(second, want_bytes=b"echo:admin")
                first.send_bytes(b"hello")
                read_until(second, want_bytes=b"echo:hello")
            read_until(first, want_viewers=["tester"])
        assert len(accepted) == 1 and b"".join(typed) == b"adminhello"
    finally:
        server.close()
        for conn in accepted:
            conn.close()


def test_restore_revision(client, monkeypatch):
    from mikrobox import routeros

    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": r1["id"], "port": 1}})
    base = f"/api/labs/{lab}/nodes/{r1['id']}"
    backups = client.app.state.backups
    assert backups.store(r1["id"], "/system identity\nset name=OLD\n")
    rev = client.get(base + "/revisions").json()["revisions"][0]["id"]

    assert client.post(f"{base}/revisions/{rev}/restore", json={}).json() == {"ok": False, "message": "Start R1 first."}
    monkeypatch.setattr(client.rt, "node_running", lambda n: True)
    calls = []
    monkeypatch.setattr(routeros, "export_config", lambda *a, **k: "/system identity\nset name=CURRENT\n")
    monkeypatch.setattr(routeros, "restore_config",
                        lambda host, u, p, text, replace: calls.append((host, text, replace)) or "")
    r = client.post(f"{base}/revisions/{rev}/restore", json={"mode": "replace"}).json()
    assert r["ok"] and r["via"] == "ssh" and "resetting" in r["message"]
    assert calls == [("10.99.1.1", "/system identity\nset name=OLD\n", True)]
    # The config it replaced was saved first, so the restore can be undone.
    assert len(client.get(base + "/revisions").json()["revisions"]) == 2
    r = client.post(f"{base}/revisions/{rev}/restore", json={"mode": "merge"}).json()
    assert r["ok"] and calls[-1][2] is False and "on top" in r["message"]
    assert client.post(f"{base}/revisions/20000101T000000Z/restore", json={}).status_code == 404


def test_restore_through_the_guest_agent(client, monkeypatch):
    import base64, json, socket, threading

    lab = make_lab(client)
    leaf = add_node(client, lab, name="LEAF1")
    node = client.rt.store.state.lab(lab).node(leaf["id"])
    client.app.state.backups.store(leaf["id"], "/system identity\nset name=LEAF1\n")
    rev = client.get(f"/api/labs/{lab}/nodes/{leaf['id']}/revisions").json()["revisions"][0]["id"]
    monkeypatch.setattr(client.rt, "node_running", lambda n: True)
    client.rt.s.run_dir.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(client.rt.agent_socket(node)))
    server.listen(4)
    log = []

    def agent():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            f = conn.makefile("rwb")
            for line in f:
                msg = json.loads(line)
                cmd, args = msg["execute"], msg.get("arguments", {})
                if cmd == "guest-sync":
                    reply = {"return": args["id"]}
                elif cmd == "guest-file-open":
                    log.append(("open", args["path"], args["mode"])); reply = {"return": 5}
                elif cmd == "guest-file-write":
                    log.append(("write", base64.b64decode(args["buf-b64"]).decode())); reply = {"return": {"count": 1}}
                elif cmd == "guest-file-close":
                    log.append(("close",)); reply = {"return": {}}
                elif cmd == "guest-exec":
                    log.append(("exec", base64.b64decode(args["input-data"]).decode())); reply = {"return": {"pid": 9}}
                else:
                    reply = {"return": {"exited": True, "exitcode": 0, "out-data": base64.b64encode(b"/system identity\n").decode()}}
                f.write(json.dumps(reply).encode() + b"\n"); f.flush()

    threading.Thread(target=agent, daemon=True).start()
    try:
        r = client.post(f"/api/labs/{lab}/nodes/{leaf['id']}/revisions/{rev}/restore", json={}).json()
        assert r["ok"] and r["via"] == "agent", r
        assert ("open", "mikrobox-restore.rsc", "w") in log
        assert ("write", "/system identity\nset name=LEAF1\n") in log
        assert log[-1] == ("exec", "/system reset-configuration no-defaults=yes skip-backup=yes "
                                   "run-after-reset=mikrobox-restore.rsc")
    finally:
        server.close()
        client.rt.agent_socket(node).unlink()


def test_documentation_export(client):
    import base64, io, zipfile

    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1", x=100, y=100)
    sw = add_node(client, lab, name="SW1", role="switch", x=500, y=500)
    pc = add_node(client, lab, kind="client", name="PC1", address="10.0.0.2/24", gateway="10.0.0.1", x=120, y=160)
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": r1["id"], "port": 1}})
    client.post(f"/api/labs/{lab}/links",
                json={"a": {"node": r1["id"], "port": 2}, "b": {"node": pc["id"], "port": 1}})
    client.post(f"/api/labs/{lab}/annotations",
                json={"kind": "rect", "x": 50, "y": 50, "w": 200, "h": 200, "text": "Site A"})
    client.post(f"/api/labs/{lab}/annotations",
                json={"kind": "note", "x": 0, "y": 0, "w": 10, "h": 10, "text": "Line one\nLine two"})
    client.app.state.backups.store(r1["id"], "/system identity\nset name=R1\n")

    png = b"\x89PNG fake image"
    r = client.post(f"/api/labs/{lab}/documentation", json={"png": base64.b64encode(png).decode()})
    assert r.status_code == 200 and "first-documentation.zip" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["configs/R1.txt", "first.md", "topology.png"]
    assert z.read("topology.png") == png and b"set name=R1" in z.read("configs/R1.txt")
    md = z.read("first.md").decode()
    assert "![Topology](topology.png)" in md
    assert "| R1 | Router (CHR) | 8 | 1 | 312 | 10.99.1.1 | Stopped |" in md
    assert "| SW1 | Switch (CHR) | 24 |" in md and "| PC1 | Client | 1 | 1 | 128 | 10.0.0.2/24 | Stopped |" in md
    assert "| R1 | ether1 | Cloud | |" in md and "| R1 | ether2 | PC1 | eth0 |" in md
    assert "- **Site A**: R1, PC1" in md
    assert "> Line one\n> Line two" in md
    assert "[configs/R1.txt](configs/R1.txt)" in md and "| SW1 | not available | never backed up |" in md
    assert client.post(f"/api/labs/{lab}/documentation", json={"png": "not base64!"}).status_code == 400
    no_image = zipfile.ZipFile(io.BytesIO(client.post(f"/api/labs/{lab}/documentation", json={}).content))
    assert "topology.png" not in no_image.namelist()


def test_connectivity_test_rules(client, monkeypatch):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    pc = add_node(client, lab, kind="client", name="PC1")
    assert client.post(f"/api/labs/{lab}/nodes/{r1['id']}/test", json={"target": "10.0.0.1"}).status_code == 400
    url = f"/api/labs/{lab}/nodes/{pc['id']}/test"
    assert client.post(url, json={"target": "10.0.0.1"}).status_code == 409  # stopped
    monkeypatch.setattr(client.rt, "node_running", lambda n: True)
    assert client.post(url, json={"target": "-c 1; reboot"}).status_code == 422
    assert client.post(url, json={"target": "--help"}).status_code == 422
    r = client.post(url, json={"target": "192.168.20.2", "tool": "traceroute"}).json()
    assert r["ok"] is False and r["command"].startswith("traceroute -n") and "Dry-run" in r["output"]


def test_nuke_empties_the_lab_and_keeps_it(client, monkeypatch):
    lab = make_lab(client)
    other = client.post("/api/labs", json={"name": "keep-me"}).json()["id"]
    keep = add_node(client, other, name="SAFE")
    r1 = add_node(client, lab, name="R1")
    r2 = add_node(client, lab, name="R2")
    pc = add_node(client, lab, kind="client", name="PC1")
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": r1["id"], "port": 1}})
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": r1["id"], "port": 2}, "b": {"node": r2["id"], "port": 2}})
    client.post(f"/api/labs/{lab}/annotations", json={"kind": "note", "x": 0, "y": 0, "w": 100, "h": 80, "text": "hi"})
    client.patch(f"/api/labs/{lab}", json={"mgmt_x": 700, "mgmt_y": 500})
    client.put(f"/api/labs/{lab}/backup", json={"enabled": True, "scope": "selected", "username": "admin"})
    client.put(f"/api/labs/{lab}/nodes/{r1['id']}/backup", json={"included": True})
    backups = client.app.state.backups
    backups.store(r1["id"], "/system identity\nset name=R1\n")
    for n in (r1, keep):
        disk = client.rt.disk_path(client.rt.store.state.lab(lab if n is r1 else other).node(n["id"]))
        disk.parent.mkdir(parents=True, exist_ok=True)
        disk.write_bytes(b"disk")

    url = f"/api/labs/{lab}/clear"
    r = client.post(url, json={"confirm": "wrong"})
    assert r.status_code == 400 and "first" in r.json()["detail"]
    assert len(client.get(f"/api/labs/{lab}").json()["nodes"]) == 3  # nothing happened

    stopped = []
    real_stop = client.rt.stop_node
    monkeypatch.setattr(client.rt, "stop_node", lambda l, n: stopped.append(n.name) or real_stop(l, n))
    r = client.post(url, json={"confirm": " first "})
    assert r.status_code == 200, r.text
    assert r.json()["removed"] == {"devices": 3, "links": 2, "annotations": 1, "revisions": 1}
    assert sorted(stopped) == ["PC1", "R1", "R2"]
    view = client.get(f"/api/labs/{lab}").json()
    assert view["name"] == "first" and view["id"] == lab
    assert view["nodes"] == [] and view["links"] == [] and view["annotations"] == []
    assert (view["mgmt_x"], view["mgmt_y"]) == (90, 80)
    assert view["backup"]["enabled"] is True  # lab settings stay
    assert backups.settings(lab)["nodes"] == []
    assert backups.revisions(r1["id"]) == []
    assert not (client.tmp / "data" / "disks" / f"n{r1['id']}.qcow2").exists()
    assert (client.tmp / "data" / "dhcp-hosts").exists() is False or "R1" not in (client.tmp / "run" / "dhcp-hosts").read_text()

    # The other lab is untouched.
    kept = client.get(f"/api/labs/{other}").json()
    assert [n["name"] for n in kept["nodes"]] == ["SAFE"]
    assert client.rt.disk_path(client.rt.store.state.lab(other).node(keep["id"])).exists()
    # A fresh device can be added straight away.
    assert add_node(client, lab, name="R1")["name"] == "R1"


# -- instant updates and the activity log ------------------------------------

def _next_event(ws, wanted, tries=20):
    for _ in range(tries):
        msg = ws.receive_json()
        if msg.get("t") == wanted:
            return msg
    raise AssertionError(f"no {wanted} event")


def test_changes_are_pushed_to_open_workspaces(client):
    with client.websocket_connect("/api/events") as ws:
        assert ws.receive_json() == {"t": "hello"}
        lab = make_lab(client)
        _next_event(ws, "labs")
        r1 = add_node(client, lab, name="R1")
        _next_event(ws, "labs")
        client.post(f"/api/labs/{lab}/nodes/{r1['id']}/start")  # not saved to the file, still pushed
        _next_event(ws, "labs")
        entry = _next_event(ws, "activity")["entry"]
        assert entry["user"] == "tester" and entry["lab"] == lab
        client.post("/api/presence", json={"client": "tab-zzzz", "lab": lab})
        _next_event(ws, "presence")


def test_events_need_a_sign_in(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with TestClient(client.app).websocket_connect("/api/events") as ws:
            ws.receive_json()


def test_activity_log_records_who_did_what(client):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    r2 = add_node(client, lab, name="R2", role="switch")
    pc = add_node(client, lab, kind="client", name="PC1")
    link = client.post(f"/api/labs/{lab}/links", json={"a": {"node": r1["id"], "port": 2},
                                                     "b": {"node": r2["id"], "port": 1}}).json()
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": r1["id"], "port": 1}})
    client.patch(f"/api/labs/{lab}/nodes/{r1['id']}", json={"x": 5, "y": 5})  # a move is not logged
    client.patch(f"/api/labs/{lab}/nodes/{r1['id']}", json={"ports": 4, "name": "EDGE1"})
    client.post(f"/api/labs/{lab}/nodes/{pc['id']}/start")
    client.post(f"/api/labs/{lab}/nodes/{pc['id']}/stop")
    client.post(f"/api/labs/{lab}/annotations", json={"kind": "rect", "x": 0, "y": 0, "w": 10, "h": 10, "text": "Site A"})
    anno = client.get(f"/api/labs/{lab}").json()["annotations"][0]["id"]
    client.delete(f"/api/labs/{lab}/annotations/{anno}")
    client.delete(f"/api/labs/{lab}/links/{link['id']}")
    client.delete(f"/api/labs/{lab}/nodes/{r2['id']}")

    every = client.get("/api/activity", params={"lab": lab}).json()
    assert every[0]["text"] == "created the first account" and every[0]["lab"] is None  # server-wide entries show too
    entries = [e for e in every if e["lab"] == lab]
    texts = [e["text"] for e in entries]
    assert texts == [
        "created the lab first", "added the router R1", "added the switch R2", "added the client PC1",
        "linked R1 ether2 to R2 ether1", "linked R1 ether1 to the Cloud",
        "renamed R1 to EDGE1", "changed EDGE1's ports",
        "started PC1", "stopped PC1", "added an area", 'deleted an area "Site A"',
        "removed the link EDGE1 ether2 to R2 ether1", "deleted the switch R2",
    ]
    assert {e["user"] for e in entries} == {"tester"}
    assert [e["kind"] for e in entries][-2:] == ["danger", "danger"]

    # Kept on disk, so it survives a restart.
    from mikrobox.activity import Activity
    from mikrobox.events import Events
    again = Activity(client.tmp / "data" / "activity.jsonl", Events())
    assert [e["text"] for e in again.recent(lab) if e["lab"] == lab] == texts
    assert again.record("x")["id"] > entries[-1]["id"]


def test_activity_for_accounts_and_sign_ins(client):
    anon = TestClient(client.app)
    anon.post("/api/auth/login", json={"username": "tester", "password": "nope"})
    anon.post("/api/auth/login", json={"username": "ghost", "password": "nope"})
    anon.post("/api/auth/login", json={"username": "tester", "password": "correct-horse"})
    client.post("/api/users", json={"username": "anna", "password": "long-enough"})
    client.delete("/api/users/anna")
    got = [(e["user"], e["text"], e["kind"]) for e in client.get("/api/activity").json()]
    assert ("tester", "failed to sign in (wrong password)", "danger") in got
    assert ("someone", "failed to sign in as an unknown user, ghost", "danger") in got
    assert ("tester", "signed in", "info") in got
    assert ("tester", "added the account anna", "info") in got
    assert ("tester", "removed the account anna", "danger") in got


def test_template_and_nuke_are_logged_once(client):
    lab = client.post("/api/templates/spine-leaf", json={}).json()
    client.post(f"/api/labs/{lab['id']}/clear", json={"confirm": "spine-leaf"})
    texts = [e["text"] for e in client.get("/api/activity", params={"lab": lab["id"]}).json() if e["lab"] == lab["id"]]
    assert texts == ["created the lab spine-leaf from the Spine and leaf template",
                     "nuked the lab: removed 10 devices, 12 links and 3 shapes and notes"]


# -- better clients -----------------------------------------------------------

def test_client_with_dhcp_and_services(client):
    lab = make_lab(client)
    pc = add_node(client, lab, kind="client", name="PC1", addressing="dhcp", services=["web", "iperf", "web"])
    assert pc["addressing"] == "dhcp" and pc["services"] == ["iperf", "web"]
    client.rt.sh.history.clear()
    assert client.post(f"/api/labs/{lab}/nodes/{pc['id']}/start").status_code == 200
    flat = [" ".join(c) for c in client.rt.sh.history]
    ns = f"mbc{pc['id']}"
    assert not any(" addr add " in c for c in flat)  # no fixed address
    udhcpc = next(c for c in flat if "udhcpc" in c)
    assert udhcpc.startswith(f"ip netns exec {ns} busybox udhcpc -f -i eth0 -s ") and "-x hostname:PC1" in udhcpc
    assert any(c.startswith(f"ip netns exec {ns} ") and "http.server 80" in c for c in flat)
    assert f"ip netns exec {ns} iperf3 -s" in flat
    assert (client.tmp / "etcns" / ns / "resolv.conf").exists()  # before udhcpc runs
    assert "Hello from PC1" in (client.tmp / "run" / f"n{pc['id']}.www" / "index.html").read_text()


def test_client_dhcp_and_dns_server(client):
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": r1["id"], "port": 1}})
    add_node(client, lab, kind="client", name="PC2", address="192.168.10.20/24")
    url = f"/api/labs/{lab}/nodes"
    bad = client.post(url, json={"kind": "client", "name": "S0", "addressing": "dhcp", "services": ["dhcp"]})
    assert bad.status_code == 400 and "fixed address" in bad.json()["detail"]
    assert client.post(url, json={"kind": "client", "name": "S1", "address": "10.0.0.1/30",
                                  "services": ["dhcp"]}).status_code == 400  # no room for a pool
    assert client.post(url, json={"name": "R9", "services": ["web"]}).status_code == 400  # clients only
    srv = add_node(client, lab, kind="client", name="SRV", address="192.168.10.2/24", gateway="192.168.10.1",
                   dns="9.9.9.9", services=["dhcp", "dns"])
    client.rt.sh.history.clear()
    client.post(f"/api/labs/{lab}/nodes/{srv['id']}/start")
    dnsmasq = next(c for c in client.rt.sh.history if "dnsmasq" in c)
    assert dnsmasq[:4] == ["ip", "netns", "exec", f"mbc{srv['id']}"]
    for arg in ("--keep-in-foreground", "--interface=eth0", "--port=53", "--server=9.9.9.9", "--domain=lab",
                "--dhcp-range=192.168.10.100,192.168.10.199,255.255.255.0,1h",
                "--dhcp-option=3,192.168.10.1", "--dhcp-option=6,192.168.10.2"):
        assert arg in dnsmasq, arg
    hosts = (client.tmp / "run" / f"n{srv['id']}.hosts").read_text()
    assert "192.168.10.20 pc2\n" in hosts and "192.168.10.2 srv\n" in hosts and "10.99.1.1 r1\n" in hosts
    # Changing to DHCP while the DHCP server is on is refused, even in one step.
    client.post(f"/api/labs/{lab}/nodes/{srv['id']}/stop")
    assert client.patch(f"{url}/{srv['id']}", json={"addressing": "dhcp"}).status_code == 400
    assert client.patch(f"{url}/{srv['id']}", json={"addressing": "dhcp", "services": []}).status_code == 200


def test_dhcp_pool_sizes():
    from mikrobox.clients import dhcp_range

    assert dhcp_range("192.168.10.2/24") == ("192.168.10.100", "192.168.10.199", "255.255.255.0")
    assert dhcp_range("10.1.1.1/28") == ("10.1.1.8", "10.1.1.14", "255.255.255.240")
    assert dhcp_range("10.1.0.1/16") == ("10.1.0.100", "10.1.0.199", "255.255.0.0")
    with pytest.raises(ValueError):
        dhcp_range("10.0.0.1/30")


def test_udhcpc_script_writes_only_the_clients_resolv_conf(tmp_path):
    import os, stat, subprocess
    from mikrobox.clients import UDHCPC_SCRIPT

    script = tmp_path / "udhcpc.sh"
    script.write_text(UDHCPC_SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "ip").write_text(f"#!/bin/sh\necho \"$@\" >> {tmp_path}/ip.log\n")
    (fakebin / "ip").chmod(0o755)
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("old\n")
    env = {"PATH": f"{fakebin}:{os.environ['PATH']}", "interface": "eth0", "ip": "192.168.88.254",
           "subnet": "255.255.255.0", "router": "192.168.88.1", "dns": "192.168.88.1 1.1.1.1",
           "MIKROBOX_RESOLV": str(resolv)}
    assert subprocess.run([str(script), "bound"], env=env).returncode == 0
    calls = (tmp_path / "ip.log").read_text().splitlines()
    assert calls == ["-4 addr flush dev eth0", "addr add 192.168.88.254/255.255.255.0 dev eth0",
                     "route del default", "route add default via 192.168.88.1 dev eth0"]
    assert resolv.read_text() == "nameserver 192.168.88.1\nnameserver 1.1.1.1\n"
    # With mask (CIDR bits) set, that is used instead of the dotted subnet.
    (tmp_path / "ip.log").unlink()
    subprocess.run([str(script), "renew"], env={**env, "mask": "24", "dns": ""})
    assert "addr add 192.168.88.254/24 dev eth0" in (tmp_path / "ip.log").read_text()
    assert resolv.read_text() == "nameserver 192.168.88.1\nnameserver 1.1.1.1\n"  # untouched without DNS


def test_client_services_are_stopped(client, tmp_path):
    import subprocess, sys, time as _time

    lab = make_lab(client)
    pc = add_node(client, lab, kind="client", name="PC1", services=["iperf"])
    node = client.rt.store.state.lab(lab).node(pc["id"])
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "iperf3"])
    client.rt.s.run_dir.mkdir(parents=True, exist_ok=True)
    (client.rt.s.run_dir / f"n{node.id}.svc-iperf.pid").write_text(f"{proc.pid}\n")
    client.rt.clients.stop(node)
    assert proc.wait(timeout=5) is not None
    assert not (client.rt.s.run_dir / f"n{node.id}.svc-iperf.pid").exists()


def test_throughput_test_command(client, monkeypatch):
    lab = make_lab(client)
    pc = add_node(client, lab, kind="client", name="PC1")
    monkeypatch.setattr(client.rt, "node_running", lambda n: True)
    r = client.post(f"/api/labs/{lab}/nodes/{pc['id']}/test", json={"target": "10.0.0.2", "tool": "iperf"}).json()
    assert r["command"].startswith("iperf3 -c 10.0.0.2 -t 5")


# -- full packet decode --------------------------------------------------------

TCPDUMP_XX = """\
21:49:03.648828 00:00:00:00:00:00 > 00:00:00:00:00:00, ethertype IPv4 (0x0800), length 71: 127.0.0.1.54004 > 127.0.0.1.53: 4660+ A? example.com. (29)
\t0x0000:  0000 0000 0000 0000 0000 0000 0800 4500
\t0x0010:  0039 adfe 4000 4011 8eb3 7f00 0001 7f00
\t0x0020:  0001 d2f4 0035 0025 fe38 1234 0100 0001
\t0x0030:  0000 0000 0000 0765 7861 6d70 6c65 0363
\t0x0040:  6f6d 0000 0100 01
21:49:03.700001 52:54:00:00:01:02 > ff:ff:ff:ff:ff:ff, ethertype ARP (0x0806), length 42: Request who-has 10.0.12.2 tell 10.0.12.1, length 28
\t0x0000:  ffff ffff ffff 5254 0000 0102 0806 0001
\t0x0010:  0800 0604 0001 5254 0000 0102 0a00 0c01
\t0x0020:  0000 0000 0000 0a00 0c02
"""


def _alive(pid):
    import os as _os
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[-1][0] != "Z"  # a zombie has stopped
    except OSError:
        return False


def _wait_for(check, seconds=3.0):
    import time as _t
    end = _t.time() + seconds
    while _t.time() < end and not check():
        _t.sleep(0.05)


def test_tcpdump_hex_is_joined_to_its_packet():
    from mikrobox.packets import Assembler

    asm = Assembler()
    done = []
    for line in TCPDUMP_XX.splitlines():
        done += asm.feed(line)
    assert len(done) == 1 and done[0][0]["proto"] == "DNS" and len(done[0][1]) == 71
    assert done[0][1][12:14] == b"\x08\x00"
    last = asm.flush()
    assert last[0][0]["proto"] == "ARP" and len(last[0][1]) == 42 and asm.flush() == []
    # Lines tcpdump could not summarise drop their bytes with them.
    asm.feed("tcpdump: something odd")
    asm.feed("\t0x0000:  ffff")
    assert asm.flush() == []


def test_pdml_becomes_a_tree():
    from mikrobox.packets import parse_pdml

    pdml = """<?xml version="1.0"?><pdml><packet>
      <proto name="geninfo" pos="0" showname="General information" size="42"/>
      <proto name="eth" showname="Ethernet II, Src: 52:54:00:00:01:02" size="14" pos="0">
        <field name="eth.dst" showname="Destination: Broadcast" size="6" pos="0" show="ff:ff:ff:ff:ff:ff"/>
        <field name="eth.dst_resolved" showname="Destination (resolved)" hide="yes" size="6" pos="0"/>
        <field name="eth.type" showname="Type: ARP (0x0806)" size="2" pos="12" show="0x0806"/>
      </proto>
      <proto name="arp" showname="Address Resolution Protocol (request)" size="28" pos="14">
        <field name="" show="Some text without a name" size="0" pos="14"/>
        <field name="arp.opcode" size="2" pos="20" show="1"/>
      </proto>
      <proto name="fake-field-wrapper"><field name="_ws.expert" showname="Expert Info" size="0" pos="0"/></proto>
    </packet></pdml>"""
    tree = parse_pdml(pdml)
    assert [n["t"] for n in tree] == ["Ethernet II, Src: 52:54:00:00:01:02", "Address Resolution Protocol (request)", "Expert Info"]
    eth = tree[0]
    assert (eth["p"], eth["s"]) == (0, 14) and [c["t"] for c in eth["c"]] == ["Destination: Broadcast", "Type: ARP (0x0806)"]
    assert tree[1]["c"][0] == {"t": "Some text without a name"}  # size 0: nothing to highlight
    assert tree[1]["c"][1] == {"t": "arp.opcode: 1", "n": "arp.opcode", "p": 20, "s": 2}
    assert eth["n"] == "eth" and "n" not in tree[2]


def test_tshark_never_runs_as_root(monkeypatch):
    from mikrobox import packets

    monkeypatch.setattr(packets.os, "geteuid", lambda: 0)
    monkeypatch.setattr(packets.shutil, "which", lambda name: "/usr/bin/" + name)
    assert packets.tshark_argv()[:4] == ["setpriv", "--reuid=nobody", "--regid=nogroup", "--clear-groups"]
    monkeypatch.setattr(packets.os, "geteuid", lambda: 1000)
    assert packets.tshark_argv()[0] == "tshark"


def test_live_packets_can_be_decoded(client, tmp_path, monkeypatch):
    import os, shutil

    fake = tmp_path / "fakebin"
    fake.mkdir()
    (tmp_path / "td.txt").write_text(TCPDUMP_XX)
    (fake / "tcpdump").write_text(f"#!/bin/sh\necho $$ > {tmp_path / 'td.pid'}\ncat {tmp_path / 'td.txt'}\nexec sleep 30\n")
    (fake / "tcpdump").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    lab = make_lab(client)
    r1 = add_node(client, lab, name="R1")
    monkeypatch.setattr(client.rt.net, "exists", lambda iface: True)
    with client.websocket_connect(f"/api/labs/{lab}/nodes/{r1['id']}/ports/1/packets") as ws:
        rows = ws.receive_json()["packets"]
        assert [(r["n"], r["proto"]) for r in rows] == [(1, "DNS"), (2, "ARP")]
        ws.send_json({"decode": 99})
        assert "no longer kept" in ws.receive_json()["error"]
        ws.send_json({"decode": 2})
        got = ws.receive_json()
        assert got["decoded"] == 2
        if shutil.which("tshark"):
            names = [n["t"] for n in got["tree"]]
            assert names[1].startswith("Ethernet II") and names[2].startswith("Address Resolution Protocol (request)")
            assert got["hex"].startswith("ffffffffffff52540000") and got["length"] == 42
            arp = got["tree"][2]
            target = next(c for c in arp["c"] if c["t"].startswith("Target IP address"))
            assert (target["p"], target["s"], target["t"]) == (38, 4, "Target IP address: 10.0.12.2")
        else:
            assert "needs tshark" in got["error"]
        # Let the server finish its cleanup before the test client tears the session down: it
        # cancels the handler right after sending the disconnect, a race a browser never causes.
        ws.close()
        _wait_for(lambda: not _alive(int((tmp_path / "td.pid").read_text())))  # the server stopped "tcpdump"


# -- Docker on Linux clients ----------------------------------------------------

import json  # used by FakeEngine below


class FakeEngine:
    """Just enough of Docker's Engine API, on a unix socket, to test Mikrobox against."""

    def __init__(self, path, images=()):
        import http.server, socketserver, threading as _th
        self.path = str(path)
        self.containers = {}  # name -> dict
        self.images = set(images)
        self.calls = []
        self.loaded = []
        engine = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def address_string(self):
                return "fake"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json"):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                if self.headers.get("Transfer-Encoding") == "chunked":
                    data = b""
                    while True:
                        size = int(self.rfile.readline().strip(), 16)
                        if not size:
                            self.rfile.readline()
                            return data
                        data += self.rfile.read(size)
                        self.rfile.readline()
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_GET(self):
                engine.calls.append(("GET", self.path))
                from urllib.parse import urlparse, parse_qs, unquote
                u = urlparse(self.path)
                parts = [unquote(p) for p in u.path.strip("/").split("/")]
                if u.path == "/_ping":
                    return self._send(200, b"OK", "text/plain")
                if u.path == "/version":
                    return self._send(200, {"Version": "27.5.1"})
                if u.path == "/containers/json":
                    return self._send(200, [{
                        "Names": ["/" + n], "Image": c["Image"], "State": c["State"],
                        "Status": "Up 1 minute" if c["State"] == "running" else "Exited (0)",
                        "Ports": [{"IP": "0.0.0.0", "PrivatePort": int(k.split("/")[0]), "PublicPort": int(b[0]["HostPort"]), "Type": k.split("/")[1]}
                                  for k, b in c["HostConfig"].get("PortBindings", {}).items()]
                                 + [{"IP": "::", "PrivatePort": int(k.split("/")[0]), "PublicPort": int(b[0]["HostPort"]), "Type": k.split("/")[1]}
                                    for k, b in c["HostConfig"].get("PortBindings", {}).items()],
                    } for n, c in engine.containers.items()])
                if parts[0] == "containers" and parts[-1] == "json":
                    return self._send(200, engine.containers[parts[1]]) if parts[1] in engine.containers else self._send(404, {"message": "No such container"})
                if parts[0] == "containers" and parts[-1] == "logs":
                    frame = lambda kind, text: bytes([kind, 0, 0, 0]) + len(text).to_bytes(4, "big") + text
                    return self._send(200, frame(1, b"hello\n") + frame(2, b"oops\n"), "application/vnd.docker.raw-stream")
                if parts[0] == "images" and parts[-1] == "json":
                    return self._send(200, {"Id": "sha256:x"}) if "/".join(parts[1:-1]) in engine.images else self._send(404, {"message": "No such image"})
                if u.path == "/images/get":
                    return self._send(200, b"TARBALL:" + parse_qs(u.query)["names"][0].encode(), "application/x-tar")
                self._send(404, {"message": "not here"})

            def do_POST(self):
                body = self._body()
                engine.calls.append(("POST", self.path))
                from urllib.parse import urlparse, parse_qs, unquote
                u = urlparse(self.path)
                q = parse_qs(u.query)
                parts = [unquote(p) for p in u.path.strip("/").split("/")]
                if u.path == "/containers/create":
                    spec = json.loads(body)
                    if spec["Image"] not in engine.images:
                        return self._send(404, {"message": "No such image"})
                    engine.containers[q["name"][0]] = {**spec, "State": "created"}
                    return self._send(201, {"Id": "abc"})
                if parts[0] == "containers" and parts[-1] in ("start", "stop", "restart"):
                    c = engine.containers.get(parts[1])
                    if c is None:
                        return self._send(404, {"message": "No such container"})
                    c["State"] = "exited" if parts[-1] == "stop" else "running"
                    return self._send(204)
                if u.path == "/images/create":
                    ref = q["fromImage"][0] + (":" + q["tag"][0] if q.get("tag") else "")
                    engine.images.add(ref)
                    lines = [{"status": "Pulling"}, {"id": "l1", "progressDetail": {"current": 5, "total": 10}}, {"status": "Done"}]
                    return self._send(200, b"\n".join(json.dumps(l).encode() for l in lines))
                if u.path == "/images/load":
                    engine.loaded.append(body)
                    engine.images.add(body.decode().split(":", 1)[1])
                    return self._send(200, {"stream": "Loaded"})
                self._send(404, {"message": "not here"})

            def do_DELETE(self):
                engine.calls.append(("DELETE", self.path))
                from urllib.parse import urlparse, unquote
                name = unquote(urlparse(self.path).path.split("/")[2])
                if engine.containers.pop(name, None) is None:
                    return self._send(404, {"message": "No such container"})
                self._send(204)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # a caller that hung up early (a quick ping) is not news

        self.server = Server(self.path, Handler)
        _th.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def engines(client, monkeypatch):
    """Pretend PCs with Docker have a running engine, a fake one per PC."""
    made = {}
    rt = client.rt
    monkeypatch.setattr(rt, "node_running", lambda n: n.id in made or n.kind.value == "client" and getattr(n, "_up", False))

    def up(node_id, images=()):
        sock = client.tmp / f"engine{node_id}.sock"
        made[node_id] = FakeEngine(sock, images)
        monkeypatch.setattr(rt.docker, "sock", lambda n, _s=rt.docker.sock: client.tmp / f"engine{n.id}.sock")
        rt.docker.state[node_id] = "running"
        return made[node_id]

    yield up
    for e in made.values():
        e.close()


def test_docker_switch_and_engine_command(client):
    lab = make_lab(client)
    pc = add_node(client, lab, kind="client", name="PC1")
    r1 = add_node(client, lab, name="R1")
    url = f"/api/labs/{lab}/nodes/{pc['id']}"
    assert client.post(f"/api/labs/{lab}/nodes/{r1['id']}/docker", json={"on": True}).status_code == 400
    assert client.post(f"{url}/containers", json={"name": "web", "image": "nginx:alpine"}).status_code == 409  # off
    got = client.post(f"{url}/docker", json={"on": True}).json()
    assert got["engine"]["on"] and got["engine"]["state"] == "stopped"
    client.rt.sh.history.clear()
    client.post(f"{url}/start")
    launch = next(c for c in client.rt.sh.history if c[0].endswith("docker-launch.sh"))
    ns = f"mbc{pc['id']}"
    assert launch[1] == str(client.rt.s.netns_dir / ns) and launch[2].endswith(f"{ns}/resolv.conf")
    assert ["--data-root", str(client.tmp / "data" / "docker" / f"n{pc['id']}")] == launch[4:6]
    assert "-H" in launch and launch[launch.index("-H") + 1] == f"unix://{client.tmp / 'run' / 'docker' / f'n{pc[chr(105) + chr(100)]}.sock'}"
    script = (client.tmp / "run" / "docker" / "docker-launch.sh").read_text()
    assert "mount -t sysfs sysfs /sys" in script and "mount --move" in script and "exec dockerd" in script
    # Saved containers survive in the lab and come back in exports.
    assert client.post(f"{url}/containers", json={"name": "web", "image": "nginx:alpine",
                                                  "ports": [{"host": 8080, "container": 80}]}).status_code == 201
    exported = client.get(f"/api/labs/{lab}/export").json()
    node = next(n for n in exported["nodes"] if n["name"] == "PC1")
    assert node["docker"] and node["containers"][0]["image"] == "nginx:alpine"
    assert client.post(f"/api/labs/{lab}/nodes", json={"name": "R2", "kind": "chr"}).status_code == 201


def test_container_rules(client):
    lab = make_lab(client)
    pc = add_node(client, lab, kind="client", name="PC1")
    url = f"/api/labs/{lab}/nodes/{pc['id']}"
    client.post(f"{url}/docker", json={"on": True})
    bad = [
        {"name": "web app", "image": "nginx"},
        {"name": "web", "image": "nginx; rm -rf /"},
        {"name": "web", "image": "nginx", "env": {"1BAD": "x"}},
        {"name": "web", "image": "nginx", "ports": [{"host": 80, "container": 80}, {"host": 80, "container": 81}]},
        {"name": "web", "image": "nginx", "ports": [{"host": 0, "container": 80}]},
    ]
    for body in bad:
        assert client.post(f"{url}/containers", json=body).status_code == 422, body
    assert client.post(f"{url}/containers", json={"name": "web", "image": "nginx"}).status_code == 201
    assert client.post(f"{url}/containers", json={"name": "web", "image": "httpd"}).status_code == 409
    view = client.get(f"{url}/containers").json()
    assert view["containers"][0]["state"] == "pending" and view["containers"][0]["status"] == "Starts with the PC"
    assert view["containers"][0]["command"] == "docker run -d --name web --restart unless-stopped nginx"
    assert client.delete(f"{url}/containers/web").status_code == 200
    assert client.get(f"{url}/containers").json()["containers"] == []


def test_containers_on_a_running_engine(client, engines):
    import time as _t
    from mikrobox.docker import demux, run_command, split_ref

    lab = make_lab(client)
    pc = add_node(client, lab, kind="client", name="PC1")
    url = f"/api/labs/{lab}/nodes/{pc['id']}"
    client.post(f"{url}/docker", json={"on": True})
    eng = engines(pc["id"])
    spec = {"name": "web", "image": "nginx:alpine", "ports": [{"host": 8080, "container": 80}], "env": {"A": "1"}}
    assert client.post(f"{url}/containers", json=spec).status_code == 201
    for _ in range(50):
        rows = client.get(f"{url}/containers").json()["containers"]
        if rows and rows[0]["running"]:
            break
        _t.sleep(0.05)
    row = rows[0]
    assert row["running"] and row["saved"] and row["ports"] == [{"host": 8080, "container": 80, "proto": "tcp"}]
    assert ("POST", "/images/create?fromImage=nginx&tag=alpine") in eng.calls  # downloaded
    created = eng.containers["web"]
    assert created["Env"] == ["A=1"] and created["HostConfig"]["PortBindings"] == {"80/tcp": [{"HostPort": "8080"}]}
    assert created["HostConfig"]["RestartPolicy"] == {"Name": "unless-stopped"}
    # A container made in the console shows up too, not saved.
    eng.images.add("busybox")
    eng.containers["mine"] = {"Image": "busybox", "State": "running", "HostConfig": {}}
    rows = {r["name"]: r for r in client.get(f"{url}/containers").json()["containers"]}
    assert rows["mine"]["running"] and not rows["mine"]["saved"] and rows["mine"]["command"] == ""
    assert client.post(f"{url}/containers/web/stop").json()
    assert eng.containers["web"]["State"] == "exited"
    assert client.get(f"{url}/containers/web/logs").json()["text"] == "hello\noops\n"
    assert client.delete(f"{url}/containers/mine").status_code == 200 and "mine" not in eng.containers
    assert client.get(f"/api/labs/{lab}").json()["nodes"][0]["docker_summary"] == {"total": 1, "running": 0}
    acts = [e["text"] for e in client.get("/api/activity", params={"lab": lab}).json()]
    assert "added container web (nginx:alpine) to PC1" in acts and "stopped container web on PC1" in acts
    # helpers
    assert demux(b"plain text, no frames") == "plain text, no frames"
    assert split_ref("ghcr.io/x/y:1.2") == ("ghcr.io/x/y", "1.2")


def test_images_are_copied_between_pcs(client, engines):
    import time as _t

    lab = make_lab(client)
    pc1 = add_node(client, lab, kind="client", name="PC1")
    pc2 = add_node(client, lab, kind="client", name="PC2")
    for pc in (pc1, pc2):
        client.post(f"/api/labs/{lab}/nodes/{pc['id']}/docker", json={"on": True})
    engines(pc1["id"], images={"grafana/grafana"})
    second = engines(pc2["id"])
    client.post(f"/api/labs/{lab}/nodes/{pc2['id']}/containers", json={"name": "g", "image": "grafana/grafana"})
    for _ in range(50):
        if "g" in second.containers and second.containers["g"]["State"] == "running":
            break
        _t.sleep(0.05)
    assert second.loaded == [b"TARBALL:grafana/grafana"]  # copied from PC1, not downloaded
    assert not any(c[1].startswith("/images/create") for c in second.calls)


def test_pull_proxy_only_connects_to_https(tmp_path):
    import socket as _s, ssl, threading as _th
    from mikrobox.docker import PullProxy

    lst = _s.socket(); lst.bind(("127.0.0.1", 0)); lst.listen(4)
    proxy = PullProxy(lst)
    port = lst.getsockname()[1]
    try:
        c = _s.create_connection(("127.0.0.1", port))
        c.sendall(b"CONNECT example.com:22 HTTP/1.1\r\nHost: example.com:22\r\n\r\n")
        assert c.recv(100).startswith(b"HTTP/1.1 403")
        c = _s.create_connection(("127.0.0.1", port))
        c.sendall(b"GET http://example.com/ HTTP/1.1\r\n\r\n")
        assert c.recv(100).startswith(b"HTTP/1.1 403")
    finally:
        proxy.close()


def test_pull_proxy_close_lets_go_of_every_socket():
    # Sockets made inside a PC keep its network namespace alive, so close() must wake
    # the accept loop and end open relays, not just drop the listener's file descriptor.
    import socket as _s, time as _t
    from mikrobox.docker import PullProxy

    lst = _s.socket(); lst.bind(("127.0.0.1", 0)); lst.listen(4)
    proxy = PullProxy(lst)
    c = _s.create_connection(lst.getsockname())
    c.sendall(b"CONNECT regis")  # an unfinished request: the relay waits for more
    for _ in range(100):
        if proxy.conns:
            break
        _t.sleep(0.01)
    assert len(proxy.conns) == 1
    proxy.close()
    c.settimeout(2)
    assert c.recv(10) == b""  # the relay hung up
    for _ in range(100):
        if not proxy.conns:
            break
        _t.sleep(0.01)
    assert not proxy.conns and lst.fileno() == -1


def test_links_fall_back_when_the_kernel_lacks_matchall(tmp_path):
    from mikrobox.network import Net
    from mikrobox.shell import CommandError, Shell

    class NoMatchall(Shell):
        def run(self, cmd, check=True, input=None):
            if "matchall" in cmd:
                raise CommandError("Error: TC classifier not found.")
            return super().run(cmd, check, input)

    sh = NoMatchall(dry_run=True)
    Net(Settings(data_dir=tmp_path, run_dir=tmp_path / "run", dry_run=True), sh).connect("mb1p1", "mb2p1")
    filters = [c for c in sh.history if c[:3] == ["tc", "filter", "add"]]
    assert [c[4] for c in filters] == ["mb1p1", "mb2p1"]
    assert all("u32" in c and c[-1] in ("mb1p1", "mb2p1") for c in filters)


def test_docker_in_console_and_on_delete(client, monkeypatch):
    lab = make_lab(client)
    pc = add_node(client, lab, kind="client", name="PC1")
    client.post(f"/api/labs/{lab}/nodes/{pc['id']}/docker", json={"on": True})
    data = client.tmp / "data" / "docker" / f"n{pc['id']}"
    data.mkdir(parents=True)
    (data / "something").write_text("x")
    monkeypatch.setattr(client.rt.docker.s, "dry_run", False)
    monkeypatch.setattr(client.rt.docker, "stop", lambda node: None)
    client.rt.delete_disk(client.rt.store.state.lab(lab).node(pc["id"]))
    assert not data.exists()


# -- links that open lab pages in your browser ----------------------------------

@pytest.fixture
def pages(tmp_path):
    """A plain HTTP page and an HTTPS one (self-signed), standing in for pages in the lab."""
    import http.server, ssl, subprocess, threading as _th

    class Page(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = f"page {self.path} over {'https' if isinstance(self.connection, ssl.SSLSocket) else 'http'}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    plain = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    secure = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=lab",
                    "-keyout", str(tmp_path / "k.pem"), "-out", str(tmp_path / "c.pem")], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    secure.socket = ctx.wrap_socket(secure.socket, server_side=True)
    for srv in (plain, secure):
        _th.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"http": plain.server_address[1], "https": secure.server_address[1]}
    for srv in (plain, secure):
        srv.shutdown()


def _fetch(url, insecure=True):
    import ssl, urllib.request
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    local = url.replace("testserver", "127.0.0.1")
    with urllib.request.urlopen(local, timeout=5, context=ctx) as r:
        return r.read().decode()


@pytest.fixture
def lab_pc(client, monkeypatch):
    """A running PC whose 'inside' is this machine, so links reach the test pages."""
    import socket as _s
    lab = make_lab(client)
    pc = add_node(client, lab, kind="client", name="PC1")
    running = {pc["id"]}
    monkeypatch.setattr(client.rt, "node_running", lambda n: n.id in running)
    links = client.app.state.weblinks

    def plain_socket(node_id):
        s = _s.socket(); s.setblocking(False)
        return s
    monkeypatch.setattr(links, "connector", plain_socket)
    return lab, pc, running


def test_link_to_an_address(client, pages, lab_pc):
    lab, pc, _ = lab_pc
    r = client.post("/api/web", json={"lab": lab, "node": pc["id"], "url": f"127.0.0.1:{pages['http']}/hi?x=1", "who": "anyone"})
    assert r.status_code == 201, r.text
    link = r.json()
    assert link["url"] == "http://testserver:42000/hi?x=1" and link["who"] == "anyone" and link["by"] == "tester"
    assert link["title"] == f"http://127.0.0.1:{pages['http']}" and link["detail"] == "From PC1"
    assert _fetch(link["url"]) == "page /hi?x=1 over http"
    assert _fetch(link["url"].replace("/hi?x=1", "/other")) == "page /other over http"  # any page on that site
    assert client.get("/api/web", params={"lab": lab}).json()[0]["id"] == link["id"]
    # HTTPS given in the address passes straight through: the page's own certificate.
    r = client.post("/api/web", json={"lab": lab, "node": pc["id"], "url": f"https://127.0.0.1:{pages['https']}", "who": "anyone"})
    secure = r.json()
    assert secure["url"] == "https://testserver:42001/" and _fetch(secure["url"]) == "page / over https"
    import ssl, urllib.error
    with pytest.raises((urllib.error.URLError, ssl.SSLError)):
        _fetch(secure["url"], insecure=False)  # self-signed: a browser would warn, as it should


def test_link_to_a_container_finds_https(client, pages, lab_pc, engines):
    lab, pc, _ = lab_pc
    client.post(f"/api/labs/{lab}/nodes/{pc['id']}/docker", json={"on": True})
    eng = engines(pc["id"], images={"portainer/portainer-ce"})
    eng.containers["secure"] = {"Image": "portainer/portainer-ce", "State": "running",
                                "HostConfig": {"PortBindings": {"9443/tcp": [{"HostPort": str(pages["https"])}]}}}
    eng.containers["web"] = {"Image": "nginx:alpine", "State": "running",
                             "HostConfig": {"PortBindings": {"80/tcp": [{"HostPort": str(pages["http"])}]}}}
    eng.containers["off"] = {"Image": "nginx:alpine", "State": "exited", "HostConfig": {"PortBindings": {"80/tcp": [{"HostPort": "1"}]}}}
    secure = client.post("/api/web", json={"lab": lab, "node": pc["id"], "container": "secure", "who": "anyone"}).json()
    assert secure["scheme"] == "https" and secure["title"] == "secure · portainer-ce"
    assert secure["detail"] == f"Container on PC1, port {pages['https']}"
    assert _fetch(secure["url"]) == "page / over https"
    web = client.post("/api/web", json={"lab": lab, "node": pc["id"], "container": "web", "who": "anyone"}).json()
    assert web["scheme"] == "http" and _fetch(web["url"]) == "page / over http"
    assert client.post("/api/web", json={"lab": lab, "node": pc["id"], "container": "off"}).status_code == 409
    assert client.post("/api/web", json={"lab": lab, "node": pc["id"], "container": "nope"}).status_code == 404


def test_link_only_for_me_and_closing(client, pages, lab_pc):
    import time as _t, urllib.error
    lab, pc, running = lab_pc
    mine = client.post("/api/web", json={"lab": lab, "node": pc["id"], "url": f"127.0.0.1:{pages['http']}"}).json()
    assert mine["who"] == "me" and mine["allow"] == "testclient"
    with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
        _fetch(mine["url"])  # this test connects from 127.0.0.1, not from "testclient"
    other = client.post("/api/web", json={"lab": lab, "node": pc["id"], "url": f"127.0.0.1:{pages['http']}", "who": "anyone"}).json()
    assert client.delete(f"/api/web/{mine['id']}").status_code == 204
    assert client.delete(f"/api/web/{mine['id']}").status_code == 404
    # The PC stops: its links close.
    running.discard(pc["id"])
    client.rt.stop_node(client.rt.store.state.lab(lab), client.rt.store.state.lab(lab).node(pc["id"]))
    for _ in range(40):
        if not client.get("/api/web").json():
            break
        _t.sleep(0.05)
    assert client.get("/api/web").json() == []
    with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
        _fetch(other["url"])
    assert client.post("/api/web", json={"lab": lab, "node": pc["id"], "url": "10.0.0.1"}).status_code == 409  # stopped
    acts = [e["text"] for e in client.get("/api/activity", params={"lab": lab}).json()]
    assert f"opened a link to http://127.0.0.1:{pages['http']} from PC1 for anyone" in acts


def test_pages_in_the_lab_cannot_act_with_your_sign_in(client):
    # Web links serve lab pages from this machine on other ports: to the browser, the same
    # site. Changes and websockets must come from Mikrobox's own page.
    lab = make_lab(client)
    own = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    assert client.post("/api/labs", json={"name": "ok"}, headers=own).status_code == 201
    assert client.post("/api/labs", json={"name": "cli"}).status_code == 201  # tools send neither header
    for headers in ({"Origin": "http://testserver:20000"}, {"Sec-Fetch-Site": "same-site"},
                    {"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.example"}, {"Origin": "null"}):
        r = client.post("/api/labs", json={"name": "nope"}, headers=headers)
        assert r.status_code == 403, headers
        assert client.delete(f"/api/labs/{lab}", headers=headers).status_code == 403
    assert client.get(f"/api/labs/{lab}", headers={"Origin": "http://testserver:20000"}).status_code == 200  # reading is fine
    assert client.post("/api/labs", json={"name": "proxied"}, headers={"Origin": "https://lab.example", "X-Forwarded-Host": "lab.example"}).status_code == 201
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/events", headers={"Origin": "http://testserver:20000"}) as ws:
            ws.receive_json()
    assert e.value.code == 4403
    with client.websocket_connect("/api/events", headers={"Origin": "http://testserver"}) as ws:
        assert ws.receive_json()["t"] == "hello"


def test_a_sign_in_works_only_from_where_it_was_made(tmp_path):
    from mikrobox.auth import Auth
    auth = Auth(tmp_path)
    auth.create_user("mla", "lab-password-1")
    token = auth.new_session("mla", "192.168.1.20")
    assert auth.session_user(token, "192.168.1.20") == "mla"
    assert auth.session_user(token, "::ffff:192.168.1.20") == "mla"
    assert auth.session_user(token, "10.99.1.5") is None  # a copy of the cookie, used from the lab
    old = auth.new_session("mla")  # sessions from before this check keep working
    assert auth.session_user(old, "10.99.1.5") == "mla"


def test_link_addresses_and_ports(client, lab_pc, monkeypatch):
    from mikrobox.weblinks import LinkError, parse_url
    assert parse_url("10.0.0.1") == ("http", "10.0.0.1", 80, "/")
    assert parse_url("https://r1.lab:8443/webfig/") == ("https", "r1.lab", 8443, "/webfig/")
    for bad in ("ftp://x", "http://", "http://h:port"):
        with pytest.raises(LinkError):
            parse_url(bad)
    lab, pc, _ = lab_pc
    r1 = add_node(client, lab, name="R1")
    assert client.post("/api/web", json={"lab": lab, "node": r1["id"], "url": "10.0.0.1"}).status_code == 400
    assert client.post("/api/web", json={"lab": lab, "node": pc["id"], "url": "ftp://x"}).status_code == 409
    assert client.post("/api/web", json={"lab": lab, "node": pc["id"]}).status_code == 400
    # Nothing listening there: said plainly, no link made.
    r = client.post("/api/web", json={"lab": lab, "node": pc["id"], "container": None, "url": "https://127.0.0.1:1"})
    assert r.status_code == 201  # https given, so no probe; connecting fails later, per request
    links = client.app.state.weblinks
    monkeypatch.setattr(links, "first", 42010)
    monkeypatch.setattr(links, "last", 42010)
    import socket as _s
    busy = _s.socket(); busy.bind(("0.0.0.0", 42010)); busy.listen(1)
    try:
        r = client.post("/api/web", json={"lab": lab, "node": pc["id"], "url": "10.0.0.1:80"})
        assert r.status_code == 409 and "No free port" in r.json()["detail"]
    finally:
        busy.close()
