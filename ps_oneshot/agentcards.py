"""Agent cards: what the agent carousel and the image-selector picker show for each Omaherd agent.

A card is drawn in CARD_W x CARD_H design units (twice Omarchy's expanded picker card, 768x475): machine,
status pill, the status ring with its icon, project, task and herdr tab. A strip is the narrow skewed slice
an unselected agent gets in the carousel: its icon and project name, upright. Each agent also has a status
dot (`dot_style`), which the carousel draws live, blinking while the agent waits.
"""

from __future__ import annotations

import math
import time

import cairo

from .herd import BADGES, STATUS_GREYS, STATUS_ICONS, Agent, by_machine
from .ui.theme import RGBA, Theme

from gi.repository import Pango, PangoCairo  # noqa: E402  (versions pinned by ps_oneshot.ui)

CARD_W, CARD_H = 1536, 950
CARD_DOT = (108.0, 82.0)  # the status dot's centre on a card, design units: left of the machine name
CARD_DOT_RADIUS = 13.0
HEADER_X = 142  # the machine name, after the dot
TITLE_WEIGHT = Pango.Weight.SEMIBOLD  # Omarchy labels its picker cards DemiBold


def card_order(agents: list[Agent]) -> list[Agent]:
    """Grouped by machine, as on the R3 ring, so each agent keeps its place from one opening to the next."""
    return by_machine(agents)


def most_urgent(agents: list[Agent]) -> Agent:
    """The agent a picker opens on: blocked, then done, then working, then idle; within a status, the one
    that has been in it longest."""
    return min(agents, key=lambda a: (-a.urgency, a.since if a.since > 0 else float("inf")))


def matches(agent: Agent, needle: str) -> bool:
    """Typed filter text: a case-insensitive part of the project, task, tab, agent kind or machine."""
    haystack = " ".join((agent.project, agent.short_title, agent.tab, agent.kind, agent.machine, agent.host))
    return needle.strip().lower() in haystack.lower()


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


def dot_style(agent: Agent, theme: Theme, lights: dict) -> tuple[RGBA, float]:
    """The status dot: (colour, radius in logical px). Waiting agents get the lightbar's red or yellow (and
    blink); a working one a steady light dot, an idle one a faint small one."""
    if agent.waiting:
        return (*_rgb(status_color(agent, lights)), 1.0), 5.5
    if agent.status == "working":
        return (*theme.foreground, 0.62), 4.5
    return (*theme.foreground, 0.24), 3.5


# drawing ----------------------------------------------------------------------------------------------


def _rgb(color: tuple[int, int, int]) -> tuple[float, float, float]:
    return color[0] / 255, color[1] / 255, color[2] / 255


