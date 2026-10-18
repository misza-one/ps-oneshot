"""Human-readable names for controls and bindings (cheat sheet)."""

from __future__ import annotations

import os

from .actions import describe
from .config import Binding, Config, MenuItem

GLYPHS = {
    "cross": "✕", "circle": "○", "triangle": "△", "square": "□",
    "l1": "L1", "r1": "R1", "l2": "L2", "r2": "R2", "l3": "L3", "r3": "R3",
    "create": "Create", "options": "Options", "ps": "PS", "mic": "Mic", "touchpad": "Touchpad",
    "dpad_up": "D-pad ↑", "dpad_down": "D-pad ↓", "dpad_left": "D-pad ←", "dpad_right": "D-pad →",
    "lstick_up": "L-stick ↑", "lstick_down": "L-stick ↓", "lstick_left": "L-stick ←", "lstick_right": "L-stick →",
    "rstick_up": "R-stick ↑", "rstick_down": "R-stick ↓", "rstick_left": "R-stick ←", "rstick_right": "R-stick →",
}
DIRECTIONAL = {"dpad": "D-pad", "lstick": "L-stick", "rstick": "R-stick"}
SOURCE_TEXT = {
    "herd": "Agents grouped by machine; ✕ goes to one",
    "audio": "Outputs and inputs; ✕ makes one the default",
    "voxtype-language": "Dictation language; the current one ✓",
    "settings": "Couch mode · Sounds · Effects +/− · Vibration · Mouse speed +/− · Lightbar › · Cheat sheet · Pause",
    "lights": "Lightbar colours · Alert style · Brightness +/− · Idle glow +/− · Running light",
    "favorites": "Your apps; change them right in the ring (see In a radial)",
}
# Rows follow the controller: shoulders and D-pad, sticks and touchpad, face buttons, triggers, the rest.
ORDER = (
    "l1", "r1", "dpad", "lstick", "rstick", "touchpad", "cross", "circle", "square", "triangle",
    "l2", "r2", "l3", "r3", "ps", "create", "options", "mic",
)
STICK_TEXT = {"scroll": "Scroll", "pointer": "Mouse pointer"}
PAIRS = {  # direction → (first, second, arrows) of the opposite pair it belongs to
    "up": ("up", "down", "↑ ↓"), "down": ("up", "down", "↑ ↓"),
    "left": ("left", "right", "← →"), "right": ("left", "right", "← →"),
}

Section = tuple[str, list[tuple[str, str]]]


def radial_sections(cfg: Config) -> list[Section]:
    """Where each radial menu lives and what's inside, e.g. "L3 · Omarchy" → Dictation › language."""
    sections: list[Section] = []
    live: list[tuple[str, str]] = []
    for button, binding in cfg.layers["normal"].items():
        for how, action in (("", binding.press), ("", binding.tap), (" hold", binding.hold)):
            if not action or not action.startswith("radial:"):
                continue
            radial = cfg.radials[action.removeprefix("radial:")]
            opener = GLYPHS.get(button, button) + how
            if radial.source:
                live.append((opener, SOURCE_TEXT[radial.source]))
            else:
                sections.append((f"{opener} · {radial.title}", radial_rows(radial.items)))
    if live:
        sections.append(("Live radials", live))
    return sections


def radial_rows(items: tuple[MenuItem, ...]) -> list[tuple[str, str]]:
    """Plain actions share one row; every submenu or live list gets its own."""
    plain = [item.label for item in items if item.action]
    rows = [("Items", " · ".join(plain))] if plain else []
    for item in items:
        if item.items:
            rows.append((f"{item.label} ›", " · ".join(child.label for child in item.items)))
        elif item.source:
            key = item.label if item.inline else f"{item.label} ›"  # inline choices sit in the ring itself
            rows.append((key, SOURCE_TEXT[item.source]))
    return rows


def uses_source(items: tuple[MenuItem, ...], source: str) -> bool:
    return any(item.source == source or uses_source(item.items, source) for item in items)


def binding_text(binding: Binding, titles: dict[str, str]) -> str:
    if binding.desc:
        return binding.desc
    parts = []
    for prefix, action in (
        ("", binding.press), ("tap: ", binding.tap), ("hold: ", binding.hold),
        ("soft: ", binding.soft), ("full: ", binding.full),
    ):
        if action:
            parts.append(prefix + describe(action, titles))
    return " · ".join(parts)


def _rank(button: str) -> int:
    prefix = button.partition("_")[0]
    group = prefix if prefix in DIRECTIONAL else button
    return ORDER.index(group) if group in ORDER else len(ORDER)


