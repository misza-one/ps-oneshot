"""DualSense (and generic gamepad) discovery plus sysfs LED/battery and hidraw mic-button access."""

from __future__ import annotations

import ctypes
import errno
import glob
import logging
import os
import re
import zlib
from dataclasses import dataclass, field

from .evdev import ABS_MT_POSITION_X, ABS_X, BTN_LEFT, BTN_SOUTH, EvdevDevice

log = logging.getLogger(__name__)

SYSFS_ROOT = "/sys"
DEV_ROOT = "/dev"
UDEV_RULE = "packaging/70-ps-oneshot.rules"

_HID_ID = re.compile(r"^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}$")
_BITS_PER_LONG = 8 * ctypes.sizeof(ctypes.c_long)
_permission_warned = False


def _read_text(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _write_text(path: str, value: str) -> None:
    with open(path, "w") as f:
        f.write(value)


def parse_capabilities(text: str) -> set[int]:
    """Decode a sysfs `capabilities/*` bitmap: hex longs, most significant word first."""
    codes: set[int] = set()
    for index, word in enumerate(reversed(text.split())):
        value = int(word, 16)
        base = index * _BITS_PER_LONG
        bit = 0
        while value:
            if value & 1:
                codes.add(base + bit)
            value >>= 1
            bit += 1
    return codes


@dataclass(frozen=True)
class NodeInfo:
    """What sysfs says about one `eventN` node, without opening it."""

    event: str
    name: str
    phys: str
    uniq: str
    hid_dir: str | None
    keys: frozenset[int]
    axes: frozenset[int]

    @property
    def is_gamepad(self) -> bool:
        return BTN_SOUTH in self.keys and ABS_X in self.axes

    @property
    def is_touchpad(self) -> bool:
        return BTN_LEFT in self.keys and ABS_MT_POSITION_X in self.axes


def node_info(event: str, sysfs_root: str | None = None) -> NodeInfo | None:
    base = os.path.join(sysfs_root or SYSFS_ROOT, "class", "input", event, "device")
    name = _read_text(os.path.join(base, "name"))
    if name is None:
        return None
    parent = os.path.join(base, "device")
    hid_dir = os.path.realpath(parent) if os.path.isdir(parent) else None
    if hid_dir is not None and not _HID_ID.match(os.path.basename(hid_dir)):
        hid_dir = None
    caps = os.path.join(base, "capabilities")
    return NodeInfo(
        event=event,
        name=name,
        phys=_read_text(os.path.join(base, "phys")) or "",
        uniq=_read_text(os.path.join(base, "uniq")) or "",
        hid_dir=hid_dir,
        keys=frozenset(parse_capabilities(_read_text(os.path.join(caps, "key")) or "")),
        axes=frozenset(parse_capabilities(_read_text(os.path.join(caps, "abs")) or "")),
    )


def _event_number(event: str) -> int:
    return int(event.removeprefix("event") or 0)


def scan_nodes(sysfs_root: str | None = None) -> list[NodeInfo]:
    root = os.path.join(sysfs_root or SYSFS_ROOT, "class", "input")
    events = sorted(
        (os.path.basename(p) for p in glob.glob(os.path.join(root, "event[0-9]*"))), key=_event_number
    )
    return [info for event in events if (info := node_info(event, sysfs_root)) is not None]


def pair_nodes(nodes: list[NodeInfo], match: str, ignore: str) -> list[tuple[NodeInfo, NodeInfo | None]]:
    """Pick gamepads and pair each with its touchpad sibling.

    Siblings share a HID parent; nodes without one (uinput) pair when the touchpad name starts with the
    gamepad name and phys/uniq are equal.
    """
    match_re = re.compile(match, re.IGNORECASE)
    ignore_re = re.compile(ignore, re.IGNORECASE) if ignore else None
    touchpads = [n for n in nodes if n.is_touchpad]
    claimed: set[str] = set()
    pairs: list[tuple[NodeInfo, NodeInfo | None]] = []
    for node in nodes:
        if not node.is_gamepad or not match_re.search(node.name):
            continue
        if ignore_re is not None and ignore_re.search(node.name):
            continue
        sibling = None
        for pad in touchpads:
            if pad.event in claimed or pad.event == node.event:
                continue
            if node.hid_dir is not None:
                related = pad.hid_dir == node.hid_dir
            else:
                related = (
                    pad.hid_dir is None
                    and pad.name.startswith(node.name)
                    and pad.phys == node.phys
                    and pad.uniq == node.uniq
                )
            if related:
                sibling = pad
                claimed.add(pad.event)
                break
        pairs.append((node, sibling))
    return pairs


@dataclass(frozen=True)
class HidResources:
    hidraw: str | None
    lightbar: str | None
    player_leds: list[str]
    battery: str | None


def hid_resources(hid_dir: str, dev_root: str | None = None) -> HidResources:
    hidraw_nodes = sorted(glob.glob(os.path.join(hid_dir, "hidraw", "hidraw[0-9]*")))
    hidraw = os.path.join(dev_root or DEV_ROOT, os.path.basename(hidraw_nodes[0])) if hidraw_nodes else None
    leds = sorted(glob.glob(os.path.join(hid_dir, "leds", "*")))
    lightbar = next((d for d in leds if d.endswith(":rgb:indicator")), None)
    players: dict[int, str] = {}
    for d in leds:
        m = re.search(r":white:player-([1-5])$", d)
        if m:
            players[int(m.group(1))] = d
    batteries = sorted(glob.glob(os.path.join(hid_dir, "power_supply", "*")))
    battery = next((b for b in batteries if "ps-controller-battery-" in os.path.basename(b)), None)
    return HidResources(hidraw, lightbar, [players[i] for i in sorted(players)], battery)


@dataclass
class Controller:
    hid_id: str
    name: str
    gamepad: EvdevDevice
    touchpad: EvdevDevice | None = None
    hidraw: str | None = None
    lightbar: str | None = None
    player_leds: list[str] = field(default_factory=list)
    battery: str | None = None
    _written: dict[str, str] = field(default_factory=dict, repr=False)
    _max_brightness: dict[str, int] = field(default_factory=dict, repr=False)
    _hidraw_fd: int = field(default=-1, repr=False)
    _hidraw_warned: bool = field(default=False, repr=False)
    _output_seq: int = field(default=0, repr=False)

    def _max(self, led: str) -> int:
        cached = self._max_brightness.get(led)
        if cached is None:
            text = _read_text(os.path.join(led, "max_brightness"))
            cached = self._max_brightness[led] = int(text) if text and text.isdigit() else 255
        return cached

    def _write(self, writes: list[tuple[str, str]]) -> bool:
        global _permission_warned
        for path, value in writes:
            if self._written.get(path) == value:
                continue
            try:
                _write_text(path, value)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM):
                    if not _permission_warned:
                        _permission_warned = True
                        log.warning(
                            "no permission to write %s; install %s (udev rule granting LED access)",
                            path,
                            UDEV_RULE,
                        )
                else:
                    log.debug("LED write %s=%r failed: %s", path, value, exc)
                return False
            self._written[path] = value
        return True

    def set_lightbar(self, rgb: tuple[int, int, int], brightness: float) -> bool:
        if self.lightbar is None:
            return False
        r, g, b = (max(0, min(int(c), 255)) for c in rgb)
        level = round(max(0.0, min(brightness, 1.0)) * self._max(self.lightbar))
        return self._write(
            [
                (os.path.join(self.lightbar, "multi_intensity"), f"{r} {g} {b}"),
                (os.path.join(self.lightbar, "brightness"), str(level)),
            ]
        )

    def set_player_leds(self, mask: int) -> bool:
        if not self.player_leds:
            return False
        return self._write(
            [
                (os.path.join(led, "brightness"), str(self._max(led) if mask & (1 << i) else 0))
                for i, led in enumerate(self.player_leds)
            ]
        )

    def battery_state(self) -> tuple[int | None, str]:
        if self.battery is None:
            return None, "Unknown"
        capacity = _read_text(os.path.join(self.battery, "capacity"))
        status = _read_text(os.path.join(self.battery, "status")) or "Unknown"
        return (int(capacity) if capacity and capacity.isdigit() else None), status

    def rumble(self, strong: float, weak: float, ms: int) -> bool:
        return self.gamepad.rumble(strong, weak, ms)

    def set_trigger_effect(self, right: bytes | None = None, left: bytes | None = None) -> bool:
        """Send adaptive-trigger effects (from `trigger_*`) through a hidraw output report."""
        if right is None and left is None:
            return True
        return self._send_output(right=right, left=left)

    def set_mic_mute(self, muted: bool) -> bool:
        """Mute the controller's own microphone at the hardware level and light the mic LED to match.
        The kernel flips both on every mic-button press; call this to put them back to the wanted state."""
        return self._send_output(mic_mute=muted)

    def _send_output(self, right: bytes | None = None, left: bytes | None = None, mic_mute: bool | None = None) -> bool:
        if self.hidraw is None:
            self._warn_hidraw("controller has no hidraw node")
            return False
        bluetooth = self.hid_id.upper().startswith("0005:")
        report = build_output_report(bluetooth, self._output_seq, right, left, mic_mute)
        try:
            if self._hidraw_fd < 0:
                self._hidraw_fd = os.open(self.hidraw, os.O_WRONLY | os.O_CLOEXEC)
            os.write(self._hidraw_fd, report)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM, errno.ENOENT):
                self._warn_hidraw(f"cannot open {self.hidraw}: {exc.strerror}; install {UDEV_RULE}")
            else:
                log.debug("output report write to %s failed: %s", self.hidraw, exc)
            self._close_hidraw()
            return False
        if bluetooth:
            self._output_seq = (self._output_seq + 1) & 0x0F
        return True

    def _warn_hidraw(self, message: str) -> None:
        if not self._hidraw_warned:
            self._hidraw_warned = True
            log.warning("adaptive triggers / mic LED unavailable for %s: %s", self.name, message)

    def _close_hidraw(self) -> None:
        if self._hidraw_fd >= 0:
            os.close(self._hidraw_fd)
            self._hidraw_fd = -1

    def close(self) -> None:
        self.gamepad.close()
        if self.touchpad is not None:
            self.touchpad.close()
        self._close_hidraw()


