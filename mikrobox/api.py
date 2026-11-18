"""HTTP and websocket API."""

from __future__ import annotations

import asyncio
import base64
import io
import time
import ipaddress
import json
import re
import subprocess
import threading
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError

from . import __version__, routeros, templates
from . import clients as clients_mod
from .backup import INTERVALS, Backups
from .auth import COOKIE, MIN_PASSWORD, SESSION_SECONDS, Auth, AuthError, AuthMiddleware, scope_ip
from .config import Settings
from . import console as consoles
from .activity import Activity
from .events import Events
from . import docs
from .packets import Assembler
from .packets import decode as decode_packet
from .models import (
    NAME_PATTERN, Annotation, ContainerSpec, Endpoint, Lab, Link, Node, NodeKind, Vlan, default_chr_ram, free_id,
)
from .docker import EngineError
from .weblinks import LinkError, WebLinks, parse_url, plain_ip
from .runtime import Conflict, Invalid, NotFound, Runtime
from .shell import CommandError
from .store import Store
from .trash import Trash, free_name
from .selfcheck import SelfCheck

STATIC = Path(__file__).parent / "static"
ANNO_WORDS = {"rect": "an area", "ellipse": "an area", "text": "a text label", "note": "a note"}


class LabCreate(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)


class LabUpdate(BaseModel):
    name: Optional[str] = Field(default=None, pattern=NAME_PATTERN)
    mgmt_x: Optional[float] = None
    mgmt_y: Optional[float] = None


class NodeCreate(BaseModel):
    kind: NodeKind = NodeKind.chr
    role: Literal["router", "switch", "pc", "server"] = "router"  # a client: pc (default) or server
    layer: Literal[2, 3] = 2
    bond: Optional[Literal["lacp", "active-backup"]] = None
    nic_addresses: list[Optional[str]] = Field(default_factory=list, max_length=3)
    vlans: list[Vlan] = Field(default_factory=list, max_length=8)
    name: str = Field(pattern=NAME_PATTERN)
    ports: Optional[int] = Field(default=None, ge=1)
    cpus: int = Field(default=1, ge=1, le=8)
    ram_mb: Optional[int] = Field(default=None, ge=128, le=16384)
    address: Optional[str] = None
    gateway: Optional[str] = None
    dns: Optional[str] = None
    addressing: Literal["static", "dhcp"] = "static"
    services: list[Literal["web", "iperf", "dhcp", "dns"]] = Field(default_factory=list)
    x: float = 0
    y: float = 0


class DockerSwitch(BaseModel):
    on: bool


class WebLinkCreate(BaseModel):
    lab: int
    node: int
    container: Optional[str] = None  # one of its published ports...
    port: Optional[int] = None
    url: Optional[str] = Field(default=None, max_length=2000)  # ...or any address the PC can reach
    who: Literal["me", "anyone"] = "me"
    mode: Literal["link", "view"] = "link"  # view: for the viewer tab, always for you only


class NodeUpdate(BaseModel):
    name: Optional[str] = Field(default=None, pattern=NAME_PATTERN)
    layer: Optional[Literal[2, 3]] = None  # switches only; it changes how it is drawn
    role: Optional[Literal["pc", "server"]] = None  # clients only
    bond: Optional[Literal["lacp", "active-backup"]] = None  # servers; null for no bond
    nic_addresses: Optional[list[Optional[str]]] = Field(default=None, max_length=3)
    vlans: Optional[list[Vlan]] = Field(default=None, max_length=8)
    ports: Optional[int] = Field(default=None, ge=1)
    cpus: Optional[int] = Field(default=None, ge=1, le=8)
    ram_mb: Optional[int] = Field(default=None, ge=128, le=16384)
    address: Optional[str] = None
    gateway: Optional[str] = None
    dns: Optional[str] = None
    addressing: Optional[Literal["static", "dhcp"]] = None
    services: Optional[list[Literal["web", "iperf", "dhcp", "dns"]]] = None
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


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordChange(BaseModel):
    current: str = Field(max_length=256)
    new: str = Field(max_length=256)


class BackupSettings(BaseModel):
    enabled: Optional[bool] = None
    scope: Optional[Literal["all", "selected"]] = None
    interval: Optional[int] = None  # minutes
    username: Optional[str] = Field(default=None, min_length=1, max_length=64)
    password: Optional[str] = Field(default=None, max_length=256)  # omit to keep the stored one
    sensitive: Optional[bool] = None


class BackupInclude(BaseModel):
    included: bool


class BackupRun(BaseModel):
    node: Optional[int] = None  # omit for every included router


class Restore(BaseModel):
    mode: Literal["replace", "merge"] = "replace"


class DocExport(BaseModel):
    png: Optional[str] = Field(default=None, max_length=30_000_000)  # base64 topology image
    refresh: bool = True  # take a fresh backup of running routers first


class ReachTest(BaseModel):
    target: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
    tool: Literal["ping", "traceroute", "iperf"] = "ping"


class PresenceBeat(BaseModel):
    client: str = Field(min_length=4, max_length=40)
    lab: Optional[int] = None
    node: Optional[int] = None


class PresenceLeave(BaseModel):
    client: str = Field(min_length=4, max_length=40)


class LabClear(BaseModel):
    confirm: str = Field(max_length=100)  # the lab's name, typed by the person


class TemplateUse(BaseModel):
    name: Optional[str] = Field(default=None, pattern=NAME_PATTERN)


# -- editing: several things at once, and undo ------------------------------

class Pos(BaseModel):
    id: int
    x: float
    y: float


class Move(BaseModel):
    nodes: list[Pos] = Field(default_factory=list, max_length=2000)
    annotations: list[Pos] = Field(default_factory=list, max_length=2000)
    cloud: Optional[Pos] = None  # id is ignored


class Items(BaseModel):
    nodes: list[int] = Field(default_factory=list, max_length=2000)
    links: list[int] = Field(default_factory=list, max_length=4000)
    annotations: list[int] = Field(default_factory=list, max_length=2000)


class Undelete(BaseModel):
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{8,40}$")


class PasteEnd(BaseModel):
    ref: int
    port: int = Field(ge=1)


class PasteLink(BaseModel):
    a: PasteEnd
    b: PasteEnd


class PasteNode(BaseModel):
    ref: int                      # the client's own key, for links and the answer
    source: Optional[int] = None  # the device copied, whose configuration can come along
    kind: NodeKind = NodeKind.chr
    role: Literal["router", "switch", "pc", "server"] = "router"
    bond: Optional[Literal["lacp", "active-backup"]] = None
    nic_addresses: list[Optional[str]] = Field(default_factory=list, max_length=3)
    vlans: list[Vlan] = Field(default_factory=list, max_length=8)
    layer: Literal[2, 3] = 2
    name: str = Field(pattern=NAME_PATTERN)
    ports: int = Field(ge=1)
    cpus: int = Field(default=1, ge=1, le=8)
    ram_mb: Optional[int] = Field(default=None, ge=128, le=16384)
    address: Optional[str] = None
    gateway: Optional[str] = None
    dns: Optional[str] = None
    addressing: Literal["static", "dhcp"] = "static"
    services: list[Literal["web", "iperf", "dhcp", "dns"]] = Field(default_factory=list)
    docker: bool = False
    containers: list[ContainerSpec] = Field(default_factory=list, max_length=32)
    x: float = 0
    y: float = 0


