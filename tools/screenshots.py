"""Render the README pictures: `python -m tools.screenshots [out_dir]` (default docs/images).

Draws the real overlays (radial menu, cheat sheet) offscreen into PNGs, over a plain backdrop and with
made-up agents and apps, so no personal windows or names end up in the docs. Run it in a desktop
session: app icons come from the GTK icon theme (missing ones are left out) and colours from the
current Omarchy theme.
"""

from __future__ import annotations

import math
import os
import sys
import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ps_oneshot import config  # noqa: E402
from ps_oneshot.agentcarousel import AgentCarousel, CarouselHooks  # noqa: E402
from ps_oneshot.herd import Agent  # noqa: E402
from ps_oneshot.labels import cheatsheet_sections  # noqa: E402
from ps_oneshot.menus import ICON_ADD, INLINE_COLOR, agent_slices  # noqa: E402
from ps_oneshot.ui.carousel import Carousel  # noqa: E402
from ps_oneshot.ui.cheatsheet import CheatSheet, _Metrics  # noqa: E402
from ps_oneshot.ui.radial import RadialItem, RadialMenu, _Hold, draw_halo  # noqa: E402
from ps_oneshot.ui.theme import Theme, load_theme  # noqa: E402

SCALE = 1.5  # pixels per logical pixel: sharp in GitHub's README column
SCREEN = (1920, 1080)  # the logical monitor the overlays size themselves for
BATTERY = (75, False)  # the radial's corner panel: percent, charging

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
    hold: tuple[str, str, str, float] | None = None,
) -> cairo.ImageSurface:
    menu = RadialMenu(lambda: theme)
    menu.theme = theme
    menu._area = Gtk.DrawingArea()  # unrealized: only lends the icon scale
    width, height = menu._content_size(*SCREEN)
    menu._title = title
    menu._set_items(items)
    menu._selection = selection
    menu._pop = [1.0 if index == selection else 0.0 for index in range(len(items))]
    menu._legend = legend
    menu._battery = BATTERY
    if hold is not None:
        glyph, verb, label, progress = hold  # progress of a one-second hold
        menu._hold = _Hold(glyph, verb, label, time.monotonic() - progress, 1.0)
    surface, cr = canvas(width, height)
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


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join("docs", "images")
    os.makedirs(out, exist_ok=True)
    Gtk.init()
    theme = load_theme()
    cfg = config.load(os.devnull)  # the shipped defaults (an empty user file), not this machine's config
    pictures = {
        "favorites.png": ring(theme, "Omarchy", FAVORITES, 3, [
            ("✕", "Open"), ("□", "Hold: put Files here"), ("△", "Hold: remove"), ("L1 R1", "Move"), ("○", "Close"),
        ]),
        "hold.png": ring(theme, "Omarchy", FAVORITES, 5, [
            ("✕", "Pick an app"), ("□", "Hold: put Files here"), ("○", "Close"),
        ], hold=("□", "Pin here", "Files", 0.62)),
        "agents.png": ring(
            theme, "Agents", [s.item for s in agent_slices(agents(), cfg.section("lights"))], 0,
            [("✕", "Go to agent"), ("△", "Hold: close agent"), ("○", "Close")],
        ),
        "carousel.png": carousel(theme, cfg),
        "cheatsheet.png": cheat_sheet(theme, cfg),
    }
    for name, surface in pictures.items():
        path = os.path.join(out, name)
        surface.write_to_png(path)
        print(f"{path}  {surface.get_width()}×{surface.get_height()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