# Adaptive trigger effects: 11-byte blocks per Nielk1's TriggerEffectGenerator (MIT).
TRIGGER_EFFECT_SIZE = 11


def _effect(mode: int, *payload: int) -> bytes:
    return bytes([mode, *payload]).ljust(TRIGGER_EFFECT_SIZE, b"\0")


def trigger_off() -> bytes:
    return _effect(0x05)


def trigger_feedback(position: int, strength: int) -> bytes:
    """Resistance from `position` (0..9) to the end of travel; strength 0..8 (0 = off)."""
    if not 0 <= position <= 9:
        raise ValueError(f"feedback position {position} not in 0..9")
    if not 0 <= strength <= 8:
        raise ValueError(f"feedback strength {strength} not in 0..8")
    if strength == 0:
        return trigger_off()
    active = 0
    forces = 0
    for zone in range(position, 10):
        active |= 1 << zone
        forces |= (strength - 1) << (3 * zone)
    return _effect(0x21, *active.to_bytes(2, "little"), *forces.to_bytes(4, "little"))


def trigger_weapon(start: int, end: int, strength: int) -> bytes:
    """Resistance between zones `start` (2..7) and `end` (start+1..8) that snaps on release; strength 0..8."""
    if not 2 <= start <= 7:
        raise ValueError(f"weapon start {start} not in 2..7")
    if not start < end <= 8:
        raise ValueError(f"weapon end {end} not in {start + 1}..8")
    if not 0 <= strength <= 8:
        raise ValueError(f"weapon strength {strength} not in 0..8")
    if strength == 0:
        return trigger_off()
    zones = (1 << start) | (1 << end)
    return _effect(0x25, *zones.to_bytes(2, "little"), strength - 1)


