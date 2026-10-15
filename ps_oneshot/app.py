"""Main daemon: controllers → bindings/radial menus → Hyprland, plus lights, rumble, HERD and dictation."""

from __future__ import annotations

import logging
import math
import re
import shutil
import signal
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TypeVar

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import APP_NAME, apps, sounds  # noqa: E402
from . import lights as fx  # noqa: E402
from .actions import ActionRunner, spawn  # noqa: E402
from .config import TRIGGERS, Binding, Config  # noqa: E402
from .couch import CouchMode  # noqa: E402
from .dictation import CaptureWatcher, DictationLog, capture_path  # noqa: E402
from .favorites import Favorites  # noqa: E402
from .herd import Agent, HerdProvider, agent_in_window, top_status, transitions, waiting_queue  # noqa: E402
from .hypr import EventStream, Hyprland  # noqa: E402
from .linux import dualsense  # noqa: E402
from .linux.evdev import KEY_NAMES, list_event_nodes  # noqa: E402
from .linux.uinput import VirtualKeyboard, VirtualPointer  # noqa: E402
from .labels import cheatsheet_sections  # noqa: E402
from .menus import Hooks, RadialMenus  # noqa: E402
from .pad import Pad  # noqa: E402
from .sources import Choice, LevelControl, MicMuteWatcher, next_stop, read_number  # noqa: E402
from .terminals import FootFollower  # noqa: E402
from .ui.cheatsheet import CheatSheet  # noqa: E402
from .ui.overlay import NAMESPACE as OVERLAY_NAMESPACE  # noqa: E402
from .ui.radial import RadialMenu  # noqa: E402
from .ui.theme import load_theme  # noqa: E402

log = logging.getLogger(__name__)

LIGHT_FRAME_MS = 50
SHOT_WAIT_S = 20.0  # a shot waits this long for its transcription before giving up
SHOT_POLL_MS = 40  # how often a shot checks whether VoxType has finished typing
SHOT_SETTLE_MS = 120  # pause between the last typed character and Enter
SHOT_KICK_MS = 160  # how long the trigger itself buzzes after a shot
CABLE_SWITCH_S = 15.0  # moving a controller from Bluetooth onto its cable gives up after this long
KEY_ENTER = KEY_NAMES["enter"]
KEY_BACKSPACE = KEY_NAMES["backspace"]
VirtualT = TypeVar("VirtualT", VirtualKeyboard, VirtualPointer)


@dataclass
class Held:
    binding: Binding | None
    modifier: bool = False
    chord: bool = False  # another button was pressed while this modifier was held
    hold_timer: int = 0
    repeat_timer: int = 0
    hold_fired: bool = False


@dataclass
class TriggerPress:
    binding: Binding
    full_fired: bool = False
    started: float = field(default_factory=time.monotonic)


@dataclass
class CableSwitch:
    """A controller on its way from Bluetooth onto the cable it was plugged into (App._switch_to_cable)."""

    usb: dualsense.RefusedUsb
    mac: str
    deadline: float
    reattached: bool = False


