"""Virtual input devices through /dev/uinput (stdlib only)."""

from __future__ import annotations

import ctypes
import fcntl
import os
import struct
from collections.abc import Iterable, Mapping

from .evdev import (
    ABSINFO,
    BTN_LEFT,
    BTN_MIDDLE,
    BTN_RIGHT,
    EV_ABS,
    EV_FF,
    EV_KEY,
    EV_REL,
    EV_SYN,
    INPUT_EVENT,
    KEY_NAMES,
    REL_HWHEEL,
    REL_HWHEEL_HI_RES,
    REL_WHEEL,
    REL_WHEEL_HI_RES,
    REL_X,
    REL_Y,
    SYN_REPORT,
    AbsInfo,
    _IO,
    _IOW,
)

UINPUT_PATH = "/dev/uinput"
UINPUT_MAX_NAME_SIZE = 80
BUS_USB = 0x03
BUS_VIRTUAL = 0x06

# struct uinput_setup { struct input_id id; char name[80]; __u32 ff_effects_max; }
_UINPUT_SETUP = struct.Struct(f"4H{UINPUT_MAX_NAME_SIZE}sI")
# struct uinput_abs_setup { __u16 code; struct input_absinfo absinfo; }
_UINPUT_ABS_SETUP = struct.Struct(f"H2x{ABSINFO.format}")

UI_DEV_CREATE = _IO("U", 1)
UI_DEV_DESTROY = _IO("U", 2)
UI_DEV_SETUP = _IOW("U", 3, _UINPUT_SETUP.size)
UI_ABS_SETUP = _IOW("U", 4, _UINPUT_ABS_SETUP.size)
_INT = ctypes.sizeof(ctypes.c_int)
UI_SET_EVBIT = _IOW("U", 100, _INT)
UI_SET_KEYBIT = _IOW("U", 101, _INT)
UI_SET_RELBIT = _IOW("U", 102, _INT)
UI_SET_ABSBIT = _IOW("U", 103, _INT)
UI_SET_FFBIT = _IOW("U", 107, _INT)
UI_SET_PHYS = _IOW("U", 108, ctypes.sizeof(ctypes.c_char_p))
UI_SET_PROPBIT = _IOW("U", 110, _INT)


class UInputDevice:
    """A uinput device with the given capabilities, created on construction."""

    def __init__(
        self,
        name: str,
        *,
        keys: Iterable[int] = (),
        rels: Iterable[int] = (),
        axes: Mapping[int, AbsInfo] | None = None,
        props: Iterable[int] = (),
        ff: Iterable[int] = (),
        ff_effects_max: int = 0,
        bustype: int = BUS_VIRTUAL,
        vendor: int = 0,
        product: int = 0,
        version: int = 0,
        phys: str = "",
    ) -> None:
        encoded = name.encode()
        if len(encoded) >= UINPUT_MAX_NAME_SIZE:
            raise ValueError(f"uinput device name too long: {name!r}")
        self.name = name
        self.fd = os.open(UINPUT_PATH, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            keys, rels, ff = list(keys), list(rels), list(ff)
            axes = dict(axes or {})
            for ev, codes, request in (
                (EV_KEY, keys, UI_SET_KEYBIT),
                (EV_REL, rels, UI_SET_RELBIT),
                (EV_ABS, list(axes), UI_SET_ABSBIT),
                (EV_FF, ff, UI_SET_FFBIT),
            ):
                if codes:
                    fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
                    for code in codes:
                        fcntl.ioctl(self.fd, request, code)
            for prop in props:
                fcntl.ioctl(self.fd, UI_SET_PROPBIT, prop)
            if phys:
                fcntl.ioctl(self.fd, UI_SET_PHYS, phys.encode() + b"\0")
            for code, info in axes.items():
                fcntl.ioctl(
                    self.fd,
                    UI_ABS_SETUP,
                    _UINPUT_ABS_SETUP.pack(
                        code, info.value, info.minimum, info.maximum, info.fuzz, info.flat, info.resolution
                    ),
                )
            fcntl.ioctl(
                self.fd,
                UI_DEV_SETUP,
                _UINPUT_SETUP.pack(bustype, vendor, product, version, encoded, ff_effects_max),
            )
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except BaseException:
            os.close(self.fd)
            raise

    def emit(self, type_: int, code: int, value: int) -> None:
        os.write(self.fd, INPUT_EVENT.pack(0, 0, type_, code, value))

    def syn(self) -> None:
        self.emit(EV_SYN, SYN_REPORT, 0)

    def close(self) -> None:
        if self.fd < 0:
            return
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        finally:
            os.close(self.fd)
            self.fd = -1


class VirtualKeyboard:
    def __init__(self, name: str = "ps-oneshot keyboard") -> None:
        self._dev = UInputDevice(name, keys=sorted(set(KEY_NAMES.values())))

    def key(self, code: int, down: bool) -> None:
        self._dev.emit(EV_KEY, code, 1 if down else 0)
        self._dev.syn()

    def tap(self, codes: list[int]) -> None:
        for code in codes:
            self.key(code, True)
        for code in reversed(codes):
            self.key(code, False)

    def close(self) -> None:
        self._dev.close()


class VirtualPointer:
    NOTCH = 120

    def __init__(self, name: str = "ps-oneshot pointer") -> None:
        self._dev = UInputDevice(
            name,
            keys=(BTN_LEFT, BTN_RIGHT, BTN_MIDDLE),
            rels=(REL_X, REL_Y, REL_WHEEL, REL_HWHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES),
        )
        self._acc_v = 0
        self._acc_h = 0

    def move(self, dx: int, dy: int) -> None:
        emitted = False
        for code, value in ((REL_X, dx), (REL_Y, dy)):
            if value:
                self._dev.emit(EV_REL, code, int(value))
                emitted = True
        if emitted:
            self._dev.syn()

    def scroll(self, vertical: int, horizontal: int) -> None:
        if not vertical and not horizontal:
            return
        if vertical:
            self._dev.emit(EV_REL, REL_WHEEL_HI_RES, int(vertical))
            self._acc_v, notches = self._accumulate(self._acc_v + int(vertical))
            if notches:
                self._dev.emit(EV_REL, REL_WHEEL, notches)
        if horizontal:
            self._dev.emit(EV_REL, REL_HWHEEL_HI_RES, int(horizontal))
            self._acc_h, notches = self._accumulate(self._acc_h + int(horizontal))
            if notches:
                self._dev.emit(EV_REL, REL_HWHEEL, notches)
        self._dev.syn()

    @classmethod
    def _accumulate(cls, total: int) -> tuple[int, int]:
        """Split accumulated hi-res units into (remainder, whole notches), truncating toward zero."""
        notches = int(total / cls.NOTCH)
        return total - notches * cls.NOTCH, notches

    def button(self, code: int, down: bool) -> None:
        self._dev.emit(EV_KEY, code, 1 if down else 0)
        self._dev.syn()

    def close(self) -> None:
        self._dev.close()
