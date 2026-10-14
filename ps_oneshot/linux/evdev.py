"""Minimal evdev access through raw ioctls (stdlib only)."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import glob
import logging
import os
import re
import struct
from dataclasses import dataclass

log = logging.getLogger(__name__)

# --- ioctl encoding (asm-generic/ioctl.h) -------------------------------------------------------

_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2


def _IOC(direction: int, kind: str, nr: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (ord(kind) << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def _IO(kind: str, nr: int) -> int:
    return _IOC(_IOC_NONE, kind, nr, 0)


def _IOR(kind: str, nr: int, size: int) -> int:
    return _IOC(_IOC_READ, kind, nr, size)


def _IOW(kind: str, nr: int, size: int) -> int:
    return _IOC(_IOC_WRITE, kind, nr, size)


def _IOWR(kind: str, nr: int, size: int) -> int:
    return _IOC(_IOC_READ | _IOC_WRITE, kind, nr, size)


# --- event codes (linux/input-event-codes.h) ----------------------------------------------------

EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03
EV_FF = 0x15
SYN_REPORT = 0

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BTN_SOUTH = 0x130
BTN_EAST = 0x131
BTN_NORTH = 0x133
BTN_WEST = 0x134
BTN_TL = 0x136
BTN_TR = 0x137
BTN_TL2 = 0x138
BTN_TR2 = 0x139
BTN_SELECT = 0x13A
BTN_START = 0x13B
BTN_MODE = 0x13C
BTN_THUMBL = 0x13D
BTN_THUMBR = 0x13E
BTN_TOOL_FINGER = 0x145
BTN_TOUCH = 0x14A
BTN_TOOL_DOUBLETAP = 0x14D

ABS_X = 0x00
ABS_Y = 0x01
ABS_Z = 0x02
ABS_RX = 0x03
ABS_RY = 0x04
ABS_RZ = 0x05
ABS_HAT0X = 0x10
ABS_HAT0Y = 0x11
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39

REL_X = 0x00
REL_Y = 0x01
REL_HWHEEL = 0x06
REL_WHEEL = 0x08
REL_WHEEL_HI_RES = 0x0B
REL_HWHEEL_HI_RES = 0x0C

INPUT_PROP_POINTER = 0x00
INPUT_PROP_BUTTONPAD = 0x02

FF_RUMBLE = 0x50

KEY_MAX = 0x2FF
ABS_MAX = 0x3F
FF_MAX = 0x7F


def _key_names() -> dict[str, int]:
    names: dict[str, int] = {}
    for offset, row in ((2, "1234567890"), (16, "qwertyuiop"), (30, "asdfghjkl"), (44, "zxcvbnm")):
        for i, ch in enumerate(row):
            names[ch] = offset + i
    for i in range(10):
        names[f"f{i + 1}"] = 59 + i
    names["f11"] = 87
    names["f12"] = 88
    for i in range(12):
        names[f"f{i + 13}"] = 183 + i
    names.update(
        esc=1, minus=12, equal=13, backspace=14, tab=15, leftbrace=26, rightbrace=27, enter=28,
        leftctrl=29, semicolon=39, apostrophe=40, grave=41, leftshift=42, backslash=43, comma=51,
        period=52, slash=53, rightshift=54, leftalt=56, space=57, capslock=58, rightctrl=97,
        print=99, rightalt=100, home=102, up=103, pageup=104, left=105, right=106, end=107,
        down=108, pagedown=109, insert=110, delete=111, mute=113, volumedown=114, volumeup=115,
        leftmeta=125, rightmeta=126, compose=127, nextsong=163, playpause=164, previoussong=165,
        micmute=248, kpasterisk=55, kpminus=74, kpplus=78, kpdot=83, kpenter=96, kpslash=98,
    )
    # Keypad keys type the same symbol whatever the keyboard layout (unlike minus/equal on a German one).
    for digit, code in zip("7894561230", (71, 72, 73, 75, 76, 77, 79, 80, 81, 82)):
        names[f"kp{digit}"] = code
    aliases = {
        "return": "enter", "escape": "esc", "dot": "period", "ctrl": "leftctrl",
        "control": "leftctrl", "shift": "leftshift", "alt": "leftalt", "super": "leftmeta",
        "meta": "leftmeta", "win": "leftmeta", "sysrq": "print",
    }
    for alias, target in aliases.items():
        names[alias] = names[target]
    return names


KEY_NAMES: dict[str, int] = _key_names()


@dataclass(frozen=True)
class InputEvent:
    type: int
    code: int
    value: int
    time: float


@dataclass(frozen=True)
class AbsInfo:
    value: int
    minimum: int
    maximum: int
    fuzz: int = 0
    flat: int = 0
    resolution: int = 0


# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
INPUT_EVENT = struct.Struct("llHHi")
# struct input_absinfo { __s32 value, minimum, maximum, fuzz, flat, resolution; }
ABSINFO = struct.Struct("6i")


class FFRumble(ctypes.Structure):
    _fields_ = [("strong_magnitude", ctypes.c_uint16), ("weak_magnitude", ctypes.c_uint16)]


class _FFPeriodicLayout(ctypes.Structure):
    """Largest ff_effect union member; only present to give the union its kernel size/alignment."""

    _fields_ = [
        ("head", ctypes.c_uint16 * 5),
        ("envelope", ctypes.c_uint16 * 4),
        ("custom_len", ctypes.c_uint32),
        ("custom_data", ctypes.c_void_p),
    ]


class _FFUnion(ctypes.Union):
    _fields_ = [("rumble", FFRumble), ("periodic", _FFPeriodicLayout)]


class FFEffect(ctypes.Structure):
    """`struct ff_effect` (linux/input.h)."""

    _fields_ = [
        ("type", ctypes.c_uint16),
        ("id", ctypes.c_int16),
        ("direction", ctypes.c_uint16),
        ("trigger_button", ctypes.c_uint16),
        ("trigger_interval", ctypes.c_uint16),
        ("replay_length", ctypes.c_uint16),
        ("replay_delay", ctypes.c_uint16),
        ("u", _FFUnion),
    ]


def EVIOCGNAME(length: int) -> int:
    return _IOC(_IOC_READ, "E", 0x06, length)


def EVIOCGPHYS(length: int) -> int:
    return _IOC(_IOC_READ, "E", 0x07, length)


def EVIOCGUNIQ(length: int) -> int:
    return _IOC(_IOC_READ, "E", 0x08, length)


def EVIOCGBIT(ev: int, length: int) -> int:
    return _IOC(_IOC_READ, "E", 0x20 + ev, length)


def EVIOCGABS(axis: int) -> int:
    return _IOR("E", 0x40 + axis, ABSINFO.size)


EVIOCSFF = _IOW("E", 0x80, ctypes.sizeof(FFEffect))
EVIOCGRAB = _IOW("E", 0x90, ctypes.sizeof(ctypes.c_int))

_STRING_LEN = 256


def _bit_set(bits: bytes, code: int) -> bool:
    return code // 8 < len(bits) and bool(bits[code // 8] & (1 << (code % 8)))


class EvdevDevice:
    def __init__(self, path: str, writable: bool = False) -> None:
        self.path = path
        self.writable = False
        if writable:
            try:
                self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
                self.writable = True
            except PermissionError:
                self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        else:
            self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        self._bits: dict[int, bytes] = {}
        self._effect_id = -1
        try:
            self.name = self._string(EVIOCGNAME)
        except OSError:
            os.close(self.fd)  # gone between open and the first ioctl: don't leak the descriptor
            raise

    def __repr__(self) -> str:
        return f"EvdevDevice({self.path!r}, name={self.name!r})"

    def _string(self, request) -> str:
        buf = bytearray(_STRING_LEN)
        try:
            fcntl.ioctl(self.fd, request(_STRING_LEN), buf, True)
        except OSError as exc:
            if exc.errno == errno.ENOENT:  # property not set for this device
                return ""
            raise
        return bytes(buf).split(b"\0", 1)[0].decode(errors="replace")

    def _capabilities(self, ev: int, maximum: int) -> bytes:
        bits = self._bits.get(ev)
        if bits is None:
            buf = bytearray(maximum // 8 + 1)
            fcntl.ioctl(self.fd, EVIOCGBIT(ev, len(buf)), buf, True)
            bits = self._bits[ev] = bytes(buf)
        return bits

    def read_events(self) -> list[InputEvent]:
        events: list[InputEvent] = []
        size = INPUT_EVENT.size
        while True:
            try:
                data = os.read(self.fd, size * 64)
            except BlockingIOError:
                return events
            if not data:
                return events
            for sec, usec, type_, code, value in INPUT_EVENT.iter_unpack(data[: len(data) - len(data) % size]):
                events.append(InputEvent(type_, code, value, sec + usec / 1_000_000))

    def abs_info(self, code: int) -> AbsInfo:
        buf = bytearray(ABSINFO.size)
        fcntl.ioctl(self.fd, EVIOCGABS(code), buf, True)
        return AbsInfo(*ABSINFO.unpack(buf))

    def has_key(self, code: int) -> bool:
        return _bit_set(self._capabilities(EV_KEY, KEY_MAX), code)

    def has_abs(self, code: int) -> bool:
        return _bit_set(self._capabilities(EV_ABS, ABS_MAX), code)

    def has_ff(self, code: int) -> bool:
        return _bit_set(self._capabilities(EV_FF, FF_MAX), code)

    def uniq(self) -> str:
        return self._string(EVIOCGUNIQ)

    def phys(self) -> str:
        return self._string(EVIOCGPHYS)

    def grab(self, on: bool) -> None:
        fcntl.ioctl(self.fd, EVIOCGRAB, 1 if on else 0)

    def rumble(self, strong: float, weak: float, duration_ms: int) -> bool:
        try:
            if not self.writable or not self.has_ff(FF_RUMBLE):
                return False
        except OSError:
            return False
        effect = FFEffect()
        effect.type = FF_RUMBLE
        effect.id = self._effect_id
        effect.replay_length = max(0, min(int(duration_ms), 0xFFFF))
        effect.u.rumble.strong_magnitude = round(max(0.0, min(strong, 1.0)) * 0xFFFF)
        effect.u.rumble.weak_magnitude = round(max(0.0, min(weak, 1.0)) * 0xFFFF)
        buf = bytearray(effect)
        try:
            fcntl.ioctl(self.fd, EVIOCSFF, buf, True)
            self._effect_id = FFEffect.from_buffer(buf).id
            os.write(self.fd, INPUT_EVENT.pack(0, 0, EV_FF, self._effect_id, 1))
        except OSError as exc:
            log.debug("rumble on %s failed: %s", self.path, exc)
            self._effect_id = -1
            return False
        return True

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def list_event_nodes(root: str = "/dev/input") -> list[str]:
    def number(path: str) -> int:
        return int(re.sub(r"\D", "", os.path.basename(path)) or 0)

    return sorted(glob.glob(os.path.join(root, "event[0-9]*")), key=number)
