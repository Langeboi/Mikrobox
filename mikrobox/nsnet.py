"""Sockets inside a Linux PC's network namespace.

A socket belongs to the network namespace it was created in, whatever thread uses it
later. So the server can listen or connect "as" a PC without running anything inside it:
switch this thread into the PC's namespace, create the socket, switch straight back.
Call these from a worker thread, never the event loop's own.
"""

from __future__ import annotations

import ctypes
import os
import socket

CLONE_NEWNET = 0x40000000


def _setns(fd: int) -> None:
    if hasattr(os, "setns"):  # Python 3.12 and later
        os.setns(fd, CLONE_NEWNET)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.setns(fd, CLONE_NEWNET) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def socket_in(netns_path: str, family: int = socket.AF_INET, kind: int = socket.SOCK_STREAM) -> socket.socket:
    """A new, unconnected socket that lives in the given network namespace."""
    home = os.open("/proc/thread-self/ns/net", os.O_RDONLY)
    try:
        target = os.open(netns_path, os.O_RDONLY)
    except OSError:
        os.close(home)
        raise
    try:
        _setns(target)
        try:
            return socket.socket(family, kind)
        finally:
            _setns(home)  # never leave this thread in the PC
    finally:
        os.close(target)
        os.close(home)
