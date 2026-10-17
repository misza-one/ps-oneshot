"""Cheat sheet overlay: a centred panel listing the controls in balanced columns."""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from dataclasses import dataclass

import cairo
from gi.repository import Pango

from ps_oneshot.ui.overlay import (
    Overlay,
    draw_layout,
    ease_out,
    glyph_markup,
    layout_size,
    make_layout,
    mix,
    rounded_rect,
)
from ps_oneshot import APP_NAME
from ps_oneshot.ui.theme import Theme

Sections = list[tuple[str, list[tuple[str, str]]]]

TITLE = APP_NAME
SUBTITLE = "DualSense controls"
TITLE_ICON = "\uf11b"  # Nerd Font gamepad; dropped when the font lacks it
MAX_COLUMNS = 4
TARGET_ASPECT = 1.7
SCROLL_SPEED = 1500.0  # px per second at full stick deflection (at unit 1)


@dataclass(frozen=True)
class _Metrics:
    unit: float

    def __getattr__(self, name: str) -> float:
        try:
            return _BASE[name] * self.unit
        except KeyError:
            raise AttributeError(name) from None


_BASE = {
    "pad": 40.0,
    "shadow": 28.0,
    "radius": 20.0,
    "title": 26.0,
    "subtitle": 14.0,
    "header_gap": 26.0,
    "section_title": 12.5,
    "section_title_gap": 12.0,
    "section_gap": 28.0,
    "key": 14.0,
    "key_pad_x": 10.0,
    "key_pad_y": 4.0,
    "key_radius": 7.0,
    "key_gap": 14.0,
    "desc": 14.5,
    "desc_width": 290.0,
    "row_gap": 9.0,
    "column_gap": 52.0,
}


@dataclass(frozen=True)
class _Row:
    key: Pango.Layout
    desc: Pango.Layout
    key_width: float
    key_height: float
    desc_width: float
    height: float


@dataclass(frozen=True)
class _Section:
    title: Pango.Layout
    rows: list[_Row]
    height: float


@dataclass(frozen=True)
class _Column:
    sections: list[_Section]
    key_width: float
    width: float
    height: float


@dataclass(frozen=True)
class _Plan:
    m: _Metrics
    icon: Pango.Layout | None
    title: Pango.Layout
    subtitle: Pango.Layout
    header_height: float
    columns: list[_Column]
    width: float
    height: float  # the panel, at most what the screen allows
    body_height: float  # all the rows
    view_height: float  # the rows on screen; less than body_height means it scrolls


