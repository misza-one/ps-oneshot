"""Omaherd/herdr agent inbox: poll status, rank urgency, focus an agent's pane.

Talks to Omaherd directly (same commands its bar widget uses), so it works with or without a
Stream Deck.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import GLib

from . import APP_NAME
from .actions import spawn
from .hypr import Hyprland

log = logging.getLogger(__name__)

URGENCY = {"blocked": 3, "done": 2, "working": 1, "idle": 0}
BADGES = {"blocked": "WAIT", "done": "DONE", "working": "RUN", "idle": "IDLE"}
STATUS_ICONS = {"blocked": "\uf256", "done": "\uf00c", "working": "\uf013", "idle": "\uf186"}  # Nerd Font
STATUS_GREYS = {"working": (90, 102, 118), "idle": (58, 62, 70)}
KIND_COLORS = {
    "pi": (244, 114, 182), "claude": (224, 145, 90), "codex": (52, 211, 153),
    "gemini": (96, 165, 250), "cursor": (167, 139, 250), "devin": (251, 191, 36),
    "agy": (236, 72, 153), "cline": (45, 212, 191), "omp": (248, 79, 204),
    "mastracode": (251, 113, 133), "opencode": (59, 130, 246), "copilot": (192, 132, 252),
    "kimi": (253, 224, 71), "kiro": (251, 146, 60), "droid": (74, 222, 128),
    "amp": (232, 121, 249), "grok": (226, 232, 240), "hermes": (163, 230, 53),
    "kilo": (252, 165, 165), "qodercli": (125, 211, 252), "qwen": (129, 140, 248),
    "maki": (253, 164, 175),
}
HERDR_OPTIONS_WITH_VALUES = {"--session", "--remote", "--remote-keybindings"}
HERDR_NONCLIENT_FLAGS = {"--no-session", "--default-config", "--skill", "--version", "-V", "--help", "-h"}
# A non-interactive ssh shell often lacks the dirs herdr gets installed into.
REMOTE_PATH = '"$HOME/.local/bin:$HOME/bin:$HOME/.local/share/mise/shims:$HOME/.nix-profile/bin:/usr/local/bin:$PATH"'
# Omaherd's ssh connection masters (its status polling keeps one open per host): riding them is instant.
OMAHERD_SSH = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "omaherd")


@dataclass(frozen=True)
class Agent:
    key: str
    project: str
    kind: str
    status: str
    title: str
    pane_id: str
    session: str
    host: str
    hostname: str
    since: int
    tab: str = ""
    focused: bool = False  # the focused pane of its herdr session

    @property
    def local(self) -> bool:
        return self.host == "local"

    @property
    def machine(self) -> str:
        """Machine name for display: the real hostname, falling back to the SSH alias."""
        return self.hostname or self.host

    @property
    def caption(self) -> str:
        """What tells sibling agents of one project apart: a named tab, else the task title."""
        tab = self.tab.strip()
        return tab if tab and not tab.isdigit() else self.short_title

    @property
    def urgency(self) -> int:
        return URGENCY.get(self.status, 0)

    @property
    def waiting(self) -> bool:
        return self.status in ("blocked", "done")

    @property
    def color(self) -> tuple[int, int, int]:
        return KIND_COLORS.get(self.kind, (226, 232, 240))

    @property
    def short_title(self) -> str:
        """Title without the agent's leading glyph/spinner tokens (e.g. "π ⠏ Fix tests" → "Fix tests")."""
        words = self.title.split()
        while words and len(words[0]) == 1:
            words.pop(0)
        return " ".join(words)


def parse_payload(text: str) -> list[Agent] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        return None
    agents = []
    for entry in data["agents"]:
        if not isinstance(entry, dict):
            continue
        host = str(entry.get("host") or "local")
        pane = str(entry.get("paneId") or "")
        agents.append(Agent(
            key=str(entry.get("key") or f"{host}|{entry.get('session')}|{pane}"),
            project=str(entry.get("workspaceLabel") or entry.get("cwdLabel") or "project"),
            kind=str(entry.get("agent") or entry.get("displayAgent") or entry.get("name") or "").lower(),
            status=str(entry.get("status") or "unknown").lower(),
            title=str(entry.get("title") or ""),
            pane_id=pane,
            session=str(entry.get("session") or "default"),
            host=host,
            hostname=str(entry.get("hostname") or ""),
            since=_int(entry.get("since")),
            tab=str(entry.get("tabLabel") or ""),
            focused=bool(entry.get("focused")),
        ))
    return agents


def by_machine(agents: list[Agent]) -> list[Agent]:
    """Local agents first, then each SSH host in order of first appearance; order kept within a host."""
    hosts: dict[str, int] = {}
    for agent in agents:
        hosts.setdefault(agent.host, len(hosts) + 1)
    return sorted(agents, key=lambda a: 0 if a.local else hosts[a.host])


def agent_in_window(agents: list[Agent], title: str) -> Agent | None:
    """The agent a herdr window shows: titles read "host: workspace"; the focused pane wins."""
    machine, _, project = title.partition(": ")
    candidates = [a for a in agents if machine in (a.hostname, a.host) and a.project == project.strip()]
    return next((a for a in candidates if a.focused), candidates[0] if candidates else None)