class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.hypr = Hyprland()
        self.couch = CouchMode(self.hypr, cfg.section("couch"))
        self.keyboard = self._virtual(VirtualKeyboard)
        self.pointer = self._virtual(VirtualPointer)
        self.actions = ActionRunner(
            self.hypr,
            self._key_tap,
            KEY_NAMES,
            {
                "radial": self._open_radial, "herd": self._herd_action, "ui": self._ui_action,
                "app": self._app_action, "dictation": self._dictation_action, "sounds": self._sounds_action,
                "rumble": self._rumble_action, "agent": self._agent_action, "touchpad": self._touchpad_action,
                "zoom": self._zoom_action, "text": self._text_action, "couch": self._couch_action,
                "brightness": self._brightness_action,
                "launch": lambda app_id: apps.launch(self.hypr, app_id, cfg.get("apps", "launch")),
            },
        )
        theme = load_theme()
        accent = theme.accent
        idle_rgb = (int(accent[0] * 255), int(accent[1] * 255), int(accent[2] * 255))
        self.lights = fx.LightEngine(cfg.section("lights"), idle_rgb)
        self.cheatsheet = CheatSheet(load_theme)
        self.events = EventStream(self.hypr.events_path, self._on_hypr_event)
        herd_cfg = cfg.section("herd")
        self.herd = HerdProvider(herd_cfg, self._on_herd, self.hypr) if HerdProvider.available(herd_cfg) else None
        self.favorites = Favorites(cfg.section("favorites"))
        self.menus = RadialMenus(cfg, RadialMenu(load_theme), self.herd, self.favorites, Hooks(
            run_action=self.actions.run, rumble=self.rumble, monitor=lambda: self.monitor,
            capture_sticks=self._sync_stick_capture, settings=self._settings_choices,
            window_class=lambda: self.window_class, paused=lambda: self.paused, battery=self._battery_level,
        ))
        self.dictation = DictationLog(suffix=_voxtype_suffix())
        self.capture: CaptureWatcher | None = None
        self.mic = MicMuteWatcher(self._on_mic_mute)
        self.foot: FootFollower | None = None
        self.brightness = LevelControl(cfg.section("brightness"))
        self.sounds_on = bool(sounds.load_state("sounds", bool(cfg.get("sounds", "enabled"))))
        self.speaker = sounds.BluetoothSpeaker()
        self.rumble_on = bool(sounds.load_state("rumble", bool(cfg.get("rumble", "enabled"))))
        self._shot_window: tuple[str, float] | None = None  # (window, deadline): next dictation gets sent
        self._text_size: tuple[float, float] | None = None  # (size, when) of our last system text-size step
        self._last_full: tuple[Pad, str, float] | None = None  # (pad, trigger, held s) of the last full press
        self._voxtype_state = capture_path(cfg.get("voxtype", "state_file"))
        touchpad = cfg.raw["touchpad"]
        touchpad["sensitivity"] = float(sounds.load_state("touchpad_sensitivity", touchpad["sensitivity"]))
        sounds_cfg = cfg.raw["sounds"]
        sounds_cfg["volume"] = float(sounds.load_state("sounds_volume", sounds_cfg["volume"]))

        self.pads: dict[str, Pad] = {}
        self.held: dict[tuple[str, str], Held] = {}
        self.trigger_presses: dict[tuple[str, str], TriggerPress] = {}
        self.paused_manual = False
        self.paused_auto = False
        self.menu_layers: Counter[str] = Counter()
        self.monitor: str | None = None
        self.window_class = ""
        self.window_title = ""
        self.window = ""
        self.battery_low = False
        self._battery_warned: set[str] = set()
        self._herd_status: dict[str, str] = {}
        self._jump: tuple[str, float] | None = None
        self._light_timer = 0
        self._nodes: set[str] = set()
        self._cable: CableSwitch | None = None
        self._cable_tried: set[str] = set()  # refused USB interfaces already handled, so a failure doesn't loop
        self._menu_re = re.compile(cfg.get("menu", "namespaces"))
        self._game_class = re.compile(cfg.get("game", "pause_classes"))
        self._game_title = re.compile(cfg.get("game", "pause_titles"))
        self._terminal_re = re.compile(cfg.get("dictation", "terminal_classes"))
        self._agent_class = re.compile(cfg.get("agents", "window_classes"))
        self._agent_title = re.compile(cfg.get("agents", "window_titles"))
        self.loop = GLib.MainLoop()

    @staticmethod
    def _virtual(kind: type[VirtualT]) -> VirtualT | None:
        try:
            return kind()
        except OSError as error:
            log.warning("cannot create %s (/dev/uinput: %s); key/pointer output disabled", kind.__name__, error.strerror)
            return None

    # lifecycle -----------------------------------------------------------------

    def run(self) -> None:
        self.monitor = self.hypr.focused_monitor()
        self.hypr.no_layer_animation(OVERLAY_NAMESPACE)  # overlays run their own, much quicker, open animation
        self.couch.recover()
        self.menu_layers.update(ns for ns in self.hypr.layer_namespaces() if self._menu_re.search(ns))
        window_class, self.window_title, self.window = self.hypr.active_window()
        self.window_class = window_class
        self._update_game(window_class, self.window_title)
        self.capture = CaptureWatcher(capture_path(self.cfg.get("dictation", "capture_file")), self._on_dictation)
        if self.cfg.get("foot", "follow"):
            self.foot = FootFollower(self.hypr, after=self._after_foot_follow)
        self.events.start()
        self.mic.start()
        if self.herd is not None:
            self.herd.start()
        self._scan()
        GLib.timeout_add_seconds(int(self.cfg.get("devices", "rescan_seconds")), self._scan)
        GLib.timeout_add_seconds(60, self._check_battery)
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._quit)
        log.info("ps-oneshot running (preset %s, herd %s)", self.cfg.preset, "on" if self.herd else "off")
        self.loop.run()

    def _quit(self) -> bool:
        for pad in list(self.pads.values()):
            pad.controller.set_player_leds(0)
            pad.controller.set_lightbar(tuple(self.cfg.get("lights", "paused")), 0.5)
            self._apply_trigger_effects(pad, enabled=False)
            pad.close()
        self.pads.clear()
        if self.herd is not None:
            self.herd.stop()
        self.mic.stop()
        if self.capture is not None:
            self.capture.close()
        if self.foot is not None:
            self.foot.close()
        for device in (self.keyboard, self.pointer):
            if device is not None:
                device.close()
        self.loop.quit()
        return False

    def _after_foot_follow(self) -> None:
        for action in self.cfg.get("foot", "after"):
            self.actions.run(action)

    @property
    def paused(self) -> bool:
        return self.paused_manual or self.paused_auto

    # devices -------------------------------------------------------------------

    def _scan(self) -> bool:
        self._switch_to_cable()
        nodes = set(list_event_nodes())
        if nodes == self._nodes:
            return True
        known = {pad.controller.gamepad.path for pad in self.pads.values()}
        devices = self.cfg.section("devices")
        for controller in dualsense.discover(devices["match"], devices["ignore"]):
            if controller.gamepad.path in known:
                controller.close()
                continue
            pad = Pad(
                controller, self.cfg.raw, self.pointer,
                self._on_button, self._on_trigger, self._on_stick, self._on_lost,
            )
            pad.attach()
            pad.set_active(not self.paused)
            pad.capture_sticks(self.menus.is_open or self.cheatsheet.visible)
            self.pads[controller.gamepad.path] = pad
            log.info(
                "connected %s (touchpad %s, lights %s, mic button %s)", controller.name,
                "yes" if controller.touchpad else "no", "yes" if controller.lightbar else "no",
                "yes" if controller.hidraw else "no",
            )
            self._apply_trigger_effects(pad, enabled=not self.paused)
            self._sync_mic_mute()
            self.rumble(fx.RUMBLE_CONFIRM)
            self.refresh_lights()
            self._check_battery()
        devices_seen = dualsense.pair_nodes(dualsense.scan_nodes(), devices["match"], devices["ignore"])
        # A controller that couldn't be opened yet (udev still granting access) is retried on the next scan.
        self._nodes = nodes if len(self.pads) >= len(devices_seen) else set()
        return True

    def _switch_to_cable(self) -> None:
        """Plugged in while on Bluetooth, a controller stays on Bluetooth: the kernel refuses its USB side as a
        duplicate, so the microphone and speaker on the cable stay silent. Disconnect Bluetooth, then have the
        kernel take the refused USB interface again, and the controller runs on the cable. Every scan."""
        switch = self._cable
        if switch is None:
            self._start_cable_switch()
            return
        on_cable = [p for p in self.pads.values() if p.controller.uniq == switch.mac and not p.controller.bluetooth]
        if on_cable:
            log.info("%s now on the cable", on_cable[0].name)
            self._cable = None
        elif time.monotonic() > switch.deadline:
            log.warning("could not move the controller from Bluetooth to the cable")
            self._cable = None
            self._cable_hint()
        elif not switch.reattached and all(p.controller.uniq != switch.mac for p in self.pads.values()):
            switch.reattached = True  # Bluetooth is gone, so the kernel can take the USB side now
            try:
                dualsense.reattach_usb(switch.usb)
            except OSError as error:
                log.warning("cannot re-attach %s: %s; install %s", switch.usb.usb_node, error, dualsense.UDEV_RULE)
            self._nodes = set()  # look for it on the next scan

    def _start_cable_switch(self) -> None:
        bluetooth = [p for p in self.pads.values() if p.controller.bluetooth]
        if not bluetooth or self.paused:  # in game mode the controller is left alone
            return
        refused = [usb for usb in dualsense.refused_usb() if usb.hid_id not in self._cable_tried]
        if not refused:
            return
        self._cable_tried.add(refused[0].hid_id)
        pad = bluetooth[0]
        if len(bluetooth) > 1 or not pad.controller.uniq or not shutil.which("bluetoothctl"):
            self._cable_hint()  # can't tell which controller was plugged in, or can't drop its Bluetooth
            return
        log.info("%s plugged in while on Bluetooth: moving it to the cable", pad.name)
        spawn(["notify-send", "-a", APP_NAME, "-t", "2500", "Controller plugged in: moving it to the cable"])
        spawn(["bluetoothctl", "disconnect", pad.controller.uniq])
        self._cable = CableSwitch(refused[0], pad.controller.uniq, time.monotonic() + CABLE_SWITCH_S)

    def _cable_hint(self) -> None:
        spawn([
            "notify-send", "-a", APP_NAME, "-t", "8000",
            "Controller plugged in but still on Bluetooth, where its microphone doesn't work. "
            "Disconnect it from Bluetooth, then plug the cable in again.",
        ])

    def _on_lost(self, pad: Pad) -> None:
        path = pad.controller.gamepad.path
        if self.pads.pop(path, None) is None:
            return
        log.info("disconnected %s", pad.name)
        for key in [k for k in self.held if k[0] == path]:
            held = self.held.pop(key)
            self._cancel(held)
            if held.binding and held.binding.release:
                self.actions.run(held.binding.release)  # e.g. stop push-to-talk when the pad drops mid-word
        for key in [k for k in self.trigger_presses if k[0] == path]:
            press = self.trigger_presses.pop(key)
            if press.binding.release and not press.full_fired:
                self.actions.run(press.binding.release)
        pad.close()
        self.refresh_lights()
        self._sync_stick_buttons()  # its held modifiers are gone
        self._nodes = set()  # force a rescan for reconnects
        if not self.pads:
            self.menus.close()
            self._hide_cheatsheet()

    def _sync_stick_capture(self, _radial_open: bool = False) -> None:
        """An open radial or cheat sheet owns the sticks: no scrolling, no stick buttons underneath."""
        captured = self.menus.is_open or self.cheatsheet.visible
        for pad in self.pads.values():
            pad.capture_sticks(captured)

    def _show_cheatsheet(self) -> None:
        self.cheatsheet.show(cheatsheet_sections(self.cfg), self.monitor)
        self._sync_stick_capture()

    def _hide_cheatsheet(self) -> None:
        self.cheatsheet.hide()
        self._sync_stick_capture()

    def _on_mic_mute(self, muted: bool) -> None:
        log.debug("microphone %s", "muted" if muted else "live")
        self._sync_mic_mute()

    def _sync_mic_mute(self) -> bool:
        """The controller's own microphone and its mic LED follow the system microphone: muted and lit
        when it's muted (like the PS5 and the keyboard mute LED), live otherwise. The kernel toggles
        both on every mic-button press, holds for the voice radial included, which would otherwise
        leave the controller mic silently muted (Whisper then "hears" Thank you / Dziękuję)."""
        if self.mic.muted is not None and not self.paused:
            for pad in self.pads.values():
                if pad.controller.hidraw:
                    pad.controller.set_mic_mute(self.mic.muted)
        return False

    def _apply_trigger_effects(self, pad: Pad, enabled: bool) -> None:
        """Staged triggers get a resistance zone that clicks through at the full-press point."""
        triggers = self.cfg.section("triggers")
        effects = {}
        for name in TRIGGERS:
            binding = self.cfg.layers["normal"].get(name)
            if enabled and triggers["adaptive"] and binding is not None and binding.staged:
                start, end = triggers["click_zone"]
                effects[name] = dualsense.trigger_weapon(int(start), int(end), int(triggers["click_strength"]))
            else:
                effects[name] = dualsense.trigger_off()
        if pad.controller.hidraw:
            pad.controller.set_trigger_effect(right=effects["r2"], left=effects["l2"])

    # input ------------------------------------------------------------------------

    def _on_button(self, pad: Pad, button: str, down: bool) -> None:
        key = (pad.controller.gamepad.path, button)
        if button == "mic":
            # The kernel flips the controller mic's mute (and LED) on every press; put back the real state.
            GLib.timeout_add(150, self._sync_mic_mute)
        if not down:
            self.menus.on_release(button)
            held = self.held.pop(key, None)
            if held is not None:
                self._release(held)
                if held.modifier:
                    self._sync_stick_buttons()
            return
        if self.paused:
            # Only the pause binding works while paused (it may be a tap or a hold).
            binding = self.cfg.layers["normal"].get(button)
            if binding is not None and "app:pause" in binding.actions():
                held = self.held[key] = Held(binding=binding)
                self._press(binding, held)
            return
        if self.menus.is_open:
            if self.menus.on_button(button) or button != "ps":
                return
            self.menus.close()  # PS leaves the radial and then does its usual job
        if self.cheatsheet.visible and button == "circle":
            self._hide_cheatsheet()
            return
        if self.cheatsheet.visible and button in ("dpad_up", "dpad_down"):
            self.cheatsheet.scroll_step(1 if button == "dpad_down" else -1)
            return
        layer, binding = self._resolve(button)
        if button in TRIGGERS and binding is not None and binding.staged:
            return  # handled from the analog axis in _on_trigger
        self._mark_chord()
        # A modifier that a held layer (or the menu) binds is that binding, e.g. □ held + ○ = layout toggle.
        if button in self.cfg.modifiers and layer == "normal":
            held = Held(binding=self.cfg.layers["normal"].get(button), modifier=True)
        else:
            held = Held(binding=binding)
        self.held[key] = held
        if held.modifier:
            self._sync_stick_buttons()
        if held.binding is not None:
            self._press(held.binding, held)

    def _sync_stick_buttons(self) -> None:
        """A stick the held modifiers' layers bind (○ + right stick) acts as buttons while they're held,
        whatever its own mode; it scrolls or points again on release."""
        layers = [self.cfg.layers[self.cfg.modifiers[button]] for (_, button), h in self.held.items() if h.modifier]
        sides = {
            side for side, prefix in (("left", "lstick"), ("right", "rstick"))
            if any(f"{prefix}_{d}" in layer for layer in layers for d in ("up", "down", "left", "right"))
        }
        for pad in self.pads.values():
            if pad.stick_buttons != sides:
                pad.force_stick_buttons(sides)

    def _mark_chord(self) -> None:
        for held in self.held.values():
            if held.modifier:
                held.chord = True

    def _active_layers(self) -> list[str]:
        layers = []
        if self.menu_layers and "menu" in self.cfg.layers:
            layers.append("menu")
        layers += [self.cfg.modifiers[button] for (_, button), h in self.held.items() if h.modifier]
        layers.append("normal")
        return list(dict.fromkeys(layers))

    def _resolve(self, button: str) -> tuple[str, Binding | None]:
        for layer in self._active_layers():
            binding = self.cfg.layers[layer].get(button)
            if binding is not None:
                return layer, binding
        return "normal", None

    def _press(self, binding: Binding, held: Held) -> None:
        if binding.press:
            self.actions.run(binding.press)
            if binding.repeat:
                held.repeat_timer = GLib.timeout_add(
                    int(self.cfg.get("input", "repeat_delay_ms")), self._repeat_start, binding.press, held
                )
        if binding.hold:
            held.hold_timer = GLib.timeout_add(int(self.cfg.get("input", "hold_ms")), self._hold_fire, binding, held)
        if binding.light:
            self.refresh_lights()

    def _repeat_start(self, action: str, held: Held) -> bool:
        self.actions.run(action)
        held.repeat_timer = GLib.timeout_add(int(self.cfg.get("input", "repeat_interval_ms")), self._repeat, action)
        return False

    def _repeat(self, action: str) -> bool:
        self.actions.run(action)
        return True

    def _hold_fire(self, binding: Binding, held: Held) -> bool:
        held.hold_timer = 0
        held.hold_fired = True
        assert binding.hold is not None
        if held.chord:
            return False  # a modifier with another button pressed first is a layer, not a hold
        self.rumble(fx.RUMBLE_CONFIRM)
        self.actions.run(binding.hold)
        return False

    @staticmethod
    def _cancel(held: Held) -> None:
        for timer in (held.hold_timer, held.repeat_timer):
            if timer:
                GLib.source_remove(timer)
        held.hold_timer = held.repeat_timer = 0

    def _release(self, held: Held) -> None:
        self._cancel(held)
        binding = held.binding
        if binding is None:
            return
        if binding.tap and not held.hold_fired and not held.chord and (not self.paused or binding.tap == "app:pause"):
            self.actions.run(binding.tap)
        if binding.release:
            self.actions.run(binding.release)
        if binding.press == "ui:cheatsheet" or binding.hold == "ui:cheatsheet":
            self._hide_cheatsheet()  # shown while held; a tap binding toggles instead
        if binding.light:
            self.refresh_lights()

    def _on_trigger(self, pad: Pad, name: str, value: float) -> None:
        key = (pad.controller.gamepad.path, name)
        triggers = self.cfg.section("triggers")
        soft_at, full_at = float(triggers["soft_at"]), float(triggers["full_at"])
        press = self.trigger_presses.get(key)
        if press is None:
            if value < soft_at or self.paused or self.menus.is_open:
                return
            _, binding = self._resolve(name)
            if binding is None or not binding.staged:
                return
            self._mark_chord()
            press = self.trigger_presses[key] = TriggerPress(binding)
            if binding.press:
                self.actions.run(binding.press)
            if binding.light:
                self.refresh_lights()
        if not press.full_fired and value >= full_at:
            press.full_fired = True
            if press.binding.full:
                self._last_full = (pad, name, time.monotonic() - press.started)
                self.rumble(fx.RUMBLE_CONFIRM)
                self.actions.run(press.binding.full)
        elif value < soft_at / 2:
            del self.trigger_presses[key]
            if not press.full_fired and press.binding.soft:  # a full press isn't a soft one
                self.rumble(fx.RUMBLE_TICK)
                self.actions.run(press.binding.soft)
            if press.binding.release:
                self.actions.run(press.binding.release)
            if press.binding.light:
                self.refresh_lights()

    def _on_stick(self, pad: Pad) -> None:
        if self.paused or not (self.menus.is_open or self.cheatsheet.visible):
            return
        x, y = max(pad.sticks.values(), key=lambda v: v[0] ** 2 + v[1] ** 2)
        if self.menus.is_open:
            self.menus.on_stick(x, y)
            return
        deadzone = float(self.cfg.get("input", "stick_deadzone"))
        past = max(0.0, abs(y) - deadzone) / (1.0 - deadzone)
        self.cheatsheet.scroll(math.copysign(past, y))

    def _key_tap(self, codes: list[int]) -> None:
        if self.keyboard is None:
            log.warning("key action ignored: no virtual keyboard")
            return
        self.keyboard.tap(codes)
        if codes == [KEY_ENTER]:
            self.dictation.clear(self.window)  # submitted: earlier phrases are no longer editable text
            if self.sounds_on and self._in_agent_window():
                self._play_sound("submit")

    def _in_agent_window(self) -> bool:
        return bool(self._agent_class.search(self.window_class) and self._agent_title.search(self.window_title))

    def _play_sound(self, key: str) -> None:
        """Sound effect `[sounds] <key>` on the controller's speaker: over Bluetooth straight to the controller,
        wired through its sound card (sounds.play); the default output when neither can take it."""
        cfg = self.cfg.section("sounds")
        if not cfg[key]:
            return  # "" = silent
        if cfg["device"] in ("auto", "controller"):
            pad = next((p for p in self.pads.values() if p.controller.bluetooth and p.controller.hidraw), None)
            if pad is not None and self.speaker.play(pad.controller.hidraw, cfg[key], float(cfg["volume"])):
                return
        sounds.play(cfg[key], cfg)

    # built-in actions ---------------------------------------------------------------

    def _open_radial(self, name: str) -> None:
        self.menus.open(name)

    def _herd_action(self, arg: str) -> None:
        if arg != "jump":
            log.warning("unknown herd action %r", arg)
            return
        queue = waiting_queue(self.herd.agents) if self.herd else []
        if not queue:
            self.rumble(fx.RUMBLE_NOTHING)
            return
        index = 0
        cycle = float(self.cfg.get("herd", "jump_cycle_seconds"))
        if self._jump and time.monotonic() - self._jump[1] < cycle:
            keys = [agent.key for agent in queue]
            if self._jump[0] in keys:
                index = (keys.index(self._jump[0]) + 1) % len(queue)
        agent = queue[index]
        self._jump = (agent.key, time.monotonic())
        self.rumble(fx.RUMBLE_CONFIRM)
        assert self.herd is not None
        self.herd.focus(agent)

    def _dictation_action(self, arg: str) -> None:
        """back: newest phrase in this window, else a word. clear: every phrase since Enter, else the line.
        shoot: stop dictating and press Enter once VoxType has typed it; a quick pull with nothing said
        just presses Enter."""
        if arg == "shoot":
            voxtype = self.cfg.section("voxtype")
            if self.sounds_on:
                self._play_sound("submit")
            self._shot_kick()
            held = self._last_full[2] if self._last_full else float("inf")
            if held < float(voxtype["quick_shot"]):
                # Nothing was said: discard the blip (Whisper invents words for silence) and send.
                self.actions.run("exec:" + voxtype["cancel"])
                self._shot_press_enter(self.window)
                return
            self.actions.run("exec:" + voxtype["shoot"])
            self._shot_window = (self.window, time.monotonic() + SHOT_WAIT_S)
            GLib.timeout_add(SHOT_POLL_MS, self._shot_enter, self.window, time.monotonic() + SHOT_WAIT_S)
            return
        if arg not in ("back", "clear"):
            log.warning("unknown dictation action %r", arg)
            return
        count = self.dictation.pop_last(self.window) if arg == "back" else self.dictation.pop_all(self.window)
        if count:
            if self.keyboard is not None:
                for _ in range(count):
                    self.keyboard.tap([KEY_BACKSPACE])
            return
        fallback = "erase_word" if arg == "back" else "erase_line"
        if self._terminal_re.search(self.window_class):
            fallback += "_terminal"
        self.actions.run("key:" + self.cfg.get("dictation", fallback))

    def _shot_kick(self) -> None:
        """Recoil: a hard rumble kick, and the trigger that fired buzzes under your finger for a moment."""
        self.rumble(fx.RUMBLE_SHOT)
        if self._last_full is None:
            return
        pad, trigger, _ = self._last_full
        if self.rumble_on and pad.controller.hidraw and trigger in TRIGGERS:
            buzz = dualsense.trigger_vibration(0, 8, 45)
            pad.controller.set_trigger_effect(**{"right" if trigger == "r2" else "left": buzz})
            GLib.timeout_add(SHOT_KICK_MS, self._restore_triggers, pad)

    def _restore_triggers(self, pad: Pad) -> bool:
        if pad in self.pads.values():
            self._apply_trigger_effects(pad, enabled=not self.paused)
        return False

    def _agent_action(self, arg: str) -> None:
        if arg != "thinking":
            log.warning("unknown agent action %r", arg)
            return
        agents_cfg = self.cfg.section("agents")
        if not self._in_agent_window():
            if agents_cfg["thinking_elsewhere"]:
                self.actions.run(agents_cfg["thinking_elsewhere"])
            return
        agent = agent_in_window(self.herd.agents, self.window_title) if self.herd else None
        kind = agent.kind if agent else ""
        keys = agents_cfg["thinking"].get(kind, agents_cfg["thinking_default"])
        if not keys:
            spawn(["notify-send", "-a", APP_NAME, "-t", "2000", f"No thinking-level key for {kind or 'this agent'}"])
            return
        self.actions.run("key:" + keys)
        if self.sounds_on:
            self._play_sound("thinking")

    def _touchpad_action(self, arg: str) -> None:
        touchpad = self.cfg.raw["touchpad"]
        factor = {"slower": 1 / 1.25, "faster": 1.25}.get(arg)
        if factor is None:
            log.warning("unknown touchpad action %r", arg)
            return
        touchpad["sensitivity"] = round(min(4.0, max(0.1, touchpad["sensitivity"] * factor)), 2)
        sounds.save_state("touchpad_sensitivity", touchpad["sensitivity"])
        spawn(["notify-send", "-a", APP_NAME, "-t", "1200", f"Touchpad speed {touchpad['sensitivity']:.2f}"])

    def _on_dictation(self, text: str) -> None:
        log.debug("dictated %d chars into %s", len(text), self.window)
        shot, self._shot_window = self._shot_window, None
        if shot is not None and shot[0] == self.window and time.monotonic() < shot[1]:
            self.dictation.clear(self.window)  # it's about to be sent: nothing here stays editable
            return
        self.dictation.add(self.window, text)

    def _shot_enter(self, window: str, deadline: float) -> bool:
        """Enter once VoxType is idle again: done transcribing and typing (or there was nothing to type)."""
        if self.window != window or time.monotonic() > deadline:
            log.info("shot not sent: %s", "focus moved" if self.window != window else "VoxType still busy")
            return False  # don't send into the wrong window
        try:
            with open(self._voxtype_state, encoding="utf-8") as handle:
                busy = handle.read().strip() != "idle"
        except OSError:
            busy = False
        if busy:
            return True
        GLib.timeout_add(SHOT_SETTLE_MS, self._shot_press_enter, window)
        return False

    def _shot_press_enter(self, window: str) -> bool:
        if self.keyboard is not None and self.window == window:
            self.keyboard.tap([KEY_ENTER])
            self.dictation.clear(window)
            log.info("shot sent")
        else:
            log.info("shot not sent: focus moved")
        return False

    def _ui_action(self, arg: str) -> None:
        if arg != "cheatsheet":
            log.warning("unknown ui action %r", arg)
        elif self.cheatsheet.visible:
            self._hide_cheatsheet()
        else:
            self._show_cheatsheet()

    def _sounds_action(self, arg: str) -> None:
        if arg in ("louder", "quieter"):
            cfg = self.cfg.raw["sounds"]
            step = 0.1 if arg == "louder" else -0.1
            cfg["volume"] = round(min(1.0, max(0.1, float(cfg["volume"]) + step)), 2)
            sounds.save_state("sounds_volume", cfg["volume"])
            spawn(["notify-send", "-a", APP_NAME, "-t", "1200", f"Sound effects volume {cfg['volume']:.0%}"])
            if self.sounds_on:
                self._play_sound("submit")  # hear the new level
            return
        if arg != "toggle":
            log.warning("unknown sounds action %r", arg)
            return
        self.sounds_on = not self.sounds_on
        sounds.save_state("sounds", self.sounds_on)
        spawn(["notify-send", "-a", APP_NAME, "-t", "1500", f"Sound effects {'on' if self.sounds_on else 'off'}"])
        if self.sounds_on:
            self._play_sound("submit")

    def _rumble_action(self, arg: str) -> None:
        if arg != "toggle":
            log.warning("unknown rumble action %r", arg)
            return
        self.rumble_on = not self.rumble_on
        sounds.save_state("rumble", self.rumble_on)
        spawn(["notify-send", "-a", APP_NAME, "-t", "1500", f"Vibration {'on' if self.rumble_on else 'off'}"])
        self.rumble(fx.RUMBLE_CONFIRM)

    def _zoom_action(self, arg: str) -> None:
        """Text size in the focused window, with that app's own zoom keys: in, out or reset."""
        steps = ("in", "out", "reset")
        if arg not in steps:
            log.warning("unknown zoom action %r", arg)
            return
        zoom = self.cfg.section("zoom")
        keys = next(
            (combos for pattern, combos in zoom["classes"].items() if re.search(pattern, self.window_class)),
            zoom["keys"],
        )
        self.actions.run("key:" + keys[steps.index(arg)])

    def _text_action(self, arg: str) -> None:
        """System-wide text size, one of `[text] stops` per step (Omarchy: the panel's Text size)."""
        text = self.cfg.section("text")
        if arg == "reset":
            self._text_size = None
            self.actions.run("exec:" + text["reset"])
            spawn(["notify-send", "-a", APP_NAME, "-t", "1200", "Text size reset"])
            return
        if arg not in ("bigger", "smaller"):
            log.warning("unknown text action %r", arg)
            return
        # A step taken moments ago may not be readable yet (the command applies it asynchronously).
        recent = self._text_size if self._text_size and time.monotonic() - self._text_size[1] < 3 else None
        current = recent[0] if recent else read_number(text["get"])
        if current is None:
            log.warning("text size: %r printed no size", text["get"])
            return
        size = next_stop([float(s) for s in text["stops"]], current, 1 if arg == "bigger" else -1)
        if size is None:
            self.rumble(fx.RUMBLE_NOTHING)  # already at the end of the range
            return
        self._text_size = (size, time.monotonic())
        self.actions.run("exec:" + text["set"].format(size=f"{size:g}"))
        spawn(["notify-send", "-a", APP_NAME, "-t", "1200", f"Text size {size:g}"])

    def _brightness_action(self, arg: str) -> None:
        if arg not in ("up", "down"):
            log.warning("unknown brightness action %r", arg)
            return
        if self.brightness.step(1 if arg == "up" else -1) is None:
            log.warning("brightness: %r printed no level", self.cfg.get("brightness", "get"))

    def _couch_action(self, arg: str) -> None:
        if arg not in ("toggle", "on", "off"):
            log.warning("unknown couch action %r", arg)
            return
        active = self.couch.active
        if (arg == "on" and active) or (arg == "off" and not active):
            return  # never stack scaling on top of an already scaled screen
        couch_cfg = self.cfg.section("couch")
        # Hooks run once the screen has its new size: an OSD restarted now measures it right, and a
        # monitor-profile daemon stopped now is gone long before its next poll would undo the change.
        if active:
            self.couch.exit()
            hooks = couch_cfg["exit"]
            message = "Couch mode off"
        else:
            changed = self.couch.enter()
            hooks = couch_cfg["enter"] if changed else []
            message = "Couch mode on: " + ", ".join(changed) if changed else "Couch mode: no monitor changed"
        for action in hooks:
            self.actions.run(action)
        log.info("%s", message)
        self.rumble(fx.RUMBLE_CONFIRM)
        spawn(["notify-send", "-a", APP_NAME, "-t", "2500", message])

    def _settings_choices(self) -> list[Choice]:
        """The "settings" radial source: PS One Shot's own switches. Ring order is clockwise from the top:
        couch mode on top, toggles upper left/right, mouse speed and effects volume down on the left / up on
        the right, then pause and the cheat sheet at the bottom. Steppers show their level in the hub."""
        speed = f"speed {self.cfg.raw['touchpad']['sensitivity']:.2f}"
        volume = f"volume {self.cfg.get('sounds', 'volume'):.0%}"
        couch = self.couch.active
        return [
            # Not keep_open: the screen rescales under the ring.
            Choice(label="Couch mode", icon="\U000f04b9", action="couch:toggle", active=couch,
                   sublabel="on" if couch else "off"),
            Choice(label="Vibration", icon="\U000f0566" if self.rumble_on else "\U000f0cd9", action="rumble:toggle",
                   sublabel="on" if self.rumble_on else "off", active=self.rumble_on, keep_open=True),
            Choice(label="Mouse +", icon="\uf067", action="touchpad:faster", detail=speed, keep_open=True),
            Choice(label="Effects +", icon="\U000f075d", action="sounds:louder", detail=volume, keep_open=True),
            Choice(label="Pause", icon="\uf04c", action="app:pause", detail="hold PS to resume"),
            Choice(label="Cheat sheet", icon="\uf11c", action="ui:cheatsheet"),
            Choice(label="Effects −", icon="\U000f075e", action="sounds:quieter", detail=volume, keep_open=True),
            Choice(label="Mouse −", icon="\uf068", action="touchpad:slower", detail=speed, keep_open=True),
            Choice(label="Sounds", icon="\uf028" if self.sounds_on else "\uf026", action="sounds:toggle",
                   sublabel="on" if self.sounds_on else "off", active=self.sounds_on, keep_open=True),
        ]

    def _app_action(self, arg: str) -> None:
        if arg != "pause":
            log.warning("unknown app action %r", arg)
            return
        self.paused_manual = not self.paused_manual
        spawn(["notify-send", "-a", APP_NAME, "-t", "1500", "Controller " + ("paused" if self.paused_manual else "active")])
        self._apply_pause()

    def _apply_pause(self) -> None:
        paused = self.paused
        if paused:
            for key, held in list(self.held.items()):
                self._cancel(held)
                if held.binding and held.binding.release:
                    self.actions.run(held.binding.release)  # e.g. stop push-to-talk
                del self.held[key]
            for press in self.trigger_presses.values():
                if press.binding.release and not press.full_fired:
                    self.actions.run(press.binding.release)
            self.trigger_presses.clear()
            self.menus.close()
            self._hide_cheatsheet()
            self._sync_stick_buttons()
        for pad in self.pads.values():
            pad.set_active(not paused)
            self._apply_trigger_effects(pad, enabled=not paused)
            self._sync_mic_mute()
            if paused:
                pad.controller.set_lightbar(tuple(self.cfg.get("lights", "paused")), 0.6)
                pad.controller.set_player_leds(0)
        self.rumble(fx.RUMBLE_PAUSE, force=True)
        self.refresh_lights()

    # Hyprland events -------------------------------------------------------------

    def _on_hypr_event(self, event: str, data: str) -> None:
        if event == "openlayer" and self._menu_re.search(data):
            self.menu_layers[data] += 1
        elif event == "closelayer" and self.menu_layers[data] > 0:
            self.menu_layers[data] -= 1
            if not self.menu_layers[data]:
                del self.menu_layers[data]
        elif event == "configreloaded":
            self.hypr.no_layer_animation(OVERLAY_NAMESPACE)
        elif event == "focusedmon":
            self.monitor = data.split(",", 1)[0]
        elif event == "activewindowv2":
            self.window = data.removeprefix("0x")
        elif event == "closewindow":
            self.dictation.clear(data.removeprefix("0x"))
        elif event == "activewindow":
            window_class, _, title = data.partition(",")
            self.window_class, self.window_title = window_class, title
            if self._update_game(window_class, title):
                self._apply_pause()
        elif event == "windowtitlev2":
            address, _, title = data.partition(",")
            if address.removeprefix("0x") == self.window:
                self.window_title = title  # herdr retitles its window when you switch workspace
                if self._update_game(self.window_class, title):  # e.g. Steam turning into Big Picture
                    self._apply_pause()

    def _update_game(self, window_class: str, title: str) -> bool:
        paused = bool(window_class and self._game_class.search(window_class)) or bool(
            title and self._game_title.search(title)
        )
        changed = paused != self.paused_auto
        if changed:
            log.info("game mode %s (%s)", "on" if paused else "off", window_class or title)
        self.paused_auto = paused
        return changed

    # HERD --------------------------------------------------------------------------

    def _on_herd(self, agents: list[Agent]) -> None:
        fresh = transitions(self._herd_status, agents)
        self._herd_status = {agent.key: agent.status for agent in agents}
        if fresh and self.cfg.get("rumble", "herd_alerts"):
            blocked = any(agent.status == "blocked" for agent in fresh)
            self.rumble(fx.RUMBLE_BLOCKED if blocked else fx.RUMBLE_DONE)
        if self.sounds_on and any(agent.status == "blocked" for agent in fresh):
            self._play_sound("summon")
        self.menus.refresh_source("herd")
        self.refresh_lights()

    # lights / rumble / battery ---------------------------------------------------------

    def _light_inputs(self) -> fx.LightInputs:
        agents = self.herd.agents if self.herd else []
        recording = any(h.binding and h.binding.light == "recording" for h in self.held.values()) or any(
            p.binding.light == "recording" for p in self.trigger_presses.values()
        )
        return fx.LightInputs(
            recording=recording,
            herd_status=top_status(agents),
            waiting=sum(1 for agent in agents if agent.waiting),
            battery_low=self.battery_low,
            paused=self.paused,
        )

    def refresh_lights(self) -> bool:
        frame = self.lights.frame(self._light_inputs(), time.monotonic())
        # A lightbar write goes out as a kernel report without the rumble-mode bit, and the controller stops a
        # running rumble on those, so the lights wait (ticking on) until our rumble is over.
        rumbling = any(pad.controller.rumbling for pad in self.pads.values())
        if frame is not None and not rumbling:
            for pad in self.pads.values():
                pad.controller.set_lightbar(frame.rgb, frame.brightness)
                pad.controller.set_player_leds(frame.player_mask)
        animate = frame is not None and (frame.animated or rumbling) and bool(self.pads)
        if animate and not self._light_timer:
            self._light_timer = GLib.timeout_add(LIGHT_FRAME_MS, self._light_tick)
        return animate

    def _light_tick(self) -> bool:
        keep = self.refresh_lights()
        if not keep:
            self._light_timer = 0
        return keep

    def rumble(self, pattern: tuple[tuple[float, float, int, int], ...], force: bool = False) -> None:
        if not self.rumble_on or (self.paused and not force) or not self.pads:
            return
        delay = 0
        for strong, weak, duration, pause in pattern:
            GLib.timeout_add(delay, self._rumble_step, strong, weak, duration)
            delay += duration + pause

    def _rumble_step(self, strong: float, weak: float, duration: int) -> bool:
        for pad in self.pads.values():
            pad.controller.rumble(strong, weak, duration)
        return False

    def _check_battery(self) -> bool:
        battery = self.cfg.section("battery")
        low_any = False
        for pad in self.pads.values():
            capacity, status = pad.controller.battery_state()
            if capacity is None:
                continue
            discharging = status.lower() == "discharging"
            low = discharging and capacity <= int(battery["low"])
            low_any |= low
            for level, threshold in (("critical", int(battery["critical"])), ("low", int(battery["low"]))):
                tag = f"{pad.controller.hid_id}:{level}"
                if discharging and capacity <= threshold:
                    if tag not in self._battery_warned:
                        self._battery_warned.add(tag)
                        urgency = "critical" if level == "critical" else "normal"
                        spawn(["notify-send", "-a", APP_NAME, "-u", urgency, f"{pad.name}: battery {capacity}%"])
                    break
                self._battery_warned.discard(tag)
        if low_any != self.battery_low:
            self.battery_low = low_any
            self.refresh_lights()
        return True

    def _battery_level(self) -> tuple[int, bool] | None:
        """(percent, charging) of the emptiest controller, shown in an open radial's corner."""
        levels = []
        for pad in self.pads.values():
            capacity, status = pad.controller.battery_state()
            if capacity is not None:
                levels.append((capacity, status.lower() == "charging"))
        return min(levels, default=None)


def _voxtype_suffix() -> str:
    """VoxType's own `output.append_text`, typed after every phrase (counts toward erasing)."""
    if shutil.which("voxtype") is None:
        return ""
    try:
        result = subprocess.run(
            ["voxtype", "config", "get", "output.append_text"], capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    value = result.stdout.strip()
    return "" if result.returncode or value in ("", "null") else value.strip('"')


def run(cfg: Config) -> None:
    Gtk.init()
    App(cfg).run()
