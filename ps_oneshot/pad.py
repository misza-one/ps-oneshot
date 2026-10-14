"""One connected controller: decode evdev/hidraw into logical buttons, sticks and touchpad gestures."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from gi.repository import GLib

from .linux import evdev as ev
from .linux.dualsense import Controller, MicButtonReader
from .linux.uinput import VirtualPointer

log = logging.getLogger(__name__)

BUTTON_CODES = {
    ev.BTN_SOUTH: "cross", ev.BTN_EAST: "circle", ev.BTN_NORTH: "triangle", ev.BTN_WEST: "square",
    ev.BTN_TL: "l1", ev.BTN_TR: "r1", ev.BTN_TL2: "l2", ev.BTN_TR2: "r2",
    ev.BTN_THUMBL: "l3", ev.BTN_THUMBR: "r3",
    ev.BTN_SELECT: "create", ev.BTN_START: "options", ev.BTN_MODE: "ps",
}
STICK_AXES = {ev.ABS_X: ("left", 0), ev.ABS_Y: ("left", 1), ev.ABS_RX: ("right", 0), ev.ABS_RY: ("right", 1)}
TRIGGER_AXES = {ev.ABS_Z: "l2", ev.ABS_RZ: "r2"}
STICK_PREFIX = {"left": "lstick", "right": "rstick"}
STICK_RELEASE_RATIO = 0.65  # hysteresis for stick buttons: release below this share of the press threshold
TICK_MS = 16
TAP_MAX_S = 0.22
TAP_MAX_TRAVEL = 40  # touchpad units (pad is 1920 wide)


def shaped(x: float, y: float, deadzone: float) -> tuple[float, float]:
    """Radial deadzone with a quadratic response curve; output magnitude 0..1."""
    magnitude = math.hypot(x, y)
    if magnitude <= deadzone:
        return 0.0, 0.0
    scaled = min(1.0, (magnitude - deadzone) / (1.0 - deadzone)) ** 2
    return x / magnitude * scaled, y / magnitude * scaled


def stick_direction(x: float, y: float, threshold: float, current: str | None) -> str | None:
    """Dominant direction once past `threshold`; a held direction releases only below a lower mark."""
    magnitude = math.hypot(x, y)
    if current is not None and magnitude >= threshold * STICK_RELEASE_RATIO:
        horizontal = abs(x) >= abs(y)
        candidate = ("right" if x > 0 else "left") if horizontal else ("down" if y > 0 else "up")
        return candidate if magnitude >= threshold or candidate == current else current
    if magnitude < threshold:
        return None
    if abs(x) >= abs(y):
        return "right" if x > 0 else "left"
    return "down" if y > 0 else "up"


@dataclass
class Touch:
    slot: int = 0
    points: dict[int, list[int]] = field(default_factory=dict)  # slot → [x, y]
    last: dict[int, tuple[int, int]] = field(default_factory=dict)
    started: float = 0.0
    travel: float = 0.0
    fingers: int = 0
    clicked: bool = False
    scroll_rest: float = 0.0


class Pad:
    def __init__(
        self,
        controller: Controller,
        cfg: dict,
        pointer: VirtualPointer | None,
        on_button: Callable[[Pad, str, bool], None],
        on_trigger: Callable[[Pad, str, float], None],
        on_stick: Callable[[Pad], None],
        on_lost: Callable[[Pad], None],
    ) -> None:
        self.controller = controller
        self.cfg = cfg
        self.pointer = pointer
        self.on_button = on_button
        self.on_trigger = on_trigger
        self.on_stick = on_stick
        self.on_lost = on_lost
        self.sticks = {"left": [0.0, 0.0], "right": [0.0, 0.0]}
        self.sticks_captured = False  # radial menu owns the sticks
        self.stick_buttons: set[str] = set()  # sides acting as buttons whatever their mode (a held layer binds them)
        self.active = False
        self.triggers = {"l2": 0.0, "r2": 0.0}
        self._ranges = {
            code: controller.gamepad.abs_info(code)
            for code in (*STICK_AXES, *TRIGGER_AXES)
            if controller.gamepad.has_abs(code)
        }
        self._stick_dirs: dict[str, str | None] = {"left": None, "right": None}
        self._hat = [0, 0]
        self._watches: list[int] = []
        self._tick = 0
        self._scroll_rest = [0.0, 0.0]
        self._pointer_rest = [0.0, 0.0]
        self._touch = Touch()
        self._mic: MicButtonReader | None = None
        self._grabbed = False
        self._click_button = 0  # mouse button held by a physical touchpad press

    @property
    def name(self) -> str:
        return self.controller.name

    def attach(self) -> None:
        self._watch(self.controller.gamepad.fd, self._on_gamepad)
        if self.controller.touchpad is not None:
            self._watch(self.controller.touchpad.fd, self._on_touchpad)
        if self.controller.hidraw:
            try:
                self._mic = MicButtonReader(self.controller.hidraw)
                self._watch(self._mic.fd, self._on_hidraw)
            except OSError as error:
                log.info("mic button unavailable (%s: %s)", self.controller.hidraw, error.strerror)

    def _watch(self, fd: int, handler: Callable[[], None]) -> None:
        def callback(_fd: int, _cond: GLib.IOCondition) -> bool:
            try:
                handler()
            except OSError:
                self.on_lost(self)
                return False
            return True

        self._watches.append(GLib.unix_fd_add_full(
            GLib.PRIORITY_HIGH, fd, GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR, callback
        ))

    def close(self) -> None:
        for watch in self._watches:
            GLib.source_remove(watch)
        self._watches.clear()
        if self._tick:
            GLib.source_remove(self._tick)
            self._tick = 0
        if self._mic is not None:
            self._mic.close()
        self.active = False  # closing the fd releases any grab
        self.controller.close()

    def set_active(self, active: bool) -> None:
        """Active = we own the controller (not paused). Touchpad grab follows it in mouse mode."""
        self.active = active
        want_grab = active and self.pointer is not None and self.cfg["touchpad"]["mode"] == "mouse"
        touchpad = self.controller.touchpad
        if touchpad is not None and want_grab != self._grabbed:
            try:
                touchpad.grab(want_grab)
                self._grabbed = want_grab
            except OSError as error:
                log.warning("touchpad grab failed: %s", error)
        if not active:
            self.sticks = {"left": [0.0, 0.0], "right": [0.0, 0.0]}
            self._update_stick_buttons()
            if self._click_button:
                self._on_touchpad_press(False)  # don't leave a mouse button stuck down

    def capture_sticks(self, captured: bool) -> None:
        """The radial menu owns the sticks while open: no scrolling, no stick buttons."""
        self.sticks_captured = captured
        self._update_stick_buttons()

    def force_stick_buttons(self, sides: set[str]) -> None:
        """Turn scroll/pointer sticks into buttons for now, e.g. while ○ is held and its layer binds them."""
        self.stick_buttons = set(sides)
        self._update_stick_buttons()
        self._ensure_tick()

    # gamepad -----------------------------------------------------------------

    def _normalise(self, code: int, value: int) -> float:
        info = self._ranges[code]
        if code in TRIGGER_AXES:
            return max(0.0, min(1.0, (value - info.minimum) / max(1, info.maximum - info.minimum)))
        centre = (info.maximum + info.minimum) / 2
        half = max(1.0, (info.maximum - info.minimum) / 2)
        return max(-1.0, min(1.0, (value - centre) / half))

    def _on_gamepad(self) -> None:
        stick_moved = False
        for event in self.controller.gamepad.read_events():
            if event.type == ev.EV_KEY and event.code in BUTTON_CODES and event.value in (0, 1):
                self.on_button(self, BUTTON_CODES[event.code], bool(event.value))
            elif event.type == ev.EV_ABS and event.code in STICK_AXES and event.code in self._ranges:
                stick, axis = STICK_AXES[event.code]
                self.sticks[stick][axis] = self._normalise(event.code, event.value)
                stick_moved = True
            elif event.type == ev.EV_ABS and event.code in TRIGGER_AXES and event.code in self._ranges:
                name = TRIGGER_AXES[event.code]
                self.triggers[name] = self._normalise(event.code, event.value)
                self.on_trigger(self, name, self.triggers[name])
            elif event.type == ev.EV_ABS and event.code in (ev.ABS_HAT0X, ev.ABS_HAT0Y):
                self._on_hat(0 if event.code == ev.ABS_HAT0X else 1, event.value)
        if stick_moved:
            self.on_stick(self)
            self._update_stick_buttons()
            self._ensure_tick()

    def _on_hat(self, axis: int, value: int) -> None:
        names = (("dpad_left", "dpad_right"), ("dpad_up", "dpad_down"))[axis]
        previous = self._hat[axis]
        if previous == value:
            return
        if previous:
            self.on_button(self, names[previous > 0], False)
        self._hat[axis] = value
        if value:
            self.on_button(self, names[value > 0], True)

    def _on_hidraw(self) -> None:
        assert self._mic is not None
        for pressed in self._mic.read_transitions():
            self.on_button(self, "mic", pressed)

    # sticks → buttons -------------------------------------------------------------

    def _update_stick_buttons(self) -> None:
        threshold = float(self.cfg["input"]["stick_button_at"])
        for side in ("left", "right"):
            current = self._stick_dirs[side]
            wanted = None
            as_buttons = self.cfg["sticks"][side] == "buttons" or side in self.stick_buttons
            if as_buttons and self.active and not self.sticks_captured:
                wanted = stick_direction(*self.sticks[side], threshold, current)
            if wanted == current:
                continue
            prefix = STICK_PREFIX[side]
            if current is not None:
                self.on_button(self, f"{prefix}_{current}", False)
            self._stick_dirs[side] = wanted
            if wanted is not None:
                self.on_button(self, f"{prefix}_{wanted}", True)

    # sticks → scroll / pointer --------------------------------------------------

    def _ensure_tick(self) -> None:
        if not self._tick and self.active and not self.sticks_captured and self._stick_output():
            self._tick = GLib.timeout_add(TICK_MS, self._on_tick)

    def _stick_output(self) -> list[tuple[str, float, float]]:
        deadzone = float(self.cfg["input"]["stick_deadzone"])
        out = []
        for side in ("left", "right"):
            mode = self.cfg["sticks"][side]
            if mode in ("none", "buttons") or side in self.stick_buttons:
                continue
            x, y = shaped(*self.sticks[side], deadzone)
            if x or y:
                out.append((mode, x, y))
        return out

    def _on_tick(self) -> bool:
        output = self._stick_output() if self.active and not self.sticks_captured else []
        if not output or self.pointer is None:
            self._tick = 0
            self._scroll_rest = [0.0, 0.0]
            self._pointer_rest = [0.0, 0.0]
            return False
        dt = TICK_MS / 1000
        for mode, x, y in output:
            if mode == "scroll":
                rate = float(self.cfg["sticks"]["scroll_speed"]) * 120 * dt
                # Stick up (−y) scrolls content up: positive wheel.
                self._scroll_rest[0] += -y * rate
                self._scroll_rest[1] += x * rate
                vertical, horizontal = int(self._scroll_rest[0]), int(self._scroll_rest[1])
                self._scroll_rest[0] -= vertical
                self._scroll_rest[1] -= horizontal
                if vertical or horizontal:
                    self.pointer.scroll(vertical, horizontal)
            else:
                rate = float(self.cfg["sticks"]["pointer_speed"]) * dt
                self._pointer_rest[0] += x * rate
                self._pointer_rest[1] += y * rate
                dx, dy = int(self._pointer_rest[0]), int(self._pointer_rest[1])
                self._pointer_rest[0] -= dx
                self._pointer_rest[1] -= dy
                if dx or dy:
                    self.pointer.move(dx, dy)
        return True

    # touchpad --------------------------------------------------------------------

    def _on_touchpad(self) -> None:
        touchpad = self.controller.touchpad
        assert touchpad is not None
        touch = self._touch
        for event in touchpad.read_events():
            if event.type == ev.EV_KEY and event.code == ev.BTN_LEFT:
                if event.value:
                    touch.clicked = True
                self._on_touchpad_press(bool(event.value))
            elif event.type == ev.EV_ABS:
                if event.code == ev.ABS_MT_SLOT:
                    touch.slot = event.value
                elif event.code == ev.ABS_MT_TRACKING_ID:
                    if event.value < 0:
                        touch.points.pop(touch.slot, None)
                    else:
                        touch.points.setdefault(touch.slot, [0, 0])
                elif event.code == ev.ABS_MT_POSITION_X and touch.slot in touch.points:
                    touch.points[touch.slot][0] = event.value
                elif event.code == ev.ABS_MT_POSITION_Y and touch.slot in touch.points:
                    touch.points[touch.slot][1] = event.value
            elif event.type == ev.EV_SYN and event.code == ev.SYN_REPORT:
                self._touch_frame()

    def _on_touchpad_press(self, down: bool) -> None:
        """Physical press: a mouse button (two fingers = right) or the `touchpad` binding."""
        if not down and self._click_button:
            assert self.pointer is not None
            self.pointer.button(self._click_button, False)
            self._click_button = 0
            return
        touchpad_cfg = self.cfg["touchpad"]
        as_mouse = touchpad_cfg["mode"] == "mouse" and touchpad_cfg["click"] == "mouse"
        if as_mouse and down and self.active and self.pointer is not None:
            self._click_button = ev.BTN_RIGHT if len(self._touch.points) >= 2 else ev.BTN_LEFT
            self.pointer.button(self._click_button, True)
        elif not as_mouse:
            self.on_button(self, "touchpad", down)

    def _touch_frame(self) -> None:
        touch = self._touch
        current = {slot: (p[0], p[1]) for slot, p in touch.points.items()}
        if current and not touch.last:
            touch.started = time.monotonic()
            touch.travel = 0.0
            touch.fingers = 0
            touch.clicked = False
            touch.scroll_rest = 0.0
        touch.fingers = max(touch.fingers, len(current))
        shared = [slot for slot in current if slot in touch.last]
        if shared and self.active and self.pointer is not None and self.cfg["touchpad"]["mode"] == "mouse":
            dx = sum(current[s][0] - touch.last[s][0] for s in shared) / len(shared)
            dy = sum(current[s][1] - touch.last[s][1] for s in shared) / len(shared)
            touch.travel += math.hypot(dx, dy)
            if len(current) == 1:
                self._touch_move(dx, dy)
            elif len(current) == 2:
                self._touch_scroll(dy)
        if not current and touch.last:
            self._touch_end()
        touch.last = current

    def _touch_move(self, dx: float, dy: float) -> None:
        assert self.pointer is not None
        speed = math.hypot(dx, dy)
        gain = float(self.cfg["touchpad"]["sensitivity"]) * (0.8 + min(speed / 25, 1.6))
        self._pointer_rest[0] += dx * gain
        self._pointer_rest[1] += dy * gain
        mx, my = int(self._pointer_rest[0]), int(self._pointer_rest[1])
        self._pointer_rest[0] -= mx
        self._pointer_rest[1] -= my
        if mx or my:
            self.pointer.move(mx, my)

    def _touch_scroll(self, dy: float) -> None:
        assert self.pointer is not None
        direction = 1 if self.cfg["touchpad"]["natural_scroll"] else -1
        self._touch.scroll_rest += dy * float(self.cfg["touchpad"]["scroll_speed"]) * 2.4 * direction
        units = int(self._touch.scroll_rest)
        self._touch.scroll_rest -= units
        if units:
            self.pointer.scroll(units, 0)

    def _touch_end(self) -> None:
        touch = self._touch
        if (
            self.active
            and self.pointer is not None
            and self.cfg["touchpad"]["mode"] == "mouse"
            and self.cfg["touchpad"]["tap_to_click"]
            and not touch.clicked
            and touch.travel < TAP_MAX_TRAVEL
            and time.monotonic() - touch.started < TAP_MAX_S
        ):
            button = ev.BTN_RIGHT if touch.fingers >= 2 else ev.BTN_LEFT
            self.pointer.button(button, True)
            self.pointer.button(button, False)