def _int(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def waiting_queue(agents: list[Agent]) -> list[Agent]:
    """Waiting agents, most urgent first; within a status the longest-waiting first."""
    return sorted((a for a in agents if a.waiting), key=lambda a: (-a.urgency, a.since))


def top_status(agents: list[Agent]) -> str | None:
    best = max(agents, key=lambda a: a.urgency, default=None)
    return best.status if best is not None and best.urgency > 0 else None


def transitions(before: dict[str, str], agents: list[Agent]) -> list[Agent]:
    """Agents that just entered a waiting status (not on first sight)."""
    return [a for a in agents if a.waiting and a.key in before and before[a.key] != a.status]


class HerdProvider:
    def __init__(self, cfg: dict, on_update: Callable[[list[Agent]], None], hypr: Hyprland) -> None:
        self._status = [os.path.expanduser(p) for p in cfg["status_command"]]
        self._fallback = [os.path.expanduser(p) for p in cfg["status_fallback"]]
        self._attach = os.path.expanduser(cfg["attach"])
        self._terminal = cfg["terminal"]
        self._interval = float(cfg["interval"])
        self._hypr = hypr
        self._on_update = on_update
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.agents: list[Agent] = []
        self.online = False

    @staticmethod
    def available(cfg: dict) -> bool:
        if cfg["enabled"] is False:
            return False
        if cfg["enabled"] is True:
            return True
        return shutil.which(cfg["status_command"][0]) is not None and os.path.isfile(
            os.path.expanduser(cfg["attach"])
        )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ps-oneshot-herd", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while True:
            agents = self.poll()
            GLib.idle_add(self._deliver, agents)
            if self._stop.wait(self._interval):
                return

    def _deliver(self, agents: list[Agent] | None) -> bool:
        self.online = agents is not None
        self.agents = agents or []
        self._on_update(self.agents)
        return False

    def poll(self) -> list[Agent] | None:
        for command in (self._status, self._fallback):
            if not command or shutil.which(command[0]) is None:
                continue
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                continue
            agents = parse_payload(result.stdout)
            if agents is not None:
                return agents
        return None

    def focus(self, agent: Agent) -> None:
        if (command := self._focus_command(agent)) is not None:
            threading.Thread(target=self._focus_sequence, args=(command, agent), daemon=True).start()

    def focus_now(self, agent: Agent) -> None:
        """`focus`, blocking until it's done (for a one-shot process that exits right after)."""
        if (command := self._focus_command(agent)) is not None:
            self._focus_sequence(command, agent)

    def _focus_command(self, agent: Agent) -> list[str] | None:
        if not agent.pane_id or not os.path.isfile(self._attach):
            log.info("cannot focus %s: no pane id or attach helper", agent.key)
            return None
        return [
            self._attach,
            "--host", agent.host,
            "--session", agent.session,
            "--target", agent.pane_id,
            "--workspace", agent.project,
            "--hostname", agent.hostname,
            "--timeout", "3",
            "--focus",
        ]

    def close(self, agent: Agent) -> None:
        """Close the agent's herdr pane, which ends the agent, then show the list without it."""
        if not agent.pane_id:
            log.info("cannot close %s: no pane id", agent.key)
            return
        command = herdr_command(agent.host, agent.session, "pane", "close", agent.pane_id)
        threading.Thread(target=self._close, args=(command, agent), daemon=True).start()

    def _close(self, command: list[str], agent: Agent) -> None:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
            failure = (result.stderr or result.stdout).strip().splitlines()[-1:] if result.returncode else None
        except (OSError, subprocess.TimeoutExpired) as error:
            failure = [str(error)]
        if failure is not None:
            reason = failure[0] if failure else "herdr failed"
            log.warning("closing %s failed: %s", agent.key, reason)
            spawn(["notify-send", "-a", APP_NAME, "-t", "4000", f"Couldn't close {agent.project}: {reason}"])
            return
        log.info("closed %s on %s", agent.project, agent.machine)
        GLib.idle_add(self._deliver, self.poll())  # gone from the ring now, not at the next poll

    def _focus_sequence(self, command: list[str], agent: Agent) -> None:
        try:
            clients = self._hypr.json("clients")
        except (OSError, ValueError):
            clients = []
        windows = session_windows(agent.host, agent.session, clients)
        if windows:
            # A window shows this session: go there and never open another. omaherd raises the one
            # showing the agent's workspace and switches it to the pane. When several show other
            # workspaces it can't tell them apart and would attach in a new terminal instead, so
            # --no-attach, and the most recently used one comes up first.
            self._hypr.focus_window(windows[0])
            _run_detached([*command, "--no-attach"])
            return
        # No window shows it: select the pane, then open a whole client on the session. (omaherd's own
        # fallback attaches to the one agent only, which later focuses wouldn't count as a window.)
        before = self._hypr.active_window()[2]
        try:
            ok = subprocess.run(
                [*command, "--no-attach"], stdin=subprocess.DEVNULL, capture_output=True, timeout=10
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired) as error:
            log.warning("herd focus failed: %s", error)
            return
        if ok and self._hypr.active_window()[2] == before:  # omaherd raised nothing it recognised either
            _run_detached([*self._terminal.split(), self._attach, "--host", agent.host, "--session", agent.session])


def _run_detached(command: list[str]) -> None:
    try:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        ).wait()
    except OSError as error:
        log.warning("cannot run %s: %s", command[0], error)


