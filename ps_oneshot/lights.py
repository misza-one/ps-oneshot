"""Lightbar + player-LED state: one priority-ordered signal wins, with the deck's breathing pulse."""

from __future__ import annotations

import math
from dataclasses import dataclass

RGB = tuple[int, int, int]

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
    waiting: int = 0
    battery_low: bool = False
    paused: bool = False


@dataclass(frozen=True)
class LightFrame:
    rgb: RGB
    brightness: float
    player_mask: int
    animated: bool


def breath(now: float, period: float = 1.9) -> float:
    """0.18..1.0 cosine pulse; same period as Omaherd's working dots."""
    phase = (now % period) / period
    return 0.18 + 0.82 * (0.5 + 0.5 * math.cos(phase * 2 * math.pi))


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
        self.idle_rgb = idle_rgb

    def frame(self, state: LightInputs, now: float) -> LightFrame | None:
        """None means: leave the LEDs alone (paused, so games own them)."""
        if state.paused or not self._cfg["enabled"]:
            return None
        top = float(self._cfg["brightness"])
        idle = float(self._cfg["idle_brightness"])
        mask = player_mask(state.waiting)
        if state.recording:
            period = float(self._cfg["recording_pulse"])
            if period <= 0:
                return LightFrame(tuple(self._cfg["recording"]), top, mask, False)
            return LightFrame(tuple(self._cfg["recording"]), top * (0.3 + 0.7 * breath(now, period)), mask, True)
        if state.herd_status in ("blocked", "done"):
            return LightFrame(tuple(self._cfg[state.herd_status]), top * self._alert_level(now), mask, True)
        if state.battery_low:
            blink = 1.0 if (now % 2.0) < 0.35 else idle
            return LightFrame(tuple(self._cfg["battery_low"]), top * blink, mask, True)
        if state.herd_status == "working":
            return LightFrame(self.idle_rgb, top * (idle + (0.4 - idle) * breath(now)), mask, True)
        return LightFrame(self.idle_rgb, top * idle, mask, False)

    def _alert_level(self, now: float) -> float:
        """Agent alerts: hard on/off blink (default) or the deck's breathing pulse."""
        if self._cfg["alert_style"] == "blink":
            period = float(self._cfg["blink_period"])
            return 1.0 if (now % period) < period / 2 else 0.0
        return 0.22 + 0.78 * breath(now)
