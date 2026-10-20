"""The daemon's control socket: another process asks the running daemon to run an action.

`ps-oneshot agents` (bound to a key in Hyprland) sends `herd:pick` here, so the carousel opens in the daemon,
which already has the agents, the overlays and the pad. One line in, `ok` or `error: <why>` back. Two lines
aren't actions: `status` answers with the daemon's state as one JSON line (`status.snapshot`), and `watch`
keeps the connection open and sends that line again whenever it changes (the Omarchy bar widget). Lines sent
after `watch` on that connection are actions, run without a reply, so the widget's clicks need no connection of
their own. The socket sits in $XDG_RUNTIME_DIR, mode 0600: only this user can talk to it. A second daemon (a
test instance) finds it answering and leaves it alone.
"""

from __future__ import annotations

import logging
import os
import socket
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import GLib

log = logging.getLogger(__name__)

MAX_LINE = 4096
READ_TIMEOUT_S = 0.5


@dataclass
class _Watcher:
    conn: socket.socket
    source: int  # its GLib fd watch
    partial: bytes = b""  # an action line still coming in


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
    def __init__(
        self, on_action: Callable[[str], str | None], path: str | None = None, status: Callable[[], str] | None = None,
    ) -> None:
        """`on_action` runs an action on the main loop and returns None, or why it couldn't. `status` gives the
        state line for `status` and `watch`."""
        self._on_action = on_action
        self._status = status
        self._path = path or socket_path()
        self._sock: socket.socket | None = None
        self._inode = 0
        self._source = 0
        self._watchers: dict[int, _Watcher] = {}  # by fd
        self._published = ""

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
        for fd in list(self._watchers):
            self._drop(fd)
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

    def publish(self) -> None:
        """Send the state line to every watcher, if it changed since the last one sent."""
        if not self._watchers or self._status is None:
            return
        line = self._status()
        if line != self._published:
            self._published = line
            for fd in list(self._watchers):
                self._send(fd, line)

    def _on_ready(self, _fd: int, _condition: GLib.IOCondition) -> bool:
        assert self._sock is not None
        try:
            conn, _ = self._sock.accept()
        except BlockingIOError:
            return True
        except OSError as error:
            log.warning("control socket: %s", error)
            return True
        try:
            conn.settimeout(READ_TIMEOUT_S)
            data = b""
            while len(data) < MAX_LINE and (chunk := conn.recv(MAX_LINE)):
                data += chunk
                if b"\n" in data:
                    break
            first, _, rest = data.partition(b"\n")
            line = first.decode(errors="replace").strip()
            if line == "watch" and self._status is not None:
                self._watch(conn, rest)
                return True
            if line == "status" and self._status is not None:
                conn.sendall(self._status().encode() + b"\n")
            elif line:  # empty: another daemon checking whether we're here
                log.debug("control: %s", line)
                error = self._on_action(line)
                conn.sendall(b"ok\n" if error is None else f"error: {error}\n".encode())
        except OSError as error:
            log.debug("control client went away: %s", error)
        conn.close()
        return True

    # watchers -------------------------------------------------------------------

    def _watch(self, conn: socket.socket, rest: bytes) -> None:
        conn.setblocking(False)
        fd = conn.fileno()
        condition = GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR
        source = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, fd, condition, self._on_watcher)
        self._watchers[fd] = _Watcher(conn, source)
        assert self._status is not None
        self._published = self._status()
        self._send(fd, self._published)
        log.debug("control: a watcher joined (%d)", len(self._watchers))
        if rest and fd in self._watchers and not self._take(fd, rest):
            self._drop(fd)

    def _on_watcher(self, fd: int, _condition: GLib.IOCondition) -> bool:
        """An action line from the watcher (the widget's clicks), or it hung up."""
        watcher = self._watchers[fd]
        try:
            data = watcher.conn.recv(MAX_LINE)
        except BlockingIOError:
            return True
        except OSError:
            data = b""
        if not data or not self._take(fd, data):
            self._drop(fd, source=False)
            return False
        return fd in self._watchers  # an action's publish may have dropped it

    def _take(self, fd: int, data: bytes) -> bool:
        """Run the complete action lines in `data`. False: a line too long to be one, drop the watcher."""
        watcher = self._watchers[fd]
        *lines, watcher.partial = (watcher.partial + data).split(b"\n")
        if len(watcher.partial) > MAX_LINE:
            return False
        for line in lines:
            if action := line.decode(errors="replace").strip():
                log.debug("control (watcher): %s", action)
                if (error := self._on_action(action)) is not None:
                    log.info("control action %s: %s", action, error)
        return True

    def _send(self, fd: int, line: str) -> None:
        """Non-blocking: a watcher whose socket is full isn't reading, so it's dropped (it reconnects)."""
        data = line.encode() + b"\n"
        try:
            sent = self._watchers[fd].conn.send(data)
        except OSError:
            sent = 0
        if sent < len(data):
            self._drop(fd)

    def _drop(self, fd: int, source: bool = True) -> None:
        watcher = self._watchers.pop(fd, None)
        if watcher is None:
            return
        if source:
            GLib.source_remove(watcher.source)
        watcher.conn.close()
