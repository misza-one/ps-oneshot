"""Radial menu sessions: static item trees plus live sources (agents, audio devices, dictation language, favorites)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial

from gi.repository import GLib

from . import lights as fx
from .apps import DesktopApp
from .config import Config, MenuItem
from .favorites import Favorites
from .herd import BADGES, STATUS_GREYS, STATUS_ICONS, Agent, HerdProvider, by_machine
from .sources import Choice, audio_choices, voxtype_choices
from .ui.radial import RadialItem, RadialMenu, slice_for_vector

log = logging.getLogger(__name__)

RGB = tuple[int, int, int]
MAX_SLICES = 12
LOCAL_COLOR: RGB = (96, 165, 250)
REMOTE_COLORS: tuple[RGB, ...] = ((251, 146, 60), (167, 139, 250), (52, 211, 153), (244, 114, 182))
INLINE_COLOR: RGB = (130, 251, 156)
ICON_LOCAL, ICON_REMOTE = "\uf109", "\uf233"
ICON_ADD = "\uf067"


@dataclass
class Slice:
    item: RadialItem
    action: str | None = None
    agent: Agent | None = None
    children: tuple[MenuItem, ...] = ()
    source: str | None = None
    keep_open: bool = False
    favorite: int | None = None  # slot index of a favorites slice


@dataclass
class View:
    title: str
    slices: list[Slice]
    source: str | None = None
    selection: int | None = None
    name: str = ""  # configured title of a source view; `title` may read "No agents" etc.
    items: tuple[MenuItem, ...] = ()  # a configured ring's items, to rebuild it when its favorites change


@dataclass
class Hooks:
    run_action: Callable[[str], None]
    rumble: Callable[[tuple], None]
    monitor: Callable[[], str | None]
    capture_sticks: Callable[[bool], None]
    settings: Callable[[], list[Choice]]  # the app's own toggles for the "settings" source
    window_class: Callable[[], str]  # the focused window's, for pinning it as a favorite
    paused: Callable[[], bool]


def machine_groups(agents: list[Agent]) -> dict[str, tuple[str, RGB]]:
    """host → (group label, colour); local first, SSH hosts in order of appearance."""
    groups: dict[str, tuple[str, RGB]] = {}
    remote = 0
    for agent in agents:
        if agent.host in groups:
            continue
        if agent.local:
            groups[agent.host] = (f"{ICON_LOCAL} {agent.machine}", LOCAL_COLOR)
        else:
            groups[agent.host] = (f"{ICON_REMOTE} {agent.machine}", REMOTE_COLORS[remote % len(REMOTE_COLORS)])
            remote += 1
    return groups


def agent_slices(agents: list[Agent], lights: dict) -> list[Slice]:
    """One slice per agent, grouped by machine, coloured by kind, badged by status (`lights` colours)."""
    ordered = by_machine(agents)[:MAX_SLICES]
    groups = machine_groups(ordered)
    slices = []
    for agent in ordered:
        group, color = groups[agent.host]
        status_color = tuple(lights[agent.status]) if agent.status in ("blocked", "done") else STATUS_GREYS.get(
            agent.status, STATUS_GREYS["idle"]
        )
        slices.append(Slice(
            item=RadialItem(
                label=agent.project,
                sublabel=agent.caption,
                icon=STATUS_ICONS.get(agent.status, ""),
                color=agent.color,
                badge=BADGES.get(agent.status, agent.status.upper()),
                badge_color=status_color,
                pulse=agent.waiting,
                group=group,
                group_color=color,
                detail=" · ".join(p for p in (agent.kind, agent.machine, agent.short_title) if p),
            ),
            agent=agent,
        ))
    return slices


class RadialMenus:
    def __init__(
        self, cfg: Config, ui: RadialMenu, herd: HerdProvider | None, favorites: Favorites, hooks: Hooks
    ) -> None:
        self.cfg = cfg
        self.ui = ui
        self.herd = herd
        self.favorites = favorites
        self.hooks = hooks
        self.stack: list[View] = []
        self.name = ""  # the open radial
        self._hold: tuple[str, int] | None = None  # (button, timer) while □/△ is held to confirm
        self._pin_app: DesktopApp | None = None  # what □ would pin: the focused window's app

    @property
    def is_open(self) -> bool:
        return bool(self.stack)

    # building ------------------------------------------------------------------

    def _static_view(self, title: str, items: tuple[MenuItem, ...]) -> View:
        slices: list[Slice] = []
        for item in items:
            if item.inline and item.source:
                # Splice the live choices into this ring, grouped under the item's label.
                _, live = self._source_slices(item.label, item.source)
                slices += [
                    replace(s, item=replace(s.item, group=item.label, group_color=INLINE_COLOR)) for s in live
                ]
                continue
            slices.append(Slice(
                item=RadialItem(label=item.label, icon=item.icon, detail="›" if item.items or item.source else ""),
                action=item.action,
                children=item.items,
                source=item.source,
                keep_open=item.keep_open,
            ))
        return View(title=title, slices=slices[:MAX_SLICES], items=items)

    def _source_view(self, title: str, source: str) -> View:
        shown, slices = self._source_slices(title, source)
        return View(title=shown, slices=slices, source=source, name=title)

    def _source_slices(self, title: str, source: str) -> tuple[str, list[Slice]]:
        if source == "herd":
            if self.herd is None:
                return "Omaherd not found", []
            slices = agent_slices(self.herd.agents, self.cfg.section("lights"))
            return (title if slices else "No agents"), slices
        if source == "audio":
            slices = [
                Slice(
                    item=RadialItem(
                        label=c.label, icon=c.icon, sublabel=c.sublabel, group=c.group,
                        group_color=c.group_color, active=c.active,
                    ),
                    action=c.action,
                )
                for c in audio_choices(self.cfg.section("audio"))
            ]
            return (title if slices else "No audio devices"), slices
        if source == "voxtype-language":
            choices = voxtype_choices(self.cfg.section("voxtype"))
            if choices is None:
                return "VoxType not found", []
            return title, self._choice_slices(choices[:MAX_SLICES])
        if source == "settings":
            return title, self._choice_slices(self.hooks.settings()[:MAX_SLICES])
        if source == "favorites":
            return title, self._favorite_slices()
        raise ValueError(f"unknown radial source {source!r}")

    def _favorite_slices(self) -> list[Slice]:
        self._pin_app = self.favorites.candidate(self.hooks.window_class())
        slices = []
        for index, favorite in enumerate(self.favorites.shown()[:MAX_SLICES]):
            if favorite is None:
                slices.append(Slice(item=RadialItem(label="Add", icon=ICON_ADD), favorite=index))
                continue
            item = RadialItem(label=favorite.label, icon=favorite.icon, image=favorite.image)
            slices.append(Slice(item=item, action=favorite.action, favorite=index))
        return slices

    @staticmethod
    def _choice_slices(choices: list[Choice]) -> list[Slice]:
        return [
            Slice(
                item=RadialItem(label=c.label, icon=c.icon, sublabel=c.sublabel, active=c.active),
                action=c.action,
                keep_open=c.keep_open,
            )
            for c in choices
        ]

    # session -------------------------------------------------------------------

    def open(self, name: str) -> None:
        self._start(name)
        self._show()

    def _start(self, name: str) -> None:
        radial = self.cfg.radials[name]
        self.name = name
        if radial.source:
            self.stack = [self._source_view(radial.title, radial.source)]
        else:
            self.stack = [self._static_view(radial.title, radial.items)]
        self.hooks.capture_sticks(True)

    def close(self) -> None:
        self._cancel_hold()
        if not self.stack:
            return
        self.stack = []
        self.ui.hide()
        self.hooks.capture_sticks(False)

    def _show(self) -> None:
        self._cancel_hold()
        view = self.stack[-1]
        self.ui.show(view.title, [s.item for s in view.slices], self.hooks.monitor())
        self._sync()

    def _sync(self) -> None:
        """Push the top view's selection, and what the buttons do on it, to the overlay."""
        view = self.stack[-1]
        self.ui.set_selection(view.selection)
        self.ui.set_legend(self._legend(view))

    def refresh_source(self, source: str) -> None:
        """Rebuild open views fed by `source` (HERD statuses changed, a toggle flipped), keeping the selection.
        A configured ring with that source spliced in (`inline`) is rebuilt too."""
        for index, view in enumerate(self.stack):
            spliced = view.source is None and any(item.inline and item.source == source for item in view.items)
            if view.source != source and not spliced:
                continue
            previous = view.selection
            selected = view.slices[previous].agent if previous is not None and previous < len(view.slices) else None
            if spliced:
                title, view.slices = view.title, self._static_view(view.title, view.items).slices
            else:
                title, view.slices = self._source_slices(view.name, source)
            if selected is not None:  # agents can reorder: follow the agent, not the slot
                previous = next((i for i, s in enumerate(view.slices) if s.agent and s.agent.key == selected.key), None)
            view.selection = previous if previous is not None and previous < len(view.slices) else None
            if index != len(self.stack) - 1:
                view.title = title
            elif title != view.title:
                view.title = title
                self._show()
            else:
                self.ui.update_items([s.item for s in view.slices])
                self._sync()

    # input -----------------------------------------------------------------------

    def on_stick(self, x: float, y: float) -> None:
        view = self.stack[-1]
        index = slice_for_vector(x, y, len(view.slices), float(self.cfg.get("input", "radial_deadzone")))
        if index is not None and index != view.selection:
            self._cancel_hold()  # a hold belongs to the slice it started on
            view.selection = index
            self._sync()
            if self.cfg.get("rumble", "feedback"):
                self.hooks.rumble(fx.RUMBLE_TICK)

    def on_button(self, button: str) -> bool:
        """Handle a press while open; False if the button isn't a radial control."""
        if self._hold is not None and button != self._hold[0]:
            self._cancel_hold()
        view = self.stack[-1]
        chosen = self._selected(view)
        if button in ("l3", "r3"):
            self.close()
        elif button == "circle":
            if len(self.stack) > 1:
                self.stack.pop()
                self._show()
            else:
                self.close()
        elif button in ("dpad_left", "dpad_right"):
            if view.slices:
                step = 1 if button == "dpad_right" else -1
                start = view.selection if view.selection is not None else -step
                view.selection = (start + step) % len(view.slices)
                self._sync()
        elif button == "cross":
            if chosen is not None:
                self._activate(chosen)
        elif button in ("square", "triangle"):
            if chosen is not None:
                self._start_hold(button, chosen)
        elif button in ("l1", "r1"):
            if chosen is not None and chosen.favorite is not None:
                self._move(chosen.favorite, -1 if button == "l1" else 1)
        else:
            return False
        return True

    def on_release(self, button: str) -> None:
        if self._hold is not None and self._hold[0] == button:
            self._cancel_hold()  # let go too early: nothing happens

    @staticmethod
    def _selected(view: View) -> Slice | None:
        if view.selection is None or view.selection >= len(view.slices):
            return None
        return view.slices[view.selection]

    def _legend(self, view: View) -> list[tuple[str, str]]:
        """What the buttons do on the selected slice, for the overlay's corner."""
        back = ("○", "Back" if len(self.stack) > 1 else "Close")
        chosen = self._selected(view)
        if chosen is None:
            return [("Stick", "Choose"), back]
        if chosen.favorite is None:
            verb = "Open" if chosen.children or chosen.source else "Go to agent" if chosen.agent else "Select"
            rows = [("✕", verb)]
            if chosen.agent is not None and self.herd is not None:
                rows.append(("△", "Hold: close agent"))
            return [*rows, back]
        filled = self.favorites.slots[chosen.favorite] is not None
        rows = [("✕", "Open" if filled else "Pick an app")]
        verb = self._pin_verb(chosen.favorite)
        if verb and self._pin_app is not None:
            rows.append(("□", f"Hold: {verb} {self._pin_app.name} here"))
        if filled:
            rows += [("△", "Hold: remove"), ("L1 R1", "Move")]
        return [*rows, back]

    def _activate(self, chosen: Slice) -> None:
        self.hooks.rumble(fx.RUMBLE_CONFIRM)
        if chosen.children:
            self.stack.append(self._static_view(chosen.item.label, chosen.children))
            self._show()
        elif chosen.source:
            self.stack.append(self._source_view(chosen.item.label, chosen.source))
            self._show()
        elif chosen.agent is not None:
            self.close()
            if self.herd is not None:
                self.herd.focus(chosen.agent)
        elif chosen.favorite is not None and not chosen.action:  # an empty slot: pick from the list
            name, origin, depth, index = self.name, self.stack[-1], len(self.stack), chosen.favorite
            self.close()
            self.favorites.pick(index, lambda: self._picked(name, origin, depth, index))
        elif chosen.action:
            if not chosen.keep_open:
                self.close()
            self.hooks.run_action(chosen.action)
            if chosen.keep_open and self.stack:  # e.g. a toggle's ✓ flips
                view = self.stack[-1]
                for source in {view.source, *(item.source for item in view.items if item.inline)} - {None}:
                    self.refresh_source(source)

    # holds and favorites ---------------------------------------------------------

    def _pin_verb(self, index: int) -> str | None:
        """What holding □ on slot `index` does with the focused window's app: "put" it there, "move" it
        there from another slot, or None when there's no app or it's already there."""
        if self._pin_app is None:
            return None
        action = f"launch:{self._pin_app.id}"
        if (self.favorites.slots[index] or {}).get("action") == action:
            return None
        return "move" if any(slot and slot["action"] == action for slot in self.favorites.slots) else "put"

    def _start_hold(self, button: str, chosen: Slice) -> None:
        """□ and △ act only once held for input.hold_ms, so a stray press changes nothing. Meanwhile the hub
        says what's about to happen and fills a ring."""
        plan = self._hold_plan(button, chosen)
        if plan is None:
            self.hooks.rumble(fx.RUMBLE_NOTHING)
            return
        (glyph, verb, name), finish = plan
        self._cancel_hold()
        ms = int(self.cfg.get("input", "hold_ms"))
        self._hold = (button, GLib.timeout_add(ms, self._hold_done, finish))
        self.ui.set_hold(glyph, verb, name, ms / 1000)

    def _hold_plan(self, button: str, chosen: Slice) -> tuple[tuple[str, str, str], Callable[[], None]] | None:
        """What holding `button` on the slice does: (glyph, verb, name) for the hub, and the action. On an
        agent △ closes it; on a favorite □ pins the focused window's app there and △ empties the slot."""
        if chosen.agent is not None:
            if button == "triangle" and self.herd is not None:
                return ("△", "Close", chosen.item.label), partial(self.herd.close, chosen.agent)
            return None
        if chosen.favorite is None:
            return None
        index = chosen.favorite
        if button == "square":
            verb, app = self._pin_verb(index), self._pin_app
            if verb and app:
                return ("□", "Move here" if verb == "move" else "Pin here", app.name), partial(self._pin, index, app)
            return None
        if self.favorites.slots[index] is not None:
            return ("△", "Remove", chosen.item.label), partial(self._remove, index)
        return None

    def _pin(self, index: int, app: DesktopApp) -> None:
        self.favorites.pin(index, app)
        self._reload_favorites(index)

    def _remove(self, index: int) -> None:
        self.favorites.remove(index)
        self._reload_favorites(index)

    def _hold_done(self, finish: Callable[[], None]) -> bool:
        self._hold = None
        self.ui.clear_hold()
        self.hooks.rumble(fx.RUMBLE_CONFIRM)
        finish()
        return False

    def _cancel_hold(self) -> None:
        if self._hold is not None:
            GLib.source_remove(self._hold[1])
            self._hold = None
            self.ui.clear_hold()

    def _move(self, index: int, step: int) -> None:
        target = self.favorites.move(index, step)
        if target is None:
            self.hooks.rumble(fx.RUMBLE_NOTHING)
            return
        self.hooks.rumble(fx.RUMBLE_TICK)
        self._reload_favorites(target)

    def _reload_favorites(self, index: int) -> None:
        """Rebuild the open view after an edit, with slot `index` selected."""
        view = self.stack[-1]
        if view.source:
            _, view.slices = self._source_slices(view.name, view.source)
        else:
            view.slices = self._static_view(view.title, view.items).slices
        view.selection = next((i for i, s in enumerate(view.slices) if s.favorite == index), view.selection)
        self.ui.update_items([s.item for s in view.slices])
        self._sync()

    def _picked(self, name: str, origin: View, depth: int, index: int) -> None:
        """Back to the ring the slot is on, rebuilt with the slot selected, unless something else took over
        meanwhile. From a submenu, ○ then leads straight to the top ring."""
        if self.is_open or self.hooks.paused():
            return
        self._start(name)
        if depth > 1:
            if origin.source:
                self.stack.append(self._source_view(origin.name, origin.source))
            else:
                self.stack.append(self._static_view(origin.title, origin.items))
        view = self.stack[-1]
        view.selection = next((i for i, s in enumerate(view.slices) if s.favorite == index), None)
        self._show()
