"""HTTP and websocket API."""

from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .config import Settings
from .console import netns_console, serial_console
from .models import (
    NAME_PATTERN, Endpoint, Lab, Link, Node, NodeKind, default_chr_ram, free_id,
)
from .runtime import Conflict, Invalid, NotFound, Runtime, iface_name, netns_name
from .shell import CommandError
from .store import Store

STATIC = Path(__file__).parent / "static"


class LabCreate(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)


class NodeCreate(BaseModel):
    kind: NodeKind = NodeKind.chr
    name: str = Field(pattern=NAME_PATTERN)
    ports: Optional[int] = Field(default=None, ge=1)
    cpus: int = Field(default=1, ge=1, le=8)
    ram_mb: Optional[int] = Field(default=None, ge=128, le=16384)
    address: Optional[str] = None
    gateway: Optional[str] = None


class NodeUpdate(BaseModel):
    name: Optional[str] = Field(default=None, pattern=NAME_PATTERN)
    ports: Optional[int] = Field(default=None, ge=1)
    cpus: Optional[int] = Field(default=None, ge=1, le=8)
    ram_mb: Optional[int] = Field(default=None, ge=128, le=16384)
    address: Optional[str] = None
    gateway: Optional[str] = None


class LinkCreate(BaseModel):
    a: Endpoint
    b: Optional[Endpoint] = None  # omit to plug "a" into the management bridge


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir / "state.json")
    rt = Runtime(settings, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt.ensure_host()
        yield

    app = FastAPI(title="Mikrobox", version=__version__, lifespan=lifespan)
    app.state.rt = rt

    for exc, code in ((NotFound, 404), (Conflict, 409), (Invalid, 400), (CommandError, 500)):
        def handler(request, e, code=code):
            return JSONResponse({"detail": str(e)}, status_code=code)
        app.add_exception_handler(exc, handler)

    @app.exception_handler(ValidationError)
    def invalid_model(request, e: ValidationError):
        detail = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        return JSONResponse({"detail": detail}, status_code=400)

    # -- helpers ---------------------------------------------------------

    def get_lab(lab_id: int) -> Lab:
        lab = store.state.lab(lab_id)
        if lab is None:
            raise NotFound(f"Lab {lab_id} not found.")
        return lab

    def get_node(lab: Lab, node_id: int) -> Node:
        node = lab.node(node_id)
        if node is None:
            raise NotFound(f"Node {node_id} not found in lab {lab.id}.")
        return node

    def port_limit(kind: NodeKind) -> int:
        return settings.max_chr_ports if kind == NodeKind.chr else settings.max_client_ports

    def check_ports(kind: NodeKind, ports: int) -> None:
        if ports > port_limit(kind):
            raise Invalid(f"A {kind.value} node can have at most {port_limit(kind)} ports.")

    def check_name(lab: Lab, name: str, skip: int | None = None) -> None:
        if any(n.name == name and n.id != skip for n in lab.nodes):
            raise Conflict(f"A node named {name} already exists in this lab.")

    def node_view(lab: Lab, node: Node) -> dict:
        ip = rt.mgmt_ip(lab, node)
        view = node.model_dump(mode="json")
        view["running"] = rt.node_running(node)
        view["mgmt_ip"] = ip
        view["ssh"] = f"ssh admin@{ip}" if ip and node.kind == NodeKind.chr else None
        view["console"] = f"/console?lab={lab.id}&node={node.id}"
        return view

    def lab_view(lab: Lab) -> dict:
        return {
            "id": lab.id,
            "name": lab.name,
            "nodes": [node_view(lab, n) for n in lab.nodes],
            "links": [l.model_dump(mode="json") for l in lab.links],
        }

    # -- system ----------------------------------------------------------

    @app.get("/api/system")
    def system():
        return rt.system_info()

    @app.get("/console", include_in_schema=False)
    def console_page():
        return FileResponse(STATIC / "console.html")

    # -- labs ------------------------------------------------------------

    @app.get("/api/labs")
    def list_labs():
        return [lab_view(lab) for lab in store.state.labs]

    @app.post("/api/labs", status_code=201)
    def create_lab(body: LabCreate):
        with rt.lock:
            if any(l.name == body.name for l in store.state.labs):
                raise Conflict(f"A lab named {body.name} already exists.")
            lab = Lab(id=free_id({l.id for l in store.state.labs}), name=body.name)
            store.state.labs.append(lab)
            store.save()
            return lab_view(lab)

    @app.get("/api/labs/{lab_id}")
    def read_lab(lab_id: int):
        return lab_view(get_lab(lab_id))

    @app.delete("/api/labs/{lab_id}", status_code=204)
    def delete_lab(lab_id: int):
        with rt.lock:
            lab = get_lab(lab_id)
            rt.stop_lab(lab)
            for node in lab.nodes:
                rt.delete_disk(node)
            store.state.labs.remove(lab)
            store.save()
            rt.sync_dhcp()

    @app.post("/api/labs/{lab_id}/start")
    def start_lab(lab_id: int):
        lab = get_lab(lab_id)
        rt.start_lab(lab)
        return lab_view(lab)

    @app.post("/api/labs/{lab_id}/stop")
    def stop_lab(lab_id: int):
        lab = get_lab(lab_id)
        rt.stop_lab(lab)
        return lab_view(lab)

    # -- nodes -----------------------------------------------------------

    @app.post("/api/labs/{lab_id}/nodes", status_code=201)
    def create_node(lab_id: int, body: NodeCreate):
        with rt.lock:
            lab = get_lab(lab_id)
            check_name(lab, body.name)
            is_chr = body.kind == NodeKind.chr
            ports = body.ports or (8 if is_chr else 1)
            check_ports(body.kind, ports)
            if is_chr and (body.address or body.gateway):
                raise Invalid("address and gateway apply to client nodes only.")
            ram = body.ram_mb or (default_chr_ram(body.cpus, ports) if is_chr else 128)
            node = Node(
                id=free_id({n.id for n in store.state.all_nodes()}),
                kind=body.kind, name=body.name, ports=ports, cpus=body.cpus,
                ram_mb=ram, address=body.address, gateway=body.gateway,
            )
            lab.nodes.append(node)
            store.save()
            return node_view(lab, node)

    @app.patch("/api/labs/{lab_id}/nodes/{node_id}")
    def update_node(lab_id: int, node_id: int, body: NodeUpdate):
        with rt.lock:
            lab = get_lab(lab_id)
            node = get_node(lab, node_id)
            if rt.node_running(node):
                raise Conflict(f"Stop {node.name} before changing it.")
            changes = body.model_dump(exclude_unset=True)
            if "name" in changes:
                check_name(lab, changes["name"], skip=node.id)
            if "ports" in changes:
                check_ports(node.kind, changes["ports"])
                used = max(
                    (e.port for l in lab.links for e in l.endpoints() if e.node == node.id),
                    default=0,
                )
                if changes["ports"] < used:
                    raise Conflict(f"Port {used} is connected; remove that link first.")
            updated = Node.model_validate({**node.model_dump(), **changes})
            lab.nodes[lab.nodes.index(node)] = updated
            store.save()
            rt.sync_dhcp()
            return node_view(lab, updated)

    @app.delete("/api/labs/{lab_id}/nodes/{node_id}", status_code=204)
    def delete_node(lab_id: int, node_id: int):
        with rt.lock:
            lab = get_lab(lab_id)
            node = get_node(lab, node_id)
            rt.stop_node(lab, node)
            rt.delete_disk(node)
            lab.links = [l for l in lab.links if not l.touches(node.id)]
            lab.nodes.remove(node)
            store.save()
            rt.sync_dhcp()

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/start")
    def start_node(lab_id: int, node_id: int):
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        rt.start_node(lab, node)
        return node_view(lab, node)

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/stop")
    def stop_node(lab_id: int, node_id: int):
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        rt.stop_node(lab, node)
        return node_view(lab, node)

    # -- links -----------------------------------------------------------

    @app.post("/api/labs/{lab_id}/links", status_code=201)
    def create_link(lab_id: int, body: LinkCreate):
        with rt.lock:
            lab = get_lab(lab_id)
            rt.check_endpoint(lab, body.a)
            if body.b is not None:
                if body.a == body.b:
                    raise Invalid("A port cannot be linked to itself.")
                rt.check_endpoint(lab, body.b)
            link = Link(
                id=free_id({l.id for l in store.state.all_links()}), a=body.a, b=body.b
            )
            lab.links.append(link)
            store.save()
            rt.sync_dhcp()
            rt.apply_link(lab, link)
            return link.model_dump(mode="json")

    @app.delete("/api/labs/{lab_id}/links/{link_id}", status_code=204)
    def delete_link(lab_id: int, link_id: int):
        with rt.lock:
            lab = get_lab(lab_id)
            link = next((l for l in lab.links if l.id == link_id), None)
            if link is None:
                raise NotFound(f"Link {link_id} not found in lab {lab_id}.")
            rt.remove_link(link)
            lab.links.remove(link)
            store.save()
            rt.sync_dhcp()

    # -- capture ---------------------------------------------------------

    @app.get("/api/labs/{lab_id}/nodes/{node_id}/ports/{port}/capture")
    def capture(lab_id: int, node_id: int, port: int):
        """Live pcap stream. Pipe it into Wireshark: curl -sN URL | wireshark -k -i -"""
        node = get_node(get_lab(lab_id), node_id)
        iface = iface_name(node.id, port)
        if port < 1 or port > node.ports or not rt.net.exists(iface):
            raise Conflict(f"{node.name} port {port} is not up. Start the node first.")

        def stream():
            proc = subprocess.Popen(
                ["tcpdump", "-U", "-n", "-i", iface, "-w", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            try:
                while chunk := proc.stdout.read1(65536):
                    yield chunk
            finally:
                proc.terminate()

        return StreamingResponse(
            stream(),
            media_type="application/vnd.tcpdump.pcap",
            headers={"Content-Disposition": f'attachment; filename="{node.name}-p{port}.pcap"'},
        )

    # -- console ---------------------------------------------------------

    @app.websocket("/api/labs/{lab_id}/nodes/{node_id}/console")
    async def console(ws: WebSocket, lab_id: int, node_id: int):
        await ws.accept()
        lab = store.state.lab(lab_id)
        node = lab.node(node_id) if lab else None
        if node is None or not rt.node_running(node):
            await ws.send_text("\r\nNode is not running.\r\n")
            await ws.close()
            return
        try:
            if node.kind == NodeKind.chr:
                await serial_console(ws, rt.serial_socket(node))
            else:
                await netns_console(ws, netns_name(node.id), node.name)
        except OSError as e:
            await ws.send_text(f"\r\nConsole unavailable: {e}\r\n")
        try:
            await ws.close()
        except RuntimeError:
            pass

    return app
