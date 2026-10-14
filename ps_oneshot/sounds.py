"""Sound effects, synthesized here (or any audio file): a blaster "pew" when you send a prompt,
a power-up chirp for thinking levels, a beacon when an agent needs you.

They play on the DualSense's own speaker. Wired, the kernel makes it a sound card (the right channel goes to
the speaker) and pw-play targets it. Over Bluetooth Linux has no audio device for it, so `BluetoothSpeaker`
sends the sound to the controller itself, Opus-encoded in HID output reports. Otherwise, or with
`device = "system"`, they play on the default output.
"""

from __future__ import annotations

import array
import json
import logging
import math
import os
import shutil
import subprocess
import threading
import time
import wave

from .actions import spawn
from .linux import dualsense, opus

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


SPEAKER_VOLUME = 0x64  # the controller speaker's own level; [sounds] volume scales the samples
LEAD_IN_FRAMES = 3  # a little silence first, so the speaker amp is awake when the sound starts


def bluetooth_samples(spec: str) -> array.array | None:
    """Mono 16-bit samples at the rate the controller plays over Bluetooth: a built-in sound, or a 16-bit WAV
    file (mixed down, resampled). None for anything else."""
    if spec.startswith("builtin:"):
        make = BUILTINS.get(spec.removeprefix("builtin:"))
        return make(rate=dualsense.BT_AUDIO_RATE) if make else None
    try:
        with wave.open(os.path.expanduser(spec), "rb") as source:
            if source.getsampwidth() != 2:
                return None
            channels, rate = source.getnchannels(), source.getframerate()
            data = array.array("h", source.readframes(source.getnframes()))
    except (OSError, EOFError, wave.Error):
        return None
    mono = array.array("h", (sum(data[i:i + channels]) // channels for i in range(0, len(data), channels)))
    return resample(mono, rate, dualsense.BT_AUDIO_RATE)


def resample(samples: array.array, source_rate: int, target_rate: int) -> array.array:
    """Linear interpolation; plenty for short effects."""
    if source_rate == target_rate or not samples:
        return samples
    step = source_rate / target_rate
    out = array.array("h")
    last = len(samples) - 1
    for index in range(int(len(samples) / step)):
        position = index * step
        left = int(position)
        right = min(left + 1, last)
        out.append(int(samples[left] + (samples[right] - samples[left]) * (position - left)))
    return out


class BluetoothSpeaker:
    """Sound effects on a DualSense's built-in speaker over Bluetooth: Opus frames in HID output reports
    (0x36), paced to the controller's audio clock. The reports set only the speaker's own fields plus the
    rumble-mode bit (without it the controller stops a running rumble), so adaptive triggers, rumble and
    lights carry on untouched. Encoded sounds are kept, so each is encoded once."""

    def __init__(self) -> None:
        self._encoded: dict[tuple[str, float], list[bytes] | None] = {}
        self._stop = threading.Event()

    def play(self, hidraw: str, spec: str, volume: float) -> bool:
        """Start the sound; False when it can't go this way (no libopus, not a built-in or WAV sound)."""
        key = (spec, round(volume, 3))
        if key not in self._encoded:
            self._encoded[key] = self._encode(spec, volume)
        frames = self._encoded[key]
        if frames is None:
            return False
        self._stop.set()  # a newer sound cuts off the one still playing
        self._stop = threading.Event()
        threading.Thread(target=self._stream, args=(hidraw, frames, self._stop), daemon=True).start()
        return True

    @staticmethod
    def _encode(spec: str, volume: float) -> list[bytes] | None:
        samples = bluetooth_samples(spec)
        if samples is None or not opus.available():
            log.info("%s can't play on the controller over Bluetooth (needs libopus and a built-in or WAV sound)", spec)
            return None
        gain = max(0.0, min(volume, 1.0))
        stereo = array.array("h", [0] * (LEAD_IN_FRAMES * dualsense.BT_AUDIO_FRAME_SAMPLES * 2))
        for sample in samples:
            value = int(sample * gain)
            stereo.append(value)
            stereo.append(value)
        try:
            return opus.encode_cbr(stereo, 2, dualsense.BT_AUDIO_FRAME_SAMPLES, dualsense.BT_AUDIO_FRAME_BYTES)
        except OSError as error:
            log.warning("cannot encode %s for the controller speaker: %s", spec, error)
            return None

    @staticmethod
    def _stream(hidraw: str, frames: list[bytes], stop: threading.Event) -> None:
        try:
            fd = os.open(hidraw, os.O_WRONLY | os.O_CLOEXEC)
        except OSError as error:
            log.warning("controller speaker: cannot open %s: %s", hidraw, error)
            return
        try:
            os.write(fd, dualsense.build_output_report(True, 0, None, None, speaker=SPEAKER_VOLUME, keep_rumble=True))
            start = time.monotonic()
            for index, frame in enumerate(frames):
                if stop.is_set():
                    break
                delay = start + index * dualsense.BT_AUDIO_FRAME_SECONDS - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                os.write(fd, dualsense.build_audio_report(index, index, frame))
        except OSError as error:
            log.debug("controller speaker write failed: %s", error)
        finally:
            os.close(fd)


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
