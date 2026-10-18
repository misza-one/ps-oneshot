"""Lightbar + player-LED state: one priority-ordered signal wins, with the deck's breathing pulse."""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass, replace
from functools import lru_cache

RGB = tuple[int, int, int]

BREATH_PERIOD_S = 1.9  # Omaherd's working dots
# An agent's hue closer than this to its alert's can't be told apart on the lightbar (Devin's or Kimi's yellow
# on a done alert): the alert blinks dark instead of alternating.
ALTERNATE_MIN_HUE_DEG = 15
NEUTRAL_SATURATION = 0.15  # below this a colour has no hue to speak of (white, greys)
SCANNER = (0, 1, 2, 3, 4, 3, 2, 1)  # player LEDs left to right and back, one BREATH_PERIOD_S per sweep
ALERT_STYLES = ("alternate", "blink", "breathe")
# The Lightbar ring's steppers: [lights] brightness and idle_brightness.
BRIGHTNESS_STOPS = (0.2, 0.4, 0.6, 0.8, 1.0)
IDLE_STOPS = (0.0, 0.04, 0.08, 0.12, 0.2, 0.3)
# What the Lightbar ring changes, remembered as state key `lights` (only the keys changed there).
RING_SETTINGS = ("vivid", "scanner", "alert_style", "brightness", "idle_brightness")

# (strong, weak, duration_ms, pause_after_ms)
RUMBLE_BLOCKED = ((1.0, 0.6, 110, 90), (1.0, 0.6, 110, 0))
RUMBLE_DONE = ((0.0, 0.75, 90, 0),)
RUMBLE_TICK = ((0.6, 0.6, 40, 0),)  # shorter pulses barely move the DualSense's actuators, whatever their strength
RUMBLE_CONFIRM = ((0.35, 0.0, 40, 0),)
RUMBLE_NOTHING = ((0.0, 0.3, 30, 60), (0.0, 0.3, 30, 0))
RUMBLE_PAUSE = ((0.6, 0.0, 70, 70), (0.3, 0.0, 70, 0))
RUMBLE_SHOT = ((1.0, 0.9, 55, 0), (0.55, 0.2, 70, 0), (0.2, 0.0, 60, 0))  # recoil: kick, then fading


@dataclass(frozen=True)
class LightInputs:
    recording: bool = False
    herd_status: str | None = None  # blocked | done | working | None
    alert_rgb: RGB | None = None  # the colour of the agent a blocked/done alert is about (the one PS jumps to)
    waiting: int = 0
    battery_low: bool = False
    paused: bool = False


@dataclass(frozen=True)
class LightFrame:
    rgb: RGB
    brightness: float
    player_mask: int
    animated: bool


def breath(now: float, period: float = BREATH_PERIOD_S) -> float:
    """0.18..1.0 cosine pulse; same period as Omaherd's working dots."""
    phase = (now % period) / period
    return 0.18 + 0.82 * (0.5 + 0.5 * math.cos(phase * 2 * math.pi))


def scanner_mask(now: float) -> int:
    """One player LED running back and forth, in step with the lightbar's breathing."""
    phase = (now % BREATH_PERIOD_S) / BREATH_PERIOD_S
    return 1 << SCANNER[int(phase * len(SCANNER)) % len(SCANNER)]


@lru_cache(maxsize=64)
def vivid(rgb: RGB) -> RGB:
    """A screen colour for the lightbar: its hue at full saturation and full value, the way RGB-sync tools light
    a PC from the theme (#faa968 → ff7200). A pastel carries white, and a dimmed LED renders that white bluish:
    the peach accent glowed lilac at the idle brightness. Greys keep their (lack of) hue, at full value."""
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    if v == 0:
        return 255, 255, 255
    if s < NEUTRAL_SATURATION:
        r, g, b = (c / v for c in (rgb[0] / 255, rgb[1] / 255, rgb[2] / 255))
    else:
        r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
    return round(r * 255), round(g * 255), round(b * 255)