def trigger_vibration(position: int, amplitude: int, frequency: int) -> bytes:
    """The trigger itself buzzes from `position` (0..9) on; amplitude 0..8 (0 = off), frequency in Hz."""
    if not 0 <= position <= 9:
        raise ValueError(f"vibration position {position} not in 0..9")
    if not 0 <= amplitude <= 8:
        raise ValueError(f"vibration amplitude {amplitude} not in 0..8")
    if not 0 <= frequency <= 255:
        raise ValueError(f"vibration frequency {frequency} not in 0..255")
    if amplitude == 0 or frequency == 0:
        return trigger_off()
    active = 0
    amplitudes = 0
    for zone in range(position, 10):
        active |= 1 << zone
        amplitudes |= (amplitude - 1) << (3 * zone)
    return _effect(0x26, *active.to_bytes(2, "little"), *amplitudes.to_bytes(4, "little"), 0, 0, frequency)


# hid-playstation `struct dualsense_output_report_common` (47 bytes) and its framing.
_COMMON_SIZE = 47
_FLAG0_RIGHT_TRIGGER = 0x04
_FLAG0_LEFT_TRIGGER = 0x08
_RIGHT_TRIGGER_OFFSET = 10
_LEFT_TRIGGER_OFFSET = 21
_FLAG1_MIC_LED = 0x01
_FLAG1_POWER_SAVE = 0x02  # power_save_control is valid; its MIC_MUTE bit cuts the built-in microphone
_MIC_LED_OFFSET = 8
_POWER_SAVE_OFFSET = 9
_POWER_SAVE_MIC_MUTE = 0x10
_USB_OUTPUT_ID = 0x02
_USB_OUTPUT_SIZE = 63
_BT_OUTPUT_ID = 0x31
_BT_OUTPUT_SIZE = 78
_BT_OUTPUT_TAG = 0x10
_BT_CRC_SEED = b"\xa2"


