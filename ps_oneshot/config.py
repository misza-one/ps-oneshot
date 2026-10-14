"""Layered TOML config: base defaults → preset → user file."""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

STICK_BUTTONS = tuple(f"{stick}_{d}" for stick in ("lstick", "rstick") for d in ("up", "down", "left", "right"))
BUTTONS = (
    "cross", "circle", "triangle", "square", "l1", "r1", "l2", "r2", "l3", "r3",
    "create", "options", "ps", "mic", "touchpad",
    "dpad_up", "dpad_down", "dpad_left", "dpad_right",
    *STICK_BUTTONS,
)
TRIGGERS = ("l2", "r2")
STICK_MODES = ("none", "scroll", "pointer", "buttons")
SOURCES = ("herd", "audio", "voxtype-language", "settings", "favorites")
ACTION_KINDS = {
    "wm", "dispatch", "exec", "key", "radial", "herd", "ui", "app", "dictation", "sounds", "rumble", "agent", "touchpad",
    "zoom", "text", "couch", "brightness", "launch",
}
FAVORITE_KEYS = {"label", "icon", "action"}
USER_CONFIG = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "ps-oneshot", "config.toml"
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Binding:
    press: str | None = None
    release: str | None = None
    tap: str | None = None
    hold: str | None = None
    repeat: bool = False
    light: str | None = None
    soft: str | None = None  # analog triggers: released before the full-press point
    full: str | None = None  # analog triggers: pushed past the full-press point
    desc: str | None = None

    def actions(self) -> list[str]:
        return [a for a in (self.press, self.tap, self.hold, self.release, self.soft, self.full) if a]

    @property
    def staged(self) -> bool:
        return bool(self.soft or self.full)


@dataclass(frozen=True)
class MenuItem:
    label: str
    icon: str = ""
    action: str | None = None
    keep_open: bool = False
    items: tuple[MenuItem, ...] = ()
    source: str | None = None  # live content: a submenu, or spliced into this ring when `inline`
    inline: bool = False


@dataclass(frozen=True)
class Radial:
    name: str
    title: str
    source: str | None = None
    items: tuple[MenuItem, ...] = ()


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    preset: str
    layers: dict[str, dict[str, Binding]]
    modifiers: dict[str, str]
    radials: dict[str, Radial]
    sources: list[str] = field(default_factory=list)

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})

    def get(self, section: str, key: str) -> Any:
        return self.raw[section][key]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _default(name: str) -> dict[str, Any]:
    text = resources.files("ps_oneshot").joinpath("defaults", f"{name}.toml").read_text("utf-8")
    return tomllib.loads(text)


def _parse_binding(path: str, value: Any) -> Binding:
    if isinstance(value, str):
        return Binding(press=value)
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: expected an action string or table")
    allowed = {"press", "release", "tap", "hold", "repeat", "light", "soft", "full", "desc"}
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    binding = Binding(**value)
    if binding.press and binding.tap:
        raise ConfigError(f"{path}: `press` and `tap` are exclusive (tap fires on release)")
    if binding.repeat and not binding.press:
        raise ConfigError(f"{path}: `repeat` needs a `press` action")
    if binding.staged and (binding.tap or binding.hold or binding.repeat):
        raise ConfigError(f"{path}: `soft`/`full` (analog stages) can't mix with tap/hold/repeat")
    if binding.staged and path.rsplit(".", 1)[-1] not in TRIGGERS:
        raise ConfigError(f"{path}: `soft`/`full` only work on analog triggers ({', '.join(TRIGGERS)})")
    if not binding.actions():
        raise ConfigError(f"{path}: no action")
    return binding


