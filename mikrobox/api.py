"""HTTP and websocket API."""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import re
import subprocess
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Query, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import __version__, routeros, templates
from .config import Settings
from .console import netns_console, serial_console
from .packets import parse_line
from .models import (
    NAME_PATTERN, Annotation, Endpoint, Lab, Link, Node, NodeKind, default_chr_ram, free_id,
)
from .runtime import Conflict, Invalid, NotFound, Runtime, iface_name, netns_name
from .shell import CommandError
from .store import Store

STATIC = Path(__file__).parent / "static"


class LabCreate(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)


class LabUpdate(BaseModel):
    name: Optional[str] = Field(default=None, pattern=NAME_PATTERN)
    mgmt_x: Optional[float] = None
    mgmt_y: Optional[float] = None


class NodeCreate(BaseModel):
    kind: NodeKind = NodeKind.chr
    role: Literal["router", "switch"] = "router"
    name: str = Field(pattern=NAME_PATTERN)
    ports: Optional[int] = Field(default=None, ge=1)
    cpus: int = Field(default=1, ge=1, le=8)
    ram_mb: Optional[int] = Field(default=None, ge=128, le=16384)
    address: Optional[str] = None
    gateway: Optional[str] = None
    dns: Optional[str] = None
    x: float = 0
    y: float = 0


class NodeUpdate(BaseModel):
    name: Optional[str] = Field(default=None, pattern=NAME_PATTERN)
    ports: Optional[int] = Field(default=None, ge=1)
    cpus: Optional[int] = Field(default=None, ge=1, le=8)
    ram_mb: Optional[int] = Field(default=None, ge=128, le=16384)
    address: Optional[str] = None
    gateway: Optional[str] = None
    dns: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None


class AnnotationCreate(BaseModel):
    kind: Literal["rect", "ellipse", "text", "note"]
    x: float
    y: float
    w: float
    h: float
    text: str = ""
    color: Literal["gray", "blue", "green", "amber", "red", "purple"] = "blue"
    fill: Literal["none", "light", "medium", "strong"] = "light"
    border: Literal["dashed", "solid", "dotted", "none"] = "dashed"
    border_width: float = 1.5
    radius: float = 12
    label_pos: Literal["auto", "top-left", "top", "center", "bottom-left", "bottom"] = "auto"
    label_size: Literal["s", "m", "l", "xl"] = "m"


class AnnotationUpdate(BaseModel):
    x: Optional[float] = None
    y: Optional[float] = None
    w: Optional[float] = None
    h: Optional[float] = None
    text: Optional[str] = None
    color: Optional[Literal["gray", "blue", "green", "amber", "red", "purple"]] = None
    fill: Optional[Literal["none", "light", "medium", "strong"]] = None
    border: Optional[Literal["dashed", "solid", "dotted", "none"]] = None
    border_width: Optional[float] = None
    radius: Optional[float] = None
    label_pos: Optional[Literal["auto", "top-left", "top", "center", "bottom-left", "bottom"]] = None
    label_size: Optional[Literal["s", "m", "l", "xl"]] = None


class LabImport(BaseModel):
    format: Literal["mikrobox-lab"]
    name: str = Field(pattern=NAME_PATTERN)
    nodes: list[Node] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    mgmt_x: float = 90
    mgmt_y: float = 80


class Creds(BaseModel):
    # Address to reach the router on; needed when it is not known automatically.
    host: Optional[str] = None
    username: str = Field(default="admin", min_length=1, max_length=64)
    password: str = Field(default="", max_length=256)


class ConfigExport(Creds):
    sensitive: bool = False


class ConfigImport(Creds):
    config: str = Field(min_length=1, max_length=2_000_000)


class TemplateUse(BaseModel):
    name: Optional[str] = Field(default=None, pattern=NAME_PATTERN)


