"""Radial (pie) menu overlay: a donut of up to 12 slices picked with a stick."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import cairo
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, PangoCairo

from ps_oneshot.ui.overlay import (
    GLYPH_COLORS,
    Overlay,
    draw_layout,
    ease_out,
    glyph_markup,
    hex_rgb,
    layout_size,
    make_layout,
    mix,
    rounded_rect,
)
from ps_oneshot.ui.theme import RGB, Theme

log = logging.getLogger(__name__)

MAX_ITEMS = 12
PULSE_PERIOD = 1.9  # seconds per breath
PULSE_FRAME_MS = 40
POP_TIME = 0.05  # selection pop time constant, seconds
DIAMETER = 0.46  # of the shorter monitor side
TAU = 2 * math.pi
# Group arcs, in units of the donut radius: centre line just outside the rim, then the labels.
GROUP_ARC_RADIUS = 1.07
GROUP_ARC_WIDTH = 0.024
GROUP_LABEL_GAP = 0.035
GROUP_LABEL_MAX_WIDTH = 0.62
CHECK_GLYPHS = ("\uf00c", "✓")  # Nerd Font check, then a plain fallback
MARQUEE_PAUSE = 1.1  # seconds a name too long for its slice rests at each end
MARQUEE_SPEED = 2.2  # how fast it scrolls, in text heights per second
IMAGE_LOOKUP_PX = 128  # app icons are looked up once at this size (× the display scale) and scaled to fit
LEGEND_TEXT = 0.056  # legend text size, of the ring radius
LEGEND_MARGIN = 0.04  # legend distance from the corner of the drawing area, of the ring radius
# The battery panel in the opposite corner: Material Design battery glyphs by tenth of charge (outline for
# 0-9 %, 10 … 90, full), the same 10 % steps the controller reports in.
BATTERY_ICONS = ("\U000f008e", *(chr(0xF007A + tenth) for tenth in range(9)), "\U000f0079")
BATTERY_CHARGING = "\U000f0084"


def marquee_offset(overflow: float, speed: float, elapsed: float, pause: float = MARQUEE_PAUSE) -> float:
    """How far a name `overflow` px too wide has scrolled left after `elapsed` s: it rests, slides to
    its end, rests, slides back, and so on."""
    if overflow <= 0 or speed <= 0:
        return 0.0
    travel = overflow / speed
    t = elapsed % (2 * (pause + travel))
    if t < pause:
        return 0.0
    if t < pause + travel:
        return (t - pause) * speed
    if t < 2 * pause + travel:
        return overflow
    return overflow - (t - 2 * pause - travel) * speed


@dataclass(frozen=True)
class RadialItem:
    label: str
    icon: str = ""
    image: str = ""  # an app icon (Gio icon string), drawn instead of `icon` when the icon theme has it
    color: tuple[int, int, int] | None = None
    badge: str = ""
    badge_color: tuple[int, int, int] | None = None  # badge pill fill; defaults to the slice tint
    pulse: bool = False
    detail: str = ""
    sublabel: str = ""  # smaller muted second line
    group: str = ""  # adjacent items with the same group share an outer arc labelled with it
    group_color: tuple[int, int, int] | None = None
    active: bool = False  # the current choice: accent ring + check, quieter than the selection


@dataclass(frozen=True)
class GroupSpan:
    start: int
    length: int
    name: str


@dataclass(frozen=True)
class _Hold:
    """A button held to confirm something: the hub shows `title`/`label` and fills a ring in the glyph's colour."""

    glyph: str
    title: str
    label: str
    start: float  # monotonic seconds
    seconds: float


