"""Overlay colours, taken from the active Omarchy theme when there is one."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

RGB = tuple[float, float, float]

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
    for path in _THEME_FILES:
        path = path.expanduser()
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                return _theme_from(tomllib.load(handle))
        except (OSError, ValueError) as error:  # TOMLDecodeError and bad UTF-8 are ValueErrors
            log.warning("cannot read theme %s: %s", path, error)
    return FALLBACK


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
