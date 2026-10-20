"""Render the README pictures: `python -m tools.screenshots [--theme NAME] [out_dir]` (default docs/images).

Draws the real overlays (radial menu, cheat sheet) offscreen into PNGs, over a plain backdrop and with
made-up agents and apps, so no personal windows or names end up in the docs. Run it in a desktop
session: app icons come from the GTK icon theme (missing ones are left out) and colours from the
current Omarchy theme, or the installed one `--theme` names (the docs are drawn in retro-82). The Omarchy bar widget is QML in another process, so `bar.png` and the
marketplace's `preview.png` (written to the repository root) redraw it here, after plugin/BarWidget.qml.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
import tomllib
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango, PangoCairo  # noqa: E402

from ps_oneshot import config  # noqa: E402
from ps_oneshot.agentcarousel import AgentCarousel, CarouselHooks  # noqa: E402
from ps_oneshot.herd import Agent  # noqa: E402
from ps_oneshot.labels import cheatsheet_sections  # noqa: E402
from ps_oneshot.menus import ICON_ADD, INLINE_COLOR, agent_slices  # noqa: E402
from ps_oneshot.status import machines  # noqa: E402
from ps_oneshot.ui.carousel import Carousel  # noqa: E402
from ps_oneshot.ui.cheatsheet import CheatSheet, _Metrics  # noqa: E402
from ps_oneshot.ui.radial import RadialItem, RadialMenu, _Hold, draw_halo  # noqa: E402
from ps_oneshot.ui.theme import Theme, _theme_from, _with_shell, load_theme  # noqa: E402

SCALE = 1.5  # pixels per logical pixel: sharp in GitHub's README column
SCREEN = (1920, 1080)  # the logical monitor the overlays size themselves for
BATTERY = (75, False)  # the radial's corner panel: percent, charging
BAR_HEIGHT = 26  # Omarchy's horizontal bar
PANEL_WIDTH = 360  # the widget's panel, as plugin/BarWidget.qml asks for it
PS_LOGO, PAUSE = "\U000f0414", "\U000f03e4"

FAVORITE = {"group": "Favorites", "group_color": INLINE_COLOR}
FAVORITES = [
    RadialItem(label="Terminal", icon="\uf120", **FAVORITE),
    RadialItem(label="Browser", icon="\uf0ac", **FAVORITE),
    RadialItem(label="Obsidian", image="obsidian", icon="\uf249", **FAVORITE),
    RadialItem(label="WhatsApp", image="whatsapp", icon="\uf232", **FAVORITE),
    RadialItem(label="YouTube", image="youtube", icon="\uf16a", **FAVORITE),
    RadialItem(label="Add", icon=ICON_ADD, **FAVORITE),
    RadialItem(label="PS One Shot", icon="\uf11b", detail="›"),
    RadialItem(label="System", icon="\uf013", detail="›"),
]


def agents() -> list[Agent]:
    now_ms = int(time.time() * 1000)

    def agent(project: str, kind: str, status: str, host: str, hostname: str, tab: str, title: str = "",
              minutes: float = 0) -> Agent:
        return Agent(key=f"{host}:{project}", project=project, kind=kind, status=status, title=title,
                     pane_id="w1:p1", session="default", host=host, hostname=hostname,
                     since=now_ms - int(minutes * 60_000), tab=tab)

    return [
        agent("landing-page", "claude", "blocked", "local", "workstation", "hero copy", "Pick a headline", 4),
        agent("mobile-app", "codex", "working", "local", "workstation", "tests", "Fix the flaky login test", 2),
        agent("docs", "omp", "done", "local", "workstation", "guide", "Write the setup guide", 1),
        agent("api", "pi", "working", "build", "build-server", "migrations", "Add the orders table", 6),
        agent("billing", "opencode", "blocked", "build", "build-server", "deploy", "Approve the prod deploy", 9),
        agent("infra", "claude", "idle", "build", "build-server", "terraform", "Plan the VPC", 30),
    ]


def backdrop(cr: cairo.Context, width: float, height: float, theme: Theme, alphas: tuple[float, float]) -> None:
    """A quiet stand-in desktop (theme colours, two soft glows) under the overlay's own dimming."""
    cr.set_source_rgb(*theme.surface)
    cr.paint()
    for x, y, rgb in ((0.15, 0.1, theme.accent), (0.9, 0.85, theme.muted)):
        glow = cairo.RadialGradient(width * x, height * y, 0, width * x, height * y, max(width, height) * 0.75)
        glow.add_color_stop_rgba(0, *rgb, 0.35)
        glow.add_color_stop_rgba(1, *rgb, 0.0)
        cr.set_source(glow)
        cr.paint()
    centre, edge = alphas
    dim = cairo.RadialGradient(width / 2, height / 2, 0, width / 2, height / 2, max(width, height) * 0.71)
    dim.add_color_stop_rgba(0, *theme.background, centre)
    dim.add_color_stop_rgba(1, *theme.background, edge)
    cr.set_source(dim)
    cr.paint()


