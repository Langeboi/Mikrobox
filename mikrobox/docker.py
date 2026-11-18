"""A Docker engine per Linux PC.

Each PC with Docker switched on runs its own dockerd inside its network namespace, so
docker0, container networks, published ports and NAT all live inside the PC, exactly as
on a real Linux machine: `docker run -p 8080:80 nginx` on PC1 answers on PC1's address.

The engine needs three things a plain `ip netns exec` does not give it (see LAUNCH):
a /sys for the PC's namespace with the host's cgroups still mounted, the PC's own
resolv.conf, and no view of a host-wide containerd, so that it runs its own beside it.

Images come through the Mikrobox server, not through the lab: the server listens on
127.0.0.1:PULL_PORT inside each such PC and relays HTTPS to the internet from the host,
and the engine is told to use that as its proxy. Containers added from the web interface
are saved with the lab (Node.containers) and recreated when missing; an image another PC
already has is copied over locally instead of downloaded again.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import nsnet
from .network import pid_alive

PULL_PORT = 47321  # inside each PC, on 127.0.0.1 only

LAUNCH = """#!/bin/sh
# Starts a PC's Docker engine inside the PC. Written by Mikrobox (docker.py).
# usage: docker-launch.sh <netns path> <resolv.conf> <stash dir> dockerd args...
set -e
ns_path=$1 resolv=$2 stash=$3
shift 3
exec nsenter --net="$ns_path" unshare --mount --propagation private sh -ec '
  resolv=$1 stash=$2
  shift 2
  mkdir -p "$stash"
  # A /sys that shows the interfaces of this PC, with the cgroups moved back in.
  mount --rbind /sys/fs/cgroup "$stash"
  mount -t sysfs sysfs /sys
  mount --move "$stash" /sys/fs/cgroup
  # Hide a host-wide containerd, so this engine starts its own.
  if [ -d /run/containerd ]; then mount -t tmpfs tmpfs /run/containerd; fi
  if [ -f "$resolv" ]; then mount --bind "$resolv" /etc/resolv.conf; fi
  # Let go of the other PCs: a copy of their namespace mounts in here would keep
  # them from being removed when they stop.
  umount -l /run/netns 2>/dev/null || true
  exec dockerd "$@"' launch "$resolv" "$stash" "$@"
"""


def _state(proc: Path) -> str:
    """A process's state letter from /proc (Z for a zombie), or "" if it is gone."""
    try:
        return (proc / "stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return ""


class EngineError(Exception):
    pass


# ---------------------------------------------------------------- Engine API client

class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.path)
        self.sock = s


class Engine:
    """The few calls of Docker's Engine API that Mikrobox needs, over its unix socket."""

    def __init__(self, sock: str | Path):
        self.sock = str(sock)

    def open(self, method: str, path: str, body=None, timeout: float = 8, headers: dict | None = None):
        conn = _UnixConnection(self.sock, timeout)
        hdrs = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=hdrs, encode_chunked=not isinstance(body, (bytes, type(None))))
        return conn, conn.getresponse()

    def call(self, method: str, path: str, body=None, timeout: float = 8):
        try:
            conn, resp = self.open(method, path, body, timeout)
        except OSError as e:
            raise EngineError(f"The Docker engine did not answer: {e}") from None
        try:
            data = resp.read()
        finally:
            conn.close()
        if resp.status >= 400:
            try:
                msg = json.loads(data).get("message") or data.decode()
            except ValueError:
                msg = data.decode(errors="replace")
            raise EngineError(msg.strip() or f"Docker said {resp.status}")
        if not data:
            return None
        try:
            return json.loads(data)
        except ValueError:
            return data

    def ping(self) -> bool:
        try:
            return self.call("GET", "/_ping", timeout=2) in (b"OK", "OK")
        except (EngineError, OSError):
            return False


def split_ref(ref: str) -> tuple[str, str]:
    """"nginx:alpine" -> ("nginx", "alpine"); a registry port is not a tag."""
    if "@" in ref:
        return ref, ""
    name, _, tag = ref.rpartition(":")
    if not name or "/" in tag:
        return ref, "latest"
    return name, tag


def demux(data: bytes) -> str:
    """Container logs come in frames (8 byte header) unless the container has a terminal."""
    out, i = [], 0
    while i + 8 <= len(data):
        kind, size = data[i], int.from_bytes(data[i + 4:i + 8], "big")
        if kind not in (0, 1, 2) or data[i + 1:i + 4] != b"\0\0\0" or i + 8 + size > len(data):
            return data.decode(errors="replace")  # not framed after all
        out.append(data[i + 8:i + 8 + size])
        i += 8 + size
    if i != len(data):
        return data.decode(errors="replace")
    return b"".join(out).decode(errors="replace")


