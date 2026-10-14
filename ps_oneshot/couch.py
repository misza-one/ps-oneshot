"""Couch Coding Mode: scale the screen up to read it from across the room, and back.

Entering re-applies each chosen monitor's rule with its scale multiplied by `[couch] scale`, and
takes the `[couch] bar_hide` widgets off the Omarchy bar (~/.config/omarchy/shell.json), which has
no room for all of them at the bigger scale. Leaving reloads the Hyprland config, which puts every
monitor back to its configured rule exactly (including settings a runtime rule can't read back,
like ICC profiles), and puts the widgets back where they were.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from . import sounds
from .actions import spawn
from .hypr import Hyprland, clean_scale

log = logging.getLogger(__name__)

STATE_KEY = "couch"  # {"instance": Hyprland instance signature, "scales": {monitor: couch scale}}
BAR_STATE_KEY = "couch_bar"  # widgets taken off the bar: [{"section", "index", "entry"}]
SHELL_CONFIG = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "omarchy", "shell.json"
)


def _entry_id(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("id", ""))
    return entry if isinstance(entry, str) else ""


def take_entries(layout: dict[str, list], ids: set[str]) -> list[dict[str, Any]]:
    """Remove the widgets with these ids from the bar layout; returns where each one was."""
    taken = []
    for section, entries in layout.items():
        taken += [{"section": section, "index": i, "entry": e} for i, e in enumerate(entries) if _entry_id(e) in ids]
        layout[section] = [e for e in entries if _entry_id(e) not in ids]
    return taken


def put_back_entries(layout: dict[str, list], taken: list[dict[str, Any]]) -> None:
    """Reinsert taken widgets at their old positions (lowest first, so later indexes line up again)."""
    for item in sorted(taken, key=lambda item: item["index"]):
        entries = layout.setdefault(item["section"], [])
        entries.insert(min(item["index"], len(entries)), item["entry"])


def _edit_bar_layout(edit) -> bool:
    """Apply `edit(layout)` to the Omarchy bar layout and have the shell re-read it."""
    try:
        with open(SHELL_CONFIG, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as error:
        log.warning("couch mode: cannot read %s: %s", SHELL_CONFIG, error)
        return False
    layout = config.get("bar", {}).get("layout")
    if not isinstance(layout, dict):
        return False
    edit(layout)
    temporary = SHELL_CONFIG + ".ps-oneshot.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    os.replace(temporary, SHELL_CONFIG)  # atomic: the shell never reads half a file
    spawn(["omarchy-shell", "shell", "reloadConfig"])
    return True


class CouchMode:
    def __init__(self, hypr: Hyprland, cfg: dict[str, Any]) -> None:
        self._hypr = hypr
        self._cfg = cfg

    def recover(self) -> None:
        """Widgets still off the bar from a couch session that ended without us (crash, new login)."""
        if sounds.load_state(BAR_STATE_KEY, None) and not self.active:
            self._restore_bar()

    @property
    def active(self) -> bool:
        """On while a monitor still shows the scale we set in this Hyprland session. Anything that
        put the scale back (a reload, a monitor profile daemon, a new login) turns it off."""
        state = sounds.load_state(STATE_KEY, None)
        if not isinstance(state, dict) or state.get("instance") != self._hypr.instance:
            return False
        try:
            monitors = self._hypr.json("monitors")
        except (OSError, ValueError):
            return True
        scales: dict[str, float] = state.get("scales", {})
        return any(abs(float(m["scale"]) - scales[m["name"]]) < 0.01 for m in monitors if m["name"] in scales)

    def enter(self) -> list[str]:
        """Scale the configured monitors (default: the focused one) up; returns "NAME ×SCALE" per change."""
        try:
            monitors = self._hypr.json("monitors")
        except (OSError, ValueError) as error:
            log.warning("couch mode: cannot read monitors: %s", error)
            return []
        wanted = list(self._cfg["monitors"]) or [m["name"] for m in monitors if m.get("focused")]
        scales: dict[str, float] = {}
        for monitor in monitors:
            if monitor["name"] not in wanted or monitor.get("disabled"):
                continue
            scale = clean_scale(float(monitor["scale"]) * float(self._cfg["scale"]), monitor["width"], monitor["height"])
            if self._hypr.set_monitor(monitor, scale):
                scales[monitor["name"]] = scale
        if scales:
            sounds.save_state(STATE_KEY, {"instance": self._hypr.instance, "scales": scales})
            self._hide_bar()
        return [f"{name} ×{scale:g}" for name, scale in scales.items()]

    def exit(self) -> bool:
        sounds.save_state(STATE_KEY, None)
        self._restore_bar()
        return self._hypr.reload()

    def _hide_bar(self) -> None:
        ids = set(self._cfg["bar_hide"])
        if not ids or not os.path.exists(SHELL_CONFIG):
            return
        taken: list[dict[str, Any]] = []
        if _edit_bar_layout(lambda layout: taken.extend(take_entries(layout, ids))):
            # Added to what may still be off the bar from a session that didn't end cleanly.
            sounds.save_state(BAR_STATE_KEY, (sounds.load_state(BAR_STATE_KEY, None) or []) + taken)

    def _restore_bar(self) -> None:
        taken = sounds.load_state(BAR_STATE_KEY, None)
        if taken and _edit_bar_layout(lambda layout: put_back_entries(layout, taken)):
            sounds.save_state(BAR_STATE_KEY, None)
