"""Live audio/dictation state: devices and mic mute (PipeWire via pactl), VoxType dictation language."""

from __future__ import annotations

import json
import logging
import re
import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import GLib

from . import APP_NAME

log = logging.getLogger(__name__)

RGB = tuple[int, int, int]
OUTPUT_COLOR: RGB = (120, 190, 255)
INPUT_COLOR: RGB = (255, 170, 100)
ICON_SPEAKER, ICON_HEADPHONES, ICON_MIC = "\uf028", "\uf025", "\uf130"
LABEL_NOISE = re.compile(r"\s+(Analog|Digital) (Stereo|Mono|Surround[\w.]*)( \(IEC958\))?$|\s+(Mono|Stereo)$")
MAX_SLICES = 12


@dataclass(frozen=True)
class Choice:
    label: str
    action: str
    icon: str = ""
    sublabel: str = ""
    group: str = ""
    group_color: RGB | None = None
    active: bool = False
    keep_open: bool = False  # radial stays open after picking it (toggles, steppers)


def _pactl_json(*args: str) -> object:
    result = subprocess.run(["pactl", "-f", "json", *args], capture_output=True, text=True, timeout=3)
    return json.loads(result.stdout or "null")


def _device_label(entry: dict) -> str:
    properties = entry.get("properties", {})
    text = str(entry.get("description") or properties.get("device.description") or entry.get("name") or "?")
    return LABEL_NOISE.sub("", text).removesuffix(" Sink").removesuffix(" Source") or text


def _available(entry: dict) -> bool:
    ports = entry.get("ports") or []
    return not ports or any(port.get("availability") != "not available" for port in ports)


def audio_devices(sinks: list[dict], sources: list[dict], default_sink: str, default_source: str) -> list[dict]:
    """Selectable outputs then inputs (monitors and unplugged ports dropped), each with role/active flags."""
    outputs = [
        {**s, "role": "output", "active": s.get("name") == default_sink} for s in sinks if _available(s)
    ]
    inputs = [
        {**s, "role": "input", "active": s.get("name") == default_source}
        for s in sources
        if _available(s) and s.get("properties", {}).get("device.class") != "monitor"
    ]
    half = MAX_SLICES // 2
    outputs_cap = max(half, MAX_SLICES - len(inputs))
    inputs_cap = MAX_SLICES - min(len(outputs), outputs_cap)
    return _keep_active(outputs, outputs_cap) + _keep_active(inputs, inputs_cap)


def _keep_active(devices: list[dict], cap: int) -> list[dict]:
    if len(devices) <= cap:
        return devices
    kept = devices[:cap]
    active = next((d for d in devices if d["active"]), None)
    if active is not None and active not in kept:
        kept[-1] = active
    return kept


def audio_choices(cfg: dict) -> list[Choice]:
    if shutil.which("pactl") is None:
        return []
    try:
        info = _pactl_json("info")
        sinks = _pactl_json("list", "sinks")
        sources = _pactl_json("list", "sources")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        log.warning("audio devices unavailable: %s", error)
        return []
    if not isinstance(info, dict) or not isinstance(sinks, list) or not isinstance(sources, list):
        return []
    devices = audio_devices(sinks, sources, str(info.get("default_sink_name")), str(info.get("default_source_name")))
    choices = []
    for device in devices:
        output = device["role"] == "output"
        properties = device.get("properties", {})
        node_id = str(properties.get("object.id") or device.get("index"))
        template = cfg["set_output" if output else "set_input"]
        command = template.format(id=shlex.quote(node_id), name=shlex.quote(str(device.get("name"))))
        headset = "headphone" in str(properties.get("device.icon_name", "")) or properties.get("device.form_factor") in (
            "headset", "headphone",
        )
        choices.append(Choice(
            label=_device_label(device),
            action=f"exec:{command}",
            icon=(ICON_HEADPHONES if headset else ICON_SPEAKER) if output else ICON_MIC,
            group="Output" if output else "Input",
            group_color=OUTPUT_COLOR if output else INPUT_COLOR,
            active=bool(device["active"]),
        ))
    return choices