def canvas(width: float, height: float) -> tuple[cairo.ImageSurface, cairo.Context]:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, round(width * SCALE), round(height * SCALE))
    surface.set_device_scale(SCALE, SCALE)
    return surface, cairo.Context(surface)


def ring(
    theme: Theme, title: str, items: list[RadialItem], selection: int, legend: list[tuple[str, str]],
    hold: tuple[str, str, str, float] | None = None, desktop: bool = True,
) -> cairo.ImageSurface:
    """`desktop`: over a stand-in desktop with the battery in its corner; without, for composing elsewhere."""
    menu = RadialMenu(lambda: theme)
    menu.theme = theme
    menu._area = Gtk.DrawingArea()  # unrealized: only lends the icon scale
    width, height = menu._content_size(*SCREEN)
    menu._title = title
    menu._set_items(items)
    menu._selection = selection
    menu._pop = [1.0 if index == selection else 0.0 for index in range(len(items))]
    menu._legend = legend
    menu._battery = BATTERY if desktop else None
    if hold is not None:
        glyph, verb, label, progress = hold  # progress of a one-second hold
        menu._hold = _Hold(glyph, verb, label, time.monotonic() - progress, 1.0)
    surface, cr = canvas(width, height)
    if desktop:
        backdrop(cr, width, height, theme, menu.backdrop_alpha)
    cr.save()
    cr.translate(width / 2, height / 2)
    draw_halo(cr, theme, menu._radius)  # the live overlay paints it with CSS
    cr.restore()
    menu._draw(cr, round(width), round(height))
    return surface


class _Herd:
    def __init__(self, agents: list[Agent]) -> None:
        self.agents = agents


def carousel(theme: Theme, cfg: config.Config) -> cairo.ImageSurface:
    """The agents carousel as it opens: on the most urgent agent, its blinking dots lit."""
    view = Carousel(lambda: theme)
    view.theme = theme
    view._area = Gtk.DrawingArea()  # unrealized: only lends the scale
    session = AgentCarousel(cfg, view, _Herd(agents()), CarouselHooks(  # type: ignore[arg-type]
        rumble=lambda _pattern: None, monitor=lambda: None, capture_sticks=lambda: None, notify=print,
    ))
    session.ui = view
    view.reload_theme = lambda: None  # type: ignore[method-assign]  # keep the theme we were given
    view._present = lambda _monitor: None  # type: ignore[method-assign]  # offscreen: no window
    view._painted_theme = theme
    session.open()
    width, height = view._content_size(*SCREEN)
    surface, cr = canvas(width, height)
    backdrop(cr, width, height, theme, (0.0, 0.0))
    r, g, b, alpha = theme.picker_scrim
    cr.set_source_rgba(r, g, b, alpha)
    cr.paint()
    view._draw(cr, round(width), round(height))
    return surface