def herdr_command(host: str, session: str, *args: str, timeout: int = 3) -> list[str]:
    """`herdr --session <session> <args>` on the agent's machine: here, or over ssh for an SSH host (on
    Omaherd's connection master when there is one)."""
    herdr = ["herdr", "--session", session, *args]
    if host == "local":
        return herdr
    options = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}"]
    if os.path.isdir(OMAHERD_SSH):
        options += ["-o", "ControlMaster=auto", "-o", f"ControlPath={OMAHERD_SSH}/ssh-%C", "-o", "ControlPersist=60"]
    script = f"PATH={REMOTE_PATH}; export PATH; exec {shlex.join(herdr)}"
    return ["ssh", *options, "--", host, f"exec /bin/sh -lc {shlex.quote(script)}"]


def _option_value(command: list[str], option: str, default: str = "") -> str:
    for index, part in enumerate(command[1:], start=1):
        if part == option and index + 1 < len(command):
            return command[index + 1]
        if part.startswith(option + "="):
            return part.split("=", 1)[1]
    return default


def _is_herdr_client(command: list[str]) -> bool:
    if not command or os.path.basename(command[0]) != "herdr":
        return False
    if any(flag in HERDR_NONCLIENT_FLAGS for flag in command[1:]):
        return False
    skip_value = False
    for part in command[1:]:
        if skip_value:
            skip_value = False
            continue
        if part in HERDR_OPTIONS_WITH_VALUES:
            skip_value = True
            continue
        if any(part.startswith(option + "=") for option in HERDR_OPTIONS_WITH_VALUES) or part.startswith("-"):
            continue
        return False  # a subcommand, not an interactive client
    return True


def _is_remote_herdr_client(command: list[str], host: str, session: str) -> bool:
    """`ssh … -- <host> …` whose remote command ends in an interactive herdr for `session`. omaherd wraps it as
    `exec /bin/sh -lc '…; exec herdr --session <session>'`; a one-agent `… agent attach <pane>` doesn't count."""
    if not command or os.path.basename(command[0]) != "ssh" or "--" not in command:
        return False
    separator = command.index("--")
    if separator + 2 >= len(command) or command[separator + 1] != host:
        return False
    remote = " ".join(command[separator + 2:])
    start = remote.rfind("exec herdr")
    if start < 0:
        return False
    try:
        herdr = shlex.split(remote[start + len("exec "):].rstrip("'\" "))
    except ValueError:
        return False
    return _is_herdr_client(herdr) and _option_value(herdr, "--session", "default") == session


def _shows_session(command: list[str], host: str, session: str) -> bool:
    """A herdr client process for `session` on `host`: local `herdr`, `herdr --remote <host>`, or the
    `ssh -t -- <host> … exec herdr --session <session>` omaherd attaches with."""
    if host != "local" and _is_remote_herdr_client(command, host, session):
        return True
    if not _is_herdr_client(command):
        return False
    remote = _option_value(command, "--remote")
    if (host == "local" and remote) or (host != "local" and remote != host):
        return False
    return _option_value(command, "--session", "default") == session


def session_windows(host: str, session: str, clients: list[dict], proc_root: str = "/proc") -> list[str]:
    """Addresses of the windows showing herdr `session` on `host`, most recently used first: each window
    whose process holds such a client (Hyprland `clients`, matched by walking up from the client's pid)."""
    parents: dict[int, int] = {}
    holders: list[int] = []
    try:
        entries = list(os.scandir(proc_root))
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            with open(os.path.join(entry.path, "cmdline"), "rb") as handle:
                raw = handle.read()
            with open(os.path.join(entry.path, "stat"), encoding="utf-8", errors="replace") as handle:
                parent = int(handle.read().rsplit(")", 1)[1].split()[1])  # comm may hold spaces and ")"
        except (OSError, IndexError, ValueError):
            continue
        pid = int(entry.name)
        parents[pid] = parent
        if _shows_session([part.decode("utf-8", "replace") for part in raw.split(b"\0") if part], host, session):
            holders.append(pid)
    by_pid: dict[int, list[dict]] = {}
    for client in clients:
        by_pid.setdefault(int(client.get("pid") or 0), []).append(client)
    found: dict[str, dict] = {}
    for pid in holders:
        for _ in range(32):
            if pid in by_pid:
                found.update((str(window["address"]), window) for window in by_pid[pid])
                break
            pid = parents.get(pid, 0)
            if pid <= 1:
                break
    return sorted(found, key=lambda address: found[address].get("focusHistoryID", 1 << 30))
