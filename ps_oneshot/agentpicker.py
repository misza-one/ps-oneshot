"""Keyboard agent picker: Omaherd's agents as cards in Omarchy's image selector (the theme switcher's carousel).

`ps-oneshot agents` polls the status once, draws one card per agent (cairo, current theme), opens them with
`omarchy-shell image-selector open`, waits for the choice and goes to that agent the way the R3 ring does.
The selector takes the keyboard (arrows, Enter, Escape, typing filters) and, through the `menu` layer, the
pad. It runs as its own process, so a key binding in Hyprland is enough to open it.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

import cairo

from . import APP_NAME
from .config import Config
from .herd import BADGES, STATUS_GREYS, STATUS_ICONS, Agent, HerdProvider, by_machine, waiting_queue
from .hypr import Hyprland, HyprlandError
from .ui.theme import Theme, load_theme

from gi.repository import Pango, PangoCairo  # noqa: E402  (versions pinned by ps_oneshot.ui)

log = logging.getLogger(__name__)

# Twice the selector's expanded card (768x475), so it stays sharp at scale 2. Unselected cards are narrow
# strips cut from the middle, so everything that tells agents apart sits on the vertical centre line.
CARD_W, CARD_H = 1536, 950
PICK_TIMEOUT_S = 600  # give up on a selector that never answers (omarchy-shell restarted under it)
DONE_POLL_S = 0.02


def card_order(agents: list[Agent]) -> list[Agent]:
    """Waiting agents first, most urgent and longest waiting first; then the rest, grouped by machine."""
    waiting = waiting_queue(agents)
    return waiting + [a for a in by_machine(agents) if a not in waiting]


def card_names(agents: list[Agent]) -> list[str]:
    """One distinct file name per card. The selector filters on it, so it carries what you'd type."""
    names: list[str] = []
    for agent in agents:
        parts = [agent.project, agent.short_title, agent.kind]
        if not agent.local:
            parts.append(agent.machine)
        base = " ".join(p for p in parts if p)
        base = "".join(" " if c in "/\t\n\r" else c for c in base).strip()[:120] or "agent"
        name, n = base, 2
        while name in names:
            name, n = f"{base} {n}", n + 1
        names.append(name)
    return names