class NodeDuplicate(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    copy_config: bool = False


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

    for exc, code in ((NotFound, 404), (Conflict, 409), (Invalid, 400), (CommandError, 500),
                      (routeros.RouterError, 502)):
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
        view["cloud_port"] = rt.cloud_port(lab, node)
        view["ssh"] = f"ssh admin@{ip}" if ip and node.kind == NodeKind.chr else None
        view["console"] = f"/console?lab={lab.id}&node={node.id}"
        return view

    def lab_view(lab: Lab) -> dict:
        return {
            "id": lab.id,
            "name": lab.name,
            "mgmt_x": lab.mgmt_x,
            "mgmt_y": lab.mgmt_y,
            "nodes": [node_view(lab, n) for n in lab.nodes],
            "links": [l.model_dump(mode="json") for l in lab.links],
            "annotations": [a.model_dump(mode="json") for a in lab.annotations],
        }

    def router_host(lab: Lab, node: Node, override: str | None = None) -> str:
        """Address of a running router on the Cloud, or a clear reason why not."""
        if node.kind != NodeKind.chr:
            raise Invalid(f"{node.name} is not a router.")
        if not rt.node_running(node):
            raise Conflict(f"{node.name} is not running.")
        if override:
            try:
                return str(ipaddress.ip_address(override.strip()))
            except ValueError:
                raise Invalid(f"{override} is not an IP address.")
        if rt.cloud_port(lab, node) is None:
            raise Conflict(f"{node.name} is not linked to the Cloud, so it cannot be reached.")
        ip = rt.mgmt_ip(lab, node)
        if ip is None:
            raise Conflict(f"{node.name}'s address is not known yet. Enter it, or check the "
                           "router's console with /ip address print.")
        return ip

    # -- system ----------------------------------------------------------

    @app.get("/api/system")
    def system():
        return rt.system_info()

    @app.get("/", include_in_schema=False)
    def index_page():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")

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

    @app.post("/api/labs/import", status_code=201)
    def import_lab(body: LabImport):
        """Recreate a lab from an export. Topology only; router disks start fresh."""
        with rt.lock:
            names = {l.name for l in store.state.labs}
            name, n = body.name, 1
            while name in names:
                n += 1
                name = f"{body.name}-{n}"[:63]
            used = {x.id for x in store.state.all_nodes()}
            idmap: dict[int, int] = {}
            nodes = []
            for node in body.nodes:
                check_ports(node.kind, node.ports)
                new = free_id(used)
                used.add(new)
                idmap[node.id] = new
                nodes.append(node.model_copy(update={"id": new}))
            lab = Lab(id=free_id({l.id for l in store.state.labs}), name=name, nodes=nodes,
                      mgmt_x=body.mgmt_x, mgmt_y=body.mgmt_y)
            used_links = {l.id for l in store.state.all_links()}
            for link in body.links:
                ends = []
                for e in link.endpoints():
                    if e.node not in idmap:
                        raise Invalid(f"A link refers to node {e.node}, which is not in the file.")
                    ep = Endpoint(node=idmap[e.node], port=e.port)
                    rt.check_endpoint(lab, ep)
                    ends.append(ep)
                new = free_id(used_links)
                used_links.add(new)
                lab.links.append(Link(id=new, a=ends[0], b=ends[1] if len(ends) > 1 else None))
            lab.annotations = [a.model_copy(update={"id": i + 1})
                               for i, a in enumerate(body.annotations)]
            store.state.labs.append(lab)
            store.save()
            rt.sync_dhcp()
            return lab_view(lab)

    @app.get("/api/templates")
    def list_templates():
        return templates.describe()

    @app.post("/api/templates/{template_id}", status_code=201)
    def use_template(template_id: str, body: TemplateUse):
        """Create a new lab from a ready-made layout."""
        if template_id not in templates.TEMPLATES:
            raise NotFound(f"No template called {template_id}.")
        data = templates.TEMPLATES[template_id][2]().data(body.name or template_id)
        return import_lab(LabImport.model_validate(data))

    @app.get("/api/labs/{lab_id}/export")
    def export_lab(lab_id: int):
        lab = get_lab(lab_id)
        data = {"format": "mikrobox-lab", **lab.model_dump(mode="json", exclude={"id"})}
        return JSONResponse(data, headers={
            "Content-Disposition": f'attachment; filename="{lab.name}.mikrobox.json"'})

    @app.post("/api/labs/{lab_id}/config/export")
    def export_all_configs(lab_id: int, body: ConfigExport):
        """Zip of /export from every running router that is on Management."""
        lab = get_lab(lab_id)
        buf, problems, done = io.BytesIO(), [], 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for node in lab.nodes:
                if node.kind != NodeKind.chr:
                    continue
                try:
                    host = router_host(lab, node)  # per-router: the override does not apply
                    z.writestr(f"{node.name}.txt", routeros.export_config(
                        host, body.username, body.password, body.sensitive))
                    done += 1
                except (Conflict, routeros.RouterError) as e:
                    problems.append(f"{node.name}: {e}")
            if problems:
                z.writestr("SKIPPED.txt", "\n".join(problems) + "\n")
        if not done:
            raise Conflict("No router config could be exported. " + " ".join(problems))
        return Response(buf.getvalue(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{lab.name}-configs.zip"',
            "X-Mikrobox-Skipped": str(len(problems))})

    @app.get("/api/labs/{lab_id}")
    def read_lab(lab_id: int):
        return lab_view(get_lab(lab_id))

    @app.patch("/api/labs/{lab_id}")
    def update_lab(lab_id: int, body: LabUpdate):
        with rt.lock:
            lab = get_lab(lab_id)
            changes = body.model_dump(exclude_unset=True, exclude_none=True)
            name = changes.get("name")
            if name and any(l.name == name and l.id != lab.id for l in store.state.labs):
                raise Conflict(f"A lab named {name} already exists.")
            for key, value in changes.items():
                setattr(lab, key, value)
            store.save()
            return lab_view(lab)

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
            if body.role == "switch" and not is_chr:
                raise Invalid("Only a CHR can be a switch.")
            ports = body.ports or ((24 if body.role == "switch" else 8) if is_chr else 1)
            check_ports(body.kind, ports)
            if is_chr and (body.address or body.gateway or body.dns):
                raise Invalid("address, gateway and dns apply to client nodes only.")
            ram = body.ram_mb or (default_chr_ram(body.cpus, ports) if is_chr else 128)
            node = Node(
                id=free_id({n.id for n in store.state.all_nodes()}),
                kind=body.kind, role=body.role, name=body.name, ports=ports, cpus=body.cpus,
                ram_mb=ram, address=body.address, gateway=body.gateway, dns=body.dns,
                x=body.x, y=body.y,
            )
            lab.nodes.append(node)
            store.save()
            return node_view(lab, node)

    @app.patch("/api/labs/{lab_id}/nodes/{node_id}")
    def update_node(lab_id: int, node_id: int, body: NodeUpdate):
        with rt.lock:
            lab = get_lab(lab_id)
            node = get_node(lab, node_id)
            changes = body.model_dump(exclude_unset=True)
            for key in ("x", "y", "name", "ports", "cpus", "ram_mb"):
                if changes.get(key, 0) is None:
                    del changes[key]
            if rt.node_running(node) and set(changes) - {"x", "y"}:
                raise Conflict(f"Stop {node.name} before changing it.")
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

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/duplicate", status_code=201)
    def duplicate_node(lab_id: int, node_id: int, body: NodeDuplicate):
        """A new device with the same settings and no links."""
        with rt.lock:
            lab = get_lab(lab_id)
            src = get_node(lab, node_id)
            check_name(lab, body.name)
            if body.copy_config:
                if src.kind != NodeKind.chr:
                    raise Invalid("Only routers have a configuration to copy.")
                if rt.node_running(src):
                    raise Conflict(f"Stop {src.name} first to copy its configuration.")
            node = src.model_copy(update={
                "id": free_id({n.id for n in store.state.all_nodes()}), "name": body.name,
                "x": src.x + 60, "y": src.y + 60,
                "address": None,  # two clients with one address would only confuse
            })
            rt.delete_disk(node)  # a stale disk from an earlier node with this id
            copied = body.copy_config and rt.copy_disk(src, node)
            lab.nodes.append(node)
            store.save()
            return {**node_view(lab, node), "config_copied": bool(copied)}

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/reset")
    def reset_node(lab_id: int, node_id: int):
        """Throw away the router's disk so it boots with factory defaults."""
        with rt.lock:
            lab = get_lab(lab_id)
            node = get_node(lab, node_id)
            if rt.node_running(node):
                raise Conflict(f"Stop {node.name} before resetting it.")
            rt.delete_disk(node)
            return node_view(lab, node)

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/config/export")
    def export_config(lab_id: int, node_id: int, body: ConfigExport):
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        text = routeros.export_config(
            router_host(lab, node, body.host), body.username, body.password, body.sensitive)
        return PlainTextResponse(text, headers={
            "Content-Disposition": f'attachment; filename="{node.name}.txt"'})

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/config/import")
    def import_config(lab_id: int, node_id: int, body: ConfigImport):
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        output = routeros.import_config(
            router_host(lab, node, body.host), body.username, body.password, body.config)
        return {"output": output}

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/identity")
    def push_identity(lab_id: int, node_id: int, body: Creds):
        """Set the router's RouterOS identity to the node's name."""
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        output = routeros.set_identity(
            router_host(lab, node, body.host), body.username, body.password, node.name)
        return {"output": output}

    # -- annotations -----------------------------------------------------

    @app.post("/api/labs/{lab_id}/annotations", status_code=201)
    def create_annotation(lab_id: int, body: AnnotationCreate):
        with rt.lock:
            lab = get_lab(lab_id)
            if len(lab.annotations) >= 500:
                raise Conflict("This lab already has 500 shapes and notes.")
            anno = Annotation(id=free_id({a.id for a in lab.annotations}), **body.model_dump())
            lab.annotations.append(anno)
            store.save()
            return anno.model_dump(mode="json")

    @app.patch("/api/labs/{lab_id}/annotations/{anno_id}")
    def update_annotation(lab_id: int, anno_id: int, body: AnnotationUpdate):
        with rt.lock:
            lab = get_lab(lab_id)
            anno = next((a for a in lab.annotations if a.id == anno_id), None)
            if anno is None:
                raise NotFound(f"Shape {anno_id} not found in lab {lab_id}.")
            changes = body.model_dump(exclude_unset=True, exclude_none=True)
            updated = Annotation.model_validate({**anno.model_dump(), **changes})
            lab.annotations[lab.annotations.index(anno)] = updated
            store.save()
            return updated.model_dump(mode="json")

    @app.delete("/api/labs/{lab_id}/annotations/{anno_id}", status_code=204)
    def delete_annotation(lab_id: int, anno_id: int):
        with rt.lock:
            lab = get_lab(lab_id)
            lab.annotations = [a for a in lab.annotations if a.id != anno_id]
            store.save()

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
    def capture(
        lab_id: int, node_id: int, port: int,
        seconds: Optional[int] = Query(default=None, ge=1, le=3600),
    ):
        """Pcap stream. Live: curl -sN URL | wireshark -k -i -   Timed file: add ?seconds=30"""
        node = get_node(get_lab(lab_id), node_id)
        iface = iface_name(node.id, port)
        if port < 1 or port > node.ports or not rt.net.exists(iface):
            raise Conflict(f"{node.name} port {port} is not up. Start the node first.")

        def stream():
            cmd = ["tcpdump", "-U", "-n", "-i", iface, "-w", "-"]
            if seconds:
                cmd = ["timeout", str(seconds)] + cmd
            proc = subprocess.Popen(
                cmd,
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

    @app.websocket("/api/labs/{lab_id}/nodes/{node_id}/ports/{port}/packets")
    async def packets(ws: WebSocket, lab_id: int, node_id: int, port: int,
                      bpf: str = Query(default="", alias="filter")):
        """Live decoded packets as JSON: {"packets": [...]} batches, or {"error": "..."}."""
        await ws.accept()
        lab = store.state.lab(lab_id)
        node = lab.node(node_id) if lab else None
        iface = iface_name(node_id, port)
        if node is None or port < 1 or port > node.ports or not rt.net.exists(iface):
            await ws.send_text(json.dumps({"error": "That port is not up. Start the device first."}))
            await ws.close()
            return
        if len(bpf) > 200 or not re.fullmatch(r"[\w\s.:/()\[\]!=<>&|-]*", bpf):
            await ws.send_text(json.dumps({"error": "That filter has characters that are not allowed."}))
            await ws.close()
            return
        proc = await asyncio.create_subprocess_exec(
            "tcpdump", "-l", "-n", "-e", "-i", iface, *bpf.split(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        async def pump():
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                batch = [line]
                # Group whatever else arrives within a moment into one message.
                try:
                    while len(batch) < 200:
                        more = await asyncio.wait_for(proc.stdout.readline(), 0.1)
                        if not more:
                            break
                        batch.append(more)
                except asyncio.TimeoutError:
                    pass
                rows = [r for r in (parse_line(l.decode(errors="replace")) for l in batch) if r]
                if rows:
                    await ws.send_text(json.dumps({"packets": rows}))
            err = (await proc.stderr.read()).decode(errors="replace")
            bad = [l for l in err.splitlines() if "error" in l.lower()]
            if bad:
                await ws.send_text(json.dumps({"error": bad[-1].replace("tcpdump: ", "")}))

        async def wait_closed():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return

        tasks = [asyncio.create_task(pump()), asyncio.create_task(wait_closed())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if proc.returncode is None:
                proc.terminate()
            try:
                await ws.close()
            except Exception:  # the browser already went away
                pass

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
