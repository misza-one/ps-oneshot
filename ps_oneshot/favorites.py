"""Favorites: radial slots you fill from the controller, remembered in the state file.

A slot holds an entry shaped like a config item: {"action": "launch:<desktop id>"} takes the app's name and
icon, {"label", "icon", "action"} runs any action, and None is an empty slot. `[favorites] items` only seeds
the slots; once you change one, the state file wins."""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gi.repository import GLib

from . import APP_NAME, sounds
from .actions import spawn
from .apps import DesktopApp, app_for_window, installed

log = logging.getLogger(__name__)

STATE_KEY = "favorites"
Entry = dict[str, str]


@dataclass(frozen=True)
class Favorite:
    """What a filled slot shows and runs."""

    label: str
    action: str
    icon: str = ""  # Nerd Font glyph
    image: str = ""  # the app's own icon (Gio icon string), used when no glyph is set


def place(slots: list[Entry | None], index: int, entry: Entry) -> None:
    """Put `entry` in slot `index`. If it's in another slot already, the two trade places."""
    current = next((i for i, slot in enumerate(slots) if slot and slot["action"] == entry["action"]), None)
    if current is not None:
        slots[current] = slots[index]
    slots[index] = entry


def shift(slots: list[Entry | None], index: int, step: int) -> int | None:
    """Swap slot `index` with its neighbour `step` away: the new index, or None past either end."""
    target = index + step
    if not 0 <= target < len(slots):
        return None
    slots[index], slots[target] = slots[target], slots[index]
    return target


def resolve(entry: Entry, apps: dict[str, DesktopApp]) -> Favorite:
    action = entry["action"]
    app = apps.get(action.removeprefix("launch:")) if action.startswith("launch:") else None
    label = entry.get("label") or (app.name if app else action.removeprefix("launch:").removesuffix(".desktop"))
    icon = entry.get("icon", "")
    return Favorite(label=label, action=action, icon=icon, image="" if icon or app is None else app.icon)


def notify(message: str) -> None:
    spawn(["notify-send", "-a", APP_NAME, "-t", "4000", message])


class Favorites:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self._cfg = cfg
        self._picking = False
        count = int(cfg["slots"])
        saved = sounds.load_state(STATE_KEY, None)
        entries = saved if isinstance(saved, list) else cfg["items"]
        self.slots: list[Entry | None] = [
            dict(entry) if isinstance(entry, dict) and isinstance(entry.get("action"), str) else None
            for entry in entries[:count]
        ]
        self.slots += [None] * (count - len(self.slots))

    def shown(self) -> list[Favorite | None]:
        apps = {app.id: app for app in installed()}
        return [resolve(entry, apps) if entry else None for entry in self.slots]

    def _save(self) -> None:
        sounds.save_state(STATE_KEY, self.slots)

    # editing ------------------------------------------------------------------------

    @staticmethod
    def candidate(window_class: str) -> DesktopApp | None:
        """The installed app that owns windows of `window_class`, which pinning the focused window would add."""
        return app_for_window(window_class, installed(visible_only=True))

    def pin(self, index: int, app: DesktopApp) -> None:
        place(self.slots, index, {"action": f"launch:{app.id}"})
        self._save()

    def remove(self, index: int) -> None:
        self.slots[index] = None
        self._save()

    def move(self, index: int, step: int) -> int | None:
        target = shift(self.slots, index, step)
        if target is not None:
            self._save()
        return target

    # the list -----------------------------------------------------------------------

    def options(self) -> dict[str, Entry]:
        """The picker's lines → entries: the configured favorites, then every installed app by name."""
        apps = installed(visible_only=True)
        by_id = {app.id: app for app in apps}
        options: dict[str, Entry] = {}
        for entry in self._cfg["items"]:
            options.setdefault(resolve(entry, by_id).label, dict(entry))
        offered = {entry["action"] for entry in options.values()}
        for app in sorted(apps, key=lambda app: app.name.lower()):
            action = f"launch:{app.id}"
            if action in offered or not app.name:
                continue
            label = f"{app.name} ({app.id.removesuffix('.desktop')})" if app.name in options else app.name
            options[label] = {"action": action}
        return options

    def pick(self, index: int, done: Callable[[], None]) -> None:
        """Show the [favorites] picker. The chosen line fills slot `index`; `done` runs either way."""
        if self._picking:
            return
        self._picking = True
        options = self.options()
        threading.Thread(target=self._run_picker, args=(index, options, done), daemon=True).start()

    def _run_picker(self, index: int, options: dict[str, Entry], done: Callable[[], None]) -> None:
        try:
            result = subprocess.run(
                ["sh", "-c", self._cfg["picker"]], input="\n".join(options) + "\n", capture_output=True, text=True
            )
        except OSError as error:
            result = subprocess.CompletedProcess([], 127, "", str(error))
        GLib.idle_add(self._picked, index, options, result, done)

    def _picked(
        self, index: int, options: dict[str, Entry], result: subprocess.CompletedProcess, done: Callable[[], None]
    ) -> bool:
        self._picking = False
        lines = result.stdout.splitlines()
        # Omarchy's picker answers "<label>\t<subtext>" for rows with a subtext; dmenus answer the line.
        choice = lines[0].split("\t")[0] if result.returncode == 0 and lines else ""
        if choice in options:
            place(self.slots, index, options[choice])
            self._save()
            log.info("favorite %d: %s", index + 1, choice)
        elif result.returncode > 1:  # 1 is a dismissed list
            message = (result.stderr.strip().splitlines() or [f"exit status {result.returncode}"])[-1]
            log.warning("favorites picker failed: %s", message)
            notify(f"Favorites list failed: {message}")
        done()
        return False
