"""Keyboard-layout awareness for `key:` actions, through libxkbcommon (ctypes).

A virtual key press is a physical key: KEY_SLASH types "/" on a US layout and "-" on a German one. A
`Layout` compiles the keymap Hyprland gives a keyboard and finds, for a keysym, the physical key and the
modifiers that type it there (`/` on a German layout: Shift+7; `@`: AltGr+Q).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

KEYCODE_OFFSET = 8  # an xkb keycode is the evdev code + 8
NO_SYMBOL = 0
_INVALID = 0xFFFFFFFF
_CASE_INSENSITIVE = 1
_MAX_MASKS = 8
# xkb modifier names → the modifier a `KeyPosition` asks for. Others (Lock, NumLock, LevelFive) can't be
# pressed for a symbol, so levels that need them are skipped.
_MODIFIERS = {
    "Shift": "shift", "Control": "ctrl", "Mod1": "alt", "Alt": "alt", "Mod4": "super", "Super": "super",
    "Mod5": "altgr", "LevelThree": "altgr",
}


class LayoutError(RuntimeError):
    pass


class _RuleNames(ctypes.Structure):
    _fields_ = [(name, ctypes.c_char_p) for name in ("rules", "model", "layout", "variant", "options")]


def _load() -> ctypes.CDLL | None:
    path = ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0"
    try:
        lib = ctypes.CDLL(path)
    except OSError as error:
        log.warning("libxkbcommon unavailable, key: actions use US-layout positions: %s", error)
        return None
    u32, ptr = ctypes.c_uint32, ctypes.c_void_p
    signatures = {
        "xkb_context_new": (ptr, [ctypes.c_int]),
        "xkb_context_unref": (None, [ptr]),
        "xkb_keymap_new_from_names": (ptr, [ptr, ctypes.POINTER(_RuleNames), ctypes.c_int]),
        "xkb_keymap_unref": (None, [ptr]),
        "xkb_keymap_min_keycode": (u32, [ptr]),
        "xkb_keymap_max_keycode": (u32, [ptr]),
        "xkb_keymap_num_mods": (u32, [ptr]),
        "xkb_keymap_mod_get_name": (ctypes.c_char_p, [ptr, u32]),
        "xkb_keymap_num_layouts_for_key": (u32, [ptr, u32]),
        "xkb_keymap_num_levels_for_key": (u32, [ptr, u32, u32]),
        "xkb_keymap_key_get_syms_by_level": (ctypes.c_int, [ptr, u32, u32, u32, ctypes.POINTER(ctypes.POINTER(u32))]),
        "xkb_keymap_key_get_mods_for_level": (ctypes.c_size_t, [ptr, u32, u32, u32, ctypes.POINTER(u32), ctypes.c_size_t]),
        "xkb_keysym_from_name": (u32, [ctypes.c_char_p, ctypes.c_int]),
    }
    try:
        for name, (restype, argtypes) in signatures.items():
            function = getattr(lib, name)
            function.restype, function.argtypes = restype, argtypes
    except AttributeError as error:  # key_get_mods_for_level needs libxkbcommon >= 1.0
        log.warning("libxkbcommon too old, key: actions use US-layout positions: %s", error)
        return None
    return lib


_lib: ctypes.CDLL | None = None
_loaded = False


def _library() -> ctypes.CDLL | None:
    global _lib, _loaded
    if not _loaded:
        _lib, _loaded = _load(), True
    return _lib


def available() -> bool:
    return _library() is not None


def keysym(name: str) -> int:
    """The keysym called `name` ("slash", "at", "Udiaeresis", "a"), or NO_SYMBOL."""
    lib = _library()
    if lib is None or not name:
        return NO_SYMBOL
    encoded = name.encode()
    return lib.xkb_keysym_from_name(encoded, 0) or lib.xkb_keysym_from_name(encoded, _CASE_INSENSITIVE)


@dataclass(frozen=True)
class KeyPosition:
    code: int  # evdev key code
    mods: tuple[str, ...]  # modifiers to hold: "shift", "altgr", "ctrl", "alt", "super"


class Layout:
    """One layout (`index` in a multi-layout keymap) of the keymap built from Hyprland's RMLVO names."""

    def __init__(
        self, layout: str, variant: str = "", options: str = "", model: str = "", rules: str = "", index: int = 0
    ) -> None:
        lib = _library()
        if lib is None:
            raise LayoutError("libxkbcommon unavailable")
        context = lib.xkb_context_new(0)
        if not context:
            raise LayoutError("cannot create an xkb context")
        names = _RuleNames(*(value.encode() if value else None for value in (rules, model, layout, variant, options)))
        keymap = lib.xkb_keymap_new_from_names(context, ctypes.byref(names), 0)
        try:
            if not keymap:
                raise LayoutError(f"cannot compile the keymap for layout {layout!r} variant {variant!r}")
            self._positions = _positions(lib, keymap, index)
        finally:
            if keymap:
                lib.xkb_keymap_unref(keymap)
            lib.xkb_context_unref(context)

    def locate(self, sym: int) -> KeyPosition | None:
        """The key and modifiers that type `sym`, needing as few modifiers as possible; None if none does."""
        return self._positions.get(sym)


def _positions(lib: ctypes.CDLL, keymap: int, index: int) -> dict[int, KeyPosition]:
    labels = {}
    for mod in range(lib.xkb_keymap_num_mods(keymap)):
        name = lib.xkb_keymap_mod_get_name(keymap, mod)
        labels[mod] = _MODIFIERS.get(name.decode() if name else "")
    best: dict[int, tuple[tuple[int, int, int], KeyPosition]] = {}
    masks = (ctypes.c_uint32 * _MAX_MASKS)()
    syms = ctypes.POINTER(ctypes.c_uint32)()
    for key in range(lib.xkb_keymap_min_keycode(keymap), lib.xkb_keymap_max_keycode(keymap) + 1):
        if key < KEYCODE_OFFSET:
            continue
        layouts = lib.xkb_keymap_num_layouts_for_key(keymap, key)
        if not layouts:
            continue
        group = index % layouts  # xkb wraps an out-of-range layout
        for level in range(lib.xkb_keymap_num_levels_for_key(keymap, key, group)):
            mods = _pressable(masks, lib.xkb_keymap_key_get_mods_for_level(
                keymap, key, group, level, masks, _MAX_MASKS
            ), labels)
            if mods is None:
                continue
            count = lib.xkb_keymap_key_get_syms_by_level(keymap, key, group, level, ctypes.byref(syms))
            for i in range(max(0, count)):
                cost = (len(mods), level, key)
                sym = syms[i]
                if sym != NO_SYMBOL and (sym not in best or cost < best[sym][0]):
                    best[sym] = (cost, KeyPosition(key - KEYCODE_OFFSET, mods))
    return {sym: position for sym, (_cost, position) in best.items()}


def _pressable(masks: ctypes.Array, count: int, labels: dict[int, str | None]) -> tuple[str, ...] | None:
    """The modifiers of the smallest mask made only of modifiers we can press; None if there's none."""
    options = []
    for mask in masks[: min(count, _MAX_MASKS)]:
        bits = [bit for bit in range(32) if mask & (1 << bit)]
        mods = [labels.get(bit) for bit in bits]
        if all(mods):
            options.append(tuple(sorted(set(mods))))  # type: ignore[arg-type]
    return min(options, key=len) if options else None
