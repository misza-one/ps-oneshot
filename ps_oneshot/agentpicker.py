"""`ps-oneshot agents`: open the running daemon's agent carousel, or without a daemon, a stand-in picker.

The daemon gets `herd:pick` over its control socket and shows its own carousel, live and blinking. When no
daemon answers, this process polls Omaherd once, draws the same cards as PNGs and shows them in Omarchy's
image selector (the theme switcher's carousel; static), then goes to the chosen agent as the R3 ring does.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import APP_NAME, control
from .agentcards import card_order, draw_card, most_urgent
from .config import Config
from .herd import Agent, HerdProvider
from .hypr import Hyprland, HyprlandError
from .ui.theme import load_theme

log = logging.getLogger(__name__)

PICK_TIMEOUT_S = 600  # give up on a selector that never answers (omarchy-shell restarted under it)
DONE_POLL_S = 0.02


def card_names(agents: list[Agent]) -> list[str]:
    """One distinct file name per card. The selector filters on it, so it carries what you'd type."""
    names: list[str] = []
    for agent in agents:
        parts = [agent.project, agent.short_title, agent.kind]
        if not agent.local:
            parts.append(agent.machine)
        base = " ".join(p for p in parts if p)
        base = "".join(" " if c in "/\t\n\r" else c for c in base).strip()[:120] or "agent"
        name, n = base, 2
        while name in names:
            name, n = f"{base} {n}", n + 1
        names.append(name)
    return names



# the picker -------------------------------------------------------------------------------------------


def _notify(text: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", APP_NAME, "-t", "4000", text], check=False, timeout=3)


def _cards_dir() -> str:
    """A new directory per run, the old ones removed: the selector's image cache is keyed by path."""
    root = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "ps-oneshot", "agents")
    os.makedirs(root, exist_ok=True)
    for old in os.listdir(root):
        shutil.rmtree(os.path.join(root, old), ignore_errors=True)
    return tempfile.mkdtemp(dir=root)


def choose(paths: list[str], selected: str) -> str | None:
    """Show the cards in the image selector; the chosen card's path, or None when it was dismissed."""
    rows = "\n".join(f"{p}\t{p}" for p in paths)
    directory = os.path.dirname(paths[0])
    selection, done = os.path.join(directory, ".selection"), os.path.join(directory, ".done")
    open(selection, "w").close()
    encoded = base64.b64encode(rows.encode()).decode()
    result = subprocess.run(
        ["omarchy-shell", "image-selector", "open", "", encoded, selected, selection, done, "false", "true"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode or result.stdout.strip() != "ok":
        raise OSError((result.stderr or result.stdout).strip() or "image selector refused the request")
    deadline = time.monotonic() + PICK_TIMEOUT_S
    while not os.path.exists(done):
        if time.monotonic() > deadline:
            subprocess.run(["omarchy-shell", "image-selector", "cancel", done], capture_output=True, timeout=5)
            return None
        time.sleep(DONE_POLL_S)
    with open(selection) as handle:
        chosen = handle.read().strip()
    return chosen or None


def run(cfg: Config) -> int:
    """The daemon's carousel when it's running, else the image-selector picker."""
    reply = control.send("herd:pick")
    if reply == "ok":
        return 0
    if reply is not None:
        print(f"agents: {reply}", file=sys.stderr)
        return 1
    return pick(cfg)


def pick(cfg: Config) -> int:
    herd_cfg = cfg.section("herd")
    if not HerdProvider.available(herd_cfg):
        print("agents: Omaherd isn't available ([herd] enabled, status_command, attach)", file=sys.stderr)
        return 1
    if shutil.which("omarchy-shell") is None:
        print("agents: needs omarchy-shell (Omarchy's image selector)", file=sys.stderr)
        return 1
    try:
        hypr = Hyprland()
    except HyprlandError as error:
        print(f"agents: {error}", file=sys.stderr)
        return 1
    provider = HerdProvider(herd_cfg, lambda _agents: None, hypr)
    agents = provider.poll()
    if agents is None:
        _notify("Omaherd didn't answer")
        return 1
    if not agents:
        _notify("No agents running")
        return 0

    ordered = card_order(agents)
    theme, lights, now = load_theme(), cfg.section("lights"), time.time()
    directory = _cards_dir()
    try:
        paths = [os.path.join(directory, f"{name}.png") for name in card_names(ordered)]
        for path, agent in zip(paths, ordered):
            draw_card(path, agent, theme, lights, now)
        try:
            chosen = choose(paths, paths[ordered.index(most_urgent(ordered))])
        except (OSError, subprocess.TimeoutExpired) as error:
            log.warning("image selector: %s", error)
            _notify(f"Couldn't open the agent picker: {error}")
            return 1
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if chosen is None:
        return 0
    by_path = dict(zip(paths, ordered))
    agent = by_path.get(chosen)
    if agent is None:
        log.warning("image selector chose %r, not one of the cards", chosen)
        return 1
    log.info("picked %s on %s", agent.project, agent.machine)
    provider.focus_now(agent)
    return 0
