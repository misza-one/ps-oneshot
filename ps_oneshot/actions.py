"""Execute action strings (`wm:`, `dispatch:`, `exec:`, `key:`, and app built-ins)."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable

from gi.repository import GLib

from .hypr import Hyprland, HyprlandError

log = logging.getLogger(__name__)

WM_LABELS = {
    "workspace": "Workspace {arg}",
    "focus": "Focus window {dir}",
    "swap": "Move window {dir}",
    "movetoworkspace": "Send window to workspace {arg}",
    "fullscreen": "Fullscreen",
    "maximize": "Maximize",
    "float": "Toggle floating",
    "pin": "Pin window",
    "close": "Close window",
    "special": "Toggle {arg}",
    "cyclenext": "Next window",
    "cycleprev": "Previous window",
    "monitor": "Monitor {arg}",
}
RELATIVE_WM_LABELS = {
    "workspace": "{word} workspace",
    "movetoworkspace": "Send window to {word} workspace",
}
DIRECTION_WORDS = {"l": "left", "r": "right", "u": "up", "d": "down"}
WORKSPACE_WORDS = {"e-1": "previous", "e+1": "next", "r-1": "previous", "r+1": "next"}


def parse_combo(combo: str, names: dict[str, int]) -> list[int]:
    codes = []
    for part in combo.lower().split("+"):
        part = part.strip()
        if part not in names:
            raise ValueError(f"unknown key {part!r} in {combo!r}")
        codes.append(names[part])
    return codes


def describe(action: str, radial_titles: dict[str, str]) -> str:
    kind, _, arg = action.partition(":")
    if kind == "wm":
        op, _, value = arg.partition(" ")
        if op in RELATIVE_WM_LABELS and value in WORKSPACE_WORDS:
            return RELATIVE_WM_LABELS[op].format(word=WORKSPACE_WORDS[value]).capitalize()
        return WM_LABELS.get(op, op).format(arg=value, dir=DIRECTION_WORDS.get(value, value))
    if kind == "radial":
        return f"{radial_titles.get(arg, arg)} radial"
    if kind == "herd":
        return "Jump to waiting agent"
    if kind == "ui":
        return "Cheat sheet"
    if kind == "app":
        return "Pause / resume"
    if kind == "dictation":
        return {"back": "Erase last dictation", "clear": "Erase all dictation", "shoot": "Send dictation"}.get(arg, arg)
    if kind == "agent":
        return {"thinking": "Next thinking level"}.get(arg, arg)
    if kind == "sounds":
        return "Sound effects on/off"
    if kind == "rumble":
        return "Vibration on/off"
    if kind == "touchpad":
        return {"slower": "Touchpad slower", "faster": "Touchpad faster"}.get(arg, arg)
    if kind == "zoom":
        return {"in": "Text bigger", "out": "Text smaller", "reset": "Text size reset"}.get(arg, arg)
    if kind == "text":
        return {"bigger": "System text bigger", "smaller": "System text smaller", "reset": "System text reset"}.get(
            arg, arg
        )
    if kind == "couch":
        return {"toggle": "Couch mode on/off", "on": "Couch mode on", "off": "Couch mode off"}.get(arg, arg)
    if kind == "brightness":
        return {"up": "Brighter", "down": "Darker"}.get(arg, arg)
    if kind == "key":
        return ", ".join(" + ".join(p.capitalize() for p in combo.split("+")) for combo in arg.split())
    if kind == "launch":
        return arg.removesuffix(".desktop")
    if kind == "exec":
        return os.path.basename(arg.split()[0]) if arg.split() else arg
    return arg


def spawn(command: str | list[str]) -> None:
    argv = ["sh", "-c", command] if isinstance(command, str) else command
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        log.warning("cannot run %r: %s", command, error)
        return
    GLib.child_watch_add(GLib.PRIORITY_DEFAULT, process.pid, lambda *_: process.wait())


class ActionRunner:
    def __init__(
        self,
        hypr: Hyprland,
        key_tap: Callable[[list[int]], None],
        key_names: dict[str, int],
        builtins: dict[str, Callable[[str], None]],
    ) -> None:
        self._hypr = hypr
        self._key_tap = key_tap
        self._key_names = key_names
        self._builtins = builtins

    def run(self, action: str) -> None:
        kind, _, arg = action.partition(":")
        log.debug("action %s", action)
        if kind == "wm":
            op, _, value = arg.partition(" ")
            try:
                self._hypr.wm(op, value.strip())
            except HyprlandError as error:
                log.warning("%s: %s", action, error)
        elif kind == "dispatch":
            self._hypr.dispatch(arg)
        elif kind == "exec":
            spawn(os.path.expanduser(arg) if arg.startswith("~") else arg)
        elif kind == "key":
            try:
                combos = [parse_combo(combo, self._key_names) for combo in arg.split()]
            except ValueError as error:
                log.warning("%s: %s", action, error)
                return
            for codes in combos:
                self._key_tap(codes)
        elif kind in self._builtins:
            self._builtins[kind](arg)
        else:
            log.warning("unknown action %r", action)