def _layout(cr: cairo.Context, theme: Theme, text: str, size: float, weight: Pango.Weight = Pango.Weight.NORMAL,
            width: float | None = None, lines: int = 1, align: Pango.Alignment = Pango.Alignment.CENTER) -> Pango.Layout:
    layout = PangoCairo.create_layout(cr)
    font = Pango.FontDescription.from_string(theme.font)
    font.set_absolute_size(size * Pango.SCALE)
    font.set_weight(weight)
    layout.set_font_description(font)
    if width is not None:
        layout.set_width(int(width * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_height(-lines)
        layout.set_ellipsize(Pango.EllipsizeMode.END)
    layout.set_alignment(align)
    layout.set_text(text, -1)
    return layout


def _text(
    cr: cairo.Context, theme: Theme, text: str, size: float, color: tuple[float, ...], y: float,
    width: float = CARD_W - 200, weight: Pango.Weight = Pango.Weight.NORMAL, lines: int = 1, x: float | None = None,
    align: Pango.Alignment = Pango.Alignment.CENTER,
) -> float:
    """Draw `text` with its top at `y` (centred on the card unless `x` is given); returns its height."""
    layout = _layout(cr, theme, text, size, weight, width, lines, align)
    _ink, logical = layout.get_pixel_extents()
    cr.set_source_rgba(*color)
    cr.move_to((CARD_W - width) / 2 if x is None else x, y)
    PangoCairo.show_layout(cr, layout)
    return logical.height


def _pill(cr: cairo.Context, theme: Theme, text: str, color: tuple[float, float, float], right: float, top: float) -> None:
    layout = _layout(cr, theme, text, 34, TITLE_WEIGHT)
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


def _backdrop(cr: cairo.Context, theme: Theme, kind: tuple[float, float, float], width: float, height: float,
              glow: float) -> None:
    """The theme's surface fading into its background, with the agent kind's colour as a soft middle column."""
    gradient = cairo.LinearGradient(0, 0, 0, height)
    gradient.add_color_stop_rgb(0, *theme.surface)
    gradient.add_color_stop_rgb(1, *theme.background)
    cr.set_source(gradient)
    cr.paint()
    column = cairo.LinearGradient(width / 2 - glow, 0, width / 2 + glow, 0)
    column.add_color_stop_rgba(0, *kind, 0)
    column.add_color_stop_rgba(0.5, *kind, 0.16)
    column.add_color_stop_rgba(1, *kind, 0)
    cr.set_source(column)
    cr.paint()


def _icon(cr: cairo.Context, theme: Theme, agent: Agent, cx: float, cy: float, size: float,
          color: tuple[float, float, float]) -> None:
    icon = STATUS_ICONS.get(agent.status, "")
    if not icon:
        return
    layout = _layout(cr, theme, icon, size)
    ink, _logical = layout.get_pixel_extents()
    cr.set_source_rgb(*color)
    cr.move_to(cx - ink.x - ink.width / 2, cy - ink.y - ink.height / 2)
    PangoCairo.show_layout(cr, layout)


def paint_card(cr: cairo.Context, agent: Agent, theme: Theme, lights: dict, now: float | None = None,
               dot: bool = True) -> None:
    """A card in CARD_W x CARD_H units. `dot` draws the status dot too; the carousel draws its own, live."""
    kind = _rgb(agent.color)
    status = _rgb(status_color(agent, lights))
    fg, muted = theme.foreground, theme.muted
    _backdrop(cr, theme, kind, CARD_W, CARD_H, 260)

    # Header: where it runs on the left, status and how long on the right.
    where = agent.machine if not agent.local else f"{agent.hostname} (local)" if agent.hostname else "this machine"
    _text(cr, theme, where, 34, (*muted, 1), 62, width=880, x=HEADER_X, align=Pango.Alignment.LEFT)
    if dot:
        rgba, _radius = dot_style(agent, theme, lights)
        cr.arc(*CARD_DOT, CARD_DOT_RADIUS, 0, 2 * math.pi)
        cr.set_source_rgba(*rgba)
        cr.fill()
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
    _icon(cr, theme, agent, cx, cy, 130, kind)

    y = 540
    y += _text(cr, theme, agent.project, 84, (*fg, 1), y, weight=TITLE_WEIGHT) + 18
    title = agent.short_title
    if title:
        y += _text(cr, theme, title, 46, (*fg, 0.86), y, lines=2) + 22
    tab = agent.tab.strip()
    footer = " · ".join(p for p in (agent.kind, "" if tab.isdigit() else tab) if p)
    if footer:
        _text(cr, theme, footer, 36, (*kind, 1), min(y, CARD_H - 100), weight=TITLE_WEIGHT)


def strip_centre(width: float, height: float, skew: float, y: float) -> float:
    """The x of a skewed strip's centre line at height `y` (its top edge starts `skew` further right)."""
    return (width + skew) / 2 - skew * y / height


def paint_strip(cr: cairo.Context, agent: Agent, theme: Theme, width: float, height: float, skew: float) -> None:
    """An unselected agent's strip, in logical px: its icon, and its project running up the strip."""
    kind = _rgb(agent.color)
    _backdrop(cr, theme, kind, width, height, width * 0.9)
    icon_y = height * 0.30
    _icon(cr, theme, agent, strip_centre(width, height, skew, icon_y), icon_y, 30, kind)
    # The project, reading bottom to top between the icon and the status dot.
    top, bottom = height * 0.42, height - 44
    layout = _layout(cr, theme, agent.project, 15, TITLE_WEIGHT, width=bottom - top, align=Pango.Alignment.LEFT)
    _ink, logical = layout.get_pixel_extents()
    mid = (top + bottom) / 2
    cr.save()
    cr.translate(strip_centre(width, height, skew, mid), bottom)
    cr.rotate(-math.pi / 2)
    cr.set_source_rgba(*theme.foreground, 0.92)
    cr.move_to(0, -logical.height / 2)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


def draw_card(path: str, agent: Agent, theme: Theme, lights: dict, now: float | None = None) -> None:
    """A card as a PNG, for Omarchy's image selector."""
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, CARD_W, CARD_H)
    paint_card(cairo.Context(surface), agent, theme, lights, now)
    surface.write_to_png(path)