def _parse_items(path: str, value: Any) -> tuple[MenuItem, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{path}: expected a list")
    items = []
    for index, entry in enumerate(value):
        where = f"{path}[{index}]"
        if not isinstance(entry, dict) or "label" not in entry:
            raise ConfigError(f"{where}: needs at least `label`")
        children = _parse_items(f"{where}.items", entry["items"]) if "items" in entry else ()
        source = entry.get("source")
        if source is not None and source not in SOURCES:
            raise ConfigError(f"{where}.source: expected one of {SOURCES}")
        if sum(map(bool, (children, entry.get("action"), source))) != 1:
            raise ConfigError(f"{where}: needs exactly one of `action`, `items` or `source`")
        if entry.get("inline") and not source:
            raise ConfigError(f"{where}: `inline` needs a `source`")
        items.append(MenuItem(
            label=str(entry["label"]),
            icon=str(entry.get("icon", "")),
            action=entry.get("action"),
            keep_open=bool(entry.get("keep_open", False)),
            items=children,
            source=source,
            inline=bool(entry.get("inline", False)),
        ))
    if len(items) > 12:
        raise ConfigError(f"{path}: at most 12 items fit a radial menu")
    return tuple(items)


def _validate_actions(path: str, actions: list[str], radials: dict[str, Radial]) -> None:
    for action in actions:
        kind, _, arg = action.partition(":")
        if kind == "radial" and arg not in radials:
            raise ConfigError(f"{path}: unknown radial menu {arg!r}")
        if kind not in ACTION_KINDS:
            raise ConfigError(f"{path}: unknown action type in {action!r}")


def resolve_preset(raw: dict[str, Any]) -> str:
    preset = raw.get("preset", "auto")
    if preset not in ("auto", "hyprland", "omarchy"):
        raise ConfigError(f"preset: expected auto | hyprland | omarchy, got {preset!r}")
    if preset == "auto":
        return "omarchy" if shutil.which("omarchy") else "hyprland"
    return preset


def build(raw: dict[str, Any], preset: str, sources: list[str]) -> Config:
    radials: dict[str, Radial] = {}
    for name, entry in raw.get("radial", {}).items():
        source = entry.get("source")
        if source is not None and source not in SOURCES:
            raise ConfigError(f"radial.{name}.source: expected one of {SOURCES}")
        items = _parse_items(f"radial.{name}.items", entry.get("items", [])) if source is None else ()
        radials[name] = Radial(name=name, title=str(entry.get("title", name)), source=source, items=items)
    for name, radial in radials.items():
        _validate_item_actions(f"radial.{name}", radial.items, radials)

    layers: dict[str, dict[str, Binding]] = {}
    for layer, table in raw.get("bindings", {}).items():
        parsed = {}
        for button, value in table.items():
            path = f"bindings.{layer}.{button}"
            if button not in BUTTONS:
                raise ConfigError(f"{path}: unknown button (valid: {', '.join(BUTTONS)})")
            if value == "":
                continue  # explicit unbind of a default
            parsed[button] = _parse_binding(path, value)
            _validate_actions(path, parsed[button].actions(), radials)
        layers[layer] = parsed
    if "normal" not in layers:
        raise ConfigError("bindings.normal is required")

    modifiers = dict(raw.get("modifiers", {}))
    for button, layer in modifiers.items():
        if button not in BUTTONS:
            raise ConfigError(f"modifiers.{button}: unknown button")
        if layer not in layers:
            raise ConfigError(f"modifiers.{button}: no [bindings.{layer}] table")
        own = layers["normal"].get(button)
        if own is not None and own.repeat:
            raise ConfigError(f"bindings.normal.{button}: a modifier can't auto-repeat")

    for side in ("left", "right"):
        if raw["sticks"][side] not in STICK_MODES:
            raise ConfigError(f"sticks.{side}: expected one of {STICK_MODES}")
    if raw["touchpad"]["mode"] not in ("mouse", "system"):
        raise ConfigError("touchpad.mode: expected mouse | system")
    if raw["touchpad"]["click"] not in ("mouse", "button"):
        raise ConfigError("touchpad.click: expected mouse | button")
    if raw["lights"]["alert_style"] not in ("blink", "breathe"):
        raise ConfigError("lights.alert_style: expected blink | breathe")
    if raw["sounds"]["device"] not in ("auto", "controller", "system"):
        raise ConfigError("sounds.device: expected auto | controller | system")
    _regex("dictation.terminal_classes", raw["dictation"]["terminal_classes"])
    _regex("agents.window_classes", raw["agents"]["window_classes"])
    _regex("agents.window_titles", raw["agents"]["window_titles"])
    if raw["agents"]["thinking_elsewhere"]:
        _validate_actions("agents.thinking_elsewhere", [raw["agents"]["thinking_elsewhere"]], radials)
    for key in ("pause_classes", "pause_titles"):
        _regex(f"game.{key}", raw["game"][key])
    _regex("menu.namespaces", raw["menu"]["namespaces"])
    _regex("devices.match", raw["devices"]["match"])
    _regex("devices.ignore", raw["devices"]["ignore"])
    zoom = raw["zoom"]
    for path, combos in [("zoom.keys", zoom["keys"]), *((f"zoom.classes.{k!r}", v) for k, v in zoom["classes"].items())]:
        if not (isinstance(combos, list) and len(combos) == 3 and all(isinstance(c, str) and c for c in combos)):
            raise ConfigError(f"{path}: expected three key combos [bigger, smaller, reset]")
    for pattern in zoom["classes"]:
        _regex(f"zoom.classes.{pattern!r}", pattern)
    couch = raw["couch"]
    if isinstance(couch["scale"], bool) or not isinstance(couch["scale"], (int, float)) or couch["scale"] <= 0:
        raise ConfigError("couch.scale: expected a number > 0 (multiplier of the monitor's scale)")
    if not isinstance(couch["monitors"], list) or not all(isinstance(name, str) for name in couch["monitors"]):
        raise ConfigError("couch.monitors: expected a list of monitor names ([] = the focused monitor)")
    if not isinstance(couch["bar_hide"], list) or not all(isinstance(name, str) for name in couch["bar_hide"]):
        raise ConfigError("couch.bar_hide: expected a list of bar widget ids")
    for key in ("enter", "exit"):
        if not isinstance(couch[key], list) or not all(isinstance(action, str) for action in couch[key]):
            raise ConfigError(f"couch.{key}: expected a list of actions")
        _validate_actions(f"couch.{key}", couch[key], radials)
    text = raw["text"]
    stops = text["stops"]
    if not (isinstance(stops, list) and len(stops) >= 2 and all(isinstance(s, (int, float)) for s in stops)
            and stops == sorted(stops)):
        raise ConfigError("text.stops: expected at least two ascending numbers")
    if "{size}" not in text["set"]:
        raise ConfigError("text.set: needs a {size} placeholder")
    brightness = raw["brightness"]
    if "{value}" not in brightness["set"]:
        raise ConfigError("brightness.set: needs a {value} placeholder (percent)")
    if isinstance(brightness["step"], bool) or not isinstance(brightness["step"], (int, float)) or brightness["step"] <= 0:
        raise ConfigError("brightness.step: expected a number > 0 (percent)")
    foot = raw["foot"]
    if not isinstance(foot["after"], list) or not all(isinstance(action, str) for action in foot["after"]):
        raise ConfigError("foot.after: expected a list of actions")
    _validate_actions("foot.after", foot["after"], radials)
    _validate_favorites(raw, radials)
    launch = raw["apps"]["launch"]
    if not isinstance(launch, str) or not any(f"{{{key}}}" in launch for key in ("id", "path")):
        raise ConfigError("apps.launch: expected a command with an {id} (desktop id) or {path} placeholder")
    try:
        launch.format(id="x", path="x")
    except (KeyError, IndexError, ValueError) as error:
        raise ConfigError(f"apps.launch: only {{id}} and {{path}} get filled in, not {error}") from None
    period = raw["lights"]["blink_period"]
    if isinstance(period, bool) or not isinstance(period, (int, float)) or period <= 0:
        raise ConfigError("lights.blink_period: expected seconds > 0")

    return Config(raw=raw, preset=preset, layers=layers, modifiers=modifiers, radials=radials, sources=sources)


def _regex(path: str, pattern: str) -> None:
    try:
        re.compile(pattern)
    except re.error as error:
        raise ConfigError(f"{path}: invalid regex: {error}") from None


def _validate_favorites(raw: dict[str, Any], radials: dict[str, Radial]) -> None:
    favorites = raw["favorites"]
    slots = favorites["slots"]
    if isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= 12:
        raise ConfigError("favorites.slots: expected a whole number from 1 to 12")
    items = favorites["items"]
    if not isinstance(items, list) or len(items) > slots:
        raise ConfigError(f"favorites.items: expected a list of at most favorites.slots ({slots}) entries")
    for index, entry in enumerate(items):
        where = f"favorites.items[{index}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("action"), str) or not entry["action"]:
            raise ConfigError(f"{where}: needs an `action`")
        unknown = set(entry) - FAVORITE_KEYS
        if unknown:
            raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
        if any(key in entry and not isinstance(entry[key], str) for key in ("label", "icon")):
            raise ConfigError(f"{where}: `label` and `icon` are text")
        if not entry.get("label") and not entry["action"].startswith("launch:"):
            raise ConfigError(f"{where}: needs a `label` (only launch: takes the app's own name)")
        _validate_actions(where, [entry["action"]], radials)
    if not isinstance(favorites["picker"], str) or not favorites["picker"].strip():
        raise ConfigError("favorites.picker: expected a command (a dmenu-style list)")


def _validate_item_actions(path: str, items: tuple[MenuItem, ...], radials: dict[str, Radial]) -> None:
    for index, item in enumerate(items):
        if item.action:
            _validate_actions(f"{path}[{index}]", [item.action], radials)
        _validate_item_actions(f"{path}[{index}].items", item.items, radials)


def load(path: str | None = None) -> Config:
    """Defaults, preset, then `path` (default: the user config, which may be absent). A file named
    explicitly has to exist: silently running on the defaults instead would match any DualSense."""
    if path is not None and not os.path.exists(path):
        raise ConfigError(f"{path}: no such file")
    path = path or USER_CONFIG
    raw = _default("base")
    sources = ["defaults/base.toml"]
    user: dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, "rb") as handle:
                user = tomllib.load(handle)
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"{path}: {error}") from None
    preset = resolve_preset({**raw, **user})
    if preset == "omarchy":
        raw = deep_merge(raw, _default("omarchy"))
        sources.append("defaults/omarchy.toml")
    if user:
        raw = deep_merge(raw, user)
        sources.append(path)
    return build(raw, preset, sources)
