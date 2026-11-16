"""Talks to a running CHR over SSH on the management network."""

from __future__ import annotations

import io
import socket

IMPORT_FILE = "mikrobox-import.rsc"


class RouterError(Exception):
    pass


def _connect(host: str, username: str, password: str, timeout: float = 10):
    import paramiko

    try:
        sock = socket.create_connection((host, 22), timeout=timeout)
    except OSError as e:
        raise RouterError(f"No SSH answer from {host}: {e}. Is the router booted and on Management?")
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        try:
            transport.auth_password(username, password)
        except paramiko.AuthenticationException:
            if password:
                raise
            transport.auth_none(username)  # a fresh CHR has no password
    except paramiko.AuthenticationException:
        transport.close()
        raise RouterError(f"Login to {host} as {username} was refused. Check the password.")
    except (paramiko.SSHException, OSError, EOFError) as e:
        transport.close()
        raise RouterError(f"SSH to {host} failed: {e}")
    return transport


def _exec(transport, command: str, timeout: float = 60) -> str:
    chan = transport.open_session(timeout=timeout)
    chan.settimeout(timeout)
    chan.exec_command(command)
    chunks = []
    while True:
        data = chan.recv(65536)
        if not data:
            break
        chunks.append(data)
    chan.close()
    return b"".join(chunks).decode("utf-8", errors="replace")


def run(host: str, username: str, password: str, command: str) -> str:
    transport = _connect(host, username, password)
    try:
        return _exec(transport, command)
    except (OSError, EOFError) as e:
        raise RouterError(f"Command failed on {host}: {e}")
    finally:
        transport.close()


def export_config(host: str, username: str, password: str, sensitive: bool = False) -> str:
    return run(host, username, password, "/export show-sensitive" if sensitive else "/export")


def import_config(host: str, username: str, password: str, config: str) -> str:
    import paramiko

    transport = _connect(host, username, password)
    try:
        sftp = paramiko.SFTPClient.from_transport(transport)
        sftp.putfo(io.BytesIO(config.encode("utf-8")), IMPORT_FILE)
        sftp.close()
        output = _exec(transport, f"/import file-name={IMPORT_FILE}", timeout=180)
        _exec(transport, f'/file remove [find name="{IMPORT_FILE}"]')
        return output
    except (paramiko.SSHException, OSError, EOFError) as e:
        raise RouterError(f"Import on {host} failed: {e}")
    finally:
        transport.close()


def set_identity(host: str, username: str, password: str, name: str) -> str:
    return run(host, username, password, f'/system identity set name="{name}"')