def cheatsheet_rows(
    bindings: dict[str, Binding], titles: dict[str, str], extra: dict[str, str] | None = None
) -> list[tuple[str, str]]:
    """One row per binding in controller order. Four same-kind directions (focus ←→↑↓, arrow keys)
    collapse into one row, otherwise opposite pairs do ("Volume up / down"). `extra` adds controls
    that work without a binding (stick, touchpad name → text)."""
    rows: list[tuple[int, str, str]] = []
    done: set[str] = set()
    for button, binding in bindings.items():
        if button in done:
            continue
        prefix, _, direction = button.partition("_")
        if prefix in DIRECTIONAL:
            group = [f"{prefix}_{d}" for d in ("up", "down", "left", "right")]
            summary = None
            if all(b in bindings for b in group):
                summary = collapse_directions([binding_text(bindings[b], titles) for b in group])
            if summary:
                rows.append((_rank(button), DIRECTIONAL[prefix], summary))
                done.update(group)
                continue
            first, second, arrows = PAIRS[direction]
            pair = [f"{prefix}_{first}", f"{prefix}_{second}"]
            if all(b in bindings for b in pair):
                texts = [binding_text(bindings[b], titles) for b in pair]
                rows.append((_rank(button), f"{DIRECTIONAL[prefix]} {arrows}", pair_text(*texts)))
                done.update(pair)
                continue
        rows.append((_rank(button), GLYPHS.get(button, button), binding_text(binding, titles)))
    for name, text in (extra or {}).items():
        rows.append((_rank(name), DIRECTIONAL.get(name, GLYPHS.get(name, name)), text))
    rows.sort(key=lambda row: row[0])  # stable: same-rank rows keep their order
    return [(key, text) for _, key, text in rows]


def unbound_controls(cfg: Config) -> dict[str, str]:
    """Controls that work without a binding: sticks in scroll/pointer mode and the touchpad mouse."""
    extra = {}
    for side, name in (("left", "lstick"), ("right", "rstick")):
        mode = cfg.raw["sticks"][side]
        if mode in STICK_TEXT:
            extra[name] = STICK_TEXT[mode]
    touchpad = cfg.raw["touchpad"]
    if touchpad["mode"] == "mouse":
        parts = ["Pointer", "2 fingers: scroll"]
        clicks = [how for how, on in (("press", touchpad["click"] == "mouse"), ("tap", touchpad["tap_to_click"])) if on]
        if clicks:
            right = " (2 fingers: right click)" if touchpad["click"] == "mouse" else ""
            parts.append("/".join(clicks) + ": click" + right)
        extra["touchpad"] = " · ".join(parts)
    return extra


def collapse_directions(texts: list[str]) -> str | None:
    """["Up","Down","Left","Right"] → "Arrow keys"; ["Focus window up", …] → "Focus window". Four actions
    that merely start alike (Ctrl+C, Ctrl+V, …) don't collapse: None."""
    words = ["up", "down", "left", "right"]
    if [t.lower() for t in texts] == words:
        return "Arrow keys"
    prefix = os.path.commonprefix(texts)
    if prefix.strip() and [text[len(prefix):].strip().lower() for text in texts] == words:
        return prefix.strip()
    return None


def pair_text(first: str, second: str) -> str:
    """"Volume up" + "Volume down" → "Volume up / down": words both share at the start or end once."""
    prefix = os.path.commonprefix([first, second])
    prefix = prefix[: prefix.rfind(" ") + 1]
    suffix = os.path.commonprefix([first[::-1], second[::-1]])[::-1]
    suffix = suffix[suffix.find(" "):] if " " in suffix else ""
    a, b = first[len(prefix): len(first) - len(suffix)], second[len(prefix): len(second) - len(suffix)]
    return f"{prefix}{a} / {b}{suffix}" if a and b else f"{first} / {second}"


def cheatsheet_sections(cfg: Config) -> list[Section]:
    titles = {name: radial.title for name, radial in cfg.radials.items()}
    modifier_titles = {layer: f"{GLYPHS.get(button, button)} + button" for button, layer in cfg.modifiers.items()}
    sections: list[Section] = []
    for layer, bindings in cfg.layers.items():
        title = {"normal": "Desktop", "menu": "In menus"}.get(layer, modifier_titles.get(layer, layer))
        if layer != "normal":
            sections.append((title, cheatsheet_rows(bindings, titles)))
            continue
        # From L2 on (talking, radials, agents, system buttons) the desktop rows get a section of
        # their own, so the columns can balance.
        split = ORDER.index("l2")
        moving = {b: binding for b, binding in bindings.items() if _rank(b) < split}
        rest = {b: binding for b, binding in bindings.items() if _rank(b) >= split}
        rows = cheatsheet_rows(moving, titles, unbound_controls(cfg))
        rows += [
            (GLYPHS.get(b, b), f"{target.title()} layer (hold)")
            for b, target in cfg.modifiers.items() if b not in bindings
        ]
        sections.append((title, rows))
        if rest:
            sections.append(("Voice, agents & menus", cheatsheet_rows(rest, titles)))
    controls = [("Stick", "Select"), ("✕", "Confirm"), ("○", "Back / close"), ("D-pad ← →", "Step selection")]
    radials = cfg.radials.values()
    if any(radial.source == "herd" or uses_source(radial.items, "herd") for radial in radials):
        controls.append(("△ (hold)", "Agent: close it"))
    if any(radial.source == "favorites" or uses_source(radial.items, "favorites") for radial in radials):
        controls += [
            ("✕ on Add", "Favorite: pick an app"), ("□ (hold)", "Favorite: pin the current window"),
            ("△ (hold)", "Favorite: remove"), ("L1 / R1", "Favorite: move"),
        ]
    sections.append(("In a radial", controls))
    toggles = [GLYPHS.get(b, b) for b, binding in cfg.layers["normal"].items() if binding.tap == "ui:cheatsheet"]
    sections.append(("In this sheet", [
        ("Stick", "Scroll"), ("D-pad ↑ ↓", "Scroll a step"), (" / ".join(["○", *toggles]), "Close"),
    ]))
    return sections + radial_sections(cfg)
