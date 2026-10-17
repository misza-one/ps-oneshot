"""The agent carousel overlay: Omarchy's image picker (the theme switcher) redrawn by us, so its dots can blink.

One expanded card in the middle and skewed strips to both sides, with the picker's geometry and its colours
from the theme's shell.toml ([image-picker]: scrim, selected and unselected borders). Unselected strips are
dimmed the way Omarchy dims them. Every item has a status dot: on its strip, on the card, and in a row under
the carousel; dots marked `blink` blink. Cards and strips are painted once into surfaces and only composited
per frame, and frames come only on a change or a blink.

The overlay holds the keyboard while open; `on_key(name, char, down, repeat)` gets every key.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import cairo
from gi.repository import Gdk, GLib, Pango, PangoCairo

from ps_oneshot.ui.overlay import Overlay, draw_layout, make_layout, rounded_rect
from ps_oneshot.ui.theme import RGB, RGBA, Theme

# Omarchy's image picker, in logical px.
EXPANDED_W, EXPANDED_H = 768, 475
SLICE_W, SLICE_H = 108, 432
SLICE_STEP = SLICE_W - 30  # its sliceSpacing is -30: strips overlap by their skew
SKEW = 28
SLICE_DIM = 0.42  # unselected strips under the theme background at this alpha
MAX_STRIPS = 16  # per side
TOP_GAP = 30
CHROME = 64  # under the carousel: the dot row and the filter text
SIDE_MARGIN = 80  # the picker leaves 40 px of screen on each side
BLINK_MS = 520
DOT_ROW_SPACING = 24
STRIP_DOT_FROM_BOTTOM = 24


@dataclass(frozen=True)
class CarouselItem:
    key: str  # changes whenever the card or strip would look different (their surfaces are cached by it)
    paint_card: Callable[[cairo.Context], None]  # EXPANDED_W x EXPANDED_H
    paint_strip: Callable[[cairo.Context], None]  # SLICE_W x SLICE_H, before the skew mask
    card_dot: tuple[float, float]  # where the card's status dot goes, logical px within the card
    dot: RGBA
    dot_radius: float
    blink: bool = False


class Carousel(Overlay):
    takes_keyboard = True

    def __init__(self, theme_loader: Callable[[], Theme]) -> None:
        super().__init__(theme_loader, "carousel")
        self._items: list[CarouselItem] = []
        self._selection = 0
        self._filter = ""
        self._empty_text = ""
        self._cards: dict[str, cairo.ImageSurface] = {}
        self._strips: dict[str, cairo.ImageSurface] = {}
        self._painted_theme: Theme | None = None
        self._blink_on = True
        self._blink_id = 0
        self.on_key: Callable[[str, str, bool, bool], None] | None = None

    # api -----------------------------------------------------------------------------------------------

    def show(self, items: list[CarouselItem], selection: int, monitor: str | None, filter_text: str = "",
             empty_text: str = "") -> None:
        self._set(items, selection, filter_text, empty_text)
        self._present(monitor)
        if self.theme != self._painted_theme:  # _present reloads the theme
            self._cards.clear()
            self._strips.clear()
            self._painted_theme = self.theme
        self._sync_blink()

    def update(self, items: list[CarouselItem], selection: int, filter_text: str = "", empty_text: str = "") -> None:
        self._set(items, selection, filter_text, empty_text)
        self._sync_blink()
        self._queue_draw()

    def _set(self, items: list[CarouselItem], selection: int, filter_text: str, empty_text: str) -> None:
        self._items = list(items)
        self._selection = min(max(selection, 0), max(len(items) - 1, 0))
        self._filter, self._empty_text = filter_text, empty_text
        keys = {item.key for item in items}
        for cache in (self._cards, self._strips):
            for key in [k for k in cache if k not in keys]:
                del cache[key]

    # blinking ------------------------------------------------------------------------------------------

    def _sync_blink(self) -> None:
        wanted = self.visible and any(item.blink for item in self._items)
        if wanted and not self._blink_id:
            self._blink_on = True
            self._blink_id = GLib.timeout_add(BLINK_MS, self._on_blink)
        elif not wanted and self._blink_id:
            GLib.source_remove(self._blink_id)
            self._blink_id = 0
            self._blink_on = True

    def _on_blink(self) -> bool:
        self._blink_on = not self._blink_on
        self._queue_draw()
        return GLib.SOURCE_CONTINUE

    def _on_hidden(self) -> None:
        if self._blink_id:
            GLib.source_remove(self._blink_id)
            self._blink_id = 0

    # keyboard ------------------------------------------------------------------------------------------

    def _on_key(self, name: str, down: bool, repeat: bool) -> None:
        if self.on_key is None:
            return
        codepoint = Gdk.keyval_to_unicode(Gdk.keyval_from_name(name)) if name else 0
        char = chr(codepoint) if codepoint >= 32 and codepoint != 127 else ""
        self.on_key(name, char, down, repeat)

    # geometry ------------------------------------------------------------------------------------------

    def _backdrop(self) -> tuple[RGB, float, float]:
        r, g, b, a = self.theme.picker_scrim
        return (r, g, b), a, a  # Omarchy's picker scrim is flat

    def _content_size(self, monitor_width: int, monitor_height: int) -> tuple[float, float]:
        width = min(monitor_width - SIDE_MARGIN, EXPANDED_W + 13 * SLICE_STEP + 40)
        return width, TOP_GAP + EXPANDED_H + CHROME

    # drawing -------------------------------------------------------------------------------------------

    def _surface(self, cache: dict[str, cairo.ImageSurface], key: str, width: float, height: float,
                 paint: Callable[[cairo.Context], None]) -> cairo.ImageSurface:
        surface = cache.get(key)
        if surface is None:
            scale = self._area.get_scale_factor() if self._area is not None else 1
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, math.ceil(width * scale), math.ceil(height * scale))
            surface.set_device_scale(scale, scale)
            paint(cairo.Context(surface))
            cache[key] = surface
        return surface

    def _draw(self, cr: cairo.Context, width: int, height: int) -> None:
        theme = self.theme
        preview_x = (width - EXPANDED_W) / 2
        if not self._items:
            if self._empty_text:
                layout = make_layout(PangoCairo.create_context(cr), theme, self._empty_text, 24,
                                     weight=Pango.Weight.SEMIBOLD)
                draw_layout(cr, layout, width / 2, TOP_GAP + EXPANDED_H / 2, theme.foreground, 0.85)
            self._draw_filter(cr, width)
            return
        sel = self._selection
        # Farthest strips first, so nearer ones (and the card) overlap them, as in Omarchy's z order.
        order = sorted((i for i in range(len(self._items)) if i != sel and abs(i - sel) <= MAX_STRIPS),
                       key=lambda i: -abs(i - sel))
        strip_y = TOP_GAP + (EXPANDED_H - SLICE_H) / 2
        for i in order:
            rel = i - sel
            x = preview_x + rel * SLICE_STEP if rel < 0 else preview_x + EXPANDED_W - 30 + (rel - 1) * SLICE_STEP
            if x + SLICE_W < 0 or x > width:
                continue
            item = self._items[i]
            surface = self._surface(self._strips, item.key, SLICE_W, SLICE_H, item.paint_strip)
            self._draw_slice(cr, surface, x, strip_y, SLICE_W, SLICE_H, selected=False)
            dot_y = strip_y + SLICE_H - STRIP_DOT_FROM_BOTTOM
            dot_x = x + (SLICE_W + SKEW) / 2 - SKEW * (dot_y - strip_y) / SLICE_H
            self._draw_dot(cr, item, dot_x, dot_y, 1.0)
        item = self._items[sel]
        surface = self._surface(self._cards, item.key, EXPANDED_W, EXPANDED_H, item.paint_card)
        self._draw_slice(cr, surface, preview_x, TOP_GAP, EXPANDED_W, EXPANDED_H, selected=True)
        self._draw_dot(cr, item, preview_x + item.card_dot[0], TOP_GAP + item.card_dot[1], 1.25)
        self._draw_dot_row(cr, width)
        self._draw_filter(cr, width)

    def _draw_slice(self, cr: cairo.Context, surface: cairo.ImageSurface, x: float, y: float, w: float, h: float,
                    selected: bool) -> None:
        theme = self.theme
        cr.save()
        _parallelogram(cr, x, y, w, h)
        cr.clip()
        cr.set_source_surface(surface, x, y)
        cr.paint()
        if not selected:
            cr.set_source_rgba(*theme.background, SLICE_DIM)
            cr.paint()
        cr.restore()
        _parallelogram(cr, x, y, w, h)
        cr.set_source_rgba(*(theme.picker_selected if selected else theme.picker_unselected))
        cr.set_line_width(3 if selected else 1)
        cr.stroke()

    def _draw_dot(self, cr: cairo.Context, item: CarouselItem, x: float, y: float, scale: float,
                  halo: float = 2.2) -> None:
        r, g, b, alpha = item.dot
        radius = item.dot_radius * scale
        if item.blink and not self._blink_on:
            alpha *= 0.22
        elif item.blink:
            cr.arc(x, y, radius * halo, 0, 2 * math.pi)  # a soft halo while lit
            cr.set_source_rgba(r, g, b, 0.26)
            cr.fill()
        cr.arc(x, y, radius, 0, 2 * math.pi)
        cr.set_source_rgba(r, g, b, alpha)
        cr.fill()

    def _draw_dot_row(self, cr: cairo.Context, width: int) -> None:
        """Every agent as a dot, in carousel order, the selected one ringed: the whole herd at a glance, even
        the strips that don't fit on screen."""
        count = len(self._items)
        y = TOP_GAP + EXPANDED_H + 24
        x0 = width / 2 - (count - 1) * DOT_ROW_SPACING / 2
        # On a pill of the theme's background, so the row reads over whatever the desktop shows.
        pad = DOT_ROW_SPACING * 0.75
        rounded_rect(cr, x0 - pad, y - 12, (count - 1) * DOT_ROW_SPACING + 2 * pad, 24, 12)
        cr.set_source_rgba(*self.theme.background, 0.78)
        cr.fill_preserve()
        cr.set_source_rgba(*self.theme.picker_unselected)
        cr.set_line_width(1)
        cr.stroke()
        for i, item in enumerate(self._items):
            x = x0 + i * DOT_ROW_SPACING
            self._draw_dot(cr, item, x, y, 0.85, halo=1.8)
            if i == self._selection:
                cr.arc(x, y, item.dot_radius * 0.85 + 4.5, 0, 2 * math.pi)
                cr.set_source_rgba(*self.theme.picker_selected)
                cr.set_line_width(1.5)
                cr.stroke()

    def _draw_filter(self, cr: cairo.Context, width: int) -> None:
        if not self._filter:
            return
        layout = make_layout(PangoCairo.create_context(cr), self.theme, self._filter, 14, width=EXPANDED_W)
        lw, lh = layout.get_pixel_size()
        y = TOP_GAP + EXPANDED_H + 50
        rounded_rect(cr, width / 2 - lw / 2 - 10, y - lh / 2 - 3, lw + 20, lh + 6, (lh + 6) / 2)
        cr.set_source_rgba(*self.theme.background, 0.78)
        cr.fill()
        draw_layout(cr, layout, width / 2, y, self.theme.foreground, 0.9)


def _parallelogram(cr: cairo.Context, x: float, y: float, w: float, h: float) -> None:
    """Omarchy's slice shape: the top edge shifted SKEW px right of the bottom one."""
    cr.new_path()
    cr.move_to(x + SKEW, y)
    cr.line_to(x + w, y)
    cr.line_to(x + w - SKEW, y + h)
    cr.line_to(x, y + h)
    cr.close_path()