class CheatSheet(Overlay):
    backdrop_alpha = (0.78, 0.62)

    def __init__(self, theme_loader: Callable[[], Theme]) -> None:
        super().__init__(theme_loader, "cheatsheet")
        self._sections: Sections = []
        self._plan: _Plan | None = None
        self._scroll = 0.0  # px the rows are scrolled, eased towards _scroll_target
        self._scroll_target = 0.0
        self._scroll_speed = 0.0  # px/s while a stick is pushed

    def show(self, sections: Sections, monitor: str | None) -> None:
        self._sections = list(sections)
        self._scroll = self._scroll_target = self._scroll_speed = 0.0
        self._present(monitor)

    @property
    def scroll_max(self) -> float:
        plan = self._plan
        return max(0.0, plan.body_height - plan.view_height) if plan is not None else 0.0

    def scroll(self, amount: float) -> None:
        """Stick deflection past the deadzone, -1..1 (positive = down); scrolls until it changes."""
        if self._plan is None or self.scroll_max <= 0:
            return
        self._scroll_speed = math.copysign(abs(amount) ** 1.5, amount) * SCROLL_SPEED * self._plan.m.unit
        if self._scroll_speed:
            self._start_ticking()

    def scroll_step(self, direction: int) -> None:
        """D-pad: a step of about half the visible rows, up (-1) or down (1)."""
        if self._plan is None or self.scroll_max <= 0:
            return
        step = self._plan.view_height * 0.45
        self._scroll_target = min(max(self._scroll_target + direction * step, 0.0), self.scroll_max)
        self._start_ticking()

    def _animate(self, dt: float) -> bool:
        limit = self.scroll_max
        if self._scroll_speed:
            self._scroll_target = min(max(self._scroll_target + self._scroll_speed * dt, 0.0), limit)
        pushing = (self._scroll_speed > 0 and self._scroll_target < limit) or (
            self._scroll_speed < 0 and self._scroll_target > 0
        )
        gap = self._scroll_target - self._scroll
        if abs(gap) < 0.5:
            self._scroll = self._scroll_target
            return pushing
        self._scroll += gap * min(1.0, dt * 18.0)
        return True

    def _on_hidden(self) -> None:
        self._scroll_speed = 0.0

    def _content_size(self, monitor_width: int, monitor_height: int) -> tuple[float, float]:
        assert self._area is not None
        context = self._area.get_pango_context()
        unit = min(1.6, max(0.75, min(monitor_width, monitor_height) / 1080))
        attempts = 6
        for attempt in range(attempts):
            m = _Metrics(unit)
            # Shrink only to fit the width (the last attempt takes any width); a tall sheet scrolls instead.
            max_width = monitor_width * 0.92 if attempt < attempts - 1 else math.inf
            plan = self._best_plan(self._measure(context, m), context, m, max_width, monitor_height * 0.9)
            if plan is not None:
                break
            unit *= 0.86
        assert plan is not None
        self._plan = plan
        margin = plan.m.shadow * 2
        return plan.width + margin, plan.height + margin

    def _measure(self, context: Pango.Context, m: _Metrics) -> list[_Section]:
        theme = self.theme
        spacing = int(m.section_title * 0.12 * Pango.SCALE)
        layouts = [
            (
                make_layout(
                    context,
                    theme,
                    _title_markup(name, spacing, m.section_title),
                    m.section_title,
                    weight=Pango.Weight.SEMIBOLD,
                    markup=True,
                    align=Pango.Alignment.LEFT,
                ),
                [
                    (
                        make_layout(
                            context, theme, glyph_markup(key, m.key), m.key, weight=Pango.Weight.SEMIBOLD, markup=True
                        ),
                        make_layout(
                            context, theme, desc, m.desc, width=m.desc_width, lines=0, align=Pango.Alignment.LEFT
                        ),
                    )
                    for key, desc in rows
                ],
            )
            for name, rows in self._sections
        ]
        # Every key cap gets the same height, so rows with enlarged ✕ ○ △ □ glyphs line up.
        key_height = 2 * m.key_pad_y + max(
            (layout_size(key)[1] for _title, rows in layouts for key, _desc in rows), default=0.0
        )
        sections = []
        for title, rows in layouts:
            measured = []
            for key, desc in rows:
                desc_w, desc_h = layout_size(desc)
                measured.append(
                    _Row(
                        key=key,
                        desc=desc,
                        key_width=max(key_height, layout_size(key)[0] + 2 * m.key_pad_x),
                        key_height=key_height,
                        desc_width=desc_w,
                        height=max(key_height, desc_h),
                    )
                )
            height = layout_size(title)[1] + m.section_title_gap
            height += sum(row.height for row in measured) + m.row_gap * max(0, len(measured) - 1)
            sections.append(_Section(title=title, rows=measured, height=height))
        return sections

    def _best_plan(
        self,
        sections: list[_Section],
        context: Pango.Context,
        m: _Metrics,
        max_width: float,
        max_height: float,
    ) -> _Plan | None:
        theme = self.theme
        title = make_layout(context, theme, TITLE, m.title, weight=Pango.Weight.SEMIBOLD, align=Pango.Alignment.LEFT)
        subtitle = make_layout(context, theme, SUBTITLE, m.subtitle, align=Pango.Alignment.LEFT)
        icon: Pango.Layout | None = make_layout(context, theme, TITLE_ICON, m.title * 1.05)
        if icon is not None and icon.get_unknown_glyphs_count() > 0:
            icon = None
        header_height = max(layout_size(title)[1], layout_size(subtitle)[1])

        best: _Plan | None = None  # fits the screen, closest to TARGET_ASPECT
        shortest: _Plan | None = None  # nothing fits: the one that scrolls least
        chrome = 2 * m.pad + header_height + m.header_gap
        for count in range(1, min(MAX_COLUMNS, max(1, len(sections))) + 1):
            columns = [_column(group, m) for group in _partition(sections, count)]
            body_width = sum(c.width for c in columns) + m.column_gap * max(0, len(columns) - 1)
            header_width = layout_size(title)[0] + layout_size(subtitle)[0] + m.title * 3
            width = 2 * m.pad + max(body_width, header_width)
            if width > max_width:
                continue
            body_height = max((c.height for c in columns), default=0.0)
            height = min(chrome + body_height, max_height)
            plan = _Plan(
                m, icon, title, subtitle, header_height, columns, width, height, body_height, max(0.0, height - chrome)
            )
            if chrome + body_height > max_height:
                if shortest is None or body_height < shortest.body_height:
                    shortest = plan
            elif best is None or abs(math.log(width / height / TARGET_ASPECT)) < abs(
                math.log(best.width / best.height / TARGET_ASPECT)
            ):
                best = plan
        return best or shortest

    def _draw(self, cr: cairo.Context, width: int, height: int) -> None:
        plan = self._plan
        if plan is None:
            return
        theme = self.theme
        m = plan.m
        opened = ease_out(self.open_progress)
        scale = 0.965 + 0.035 * opened
        cr.translate(width / 2, height / 2)
        cr.scale(scale, scale)
        cr.translate(-plan.width / 2, -plan.height / 2 + (1 - opened) * m.pad * 0.4)

        # Soft shadow, then the panel.
        for step in range(1, 7):
            spread = m.shadow * step / 6
            rounded_rect(cr, -spread, -spread * 0.6, plan.width + 2 * spread, plan.height + 2 * spread, m.radius + spread)
            cr.set_source_rgba(0, 0, 0, 0.035)
            cr.fill()
        rounded_rect(cr, 0, 0, plan.width, plan.height, m.radius)
        cr.set_source_rgba(*theme.background, 0.97)
        cr.fill_preserve()
        cr.set_line_width(1.5)
        cr.set_source_rgba(*theme.muted, 0.45)
        cr.stroke()

        # Header: icon + title on the left, subtitle on the right, hairline beneath.
        x = m.pad
        y = m.pad + plan.header_height / 2
        if plan.icon is not None:
            # Nerd Font icons overhang their advance width, so place the ink, not the logical box.
            ink, logical = plan.icon.get_pixel_extents()
            draw_layout(cr, plan.icon, x - ink.x + logical.x + logical.width / 2, y, theme.accent)
            x += ink.width + m.title * 0.5
        title_h = layout_size(plan.title)[1]
        draw_layout(cr, plan.title, x, y - title_h / 2, theme.foreground, centered=False)
        sub_w, sub_h = layout_size(plan.subtitle)
        draw_layout(cr, plan.subtitle, plan.width - m.pad - sub_w, y - sub_h / 2, theme.muted, centered=False)
        rule_y = m.pad + plan.header_height + m.header_gap / 2
        cr.new_path()
        cr.move_to(m.pad, rule_y)
        cr.line_to(plan.width - m.pad, rule_y)
        cr.set_line_width(1.0)
        cr.set_source_rgba(*theme.muted, 0.3)
        cr.stroke()

        top = m.pad + plan.header_height + m.header_gap
        scroll_max = self.scroll_max
        clip_top, clip_bottom = rule_y + 1.0, plan.height - m.pad * 0.5
        cr.save()
        cr.rectangle(0, clip_top, plan.width, clip_bottom - clip_top)
        cr.clip()
        x = m.pad
        for column in plan.columns:
            self._draw_column(cr, column, x, top - self._scroll, theme, m)
            x += column.width + m.column_gap
        if scroll_max > 0:
            # Rows fade into the panel where more is hidden.
            fade, left, right = m.pad * 1.2, m.pad * 0.5, plan.width - m.pad * 0.8
            _fade(cr, left, right, clip_top, fade, theme.background, self._scroll / fade, downwards=True)
            bottom_strength = (scroll_max - self._scroll) / fade
            _fade(cr, left, right, clip_bottom - fade, fade, theme.background, bottom_strength, downwards=False)
        cr.restore()
        if scroll_max > 0:
            self._draw_scrollbar(cr, plan, clip_top, clip_bottom, scroll_max, theme)

    def _draw_scrollbar(
        self, cr: cairo.Context, plan: _Plan, top: float, bottom: float, scroll_max: float, theme: Theme
    ) -> None:
        m = plan.m
        bar = 4.0 * m.unit
        x = plan.width - m.pad * 0.5 - bar / 2
        track_top, track = top + m.pad * 0.25, bottom - top - m.pad * 0.5
        thumb = max(m.pad, track * plan.view_height / plan.body_height)
        thumb_y = track_top + (track - thumb) * min(1.0, self._scroll / scroll_max)
        rounded_rect(cr, x, track_top, bar, track, bar / 2)
        cr.set_source_rgba(*theme.muted, 0.18)
        cr.fill()
        rounded_rect(cr, x, thumb_y, bar, thumb, bar / 2)
        cr.set_source_rgba(*theme.accent, 0.85)
        cr.fill()

    def _draw_column(self, cr: cairo.Context, column: _Column, x: float, y: float, theme: Theme, m: _Metrics) -> None:
        for section in column.sections:
            title_h = layout_size(section.title)[1]
            draw_layout(cr, section.title, x, y, theme.accent, centered=False)
            y += title_h + m.section_title_gap
            for row in section.rows:
                pill_y = y + (row.height - row.key_height) / 2
                rounded_rect(cr, x, pill_y, row.key_width, row.key_height, m.key_radius)
                cr.set_source_rgb(*mix(theme.surface, theme.foreground, 0.04))
                cr.fill_preserve()
                cr.set_line_width(1.0)
                cr.set_source_rgba(*theme.muted, 0.35)
                cr.stroke()
                draw_layout(cr, row.key, x + row.key_width / 2, pill_y + row.key_height / 2, theme.foreground)
                desc_h = layout_size(row.desc)[1]
                draw_layout(
                    cr,
                    row.desc,
                    x + column.key_width + m.key_gap,
                    y + (row.height - desc_h) / 2,
                    theme.foreground,
                    0.82,
                    centered=False,
                )
                y += row.height + m.row_gap
            y += m.section_gap - m.row_gap


