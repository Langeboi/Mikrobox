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
    assert "ip link add mbmgmt type bridge" in cmds
    assert "ip addr add 10.99.0.1/16 dev mbmgmt" in cmds


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
