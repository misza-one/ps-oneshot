"""Remember dictated text per window so it can be erased again with exact Backspace counts.

The dictation tool reports each phrase by writing it to `capture_file` (VoxType:
`[output.post_process] command = 'tee "$XDG_RUNTIME_DIR/ps-oneshot/dictation"'`). Erasing is
best-effort: if you moved the cursor or typed in between, the count no longer matches.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from gi.repository import Gio

log = logging.getLogger(__name__)

MAX_PER_WINDOW = 50


class DictationLog:
    """Phrases dictated into each window since its last Enter, oldest first."""

    def __init__(self, suffix: str = "") -> None:
        self.suffix = suffix  # appended by the dictation tool itself (VoxType output.append_text)
        self._windows: dict[str, list[str]] = {}

    def add(self, window: str, text: str) -> None:
        phrases = self._windows.setdefault(window, [])
        phrases.append(text + self.suffix)
        del phrases[:-MAX_PER_WINDOW]

    def clear(self, window: str) -> None:
        """Text was submitted (Enter) or the window closed: nothing left to erase."""
        self._windows.pop(window, None)

    def pop_last(self, window: str) -> int | None:
        """Characters of the newest phrase, or None if nothing is left in this window."""
        phrases = self._windows.get(window)
        return len(phrases.pop()) if phrases else None

    def pop_all(self, window: str) -> int | None:
        phrases = self._windows.pop(window, None)
        return sum(len(p) for p in phrases) if phrases else None


def capture_path(template: str) -> str:
    return os.path.expandvars(os.path.expanduser(template))


class CaptureWatcher:
    """Calls `on_text` whenever the capture file is (re)written."""

    def __init__(self, path: str, on_text: Callable[[str], None]) -> None:
        self.path = path
        self._on_text = on_text
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._monitor = Gio.File.new_for_path(path).monitor_file(Gio.FileMonitorFlags.NONE, None)
        self._monitor.connect("changed", self._changed)

    def _changed(self, _monitor: Gio.FileMonitor, _file: Gio.File, _other: Gio.File | None, event: Gio.FileMonitorEvent) -> None:
        if event != Gio.FileMonitorEvent.CHANGES_DONE_HINT:
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as error:
            log.warning("cannot read dictation capture: %s", error)
            return
        text = text.rstrip("\n")
        if text:
            self._on_text(text)

    def close(self) -> None:
        self._monitor.cancel()
