"""Sound effects, synthesized here (or any audio file): a blaster "pew" when you send a prompt,
a power-up chirp for thinking levels, a beacon when an agent needs you.

Plays on the DualSense's own speaker when the controller is wired (the kernel routes the right
channel to the speaker over USB); Linux has no audio path to the controller over Bluetooth, so
`device = "auto"` falls back to the default output then.
"""

from __future__ import annotations

import array
import json
import logging
import math
import os
import shutil
import subprocess
import wave

from .actions import spawn

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
SONY_VENDOR = "0x054c"
STATE_FILE = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "ps-oneshot", "state.json"
)
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "ps-oneshot")


def blaster_samples(rate: int = SAMPLE_RATE, seconds: float = 0.34) -> array.array:
    """A sci-fi blaster shot: a bright, fast-falling pitch sweep with a metallic ring and a short tail."""
    samples = array.array("h")
    phase_a = phase_b = 0.0
    count = int(rate * seconds)
    for n in range(count):
        t = n / rate
        freq = 170 + 2900 * math.exp(-t / 0.055)          # the "pew": fast exponential pitch drop
        phase_a += 2 * math.pi * freq / rate
        phase_b += 2 * math.pi * freq * 1.51 / rate         # inharmonic partner → metallic ring
        tone = math.tanh(2.6 * math.sin(phase_a + 1.8 * math.sin(phase_b)))
        ring = 0.35 * math.sin(phase_b * 2.02)
        attack = min(1.0, t / 0.002)
        decay = math.exp(-t / 0.085) + 0.12 * math.exp(-t / 0.22)
        value = (tone + ring) * attack * decay
        samples.append(int(max(-1.0, min(1.0, value * 0.62)) * 32767))
    return samples


def charge_samples(rate: int = SAMPLE_RATE, seconds: float = 0.26) -> array.array:
    """Power-up chirp for a thinking-level change: a rising sweep with a bright top note."""
    samples = array.array("h")
    phase = 0.0
    count = int(rate * seconds)
    for n in range(count):
        t = n / rate
        progress = t / seconds
        freq = 320 * (2 ** (2.4 * progress))                # ~2.4 octaves up
        phase += 2 * math.pi * freq / rate
        tone = 0.7 * math.sin(phase) + 0.3 * math.tanh(3 * math.sin(phase * 2))
        flutter = 0.75 + 0.25 * math.sin(2 * math.pi * 38 * t)  # "charging" shimmer
        envelope = min(1.0, t / 0.01) * min(1.0, (seconds - t) / 0.03)
        samples.append(int(max(-1.0, min(1.0, tone * flutter * envelope * 0.55)) * 32767))
    return samples


def beacon_samples(rate: int = SAMPLE_RATE) -> array.array:
    """Incoming-transmission beacon for an agent that needs you: two rising two-tone chirps."""
    notes = ((880, 0.09), (0, 0.035), (1320, 0.11), (0, 0.12), (880, 0.09), (0, 0.035), (1320, 0.16))
    samples = array.array("h")
    for freq, seconds in notes:
        count = int(rate * seconds)
        for n in range(count):
            if not freq:
                samples.append(0)
                continue
            t = n / rate
            envelope = min(1.0, t / 0.006) * min(1.0, (seconds - t) / 0.02)
            tone = math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(2 * math.pi * freq * 2 * t)
            samples.append(int(tone * envelope * 0.45 * 32767))
    return samples


def write_wav(path: str, mono: array.array, rate: int = SAMPLE_RATE) -> None:
    """Stereo file with both channels equal (the controller speaker plays the right channel)."""
    stereo = array.array("h")
    for sample in mono:
        stereo.append(sample)
        stereo.append(sample)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(stereo.tobytes())


BUILTINS = {"blaster": blaster_samples, "charge": charge_samples, "beacon": beacon_samples}


def sound_path(spec: str) -> str | None:
    """`builtin:<name>` (generated once into the cache) or a path to any file pw-play can read."""
    if spec.startswith("builtin:"):
        name = spec.removeprefix("builtin:")
        if name not in BUILTINS:
            log.warning("unknown built-in sound %r (have: %s)", name, ", ".join(BUILTINS))
            return None
        path = os.path.join(CACHE_DIR, f"{name}.wav")
        if not os.path.exists(path):
            write_wav(path, BUILTINS[name]())
        return path
    path = os.path.expanduser(spec)
    return path if os.path.isfile(path) else None


def controller_sink() -> str | None:
    """PipeWire sink of a wired Sony controller, if any."""
    try:
        result = subprocess.run(["pactl", "-f", "json", "list", "sinks"], capture_output=True, text=True, timeout=3)
        sinks = json.loads(result.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    for sink in sinks:
        properties = sink.get("properties", {})
        if str(properties.get("device.vendor.id", "")).lower() == SONY_VENDOR or "DualSense" in str(sink.get("name")):
            return str(sink.get("name"))
    return None


def load_state(key: str, default):
    """Small runtime choices remembered across restarts (sound toggle, mouse speed)."""
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            return json.load(handle).get(key, default)
    except (OSError, ValueError):
        return default


def save_state(key: str, value) -> None:
    state: dict = {}
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        pass
    state[key] = value
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    except OSError as error:  # not remembered, but never a reason to drop the controller
        log.warning("cannot save %s to %s: %s", key, STATE_FILE, error)


def play(spec: str, cfg: dict) -> None:
    if not spec:
        return
    path = sound_path(spec)
    player = shutil.which("pw-play")
    if path is None or player is None:
        log.info("sound %r not played (file or pw-play missing)", spec)
        return
    command = [player, "--volume", str(float(cfg["volume"]))]
    device = cfg["device"]
    if device in ("auto", "controller"):
        sink = controller_sink()
        if sink is not None:
            command += ["--target", sink]
        elif device == "controller":
            return  # controller speaker only, and it isn't wired
    spawn([*command, path])
