"""Execute action strings (`wm:`, `dispatch:`, `exec:`, `key:`, and app built-ins)."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable

from gi.repository import GLib

from .hypr import Hyprland, HyprlandError
from .linux import xkb

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


# Key names that stand for the symbol they type on a US keyboard: with a layout these become that symbol,
# wherever it sits there. Every other evdev name (enter, arrows, F-keys, keypad, modifiers) is a position.
TYPED_KEYS = {
    **{c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
    "minus": "minus", "equal": "equal", "leftbrace": "bracketleft", "rightbrace": "bracketright",
    "semicolon": "semicolon", "apostrophe": "apostrophe", "grave": "grave", "backslash": "backslash",
    "comma": "comma", "period": "period", "dot": "period", "slash": "slash",
}
LAYOUT_MODIFIERS = {"shift": "leftshift", "altgr": "rightalt", "ctrl": "leftctrl", "alt": "leftalt", "super": "leftmeta"}


def parse_combo(combo: str, names: dict[str, int], layout: xkb.Layout | None = None) -> list[int]:
    """Key codes for `combo` ("ctrl+shift+tab", "slash"). Its last key is a symbol when it's one of
    TYPED_KEYS or any other xkb keysym name ("at", "question"): with `layout`, it becomes the key and the
    modifiers that type that symbol there. Without one, or for a symbol the layout lacks, it's a US position."""
    *mods, key = (part.strip() for part in combo.split("+"))
    codes = []
    for part in mods:
        if part.lower() not in names:
            raise ValueError(f"unknown key {part!r} in {combo!r}")
        codes.append(names[part.lower()])
    position = None
    if layout is not None and (key.lower() in TYPED_KEYS or key.lower() not in names):
        position = layout.locate(xkb.keysym(TYPED_KEYS.get(key.lower(), key)))
    if position is not None:
        for mod in position.mods:
            if names[LAYOUT_MODIFIERS[mod]] not in codes:
                codes.append(names[LAYOUT_MODIFIERS[mod]])
        codes.append(position.code)
    elif key.lower() in names:
        codes.append(names[key.lower()])
    else:
        raise ValueError(f"unknown key {key!r} in {combo!r}")
    return codes


def needs_layout(combos: list[str], names: dict[str, int]) -> bool:
    """Whether any combo ends in a symbol (see parse_combo), so the keyboard layout matters."""
    for combo in combos:
        key = combo.rsplit("+", 1)[-1].strip().lower()
        if key in TYPED_KEYS or key not in names:
            return True
    return False


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
        return "Agents carousel" if arg == "pick" else "Jump to waiting agent"
    if kind == "ui":
        return "Cheat sheet"
    if kind == "app":
        return "Pause / resume"
    if kind == "dictation":
        return {"back": "Erase last dictation", "clear": "Erase all dictation", "shoot": "Send dictation"}.get(arg, arg)
    if kind == "agent":
        return {"thinking": "Thinking level / effort"}.get(arg, arg)
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
    if kind == "lights":
        return {
            "vivid": "Lightbar colours vivid/as on screen", "alert": "Next lightbar alert style",
            "brighter": "Lightbar brighter", "dimmer": "Lightbar dimmer", "glow_up": "Lightbar idle glow up",
            "glow_down": "Lightbar idle glow down", "scanner": "Running light on/off",
        }.get(arg, arg)
    if kind == "triggers":
        return {
            "adaptive": "Adaptive triggers on/off", "stronger": "Trigger click stronger",
            "weaker": "Trigger click weaker",
        }.get(arg, arg)
    if kind == "sticks":
        return {
            "deadzone": "Next stick deadzone", "scroll_faster": "Stick scroll faster",
            "scroll_slower": "Stick scroll slower",
            "pointer_faster": "Stick pointer faster", "pointer_slower": "Stick pointer slower",
            "repeat_faster": "Stick arrows faster", "repeat_slower": "Stick arrows slower",
        }.get(arg, arg)
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
        keyboard: str = "",
    ) -> None:
        self._hypr = hypr
        self._key_tap = key_tap
        self._key_names = key_names
        self._builtins = builtins
        self._keyboard = keyboard  # Hyprland's name for our virtual keyboard: its layout is what types
        self._layouts: dict[tuple, xkb.Layout | None] = {}

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
            layout = self._layout() if needs_layout(arg.split(), self._key_names) else None
            try:
                combos = [parse_combo(combo, self._key_names, layout) for combo in arg.split()]
            except ValueError as error:
                log.warning("%s: %s", action, error)
                return
            for codes in combos:
                self._key_tap(codes)
        elif kind in self._builtins:
            self._builtins[kind](arg)
        else:
            log.warning("unknown action %r", action)

    def _layout(self) -> xkb.Layout | None:
        """The active layout of our virtual keyboard (else Hyprland's main one), compiled once per layout."""
        if not xkb.available():
            return None
        try:
            keyboards = self._hypr.json("devices").get("keyboards", [])
        except (OSError, ValueError, AttributeError) as error:
            log.warning("cannot read the keyboard layout: %s", error)
            return None
        keyboard = next((k for k in keyboards if k.get("name") == self._keyboard), None) or next(
            (k for k in keyboards if k.get("main")), None
        )
        if keyboard is None:
            return None
        key = tuple(str(keyboard.get(f) or "") for f in ("layout", "variant", "options", "model", "rules")) + (
            int(keyboard.get("active_layout_index") or 0),
        )
        if key not in self._layouts:
            try:
                self._layouts[key] = xkb.Layout(*key[:5], index=key[5])
                log.debug("keyboard layout %s (index %d)", key[0], key[5])
            except xkb.LayoutError as error:
                log.warning("%s; key: actions use US-layout positions", error)
                self._layouts[key] = None
        return self._layouts[key]