def cheat_sheet(theme: Theme, cfg: config.Config) -> cairo.ImageSurface:
    sheet = CheatSheet(lambda: theme)
    sheet.theme = theme
    sheet._area = Gtk.DrawingArea()  # unrealized: only lends a Pango context
    sheet._sections = cheatsheet_sections(cfg)
    # The overlay's own sizing caps the height at the screen's and scrolls; a picture shows every row.
    context, metrics = sheet._area.get_pango_context(), _Metrics(1.0)
    plan = sheet._best_plan(sheet._measure(context, metrics), context, metrics, SCREEN[0] * 0.92, math.inf)
    assert plan is not None
    sheet._plan = plan
    width, height = plan.width + plan.m.shadow * 2, plan.height + plan.m.shadow * 2
    surface, cr = canvas(width, height)
    backdrop(cr, width, height, theme, sheet.backdrop_alpha)
    sheet._draw(cr, round(width), round(height))
    return surface


# the bar widget ---------------------------------------------------------------------------------------


def _darker(rgb: tuple[float, float, float], factor: float = 1.4) -> tuple[float, float, float]:
    """Roughly Qt.darker, which the widget's secondary text uses."""
    return rgb[0] / factor, rgb[1] / factor, rgb[2] / factor


def _text(
    cr: cairo.Context, theme: Theme, text: str, x: float, middle: float, size: float,
    rgba: tuple[float, float, float, float], bold: bool = False, spacing: float = 0.0, right: bool = False,
) -> float:
    """One line, vertically centred on `middle`, from `x` (or ending at it): its width."""
    layout = PangoCairo.create_layout(cr)
    font = Pango.FontDescription.from_string(theme.font)
    font.set_absolute_size(size * Pango.SCALE)
    font.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
    layout.set_font_description(font)
    if spacing:
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_letter_spacing_new(int(spacing * Pango.SCALE)))
        layout.set_attributes(attrs)
    layout.set_text(text, -1)
    _ink, logical = layout.get_pixel_extents()
    width = logical.width
    cr.set_source_rgba(*rgba)
    cr.move_to(x - width if right else x, middle - logical.height / 2)
    PangoCairo.show_layout(cr, layout)
    return width


