"""Accounts and sign-in.

Passwords are stored as salted scrypt hashes in <data>/users.json. A browser
session is a random token in an HttpOnly cookie; only its hash is kept on disk.
Scripts can use HTTP Basic auth instead (curl -u name ...).

Locked out? On the server:  python -m mikrobox.auth passwd <username>
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

from .events import CURRENT_USER

COOKIE = "mikrobox_session"
SESSION_SECONDS = 30 * 24 * 3600
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
MIN_PASSWORD = 8
MAX_FAILURES, FAILURE_WINDOW = 8, 300  # then that name is refused for the rest of the window


class AuthError(Exception):
    pass


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, data_dir: Path, enabled: bool = True, cookie: str = COOKIE) -> None:
        self.enabled = enabled
        self.cookie = cookie
        self.users_file = data_dir / "users.json"
        self.sessions_file = data_dir / "sessions.json"
        self.lock = threading.RLock()
        self.users: dict[str, dict] = self._load(self.users_file)
        self.sessions: dict[str, dict] = self._load(self.sessions_file)
        self.failures: dict[str, list[float]] = {}

    @staticmethod
    def _load(path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _save(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    # -- accounts --------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(u["name"] for u in self.users.values())

    def _check_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"The password needs at least {MIN_PASSWORD} characters.")
        if len(password) > 256:
            raise AuthError("The password is too long.")

    def create_user(self, name: str, password: str) -> str:
        if not USERNAME.match(name or ""):
            raise AuthError("Usernames are 1-32 letters, digits, dots, dashes or underscores.")
        self._check_password(password)
        with self.lock:
            if name.lower() in self.users:
                raise AuthError(f"There is already an account called {name}.")
            salt = secrets.token_bytes(16)
            self.users[name.lower()] = {"name": name, "salt": salt.hex(),
                                        "hash": _hash(password, salt).hex(), "created": int(time.time())}
            self._save(self.users_file, self.users)
        return name

    def set_password(self, name: str, password: str) -> None:
        self._check_password(password)
        with self.lock:
            user = self.users.get(name.lower())
            if user is None:
                raise AuthError(f"No account called {name}.")
            salt = secrets.token_bytes(16)
            user.update(salt=salt.hex(), hash=_hash(password, salt).hex())
            self._save(self.users_file, self.users)

    def delete_user(self, name: str) -> None:
        with self.lock:
            if name.lower() not in self.users:
                raise AuthError(f"No account called {name}.")
            if len(self.users) == 1:
                raise AuthError("This is the only account, so it cannot be removed.")
            del self.users[name.lower()]
            self._save(self.users_file, self.users)
            self.sessions = {k: v for k, v in self.sessions.items() if v["user"] != name.lower()}
            self._save(self.sessions_file, self.sessions)

    def verify(self, name: str, password: str) -> str | None:
        """The account's name if the password is right. Throttles repeated failures."""
        key = (name or "").lower()
        now = time.time()
        recent = [t for t in self.failures.get(key, []) if now - t < FAILURE_WINDOW]
        if len(recent) >= MAX_FAILURES:
            raise AuthError("Too many failed attempts. Wait a few minutes and try again.")
        user = self.users.get(key)
        salt = bytes.fromhex(user["salt"]) if user else b"\0" * 16
        got = _hash(password or "", salt)  # hashed even for unknown names, so timing says nothing
        if user and hmac.compare_digest(got, bytes.fromhex(user["hash"])):
            self.failures.pop(key, None)
            return user["name"]
        self.failures[key] = recent + [now]
        return None

    # -- sessions --------------------------------------------------------

    def new_session(self, name: str, ip: str | None = None) -> str:
        """A sign-in, good only from the address it was made from: a copy of the cookie
        (a lab page can be sent it, see from_elsewhere) is no use anywhere else."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
            self.sessions[_digest(token)] = {"user": name.lower(), "expires": now + SESSION_SECONDS, "ip": client_ip(ip)}
            self._save(self.sessions_file, self.sessions)
        return token

    def session_user(self, token: str | None, ip: str | None = None) -> str | None:
        if not token:
            return None
        s = self.sessions.get(_digest(token))
        if not s or s["expires"] < time.time():
            return None
        if s.get("ip") and s["ip"] != client_ip(ip):
            return None
        user = self.users.get(s["user"])
        return user["name"] if user else None

    def end_session(self, token: str | None) -> None:
        if not token:
            return
        with self.lock:
            if self.sessions.pop(_digest(token), None) is not None:
                self._save(self.sessions_file, self.sessions)

    # -- requests --------------------------------------------------------

    def cookie_token(self, headers: dict[bytes, bytes]) -> str | None:
        raw = headers.get(b"cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie(raw.decode("latin-1"))
        except Exception:
            return None
        return jar[self.cookie].value if self.cookie in jar else None

    def user_from_headers(self, headers: dict[bytes, bytes], ip: str | None = None) -> str | None:
        user = self.session_user(self.cookie_token(headers), ip)
        if user:
            return user
        basic = headers.get(b"authorization", b"")
        if basic[:6].lower() == b"basic ":
            try:
                name, _, password = base64.b64decode(basic[6:]).decode().partition(":")
                return self.verify(name, password)
            except (AuthError, ValueError):
                return None
        return None


UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def client_ip(addr: str | None) -> str | None:
    if not addr:
        return None
    return addr[7:] if addr.startswith("::ffff:") else addr


def scope_ip(scope) -> str | None:
    client = scope.get("client")
    return client_ip(client[0]) if client else None


def from_elsewhere(scope) -> bool:
    """A browser request that another page made: refused, so that pages in the lab, which
    web links serve from this same machine on other ports, can't act with your sign-in.

    Cookies don't tell ports apart, and to SameSite those pages count as this site, so
    the cookie alone doesn't protect: changes and websockets must come from this page.
    Requests from scripts and tools (no Origin, no Sec-Fetch-Site) are not affected.
    """
    if scope["type"] == "http" and scope.get("method", "GET") not in UNSAFE:
        return False
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
    site = headers.get("sec-fetch-site")
    if site in ("same-site", "cross-site"):
        return True
    origin = headers.get("origin")
    if origin is None:
        return False
    hosts = {headers.get("host"), headers.get("x-forwarded-host")} - {None}
    return origin == "null" or urlsplit(origin).netloc not in hosts


class AuthMiddleware:
    """Everything except the sign-in page and its API needs a signed-in account."""

    PUBLIC = ("/login", "/favicon.ico")

    def __init__(self, app, auth: Auth) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and from_elsewhere(scope):
            if scope["type"] == "websocket":
                await receive()
                await send({"type": "websocket.close", "code": 4403})
                return
            body = b'{"detail":"Only Mikrobox\'s own pages can do that."}'
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        if scope["type"] not in ("http", "websocket") or not self.auth.enabled:
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path in self.PUBLIC or path.startswith(("/static/", "/api/auth/")):
            return await self.app(scope, receive, send)
        user = self.auth.user_from_headers(dict(scope.get("headers") or []), scope_ip(scope))
        if user:
            scope.setdefault("state", {})["user"] = user
            token = CURRENT_USER.set(user)  # so the activity log knows who did it
            try:
                return await self.app(scope, receive, send)
            finally:
                CURRENT_USER.reset(token)
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 4401})
            return
        if path.startswith("/api/"):
            body = b'{"detail":"Sign in first."}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await send({"type": "http.response.start", "status": 302,
                    "headers": [(b"location", b"/login"), (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})


def main(argv: list[str] | None = None) -> int:
    """python -m mikrobox.auth list | add NAME | passwd NAME | remove NAME"""
    from .config import Settings

    args = sys.argv[1:] if argv is None else argv
    auth = Auth(Settings.from_env().data_dir)
    if args[:1] == ["list"]:
        print("\n".join(auth.names()) or "No accounts yet.")
        return 0
    if len(args) == 2 and args[0] in ("add", "passwd", "remove"):
        try:
            if args[0] == "remove":
                auth.delete_user(args[1])
            else:
                password = getpass.getpass("New password: ")
                if getpass.getpass("Again: ") != password:
                    print("The two passwords differ.", file=sys.stderr)
                    return 1
                (auth.create_user if args[0] == "add" else auth.set_password)(args[1], password)
        except AuthError as e:
            print(str(e), file=sys.stderr)
            return 1
        print("Done. Restart the service to apply: systemctl restart mikrobox")
        return 0
    print(main.__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
