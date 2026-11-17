"""Tests that run without root, KVM or a CHR image (dry-run mode)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
        ksm_dir=tmp_path / "ksm",  # never touch the real host's KSM from tests
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
                                          "isp-mpls", "bgp-multihoming", "aruba-mikrotik"]
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
    assert anon.get("/api/auth/status").json() == {"enabled": True, "setup_needed": False, "user": None}


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



# -- experimental: Aruba AOS-CX ---------------------------------------------

def test_aoscx_node_defaults_and_qemu(client):
    lab = make_lab(client)
    cx = add_node(client, lab, kind="aoscx", name="CX1")
    assert (cx["kind"], cx["role"], cx["ports"], cx["cpus"], cx["ram_mb"]) == ("aoscx", "switch", 9, 1, 2048)
    assert client.post(f"/api/labs/{lab}/nodes", json={"kind": "aoscx", "name": "CX2", "ports": 18}).status_code == 400
    assert client.post(f"/api/labs/{lab}/nodes", json={"kind": "aoscx", "name": "CX2", "ram_mb": 1024}).status_code == 400
    assert client.post(f"/api/labs/{lab}/nodes", json={"kind": "aoscx", "name": "CX2", "address": "1.2.3.4/24"}).status_code == 400
    assert client.post(f"/api/labs/{lab}/nodes", json={"kind": "aoscx", "role": "router", "name": "CX2"}).status_code == 400
    assert client.patch(f"/api/labs/{lab}/nodes/{cx['id']}", json={"cpus": 2, "ram_mb": 4096}).json()["ram_mb"] == 4096

    node = client.rt.store.state.lab(lab).node(cx["id"])
    cmd = client.rt.qemu_command(node)
    joined = " ".join(cmd)
    assert f"if=ide,format=qcow2" in joined and "guest_agent" not in joined
    assert sum(1 for a in cmd if a.startswith("e1000,netdev=")) == 9
    assert cmd[cmd.index("-smp") + 1] == "2"

    # MikroTik is unchanged.
    r1 = add_node(client, lab, name="R1")
    cmd = " ".join(client.rt.qemu_command(client.rt.store.state.lab(lab).node(r1["id"])))
    assert "if=virtio,format=qcow2" in cmd and "virtio-net-pci,netdev=p1" in cmd and "guest_agent" in cmd


def test_aoscx_needs_its_image_and_kvm(client):
    lab = make_lab(client)
    cx = add_node(client, lab, kind="aoscx", name="CX1")
    client.rt.s.dry_run = False  # the checks below happen before anything runs
    try:
        r = client.post(f"/api/labs/{lab}/nodes/{cx['id']}/start")
        assert r.status_code == 409 and "No Aruba AOS-CX image yet" in r.json()["detail"]
        image = client.tmp / "data" / "images" / "aoscx.qcow2"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"QFI\xfb")
        r = client.post(f"/api/labs/{lab}/nodes/{cx['id']}/start")
        assert r.status_code == 409 and "needs KVM" in r.json()["detail"]
    finally:
        client.rt.s.dry_run = True
    client.rt.sh.history.clear()
    assert client.post(f"/api/labs/{lab}/nodes/{cx['id']}/start").status_code == 200
    overlay = next(c for c in client.rt.sh.history if c[:2] == ["qemu-img", "create"])
    assert overlay[overlay.index("-F") + 1] == "qcow2" and overlay[overlay.index("-b") + 1].endswith("aoscx.qcow2")


def test_aoscx_mgmt_port_gets_a_cloud_address(client):
    lab = make_lab(client)
    cx = add_node(client, lab, kind="aoscx", name="CX1")
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": cx["id"], "port": 1}})
    view = client.get(f"/api/labs/{lab}").json()["nodes"][0]
    assert view["mgmt_ip"] == f"10.99.1.{cx['id']}" and view["ssh"] == f"ssh admin@10.99.1.{cx['id']}"
    assert view["backup"] is None  # config tools stay MikroTik-only for now
    hosts = (client.tmp / "run" / "dhcp-hosts").read_text()
    assert f"52:54:00:00:{cx['id']:02x}:01,10.99.1.{cx['id']},CX1" in hosts


def test_aoscx_ports_in_documentation(client):
    import io, zipfile

    lab = make_lab(client)
    cx = add_node(client, lab, kind="aoscx", name="CX1")
    r1 = add_node(client, lab, name="R1")
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": cx["id"], "port": 3}, "b": {"node": r1["id"], "port": 2}})
    client.post(f"/api/labs/{lab}/links", json={"a": {"node": cx["id"], "port": 1}})
    md = zipfile.ZipFile(io.BytesIO(client.post(f"/api/labs/{lab}/documentation", json={}).content)).read("first.md").decode()
    assert "| CX1 | Switch (Aruba AOS-CX) | 9 | 1 | 2048 |" in md
    assert "| CX1 | 1/1/2 | R1 | ether2 |" in md and "| CX1 | mgmt | Cloud | |" in md


def _make_ova_zip(folder):
    """A stand-in for HPE's download: a .zip holding an .ova (tar) holding a .vmdk."""
    import io, tarfile, zipfile

    vmdk = b"KDMV" + b"\0" * 4096
    ova = io.BytesIO()
    with tarfile.open(fileobj=ova, mode="w") as t:
        for name, data in (("ArubaOS-CX.ovf", b"<Envelope/>"), ("../../evil.vmdk", b"KDMV"),
                           ("arubaoscx-disk-image-genericx86-p4.vmdk", vmdk)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    path = folder / "ArubaOS-CX_10_15_1020_ova.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("README.txt", "hello")
        z.writestr("ArubaOS-CX_10_15_1020.ova", ova.getvalue())
    return path, vmdk


def test_image_install_unpacks_zip_and_ova(tmp_path, monkeypatch):
    from mikrobox import images
    from mikrobox.platforms import AOSCX

    src, vmdk = _make_ova_zip(tmp_path)
    seen = []

    def fake_convert(disk, dest):
        seen.append((images.sniff(disk), disk.read_bytes()))
        dest.write_bytes(b"QFI\xfb converted")

    monkeypatch.setattr(images, "_convert", fake_convert)
    settings = Settings(data_dir=tmp_path / "data", run_dir=tmp_path / "run", dry_run=True)
    result = images.install(settings, AOSCX, src, src.name)
    assert seen == [("vmdk", vmdk)]  # the real disk, not the tiny one with the hostile name
    assert result["version"] == "10.15.1020"
    assert (tmp_path / "data" / "images" / "aoscx.qcow2").read_bytes() == b"QFI\xfb converted"
    assert (tmp_path / "data" / "images" / "aoscx.version").read_text() == "10.15.1020\n"
    assert not (tmp_path / "evil.vmdk").exists() and not (tmp_path / "data" / "evil.vmdk").exists()
    assert not list((tmp_path / "data" / "images").glob(".import-*"))

    junk = tmp_path / "notes.txt"
    junk.write_text("not an image")
    with pytest.raises(images.ImageError):
        images.install(settings, AOSCX, junk)


def test_image_upload_endpoint(client, monkeypatch):
    import time as _time
    from mikrobox import images

    src, _ = _make_ova_zip(client.tmp)
    monkeypatch.setattr(images, "_convert", lambda disk, dest: dest.write_bytes(b"QFI\xfb ok"))
    lab = make_lab(client)
    cx = add_node(client, lab, kind="aoscx", name="CX1")
    disk = client.rt.disk_path(client.rt.store.state.lab(lab).node(cx["id"]))
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(b"old overlay")

    assert client.put("/api/images/chr", content=b"x").status_code == 400
    r = client.put("/api/images/aoscx", params={"filename": src.name}, content=src.read_bytes())
    assert r.status_code == 202, r.text
    for _ in range(50):
        info = next(i for i in client.get("/api/images").json() if i["kind"] == "aoscx")
        if info["import"]["state"] != "converting":
            break
        _time.sleep(0.1)
    assert info["import"] == {"state": "done", "message": "Aruba AOS-CX 10.15.1020 is ready."}
    assert info["image"] and info["version"] == "10.15.1020"
    assert not disk.exists()  # existing switches start fresh on the new image
    assert not (client.tmp / "data" / "images" / ".upload-aoscx").exists()
    platforms = {p["kind"]: p for p in client.get("/api/system").json()["platforms"]}
    assert platforms["aoscx"]["image"] and platforms["aoscx"]["experimental"]
    assert platforms["chr"]["experimental"] is False

    r = client.put("/api/images/aoscx", content=b"just text")
    for _ in range(50):
        info = next(i for i in client.get("/api/images").json() if i["kind"] == "aoscx")
        if info["import"]["state"] != "converting":
            break
        _time.sleep(0.1)
    assert info["import"]["state"] == "error" and "not a .zip" in info["import"]["message"]


def test_operating_system_and_role(client):
    lab = make_lab(client)
    assert add_node(client, lab, name="R1")["role"] == "router"
    sw = add_node(client, lab, kind="chr", role="switch", name="SW1")  # CHR still works as a switch
    assert (sw["kind"], sw["role"], sw["ports"]) == ("chr", "switch", 24)
    cx = add_node(client, lab, kind="aoscx", name="CX1")  # role defaults to what the OS can be
    assert cx["role"] == "switch"
    platforms = {p["kind"]: p for p in client.get("/api/system").json()["platforms"]}
    assert platforms["chr"]["roles"] == ["router", "switch"] and platforms["aoscx"]["roles"] == ["switch"]
    assert platforms["aoscx"]["short"] == "CX" and platforms["chr"]["short"] == ""
    # A CX saved before roles existed is still a switch when loaded.
    from mikrobox.models import Node
    assert Node(id=9, kind="aoscx", name="OLD", ports=9).role == "switch"


def test_ksm_setting(tmp_path):
    from mikrobox.ksm import Ksm
    import os as _os

    ksm_dir = tmp_path / "ksm"
    ksm_dir.mkdir()
    (ksm_dir / "run").write_text("0\n")
    (ksm_dir / "pages_sharing").write_text("262144\n")
    settings = Settings(data_dir=tmp_path / "data", ksm_dir=ksm_dir)
    k = Ksm(settings)
    assert k.status()["enabled"] is True and k.status()["running"] is False
    k.apply()
    assert (ksm_dir / "run").read_text() == "1"
    st = k.status()
    assert st["running"] and st["saved_mb"] == 262144 * _os.sysconf("SC_PAGE_SIZE") // 2**20
    assert k.set(False)["enabled"] is False and (ksm_dir / "run").read_text() == "0"
    assert Ksm(settings).wanted() is False  # remembered
    assert Ksm(Settings(data_dir=tmp_path / "d2", ksm_dir=tmp_path / "none")).status()["available"] is False


def test_ksm_endpoint(client):
    st = client.get("/api/system").json()["ksm"]
    assert st["enabled"] is True and st["available"] is False
    assert client.put("/api/host/ksm", json={"enabled": False}).json()["enabled"] is False
    assert client.get("/api/system").json()["ksm"]["enabled"] is False


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
