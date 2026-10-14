"""uinput DualSense emulator for smoke tests.

Creates a gamepad and a sibling touchpad with hid-playstation's names, capabilities and ranges, and
services rumble (FF) uploads so `EvdevDevice.rumble()` works against it.

CLI (from the repo root): python -m tools.fake_dualsense [--settle S] [--linger S] STEP...
Steps: press:BTN_X  release:BTN_X  tap:BTN_X  stick:ABS_X:255  hat:X:Y  touch:X:Y  untouch
       click:1|0  sleep:SECONDS
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import select
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps_oneshot.linux import evdev as ev
from ps_oneshot.linux.evdev import AbsInfo, FFEffect, _IOW, _IOWR
from ps_oneshot.linux.uinput import BUS_USB, UInputDevice

NAME = "Sony Interactive Entertainment DualSense Wireless Controller"
VENDOR = 0x054C
PRODUCT = 0x0CE6

EV_UINPUT = 0x0101
UI_FF_UPLOAD = 1
UI_FF_ERASE = 2


class _FFUpload(ctypes.Structure):
    _fields_ = [
        ("request_id", ctypes.c_uint32),
        ("retval", ctypes.c_int32),
        ("effect", FFEffect),
        ("old", FFEffect),
    ]


class _FFErase(ctypes.Structure):
    _fields_ = [("request_id", ctypes.c_uint32), ("retval", ctypes.c_int32), ("effect_id", ctypes.c_uint32)]


UI_BEGIN_FF_UPLOAD = _IOWR("U", 200, ctypes.sizeof(_FFUpload))
UI_END_FF_UPLOAD = _IOW("U", 201, ctypes.sizeof(_FFUpload))
UI_BEGIN_FF_ERASE = _IOWR("U", 202, ctypes.sizeof(_FFErase))
UI_END_FF_ERASE = _IOW("U", 203, ctypes.sizeof(_FFErase))

GAMEPAD_KEYS = (
    ev.BTN_SOUTH, ev.BTN_EAST, ev.BTN_NORTH, ev.BTN_WEST, ev.BTN_TL, ev.BTN_TR, ev.BTN_TL2,
    ev.BTN_TR2, ev.BTN_SELECT, ev.BTN_START, ev.BTN_MODE, ev.BTN_THUMBL, ev.BTN_THUMBR,
)
TOUCH_X_MAX = 1919
TOUCH_Y_MAX = 1079


def code_for(name: str | int) -> int:
    """Resolve `BTN_SOUTH` / `ABS_X` style names (or pass ints through)."""
    if isinstance(name, int):
        return name
    value = getattr(ev, name.upper(), None)
    if not isinstance(value, int):
        raise ValueError(f"unknown event code {name!r}")
    return value


class FakeDualSense:
    def __init__(self, name: str = NAME) -> None:
        """`name` lets a test run next to a real controller (match it with devices.match)."""
        phys = f"fake-dualsense-{os.getpid()}/input0"
        stick = AbsInfo(128, 0, 255)
        trigger = AbsInfo(0, 0, 255)
        hat = AbsInfo(0, -1, 1)
        self.gamepad = UInputDevice(
            name,
            keys=GAMEPAD_KEYS,
            axes={
                ev.ABS_X: stick, ev.ABS_Y: stick, ev.ABS_RX: stick, ev.ABS_RY: stick,
                ev.ABS_Z: trigger, ev.ABS_RZ: trigger, ev.ABS_HAT0X: hat, ev.ABS_HAT0Y: hat,
            },
            ff=(ev.FF_RUMBLE,),
            ff_effects_max=16,
            bustype=BUS_USB,
            vendor=VENDOR,
            product=PRODUCT,
            phys=phys,
        )
        try:
            self.touchpad = UInputDevice(
                f"{name} Touchpad",
                keys=(ev.BTN_LEFT, ev.BTN_TOUCH, ev.BTN_TOOL_FINGER, ev.BTN_TOOL_DOUBLETAP),
                axes={
                    ev.ABS_X: AbsInfo(0, 0, TOUCH_X_MAX),
                    ev.ABS_Y: AbsInfo(0, 0, TOUCH_Y_MAX),
                    ev.ABS_MT_SLOT: AbsInfo(0, 0, 1),
                    ev.ABS_MT_TRACKING_ID: AbsInfo(0, 0, 65535),
                    ev.ABS_MT_POSITION_X: AbsInfo(0, 0, TOUCH_X_MAX),
                    ev.ABS_MT_POSITION_Y: AbsInfo(0, 0, TOUCH_Y_MAX),
                },
                props=(ev.INPUT_PROP_POINTER, ev.INPUT_PROP_BUTTONPAD),
                bustype=BUS_USB,
                vendor=VENDOR,
                product=PRODUCT,
                phys=phys,
            )
        except BaseException:
            self.gamepad.close()
            raise
        self.rumbles: list[tuple[int, int, int]] = []  # (strong, weak, length_ms) per played effect
        self._effects: dict[int, tuple[int, int, int]] = {}
        self._tracking_id = 0
        self._touching = False
        self._stop = threading.Event()
        self._ff_thread = threading.Thread(target=self._serve_ff, name="fake-dualsense-ff", daemon=True)
        self._ff_thread.start()

    # --- gamepad ---

    def press(self, btn: str | int) -> None:
        self._key(self.gamepad, code_for(btn), 1)

    def release(self, btn: str | int) -> None:
        self._key(self.gamepad, code_for(btn), 0)

    def stick(self, axis: str | int, value: int) -> None:
        self.gamepad.emit(ev.EV_ABS, code_for(axis), int(value))
        self.gamepad.syn()

    def hat(self, x: int, y: int) -> None:
        self.gamepad.emit(ev.EV_ABS, ev.ABS_HAT0X, int(x))
        self.gamepad.emit(ev.EV_ABS, ev.ABS_HAT0Y, int(y))
        self.gamepad.syn()

    # --- touchpad ---

    def touch(self, x: int | None, y: int | None = None) -> None:
        """Put one finger at (x, y) on the touchpad, or lift it when x is None."""
        tp = self.touchpad
        tp.emit(ev.EV_ABS, ev.ABS_MT_SLOT, 0)
        if x is None:
            if self._touching:
                tp.emit(ev.EV_ABS, ev.ABS_MT_TRACKING_ID, -1)
                tp.emit(ev.EV_KEY, ev.BTN_TOUCH, 0)
                tp.emit(ev.EV_KEY, ev.BTN_TOOL_FINGER, 0)
                self._touching = False
            tp.syn()
            return
        x = max(0, min(int(x), TOUCH_X_MAX))
        y = max(0, min(int(y if y is not None else 0), TOUCH_Y_MAX))
        if not self._touching:
            self._tracking_id = (self._tracking_id + 1) % 65536
            tp.emit(ev.EV_ABS, ev.ABS_MT_TRACKING_ID, self._tracking_id)
        tp.emit(ev.EV_ABS, ev.ABS_MT_POSITION_X, x)
        tp.emit(ev.EV_ABS, ev.ABS_MT_POSITION_Y, y)
        if not self._touching:
            tp.emit(ev.EV_KEY, ev.BTN_TOUCH, 1)
            tp.emit(ev.EV_KEY, ev.BTN_TOOL_FINGER, 1)
            self._touching = True
        tp.emit(ev.EV_ABS, ev.ABS_X, x)
        tp.emit(ev.EV_ABS, ev.ABS_Y, y)
        tp.syn()

    def click(self, down: bool) -> None:
        self._key(self.touchpad, ev.BTN_LEFT, 1 if down else 0)

    @staticmethod
    def _key(dev: UInputDevice, code: int, value: int) -> None:
        dev.emit(ev.EV_KEY, code, value)
        dev.syn()

    # --- force feedback ---

    def _serve_ff(self) -> None:
        fd = self.gamepad.fd
        size = ev.INPUT_EVENT.size
        while not self._stop.is_set():
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                data = os.read(fd, size * 16)
            except BlockingIOError:
                continue
            except OSError:
                return
            for _, _, type_, code, value in ev.INPUT_EVENT.iter_unpack(data[: len(data) - len(data) % size]):
                if type_ == EV_UINPUT and code == UI_FF_UPLOAD:
                    upload = _FFUpload(request_id=value)
                    fcntl.ioctl(fd, UI_BEGIN_FF_UPLOAD, upload, True)
                    effect = upload.effect
                    rumble = effect.u.rumble
                    self._effects[effect.id] = (rumble.strong_magnitude, rumble.weak_magnitude, effect.replay_length)
                    upload.retval = 0
                    fcntl.ioctl(fd, UI_END_FF_UPLOAD, upload)
                elif type_ == EV_UINPUT and code == UI_FF_ERASE:
                    erase = _FFErase(request_id=value)
                    fcntl.ioctl(fd, UI_BEGIN_FF_ERASE, erase, True)
                    self._effects.pop(erase.effect_id, None)
                    erase.retval = 0
                    fcntl.ioctl(fd, UI_END_FF_ERASE, erase)
                elif type_ == ev.EV_FF and value > 0 and code in self._effects:
                    strong, weak, length = self._effects[code]
                    self.rumbles.append(self._effects[code])
                    print(f"rumble strong={strong} weak={weak} ms={length}", flush=True)

    def close(self) -> None:
        self._stop.set()
        self._ff_thread.join()
        self.touchpad.close()
        self.gamepad.close()


def run_step(pad: FakeDualSense, step: str) -> None:
    op, _, rest = step.partition(":")
    args = rest.split(":") if rest else []
    if op == "press":
        pad.press(args[0])
    elif op == "release":
        pad.release(args[0])
    elif op == "tap":
        pad.press(args[0])
        time.sleep(0.05)
        pad.release(args[0])
    elif op == "stick":
        pad.stick(args[0], int(args[1]))
    elif op == "hat":
        pad.hat(int(args[0]), int(args[1]))
    elif op == "touch":
        pad.touch(int(args[0]), int(args[1]))
    elif op == "untouch":
        pad.touch(None)
    elif op == "click":
        pad.click(args[0] not in ("0", "up", "false"))
    elif op == "sleep":
        time.sleep(float(args[0]))
    else:
        raise ValueError(f"unknown step {step!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emulate a DualSense via uinput and play a scripted sequence.")
    parser.add_argument("--settle", type=float, default=1.0, help="seconds to wait after creating devices")
    parser.add_argument("--linger", type=float, default=1.0, help="seconds to keep devices after the script")
    parser.add_argument("steps", nargs="*", help="e.g. press:BTN_THUMBL sleep:0.3 stick:ABS_X:255")
    args = parser.parse_args(argv)
    pad = FakeDualSense()
    try:
        print("created", flush=True)
        time.sleep(args.settle)
        for step in args.steps:
            run_step(pad, step)
            print(f"step {step}", flush=True)
        time.sleep(args.linger)
    except KeyboardInterrupt:
        pass
    finally:
        pad.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
