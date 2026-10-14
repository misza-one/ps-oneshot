"""Opus encoding through the system libopus (ctypes): constant-bitrate frames, which is what the DualSense's
speaker takes over Bluetooth. Optional: without libopus there's no Bluetooth speaker and nothing else changes."""

from __future__ import annotations

import ctypes
import ctypes.util
from array import array

APPLICATION_AUDIO = 2049
SET_BITRATE = 4002
SET_VBR = 4006

_lib: ctypes.CDLL | None = None


def _library() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(ctypes.util.find_library("opus") or "libopus.so.0")
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_encode.restype = ctypes.c_int32
        lib.opus_encode.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
        ]
        lib.opus_encoder_destroy.restype = None
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        _lib = lib
    return _lib


def available() -> bool:
    try:
        _library()
    except OSError:
        return False
    return True


def encode_cbr(pcm: array, channels: int, frame_samples: int, frame_bytes: int, rate: int = 48_000) -> list[bytes]:
    """Interleaved 16-bit `pcm` → Opus frames of `frame_samples` per channel, each exactly `frame_bytes` long
    (hard constant bitrate). The last frame is padded with silence. Raises OSError without libopus."""
    lib = _library()
    error = ctypes.c_int()
    encoder = lib.opus_encoder_create(rate, channels, APPLICATION_AUDIO, ctypes.byref(error))
    if not encoder or error.value:
        raise OSError(f"opus_encoder_create failed ({error.value})")
    try:
        handle = ctypes.c_void_p(encoder)  # opus_encoder_ctl is variadic: no argtypes, so spell out the C types
        lib.opus_encoder_ctl(handle, ctypes.c_int(SET_VBR), ctypes.c_int32(0))
        lib.opus_encoder_ctl(handle, ctypes.c_int(SET_BITRATE), ctypes.c_int32(frame_bytes * 8 * rate // frame_samples))
        step = frame_samples * channels
        out = (ctypes.c_ubyte * frame_bytes)()
        frames = []
        for start in range(0, len(pcm), step):
            chunk = array("h", pcm[start:start + step])
            chunk.extend([0] * (step - len(chunk)))
            size = lib.opus_encode(encoder, (ctypes.c_int16 * step).from_buffer(chunk), frame_samples, out, frame_bytes)
            if size < 0:
                raise OSError(f"opus_encode failed ({size})")
            frames.append(bytes(out[:size]).ljust(frame_bytes, b"\0"))
        return frames
    finally:
        lib.opus_encoder_destroy(encoder)
