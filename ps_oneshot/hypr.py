"""Hyprland IPC: request socket (`hyprctl` protocol) and the socket2 event stream."""

from __future__ import annotations

import json
import logging
import math
import os
import socket
from collections.abc import Callable
from typing import Any

from gi.repository import GLib

log = logging.getLogger(__name__)

DIRECTIONS = {"l", "r", "u", "d"}


class HyprlandError(RuntimeError):
    pass


def _instance_dir() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    bases = (os.path.join(runtime, "hypr"), "/tmp/hypr")
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if signature:
        for base in bases:
            path = os.path.join(base, signature)
            if os.path.isdir(path):
                return path
        raise HyprlandError(f"no Hyprland socket directory for instance {signature}")
    # Started outside the session environment (e.g. a service before env import): newest live instance.
    candidates = [
        os.path.join(base, name)
        for base in bases if os.path.isdir(base)
        for name in os.listdir(base)
        if os.path.exists(os.path.join(base, name, ".socket.sock"))
    ]
    if not candidates:
        raise HyprlandError("no running Hyprland instance found")
    return max(candidates, key=os.path.getmtime)


def _lua_quote(value: str) -> str:
    """A Lua string literal. Non-ASCII stays as it is: JSON's \\uXXXX escapes aren't valid Lua."""
    escaped = []
    for char in value:
        if char in '"\\':
            escaped.append("\\" + char)
        elif ord(char) < 32 or ord(char) == 127:
            escaped.append(f"\\{ord(char):03d}")  # Lua's decimal escape
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def clean_scale(scale: float, width: int, height: int) -> float:
    """Round `scale` up to one Hyprland accepts: the mode must divide into whole logical pixels, in
    1/120 steps (fractional-scale protocol), so k/120 with k dividing gcd(width·120, height·120)."""
    whole = math.gcd(width * 120, height * 120)
    k = min(max(1, round(scale * 120)), whole)
    while whole % k:
        k += 1
    return k / 120


def _number(value: float) -> str:
    return f"{value:g}"


def monitor_fields(monitor: dict[str, Any], scale: float) -> list[tuple[str, str | float | int]]:
    """Everything Hyprland reports about a monitor's setup (j/monitors), as monitor-rule fields with
    a new scale. Re-applying a rule resets what it leaves out, so keep mode, position, colour etc."""
    fields: list[tuple[str, str | float | int]] = [
        ("mode", f"{monitor['width']}x{monitor['height']}@{_number(monitor['refreshRate'])}"),
        ("position", f"{monitor['x']}x{monitor['y']}"),
        ("scale", scale),
        ("transform", int(monitor.get("transform", 0))),
    ]
    if "2101010" in str(monitor.get("currentFormat", "")):
        fields.append(("bitdepth", 10))
    if monitor.get("mirrorOf", "none") not in ("none", ""):
        fields.append(("mirror", str(monitor["mirrorOf"])))
    if monitor.get("colorManagementPreset"):
        fields.append(("cm", str(monitor["colorManagementPreset"])))
    for key, field in (
        ("sdrBrightness", "sdrbrightness"), ("sdrSaturation", "sdrsaturation"),
        ("sdrMinLuminance", "sdr_min_luminance"), ("sdrMaxLuminance", "sdr_max_luminance"),
    ):
        if isinstance(monitor.get(key), (int, float)):
            fields.append((field, monitor[key]))
    return fields


