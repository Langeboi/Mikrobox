"""Links that open a web page inside the lab in your own browser.

Each link is a port on the Mikrobox server (WEB_PORTS) that leads to one place inside the
lab, as seen from one Linux PC: a container's published port, or any address that PC can
reach (a router's WebFig, a server on another subnet). Connections are made from inside
the PC's network namespace (nsnet), so no routes or Cloud links are needed, and the bytes
pass straight through: HTTP stays HTTP, HTTPS stays HTTPS with the page's own certificate.

A link answers only the computer that made it unless asked otherwise. Links close when
their PC stops, after IDLE_HOURS without use, or when Mikrobox restarts.

A "view" link is for the viewer tab inside Mikrobox instead: it speaks plain HTTP to your
browser and HTTP or HTTPS (certificate not checked) to the page, one request per
connection, and adjusts each request and answer so the page works in a frame (see
_serve_view). It always answers only the computer that opened it.
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
    mode: str = "link"  # or "view", for the viewer tab
    name: str = ""      # the host name the page expects (Host header, TLS name)
    page: str = ""      # view links: the last page the frame went to, for its address bar

    @property
    def target(self) -> str:
        """Where the page is, as the PC would write it: https://10.0.0.1:8443"""
        return f"{self.scheme}://{self.authority}"

    @property
    def authority(self) -> str:
        name = self.name or self.host
        shown = f"[{name}]" if ":" in name else name
        default = (self.scheme, self.port) in (("http", 80), ("https", 443))
        return shown if default else f"{shown}:{self.port}"

    def view(self, host: str) -> dict:
        shown = f"[{host}]" if ":" in host else host
        scheme = "http" if self.mode == "view" else self.scheme
        return {
            "id": self.id, "lab": self.lab, "node": self.node, "node_name": self.node_name,
            "title": self.title, "detail": self.detail, "scheme": self.scheme, "mode": self.mode,
            "url": f"{scheme}://{shown}:{self.listen}{self.path}", "port": self.listen,
            "base": f"{scheme}://{shown}:{self.listen}", "target": self.target, "path": self.path,
            "page": self.page or self.path,
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
                   path: str, allow: str | None, by: str, mode: str = "link", name: str = "") -> Link:
        if scheme is None:
            scheme = await self.detect(node.id, host, port)
        if mode == "view":
            # One viewer link per page and person is enough: reuse it.
            for l in self.links.values():
                if (l.mode, l.node, l.host, l.port, l.scheme, l.allow, l.name) == (mode, node.id, host, port, scheme, allow, name or host):
                    l.path, l.used = path or "/", time.time()
                    return l
        used = {l.listen for l in self.links.values()}
        for listen in range(self.first, self.last + 1):
            if listen in used:
                continue
            link = Link(id=self.next_id, lab=lab.id, node=node.id, node_name=node.name, title=title, detail=detail,
                        host=host, port=port, scheme=scheme, path=path or "/", listen=listen, allow=allow, by=by,
                        mode=mode, name=name or host)
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
        if link.mode == "view":
            link.conns.add(writer)
            try:
                await self._serve_view(link, reader, writer)
            finally:
                link.conns.discard(writer)
                writer.close()
            return
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

    # -- the viewer -----------------------------------------------------------

    async def _serve_view(self, link: Link, reader, writer) -> None:
        """One request from the viewer frame, passed to the page and its answer back.

        On the way in: Host, Origin and Referer name the page's own address, and
        Mikrobox's session cookie stays here. On the way out: headers that forbid
        framing go, redirects to the page's address come back to this link, and
        cookies lose Secure (this side is plain HTTP) and Domain.
        """
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError):
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            return
        headers = [tuple(x.split(":", 1)) for x in lines[1:] if ":" in x]
        headers = [(k.strip(), v.strip()) for k, v in headers]
        get = lambda name: next((v for k, v in headers if k.lower() == name), "")
        here = "http://" + (get("host") or f"localhost:{link.listen}")
        there = link.target
        upgrade = "upgrade" in get("connection").lower() and bool(get("upgrade"))
        if parts[0] == "GET" and get("sec-fetch-dest") in ("iframe", "document"):
            link.page = parts[1]  # the frame went there: the tab's address bar follows
        out = [f"{parts[0]} {parts[1]} HTTP/1.1", f"Host: {link.authority}"]
        for k, v in headers:
            low = k.lower()
            if low in ("host", "connection", "keep-alive", "proxy-connection", "upgrade"):
                continue
            if low == "origin" and v == here:
                v = there
            elif low == "referer" and v.startswith(here):
                v = there + v[len(here):]
            elif low == "cookie":
                v = "; ".join(c for c in v.split("; ") if not c.strip().startswith("mikrobox_session"))
                if not v:
                    continue
            out.append(f"{k}: {v}")
        out += ["Connection: Upgrade", f"Upgrade: {get('upgrade')}"] if upgrade else ["Connection: close"]

        try:
            sock = await self.connect(link.node, link.host, link.port)
            tls = None
            if link.scheme == "https":
                tls = ssl.create_default_context()
                tls.check_hostname = False
                tls.verify_mode = ssl.CERT_NONE
            try:
                ipaddress.ip_address(link.name)
                sni = None
            except ValueError:
                sni = link.name
            # With a socket of our own, asyncio wants a name for TLS; "" sends none (an address).
            r2, w2 = await asyncio.wait_for(asyncio.open_connection(sock=sock, ssl=tls, server_hostname=(sni or "") if tls else None), 10)
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            body = f"Mikrobox could not reach {there} from {link.node_name}.".encode()
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
            await writer.drain()
            return
        try:
            w2.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
            sending = asyncio.ensure_future(self._pipe(reader, w2, link))
            try:
                answer = await asyncio.wait_for(r2.readuntil(b"\r\n\r\n"), 120)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError):
                sending.cancel()
                return
            writer.write(rewrite_answer(answer, here, there, link.authority).encode("latin-1"))
            await writer.drain()
            await self._pipe(r2, writer, link)
            sending.cancel()
        finally:
            w2.close()

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


