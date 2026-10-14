"""Shared plumbing for the full-screen, click-through, focus-less layer-shell overlays."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable

import cairo
from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell, Pango, PangoCairo

from ps_oneshot.ui.theme import RGB, Theme

log = logging.getLogger(__name__)

NAMESPACE = "ps-oneshot"
OPEN_SECONDS = 0.12
# PlayStation face-button glyphs in their classic colours.
GLYPH_COLORS = {"✕": "#7CB2E8", "○": "#F2717F", "△": "#45D6B4", "□": "#E68FD2"}


def ease_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1.0 - (1.0 - t) ** 3


def mix(a: RGB, b: RGB, t: float) -> RGB:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def css_rgba(rgb: RGB, alpha: float) -> str:
    r, g, b = (round(c * 255) for c in rgb)
    return f"rgba({r},{g},{b},{alpha:.3f})"


def hex_rgb(color: str) -> RGB:
    value = color.lstrip("#")
    return int(value[0:2], 16) / 255, int(value[2:4], 16) / 255, int(value[4:6], 16) / 255


def glyph_markup(text: str, size: float) -> str:
    """Enlarge and colour face-button glyphs; lower them so they centre on the surrounding text."""
    rise = -int(size * 0.12 * Pango.SCALE)
    out = []
    for char in text:
        escaped = GLib.markup_escape_text(char)
        color = GLYPH_COLORS.get(char)
        out.append(f'<span foreground="{color}" size="150%" rise="{rise}">{escaped}</span>' if color else escaped)
    return "".join(out)


def find_monitor(connector: str | None) -> Gdk.Monitor | None:
    display = Gdk.Display.get_default()
    if display is None:
        return None
    monitors = display.get_monitors()
    first: Gdk.Monitor | None = None
    for index in range(monitors.get_n_items()):
        monitor = monitors.get_item(index)
        if first is None:
            first = monitor
        if connector is not None and monitor.get_connector() == connector:
            return monitor
    if connector is not None:
        log.debug("monitor %s not found, using the first one", connector)
    return first


def make_layout(
    context: Pango.Context,
    theme: Theme,
    text: str,
    size: float,
    *,
    weight: Pango.Weight = Pango.Weight.NORMAL,
    width: float | None = None,
    lines: int = 1,
    markup: bool = False,
    align: Pango.Alignment = Pango.Alignment.CENTER,
) -> Pango.Layout:
    """`size` is in pixels; `width` wraps, `lines` > 0 caps it at that many lines with an ellipsis."""
    layout = Pango.Layout.new(context)
    font = Pango.FontDescription.from_string(theme.font)
    font.set_absolute_size(size * Pango.SCALE)
    font.set_weight(weight)
    layout.set_font_description(font)
    layout.set_alignment(align)
    if width is not None:
        layout.set_width(max(1, int(width * Pango.SCALE)))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        if lines > 0:
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            layout.set_height(-lines)
    if markup:
        layout.set_markup(text, -1)
    else:
        layout.set_text(text, -1)
    return layout


def layout_size(layout: Pango.Layout) -> tuple[float, float]:
    _ink, logical = layout.get_pixel_extents()
    return logical.width, logical.height


def draw_layout(
    cr: cairo.Context,
    layout: Pango.Layout,
    x: float,
    y: float,
    rgb: RGB,
    alpha: float = 1.0,
    *,
    centered: bool = True,
) -> None:
    """Draw with the logical box centred on (x, y), or with its top-left at (x, y)."""
    _ink, logical = layout.get_pixel_extents()
    if centered:
        x -= logical.x + logical.width / 2
        y -= logical.y + logical.height / 2
    else:
        x -= logical.x
        y -= logical.y
    cr.set_source_rgba(*rgb, alpha)
    cr.move_to(x, y)
    PangoCairo.update_layout(cr, layout)
    PangoCairo.show_layout(cr, layout)


def rounded_rect(cr: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class Overlay:
    """One centred `Gtk.DrawingArea` over a dimmed backdrop on a full-screen OVERLAY layer surface.

    The surface has keyboard mode NONE and an empty input region, so it never takes focus or clicks.
    The dim backdrop is CSS (GPU-composited); only the centred area is painted with cairo.
    Subclasses implement `_draw`, `_content_size` and optionally `_animate` / `_on_hidden`.
    """

    backdrop_alpha = (0.62, 0.38)  # centre, edges

    def __init__(self, theme_loader: Callable[[], Theme], name: str) -> None:
        self._theme_loader = theme_loader
        self._name = name
        self.theme = theme_loader()
        self.open_progress = 1.0
        self._window: Gtk.Window | None = None
        self._area: Gtk.DrawingArea | None = None
        self._content: Gtk.Widget | None = None
        self._css = Gtk.CssProvider()
        self._connector: str | None = None
        self._tick_id = 0
        self._open_start: float | None = None
        self._last_frame: float | None = None

    @property
    def visible(self) -> bool:
        return self._window is not None and self._window.get_visible()

    def hide(self) -> None:
        if self._window is None or not self._window.get_visible():
            return
        self._stop_ticking()
        self._on_hidden()
        self._window.set_visible(False)

    def _present(self, monitor: str | None) -> None:
        self.theme = self._theme_loader()
        if self._window is None:
            self._build()
        assert self._window is not None and self._area is not None and self._content is not None
        was_visible = self._window.get_visible()
        if was_visible and monitor != self._connector:
            # A mapped layer surface cannot move to another output; remap it.
            self._window.set_visible(False)
            was_visible = False
        self._connector = monitor
        gdk_monitor = find_monitor(monitor)
        if gdk_monitor is not None:
            Gtk4LayerShell.set_monitor(self._window, gdk_monitor)
            geometry = gdk_monitor.get_geometry()
            width, height = self._content_size(geometry.width, geometry.height)
        else:
            width, height = self._content_size(1920, 1080)
        self._area.set_content_width(math.ceil(width))
        self._area.set_content_height(math.ceil(height))
        centre, edge = self.backdrop_alpha
        bg = self.theme.background
        self._css.load_from_string(
            f"window.{NAMESPACE}-{self._name} {{ background: transparent; }}\n"
            f".{NAMESPACE}-{self._name}-backdrop {{ background-image: radial-gradient("
            f"circle farthest-corner at center, {css_rgba(bg, centre)}, {css_rgba(bg, edge)}); }}\n"
        )
        if not was_visible:
            self.open_progress = 0.0
            self._open_start = None
            self._content.set_opacity(0.0)
            self._window.set_visible(True)
            self._start_ticking()
        self._area.queue_draw()

    def _build(self) -> None:
        window = Gtk.Window()
        window.set_decorated(False)
        window.add_css_class(f"{NAMESPACE}-{self._name}")
        Gtk4LayerShell.init_for_window(window)
        Gtk4LayerShell.set_namespace(window, NAMESPACE)
        Gtk4LayerShell.set_layer(window, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_keyboard_mode(window, Gtk4LayerShell.KeyboardMode.NONE)
        for edge in (
            Gtk4LayerShell.Edge.TOP,
            Gtk4LayerShell.Edge.BOTTOM,
            Gtk4LayerShell.Edge.LEFT,
            Gtk4LayerShell.Edge.RIGHT,
        ):
            Gtk4LayerShell.set_anchor(window, edge, True)
        Gtk4LayerShell.set_exclusive_zone(window, -1)
        Gtk.StyleContext.add_provider_for_display(
            window.get_display(), self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        backdrop = Gtk.Box(hexpand=True, vexpand=True)
        backdrop.add_css_class(f"{NAMESPACE}-{self._name}-backdrop")
        area = Gtk.DrawingArea(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        area.set_draw_func(self._on_draw)
        content = Gtk.Overlay()
        content.set_child(backdrop)
        content.add_overlay(area)
        window.set_child(content)
        window.connect("realize", _make_click_through)

        self._window, self._area, self._content = window, area, content

    def _queue_draw(self) -> None:
        if self._area is not None:
            self._area.queue_draw()

    def _start_ticking(self) -> None:
        if self._area is not None and self._tick_id == 0 and self.visible:
            self._last_frame = None
            self._tick_id = self._area.add_tick_callback(self._on_tick)

    def _stop_ticking(self) -> None:
        if self._tick_id and self._area is not None:
            self._area.remove_tick_callback(self._tick_id)
        self._tick_id = 0

    def _on_tick(self, area: Gtk.DrawingArea, clock: Gdk.FrameClock) -> bool:
        now = clock.get_frame_time() / 1_000_000
        dt = 0.0 if self._last_frame is None else now - self._last_frame
        self._last_frame = now
        busy = False
        if self.open_progress < 1.0:
            if self._open_start is None:
                self._open_start = now
            self.open_progress = min(1.0, (now - self._open_start) / OPEN_SECONDS)
            if self._content is not None:
                self._content.set_opacity(ease_out(self.open_progress))
            busy = self.open_progress < 1.0
        busy = self._animate(dt) or busy
        area.queue_draw()
        if busy:
            return GLib.SOURCE_CONTINUE
        self._tick_id = 0
        return GLib.SOURCE_REMOVE

    def _on_draw(self, _area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int) -> None:
        self._draw(cr, width, height)

    def _content_size(self, monitor_width: int, monitor_height: int) -> tuple[float, float]:
        raise NotImplementedError

    def _draw(self, cr: cairo.Context, width: int, height: int) -> None:
        raise NotImplementedError

    def _animate(self, dt: float) -> bool:
        """Advance subclass animations by `dt` seconds; return True while still animating."""
        return False

    def _on_hidden(self) -> None:
        pass


def _make_click_through(window: Gtk.Window) -> None:
    surface = window.get_surface()
    if surface is not None:
        surface.set_input_region(cairo.Region())