def look_alike(a: RGB, b: RGB) -> bool:
    """Two lightbar colours you couldn't tell apart in an alternation: hues too close, or both without one."""
    ha, sa, _ = colorsys.rgb_to_hsv(*(c / 255 for c in a))
    hb, sb, _ = colorsys.rgb_to_hsv(*(c / 255 for c in b))
    if sa < NEUTRAL_SATURATION or sb < NEUTRAL_SATURATION:
        return sa < NEUTRAL_SATURATION and sb < NEUTRAL_SATURATION
    gap = abs(ha - hb) % 1.0
    return min(gap, 1.0 - gap) * 360 < ALTERNATE_MIN_HUE_DEG


def restore_settings(cfg: dict, saved: object) -> None:
    """Put the settings changed in the Lightbar ring (state.json) over the config's, skipping bad values."""
    if not isinstance(saved, dict):
        return
    for key in RING_SETTINGS:
        value = saved.get(key)
        if key in ("vivid", "scanner"):
            ok = isinstance(value, bool)
        elif key == "alert_style":
            ok = value in ALERT_STYLES
        else:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 <= value <= 1.0
        if ok:
            cfg[key] = value


def player_mask(count: int) -> int:
    """Fill the five player LEDs from the centre out: 1 → centre, 2 → inner pair, …"""
    order = (2, 1, 3, 0, 4)
    mask = 0
    for index in order[: max(0, min(count, 5))]:
        mask |= 1 << index
    return mask


class LightEngine:
    def __init__(self, cfg: dict, idle_rgb: RGB) -> None:
        self._cfg = cfg
        self.idle_rgb = idle_rgb  # the theme accent, as on screen

    def frame(self, state: LightInputs, now: float) -> LightFrame | None:
        """None means: leave the LEDs alone (paused, so games own them). The player LEDs count the waiting agents;
        while none waits and some work, one of them runs back and forth (`scanner`)."""
        if state.paused or not self._cfg["enabled"]:
            return None
        scanning = state.herd_status == "working" and bool(self._cfg["scanner"])
        frame = self._lightbar(state, scanner_mask(now) if scanning else player_mask(state.waiting), now)
        return replace(frame, animated=True) if scanning else frame

    def _screen(self, rgb: RGB) -> RGB:
        """Colours taken from screen palettes (the accent, agent kinds); configured colours go out as written."""
        return vivid(rgb) if self._cfg["vivid"] else rgb

    def _lightbar(self, state: LightInputs, mask: int, now: float) -> LightFrame:
        top = float(self._cfg["brightness"])
        idle = float(self._cfg["idle_brightness"])
        accent = self._screen(self.idle_rgb)
        if state.recording:
            period = float(self._cfg["recording_pulse"])
            if period <= 0:
                return LightFrame(tuple(self._cfg["recording"]), top, mask, False)
            return LightFrame(tuple(self._cfg["recording"]), top * (0.3 + 0.7 * breath(now, period)), mask, True)
        if state.herd_status in ("blocked", "done"):
            agent = self._screen(state.alert_rgb) if state.alert_rgb is not None else None
            return self._alert(tuple(self._cfg[state.herd_status]), agent, top, mask, now)
        if state.battery_low:
            blink = 1.0 if (now % 2.0) < 0.35 else idle
            return LightFrame(tuple(self._cfg["battery_low"]), top * blink, mask, True)
        if state.herd_status == "working":
            return LightFrame(accent, top * (idle + (0.4 - idle) * breath(now)), mask, True)
        return LightFrame(accent, top * idle, mask, False)

    def _alert(self, status: RGB, agent: RGB | None, top: float, mask: int, now: float) -> LightFrame:
        """Agent alerts. alternate (default): the alert's colour for the first half of each blink period, the
        waiting agent's own colour for the second, so the lightbar says who is waiting (Claude's orange,
        Codex's green); blink: the alert's colour, then dark; breathe: the deck's breathing pulse."""
        style = self._cfg["alert_style"]
        if style == "breathe":
            return LightFrame(status, top * (0.22 + 0.78 * breath(now)), mask, True)
        period = float(self._cfg["blink_period"])
        if (now % period) < period / 2:
            return LightFrame(status, top, mask, True)
        if style == "alternate" and agent is not None and not look_alike(agent, status):
            return LightFrame(agent, top, mask, True)
        return LightFrame(status, 0.0, mask, True)