class Hyprland:
    def __init__(self) -> None:
        directory = _instance_dir()
        self.request_path = os.path.join(directory, ".socket.sock")
        self.events_path = os.path.join(directory, ".socket2.sock")
        self._lua: bool | None = None
        self.instance = os.path.basename(directory)  # HYPRLAND_INSTANCE_SIGNATURE of this session

    def request(self, command: str) -> str:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(self.request_path)
            sock.sendall(command.encode())
            chunks = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    def json(self, command: str) -> Any:
        return json.loads(self.request(f"j/{command}"))

    @property
    def lua(self) -> bool:
        """Hyprland >= 0.55 with a Lua config takes Lua dispatch expressions."""
        if self._lua is None:
            try:
                self._lua = self.request("eval return 1").strip() == "ok"
            except OSError:
                return True
        return self._lua

    def dispatch(self, payload: str) -> bool:
        try:
            reply = self.request(f"dispatch {payload}").strip()
        except OSError as error:
            log.warning("dispatch %r failed: %s", payload, error)
            return False
        if reply != "ok":
            log.warning("dispatch %r: %s", payload, reply.splitlines()[0] if reply else "no reply")
            return False
        return True

    def wm(self, op: str, arg: str = "") -> bool:
        return self.dispatch(self.wm_payload(op, arg))

    def wm_payload(self, op: str, arg: str = "") -> str:
        lua = self.lua
        quoted = _lua_quote(arg)
        if op in ("focus", "swap") and arg not in DIRECTIONS:
            raise HyprlandError(f"wm:{op} needs a direction l|r|u|d, got {arg!r}")
        table: dict[str, tuple[str, str]] = {
            "workspace": (f"hl.dsp.focus({{ workspace = {quoted} }})", f"workspace {arg}"),
            "focus": (f"hl.dsp.focus({{ direction = {quoted} }})", f"movefocus {arg}"),
            "swap": (f"hl.dsp.window.swap({{ direction = {quoted} }})", f"swapwindow {arg}"),
            "movetoworkspace": (f"hl.dsp.window.move({{ workspace = {quoted} }})", f"movetoworkspace {arg}"),
            "fullscreen": ('hl.dsp.window.fullscreen({ mode = "fullscreen" })', "fullscreen 0"),
            "maximize": ('hl.dsp.window.fullscreen({ mode = "maximized" })', "fullscreen 1"),
            "float": ('hl.dsp.window.float({ action = "toggle" })', "togglefloating"),
            "pin": ("hl.dsp.window.pin()", "pin"),
            "close": ("hl.dsp.window.close()", "killactive"),
            "special": (f"hl.dsp.workspace.toggle_special({quoted})", f"togglespecialworkspace {arg}"),
            "cyclenext": ("hl.dsp.window.cycle_next()", "cyclenext"),
            "cycleprev": ("hl.dsp.window.cycle_next({ next = false })", "cyclenext prev"),
            "monitor": (f"hl.dsp.focus({{ monitor = {quoted} }})", f"focusmonitor {arg}"),
        }
        if op not in table:
            raise HyprlandError(f"unknown wm op {op!r} (valid: {', '.join(sorted(table))})")
        return table[op][0 if lua else 1]

    def monitor_payload(self, monitor: dict[str, Any], scale: float) -> str:
        """Request that re-applies `monitor`'s rule with a new scale (Lua `hl.monitor` or `keyword monitor`)."""
        fields = monitor_fields(monitor, scale)
        if self.lua:
            parts = [f"output = {_lua_quote(monitor['name'])}"]
            for key, value in fields:
                parts.append(f"{key} = {_lua_quote(value) if isinstance(value, str) else _number(value)}")
            return "eval hl.monitor({ " + ", ".join(parts) + " })"
        mode, position, scale_field, *extras = fields
        head = [monitor["name"], str(mode[1]), str(position[1]), _number(scale_field[1])]
        tail = [item for key, value in extras for item in (key, value if isinstance(value, str) else _number(value))]
        return "keyword monitor " + ",".join(head + tail)

    def set_monitor(self, monitor: dict[str, Any], scale: float) -> bool:
        payload = self.monitor_payload(monitor, scale)
        try:
            reply = self.request(payload).strip()
        except OSError as error:
            log.warning("%s failed: %s", payload, error)
            return False
        if reply != "ok":
            log.warning("%s: %s", payload, reply.splitlines()[0] if reply else "no reply")
            return False
        return True

    def reload(self) -> bool:
        """Re-read the config: every monitor goes back to its configured rule."""
        try:
            return self.request("reload").strip() == "ok"
        except OSError as error:
            log.warning("reload failed: %s", error)
            return False

    def send_key(self, window: str, mods: str, key: str, down: bool) -> bool:
        """Key down or up (an xkb keysym name such as KP_Add, so the keyboard layout doesn't matter)
        straight to `window` ("address:0x…"), without focusing it. Legacy Hyprland only sends whole
        shortcuts: it goes out on `down`, and `up` does nothing."""
        if self.lua:
            state = "down" if down else "up"
            return self.dispatch(
                f"hl.dsp.send_key_state({{ mods = {_lua_quote(mods)}, key = {_lua_quote(key)}, "
                f"state = {_lua_quote(state)}, window = {_lua_quote(window)} }})"
            )
        return self.dispatch(f"sendshortcut {mods}, {key}, {window}") if down else True

    def focus_window(self, address: str) -> bool:
        """Focus a window by address ("0x…"), going to its workspace."""
        if self.lua:
            return self.dispatch(f"hl.dsp.focus({{ window = {_lua_quote('address:' + address)} }})")
        return self.dispatch(f"focuswindow address:{address}")

    def focused_monitor(self) -> str | None:
        try:
            for monitor in self.json("monitors"):
                if monitor.get("focused"):
                    return monitor.get("name")
        except (OSError, ValueError):
            pass
        return None

    def active_window(self) -> tuple[str, str, str]:
        """(class, title, address without 0x) of the focused window; empty strings if none."""
        try:
            window = self.json("activewindow")
        except (OSError, ValueError):
            return "", "", ""
        address = str(window.get("address", "")).removeprefix("0x")
        return str(window.get("class", "")), str(window.get("title", "")), address

    def layer_namespaces(self) -> list[str]:
        try:
            data = self.json("layers")
        except (OSError, ValueError):
            return []
        return [
            layer.get("namespace", "")
            for monitor in data.values()
            for level in monitor.get("levels", {}).values()
            for layer in level
        ]


class EventStream:
    """socket2 line protocol (`event>>data`) delivered on the GLib main loop; reconnects."""

    def __init__(self, path: str, callback: Callable[[str, str], None]) -> None:
        self._path = path
        self._callback = callback
        self._sock: socket.socket | None = None
        self._watch = 0
        self._buffer = b""

    def start(self) -> None:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self._path)
        except OSError as error:
            log.warning("event socket unavailable (%s); retrying", error)
            GLib.timeout_add_seconds(2, self._retry)
            return
        sock.setblocking(False)
        self._sock = sock
        self._watch = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT, sock.fileno(), GLib.IOCondition.IN | GLib.IOCondition.HUP, self._on_readable
        )

    def _retry(self) -> bool:
        self.start()
        return False

    def stop(self) -> None:
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _on_readable(self, _fd: int, _condition: GLib.IOCondition) -> bool:
        assert self._sock is not None
        try:
            data = self._sock.recv(65536)
        except BlockingIOError:
            return True
        except OSError:
            data = b""
        if not data:
            self._watch = 0
            self._sock.close()
            self._sock = None
            GLib.timeout_add_seconds(2, self._retry)
            return False
        self._buffer += data
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            event, _, payload = line.decode("utf-8", "replace").partition(">>")
            try:
                self._callback(event, payload)
            except Exception:
                log.exception("event handler failed for %s", event)
        return True