def run_command(spec) -> str:
    """The docker run command a saved container equals, shown in the interface."""
    parts = ["docker run -d", f"--name {spec.name}"]
    for p in spec.ports:
        parts.append(f"-p {p.host}:{p.container}" + ("/udp" if p.proto == "udp" else ""))
    for k, v in spec.env.items():
        parts.append(f"-e {k}={v}")
    if spec.docker_socket:
        parts.append("-v /var/run/docker.sock:/var/run/docker.sock")
    if spec.restart:
        parts.append("--restart unless-stopped")
    parts.append(spec.image)
    return " ".join(parts)


# ---------------------------------------------------------------- the pull proxy

class PullProxy:
    """Listens on 127.0.0.1:PULL_PORT inside one PC and relays CONNECT to port 443 from the host."""

    def __init__(self, listener: socket.socket):
        self.listener = listener
        self.closed = False
        self.conns: set[socket.socket] = set()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        """Close the listener and every relay. Sockets made inside the PC keep its
        network namespace alive, so none may linger; shutdown() also wakes the
        threads blocked on them, which close() alone does not."""
        self.closed = True
        for s in [self.listener, *list(self.conns)]:
            for step in (lambda: s.shutdown(socket.SHUT_RDWR), s.close):
                try:
                    step()
                except OSError:
                    pass

    def _accept(self) -> None:
        while not self.closed:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            if self.closed:
                conn.close()
                return
            self.conns.add(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        upstream = None
        try:
            conn.settimeout(15)
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 16384:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            line = head.split(b"\r\n", 1)[0].decode(errors="replace").split()
            m = re.fullmatch(r"([A-Za-z0-9.-]+):(\d+)", line[1]) if len(line) == 3 and line[0] == "CONNECT" else None
            if not m or m.group(2) != "443":  # registries only speak HTTPS
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            upstream = socket.create_connection((m.group(1), 443), timeout=15)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            rest = head.split(b"\r\n\r\n", 1)[1]
            if rest:
                upstream.sendall(rest)
            conn.settimeout(None)
            upstream.settimeout(None)
            pair = {conn: upstream, upstream: conn}
            while True:
                ready, _, _ = select.select(list(pair), [], [], 300)
                if not ready:
                    return
                for s in ready:
                    data = s.recv(65536)
                    if not data:
                        return
                    pair[s].sendall(data)
        except OSError:
            try:
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        finally:
            self.conns.discard(conn)
            for s in (conn, upstream):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass


# ---------------------------------------------------------------- engines per PC

class Docker:
    def __init__(self, rt) -> None:
        self.rt = rt
        self.s = rt.s
        self.lock = threading.RLock()
        self.state: dict[int, str] = {}      # node id -> "starting", "running" or an error text
        self.version: dict[int, str] = {}
        self.proxies: dict[int, PullProxy] = {}
        self.progress: dict[tuple[int, str], str] = {}  # (node id, container) -> what is happening
        self.errors: dict[tuple[int, str], str] = {}
        self._summary: dict[int, tuple[float, dict]] = {}

    # -- where things live --------------------------------------------------

    @property
    def available(self) -> bool:
        return self.s.dry_run or shutil.which("dockerd") is not None

    def root(self) -> Path:
        return self.s.run_dir / "docker"

    def sock(self, node) -> Path:
        return self.root() / f"n{node.id}.sock"

    def data_root(self, node) -> Path:
        return self.s.data_dir / "docker" / f"n{node.id}"

    def pidfile(self, node) -> Path:
        return self.root() / f"n{node.id}.pid"

    def engine(self, node) -> Engine:
        return Engine(self.sock(node))

    def docker_host(self, node) -> str:
        return f"unix://{self.sock(node)}"

    def netns_path(self, node) -> str:
        return str(self.s.netns_dir / self.rt.netns(node.id))

    # -- start and stop -----------------------------------------------------

    def command(self, node) -> list[str]:
        exec_root = self.root() / f"n{node.id}"
        return [
            str(self.root() / "docker-launch.sh"), self.netns_path(node),
            str(self.s.netns_etc / self.rt.netns(node.id) / "resolv.conf"), str(self.root() / f"n{node.id}.cg"),
            "--data-root", str(self.data_root(node)), "--exec-root", str(exec_root),
            "--pidfile", str(self.pidfile(node)), "-H", self.docker_host(node), "--log-level", "warn",
            "--shutdown-timeout", "5",  # stopping a PC should not wait long on its containers
        ]

    def start(self, lab, node) -> None:
        if not node.docker:
            return
        root = self.root()
        root.mkdir(parents=True, exist_ok=True)
        script = root / "docker-launch.sh"
        script.write_text(LAUNCH)
        os.chmod(script, 0o755)
        cmd = self.command(node)
        with self.lock:
            self.state[node.id] = "starting"
            self._summary.pop(node.id, None)
        if self.s.dry_run:
            self.rt.sh.history.append(cmd)
            return
        self.data_root(node).mkdir(parents=True, exist_ok=True)
        self._open_proxy(node)
        env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        env.update(HTTPS_PROXY=f"http://127.0.0.1:{PULL_PORT}", HTTP_PROXY=f"http://127.0.0.1:{PULL_PORT}")
        log = (root / f"n{node.id}.log").open("ab")
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
                         start_new_session=True, close_fds=True)
        log.close()
        threading.Thread(target=self._settle, args=(lab.id, node.id), daemon=True).start()

    def resume(self, lab, node) -> None:
        """After Mikrobox restarts: engines kept running, but the pull proxy did not."""
        if self.s.dry_run or not node.docker:
            return
        if pid_alive(self.pidfile(node), "dockerd"):
            self._open_proxy(node)
            threading.Thread(target=self._settle, args=(lab.id, node.id), daemon=True).start()

    def _open_proxy(self, node) -> None:
        self._close_proxy(node)
        try:
            lst = nsnet.socket_in(self.netns_path(node))
            lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            lst.bind(("127.0.0.1", PULL_PORT))
            lst.listen(16)
        except OSError:
            return  # the engine still runs; pulls will fail and say so
        self.proxies[node.id] = PullProxy(lst)

    def _close_proxy(self, node) -> None:
        proxy = self.proxies.pop(node.id, None)
        if proxy:
            proxy.close()

    def _settle(self, lab_id: int, node_id: int) -> None:
        """Wait for the engine, then make sure the lab's saved containers exist."""
        lab = self.rt.store.state.lab(lab_id)
        node = lab.node(node_id) if lab else None
        if node is None:
            return
        eng = self.engine(node)
        deadline = time.time() + 60
        while time.time() < deadline and not eng.ping():
            if not self.rt.node_running(node):
                return
            time.sleep(0.5)
        if not eng.ping():
            tail = ""
            try:
                lines = (self.root() / f"n{node.id}.log").read_text(errors="replace").strip().splitlines()
                tail = next((l for l in reversed(lines) if "level=error" in l or "failed" in l.lower()), lines[-1] if lines else "")
            except OSError:
                pass
            with self.lock:
                self.state[node.id] = "The Docker engine did not start. " + tail[-300:]
            return
        try:
            self.version[node.id] = (eng.call("GET", "/version") or {}).get("Version", "")
        except EngineError:
            pass
        with self.lock:
            self.state[node.id] = "running"
        for spec in list(node.containers):
            self.ensure(lab, node, spec)

    def stop(self, node) -> None:
        self._close_proxy(node)
        with self.lock:
            self.state.pop(node.id, None)
            self._summary.pop(node.id, None)
        if self.s.dry_run:
            return
        pid = pid_alive(self.pidfile(node), "dockerd")
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)  # stops the containers, then itself
            except OSError:
                pass
            # Containers get --shutdown-timeout to finish; one that ignores SIGTERM
            # makes Docker wait on its own stop timeout (10 s), so cut that short:
            # killed below, it is still restarted with its PC like the others.
            deadline = time.time() + 7
            while time.time() < deadline and pid_alive(self.pidfile(node), "dockerd"):
                time.sleep(0.25)
        # Whatever is left of this engine goes now: a stuck dockerd, its containerd and
        # shims, and any container they left running (found by its cgroup, which
        # carries the container's id whichever cgroup driver Docker uses).
        mark = str(self.root() / f"n{node.id}")
        ids = self._container_ids(node)
        killed = []
        for p in Path("/proc").iterdir():
            if not p.name.isdigit() or int(p.name) == os.getpid():
                continue
            try:
                cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                groups = (p / "cgroup").read_text() if ids else ""
            except OSError:
                continue
            if mark + " " in cmd + " " or f"{mark}/" in cmd or any(i in groups for i in ids):
                try:
                    os.kill(int(p.name), signal.SIGKILL)
                    killed.append(p)
                except OSError:
                    pass
        # Wait for them to be gone: until then they hold the PC's network namespace.
        deadline = time.time() + 5
        while killed and time.time() < deadline:
            killed = [p for p in killed if p.exists() and _state(p) not in ("Z", "X")]
            time.sleep(0.05)
        self.sock(node).unlink(missing_ok=True)

    def _container_ids(self, node) -> list[str]:
        try:
            return [d.name for d in (self.data_root(node) / "containers").iterdir() if len(d.name) == 64]
        except OSError:
            return []

    def forget(self, node) -> None:
        """A PC was deleted: its engine's disk goes too."""
        self.stop(node)
        if not self.s.dry_run:
            shutil.rmtree(self.data_root(node), ignore_errors=True)
        for p in self.root().glob(f"n{node.id}.*"):
            if p.is_file():
                p.unlink(missing_ok=True)

    def ready(self, node) -> bool:
        return self.state.get(node.id) == "running"

    # -- containers ---------------------------------------------------------

    def ensure(self, lab, node, spec, start: bool = True) -> None:
        """Make a saved container exist (fetching its image if needed) and start it."""
        key = (node.id, spec.name)
        eng = self.engine(node)
        self.errors.pop(key, None)
        try:
            try:
                eng.call("GET", f"/containers/{quote(spec.name)}/json")
                exists = True
            except EngineError:
                exists = False
            if not exists:
                self.fetch_image(lab, node, spec.image, key)
                self.progress[key] = "Creating"
                eng.call("POST", f"/containers/create?name={quote(spec.name)}", self.create_body(node, spec))
                start = True
            if start:
                self.progress[key] = "Starting"
                eng.call("POST", f"/containers/{quote(spec.name)}/start")
        except EngineError as e:
            self.errors[key] = str(e)
        finally:
            self.progress.pop(key, None)
            self._summary.pop(node.id, None)

    def create_body(self, node, spec) -> dict:
        exposed, bindings = {}, {}
        for p in spec.ports:
            key = f"{p.container}/{p.proto}"
            exposed[key] = {}
            bindings.setdefault(key, []).append({"HostPort": str(p.host)})
        host = {"PortBindings": bindings}
        if spec.restart:
            host["RestartPolicy"] = {"Name": "unless-stopped"}
        if spec.docker_socket:
            host["Binds"] = [f"{self.sock(node)}:/var/run/docker.sock"]
        return {
            "Image": spec.image, "Env": [f"{k}={v}" for k, v in spec.env.items()],
            "ExposedPorts": exposed, "HostConfig": host, "Labels": {"mikrobox.saved": "1"},
        }

    def fetch_image(self, lab, node, ref: str, key) -> None:
        eng = self.engine(node)
        try:
            eng.call("GET", f"/images/{quote(ref, safe='')}/json")
            return  # already here
        except EngineError:
            pass
        # Another PC on this server may have it already: copy it across, no download.
        for other_lab in self.rt.store.state.labs:
            for other in other_lab.nodes:
                if other.id == node.id or not other.docker or not self.ready(other):
                    continue
                src = self.engine(other)
                try:
                    src.call("GET", f"/images/{quote(ref, safe='')}/json", timeout=3)
                except EngineError:
                    continue
                self.progress[key] = f"Copying {ref} from {other.name}"
                try:
                    sconn, sresp = src.open("GET", f"/images/get?names={quote(ref, safe='')}", timeout=120)
                    try:
                        if sresp.status == 200:
                            chunks = iter(lambda: sresp.read(1 << 16), b"")
                            dconn, dresp = eng.open("POST", "/images/load?quiet=1", body=chunks, timeout=300,
                                                    headers={"Content-Type": "application/x-tar"})
                            dresp.read()
                            dconn.close()
                            if dresp.status < 400:
                                return
                    finally:
                        sconn.close()
                except OSError:
                    pass
        self.pull(node, ref, key)

    def pull(self, node, ref: str, key) -> None:
        name, tag = split_ref(ref)
        path = f"/images/create?fromImage={quote(name, safe='')}" + (f"&tag={quote(tag, safe='')}" if tag else "")
        self.progress[key] = f"Downloading {ref}"
        try:
            conn, resp = self.engine(node).open("POST", path, timeout=900)
        except OSError as e:
            raise EngineError(f"The Docker engine did not answer: {e}") from None
        layers: dict[str, tuple[int, int]] = {}
        try:
            if resp.status >= 400:
                raise EngineError(json.loads(resp.read() or b"{}").get("message", f"Docker said {resp.status}"))
            for raw in resp:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("error"):
                    raise EngineError(msg["error"])
                d = msg.get("progressDetail") or {}
                if msg.get("id") and d.get("total"):
                    layers[msg["id"]] = (d.get("current", 0), d["total"])
                if layers:
                    done = sum(c for c, _ in layers.values())
                    total = sum(t for _, t in layers.values())
                    self.progress[key] = f"Downloading {ref}, {done * 100 // max(1, total)}%"
        finally:
            conn.close()

    def containers(self, lab, node, timeout: float = 4) -> dict:
        """What the interface shows: the engine's state and every container, saved or not."""
        saved = {c.name: c for c in node.containers}
        engine = {"available": self.available, "on": node.docker, "running": self.rt.node_running(node),
                  "state": "off", "version": self.version.get(node.id, ""), "message": ""}
        rows: list[dict] = []
        if not self.available:
            engine["state"] = "missing"
        elif node.docker and engine["running"]:
            st = self.state.get(node.id, "starting")
            engine["state"] = st if st in ("starting", "running") else "failed"
            engine["message"] = "" if st in ("starting", "running") else st
        elif node.docker:
            engine["state"] = "stopped"
        live = []
        if engine["state"] == "running":
            try:
                live = self.engine(node).call("GET", "/containers/json?all=1", timeout=timeout) or []
            except EngineError as e:
                engine["message"] = str(e)
        seen = set()
        for c in live:
            name = (c.get("Names") or ["/?"])[0].lstrip("/")
            seen.add(name)
            ports = []
            for p in c.get("Ports") or []:
                if p.get("PublicPort") and (p.get("IP") in (None, "", "0.0.0.0")):
                    item = {"host": p["PublicPort"], "container": p["PrivatePort"], "proto": p.get("Type", "tcp")}
                    if item not in ports:
                        ports.append(item)
            rows.append({
                "name": name, "image": c.get("Image", ""), "state": c.get("State", ""), "status": c.get("Status", ""),
                "running": c.get("State") == "running", "ports": sorted(ports, key=lambda x: x["host"]),
                "saved": name in saved, "busy": self.progress.get((node.id, name)), "error": self.errors.get((node.id, name)),
            })
        for name, spec in saved.items():
            if name in seen:
                continue
            busy = self.progress.get((node.id, name))
            rows.append({
                "name": name, "image": spec.image, "state": "pending", "running": False, "saved": True,
                "status": busy or ("Starts with the PC" if engine["state"] in ("stopped", "off") else "Not created yet"),
                "ports": [{"host": p.host, "container": p.container, "proto": p.proto} for p in spec.ports],
                "busy": busy, "error": self.errors.get((node.id, name)),
            })
        for r in rows:
            r["command"] = run_command(saved[r["name"]]) if r["name"] in saved else ""
        rows.sort(key=lambda r: r["name"])
        return {"engine": engine, "containers": rows}

    def summary(self, lab, node) -> dict | None:
        """For the badge on the canvas: how many containers, how many running. Cached briefly."""
        if not node.docker:
            return None
        now = time.time()
        cached = self._summary.get(node.id)
        if cached and now - cached[0] < 3:
            return cached[1]
        try:
            rows = self.containers(lab, node, timeout=1.5)["containers"]
        except Exception:
            rows = []
        value = {"total": len(rows), "running": sum(1 for r in rows if r["running"])}
        self._summary[node.id] = (now, value)
        return value

    def act(self, node, name: str, action: str) -> None:
        eng = self.engine(node)
        q = quote(name)
        if action == "start":
            eng.call("POST", f"/containers/{q}/start")
        elif action == "stop":
            eng.call("POST", f"/containers/{q}/stop?t=5", timeout=20)
        elif action == "restart":
            eng.call("POST", f"/containers/{q}/restart?t=5", timeout=20)
        elif action == "remove":
            eng.call("DELETE", f"/containers/{q}?force=1&v=1", timeout=20)
        self._summary.pop(node.id, None)

    def logs(self, node, name: str, tail: int = 400) -> str:
        data = self.engine(node).call("GET", f"/containers/{quote(name)}/logs?stdout=1&stderr=1&tail={tail}", timeout=10)
        if data is None:
            return ""
        if isinstance(data, (dict, list)):
            return json.dumps(data)
        return demux(data)

    def published(self, lab, node, name: str) -> list[dict]:
        for row in self.containers(lab, node)["containers"]:
            if row["name"] == name:
                return row["ports"]
        return []