def voxtype_current(cfg: dict) -> str | None:
    try:
        result = subprocess.run(cfg["get_language"], shell=True, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip().strip('"') if result.returncode == 0 else None


def read_number(command: str) -> float | None:
    """The first number a shell command prints, e.g. the current text size or brightness (None if none)."""
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\d+(?:\.\d+)?", result.stdout) if result.returncode == 0 else None
    return float(match.group()) if match else None


class LevelControl:
    """A 0–100 level behind slow commands (DDC/CI brightness takes ~0.15 s per call). Only one `set`
    runs at a time; steps in between just move the target, so holding a button glides to it instead
    of piling up commands."""

    STALE_S = 5.0  # re-read the level after this long without a step (it may have changed elsewhere)

    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        self._level: float | None = None
        self._seen = 0.0
        self._target: int | None = None
        self._sent: int | None = None
        self._busy = False

    def step(self, direction: int) -> int | None:
        """Move the target one `step` up (1) or down (-1); None when the level can't be read."""
        now = time.monotonic()
        if not self._busy and self._target is None and (self._level is None or now - self._seen > self.STALE_S):
            self._level = read_number(self._cfg["get"])
        self._seen = now
        if self._level is None:
            return None
        base = self._target if self._target is not None else self._level
        self._target = int(max(0, min(100, round(base + direction * float(self._cfg["step"])))))
        if not self._busy:
            self._send()
        return self._target

    def _send(self) -> None:
        self._sent = self._target
        try:
            process = subprocess.Popen(
                ["sh", "-c", self._cfg["set"].format(value=self._sent)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError as error:
            log.warning("cannot set level: %s", error)
            self._target = None
            return
        self._busy = True
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, process.pid, self._done, process)

    def _done(self, _pid: int, _status: int, process: subprocess.Popen) -> None:
        process.wait()
        self._busy = False
        self._level, self._seen = float(self._sent or 0), time.monotonic()
        if self._target is not None and self._target != self._sent:
            self._send()  # the button moved on while that one ran
        else:
            self._target = None


def next_stop(stops: list[float], current: float, direction: int) -> float | None:
    """The next of the ascending `stops` above (direction 1) or below (-1) `current`, which may sit
    between stops; None at the end of the range."""
    if direction > 0:
        return next((stop for stop in stops if stop > current + 1e-6), None)
    return next((stop for stop in reversed(stops) if stop < current - 1e-6), None)


def voxtype_choices(cfg: dict) -> list[Choice] | None:
    """None when VoxType isn't installed."""
    if shutil.which("voxtype") is None:
        return None
    current = voxtype_current(cfg)
    choices = []
    for language in cfg["languages"]:
        code, label = str(language["code"]), str(language["label"])
        notify = shlex.join(["notify-send", "-a", APP_NAME, "-t", "1500", f"Dictation: {label}"])
        choices.append(Choice(
            label=label,
            sublabel=code.upper(),
            icon="\uf130",
            action=f"exec:{cfg['set_language'].format(code=shlex.quote(code))} && {notify}",
            active=code == current,
        ))
    return choices


def mic_muted() -> bool | None:
    try:
        result = subprocess.run(
            ["pactl", "get-source-mute", "@DEFAULT_SOURCE@"], capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return result.stdout.strip().lower().endswith("yes")


class MicMuteWatcher:
    """Follows the default source's mute state via `pactl subscribe`; calls back only on changes."""

    DEBOUNCE_MS = 60
    RESTART_S = 5

    def __init__(self, on_change: Callable[[bool], None]) -> None:
        self._on_change = on_change
        self._process: subprocess.Popen[bytes] | None = None
        self._watch = 0
        self._pending = 0
        self._buffer = b""
        self.muted: bool | None = None

    def start(self) -> None:
        if shutil.which("pactl") is None:
            log.info("pactl not found; mic LED won't follow the mute state")
            return
        self._process = subprocess.Popen(
            ["pactl", "subscribe"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        assert self._process.stdout is not None
        self._watch = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT, self._process.stdout.fileno(),
            GLib.IOCondition.IN | GLib.IOCondition.HUP, self._readable,
        )
        self._check()

    def stop(self) -> None:
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0
        if self._process is not None:
            self._process.terminate()
            self._process.wait()
            self._process = None

    def _readable(self, _fd: int, _condition: GLib.IOCondition) -> bool:
        assert self._process is not None and self._process.stdout is not None
        data = os.read(self._process.stdout.fileno(), 4096)
        if not data:
            self._watch = 0
            self.stop()
            GLib.timeout_add_seconds(self.RESTART_S, lambda: (self.start(), False)[1])
            return False
        self._buffer += data
        *lines, self._buffer = self._buffer.split(b"\n")
        if any(b" source " in line or b" server" in line for line in lines) and not self._pending:
            self._pending = GLib.timeout_add(self.DEBOUNCE_MS, self._check)
        return True

    def _check(self) -> bool:
        self._pending = 0
        muted = mic_muted()
        if muted is not None and muted != self.muted:
            self.muted = muted
            self._on_change(muted)
        return False
