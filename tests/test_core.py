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
        kvm_device=tmp_path / "kvm",
    )
    (tmp_path / "sysnet").mkdir()
    (tmp_path / "netns").mkdir()
    app = create_app(settings)
    with TestClient(app) as c:
        c.rt = app.state.rt
        c.tmp = tmp_path
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
                        kvm_device=tmp_path / "kvm", cloud_mode="bridged")
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