def _title_markup(name: str, spacing: int, size: float) -> str:
    """Letter-spaced upper-case section title; face-button glyphs keep their colour, like the key caps."""
    return f'<span letter_spacing="{spacing}">{glyph_markup(name.upper(), size)}</span>'


def _fade(
    cr: cairo.Context, left: float, right: float, y: float, height: float, rgb: tuple[float, float, float],
    strength: float, *, downwards: bool,
) -> None:
    """Panel-coloured gradient over the rows at the top (downwards) or bottom edge of the view."""
    strength = min(1.0, strength)
    if strength <= 0:
        return
    gradient = cairo.LinearGradient(0, y, 0, y + height)
    solid, clear = (0.0, 1.0) if downwards else (1.0, 0.0)
    gradient.add_color_stop_rgba(solid, *rgb, 0.97 * strength)
    gradient.add_color_stop_rgba(clear, *rgb, 0.0)
    cr.rectangle(left, y, right - left, height)
    cr.set_source(gradient)
    cr.fill()


def _column(sections: list[_Section], m: _Metrics) -> _Column:
    rows = [row for section in sections for row in section.rows]
    key_width = max((row.key_width for row in rows), default=0.0)
    desc_width = max((row.desc_width for row in rows), default=0.0)
    title_width = max((layout_size(section.title)[0] for section in sections), default=0.0)
    width = max(key_width + m.key_gap + desc_width, title_width)
    height = sum(section.height for section in sections) + m.section_gap * max(0, len(sections) - 1)
    return _Column(sections=sections, key_width=key_width, width=width, height=height)


def _partition(sections: list[_Section], count: int) -> list[list[_Section]]:
    """Split in order into `count` contiguous groups, minimising the tallest group."""
    if count <= 1 or len(sections) <= 1:
        return [sections]
    best: list[list[_Section]] = [sections]
    best_height = math.inf
    for cuts in itertools.combinations(range(1, len(sections)), count - 1):
        bounds = (0, *cuts, len(sections))
        groups = [sections[a:b] for a, b in itertools.pairwise(bounds)]
        tallest = max(sum(s.height for s in group) for group in groups)
        if tallest < best_height:
            best, best_height = groups, tallest
    return best