class BarWidget:
    """plugin/BarWidget.qml redrawn: battery, then each machine's agents counted by status, and the panel."""

    def __init__(self, theme: Theme, cfg: config.Config, battery: int = 75) -> None:
        self.theme = theme
        self.battery = battery
        self.machines = machines(agents())
        lights = cfg.section("lights")
        self.alert = {status: tuple(c / 255 for c in lights[status]) for status in ("blocked", "done")}

    def color(self, status: str, base: tuple[float, float, float]) -> tuple[float, float, float, float]:
        if status in self.alert:
            return (*self.alert[status], 1.0)
        return (*base, 1.0 if status == "working" else 0.4)

    @staticmethod
    def counts(machine: dict) -> list[tuple[str, int]]:
        return [(status, n) for status, n in machine["counts"].items() if n]

    def strip(self, cr: cairo.Context, x: float) -> float:
        """The widget in the bar, from `x`: its right edge."""
        fg, middle = self.theme.foreground, BAR_HEIGHT / 2
        x += 8
        x += _text(cr, self.theme, PS_LOGO, x, middle, 13, (*fg, 1.0)) + 4
        x += _text(cr, self.theme, f"{self.battery}%", x, middle, 12, (*fg, 1.0))
        for machine in self.machines:
            x += 10
            x += _text(cr, self.theme, machine["name"].split(".")[0], x, middle, 10, (*fg, 0.55)) + 4
            for status, n in self.counts(machine):
                x += _text(cr, self.theme, str(n), x, middle, 12, self.color(status, fg), bold=status in self.alert) + 4
            x -= 4
        return x + 8

    def panel(self, cr: cairo.Context, x: float, y: float) -> float:
        """The panel under the widget, its top-left at (x, y): its height."""
        theme, fg = self.theme, self.theme.foreground
        pad, inner = 14, PANEL_WIDTH - 28
        rows = sum(1 + len(m["agents"]) for m in self.machines)
        height = pad * 2 + 52 + 12 + 6 + 12 + 1 + 12 + 18 + rows * 28 + (len(self.machines) - 1) * 10 + 12 + 30
        cr.set_source_rgb(*theme.background)
        cr.rectangle(x, y, PANEL_WIDTH, height)
        cr.fill()
        cr.set_source_rgb(*theme.accent)  # Omarchy's popups border: the accent
        cr.set_line_width(2)
        cr.rectangle(x + 1, y + 1, PANEL_WIDTH - 2, height - 2)
        cr.stroke()
        left, top = x + pad, y + pad
        # the controller: logo, name and link, battery
        middle = top + 26
        _text(cr, theme, PS_LOGO, left, middle, 24, (*fg, 1.0))
        _text(cr, theme, "DualSense", left + 40, middle - 9, 14, (*fg, 1.0), bold=True)
        _text(cr, theme, "BLUETOOTH", left + 40, middle + 11, 10, (*_darker(fg), 1.0), bold=True, spacing=1.2)
        _text(cr, theme, f"{self.battery}%", left + inner, middle, 28, (*fg, 1.0), bold=True, right=True)
        top += 52 + 12
        cr.set_source_rgba(*fg, 0.12)
        cr.rectangle(left, top, inner, 6)
        cr.fill()
        cr.set_source_rgb(*theme.accent)
        cr.rectangle(left, top, inner * self.battery / 100, 6)
        cr.fill()
        top += 6 + 12
        cr.set_source_rgba(*fg, 0.12)
        cr.rectangle(left, top, inner, 1)
        cr.fill()
        top += 1 + 12
        _text(cr, theme, "AGENTS", left, top + 6, 10, (*_darker(fg), 1.0), bold=True)
        top += 18
        for index, machine in enumerate(self.machines):
            if index:
                top += 10
            hx = left + _text(cr, theme, machine["name"], left, top + 14, 11, (*fg, 1.0), bold=True) + 6
            for status, n in self.counts(machine):
                hx += _text(cr, theme, str(n), hx, top + 14, 11, self.color(status, fg), bold=True) + 6
            top += 28
            for agent in machine["agents"]:
                middle, status = top + 14, agent["status"]
                cr.set_source_rgba(*self.color(status, fg))
                cr.arc(left + 11, middle, 4 if status in self.alert else 3, 0, math.tau)
                cr.fill()
                px = left + 24 + _text(cr, theme, agent["project"], left + 24, middle, 12, (*fg, 1.0)) + 8
                badge = _text(cr, theme, agent["badge"], left + inner - 8, middle, 10, self.color(status, fg),
                              bold=True, spacing=1.0, right=True)
                cr.save()
                cr.rectangle(px, top, left + inner - 16 - badge - px, 28)
                cr.clip()
                _text(cr, theme, agent["caption"], px, middle, 11, (*_darker(fg), 1.0))
                cr.restore()
                top += 28
        top += 12
        bx = left
        for icon, label in (("\U000f06a9", "Agents"), (PAUSE, "Pause"), ("\U000f030c", "Cheat sheet")):
            width = 10 + _text(cr, theme, icon, bx + 10, top + 15, 14, (*fg, 1.0)) + 8
            width += _text(cr, theme, label, bx + width, top + 15, 11, (*fg, 1.0)) + 10
            cr.set_source_rgba(*fg, 0.3)
            cr.set_line_width(1)
            cr.rectangle(bx + 0.5, top + 0.5, width - 1, 29)
            cr.stroke()
            bx += width + 6
        return height


def _bar(cr: cairo.Context, theme: Theme, width: float) -> None:
    cr.set_source_rgb(*theme.background)
    cr.rectangle(0, 0, width, BAR_HEIGHT)
    cr.fill()


def bar_picture(theme: Theme, cfg: config.Config) -> cairo.ImageSurface:
    """The widget in a strip of bar with its panel open."""
    widget = BarWidget(theme, cfg)
    probe, _ = canvas(1, 1)
    width = 2 * 24 + max(PANEL_WIDTH, widget.strip(cairo.Context(probe), 0))
    height = BAR_HEIGHT + 8 + widget.panel(cairo.Context(probe), 0, 0) + 24
    surface, cr = canvas(width, height)
    backdrop(cr, width, height, theme, (0.0, 0.0))
    _bar(cr, theme, width)
    widget.strip(cr, 24 - 8)
    widget.panel(cr, 24, BAR_HEIGHT + 8)
    return surface