_DROP = {"x-frame-options", "strict-transport-security", "connection", "keep-alive", "public-key-pins"}


def rewrite_answer(head: bytes, here: str, there: str, authority: str) -> str:
    """A response head from the page, made fit for the viewer frame (see _serve_view)."""
    lines = head.decode("latin-1").split("\r\n")
    status, out = lines[0], [lines[0]]
    upgraded = status.split(" ")[1:2] == ["101"]
    scheme = there.split("://")[0]
    # Every way the page may write its own address: with and without the default port.
    bare = authority.rsplit(":", 1)[0] if authority.endswith((":80", ":443")) else authority
    origins = {there, f"{scheme}://{bare}", f"{scheme}://{bare}:{443 if scheme == 'https' else 80}"}
    for line in lines[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v, low = k.strip(), v.strip(), k.strip().lower()
        if low in _DROP and not (upgraded and low == "connection"):
            continue
        if low in ("content-security-policy", "content-security-policy-report-only"):
            v = "; ".join(d.strip() for d in v.split(";")
                          if d.strip() and d.split()[0].lower() not in ("frame-ancestors", "upgrade-insecure-requests"))
            if not v:
                continue
        elif low == "location":
            for o in sorted(origins, key=len, reverse=True):
                if v == o or v.startswith(o + "/") or v.startswith(o + "?"):
                    v = here + v[len(o):]
                    break
        elif low == "set-cookie":
            attrs = [a.strip() for a in v.split(";")]
            keep = [attrs[0]]
            for a in attrs[1:]:
                name = a.split("=")[0].strip().lower()
                if name in ("secure", "domain", "partitioned"):
                    continue
                if name == "samesite" and a.split("=", 1)[-1].strip().lower() == "none":
                    a = "SameSite=Lax"
                keep.append(a)
            v = "; ".join(keep)
        out.append(f"{k}: {v}")
    if not upgraded:
        out.append("Connection: close")
    return "\r\n".join(out) + "\r\n\r\n"


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