def build_output_report(
    bluetooth: bool, seq: int, right: bytes | None, left: bytes | None, mic_mute: bool | None = None,
) -> bytes:
    """Output report touching only the given fields (all other valid flags clear, so the kernel's
    lightbar/rumble/audio state is left alone)."""
    common = bytearray(_COMMON_SIZE)
    for effect, flag, offset in (
        (right, _FLAG0_RIGHT_TRIGGER, _RIGHT_TRIGGER_OFFSET),
        (left, _FLAG0_LEFT_TRIGGER, _LEFT_TRIGGER_OFFSET),
    ):
        if effect is None:
            continue
        if len(effect) != TRIGGER_EFFECT_SIZE:
            raise ValueError(f"trigger effect must be {TRIGGER_EFFECT_SIZE} bytes, got {len(effect)}")
        common[0] |= flag
        common[offset : offset + TRIGGER_EFFECT_SIZE] = effect
    if mic_mute is not None:
        # Same fields hid-playstation writes when the mic button toggles: LED plus hardware mute.
        common[1] |= _FLAG1_MIC_LED | _FLAG1_POWER_SAVE
        common[_MIC_LED_OFFSET] = 1 if mic_mute else 0
        common[_POWER_SAVE_OFFSET] = _POWER_SAVE_MIC_MUTE if mic_mute else 0
    if not bluetooth:
        return bytes([_USB_OUTPUT_ID]) + bytes(common).ljust(_USB_OUTPUT_SIZE - 1, b"\0")
    report = bytearray(_BT_OUTPUT_SIZE)
    report[0] = _BT_OUTPUT_ID
    report[1] = (seq & 0x0F) << 4
    report[2] = _BT_OUTPUT_TAG
    report[3 : 3 + _COMMON_SIZE] = common
    report[-4:] = zlib.crc32(_BT_CRC_SEED + bytes(report[:-4])).to_bytes(4, "little")
    return bytes(report)


def discover(match: str, ignore: str, sysfs_root: str | None = None) -> list[Controller]:
    controllers: list[Controller] = []
    input_root = os.path.join(DEV_ROOT, "input")
    for pad, touch in pair_nodes(scan_nodes(sysfs_root), match, ignore):
        try:
            gamepad = EvdevDevice(os.path.join(input_root, pad.event), writable=True)
        except OSError:
            continue
        touchpad = None
        if touch is not None:
            try:
                touchpad = EvdevDevice(os.path.join(input_root, touch.event))
            except OSError:
                pass
        if pad.hid_dir is not None:
            res = hid_resources(pad.hid_dir)
            hid_id = os.path.basename(pad.hid_dir)
        else:
            res = HidResources(None, None, [], None)
            hid_id = pad.event
        controllers.append(
            Controller(
                hid_id=hid_id,
                name=pad.name,
                gamepad=gamepad,
                touchpad=touchpad,
                hidraw=res.hidraw,
                lightbar=res.lightbar,
                player_leds=res.player_leds,
                battery=res.battery,
            )
        )
    return controllers


_MIC_BIT = 0x04
_USB_REPORT_ID = 0x01
_USB_BUTTONS2 = 10
_BT_REPORT_ID = 0x31
_BT_BUTTONS2 = 11


def mic_pressed(report: bytes) -> bool | None:
    """Mic button state from a raw DualSense input report; None if the report doesn't carry it."""
    if not report:
        return None
    if report[0] == _USB_REPORT_ID:
        offset = _USB_BUTTONS2
    elif report[0] == _BT_REPORT_ID:
        offset = _BT_BUTTONS2
    else:
        return None
    if len(report) <= offset:
        return None
    return bool(report[offset] & _MIC_BIT)


class MicButtonReader:
    def __init__(self, path: str) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        self._down = False

    def feed(self, report: bytes) -> bool | None:
        """Track one report; the new state on a press/release edge, else None."""
        state = mic_pressed(report)
        if state is None or state == self._down:
            return None
        self._down = state
        return state

    def read_transitions(self) -> list[bool]:
        """Drain pending reports; True = pressed, False = released, in order."""
        transitions: list[bool] = []
        while True:
            try:
                report = os.read(self.fd, 128)
            except BlockingIOError:
                return transitions
            if not report:
                return transitions
            edge = self.feed(report)
            if edge is not None:
                transitions.append(edge)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