def slice_for_vector(x: float, y: float, count: int, deadzone: float) -> int | None:
    """x, y in -1..1 with +y down; slice 0 is centred at 12 o'clock and indices run clockwise."""
    if count <= 0:
        return None
    magnitude = math.hypot(x, y)
    if magnitude == 0.0 or magnitude < deadzone:
        return None
    angle = math.atan2(x, -y) % TAU  # 0 at the top, growing clockwise
    span = TAU / count
    return int((angle + span / 2) // span) % count


def group_spans(groups: Sequence[str]) -> list[GroupSpan]:
    """Runs of equal, non-empty group names. The menu is a circle, so a run that ends on the last
    item and one that starts on the first are merged (its `start` is then the later index)."""
    spans: list[GroupSpan] = []
    for index, name in enumerate(groups):
        if not name:
            continue
        last = spans[-1] if spans else None
        if last is not None and last.name == name and last.start + last.length == index:
            spans[-1] = GroupSpan(last.start, last.length + 1, name)
        else:
            spans.append(GroupSpan(index, 1, name))
    if len(spans) > 1:
        first, last = spans[0], spans[-1]
        if first.start == 0 and last.start + last.length == len(groups) and first.name == last.name:
            spans = [*spans[1:-1], GroupSpan(last.start, last.length + first.length, last.name)]
    return spans


@dataclass(frozen=True)
class _Ring:
    inner: float
    outer: float
    gap: float
    corner: float

    def popped_outer(self, pop: float) -> float:
        """Outer edge of a slice popped by `pop` (0..1), including its outward push."""
        return self.outer * (1 + 0.095 * pop)


class RadialMenu(Overlay):
    backdrop_alpha = (0.68, 0.44)

    def __init__(self, theme_loader: Callable[[], Theme]) -> None:
        super().__init__(theme_loader, "radial")
        self._title = ""
        self._items: list[RadialItem] = []
        self._groups: list[GroupSpan] = []
        self._selection: int | None = None
        self._pop: list[float] = []
        self._pulse_id = 0
        self._radius = 0.0
        self._marquee_since = 0.0  # when the selection last moved: its name scrolls from there
        self._marquee_running = False  # the selected slice's name is scrolling (keeps frames coming)
        self._images: dict[str, Gdk.Paintable | None] = {}
        self._legend: list[tuple[str, str]] = []  # (button, what it does) for the corner panel
        self._battery: tuple[int, bool] | None = None  # (percent, charging) for the bottom-left panel
        self._hold: _Hold | None = None

    def show(self, title: str, items: list[RadialItem], monitor: str | None) -> None:
        fresh = not self.visible
        self._title = title
        self._set_items(items)
        if fresh:
            self._selection = None
            self._pop = [0.0] * len(self._items)
        self._present(monitor)
        self._sync_pulse()

    def update_items(self, items: list[RadialItem]) -> None:
        self._set_items(items)
        self._sync_pulse()
        self._queue_draw()

    def set_selection(self, index: int | None) -> None:
        if index is not None and not 0 <= index < len(self._items):
            index = None
        if index == self._selection:
            return
        self._selection = index
        self._marquee_since = GLib.get_monotonic_time() / 1_000_000
        if self.visible:
            self._start_ticking()
        else:
            self._pop = [1.0 if i == index else 0.0 for i in range(len(self._items))]

    def set_legend(self, rows: list[tuple[str, str]]) -> None:
        """What the buttons do right now, listed in the bottom-right corner."""
        if rows != self._legend:
            self._legend = list(rows)
            self._queue_draw()

    def set_battery(self, level: tuple[int, bool] | None) -> None:
        """The controller's charge (percent, charging) for the bottom-left corner; None hides it."""
        if level != self._battery:
            self._battery = level
            self._queue_draw()

    def set_hold(self, glyph: str, title: str, label: str, seconds: float) -> None:
        """Show a hold in progress for `seconds`: the hub names it and a ring fills up."""
        self._hold = _Hold(glyph, title, label, GLib.get_monotonic_time() / 1_000_000, seconds)
        self._start_ticking()

    def clear_hold(self) -> None:
        if self._hold is not None:
            self._hold = None
            self._queue_draw()

    def _set_items(self, items: list[RadialItem]) -> None:
        if len(items) > MAX_ITEMS:
            log.warning("radial menu shows at most %d items, got %d", MAX_ITEMS, len(items))
        self._items = list(items[:MAX_ITEMS])
        self._groups = group_spans([item.group for item in self._items])
        self._pop = (self._pop + [0.0] * len(self._items))[: len(self._items)]
        if self._selection is not None and self._selection >= len(self._items):
            self._selection = None

    def _sync_pulse(self) -> None:
        wanted = self.visible and (self._marquee_running or any(item.pulse for item in self._items))
        if wanted and not self._pulse_id:
            self._pulse_id = GLib.timeout_add(PULSE_FRAME_MS, self._on_pulse)
        elif not wanted and self._pulse_id:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = 0

    def _on_pulse(self) -> bool:
        self._queue_draw()
        return GLib.SOURCE_CONTINUE

    def _on_hidden(self) -> None:
        self._hold = None
        if self._pulse_id:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = 0

    def _animate(self, dt: float) -> bool:
        step = 1.0 - math.exp(-dt / POP_TIME) if dt > 0 else 0.0
        busy = False
        for i, pop in enumerate(self._pop):
            target = 1.0 if i == self._selection else 0.0
            pop += (target - pop) * step
            if abs(target - pop) < 0.01:
                pop = target
            else:
                busy = True
            self._pop[i] = pop
        return busy or self._hold is not None  # the hold ring fills frame by frame

    def _content_size(self, monitor_width: int, monitor_height: int) -> tuple[float, float]:
        self._radius = DIAMETER * min(monitor_width, monitor_height) / 2
        # Room for the selection pop, badges, group arcs and their labels beside the donut.
        reach = GROUP_ARC_RADIUS + GROUP_ARC_WIDTH + GROUP_LABEL_GAP + GROUP_LABEL_MAX_WIDTH + 0.05
        side = self._radius * 2 * reach
        return side, side

    # -- drawing -------------------------------------------------------------------------------

    def _draw(self, cr: cairo.Context, width: int, height: int) -> None:
        theme = self.theme
        radius = self._radius
        opened = ease_out(self.open_progress)
        cr.translate(width / 2, height / 2)
        cr.scale(0.86 + 0.14 * opened, 0.86 + 0.14 * opened)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        context = PangoCairo.create_context(cr)

        ring = _Ring(
            inner=radius * 0.42,
            outer=radius,
            gap=max(3.0, radius * 0.024),
            corner=max(3.0, radius * 0.045),
        )
        now = GLib.get_monotonic_time() / 1_000_000
        breath = 0.5 - 0.5 * math.cos(TAU * now / PULSE_PERIOD)

        # Soft halo under the donut to lift it off whatever is behind.
        halo = cairo.RadialGradient(0, 0, radius * 0.8, 0, 0, radius * 1.3)
        halo.add_color_stop_rgba(0, *theme.background, 0.55)
        halo.add_color_stop_rgba(1, *theme.background, 0.0)
        cr.set_source(halo)
        cr.arc(0, 0, radius * 1.3, 0, TAU)
        cr.fill()

        count = len(self._items)
        for group in self._groups:
            self._draw_group_arc(cr, theme, ring, group, count)
        order = [i for i in range(count) if i != self._selection]
        if self._selection is not None:
            order.append(self._selection)  # selected slice paints over its neighbours
        self._marquee_running = False
        for index in order:
            self._draw_slice(cr, context, theme, ring, index, count, breath)
        for index in order:
            item = self._items[index]
            if item.active:
                self._draw_check(cr, context, theme, ring, index, count)
            if item.badge:
                self._draw_badge(cr, context, theme, ring, index, count)
        for group in self._groups:
            self._draw_group_label(cr, context, theme, ring, group, count)
        self._draw_centre(cr, context, theme, ring, count)
        self._draw_legend(cr, context, theme, ring, width, height)
        self._draw_battery(cr, context, theme, ring, width, height)
        self._sync_pulse()  # frames while a name scrolls

    def _tint(self, item: RadialItem, theme: Theme) -> RGB:
        return theme.accent if item.color is None else _rgb(item.color)

    def _group_color(self, group: GroupSpan, theme: Theme) -> RGB:
        color = self._items[group.start].group_color
        return theme.muted if color is None else _rgb(color)

    def _draw_slice(
        self,
        cr: cairo.Context,
        context: Pango.Context,
        theme: Theme,
        ring: _Ring,
        index: int,
        count: int,
        breath: float,
    ) -> None:
        item = self._items[index]
        pop = self._pop[index]
        tint = self._tint(item, theme)
        span = TAU / count
        mid = -math.pi / 2 + index * span
        glow = breath if item.pulse else 0.0
        push = ring.outer * 0.035 * pop
        outer = ring.outer * (1 + 0.06 * pop)

        def shape() -> None:
            if count == 1:
                cr.new_path()
                cr.arc(0, 0, outer, 0, TAU)
                cr.new_sub_path()
                cr.arc_negative(0, 0, ring.inner, TAU, 0)
            else:
                _sector_path(cr, mid - span / 2, mid + span / 2, ring.inner, outer, ring.gap, ring.corner)

        cr.save()
        cr.translate(math.cos(mid) * push, math.sin(mid) * push)
        shape()
        if pop > 0.0:
            # Soft outer glow behind the selected slice.
            cr.set_line_width(ring.corner * 1.8)
            cr.set_source_rgba(*tint, 0.14 * pop)
            cr.stroke_preserve()
        fill = mix(theme.surface, tint, 0.07 + 0.30 * pop + 0.09 * glow + (0.05 if item.active else 0.0))
        cr.set_source_rgba(*fill, 0.93 + 0.05 * pop)
        cr.fill_preserve()
        cr.set_line_width(1.2 + 0.8 * pop)
        cr.set_source_rgba(*tint, 0.10 + 0.55 * pop + 0.30 * glow)
        cr.stroke_preserve()

        # Everything below stays inside the rounded slice.
        cr.clip()
        band = ring.outer * (0.03 + 0.02 * pop)
        cr.arc(0, 0, outer - band / 2, mid - span / 2, mid + span / 2)
        cr.set_line_width(band)
        cr.set_source_rgba(*tint, 0.40 + 0.60 * max(pop, glow))
        cr.stroke()
        if item.active:
            # Inner half of a doubled stroke: a crisp ring hugging the slice edge.
            shape()
            cr.set_line_width(max(3.0, ring.outer * 0.02))
            cr.set_source_rgba(*theme.accent, 0.85)
            cr.stroke()

        self._draw_slice_text(cr, context, theme, ring, item, mid, span, outer, pop, tint, index == self._selection)
        cr.restore()

    def _draw_slice_text(
        self,
        cr: cairo.Context,
        context: Pango.Context,
        theme: Theme,
        ring: _Ring,
        item: RadialItem,
        mid: float,
        span: float,
        outer: float,
        pop: float,
        tint: RGB,
        selected: bool,
    ) -> None:
        centre = (ring.inner + outer) / 2
        thickness = outer - ring.inner
        chord = 2 * centre * math.sin(min(span, math.pi) / 2) - ring.gap
        max_width = max(10.0, min(chord * 0.86, thickness * 1.15))
        x, y = math.cos(mid) * centre, math.sin(mid) * centre

        label_size = ring.outer * (0.068 if len(self._items) <= 8 else 0.058)
        weight = Pango.Weight.BOLD if pop > 0.5 else Pango.Weight.MEDIUM
        # (text, size, colour, alpha, lines, bold)
        texts = [(item.label, label_size, theme.foreground, 0.72 + 0.28 * pop, 1 if item.sublabel else 2, True)]
        if item.sublabel:
            sub_rgb = mix(theme.muted, theme.foreground, 0.35 + 0.25 * pop)
            texts.append((item.sublabel, label_size * 0.8, sub_rgb, 0.9, 1, False))

        def lay_out(text: str, size: float, lines: int, bold: bool, width: float | None) -> Pango.Layout:
            font_weight = weight if bold else Pango.Weight.NORMAL
            return make_layout(context, theme, text, size, weight=font_weight, width=width, lines=lines)

        # On top: the app's icon, or the glyph (whose box carries extra line spacing: tighten it).
        image = self._image(item.image) if item.image else None
        image_size = ring.outer * (0.13 + 0.025 * pop)
        glyph = None
        if image is None and item.icon:
            glyph = make_layout(context, theme, item.icon, ring.outer * (0.15 + 0.025 * pop))
            if glyph.get_unknown_glyphs_count() > 0:
                glyph = None  # a font without the glyph would draw a tofu box; better nothing
        head = image_size * 1.15 if image is not None else layout_size(glyph)[1] * 0.92 if glyph else 0.0

        # Each line gets the slice's width at its own height: rows near a slanted edge are shorter, and
        # anything wider is clipped by the slice.
        widths = [max_width] * len(texts)
        layouts = [lay_out(text, size, lines, bold, max_width) for text, size, _rgb, _alpha, lines, bold in texts]
        top = y + head - (head + sum(layout_size(layout)[1] for layout in layouts)) / 2
        pad = ring.gap + ring.corner  # off the edges: the gap to the neighbour and the rounded corners
        for index, (text, size, _rgb, _alpha, lines, bold) in enumerate(texts):
            height = layout_size(layouts[index])[1]
            room = min(_row_width(x, row, mid, span, ring.inner, outer) for row in (top, top + height)) - pad
            if room < max_width:
                widths[index] = max(10.0, room)
                layouts[index] = lay_out(text, size, lines, bold, widths[index])
            top += height

        # (layout, colour, alpha, scroll box width or 0)
        shown: list[tuple[Pango.Layout, RGB, float, float]] = []
        for (text, size, rgb, alpha, lines, bold), layout, width in zip(texts, layouts, widths):
            if selected and layout.is_ellipsized():
                # Cut off: the selected slice shows it whole on one line, scrolling through its box
                # (a bit narrower than the line's room, so the slice's rounded sides don't clip letters).
                shown.append((lay_out(text, size, lines, bold, None), rgb, alpha, width * 0.88))
            else:
                shown.append((layout, rgb, alpha, 0.0))

        heights = [layout_size(layout)[1] for layout, _rgb, _alpha, _box in shown]
        y -= (head + sum(heights)) / 2
        if image is not None:
            _draw_paintable(cr, image, x - image_size / 2, y, image_size)
        elif glyph is not None:
            draw_layout(cr, glyph, x, y + head / 2, mix(tint, theme.foreground, 0.15 * pop))
        y += head
        elapsed = GLib.get_monotonic_time() / 1_000_000 - self._marquee_since
        for (layout, rgb, alpha, box), height in zip(shown, heights):
            if box:
                overflow = layout_size(layout)[0] - box
                offset = marquee_offset(overflow, layout_size(layout)[1] * MARQUEE_SPEED, elapsed)
                cr.save()
                cr.rectangle(x - box / 2, y, box, height)
                cr.clip()
                draw_layout(cr, layout, x - box / 2 - offset, y, rgb, alpha, centered=False)
                cr.restore()
                self._marquee_running = True
            else:
                draw_layout(cr, layout, x, y + height / 2, rgb, alpha)
            y += height

    def _image(self, image: str) -> Gdk.Paintable | None:
        """An app icon, looked up once; None when neither the icon theme nor its file has it."""
        if image not in self._images:
            self._images[image] = lookup_icon(image, IMAGE_LOOKUP_PX, self._area.get_scale_factor())
        return self._images[image]

    def _corner_point(
        self, ring: _Ring, index: int, count: int, inset: float, clockwise: bool
    ) -> tuple[float, float]:
        """A point near the outer corner of a slice (`inset` in from its popped rim)."""
        span = TAU / count
        offset = min(span * 0.28, 0.42)
        angle = -math.pi / 2 + index * span + (offset if clockwise else -offset)
        r = ring.popped_outer(self._pop[index]) - inset
        return math.cos(angle) * r, math.sin(angle) * r

    def _draw_badge(
        self,
        cr: cairo.Context,
        context: Pango.Context,
        theme: Theme,
        ring: _Ring,
        index: int,
        count: int,
    ) -> None:
        item = self._items[index]
        fill = self._tint(item, theme) if item.badge_color is None else _rgb(item.badge_color)
        layout = make_layout(context, theme, item.badge, ring.outer * 0.052, weight=Pango.Weight.BOLD)
        text_w, text_h = layout_size(layout)
        height = text_h + ring.outer * 0.026
        width = max(height, text_w + height * 0.75)
        # Straddle the rim; group arcs run just far enough out to clear the pill.
        inset = height * 0.15
        x, y = self._corner_point(ring, index, count, inset, clockwise=True)

        _pill(cr, x - width / 2, y - height / 2, width, height)
        cr.set_source_rgba(*theme.background, 0.95)
        cr.set_line_width(max(2.0, ring.outer * 0.014))
        cr.stroke_preserve()
        cr.set_source_rgb(*fill)
        cr.fill()
        draw_layout(cr, layout, x, y, _ink(fill, theme))

    def _draw_check(
        self,
        cr: cairo.Context,
        context: Pango.Context,
        theme: Theme,
        ring: _Ring,
        index: int,
        count: int,
    ) -> None:
        radius = ring.outer * 0.042
        x, y = self._corner_point(ring, index, count, radius * 2.0, clockwise=False)
        cr.new_path()
        cr.arc(x, y, radius, 0, TAU)
        cr.set_source_rgb(*theme.accent)
        cr.fill()
        for glyph in CHECK_GLYPHS:
            layout = make_layout(context, theme, glyph, radius * 1.15, weight=Pango.Weight.BOLD)
            if layout.get_unknown_glyphs_count() == 0:
                draw_layout(cr, layout, x, y, _ink(theme.accent, theme))
                return

    def _group_angles(self, group: GroupSpan, count: int) -> tuple[float, float]:
        span = TAU / count
        start = -math.pi / 2 + (group.start - 0.5) * span
        return start, start + group.length * span

    def _draw_group_arc(self, cr: cairo.Context, theme: Theme, ring: _Ring, group: GroupSpan, count: int) -> None:
        radius = ring.outer * GROUP_ARC_RADIUS
        width = ring.outer * GROUP_ARC_WIDTH
        cr.save()
        cr.new_path()
        if group.length >= count:
            cr.arc(0, 0, radius, 0, TAU)
        else:
            a0, a1 = self._group_angles(group, count)
            # Round caps grow the arc by width/2 at each end; keep a slice gap between groups.
            inset = (ring.gap * 1.5 + width / 2) / radius
            cr.arc(0, 0, radius, a0 + inset, a1 - inset)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(width)
        cr.set_source_rgba(*self._group_color(group, theme), 0.9)
        cr.stroke()
        cr.restore()

    def _draw_group_label(
        self,
        cr: cairo.Context,
        context: Pango.Context,
        theme: Theme,
        ring: _Ring,
        group: GroupSpan,
        count: int,
    ) -> None:
        color = self._group_color(group, theme)
        a0, a1 = self._group_angles(group, count)
        mid = -math.pi / 2 if group.length >= count else (a0 + a1) / 2
        cos, sin = math.cos(mid), math.sin(mid)
        edge = ring.outer * (GROUP_ARC_RADIUS + GROUP_ARC_WIDTH / 2 + GROUP_LABEL_GAP)
        size = ring.outer * 0.05
        pad_x, pad_y = size * 0.7, size * 0.3

        # A leading Nerd Font icon overhangs its advance width; lay it out by its ink instead.
        icon_text, text = _split_icon(group.name)
        icon = make_layout(context, theme, icon_text, size * 1.1) if icon_text else None
        if icon is not None and icon.get_unknown_glyphs_count() > 0:
            icon = None
        icon_w = 0.0
        if icon is not None:
            icon_ink, icon_logical = icon.get_pixel_extents()
            icon_w = icon_ink.width + size * 0.45

        layout = make_layout(context, theme, text, size, weight=Pango.Weight.BOLD)
        text_w, text_h = layout_size(layout)
        height = text_h + 2 * pad_y
        max_width = ring.outer * GROUP_LABEL_MAX_WIDTH
        if group.length < count:
            extent = min(a1 - a0, math.pi)
            chord = 2 * edge * math.sin(extent / 2)
            if chord * abs(cos) < height * 1.3:
                # Neighbouring group labels sit side by side here; keep within this group's arc.
                max_width = min(max_width, max(chord * 0.92, ring.outer * 0.24))
        if icon_w + text_w + 2 * pad_x > max_width:
            text_width = max(size, max_width - 2 * pad_x - icon_w)
            layout = make_layout(context, theme, text, size, weight=Pango.Weight.BOLD, width=text_width)
            text_w = layout_size(layout)[0]
        width = icon_w + text_w + 2 * pad_x
        # Push the label out until its box clears the arc in the direction of the group.
        distance = edge + abs(cos) * width / 2 + abs(sin) * height / 2
        x, y = cos * distance, sin * distance

        _pill(cr, x - width / 2, y - height / 2, width, height)
        cr.set_source_rgba(*theme.background, 0.92)
        cr.fill_preserve()
        cr.set_line_width(1.5)
        cr.set_source_rgba(*color, 0.7)
        cr.stroke()
        left = x - width / 2 + pad_x
        if icon is not None:
            # Centre of the logical box, shifted so the ink starts at `left`.
            icon_x = left - icon_ink.x + icon_logical.x + icon_logical.width / 2
            draw_layout(cr, icon, icon_x, y, color)
        draw_layout(cr, layout, left + icon_w + text_w / 2, y, mix(color, theme.foreground, 0.3))

    def _draw_centre(
        self, cr: cairo.Context, context: Pango.Context, theme: Theme, ring: _Ring, count: int
    ) -> None:
        radius = ring.inner - ring.gap * 1.4
        cr.new_path()
        cr.arc(0, 0, radius, 0, TAU)
        cr.set_source_rgba(*theme.background, 0.95)
        cr.fill_preserve()
        cr.set_line_width(1.5)
        cr.set_source_rgba(*theme.muted, 0.35)
        cr.stroke()

        hold = self._hold
        selected = self._items[self._selection] if self._selection is not None else None
        if hold is not None:
            self._draw_hold_ring(cr, theme, ring, radius, hold)
        elif selected is not None and count > 1:
            # Arc on the hub pointing at the selected slice.
            span = TAU / count
            mid = -math.pi / 2 + self._selection * span
            reach = min(span * 0.35, 0.5)
            cr.new_path()
            cr.arc(0, 0, radius - ring.outer * 0.012, mid - reach, mid + reach)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_width(ring.outer * 0.018)
            cr.set_source_rgb(*self._tint(selected, theme))
            cr.stroke()

        width = radius * 1.5
        if hold is None and selected is None:
            title = make_layout(
                context, theme, self._title, ring.outer * 0.075, weight=Pango.Weight.BOLD, width=width, lines=2
            )
            draw_layout(cr, title, 0, 0, theme.foreground, 0.9)
            return

        small = ring.outer * 0.042
        if hold is not None:  # the hub names what the hold is about to do
            heading = f"{glyph_markup(hold.glyph, small)} {GLib.markup_escape_text(hold.title.upper())}"
            name, detail = hold.label, ""
        else:
            heading = GLib.markup_escape_text(self._title.upper())
            name, detail = selected.label, selected.detail
        title = make_layout(
            context,
            theme,
            f'<span letter_spacing="{int(ring.outer * 0.006 * Pango.SCALE)}">{heading}</span>',
            small,
            weight=Pango.Weight.BOLD,
            width=width * 0.9,
            markup=True,
        )
        label = make_layout(context, theme, name, ring.outer * 0.072, weight=Pango.Weight.BOLD, width=width, lines=2)
        blocks: list[tuple[Pango.Layout, RGB, float]] = [
            (title, theme.muted, 1.0),
            (label, theme.foreground, 1.0),
        ]
        if detail:
            detail_layout = make_layout(context, theme, detail, ring.outer * 0.05, width=width, lines=3)
            blocks.append((detail_layout, theme.foreground, 0.70))
        gap = ring.outer * 0.018
        heights = [layout_size(layout)[1] for layout, _rgb, _alpha in blocks]
        y = -(sum(heights) + gap * (len(blocks) - 1)) / 2
        for (layout, rgb, alpha), height in zip(blocks, heights):
            draw_layout(cr, layout, 0, y + height / 2, rgb, alpha)
            y += height + gap

    def _draw_hold_ring(self, cr: cairo.Context, theme: Theme, ring: _Ring, radius: float, hold: _Hold) -> None:
        """A track round the hub that fills clockwise from the top as the hold runs, in the button's colour."""
        progress = min(1.0, (GLib.get_monotonic_time() / 1_000_000 - hold.start) / max(hold.seconds, 0.001))
        color = hex_rgb(GLYPH_COLORS[hold.glyph]) if hold.glyph in GLYPH_COLORS else theme.accent
        track = radius - ring.outer * 0.016
        cr.set_line_width(ring.outer * 0.024)
        cr.new_path()
        cr.arc(0, 0, track, 0, TAU)
        cr.set_source_rgba(*color, 0.2)
        cr.stroke()
        cr.new_path()
        cr.arc(0, 0, track, -math.pi / 2, -math.pi / 2 + TAU * max(progress, 0.002))
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgb(*color)
        cr.stroke()

    def _draw_legend(
        self, cr: cairo.Context, context: Pango.Context, theme: Theme, ring: _Ring, width: int, height: int
    ) -> None:
        """Button caps and what each does right now, in a panel at the bottom-right corner of the area."""
        if not self._legend:
            return
        size = ring.outer * LEGEND_TEXT
        rows = [
            (
                make_layout(context, theme, glyph_markup(key, size), size, weight=Pango.Weight.BOLD, markup=True),
                make_layout(context, theme, text, size, width=ring.outer * 1.3, align=Pango.Alignment.LEFT),
            )
            for key, text in self._legend
        ]
        pad_x, pad_y = size * 0.45, size * 0.18
        cap_h = max(layout_size(key)[1] for key, _text in rows) + 2 * pad_y
        caps = [max(cap_h, layout_size(key)[0] + 2 * pad_x) for key, _text in rows]
        gap, row_gap, inset = size * 0.7, size * 0.4, size * 0.85
        text_w = max(layout_size(text)[0] for _key, text in rows)
        panel_w = 2 * inset + max(caps) + gap + text_w
        panel_h = 2 * inset + len(rows) * cap_h + (len(rows) - 1) * row_gap
        x = width / 2 - ring.outer * LEGEND_MARGIN - panel_w
        y = height / 2 - ring.outer * LEGEND_MARGIN - panel_h
        rounded_rect(cr, x, y, panel_w, panel_h, size * 0.8)
        cr.set_source_rgba(*theme.surface, 0.9)
        cr.fill_preserve()
        cr.set_line_width(1.0)
        cr.set_source_rgba(*theme.muted, 0.3)
        cr.stroke()
        y += inset
        for (key, text), cap_w in zip(rows, caps):
            rounded_rect(cr, x + inset, y, cap_w, cap_h, cap_h * 0.3)
            cr.set_source_rgb(*mix(theme.surface, theme.foreground, 0.08))
            cr.fill_preserve()
            cr.set_source_rgba(*theme.muted, 0.35)
            cr.stroke()
            draw_layout(cr, key, x + inset + cap_w / 2, y + cap_h / 2, theme.foreground)
            text_y = y + (cap_h - layout_size(text)[1]) / 2
            draw_layout(cr, text, x + inset + max(caps) + gap, text_y, theme.foreground, 0.85, centered=False)
            y += cap_h + row_gap

    def _draw_battery(
        self, cr: cairo.Context, context: Pango.Context, theme: Theme, ring: _Ring, width: int, height: int
    ) -> None:
        """The controller's charge in a panel at the bottom-left corner, level with the legend."""
        if self._battery is None:
            return
        percent, charging = self._battery
        icon = BATTERY_CHARGING if charging else BATTERY_ICONS[max(0, min(percent // 10, 10))]
        size = ring.outer * LEGEND_TEXT
        text = make_layout(context, theme, f"{icon} {percent}%", size, weight=Pango.Weight.BOLD)
        text_w, text_h = layout_size(text)
        inset = size * 0.85
        panel_w, panel_h = text_w + 2 * inset, text_h + 2 * size * 0.18 + 2 * inset  # a one-row legend's height
        x = -width / 2 + ring.outer * LEGEND_MARGIN
        y = height / 2 - ring.outer * LEGEND_MARGIN - panel_h
        rounded_rect(cr, x, y, panel_w, panel_h, size * 0.8)
        cr.set_source_rgba(*theme.surface, 0.9)
        cr.fill_preserve()
        cr.set_line_width(1.0)
        cr.set_source_rgba(*theme.muted, 0.3)
        cr.stroke()
        draw_layout(cr, text, x + panel_w / 2, y + panel_h / 2, theme.foreground, 0.85)


def _rgb(color: tuple[int, int, int]) -> RGB:
    return color[0] / 255, color[1] / 255, color[2] / 255


def _luminance(rgb: RGB) -> float:
    """WCAG relative luminance."""
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ink(fill: RGB, theme: Theme) -> RGB:
    """Whichever of the theme's background/foreground contrasts more with `fill`."""
    lum = _luminance(fill)

    def contrast(ink: RGB) -> float:
        light, dark = sorted((lum, _luminance(ink)), reverse=True)
        return (light + 0.05) / (dark + 0.05)

    return max((theme.background, theme.foreground), key=contrast)


def _split_icon(text: str) -> tuple[str, str]:
    """Split a leading private-use (Nerd Font) glyph off `text`: ("\\uf109", "workstation")."""
    if text and (0xE000 <= ord(text[0]) <= 0xF8FF or ord(text[0]) >= 0xF0000):
        return text[0], text[1:].lstrip()
    return "", text


def lookup_icon(image: str, size: int, scale: int) -> Gdk.Paintable | None:
    """A Gio icon string (theme icon name or file) → paintable; None if the theme or the file lacks it."""
    try:
        gicon = Gio.Icon.new_for_string(image)
    except GLib.Error:
        return None
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    if isinstance(gicon, Gio.FileIcon):
        if not os.path.isfile(gicon.get_file().get_path() or ""):
            return None
    elif not theme.has_gicon(gicon):
        return None  # its lookup would hand back the theme's "missing" picture
    return theme.lookup_by_gicon(gicon, size, scale, Gtk.TextDirection.NONE, 0)


def _draw_paintable(cr: cairo.Context, paintable: Gdk.Paintable, x: float, y: float, size: float) -> None:
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, size, size)
    node = snapshot.to_node()
    if node is not None:
        cr.save()
        cr.translate(x, y)
        node.draw(cr)
        cr.restore()



def _pill(cr: cairo.Context, x: float, y: float, width: float, height: float) -> None:
    r = height / 2
    cr.new_path()
    cr.arc(x + width - r, y + r, r, -math.pi / 2, math.pi / 2)
    cr.arc(x + r, y + r, r, math.pi / 2, 3 * math.pi / 2)
    cr.close_path()


def _sector_path(
    cr: cairo.Context, a0: float, a1: float, inner: float, outer: float, gap: float, corner: float
) -> None:
    """Annular sector a0..a1 (clockwise) whose straight edges sit `gap / 2` off the radial lines,
    so neighbouring slices are separated by a constant-width gap, with all four corners filleted."""
    h = gap / 2
    corner = max(0.0, min(corner, (outer - inner) / 2.2))
    k = corner
    outer_d = outer - k
    inner_d = inner + k
    phi_o = math.asin(min(1.0, (h + k) / outer_d))
    phi_i = math.asin(min(1.0, (h + k) / inner_d))
    cr.new_path()
    # Start edge, outer fillet: from the straight edge round to the outer arc.
    cr.arc(math.cos(a0 + phi_o) * outer_d, math.sin(a0 + phi_o) * outer_d, k, a0 - math.pi / 2, a0 + phi_o)
    cr.arc(0, 0, outer, a0 + phi_o, a1 - phi_o)
    cr.arc(math.cos(a1 - phi_o) * outer_d, math.sin(a1 - phi_o) * outer_d, k, a1 - phi_o, a1 + math.pi / 2)
    cr.arc(
        math.cos(a1 - phi_i) * inner_d,
        math.sin(a1 - phi_i) * inner_d,
        k,
        a1 + math.pi / 2,
        a1 - phi_i + math.pi,
    )
    cr.arc_negative(0, 0, inner, a1 - phi_i, a0 + phi_i)
    cr.arc(
        math.cos(a0 + phi_i) * inner_d,
        math.sin(a0 + phi_i) * inner_d,
        k,
        a0 + phi_i + math.pi,
        a0 + 3 * math.pi / 2,
    )
    cr.close_path()


def _row_width(x: float, y: float, mid: float, span: float, inner: float, outer: float) -> float:
    """Width of the horizontal row through (x, y), centred on x, that stays inside the annular sector
    centred on angle `mid` (radians, clockwise from +x as in cairo) spanning `span`, between radii
    `inner` and `outer`. 0 when (x, y) is outside it."""
    if abs(y) >= outer:
        return 0.0
    reach = math.sqrt(outer * outer - y * y)
    low, high = -reach, reach
    if abs(y) < inner:  # the hub cuts the row on the side towards the centre
        hole = math.sqrt(inner * inner - y * y)
        if x >= 0:
            low = max(low, hole)
        else:
            high = min(high, -hole)
    if span < math.pi + 1e-9:  # a wedge: inside both straight edges (each one a half-plane)
        start, end = mid - span / 2, mid + span / 2
        if abs(math.sin(start)) > 1e-9:
            bound = math.cos(start) * y / math.sin(start)
            low, high = (low, min(high, bound)) if math.sin(start) > 0 else (max(low, bound), high)
        elif math.cos(start) * y < 0:
            return 0.0
        if abs(math.sin(end)) > 1e-9:
            bound = math.cos(end) * y / math.sin(end)
            low, high = (max(low, bound), high) if math.sin(end) > 0 else (low, min(high, bound))
        elif math.cos(end) * y > 0:
            return 0.0
    return max(0.0, 2 * min(x - low, high - x))
