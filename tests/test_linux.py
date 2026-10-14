from __future__ import annotations

import ctypes
import os
import re
import socket
import tempfile
import unittest
import zlib

from ps_oneshot.linux import evdev
from ps_oneshot.linux.dualsense import (
    Controller,
    MicButtonReader,
    build_output_report,
    hid_resources,
    mic_pressed,
    pair_nodes,
    parse_capabilities,
    scan_nodes,
    trigger_feedback,
    trigger_off,
    trigger_vibration,
    trigger_weapon,
)
from ps_oneshot.linux.uinput import VirtualPointer

_HEADER = "/usr/include/linux/input-event-codes.h"
BITS = 8 * ctypes.sizeof(ctypes.c_long)
HID = "0003:054C:0CE6.0017"


def caps(*codes: int) -> str:
    """Encode codes like the kernel's sysfs bitmap (MSW first, leading zero words trimmed)."""
    words = [0] * (max(codes) // BITS + 1)
    for code in codes:
        words[code // BITS] |= 1 << (code % BITS)
    return " ".join(f"{w:x}" for w in reversed(words))


class FakeSysfs:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "class", "input"))

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def put(self, path: str, text: str) -> None:
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text + "\n")

    def hid(self, hid_id: str = HID) -> str:
        path = os.path.join(self.root, "devices", "usb", hid_id)
        os.makedirs(path, exist_ok=True)
        return path

    def event(self, n: int, name: str, keys: tuple[int, ...], axes: tuple[int, ...], *,
              hid: str | None = None, phys: str = "", uniq: str = "") -> None:
        input_dir = os.path.join(self.root, "devices", "virtual", "input", f"input{n}")
        if hid is not None:
            input_dir = os.path.join(hid, "input", f"input{n}")
        os.makedirs(os.path.join(input_dir, "capabilities"))
        if hid is not None:
            os.symlink(hid, os.path.join(input_dir, "device"))
        rel = os.path.relpath(input_dir, self.root)
        self.put(f"{rel}/name", name)
        self.put(f"{rel}/phys", phys)
        self.put(f"{rel}/uniq", uniq)
        self.put(f"{rel}/capabilities/key", caps(*keys) if keys else "0")
        self.put(f"{rel}/capabilities/abs", caps(*axes) if axes else "0")
        event_dir = os.path.join(self.root, "class", "input", f"event{n}")
        os.makedirs(event_dir)
        os.symlink(input_dir, os.path.join(event_dir, "device"))


PAD_KEYS = (evdev.BTN_SOUTH, evdev.BTN_EAST, evdev.BTN_MODE)
PAD_AXES = (evdev.ABS_X, evdev.ABS_Y, evdev.ABS_HAT0X)
TP_KEYS = (evdev.BTN_LEFT, evdev.BTN_TOUCH)
TP_AXES = (evdev.ABS_X, evdev.ABS_MT_SLOT, evdev.ABS_MT_POSITION_X, evdev.ABS_MT_POSITION_Y)
DS = "Sony Interactive Entertainment DualSense Wireless Controller"


class CapabilityParsingTest(unittest.TestCase):
    def test_roundtrip_multiword(self) -> None:
        codes = {evdev.BTN_SOUTH, evdev.BTN_THUMBR, evdev.KEY_NAMES["a"], 0x2FF}
        self.assertEqual(parse_capabilities(caps(*codes)), codes)

    def test_real_dualsense_key_bitmap(self) -> None:
        # Verbatim from a USB DualSense on x86_64.
        if BITS != 64:
            self.skipTest("bitmap captured on a 64-bit kernel")
        codes = parse_capabilities("7fdb000000000000 0 0 0 0")
        self.assertIn(evdev.BTN_SOUTH, codes)
        self.assertIn(evdev.BTN_THUMBR, codes)
        self.assertNotIn(evdev.BTN_LEFT, codes)

    def test_empty(self) -> None:
        self.assertEqual(parse_capabilities("0"), set())
        self.assertEqual(parse_capabilities(""), set())


class GroupingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fs = FakeSysfs()

    def tearDown(self) -> None:
        self.fs.cleanup()

    def test_hid_siblings_and_resources(self) -> None:
        hid = self.fs.hid()
        self.fs.event(22, DS, PAD_KEYS, PAD_AXES, hid=hid, uniq="a0:ab")
        self.fs.event(23, f"{DS} Motion Sensors", (), (evdev.ABS_X, evdev.ABS_RX), hid=hid)
        self.fs.event(24, f"{DS} Touchpad", TP_KEYS, TP_AXES, hid=hid)
        # an unrelated touchpad on a different HID device must not be grabbed
        self.fs.event(3, "Laptop Touchpad", TP_KEYS, TP_AXES, hid=self.fs.hid("0018:04F3:3140.0001"))
        for led in ("input32:white:player-2", "input32:rgb:indicator", "input32:white:player-1",
                    "input32:white:player-5", "input32:white:player-3", "input32:white:player-4"):
            self.fs.put(f"devices/usb/{HID}/leds/{led}/brightness", "0")
        self.fs.put(f"devices/usb/{HID}/hidraw/hidraw18/dev", "243:18")
        self.fs.put(f"devices/usb/{HID}/power_supply/ps-controller-battery-a0:ab/capacity", "70")

        pairs = pair_nodes(scan_nodes(self.fs.root), "DualSense|Wireless Controller", "ps-oneshot|Steam")
        self.assertEqual([(p.event, t.event if t else None) for p, t in pairs], [("event22", "event24")])
        pad = pairs[0][0]
        self.assertEqual(os.path.basename(pad.hid_dir or ""), HID)
        self.assertEqual(pad.uniq, "a0:ab")

        res = hid_resources(pad.hid_dir or "", dev_root="/dev")
        self.assertEqual(res.hidraw, "/dev/hidraw18")
        self.assertTrue((res.lightbar or "").endswith("input32:rgb:indicator"))
        self.assertEqual([p[-1] for p in res.player_leds], list("12345"))
        self.assertTrue((res.battery or "").endswith("ps-controller-battery-a0:ab"))

    def test_uinput_fallback_requires_name_prefix_and_phys(self) -> None:
        self.fs.event(30, DS, PAD_KEYS, PAD_AXES, phys="fake-1/input0")
        self.fs.event(31, f"{DS} Touchpad", TP_KEYS, TP_AXES, phys="fake-2/input0")
        self.fs.event(32, f"{DS} Touchpad", TP_KEYS, TP_AXES, phys="fake-1/input0")
        self.fs.event(33, "Other Touchpad", TP_KEYS, TP_AXES, phys="fake-1/input0")
        pairs = pair_nodes(scan_nodes(self.fs.root), "DualSense", "")
        self.assertEqual([(p.event, t.event if t else None) for p, t in pairs], [("event30", "event32")])
        self.assertIsNone(pairs[0][0].hid_dir)

    def test_match_ignore_and_numeric_order(self) -> None:
        self.fs.event(10, "ps-oneshot keyboard", PAD_KEYS, PAD_AXES)
        self.fs.event(9, "Steam Virtual Wireless Controller", PAD_KEYS, PAD_AXES)
        self.fs.event(2, "Xbox Wireless Controller", PAD_KEYS, PAD_AXES)
        self.fs.event(11, DS, PAD_KEYS, PAD_AXES)
        self.fs.event(12, f"{DS} no stick", PAD_KEYS, ())
        pairs = pair_nodes(scan_nodes(self.fs.root), "DualSense|Wireless Controller", "ps-oneshot|Steam")
        self.assertEqual([p.event for p, _ in pairs], ["event2", "event11"])


class LedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bar = os.path.join(self.tmp.name, "input1:rgb:indicator")
        os.makedirs(self.bar)
        with open(os.path.join(self.bar, "max_brightness"), "w") as f:
            f.write("255\n")
        self.ctrl = Controller(hid_id=HID, name=DS, gamepad=None, lightbar=self.bar)  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def read(self, name: str) -> str:
        with open(os.path.join(self.bar, name)) as f:
            return f.read()

    def test_writes_and_skips_unchanged(self) -> None:
        self.assertTrue(self.ctrl.set_lightbar((255, 0, 64), 0.5))
        self.assertEqual(self.read("multi_intensity"), "255 0 64")
        self.assertEqual(self.read("brightness"), "128")
        os.remove(os.path.join(self.bar, "brightness"))
        self.assertTrue(self.ctrl.set_lightbar((255, 0, 64), 0.5))
        self.assertFalse(os.path.exists(os.path.join(self.bar, "brightness")))

    def test_permission_denied_returns_false(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores file modes")
        os.chmod(self.bar, 0o500)
        try:
            with self.assertLogs("ps_oneshot.linux.dualsense", "WARNING") as logs:
                self.assertFalse(self.ctrl.set_lightbar((1, 2, 3), 1.0))
            self.assertIn("70-ps-oneshot.rules", logs.output[0])
        finally:
            os.chmod(self.bar, 0o700)

    def test_no_leds(self) -> None:
        ctrl = Controller(hid_id="event5", name="pad", gamepad=None)  # type: ignore[arg-type]
        self.assertFalse(ctrl.set_lightbar((1, 2, 3), 1.0))
        self.assertFalse(ctrl.set_player_leds(0b101))
        self.assertEqual(ctrl.battery_state(), (None, "Unknown"))


def usb_report(buttons2: int) -> bytes:
    report = bytearray(64)
    report[0] = 0x01
    report[10] = buttons2
    return bytes(report)


def bt_report(buttons2: int) -> bytes:
    report = bytearray(78)
    report[0] = 0x31
    report[11] = buttons2
    return bytes(report)


class MicReportTest(unittest.TestCase):
    def test_layouts(self) -> None:
        self.assertTrue(mic_pressed(usb_report(0x04)))
        self.assertFalse(mic_pressed(usb_report(0x03)))  # PS + touchpad click, not mic
        self.assertTrue(mic_pressed(bt_report(0x05)))
        self.assertFalse(mic_pressed(bt_report(0x00)))

    def test_ignored_reports(self) -> None:
        self.assertIsNone(mic_pressed(b""))
        self.assertIsNone(mic_pressed(bytes([0x01]) + bytes(9)))  # BT "simple" 10-byte report
        self.assertIsNone(mic_pressed(bytes([0x05]) + bytes(40)))  # feature/other ids

    def test_read_transitions_reports_edges_in_order(self) -> None:
        # SOCK_SEQPACKET keeps report boundaries like hidraw (one report per read()).
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ours.setblocking(False)
        reader = MicButtonReader.__new__(MicButtonReader)
        reader.path, reader.fd, reader._down = "socket", ours.detach(), False
        try:
            self.assertEqual(reader.read_transitions(), [])
            for report in (usb_report(4), usb_report(4), bytes([0x05, 0xFF]), usb_report(0), bt_report(4)):
                theirs.send(report)
            self.assertEqual(reader.read_transitions(), [True, False, True])
            theirs.send(bt_report(4))
            self.assertEqual(reader.read_transitions(), [])  # still held
            theirs.send(bt_report(0))
            self.assertEqual(reader.read_transitions(), [False])
        finally:
            reader.close()
            theirs.close()


class TriggerEffectTest(unittest.TestCase):
    def test_off(self) -> None:
        self.assertEqual(trigger_off(), b"\x05" + bytes(10))

    def test_weapon_zone_packing(self) -> None:
        self.assertEqual(trigger_weapon(4, 6, 6), bytes([0x25, 0x50, 0x00, 0x05]) + bytes(7))
        self.assertEqual(trigger_weapon(2, 8, 1), bytes([0x25, 0x04, 0x01, 0x00]) + bytes(7))
        self.assertEqual(trigger_weapon(3, 5, 0), trigger_off())

    def test_feedback_packing(self) -> None:
        # zones 3..9 active, 3-bit force (strength-1 = 4) per active zone
        expected = bytes([0x21]) + (0x3F8).to_bytes(2, "little") + (0o4444444000).to_bytes(4, "little")
        self.assertEqual(trigger_feedback(3, 5), expected + bytes(4))
        self.assertEqual(trigger_feedback(0, 8)[1:7], bytes([0xFF, 0x03, 0xFF, 0xFF, 0xFF, 0x3F]))
        self.assertEqual(trigger_feedback(9, 0), trigger_off())

    def test_vibration_packing(self) -> None:
        # zones 0..9, 3-bit amplitude (8-1 = 7) per zone, frequency in byte 9
        expected = bytes([0x26, 0xFF, 0x03, 0xFF, 0xFF, 0xFF, 0x3F, 0x00, 0x00, 45, 0x00])
        self.assertEqual(trigger_vibration(0, 8, 45), expected)
        self.assertEqual(trigger_vibration(0, 0, 45), trigger_off())
        self.assertEqual(trigger_vibration(5, 4, 0), trigger_off())

    def test_range_errors(self) -> None:
        for args in ((1, 5, 3), (8, 9, 3), (4, 4, 3), (4, 9, 3), (4, 6, 9), (4, 6, -1)):
            with self.assertRaises(ValueError, msg=str(args)):
                trigger_weapon(*args)
        for args in ((10, 3), (-1, 3), (3, 9)):
            with self.assertRaises(ValueError, msg=str(args)):
                trigger_feedback(*args)


class OutputReportTest(unittest.TestCase):
    def test_usb_layout(self) -> None:
        left = trigger_weapon(4, 6, 6)
        report = build_output_report(False, 7, None, left)
        self.assertEqual(len(report), 63)
        self.assertEqual(report[0], 0x02)
        self.assertEqual(report[1], 0x08)  # valid_flag0: left trigger only
        self.assertEqual(report[1 + 21 : 1 + 32], left)
        self.assertEqual(report[2:22], bytes(20))  # flag1, motors, audio, LEDs untouched
        self.assertEqual(report[33:], bytes(30))
        both = build_output_report(False, 0, trigger_off(), left)
        self.assertEqual(both[1], 0x0C)
        self.assertEqual(both[1 + 10 : 1 + 21], trigger_off())

    def test_bluetooth_framing_and_crc(self) -> None:
        right = trigger_feedback(2, 4)
        report = build_output_report(True, 0x1B, right, None)
        self.assertEqual(len(report), 78)
        self.assertEqual(report[:4], bytes([0x31, 0xB0, 0x10, 0x04]))  # seq masked to 4 bits
        self.assertEqual(report[3 + 10 : 3 + 21], right)
        self.assertEqual(report[3 + 21 : -4], bytes(78 - 4 - 24))
        crc = zlib.crc32(bytes([0xA2]) + report[:74])
        self.assertEqual(int.from_bytes(report[74:], "little"), crc)

    def test_rejects_wrong_effect_size(self) -> None:
        with self.assertRaises(ValueError):
            build_output_report(False, 0, b"\x05", None)

    def test_mic_mute_sets_led_and_hardware_mute_only(self) -> None:
        for muted in (True, False):
            report = build_output_report(False, 0, None, None, mic_mute=muted)
            self.assertEqual(report[1:3], bytes([0x00, 0x03]))  # flag0 clear; flag1 = mic LED + power save
            self.assertEqual(report[1 + 8], 1 if muted else 0)  # mute LED
            self.assertEqual(report[1 + 9], 0x10 if muted else 0)  # power_save_control MIC_MUTE: the mic itself
            self.assertEqual(report[3:9] + report[11:], bytes(len(report) - 5))  # all else zero

    def test_controller_writes_hidraw_with_bt_sequence(self) -> None:
        with tempfile.NamedTemporaryFile() as node:
            ctrl = Controller(hid_id="0005:054C:0CE6.0017", name=DS, gamepad=None,  # type: ignore[arg-type]
                              hidraw=node.name)
            self.assertTrue(ctrl.set_trigger_effect(left=trigger_weapon(4, 6, 6)))
            self.assertTrue(ctrl.set_trigger_effect(left=trigger_off()))
            ctrl._close_hidraw()
            data = node.read()
        self.assertEqual(len(data), 2 * 78)
        self.assertEqual((data[1], data[78 + 1]), (0x00, 0x10))
        self.assertEqual(data[78 + 3 + 21 : 78 + 3 + 32], trigger_off())

    def test_controller_without_hidraw(self) -> None:
        ctrl = Controller(hid_id="event5", name="pad", gamepad=None)  # type: ignore[arg-type]
        with self.assertLogs("ps_oneshot.linux.dualsense", "WARNING"):
            self.assertFalse(ctrl.set_trigger_effect(right=trigger_off()))


class ScrollAccumulationTest(unittest.TestCase):
    def test_truncates_toward_zero(self) -> None:
        self.assertEqual(VirtualPointer._accumulate(119), (119, 0))
        self.assertEqual(VirtualPointer._accumulate(250), (10, 2))
        self.assertEqual(VirtualPointer._accumulate(-130), (-10, -1))


class ConstantsTest(unittest.TestCase):
    def test_against_kernel_header(self) -> None:
        if not os.path.exists(_HEADER):
            self.skipTest("kernel headers not installed")
        with open(_HEADER) as f:
            defs = dict(re.findall(r"#define\s+(\w+)\s+(0x[0-9a-fA-F]+|\d+)\b", f.read()))
        for name in dir(evdev):
            if re.match(r"(EV|SYN|BTN|ABS|REL|INPUT_PROP)_", name) and name in defs:
                self.assertEqual(getattr(evdev, name), int(defs[name], 0), name)
        aliases = {"print": "KEY_SYSRQ", "esc": "KEY_ESC", "period": "KEY_DOT", "playpause": "KEY_PLAYPAUSE"}
        for key, code in evdev.KEY_NAMES.items():
            macro = aliases.get(key, f"KEY_{key.upper()}")
            if macro in defs:
                self.assertEqual(code, int(defs[macro], 0), key)

    def test_ff_effect_layout(self) -> None:
        self.assertEqual(ctypes.sizeof(evdev.FFEffect), 48 if ctypes.sizeof(ctypes.c_void_p) == 8 else 44)
        self.assertEqual(evdev.FFEffect.u.offset, 16 if ctypes.sizeof(ctypes.c_void_p) == 8 else 14)


if __name__ == "__main__":
    unittest.main()