def waited(since_ms: int, now: float | None = None) -> str:
    """How long ago an agent changed status, short: "45s", "12m", "3h 5m", "2d"."""
    if since_ms <= 0:
        return ""
    seconds = max(0, int((now if now is not None else time.time()) - since_ms / 1000))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours, minutes = divmod(seconds // 60, 60)
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{seconds // 86400}d"


def status_color(agent: Agent, lights: dict) -> tuple[int, int, int]:
    if agent.status in ("blocked", "done"):
        return tuple(lights[agent.status])  # type: ignore[return-value]
    return STATUS_GREYS.get(agent.status, STATUS_GREYS["idle"])


# drawing ----------------------------------------------------------------------------------------------


def _rgb(color: tuple[int, int, int]) -> tuple[float, float, float]:
    return color[0] / 255, color[1] / 255, color[2] / 255


def _text(
    cr: cairo.Context, theme: Theme, text: str, size: float, color: tuple[float, ...], y: float,
    width: float = CARD_W - 200, weight: Pango.Weight = Pango.Weight.NORMAL, lines: int = 1, x: float | None = None,
    align: Pango.Alignment = Pango.Alignment.CENTER,
) -> float:
    """Draw `text` with its top at `y` (centred on the card unless `x` is given); returns its height."""
    layout = PangoCairo.create_layout(cr)
    font = Pango.FontDescription.from_string(theme.font)
    font.set_absolute_size(size * Pango.SCALE)
    font.set_weight(weight)
    layout.set_font_description(font)
    layout.set_width(int(width * Pango.SCALE))
    layout.set_alignment(align)
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_height(-lines)
    layout.set_ellipsize(Pango.EllipsizeMode.END)
    layout.set_text(text, -1)
    _ink, logical = layout.get_pixel_extents()
    cr.set_source_rgba(*color)
    cr.move_to((CARD_W - width) / 2 if x is None else x, y)
    PangoCairo.show_layout(cr, layout)
    return logical.height


def _pill(cr: cairo.Context, theme: Theme, text: str, color: tuple[float, float, float], right: float, top: float) -> None:
    layout = PangoCairo.create_layout(cr)
    font = Pango.FontDescription.from_string(theme.font)
    font.set_absolute_size(34 * Pango.SCALE)
    font.set_weight(Pango.Weight.BOLD)
    layout.set_font_description(font)
    layout.set_text(text, -1)
    _ink, logical = layout.get_pixel_extents()
    pad_x, height = 30, 64
    width = logical.width + 2 * pad_x
    x = right - width
    cr.new_sub_path()
    cr.arc(x + height / 2, top + height / 2, height / 2, math.pi / 2, 3 * math.pi / 2)
    cr.arc(x + width - height / 2, top + height / 2, height / 2, 3 * math.pi / 2, math.pi / 2)
    cr.close_path()
    cr.set_source_rgba(*color, 0.22)
    cr.fill_preserve()
    cr.set_source_rgba(*color, 0.9)
    cr.set_line_width(3)
    cr.stroke()
    cr.set_source_rgb(*color)
    cr.move_to(x + pad_x, top + (height - logical.height) / 2)
    PangoCairo.show_layout(cr, layout)


def draw_card(path: str, agent: Agent, theme: Theme, lights: dict, now: float | None = None) -> None:
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, CARD_W, CARD_H)
    cr = cairo.Context(surface)
    kind = _rgb(agent.color)
    status = _rgb(status_color(agent, lights))
    fg, muted = theme.foreground, theme.muted

    gradient = cairo.LinearGradient(0, 0, 0, CARD_H)
    gradient.add_color_stop_rgb(0, *theme.surface)
    gradient.add_color_stop_rgb(1, *theme.background)
    cr.set_source(gradient)
    cr.paint()
    # The kind's colour as a soft column down the middle: it's what an unselected strip shows.
    glow = cairo.LinearGradient(CARD_W / 2 - 260, 0, CARD_W / 2 + 260, 0)
    glow.add_color_stop_rgba(0, *kind, 0)
    glow.add_color_stop_rgba(0.5, *kind, 0.16)
    glow.add_color_stop_rgba(1, *kind, 0)
    cr.set_source(glow)
    cr.paint()

    # Header: where it runs on the left, status and how long on the right.
    where = agent.machine if not agent.local else f"{agent.machine} (local)" if agent.machine else "local"
    _text(cr, theme, where, 34, (*muted, 1), 62, width=900, x=80, align=Pango.Alignment.LEFT)
    badge = BADGES.get(agent.status, agent.status.upper())
    since = waited(agent.since, now)
    _pill(cr, theme, f"{badge} · {since}" if since else badge, status, CARD_W - 80, 50)

    # The status ring and icon, on the centre line.
    cx, cy, radius = CARD_W / 2, 330, 150
    cr.new_path()
    cr.arc(cx, cy, radius, 0, 2 * math.pi)
    cr.set_source_rgba(*kind, 0.18)
    cr.fill_preserve()
    cr.set_source_rgb(*status)
    cr.set_line_width(14 if agent.waiting else 8)
    cr.stroke()
    icon = STATUS_ICONS.get(agent.status, "")
    if icon:
        layout = PangoCairo.create_layout(cr)
        font = Pango.FontDescription.from_string(theme.font)
        font.set_absolute_size(130 * Pango.SCALE)
        layout.set_font_description(font)
        layout.set_text(icon, -1)
        ink, _logical = layout.get_pixel_extents()
        cr.set_source_rgb(*kind)
        cr.move_to(cx - ink.x - ink.width / 2, cy - ink.y - ink.height / 2)
        PangoCairo.show_layout(cr, layout)

    y = 540
    y += _text(cr, theme, agent.project, 84, (*fg, 1), y, weight=Pango.Weight.BOLD) + 18
    title = agent.short_title
    if title:
        y += _text(cr, theme, title, 46, (*fg, 0.86), y, lines=2) + 22
    tab = agent.tab.strip()
    footer = " · ".join(p for p in (agent.kind, "" if tab.isdigit() else tab) if p)
    if footer:
        _text(cr, theme, footer, 36, (*kind, 1), min(y, CARD_H - 100), weight=Pango.Weight.BOLD)
    surface.write_to_png(path)


# the picker -------------------------------------------------------------------------------------------


def _notify(text: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", APP_NAME, "-t", "4000", text], check=False, timeout=3)


def _cards_dir() -> str:
    """A new directory per run, the old ones removed: the selector's image cache is keyed by path."""
    root = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "ps-oneshot", "agents")
    os.makedirs(root, exist_ok=True)
    for old in os.listdir(root):
        shutil.rmtree(os.path.join(root, old), ignore_errors=True)
    return tempfile.mkdtemp(dir=root)


def choose(paths: list[str], selected: str) -> str | None:
    """Show the cards in the image selector; the chosen card's path, or None when it was dismissed."""
    rows = "\n".join(f"{p}\t{p}" for p in paths)
    directory = os.path.dirname(paths[0])
    selection, done = os.path.join(directory, ".selection"), os.path.join(directory, ".done")
    open(selection, "w").close()
    encoded = base64.b64encode(rows.encode()).decode()
    result = subprocess.run(
        ["omarchy-shell", "image-selector", "open", "", encoded, selected, selection, done, "false", "true"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode or result.stdout.strip() != "ok":
        raise OSError((result.stderr or result.stdout).strip() or "image selector refused the request")
    deadline = time.monotonic() + PICK_TIMEOUT_S
    while not os.path.exists(done):
        if time.monotonic() > deadline:
            subprocess.run(["omarchy-shell", "image-selector", "cancel", done], capture_output=True, timeout=5)
            return None
        time.sleep(DONE_POLL_S)
    with open(selection) as handle:
        chosen = handle.read().strip()
    return chosen or None


def run(cfg: Config) -> int:
    herd_cfg = cfg.section("herd")
    if not HerdProvider.available(herd_cfg):
        print("agents: Omaherd isn't available ([herd] enabled, status_command, attach)", file=sys.stderr)
        return 1
    if shutil.which("omarchy-shell") is None:
        print("agents: needs omarchy-shell (Omarchy's image selector)", file=sys.stderr)
        return 1
    try:
        hypr = Hyprland()
    except HyprlandError as error:
        print(f"agents: {error}", file=sys.stderr)
        return 1
    provider = HerdProvider(herd_cfg, lambda _agents: None, hypr)
    agents = provider.poll()
    if agents is None:
        _notify("Omaherd didn't answer")
        return 1
    if not agents:
        _notify("No agents running")
        return 0

    ordered = card_order(agents)
    theme, lights, now = load_theme(), cfg.section("lights"), time.time()
    directory = _cards_dir()
    try:
        paths = [os.path.join(directory, f"{name}.png") for name in card_names(ordered)]
        for path, agent in zip(paths, ordered):
            draw_card(path, agent, theme, lights, now)
        try:
            chosen = choose(paths, paths[0])
        except (OSError, subprocess.TimeoutExpired) as error:
            log.warning("image selector: %s", error)
            _notify(f"Couldn't open the agent picker: {error}")
            return 1
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if chosen is None:
        return 0
    by_path = dict(zip(paths, ordered))
    agent = by_path.get(chosen)
    if agent is None:
        log.warning("image selector chose %r, not one of the cards", chosen)
        return 1
    log.info("picked %s on %s", agent.project, agent.machine)
    provider.focus_now(agent)
    return 0