def preview(theme: Theme, cfg: config.Config, agents_ring: cairo.ImageSurface) -> cairo.ImageSurface:
    """The marketplace card: the bar widget and its panel, and the agents ring the pad opens."""
    width, height = 1280.0, 800.0
    surface, cr = canvas(width, height)
    backdrop(cr, width, height, theme, (0.25, 0.55))
    _bar(cr, theme, width)
    widget = BarWidget(theme, cfg)
    widget.strip(cr, 24 - 8)
    widget.panel(cr, 24, BAR_HEIGHT + 8)
    ring_w, ring_h = agents_ring.get_width() / SCALE, agents_ring.get_height() / SCALE
    area_x, area_y = 24 + PANEL_WIDTH + 24, BAR_HEIGHT + 8
    scale = min((width - area_x) / ring_w, (height - area_y) / ring_h)
    cr.save()
    cr.translate(area_x + (width - area_x - ring_w * scale) / 2, area_y + (height - area_y - ring_h * scale) / 2)
    cr.scale(scale, scale)
    cr.set_source_surface(agents_ring, 0, 0)
    cr.paint()
    cr.restore()
    return surface


def named_theme(name: str) -> Theme:
    """An installed Omarchy theme, not the current one."""
    for themes in ("~/.config/omarchy/themes", "/usr/share/omarchy/themes"):
        path = Path(themes).expanduser() / name / "colors.toml"
        if path.is_file():
            with path.open("rb") as handle:
                return _with_shell(_theme_from(tomllib.load(handle)), path.with_name("shell.toml"), load_theme().font)
    raise SystemExit(f"no Omarchy theme named {name!r}")


def main() -> int:
    default = os.path.join("docs", "images")
    parser = argparse.ArgumentParser(prog="python -m tools.screenshots", description="Draw the README pictures.")
    parser.add_argument("out", nargs="?", default=default)
    parser.add_argument("--theme", help="an installed Omarchy theme to draw in (default: the current one)")
    args = parser.parse_args()
    out = args.out
    root = "." if out == default else out  # the marketplace's preview.png sits beside manifest.json
    os.makedirs(out, exist_ok=True)
    Gtk.init()
    theme = named_theme(args.theme) if args.theme else load_theme()
    cfg = config.load(os.devnull)  # the shipped defaults (an empty user file), not this machine's config
    agents_items = [s.item for s in agent_slices(agents(), cfg.section("lights"))]
    agents_legend = [("✕", "Go to agent"), ("△", "Hold: close agent"), ("○", "Close")]
    pictures = {
        os.path.join(out, "favorites.png"): ring(theme, "Omarchy", FAVORITES, 3, [
            ("✕", "Open"), ("□", "Hold: put Files here"), ("△", "Hold: remove"), ("L1 R1", "Move"), ("○", "Close"),
        ]),
        os.path.join(out, "hold.png"): ring(theme, "Omarchy", FAVORITES, 5, [
            ("✕", "Pick an app"), ("□", "Hold: put Files here"), ("○", "Close"),
        ], hold=("□", "Pin here", "Files", 0.62)),
        os.path.join(out, "agents.png"): ring(theme, "Agents", agents_items, 0, agents_legend),
        os.path.join(out, "carousel.png"): carousel(theme, cfg),
        os.path.join(out, "cheatsheet.png"): cheat_sheet(theme, cfg),
        os.path.join(out, "bar.png"): bar_picture(theme, cfg),
        os.path.join(root, "preview.png"): preview(
            theme, cfg, ring(theme, "Agents", agents_items, 0, agents_legend, desktop=False),
        ),
    }
    for path, surface in pictures.items():
        surface.write_to_png(path)
        print(f"{path}  {surface.get_width()}×{surface.get_height()}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
