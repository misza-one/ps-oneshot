"""Visual smoke test: `python -m ps_oneshot.ui.demo` shows two radial menus, then the cheat sheet."""

from __future__ import annotations

import logging
import math
import sys

from ps_oneshot.preload import ensure_layer_shell_preload

STICK_FRAME_MS = 30
CHEATSHEET_SECONDS = 2.5

WAIT, DONE, RUN, IDLE = (220, 68, 68), (232, 196, 64), (90, 102, 118), (58, 62, 70)
CLAUDE = {"label": "Claude", "icon": "\U000f06a9", "color": (217, 119, 87)}
CODEX = {"label": "Codex", "icon": "\uf120", "color": (124, 170, 255)}
LOCAL = {"group": "\uf109 workstation", "group_color": (130, 251, 156)}
SERVER = {"group": "\uf233 build-server", "group_color": (124, 160, 240)}

AGENTS = [
    {**CLAUDE, **LOCAL, "sublabel": "landing page", "badge": "WAIT", "badge_color": WAIT, "pulse": True,
     "detail": "Waiting for input · 2 min"},
    {**CODEX, **LOCAL, "sublabel": "mobile app", "badge": "RUN", "badge_color": RUN,
     "detail": "Running tests"},
    {**CLAUDE, **LOCAL, "sublabel": "db tuning", "badge": "DONE", "badge_color": DONE,
     "detail": "Finished 5 min ago"},
    {**CLAUDE, **SERVER, "sublabel": "prod-deploy", "badge": "RUN", "badge_color": RUN,
     "detail": "Deploying batch 9"},
    {**CODEX, **SERVER, "sublabel": "integration", "badge": "IDLE", "badge_color": IDLE,
     "detail": "Idle for 1 h"},
    {**CLAUDE, **SERVER, "sublabel": "backup", "badge": "WAIT", "badge_color": WAIT, "pulse": True,
     "detail": "Needs approval"},
]

OUTPUT = {"group": "Output", "group_color": (130, 251, 156)}
INPUT = {"group": "Input", "group_color": (124, 160, 240)}
AUDIO = [
    {**OUTPUT, "label": "Speakers", "icon": "\uf028", "sublabel": "default", "active": True,
     "detail": "Built-in analog stereo"},
    {**OUTPUT, "label": "Headphones", "icon": "\uf025", "sublabel": "WH-1000XM4", "detail": "Bluetooth"},
    {**OUTPUT, "label": "Monitor", "icon": "\uf108", "sublabel": "DP-3", "detail": "DisplayPort audio"},
    {**OUTPUT, "label": "DualSense", "icon": "\uf11b", "sublabel": "controller", "detail": "Controller speaker"},
    {**INPUT, "label": "Microphone", "icon": "\uf130", "sublabel": "built-in", "detail": "Internal mic"},
    {**INPUT, "label": "DualSense", "icon": "\uf130", "sublabel": "controller", "active": True,
     "detail": "Controller mic"},
    {**INPUT, "label": "Webcam", "icon": "\uf03d", "sublabel": "C920", "detail": "USB webcam mic"},
]

# (title, items, seconds on screen)
SCENES = [("Agents", AGENTS, 3.0), ("Audio", AUDIO, 2.5)]

SECTIONS = [
    (
        "Navigation",
        [
            ("D-pad ← →", "Previous / next workspace"),
            ("D-pad ↑ ↓", "Cycle windows"),
            ("Left stick", "Focus in a direction"),
            ("L1 / R1", "Move window to workspace"),
        ],
    ),
    (
        "Menus",
        [
            ("L3", "Launcher radial"),
            ("R3", "Agents radial"),
            ("✕", "Confirm selection"),
            ("○", "Cancel"),
        ],
    ),
    (
        "Windows",
        [
            ("△", "Toggle fullscreen"),
            ("□", "Toggle floating"),
            ("L2 + ○", "Close window"),
        ],
    ),
    (
        "Pointer",
        [
            ("Touchpad", "Move the pointer"),
            ("Click", "Left click"),
            ("Two fingers", "Scroll"),
        ],
    ),
    (
        "Voice",
        [
            ("Mic", "Push-to-talk (hold)"),
            ("PS", "Show this cheat sheet"),
        ],
    ),
]


def main() -> int:
    ensure_layer_shell_preload()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from gi.repository import GLib, Gtk

    from ps_oneshot.ui.cheatsheet import CheatSheet
    from ps_oneshot.ui.radial import RadialItem, RadialMenu, slice_for_vector
    from ps_oneshot.ui.theme import load_theme

    if not Gtk.init_check():
        logging.getLogger(__name__).error("cannot initialise GTK (no Wayland display?)")
        return 1

    loop = GLib.MainLoop()
    radial = RadialMenu(load_theme)
    cheatsheet = CheatSheet(load_theme)

    def play(scene: int) -> bool:
        if scene == len(SCENES):
            radial.hide()
            cheatsheet.show(SECTIONS, None)
            GLib.timeout_add(int(CHEATSHEET_SECONDS * 1000), finish)
            return GLib.SOURCE_REMOVE
        title, fields, seconds = SCENES[scene]
        items = [RadialItem(**item) for item in fields]
        radial.hide()  # replay the open animation for each scene
        radial.show(title, items, None)
        started = GLib.get_monotonic_time() / 1_000_000

        def move_stick() -> bool:
            # A virtual stick: rests for 0.4 s, then sweeps clockwise once round the menu.
            t = GLib.get_monotonic_time() / 1_000_000 - started
            if t >= seconds:
                return GLib.SOURCE_REMOVE
            angle = (t - 0.4) / (seconds - 0.4) * 2 * math.pi
            reach = 0.0 if t < 0.4 else 0.9
            radial.set_selection(
                slice_for_vector(math.sin(angle) * reach, -math.cos(angle) * reach, len(items), 0.35)
            )
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(STICK_FRAME_MS, move_stick)
        GLib.timeout_add(int(seconds * 1000), play, scene + 1)
        return GLib.SOURCE_REMOVE

    def finish() -> bool:
        cheatsheet.hide()
        loop.quit()
        return GLib.SOURCE_REMOVE

    play(0)
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
