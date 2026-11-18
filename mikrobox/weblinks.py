"""Links that open a web page inside the lab in your own browser.

Each link is a port on the Mikrobox server (WEB_PORTS) that leads to one place inside the
lab, as seen from one Linux PC: a container's published port, or any address that PC can
reach (a router's WebFig, a server on another subnet). Connections are made from inside
the PC's network namespace (nsnet), so no routes or Cloud links are needed, and the bytes
pass straight through: HTTP stays HTTP, HTTPS stays HTTPS with the page's own certificate.

A link answers only the computer that made it unless asked otherwise. Links close when
their PC stops, after IDLE_HOURS without use, or when Mikrobox restarts.
"""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
import subprocess
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import nsnet

IDLE_HOURS = 8


class LinkError(Exception):
    pass


@dataclass
class Link:
    id: int
    lab: int
    node: int
    node_name: str
    title: str
    detail: str
    host: str          # the address inside the lab, as the PC reaches it
    port: int
    scheme: str        # what the page speaks, for the link we hand out
    path: str
    listen: int        # the port on this server
    allow: str | None  # only this client address, or anyone
    by: str
    created: float = field(default_factory=time.time)
    used: float = field(default_factory=time.time)
    server: asyncio.base_events.Server | None = None
    conns: set = field(default_factory=set)

    def view(self, host: str) -> dict:
        shown = f"[{host}]" if ":" in host else host
        return {
            "id": self.id, "lab": self.lab, "node": self.node, "node_name": self.node_name,
            "title": self.title, "detail": self.detail, "scheme": self.scheme,
            "url": f"{self.scheme}://{shown}:{self.listen}{self.path}", "port": self.listen,
            "who": "me" if self.allow else "anyone", "allow": self.allow, "by": self.by,
            "created": self.created, "used": self.used,
        }


def plain_ip(addr: str | None) -> str | None:
    if not addr:
        return None
    return addr[7:] if addr.startswith("::ffff:") else addr


class WebLinks:
    def __init__(self, rt, ports: tuple[int, int], on_change=None) -> None:
        self.rt = rt
        self.first, self.last = ports
        self.links: dict[int, Link] = {}
        self.next_id = 1
        self.loop: asyncio.AbstractEventLoop | None = None
        self.on_change = on_change or (lambda: None)
        rt.stop_hooks.append(self._node_stopped)

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        loop.create_task(self._expire())

    # -- reaching into the lab ----------------------------------------------

    def connector(self, node_id: int):
        """A fresh socket inside the PC (made in a worker thread, see nsnet)."""
        s = nsnet.socket_in(str(self.rt.s.netns_dir / self.rt.netns(node_id)))
        s.setblocking(False)
        return s

    async def connect(self, node_id: int, host: str, port: int, timeout: float = 8):
        loop = asyncio.get_running_loop()
        sock = await loop.run_in_executor(None, self.connector, node_id)
        try:
            await asyncio.wait_for(loop.sock_connect(sock, (host, port)), timeout)
        except BaseException:
            sock.close()
            raise
        return sock

    async def detect(self, node_id: int, host: str, port: int) -> str:
        """https if something there finishes a TLS handshake, else http."""
        try:
            sock = await self.connect(node_id, host, port, timeout=5)
        except (OSError, asyncio.TimeoutError):
            raise LinkError(f"Nothing answers on {host}:{port} from there.")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(sock=sock, ssl=ctx, server_hostname=""), 4)
            w.close()
            return "https"
        except (ssl.SSLError, OSError, asyncio.TimeoutError, EOFError):
            sock.close()
            return "http"

    def resolve(self, node_id: int, name: str) -> str:
        """A name, looked up with the PC's own resolver."""
        try:
            return str(ipaddress.ip_address(name))
        except ValueError:
            pass
        if self.rt.s.dry_run:
            raise LinkError(f"Can't look up {name} in dry-run mode.")
        out = subprocess.run(["ip", "netns", "exec", self.rt.netns(node_id), "getent", "ahostsv4", name],
                             capture_output=True, text=True, timeout=8).stdout.split()
        if not out:
            raise LinkError(f"{name} does not resolve from there.")
        return out[0]

    # -- links ----------------------------------------------------------------

    async def open(self, *, lab, node, title: str, detail: str, host: str, port: int, scheme: str | None,
                   path: str, allow: str | None, by: str) -> Link:
        if scheme is None:
            scheme = await self.detect(node.id, host, port)
        used = {l.listen for l in self.links.values()}
        for listen in range(self.first, self.last + 1):
            if listen in used:
                continue
            link = Link(id=self.next_id, lab=lab.id, node=node.id, node_name=node.name, title=title, detail=detail,
                        host=host, port=port, scheme=scheme, path=path or "/", listen=listen, allow=allow, by=by)
            try:
                link.server = await asyncio.start_server(lambda r, w, l=link: self._serve(l, r, w), "0.0.0.0", listen,
                                                         reuse_address=True)
            except OSError:
                continue  # something else has that port
            self.next_id += 1
            self.links[link.id] = link
            self.on_change()
            return link
        raise LinkError(f"No free port left between {self.first} and {self.last}. Close a link first.")

    async def _serve(self, link: Link, reader, writer) -> None:
        peer = writer.get_extra_info("peername")
        if link.allow and plain_ip(peer[0] if peer else None) != link.allow:
            writer.close()
            return
        link.used = time.time()
        link.conns.add(writer)
        upstream = None
        try:
            sock = await self.connect(link.node, link.host, link.port)
            r2, upstream = await asyncio.open_connection(sock=sock)
            await asyncio.gather(self._pipe(reader, upstream, link), self._pipe(r2, writer, link))
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            link.conns.discard(writer)
            for w in (writer, upstream):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    @staticmethod
    async def _pipe(r, w, link: Link) -> None:
        try:
            while True:
                data = await r.read(1 << 16)
                if not data:
                    break
                link.used = time.time()
                w.write(data)
                await w.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                w.close()  # the other way stops too
            except Exception:
                pass

    def close(self, link_id: int) -> Link | None:
        link = self.links.pop(link_id, None)
        if link is None:
            return None
        if link.server:
            link.server.close()
        for w in list(link.conns):
            try:
                w.close()
            except Exception:
                pass
        self.on_change()
        return link

    def _node_stopped(self, node) -> None:
        """A runtime hook, called from worker threads."""
        if self.loop is None:
            return
        def go():
            for l in [l for l in self.links.values() if l.node == node.id]:
                self.close(l.id)
        self.loop.call_soon_threadsafe(go)

    async def _expire(self) -> None:
        while True:
            await asyncio.sleep(60)
            limit = time.time() - IDLE_HOURS * 3600
            for l in [l for l in self.links.values() if l.used < limit and not l.conns]:
                self.close(l.id)


def parse_url(text: str) -> tuple[str, str, int, str]:
    """"https://10.99.1.12:8443/x" -> ("https", "10.99.1.12", 8443, "/x"). A bare address means http."""
    text = text.strip()
    if "://" not in text:
        text = "http://" + text
    u = urlsplit(text)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise LinkError("Enter an address like http://10.0.0.1 or https://10.0.0.1:8443/.")
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        raise LinkError("That port is not a number.")
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return u.scheme, u.hostname, port, path
