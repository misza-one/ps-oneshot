"""Settings tuned in the settings ring's submenus (Lightbar, Triggers, Sticks), live over the config.

Each is a `[section] key` of the config, named "section.key". The ring steps it (`next_value`), the change
applies at once (the daemon reads these keys live; trigger effects are re-sent) and is remembered as state key
`tuning` ({"section.key": value}), put back over the config at start by `restore`. Only keys changed in the
ring are remembered; the rest keep following the config file.
"""

from __future__ import annotations

from dataclasses import dataclass

from .lights import ALERT_STYLES


@dataclass(frozen=True)
class Tunable:
    stops: tuple[float, ...] = ()  # a stepper's values, ascending
    choices: tuple[str, ...] = ()  # cycled through
    switch: bool = False  # on/off


TUNABLES = {
    "lights.vivid": Tunable(switch=True),
    "lights.scanner": Tunable(switch=True),
    "lights.alert_style": Tunable(choices=ALERT_STYLES),
    "lights.brightness": Tunable(stops=(0.2, 0.4, 0.6, 0.8, 1.0)),
    "lights.idle_brightness": Tunable(stops=(0.0, 0.04, 0.08, 0.12, 0.2, 0.3)),
    "triggers.adaptive": Tunable(switch=True),
    "triggers.click_strength": Tunable(stops=(1, 2, 3, 4, 5, 6, 7, 8)),
    "sticks.scroll_speed": Tunable(stops=(6, 8, 10, 13, 16, 20, 25, 32, 40)),
    "sticks.pointer_speed": Tunable(stops=(500, 700, 900, 1100, 1400, 1700, 2100, 2600, 3200)),
    "input.repeat_interval_ms": Tunable(stops=(40, 55, 70, 90, 120, 160)),  # stick arrows, held buttons
    "input.stick_deadzone": Tunable(stops=(0.08, 0.12, 0.18, 0.24, 0.3)),
}


def value(raw: dict, name: str) -> object:
    section, key = name.split(".", 1)
    return raw[section][key]


def next_value(raw: dict, name: str, direction: int = 1, wrap: bool = False) -> object | None:
    """A switch flips, a choice cycles, a stepper moves to the next stop up (1) or down (-1) from the current
    value, which may sit between stops. None at the end of a stepper's range, unless `wrap` starts it over."""
    tunable, current = TUNABLES[name], value(raw, name)
    if tunable.switch:
        return not current
    if tunable.choices:
        index = tunable.choices.index(current) if current in tunable.choices else -1
        return tunable.choices[(index + 1) % len(tunable.choices)]
    level = float(current)  # type: ignore[arg-type]
    stops = tunable.stops if direction > 0 else tuple(reversed(tunable.stops))
    later = (stop for stop in stops if (stop > level + 1e-6 if direction > 0 else stop < level - 1e-6))
    return next(later, stops[0] if wrap else None)


def restore(raw: dict, saved: object) -> None:
    """Put the remembered settings over the config's, skipping unknown names and values out of range."""
    if not isinstance(saved, dict):
        return
    for name, setting in saved.items():
        tunable = TUNABLES.get(name)
        if tunable is None:
            continue
        if tunable.switch:
            ok = isinstance(setting, bool)
        elif tunable.choices:
            ok = setting in tunable.choices
        else:
            ok = (isinstance(setting, (int, float)) and not isinstance(setting, bool)
                  and tunable.stops[0] <= setting <= tunable.stops[-1])
        if ok:
            section, key = name.split(".", 1)
            raw[section][key] = setting
