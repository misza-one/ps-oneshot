"""The daemon's control socket: another process asks the running daemon to run an action.

`ps-oneshot agents` (bound to a key in Hyprland) sends `herd:pick` here, so the carousel opens in the daemon,
which already has the agents, the overlays and the pad. One line in, `ok` or `error: <why>` back. The socket
sits in $XDG_RUNTIME_DIR, mode 0600: only this user can talk to it. A second daemon (a test instance) finds
it answering and leaves it alone.
"""

from __future__ import annotations

import logging
import os
import socket
import tempfile
from collections.abc import Callable

from gi.repository import GLib

log = logging.getLogger(__name__)

MAX_LINE = 4096
READ_TIMEOUT_S = 0.5


def socket_path() -> str:
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "ps-oneshot", "control.sock")


def send(action: str, path: str | None = None, timeout: float = 2.0) -> str | None:
    """Run `action` in the running daemon: its reply ("ok" or "error: …"), or None when none is listening."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path or socket_path())
            sock.sendall(action.encode() + b"\n")
            sock.shutdown(socket.SHUT_WR)
            reply = b""
            while chunk := sock.recv(MAX_LINE):
                reply += chunk
    except OSError:
        return None
    return reply.decode(errors="replace").strip() or None


def _listening(path: str) -> bool:
    """Whether a daemon holds the socket: a leftover file from one that died refuses the connection."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(path)
    except OSError:
        return False
    return True


class ControlServer:
    def __init__(self, on_action: Callable[[str], str | None], path: str | None = None) -> None:
        """`on_action` runs an action on the main loop and returns None, or why it couldn't."""
        self._on_action = on_action
        self._path = path or socket_path()
        self._sock: socket.socket | None = None
        self._inode = 0
        self._source = 0

    def start(self) -> bool:
        if _listening(self._path):
            log.info("another ps-oneshot answers on %s; not taking commands here", self._path)
            return False
        try:
            os.makedirs(os.path.dirname(self._path), mode=0o700, exist_ok=True)
            if os.path.exists(self._path):
                os.unlink(self._path)  # left behind by a daemon that didn't stop cleanly
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(self._path)
            os.chmod(self._path, 0o600)
            sock.listen(4)
            sock.setblocking(False)
        except OSError as error:
            log.warning("no control socket at %s: %s", self._path, error)
            return False
        self._sock, self._inode = sock, os.stat(self._path).st_ino
        self._source = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, sock.fileno(), GLib.IOCondition.IN, self._on_ready)
        return True

    def stop(self) -> None:
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0
        if self._sock is not None:
            self._sock.close()
            self._sock = None
            try:
                if os.stat(self._path).st_ino == self._inode:  # still ours, not a newer daemon's
                    os.unlink(self._path)
            except OSError:
                pass

    def _on_ready(self, _fd: int, _condition: GLib.IOCondition) -> bool:
        assert self._sock is not None
        try:
            conn, _ = self._sock.accept()
        except BlockingIOError:
            return True
        except OSError as error:
            log.warning("control socket: %s", error)
            return True
        with conn:
            try:
                conn.settimeout(READ_TIMEOUT_S)
                data = b""
                while len(data) < MAX_LINE and (chunk := conn.recv(MAX_LINE)):
                    data += chunk
                    if b"\n" in data:
                        break
                action = data.decode(errors="replace").strip()
                if not action:  # another daemon checking whether we're here
                    return True
                log.debug("control: %s", action)
                error = self._on_action(action)
                conn.sendall(b"ok\n" if error is None else f"error: {error}\n".encode())
            except OSError as error:
                log.debug("control client went away: %s", error)
        return True