class Paste(BaseModel):
    nodes: list[PasteNode] = Field(default_factory=list, max_length=128)
    links: list[PasteLink] = Field(default_factory=list, max_length=1024)
    annotations: list[AnnotationCreate] = Field(default_factory=list, max_length=500)
    source_lab: Optional[int] = None  # where it was copied from
    with_config: bool = False         # copy routers' disks too (stopped ones)


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

    backups = Backups(rt)

    events = Events()
    activity = Activity(settings.data_dir / "activity.jsonl", events)
    weblinks = WebLinks(rt, settings.web_ports, on_change=lambda: events.publish({"t": "web"}))
    trash = Trash(settings.data_dir / "trash")
    trash.purge()
    selfcheck = SelfCheck(rt, settings.web_ports)
    store.on_save = lambda: events.publish({"t": "labs"})
    backups.activity, backups.events = activity, events

    def changed() -> None:
        """For changes that are not saved to the lab file, such as starting a device."""
        events.publish({"t": "labs"})

    def what(node: Node) -> str:
        if node.kind == NodeKind.client:
            return "server" if node.role == "server" else "PC"
        if node.role == "switch":
            return "layer 3 switch" if node.layer == 3 else "switch"
        return "router"

    def link_text(lab: Lab, link: Link) -> str:
        a = lab.node(link.a.node)
        a_text = f"{a.name} {docs.port_name(a, link.a.port)}" if a else "?"
        if link.b is None:
            return f"{a_text} to the Cloud"
        b = lab.node(link.b.node)
        return f"{a_text} to {b.name} {docs.port_name(b, link.b.port)}" if b else f"{a_text} to ?"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        events.bind(asyncio.get_running_loop())
        weblinks.bind(asyncio.get_running_loop())
        rt.ensure_host()
        rt.resume()
        if not settings.dry_run:
            threading.Thread(target=backups.loop, daemon=True).start()
        yield

    app = FastAPI(title="Mikrobox", version=__version__, lifespan=lifespan)
    app.state.rt = rt
    app.state.backups = backups
    app.state.events, app.state.activity = events, activity
    app.state.weblinks = weblinks
    hubs: dict[str, consoles.Hub] = {}  # shared consoles, by "lab/node"
    presence: dict[str, dict] = {}  # browser tab -> who, where, when
    auth = Auth(settings.data_dir, enabled=settings.auth_enabled)
    app.state.auth = auth
    app.add_middleware(AuthMiddleware, auth=auth)

    for exc, code in ((NotFound, 404), (Conflict, 409), (Invalid, 400), (CommandError, 500), (AuthError, 400), (LinkError, 409),
                      (routeros.RouterError, 502)):
        def handler(request, e, code=code):
            return JSONResponse({"detail": str(e)}, status_code=code)
        app.add_exception_handler(exc, handler)

    @app.exception_handler(Exception)
    def unexpected(request, e: Exception):
        # Still logged in full by the server; the page gets something to show and send on.
        return JSONResponse({"detail": f"Something went wrong on the server ({type(e).__name__}: {e}). "
                                       "The full error is in the server's log."}, status_code=500)

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
        view["backup"] = backups.node_summary(lab, node)
        view["leased"] = rt.clients.leased(node) if view["running"] else None
        view["docker_summary"] = rt.docker.summary(lab, node) if node.docker and view["running"] else None
        return view

    def check_client(node: Node) -> None:
        """Rules for the client-only settings, checked on the final result of a change."""
        if node.kind != NodeKind.client:
            if node.addressing != "static" or node.services:
                raise Invalid("addressing and services apply to clients only.")
            if node.docker or node.containers:
                raise Invalid("Docker runs on Linux servers only.")
            if node.bond or node.nic_addresses:
                raise Invalid("Bonding and NIC addresses are for Linux servers.")
            if node.vlans:
                raise Invalid("VLANs here are for PCs and servers; on routers and switches, set them up in RouterOS.")
            return
        seen = set()
        for v in node.vlans:
            if node.bond and v.nic != "bond0":
                raise Invalid(f"{node.name}'s NICs are bonded: put VLAN {v.id} on bond0.")
            if not node.bond and (v.nic == "bond0" or int(v.nic[3:]) >= node.ports):
                raise Invalid(f"VLAN {v.id} is on {v.nic}, which {node.name} doesn't have.")
            if (v.nic, v.id) in seen:
                raise Invalid(f"VLAN {v.id} is on {v.nic} twice.")
            seen.add((v.nic, v.id))
        if node.role == "pc":
            # A PC is for testing from: one NIC and the tools. Servers do the rest.
            if node.services or node.docker or node.containers:
                raise Invalid(f"Only servers run services and Docker. Make {node.name} a server first.")
            if node.ports != 1:
                raise Invalid("A PC has one NIC. A server can have up to four.")
            if node.bond or node.nic_addresses:
                raise Invalid("Bonding and NIC addresses are for servers.")
        if node.bond and node.ports < 2:
            raise Invalid("A bond needs at least two NICs.")
        if node.bond and node.nic_addresses:
            raise Invalid("Bonded NICs share the bond's address. Leave the other NIC addresses empty.")
        if len(node.nic_addresses) > node.ports - 1:
            raise Invalid(f"{node.name} has {node.ports} NICs; there are addresses for more.")
        if ("dhcp" in node.services or "dns" in node.services) and (node.addressing != "static" or not node.address):
            raise Invalid("A DHCP or DNS server needs a fixed address. Set one, or switch those off.")
        if "dhcp" in node.services:
            try:
                clients_mod.dhcp_range(node.address)
            except ValueError as e:
                raise Invalid(str(e))

    def lab_view(lab: Lab) -> dict:
        return {
            "id": lab.id,
            "name": lab.name,
            "mgmt_x": lab.mgmt_x,
            "mgmt_y": lab.mgmt_y,
            "nodes": [node_view(lab, n) for n in lab.nodes],
            "links": [l.model_dump(mode="json") for l in lab.links],
            "annotations": [a.model_dump(mode="json") for a in lab.annotations],
            "backup": backups.summary(lab),
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

    # -- accounts --------------------------------------------------------

    def signed_in(response: JSONResponse, name: str, request: Request) -> JSONResponse:
        response.set_cookie(COOKIE, auth.new_session(name, scope_ip(request.scope)), max_age=SESSION_SECONDS,
                            httponly=True, samesite="lax", path="/")
        return response

    def current_user(request: Request) -> str | None:
        return getattr(request.state, "user", None)

    @app.get("/login", include_in_schema=False)
    def login_page():
        return FileResponse(STATIC / "login.html")

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        user = auth.user_from_headers(dict(request.scope["headers"]), scope_ip(request.scope)) if auth.enabled else None
        return {"enabled": auth.enabled, "setup_needed": auth.enabled and not auth.users, "user": user,
                "version": __version__, "min_password": MIN_PASSWORD}

    @app.post("/api/auth/setup")
    def auth_setup(body: Login, request: Request):
        """Create the very first account. Refused once any account exists."""
        with auth.lock:
            if auth.users:
                raise Conflict("An account already exists. Sign in instead.")
            name = auth.create_user(body.username, body.password)
        activity.record("created the first account", user=name)
        return signed_in(JSONResponse({"user": name}), name, request)

    @app.post("/api/auth/login")
    def auth_login(body: Login, request: Request):
        name = auth.verify(body.username, body.password)
        if name is None:
            known = body.username.lower() in auth.users
            activity.record("failed to sign in (wrong password)" if known else
                            f"failed to sign in as an unknown user, {body.username[:32]}",
                            kind="danger", user=body.username[:32] if known else "someone")
            return JSONResponse({"detail": "Wrong username or password."}, status_code=401)
        activity.record("signed in", user=name)
        return signed_in(JSONResponse({"user": name}), name, request)

    @app.post("/api/auth/logout")
    def auth_logout(request: Request):
        auth.end_session(auth.cookie_token(dict(request.scope["headers"])))
        response = JSONResponse({"user": None})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/users")
    def list_users(request: Request):
        return {"users": auth.names(), "me": current_user(request)}

    @app.post("/api/users", status_code=201)
    def create_user(body: Login):
        name = auth.create_user(body.username, body.password)
        activity.record(f"added the account {name}")
        return {"user": name}

    @app.delete("/api/users/{name}", status_code=204)
    def delete_user(name: str, request: Request):
        me = current_user(request)
        if me and me.lower() == name.lower():
            raise Conflict("You cannot remove the account you are signed in with.")
        auth.delete_user(name)
        activity.record(f"removed the account {name}", kind="danger")

    @app.post("/api/account/password")
    def change_password(body: PasswordChange, request: Request):
        me = current_user(request)
        if me is None:
            raise Conflict("Accounts are switched off on this server.")
        if auth.verify(me, body.current) is None:
            raise Invalid("The current password is wrong.")
        auth.set_password(me, body.new)
        activity.record("changed their password")
        return {"user": me}

    @app.post("/api/presence")
    def presence_beat(body: PresenceBeat, request: Request):
        """Each open workspace reports in every few seconds; returns who else is around."""
        now = time.time()
        before = presence.get(body.client)
        presence[body.client] = {"user": current_user(request) or "someone", "lab": body.lab,
                                 "node": body.node, "ts": now}
        stale = [k for k, v in presence.items() if now - v["ts"] > 30]
        for key in stale:
            del presence[key]
        if stale or before is None or (before["lab"], before["node"]) != (body.lab, body.node):
            events.publish({"t": "presence"})
        return {"others": [{"user": v["user"], "lab": v["lab"], "node": v["node"]}
                           for k, v in presence.items() if k != body.client]}

    @app.post("/api/presence/leave", status_code=204)
    def presence_leave(body: PresenceLeave):
        """Sent when a tab closes or reloads, so it does not linger as a ghost."""
        if presence.pop(body.client, None) is not None:
            events.publish({"t": "presence"})

    @app.websocket("/api/events")
    async def event_stream(ws: WebSocket):
        """Pushes {"t": "labs" | "presence" | "activity"} hints to the workspace."""
        await events.serve(ws)

    @app.get("/api/activity")
    def list_activity(lab: Optional[int] = None, limit: int = Query(default=200, ge=1, le=2000)):
        return activity.recent(lab, limit)

    # -- system ----------------------------------------------------------

    @app.get("/api/system")
    def system():
        return rt.system_info()

    @app.get("/api/system/check")
    async def system_check():
        """What Mikrobox needs from this VM, checked, with fixes (see selfcheck.py)."""
        return {"version": __version__, "checks": await run_in_threadpool(selfcheck.run)}

    @app.post("/api/system/check/docker")
    async def system_check_docker():
        """Start a Docker engine the way a server does, pull hello-world and run it."""
        return await run_in_threadpool(selfcheck.docker_test)

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
            activity.record(f"created the lab {lab.name}", lab)
            return lab_view(lab)

    @app.post("/api/labs/import", status_code=201)
    def import_lab(body: LabImport):
        """Recreate a lab from an export. Topology only; router disks start fresh."""
        view = do_import(body)
        activity.record(f"imported the lab {view['name']}", view["id"])
        return view

    def do_import(body: LabImport) -> dict:
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
        view = do_import(LabImport.model_validate(data))
        activity.record(f"created the lab {view['name']} from the {templates.TEMPLATES[template_id][0]} template",
                        view["id"])
        return view

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
        activity.record(f"exported the configs of {done} router{'s' if done != 1 else ''}", lab)
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
            old_name = lab.name
            for key, value in changes.items():
                setattr(lab, key, value)
            store.save()
            if name and name != old_name:
                activity.record(f"renamed the lab {old_name} to {name}", lab)
            return lab_view(lab)

    @app.delete("/api/labs/{lab_id}", status_code=204)
    def delete_lab(lab_id: int):
        with rt.lock:
            lab = get_lab(lab_id)
            rt.stop_lab(lab)
            for node in lab.nodes:
                rt.delete_disk(node)
            backups.forget_lab(lab)
            store.state.labs.remove(lab)
            store.save()
            rt.sync_dhcp()
            activity.record(f"deleted the lab {lab.name}", lab, kind="danger")

    @app.post("/api/labs/{lab_id}/start")
    def start_lab(lab_id: int):
        lab = get_lab(lab_id)
        try:
            rt.start_lab(lab)
        finally:
            changed()
        activity.record("started every device", lab)
        return lab_view(lab)

    @app.post("/api/labs/{lab_id}/stop")
    def stop_lab(lab_id: int):
        lab = get_lab(lab_id)
        rt.stop_lab(lab)
        changed()
        activity.record("stopped every device", lab)
        return lab_view(lab)

    @app.post("/api/labs/{lab_id}/clear")
    def clear_lab(lab_id: int, body: LabClear):
        """Delete everything in a lab and keep the empty lab: devices with their disks
        and saved revisions, links, shapes and notes. Needs the lab's name as confirmation."""
        with rt.lock:
            lab = get_lab(lab_id)
            if body.confirm.strip() != lab.name:
                raise Invalid(f"Type the lab's name, {lab.name}, to confirm.")
            removed = {
                "devices": len(lab.nodes),
                "links": len(lab.links),
                "annotations": len(lab.annotations),
                "revisions": sum(len(backups.revisions(n.id)) for n in lab.nodes),
            }
            rt.stop_lab(lab)
            for node in lab.nodes:
                rt.delete_disk(node)
                backups.forget_node(node.id)
                backups.set_node(lab.id, node.id, False)
            defaults = Lab(id=lab.id, name=lab.name)
            lab.nodes, lab.links, lab.annotations = [], [], []
            lab.mgmt_x, lab.mgmt_y = defaults.mgmt_x, defaults.mgmt_y
            store.save()
            rt.sync_dhcp()
            activity.record(f"nuked the lab: removed {removed['devices']} devices, {removed['links']} links "
                            f"and {removed['annotations']} shapes and notes", lab, kind="danger")
            return {"removed": removed, "lab": lab_view(lab)}

    # -- nodes -----------------------------------------------------------

    @app.post("/api/labs/{lab_id}/nodes", status_code=201)
    def create_node(lab_id: int, body: NodeCreate):
        with rt.lock:
            lab = get_lab(lab_id)
            check_name(lab, body.name)
            is_chr = body.kind == NodeKind.chr
            role = body.role
            if is_chr and role not in ("router", "switch"):
                raise Invalid("A CHR is a router or a switch.")
            if not is_chr:
                if role == "switch":
                    raise Invalid("Only a CHR can be a switch.")
                role = "server" if role == "server" else "pc"
            if body.layer == 3 and role != "switch":
                raise Invalid("Only switches have a layer.")
            ports = body.ports or ((24 if role == "switch" else 5) if is_chr else 2 if role == "server" else 1)
            check_ports(body.kind, ports)
            if is_chr and (body.address or body.gateway or body.dns):
                raise Invalid("address, gateway and dns apply to client nodes only.")
            if is_chr and (body.addressing != "static" or body.services):
                raise Invalid("addressing and services apply to clients only.")
            ram = body.ram_mb or (default_chr_ram(body.cpus, ports) if is_chr else 128)
            node = Node(
                id=free_id({n.id for n in store.state.all_nodes()}),
                kind=body.kind, role=role, layer=body.layer, name=body.name, ports=ports, cpus=body.cpus,
                bond=body.bond, nic_addresses=body.nic_addresses, vlans=body.vlans,
                ram_mb=ram, address=body.address, gateway=body.gateway, dns=body.dns,
                addressing=body.addressing, services=sorted(set(body.services)),
                x=body.x, y=body.y,
            )
            check_client(node)
            lab.nodes.append(node)
            store.save()
            activity.record(f"added the {what(node)} {node.name}", lab)
            return node_view(lab, node)

    @app.patch("/api/labs/{lab_id}/nodes/{node_id}")
    def update_node(lab_id: int, node_id: int, body: NodeUpdate):
        with rt.lock:
            lab = get_lab(lab_id)
            node = get_node(lab, node_id)
            changes = body.model_dump(exclude_unset=True)
            for key in ("x", "y", "name", "ports", "cpus", "ram_mb", "layer", "role", "nic_addresses", "vlans"):
                if changes.get(key, 0) is None:
                    del changes[key]
            if "layer" in changes and node.role != "switch":
                raise Invalid("Only switches have a layer.")
            if "role" in changes and node.kind != NodeKind.client:
                raise Invalid("Only clients are PCs or servers.")
            # Moving it and its layer (only how it is drawn) can change while it runs.
            if rt.node_running(node) and set(changes) - {"x", "y", "layer"}:
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
            if "services" in changes:
                changes["services"] = sorted(set(changes["services"] or []))
            updated = Node.model_validate({**node.model_dump(), **changes})
            check_client(updated)
            lab.nodes[lab.nodes.index(node)] = updated
            store.save()
            rt.sync_dhcp()
            settings_changed = sorted(k for k in changes if k not in ("x", "y", "name", "layer", "role")
                                      and getattr(node, k) != getattr(updated, k))
            if updated.name != node.name:
                activity.record(f"renamed {node.name} to {updated.name}", lab)
            if updated.layer != node.layer:
                activity.record(f"made {updated.name} a layer {updated.layer} switch", lab)
            if updated.role != node.role:
                activity.record(f"made {updated.name} a {what(updated)}", lab)
            if settings_changed:
                words = {"ports": "ports", "cpus": "vCPUs", "ram_mb": "RAM", "address": "address",
                         "gateway": "gateway", "dns": "DNS", "addressing": "addressing", "services": "services",
                         "bond": "bonding", "nic_addresses": "NIC addresses", "vlans": "VLANs"}
                activity.record(f"changed {updated.name}'s {', '.join(words.get(k, k) for k in settings_changed)}", lab)
            return node_view(lab, updated)

    @app.delete("/api/labs/{lab_id}/nodes/{node_id}", status_code=204)
    def delete_node(lab_id: int, node_id: int):
        with rt.lock:
            lab = get_lab(lab_id)
            node = get_node(lab, node_id)
            rt.stop_node(lab, node)
            rt.delete_disk(node)
            backups.forget_node(node.id)
            backups.set_node(lab.id, node.id, False)
            lab.links = [l for l in lab.links if not l.touches(node.id)]
            lab.nodes.remove(node)
            store.save()
            rt.sync_dhcp()
            activity.record(f"deleted the {what(node)} {node.name}", lab, kind="danger")

    # -- editing several things at once ------------------------------------
    #
    # The interface's multi-select, copy and paste, and undo use these. A delete puts
    # everything in the trash for a day (disks included), so undo can bring a router
    # back with its configuration.

    def amount(nodes: list, links: list, annos: list) -> str:
        if len(nodes) == 1 and not annos and all(l.touches(nodes[0].id) for l in links):
            return f"the {what(nodes[0])} {nodes[0].name}"
        if not nodes and len(links) == 1 and not annos:
            return "a link"
        if not nodes and not links and len(annos) == 1:
            return ANNO_WORDS[annos[0].kind]
        parts = [f"{len(xs)} {word}{'' if len(xs) == 1 else 's'}"
                 for xs, word in ((nodes, "device"), (links, "link"), (annos, "shape")) if xs]
        return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] if parts else "nothing"

    @app.post("/api/labs/{lab_id}/move")
    def move_items(lab_id: int, body: Move):
        """New positions for several devices and shapes at once (a group drag, align)."""
        with rt.lock:
            lab = get_lab(lab_id)
            for p in body.nodes:
                n = lab.node(p.id)
                if n is not None:
                    n.x, n.y = p.x, p.y
            for p in body.annotations:
                a = next((a for a in lab.annotations if a.id == p.id), None)
                if a is not None:
                    a.x, a.y = p.x, p.y
            if body.cloud is not None:
                lab.mgmt_x, lab.mgmt_y = body.cloud.x, body.cloud.y
            store.save()
            return {"ok": True}

    @app.post("/api/labs/{lab_id}/trash")
    def trash_items(lab_id: int, body: Items):
        """Delete devices (with their links), links and shapes, keeping them for undo."""
        with rt.lock:
            lab = get_lab(lab_id)
            nodes = [get_node(lab, i) for i in dict.fromkeys(body.nodes)]
            gone = {n.id for n in nodes}
            want = set(body.links)
            links = [l for l in lab.links if l.id in want or any(l.touches(i) for i in gone)]
            annos = [a for a in lab.annotations if a.id in set(body.annotations)]
            if not (nodes or links or annos):
                raise NotFound("Nothing to delete: it is already gone.")
            token, folder = trash.new()
            record = {"lab": lab.id, "time": time.time(), "nodes": [],
                      "links": [l.model_dump(mode="json") for l in links],
                      "annotations": [a.model_dump(mode="json") for a in annos]}
            for n in nodes:
                included = backups.is_included(lab.id, n.id)
                rt.stop_node(lab, n)
                rt.stash_files(n, folder / f"n{n.id}")
                backups.stash_node(n.id, folder / f"n{n.id}" / "backups")
                backups.set_node(lab.id, n.id, False)
                record["nodes"].append({"node": n.model_dump(mode="json"), "backup": included})
            for l in links:
                if not any(l.touches(i) for i in gone):
                    rt.remove_link(l)  # devices that stay up lose just this cable
            lab.links = [l for l in lab.links if l not in links]
            lab.nodes = [n for n in lab.nodes if n.id not in gone]
            lab.annotations = [a for a in lab.annotations if a not in annos]
            trash.save(token, record)
            store.save()
            rt.sync_dhcp()
            activity.record(f"deleted {amount(nodes, links, annos)}", lab, kind="danger")
            trash.purge()
            return {"token": token, "nodes": len(nodes), "links": len(links), "annotations": len(annos)}

    @app.post("/api/labs/{lab_id}/restore")
    def restore_items(lab_id: int, body: Undelete):
        """Undo a delete: everything comes back, under its old ids and names where free."""
        with rt.lock:
            lab = get_lab(lab_id)
            got = trash.load(body.token)
            if got is None:
                raise NotFound("That can no longer be undone: deleted things are kept for 24 hours.")
            record, folder = got
            if record["lab"] != lab.id:
                raise Conflict("That was deleted from another lab.")
            used = {n.id for n in store.state.all_nodes()}
            names = {n.name for n in lab.nodes}
            ids: dict[int, int] = {}
            back_nodes = []
            for item in record["nodes"]:
                data = item["node"]
                old = data["id"]
                new = old if old not in used else free_id(used)
                used.add(new)
                node = Node.model_validate({**data, "id": new, "name": free_name(data["name"], names)})
                names.add(node.name)
                rt.delete_disk(node)  # nothing should be there, but never mix two devices' files
                rt.unstash_files(node, folder / f"n{old}")
                backups.unstash_node(new, folder / f"n{old}" / "backups")
                if item.get("backup"):
                    backups.set_node(lab.id, new, True)
                lab.nodes.append(node)
                ids[old] = new
                back_nodes.append(node)
            link_ids = {l.id for l in store.state.all_links()}
            back_links, skipped = [], 0
            for data in record["links"]:
                ends = []
                for e in (data["a"], data["b"]):
                    ends.append(None if e is None else Endpoint(node=ids.get(e["node"], e["node"]), port=e["port"]))
                try:
                    for e in ends:
                        if e is not None:
                            rt.check_endpoint(lab, e)
                except (NotFound, Invalid, Conflict):
                    skipped += 1  # the other end is gone, or its port is taken now
                    continue
                lid = data["id"] if data["id"] not in link_ids else free_id(link_ids)
                link_ids.add(lid)
                link = Link(id=lid, a=ends[0], b=ends[1])
                lab.links.append(link)
                rt.apply_link(lab, link)
                back_links.append(link)
            anno_ids = {a.id for a in lab.annotations}
            back_annos = []
            for data in record["annotations"]:
                aid = data["id"] if data["id"] not in anno_ids else free_id(anno_ids)
                anno_ids.add(aid)
                anno = Annotation.model_validate({**data, "id": aid})
                lab.annotations.append(anno)
                back_annos.append(anno)
            store.save()
            rt.sync_dhcp()
            trash.drop(body.token)
            activity.record(f"restored {amount(back_nodes, back_links, back_annos)}", lab)
            return {"nodes": {str(k): v for k, v in ids.items()}, "links": [l.id for l in back_links],
                    "annotations": [a.id for a in back_annos], "skipped_links": skipped}

    @app.post("/api/labs/{lab_id}/paste", status_code=201)
    def paste_items(lab_id: int, body: Paste):
        """Copies of devices with the links between them, and shapes. Names that are taken
        get the next free one in their series; settings come along, configuration only
        when asked for (and only from stopped routers)."""
        with rt.lock:
            lab = get_lab(lab_id)
            if len(lab.annotations) + len(body.annotations) > 500:
                raise Conflict("A lab can have at most 500 shapes and notes.")
            same_lab = body.source_lab == lab.id
            names = {n.name for n in lab.nodes}
            used = {n.id for n in store.state.all_nodes()}
            made: list[tuple[PasteNode, Node]] = []
            refs: dict[int, Node] = {}
            for spec in body.nodes:
                if spec.ref in refs:
                    raise Invalid("Two devices in the paste share one ref.")
                check_ports(spec.kind, spec.ports)
                data = spec.model_dump(exclude={"ref", "source"})
                if spec.kind != NodeKind.client:
                    data.update(address=None, gateway=None, dns=None, addressing="static", services=[],
                                docker=False, containers=[], bond=None, nic_addresses=[], vlans=[])
                elif same_lab:
                    # Two clients with one address would only confuse; DHCP and DNS servers need one.
                    data["nic_addresses"] = []
                    data["vlans"] = [{**v, "address": None} for v in data["vlans"]]
                    if data["addressing"] == "static" and data["address"]:
                        data.update(address=None, services=[x for x in data["services"] if x not in ("dhcp", "dns")])
                if spec.role != "switch":
                    data["layer"] = 2
                if data["ram_mb"] is None:
                    data["ram_mb"] = default_chr_ram(spec.cpus, spec.ports) if spec.kind == NodeKind.chr else 128
                data["name"] = free_name(spec.name, names)
                names.add(data["name"])
                data["id"] = free_id(used)
                used.add(data["id"])
                node = Node.model_validate(data)
                check_client(node)
                made.append((spec, node))
                refs[spec.ref] = node
            taken: set[tuple[int, int]] = set()
            for l in body.links:
                a, b = refs.get(l.a.ref), refs.get(l.b.ref)
                if a is None or b is None:
                    raise Invalid("A link in the paste leads to a device that is not in it.")
                for n, e in ((a, l.a), (b, l.b)):
                    if e.port > n.ports or (n.id, e.port) in taken:
                        raise Invalid(f"{n.name} port {e.port} does not exist or is linked twice.")
                    taken.add((n.id, e.port))
            no_config = []
            for spec, node in made:
                rt.delete_disk(node)  # a stale disk from an earlier device with this id
                if body.with_config and spec.source is not None and node.kind == NodeKind.chr:
                    src = next((n for n in store.state.all_nodes() if n.id == spec.source), None)
                    if src is None or rt.node_running(src) or not rt.copy_disk(src, node):
                        no_config.append(node.name)
                lab.nodes.append(node)
            link_ids = {l.id for l in store.state.all_links()}
            new_links = []
            for l in body.links:
                lid = free_id(link_ids)
                link_ids.add(lid)
                link = Link(id=lid, a=Endpoint(node=refs[l.a.ref].id, port=l.a.port),
                            b=Endpoint(node=refs[l.b.ref].id, port=l.b.port))
                lab.links.append(link)
                new_links.append(link)
            anno_ids = {a.id for a in lab.annotations}
            new_annos = []
            for spec in body.annotations:
                aid = free_id(anno_ids)
                anno_ids.add(aid)
                anno = Annotation(id=aid, **spec.model_dump())
                lab.annotations.append(anno)
                new_annos.append(anno)
            store.save()
            rt.sync_dhcp()
            activity.record(f"pasted {amount([n for _, n in made], new_links, new_annos)}"
                            + (" with configuration" if body.with_config and len(no_config) < len(made) else ""), lab)
            return {"nodes": [{"ref": spec.ref, "id": node.id, "name": node.name} for spec, node in made],
                    "links": [l.id for l in new_links], "annotations": [a.id for a in new_annos],
                    "config_skipped": no_config}

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/start")
    def start_node(lab_id: int, node_id: int):
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        try:
            rt.start_node(lab, node)
        finally:
            changed()
        activity.record(f"started {node.name}", lab)
        return node_view(lab, node)

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/stop")
    def stop_node(lab_id: int, node_id: int):
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        rt.stop_node(lab, node)
        changed()
        activity.record(f"stopped {node.name}", lab)
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
            activity.record(f"duplicated {src.name} as {node.name}" + (" with its configuration" if copied else ""), lab)
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
            changed()
            activity.record(f"reset {node.name} to factory defaults", lab, kind="danger")
            return node_view(lab, node)

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/config/export")
    def export_config(lab_id: int, node_id: int, body: ConfigExport):
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        text = routeros.export_config(
            router_host(lab, node, body.host), body.username, body.password, body.sensitive)
        activity.record(f"exported {node.name}'s config", lab)
        return PlainTextResponse(text, headers={
            "Content-Disposition": f'attachment; filename="{node.name}.txt"'})

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/config/import")
    def import_config(lab_id: int, node_id: int, body: ConfigImport):
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        output = routeros.import_config(
            router_host(lab, node, body.host), body.username, body.password, body.config)
        activity.record(f"imported a config into {node.name}", lab)
        return {"output": output}

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/identity")
    def push_identity(lab_id: int, node_id: int, body: Creds):
        """Set the router's RouterOS identity to the node's name."""
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        output = routeros.set_identity(
            router_host(lab, node, body.host), body.username, body.password, node.name)
        activity.record(f"set {node.name}'s RouterOS identity", lab)
        return {"output": output}

    # -- automatic backups and revisions ---------------------------------

    @app.put("/api/labs/{lab_id}/backup")
    def set_backup(lab_id: int, body: BackupSettings):
        lab = get_lab(lab_id)
        if body.interval is not None and body.interval not in INTERVALS:
            raise Invalid(f"The interval must be one of {', '.join(map(str, INTERVALS))} minutes.")
        before = backups.settings(lab.id)
        backups.update(lab.id, body.model_dump(exclude_unset=True))
        after = backups.settings(lab.id)
        every = {15: "15 minutes", 60: "hour", 360: "6 hours", 1440: "24 hours"}.get(after["interval"], "")
        if after["enabled"] != before["enabled"]:
            activity.record(f"turned automatic backups on, every {every}" if after["enabled"]
                            else "turned automatic backups off", lab)
        elif after != before:
            activity.record("changed the automatic backup settings", lab)
        changed()
        return backups.summary(lab)

    @app.post("/api/labs/{lab_id}/backup/run")
    def run_backup(lab_id: int, body: BackupRun):
        """Back up now, with the stored login: one router, or every included one."""
        lab = get_lab(lab_id)
        if body.node is not None:
            node = get_node(lab, body.node)
            if node.kind != NodeKind.chr:
                raise Invalid(f"{node.name} is not a router.")
        results = backups.run(lab, body.node)
        changed()
        saved = [r["name"] for r in results if r.get("changed")]
        target = lab.node(body.node).name if body.node is not None else "all routers"
        activity.record(f"backed up {target}" + (f": new revisions of {', '.join(saved)}" if saved else ", no changes"), lab)
        return {"results": results}

    @app.put("/api/labs/{lab_id}/nodes/{node_id}/backup")
    def include_in_backup(lab_id: int, node_id: int, body: BackupInclude):
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        backups.set_node(lab.id, node.id, body.included)
        changed()
        return node_view(lab, node)

    @app.get("/api/labs/{lab_id}/nodes/{node_id}/revisions")
    def list_revisions(lab_id: int, node_id: int):
        node = get_node(get_lab(lab_id), node_id)
        return {"node": node.id, "name": node.name, "revisions": backups.revisions(node.id)}

    @app.get("/api/labs/{lab_id}/nodes/{node_id}/revisions/diff")
    def diff_revisions(lab_id: int, node_id: int, a: str, b: str):
        node = get_node(get_lab(lab_id), node_id)
        try:
            return backups.diff(node.id, a, b)
        except KeyError as e:
            raise NotFound(f"No revision {e.args[0]} for {node.name}.")

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/revisions/{rev}/restore")
    def restore_revision(lab_id: int, node_id: int, rev: str, body: Restore):
        """Load a saved revision into the router: replacing its config, or on top of it."""
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        try:
            result = backups.restore(lab, node, rev, replace=body.mode == "replace")
        except KeyError:
            raise NotFound(f"No revision {rev} for {node.name}.")
        if result.get("ok"):
            when = f"{rev[0:4]}-{rev[4:6]}-{rev[6:8]} {rev[9:11]}:{rev[11:13]} UTC"
            activity.record(f"restored {node.name} to the revision from {when}"
                            + (" (applied on top)" if body.mode == "merge" else ""), lab, kind="danger")
        changed()
        return result

    @app.post("/api/labs/{lab_id}/documentation")
    def documentation(lab_id: int, body: DocExport):
        """A zip with a Markdown description of the lab, its configs and the topology image."""
        lab = get_lab(lab_id)
        if body.refresh:
            for node in lab.nodes:
                if node.kind == NodeKind.chr and rt.node_running(node):
                    backups.backup_node(lab, node)
        png = None
        if body.png:
            try:
                png = base64.b64decode(body.png, validate=True)
            except ValueError:
                raise Invalid("The topology image is not valid.")
        data = docs.build_zip(lab, rt, backups, png)
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{lab.name}-documentation.zip"'})

    # -- Docker on Linux clients ---------------------------------------------

    def docker_pc(lab_id: int, node_id: int, running: bool = False):
        lab = get_lab(lab_id)
        node = get_node(lab, node_id)
        if node.kind != NodeKind.client or node.role != "server":
            raise Invalid("Docker runs on Linux servers only.")
        if running:
            if not node.docker:
                raise Conflict(f"Turn on Docker for {node.name} first.")
            if not rt.node_running(node):
                raise Conflict(f"Start {node.name} first.")
            if not rt.docker.ready(node):
                raise Conflict(f"Docker on {node.name} is still starting. Try again in a moment.")
        return lab, node

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/docker")
    def set_docker(lab_id: int, node_id: int, body: DockerSwitch):
        """Docker on or off for a PC. Works while it runs: the engine starts or stops now."""
        with rt.lock:
            lab, node = docker_pc(lab_id, node_id)
            if body.on and not rt.docker.available:
                raise Conflict("Docker isn't installed on this server. Run the installer again to add it.")
            if node.docker == body.on:
                return rt.docker.containers(lab, node)
            node.docker = body.on
            store.save()
            if rt.node_running(node):
                rt.docker.start(lab, node) if body.on else rt.docker.stop(node)
            activity.record(f"turned Docker {'on' if body.on else 'off'} for {node.name}", lab)
            return rt.docker.containers(lab, node)

    @app.get("/api/labs/{lab_id}/nodes/{node_id}/containers")
    def list_containers(lab_id: int, node_id: int):
        lab, node = docker_pc(lab_id, node_id)
        return rt.docker.containers(lab, node)

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/containers", status_code=201)
    def add_container(lab_id: int, node_id: int, spec: ContainerSpec):
        """Save a container with the lab, and create and start it now if the PC runs."""
        with rt.lock:
            lab, node = docker_pc(lab_id, node_id)
            if not node.docker:
                raise Conflict(f"Turn on Docker for {node.name} first.")
            names = {c.name for c in node.containers}
            if rt.docker.ready(node):
                names |= {r["name"] for r in rt.docker.containers(lab, node)["containers"]}
            if spec.name in names:
                raise Conflict(f"{node.name} already has a container called {spec.name}.")
            node.containers.append(spec)
            store.save()
            if rt.docker.ready(node):
                threading.Thread(target=rt.docker.ensure, args=(lab, node, spec), daemon=True).start()
            activity.record(f"added container {spec.name} ({spec.image}) to {node.name}", lab)
            return rt.docker.containers(lab, node)

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/containers/{name}/{action}")
    def container_action(lab_id: int, node_id: int, name: str, action: Literal["start", "stop", "restart"]):
        lab, node = docker_pc(lab_id, node_id, running=True)
        spec = next((c for c in node.containers if c.name == name), None)
        if spec and action == "start" and not any(r["name"] == name and r["state"] != "pending"
                                                  for r in rt.docker.containers(lab, node)["containers"]):
            threading.Thread(target=rt.docker.ensure, args=(lab, node, spec), daemon=True).start()
        else:
            try:
                rt.docker.act(node, name, action)
            except EngineError as e:
                raise Conflict(str(e))
        done = {"start": "started", "stop": "stopped", "restart": "restarted"}[action]
        activity.record(f"{done} container {name} on {node.name}", lab)
        return rt.docker.containers(lab, node)

    @app.delete("/api/labs/{lab_id}/nodes/{node_id}/containers/{name}")
    def remove_container(lab_id: int, node_id: int, name: str):
        with rt.lock:
            lab, node = docker_pc(lab_id, node_id)
            saved = [c for c in node.containers if c.name != name]
            was_saved = len(saved) != len(node.containers)
            node.containers = saved
            store.save()
            if rt.docker.ready(node):
                try:
                    rt.docker.act(node, name, "remove")
                except EngineError as e:
                    if not was_saved:
                        raise Conflict(str(e))
            activity.record(f"removed container {name} from {node.name}", lab, kind="danger")
            return rt.docker.containers(lab, node)

    @app.get("/api/labs/{lab_id}/nodes/{node_id}/containers/{name}/logs")
    def container_logs(lab_id: int, node_id: int, name: str, tail: int = 400):
        lab, node = docker_pc(lab_id, node_id, running=True)
        try:
            return {"text": rt.docker.logs(node, name, max(1, min(tail, 5000)))}
        except EngineError as e:
            raise Conflict(str(e))

    # -- links that open lab pages in your browser ------------------------------

    @app.get("/api/web")
    def list_links(request: Request, lab: Optional[int] = None):
        host = request.url.hostname or "localhost"
        return [l.view(host) for l in weblinks.links.values() if lab is None or l.lab == lab]

    @app.post("/api/web", status_code=201)
    async def open_link(body: WebLinkCreate, request: Request):
        lab = get_lab(body.lab)
        node = get_node(lab, body.node)
        if node.kind != NodeKind.client:
            raise Invalid("Pages are opened from a Linux client. Pick one.")
        if not rt.node_running(node):
            raise Conflict(f"Start {node.name} first.")
        if body.container:
            rows = (await run_in_threadpool(rt.docker.containers, lab, node))["containers"]
            row = next((r for r in rows if r["name"] == body.container), None)
            if row is None:
                raise NotFound(f"{node.name} has no container called {body.container}.")
            ports = [p for p in row["ports"] if p["proto"] == "tcp" and body.port in (None, p["host"])]
            if not ports or not row["running"]:
                raise Conflict(f"{body.container} is not running with a published TCP port.")
            host, port, scheme, path = "127.0.0.1", ports[0]["host"], None, "/"
            name = host
            title = f"{body.container} · {row['image'].split('/')[-1].split(':')[0]}"
            detail = f"Container on {node.name}, port {port}"
        elif body.url:
            scheme, name, port, path = parse_url(body.url)
            host = await run_in_threadpool(weblinks.resolve, node.id, name)
            default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
            title = f"{scheme}://{name}" + ("" if default else f":{port}")
            detail = f"From {node.name}"
        else:
            raise Invalid("Say what to open: a container, or an address.")
        viewer = body.mode == "view"
        allow = None if body.who == "anyone" and not viewer else plain_ip(request.client.host if request.client else None)
        user = (request.scope.get("state") or {}).get("user") or ""
        known = {l.id for l in weblinks.links.values()}
        link = await weblinks.open(lab=lab, node=node, title=title, detail=detail, host=host, port=port,
                                   scheme=scheme, path=path, allow=allow, by=user, mode=body.mode, name=name)
        if link.id not in known:
            activity.record((f"viewed {title} in Mikrobox from {node.name}" if viewer else
                             f"opened a link to {title} from {node.name}" + (" for anyone" if not allow else "")), lab)
        return link.view(request.url.hostname or "localhost")

    @app.delete("/api/web/{link_id}", status_code=204)
    def close_link(link_id: int):
        link = weblinks.close(link_id)
        if link is None:
            raise NotFound("That link is already closed.")
        lab = store.state.lab(link.lab)
        if lab:
            activity.record(f"closed the link to {link.title}", lab)

    @app.post("/api/labs/{lab_id}/nodes/{node_id}/test")
    def reach_test(lab_id: int, node_id: int, body: ReachTest):
        """Run ping or traceroute from a client."""
        node = get_node(get_lab(lab_id), node_id)
        if node.kind != NodeKind.client:
            raise Invalid("Tests run from a client. Pick a PC as the source.")
        if not rt.node_running(node):
            raise Conflict(f"Start {node.name} first.")
        tool = {
            "ping": ["ping", "-c", "3", "-W", "2", body.target],
            "traceroute": ["traceroute", "-n", "-w", "2", "-q", "1", "-m", "15", body.target],
            # Needs an iperf3 server at the target, e.g. another client with that service on.
            "iperf": ["iperf3", "-c", body.target, "-t", "5", "-f", "m", "--connect-timeout", "3000"],
        }[body.tool]
        if settings.dry_run:
            return {"ok": False, "command": " ".join(tool), "output": "Dry-run mode: nothing is running."}
        try:
            proc = subprocess.run(["ip", "netns", "exec", rt.netns(node.id), *tool],
                                  capture_output=True, text=True, timeout=70)
            return {"ok": proc.returncode == 0, "command": " ".join(tool),
                    "output": (proc.stdout + proc.stderr).strip()}
        except subprocess.TimeoutExpired:
            return {"ok": False, "command": " ".join(tool), "output": "Timed out after 70 seconds."}

    @app.get("/api/labs/{lab_id}/nodes/{node_id}/revisions/{rev}")
    def read_revision(lab_id: int, node_id: int, rev: str):
        node = get_node(get_lab(lab_id), node_id)
        try:
            text = backups.read(node.id, rev)
        except KeyError:
            raise NotFound(f"No revision {rev} for {node.name}.")
        return PlainTextResponse(text, headers={
            "Content-Disposition": f'attachment; filename="{node.name}-{rev}.txt"'})

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
            activity.record(f"added {ANNO_WORDS[anno.kind]}", lab)
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
            gone = next((a for a in lab.annotations if a.id == anno_id), None)
            lab.annotations = [a for a in lab.annotations if a.id != anno_id]
            store.save()
            if gone:
                label = f' "{gone.text.splitlines()[0][:40]}"' if gone.text.strip() else ""
                activity.record(f"deleted {ANNO_WORDS[gone.kind]}{label}", lab)

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
            activity.record(f"linked {link_text(lab, link)}", lab)
            return link.model_dump(mode="json")

    @app.delete("/api/labs/{lab_id}/links/{link_id}", status_code=204)
    def delete_link(lab_id: int, link_id: int):
        with rt.lock:
            lab = get_lab(lab_id)
            link = next((l for l in lab.links if l.id == link_id), None)
            if link is None:
                raise NotFound(f"Link {link_id} not found in lab {lab_id}.")
            rt.remove_link(link)
            text = link_text(lab, link)
            lab.links.remove(link)
            store.save()
            rt.sync_dhcp()
            activity.record(f"removed the link {text}", lab, kind="danger")

    # -- capture ---------------------------------------------------------

    @app.get("/api/labs/{lab_id}/nodes/{node_id}/ports/{port}/capture")
    def capture(
        lab_id: int, node_id: int, port: int,
        seconds: Optional[int] = Query(default=None, ge=1, le=3600),
    ):
        """Pcap stream. Live: curl -sN URL | wireshark -k -i -   Timed file: add ?seconds=30"""
        node = get_node(get_lab(lab_id), node_id)
        iface = rt.iface(node.id, port)
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
        iface = rt.iface(node_id, port)
        if node is None or port < 1 or port > node.ports or not rt.net.exists(iface):
            await ws.send_text(json.dumps({"error": "That port is not up. Start the device first."}))
            await ws.close()
            return
        if len(bpf) > 200 or not re.fullmatch(r"[\w\s.:/()\[\]!=<>&|-]*", bpf):
            await ws.send_text(json.dumps({"error": "That filter has characters that are not allowed."}))
            await ws.close()
            return
        proc = await asyncio.create_subprocess_exec(
            "tcpdump", "-l", "-n", "-e", "-xx", "-i", iface, *bpf.split(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1 << 20)
        await packet_session(ws, proc)

    async def packet_session(ws: WebSocket, proc) -> None:
        """Stream tcpdump -xx output as rows; answer {"decode": n} and {"pause": bool} from the page.

        The bytes of the last few thousand packets are kept so any visible row can be decoded.
        While paused, packets are read and dropped, so nothing the page shows is pushed out.
        """
        kept: dict[int, tuple[bytes, float, int]] = {}
        order: deque[int] = deque()
        st = {"n": 0, "paused": False}
        send_lock = asyncio.Lock()

        async def send(obj) -> None:
            async with send_lock:
                await ws.send_text(json.dumps(obj))

        def take(done: list) -> list[dict]:
            rows = []
            now = time.time()
            for row, data in done:
                if st["paused"]:
                    continue
                st["n"] += 1
                n = st["n"]
                kept[n] = (data, now, row["length"])
                order.append(n)
                while len(order) > 5000:
                    kept.pop(order.popleft(), None)
                rows.append({**row, "n": n})
            return rows

        async def pump():
            asm = Assembler()
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                done = asm.feed(line.decode(errors="replace"))
                # Group whatever else arrives within a moment into one message.
                try:
                    for _ in range(4000):
                        more = await asyncio.wait_for(proc.stdout.readline(), 0.1)
                        if not more:
                            break
                        done += asm.feed(more.decode(errors="replace"))
                        if len(done) >= 200:
                            break
                except asyncio.TimeoutError:
                    pass
                done += asm.flush()  # tcpdump writes each packet whole, so a pause means it is complete
                rows = take(done)
                if rows:
                    await send({"packets": rows})
            err = (await proc.stderr.read()).decode(errors="replace")
            bad = [l for l in err.splitlines() if "error" in l.lower()]
            if bad:
                await send({"error": bad[-1].replace("tcpdump: ", "")})

        async def listen():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                try:
                    ask = json.loads(msg.get("text") or "{}")
                except ValueError:
                    continue
                if "pause" in ask:
                    st["paused"] = bool(ask["pause"])
                if isinstance(ask.get("decode"), int):
                    n = ask["decode"]
                    if n not in kept:
                        await send({"decoded": n, "error": "That packet is no longer kept. Newer traffic pushed it out."})
                        continue
                    data, ts, length = kept[n]
                    await send({"decoded": n, **(await decode_packet(data, ts, length))})

        tasks = [asyncio.create_task(pump()), asyncio.create_task(listen())]

        def stop() -> None:
            for t in tasks:
                t.cancel()
            if proc.returncode is None:
                proc.terminate()

        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            # Cancelled (the server is shutting down) or failed: clean up without waiting on
            # anything, or a cancellation is caught again mid-cleanup and reported as an error.
            stop()
            raise
        stop()
        await asyncio.gather(*tasks, return_exceptions=True)
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
        user = (ws.scope.get("state") or {}).get("user")
        key = f"{lab_id}/{node_id}"
        try:
            if node.kind == NodeKind.chr:
                await consoles.join(hubs, key, lambda: consoles.SerialHub(key, hubs, rt.serial_socket(node)), ws, user)
            else:
                env = {"DOCKER_HOST": rt.docker.docker_host(node)} if node.docker else {}
                await consoles.join(hubs, key, lambda: consoles.ShellHub(key, hubs, rt.netns(node.id), node.name, env), ws, user)
        except OSError as e:
            await ws.send_text(f"\r\nConsole unavailable: {e}\r\n")
        try:
            await ws.close()
        except RuntimeError:
            pass

    return app
