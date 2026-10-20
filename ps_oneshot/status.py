"""What the control socket's `status` and `watch` report: the controllers, pause and the agents by machine.

One JSON object per line. `plugin/BarWidget.qml` (the Omarchy bar widget) draws it: battery percent, and per
machine how many agents are in each status, coloured like the lightbar and the carousel dots.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import __version__
from .herd import BADGES, Agent, by_machine

STATUSES = ("blocked", "done", "working", "idle")  # most urgent first; anything else counts as idle


@dataclass(frozen=True)
class PadState:
    name: str
    battery: int | None
    charging: bool
    bluetooth: bool
    low: bool  # discharging at or under [battery] low


def snapshot(
    pads: list[PadState], agents: list[Agent], *, paused: bool, game: bool, herd: bool, lights: dict,
) -> dict:
    return {
        "version": __version__,
        "paused": paused,
        "game": game,  # paused because a game has focus
        "pads": [
            {
                "name": p.name.removeprefix("Sony Interactive Entertainment ").removesuffix(" Wireless Controller"),
                "battery": p.battery,
                "charging": p.charging, "bluetooth": p.bluetooth, "low": p.low,
            }
            for p in pads
        ],
        "herd": herd,
        "colors": {status: _hex(lights[status]) for status in ("blocked", "done")},
        "machines": machines(agents),
    }


def machines(agents: list[Agent]) -> list[dict]:
    """Local first, then each SSH host as Omaherd lists it (the R3 ring's order). Within a machine the most
    urgent agents come first, the longest waiting first among waiting ones (the order PS jumps in)."""
    hosts: dict[str, list[Agent]] = {}
    for agent in by_machine(agents):
        hosts.setdefault(agent.host, []).append(agent)
    groups = []
    for members in hosts.values():
        members.sort(key=lambda a: (STATUSES.index(_status(a)), a.since if a.waiting else 0))
        counts: dict[str, int] = dict.fromkeys(STATUSES, 0)
        for agent in members:
            counts[_status(agent)] += 1
        first = members[0]
        groups.append({
            "host": first.host, "name": first.machine, "local": first.local, "counts": counts,
            "agents": [
                {
                    "key": a.key, "project": a.project, "kind": a.kind, "status": _status(a),
                    "badge": BADGES[_status(a)], "caption": a.caption, "color": _hex(a.color),
                }
                for a in members
            ],
        })
    return groups


def encode(state: dict) -> str:
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False)


def _status(agent: Agent) -> str:
    return agent.status if agent.status in STATUSES else "idle"


def _hex(rgb: tuple[int, int, int] | list[int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(c))) for c in rgb[:3]))
