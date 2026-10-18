"""Overlay colours, taken from the active Omarchy theme when there is one."""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from gi.repository import Gio

log = logging.getLogger(__name__)

RGB = tuple[float, float, float]
RGBA = tuple[float, float, float, float]

_THEME_FILES = (
    Path("~/.local/state/omarchy/current/theme/colors.toml"),
    Path("~/.config/omarchy/current/theme/colors.toml"),
)


@dataclass(frozen=True)
class Theme:
    background: RGB
    surface: RGB
    foreground: RGB
    muted: RGB
    accent: RGB
    # A fontconfig family; "monospace" follows the system (Omarchy) font choice.
    font: str = "monospace"
    # Omarchy's image picker (the theme switcher's carousel), from [image-picker] in the theme's shell.toml;
    # None = Omarchy's own defaults (see the properties).
    picker_scrim_rgba: RGBA | None = None
    picker_selected_rgba: RGBA | None = None
    picker_unselected_rgba: RGBA | None = None

    @property
    def picker_scrim(self) -> RGBA:
        return self.picker_scrim_rgba or (*self.background, 0.5)

    @property
    def picker_selected(self) -> RGBA:
        return self.picker_selected_rgba or (*self.accent, 1.0)

    @property
    def picker_unselected(self) -> RGBA:
        return self.picker_unselected_rgba or (*self.foreground, 0.28)


FALLBACK = Theme(
    background=(0.067, 0.071, 0.090),
    surface=(0.133, 0.141, 0.176),
    foreground=(0.890, 0.902, 0.929),
    muted=(0.478, 0.502, 0.580),
    accent=(0.478, 0.635, 0.969),
)


def hex_rgb(s: str) -> tuple[int, int, int]:
    """Parse `#rgb`, `#rrggbb` or `#rrggbbaa` (alpha ignored; `#` optional)."""
    digits = s.strip().removeprefix("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    if len(digits) not in (6, 8):
        raise ValueError(f"not a hex colour: {s!r}")
    value = int(digits[:6], 16)
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def load_theme() -> Theme:
    # Omarchy's popups (menu, pickers) honour OMARCHY_MENU_FONT over the system monospace alias; so do we.
    font = os.environ.get("OMARCHY_MENU_FONT") or FALLBACK.font
    for path in _THEME_FILES:
        path = path.expanduser()
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                theme = _theme_from(tomllib.load(handle))
        except (OSError, ValueError) as error:  # TOMLDecodeError and bad UTF-8 are ValueErrors
            log.warning("cannot read theme %s: %s", path, error)
            continue
        return _with_shell(theme, path.with_name("shell.toml"), font)
    return replace(FALLBACK, font=font)


def watch_theme(on_change: Callable[[], None]) -> Gio.FileMonitor | None:
    """Call `on_change` after every `omarchy theme set`. It swaps the theme directory in, then writes
    theme.name next to it; the overlays reload the theme each time they open, but the lightbar needs telling."""
    directory = _THEME_FILES[0].expanduser().parent.parent
    if not directory.is_dir():
        return None

    def changed(_monitor: Gio.FileMonitor, file: Gio.File, _other: Gio.File | None,
                event: Gio.FileMonitorEvent) -> None:
        if event == Gio.FileMonitorEvent.CHANGES_DONE_HINT and file.get_basename() == "theme.name":
            on_change()

    monitor = Gio.File.new_for_path(str(directory)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
    monitor.connect("changed", changed)
    return monitor


def _with_shell(theme: Theme, path: Path, font: str) -> Theme:
    """`theme` plus the [image-picker] colours of the theme's shell.toml, when it has them."""
    try:
        with path.open("rb") as handle:
            picker = tomllib.load(handle).get("image-picker", {})
    except (OSError, ValueError):
        picker = {}
    if not isinstance(picker, dict):
        picker = {}
    return replace(
        theme,
        font=font,
        picker_scrim_rgba=shell_color(picker, "scrim", theme, theme.background, 0.5),
        picker_selected_rgba=shell_color(picker, "selected-border", theme, theme.accent, 1.0),
        picker_unselected_rgba=shell_color(picker, "unselected-border", theme, theme.foreground, 0.28),
    )


def shell_color(section: dict, key: str, theme: Theme, fallback: RGB, alpha: float) -> RGBA:
    """An Omarchy shell.toml colour token: `key` is a hex colour or a palette role (accent, foreground, text,
    background, muted), `key-alpha` its opacity. Roles we can't resolve (hyprland.*) use `fallback`."""
    roles = {
        "accent": theme.accent, "foreground": theme.foreground, "text": theme.foreground,
        "background": theme.background, "muted": theme.muted,
    }
    value, rgb = str(section.get(key, "")).strip(), fallback
    if value.lower() in roles:
        rgb = roles[value.lower()]
    elif value:
        try:
            r, g, b = hex_rgb(value)
            rgb = (r / 255, g / 255, b / 255)
        except ValueError:
            pass
    try:
        alpha = min(1.0, max(0.0, float(section.get(f"{key}-alpha", alpha))))
    except (TypeError, ValueError):
        pass
    return (*rgb, alpha)


def _theme_from(colors: dict[str, object]) -> Theme:
    def pick(keys: tuple[str, ...], fallback: RGB) -> RGB:
        for key in keys:
            value = colors.get(key)
            if isinstance(value, str):
                try:
                    r, g, b = hex_rgb(value)
                except ValueError:
                    continue
                return r / 255, g / 255, b / 255
        return fallback

    return Theme(
        background=pick(("background",), FALLBACK.background),
        surface=pick(("lighter_background", "selection"), FALLBACK.surface),
        foreground=pick(("foreground",), FALLBACK.foreground),
        muted=pick(("dark_foreground",), FALLBACK.muted),
        accent=pick(("accent",), FALLBACK.accent),
    )
