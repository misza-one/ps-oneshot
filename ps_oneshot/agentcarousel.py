"""Agent carousel session: Omaherd's agents in the carousel overlay, opened on the most urgent one.

Cards keep the R3 ring's order (by machine) and follow Omaherd live while open; waiting agents' dots blink.
Pad: D-pad or either stick ←/→ moves (a held stick repeats), L1/R1 jump to the previous/next waiting agent,
✕ goes to the agent as the ring does, ○ (or L3/R3) closes. Keyboard (the overlay holds it while open):
←/→, Tab, Home/End, Enter, Escape; typing filters by project, task, tab, agent kind or machine, Backspace
edits the filter and Escape clears it before it closes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

import cairo
from gi.repository import GLib

from . import lights as fx
from .agentcards import (
    CARD_DOT, CARD_H, CARD_W, card_order, dot_style, matches, most_urgent, paint_card, paint_strip, waited,
)
from .config import Config
from .herd import Agent, HerdProvider
from .menus import KEYBOARD_HANDBACK_MS
from .ui.carousel import EXPANDED_H, EXPANDED_W, SKEW, SLICE_H, SLICE_W, Carousel, CarouselItem

STICK_ON = 0.6  # a stick past this steps the selection...
STICK_OFF = 0.4  # ...and re-arms once it's back under this
REPEAT_FIRST_MS = 380
REPEAT_MS = 110
KEYS_BACK = {"Left", "Up", "ISO_Left_Tab", "KP_Left", "KP_Up"}
KEYS_NEXT = {"Right", "Down", "Tab", "KP_Right", "KP_Down"}
KEYS_PICK = {"Return", "KP_Enter"}


@dataclass
class CarouselHooks:
    rumble: Callable[[tuple], None]
    monitor: Callable[[], str | None]
    capture_sticks: Callable[[], None]  # the carousel opened or closed: re-derive who owns the sticks
    notify: Callable[[str], None]


class AgentCarousel:
    def __init__(self, cfg: Config, ui: Carousel, herd: HerdProvider | None, hooks: CarouselHooks) -> None:
        self.cfg = cfg
        self.ui = ui
        self.herd = herd
        self.hooks = hooks
        self._open = False
        self._agents: list[Agent] = []  # every agent, in card order
        self._shown: list[Agent] = []  # those matching the filter
        self._selected = ""  # the selected agent's key
        self._filter = ""
        self._stick_dir = 0
        self._repeat_id = 0
        ui.on_key = self._on_key

    @property
    def is_open(self) -> bool:
        return self._open

    # session ---------------------------------------------------------------------------------------------

    def open(self) -> None:
        if self.herd is None:
            self.hooks.notify("Agents need Omaherd")
            return
        agents = card_order(self.herd.agents)
        if not agents:
            self.hooks.notify("No agents running")
            return
        self._agents, self._filter = agents, ""
        self._selected = most_urgent(agents).key
        self.ui.reload_theme()  # the dots take their colours from it
        self._open = True
        self._refilter()
        self.ui.show(*self._view(), self.hooks.monitor(), **self._texts())
        self.hooks.capture_sticks()

    def refresh(self, agents: list[Agent]) -> None:
        """Omaherd polled: redraw what changed, keeping the selected agent selected."""
        if not self._open:
            return
        self._agents = card_order(agents)
        if not self._agents:
            self.close()
            return
        self._refilter()
        self.ui.update(*self._view(), **self._texts())

    def close(self) -> None:
        self._stop_repeat()
        self._stick_dir = 0
        if not self._open:
            return
        self._open = False
        self.ui.hide()
        self.hooks.capture_sticks()

    # input -----------------------------------------------------------------------------------------------

    def on_button(self, button: str) -> bool:
        """A pad press while open; False if it isn't a carousel control."""
        if button in ("dpad_left", "dpad_right"):
            self._step(-1 if button == "dpad_left" else 1, feedback=True)
        elif button in ("l1", "r1"):
            self._jump_waiting(-1 if button == "l1" else 1)
        elif button == "cross":
            self._pick(feedback=True)
        elif button in ("circle", "l3", "r3"):
            self.close()
        else:
            return False
        return True

    def on_stick(self, x: float, y: float) -> None:
        direction = self._stick_dir
        if abs(x) >= STICK_ON and abs(x) > abs(y):
            direction = 1 if x > 0 else -1
        elif abs(x) < STICK_OFF:
            direction = 0
        if direction == self._stick_dir:
            return
        self._stick_dir = direction
        self._stop_repeat()
        if direction:
            self._step(direction, feedback=True)
            self._repeat_id = GLib.timeout_add(REPEAT_FIRST_MS, self._repeat, True)

    def _repeat(self, first: bool) -> bool:
        if not self._open or not self._stick_dir:
            self._repeat_id = 0
            return False
        self._step(self._stick_dir, feedback=True)
        if first:
            self._repeat_id = GLib.timeout_add(REPEAT_MS, self._repeat, False)
            return False
        return True

    def _stop_repeat(self) -> None:
        if self._repeat_id:
            GLib.source_remove(self._repeat_id)
            self._repeat_id = 0

    def _on_key(self, name: str, char: str, down: bool, repeat: bool) -> None:
        if not self._open or not down:
            return
        if name in KEYS_BACK or name in KEYS_NEXT:
            self._step(-1 if name in KEYS_BACK else 1)
        elif name in ("Home", "End", "KP_Home", "KP_End"):
            self._select(0 if "Home" in name else len(self._shown) - 1)
        elif name == "BackSpace":
            if self._filter:
                self._set_filter(self._filter[:-1])
        elif repeat:
            return  # a held key repeats moves and deletes, nothing else
        elif name in KEYS_PICK:
            self._pick()
        elif name == "Escape":
            if self._filter:
                self._set_filter("")
            else:
                self.close()
        elif char:
            self._set_filter(self._filter + char)

    # selection -------------------------------------------------------------------------------------------

    def _index(self) -> int:
        keys = [a.key for a in self._shown]
        return keys.index(self._selected) if self._selected in keys else 0

    def _step(self, step: int, feedback: bool = False) -> None:
        if len(self._shown) < 2:
            if feedback:
                self.hooks.rumble(fx.RUMBLE_NOTHING)
            return
        self._select((self._index() + step) % len(self._shown), feedback)

    def _select(self, index: int, feedback: bool = False) -> None:
        if not self._shown:
            return
        agent = self._shown[max(0, min(index, len(self._shown) - 1))]
        if agent.key == self._selected:
            return
        self._selected = agent.key
        self.ui.update(*self._view(), **self._texts())
        if feedback and self.cfg.get("rumble", "feedback"):
            self.hooks.rumble(fx.RUMBLE_TICK)

    def _jump_waiting(self, step: int) -> None:
        """L1/R1: the previous/next waiting agent, wrapping around."""
        count, start = len(self._shown), self._index()
        for offset in range(1, count + 1):
            index = (start + step * offset) % count
            if self._shown[index].waiting and index != start:
                self._select(index, feedback=True)
                return
        self.hooks.rumble(fx.RUMBLE_NOTHING)

    def _pick(self, feedback: bool = False) -> None:
        if not self._shown or self.herd is None:
            return
        agent = self._shown[self._index()]
        if feedback:
            self.hooks.rumble(fx.RUMBLE_CONFIRM)
        self.close()
        # The overlay held the keyboard: give the compositor time to hand it back before focusing.
        GLib.timeout_add(KEYBOARD_HANDBACK_MS, _once(partial(self.herd.focus, agent)))

    def _set_filter(self, text: str) -> None:
        self._filter = text
        self._refilter()
        self.ui.update(*self._view(), **self._texts())

    def _refilter(self) -> None:
        self._shown = [a for a in self._agents if matches(a, self._filter)] if self._filter else list(self._agents)
        if self._shown and self._selected not in {a.key for a in self._shown}:
            self._selected = (most_urgent(self._shown) if not self._filter else self._shown[0]).key

    # items -----------------------------------------------------------------------------------------------

    def _texts(self) -> dict[str, str]:
        return {"filter_text": self._filter, "empty_text": "No matches" if self._filter and not self._shown else ""}

    def _view(self) -> tuple[list[CarouselItem], int]:
        lights = self.cfg.section("lights")
        now = time.time()
        theme = self.ui.theme
        index = self._index()
        items = []
        for i, agent in enumerate(self._shown):
            rgba, radius = dot_style(agent, theme, lights)
            # Only the selected card shows how long; strips don't, so they aren't redrawn every poll.
            since = waited(agent.since, now) if i == index else ""
            items.append(CarouselItem(
                key="\0".join((agent.key, agent.status, agent.project, agent.title, agent.tab, agent.kind,
                               agent.machine, since)),
                paint_card=partial(self._paint_card, agent, lights, now),
                paint_strip=partial(self._paint_strip, agent),
                card_dot=(CARD_DOT[0] * EXPANDED_W / CARD_W, CARD_DOT[1] * EXPANDED_H / CARD_H),
                dot=rgba,
                dot_radius=radius,
                blink=agent.waiting,
            ))
        return items, index

    def _paint_card(self, agent: Agent, lights: dict, now: float, cr: cairo.Context) -> None:
        cr.scale(EXPANDED_W / CARD_W, EXPANDED_H / CARD_H)
        paint_card(cr, agent, self.ui.theme, lights, now, dot=False)

    def _paint_strip(self, agent: Agent, cr: cairo.Context) -> None:
        paint_strip(cr, agent, self.ui.theme, SLICE_W, SLICE_H, SKEW)


def _once(function: Callable[[], object]) -> Callable[[], bool]:
    def run() -> bool:
        function()
        return False

    return run
