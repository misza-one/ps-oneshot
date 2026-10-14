"""Open foot windows follow their config's font size.

foot can't reload foot.ini, so when something rewrites the size there (Omarchy's Text size knob,
`omarchy-display-text-size`, ○ + left stick) the running windows (herdr's terminals among them)
keep their old size until reopened. This watches foot.ini and replays each change in every open
foot window as zoom steps: Ctrl + keypad +/−, foot's own font-increase/decrease, sent to the window
by Hyprland without focusing it. A window you zoomed by hand keeps its offset.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable

from gi.repository import Gio, GLib

from .hypr import Hyprland

log = logging.getLogger(__name__)

FOOT_INI = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "foot", "foot.ini")
FOOT_CLASSES = ("foot", "footclient")
STEP_MS = 40  # between zoom steps, so foot re-lays out each one
KEY_UP_MS = 15  # key down … up: a lone send_shortcut can leave the key stuck repeating


def foot_font(text: str) -> tuple[float | None, float]:
    """(font size in pt, zoom step in pt) from foot.ini's [main]. The step is foot's 0.5 pt unless
    font-size-adjustment says otherwise; 0 when it's in px or % (steps that don't map to pt)."""
    size: float | None = None
    step = 0.5
    section = "main"
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            if line.startswith("["):
                section = line.strip("[] ")
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if section != "main":
            continue
        if key == "font" and size is None:
            match = re.search(r":size=([0-9.]+)", value)
            size = float(match.group(1)) if match else None
        elif key == "font-size-adjustment":
            match = re.fullmatch(r"([0-9.]+)(?:pt)?", value)
            step = float(match.group(1)) if match else 0.0
    return size, step


class FootFollower:
    def __init__(self, hypr: Hyprland, after: Callable[[], None]) -> None:
        """`after` runs once open windows were resized (Omarchy: hide its "Restart Foot" notice)."""
        self._hypr = hypr
        self._after = after
        self._size = self._read()[0]
        self._monitor: Gio.FileMonitor | None = None
        directory = os.path.dirname(FOOT_INI)
        if os.path.isdir(directory):
            # The directory, not the file: tools rewrite foot.ini by renaming a new file over it.
            monitor = Gio.File.new_for_path(directory).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            monitor.connect("changed", self._changed)
            self._monitor = monitor

    def close(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()

    @staticmethod
    def _read() -> tuple[float | None, float]:
        try:
            with open(FOOT_INI, encoding="utf-8") as handle:
                return foot_font(handle.read())
        except OSError:
            return None, 0.0

    def _changed(self, _monitor: Gio.FileMonitor, file: Gio.File, other: Gio.File | None, _event: object) -> None:
        if os.path.basename(FOOT_INI) not in {f.get_basename() for f in (file, other) if f is not None}:
            return
        size, step = self._read()
        if size is None:
            return  # caught mid-rewrite: keep the last size we could read
        previous, self._size = self._size, size
        if previous is None or step <= 0:
            return
        steps = round((size - previous) / step)
        if steps:
            log.info("foot font %g → %g pt: zooming open foot windows %+d steps", previous, size, steps)
            self._zoom(steps)

    def _zoom(self, steps: int) -> None:
        try:
            windows = [
                str(client["address"]) for client in self._hypr.json("clients")
                if client.get("class") in FOOT_CLASSES
            ]
        except (OSError, ValueError) as error:
            log.warning("cannot list foot windows: %s", error)
            return
        if not windows:
            return
        key = "KP_Add" if steps > 0 else "KP_Subtract"
        for index in range(abs(steps)):
            GLib.timeout_add(index * STEP_MS, self._step, windows, key)
        GLib.timeout_add(abs(steps) * STEP_MS + 600, self._done)  # after the tool's own notice is up

    def _step(self, windows: list[str], key: str) -> bool:
        for window in windows:
            self._hypr.send_key(f"address:{window}", "CTRL", key, True)
        GLib.timeout_add(KEY_UP_MS, self._release, windows, key)
        return False

    def _release(self, windows: list[str], key: str) -> bool:
        for window in windows:
            self._hypr.send_key(f"address:{window}", "CTRL", key, False)
        return False

    def _done(self) -> bool:
        self._after()
        return False
