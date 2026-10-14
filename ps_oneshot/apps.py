"""Installed desktop apps (XDG .desktop entries, read through Gio): names, icons, which windows are theirs,
and `launch:` (focus the app's window, or start it)."""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from gi.repository import Gio

from .actions import spawn
from .hypr import Hyprland

log = logging.getLogger(__name__)

# Chromium web apps (`--app=<url>`, Omarchy's `omarchy-launch-webapp <url>`) all run the browser, so only
# the URL tells them apart. Their windows are classed "<browser>-<host>_<path>-<profile>", for example
# chrome-web.whatsapp.com__-Default.
APP_URL = re.compile(r"(?:--app=|omarchy-launch-webapp\s+)[\"']?(https?://[^\s\"']+)")


@dataclass(frozen=True)
class DesktopApp:
    id: str  # desktop file id, e.g. "org.gnome.Nautilus.desktop"
    name: str
    icon: str = ""  # Gio icon string: a theme icon name or a file
    path: str = ""  # the .desktop file
    wm_class: str = ""  # StartupWMClass
    url: str = ""  # a web app's URL

    def owns(self, window_class: str) -> bool:
        """A window of this class is this app's: its StartupWMClass, its desktop id (a Wayland app id usually
        is one) or, for a web app, the browser's class for the app's host."""
        lowered = window_class.lower()
        if not lowered:
            return False
        if lowered in (self.wm_class.lower(), self.id.removesuffix(".desktop").lower()):
            return True
        host = urlsplit(self.url).hostname if self.url else None
        return bool(host) and f"-{host}_" in lowered


def installed(visible_only: bool = False) -> list[DesktopApp]:
    """Every desktop app; `visible_only` keeps those a launcher would list."""
    apps = []
    for info in Gio.AppInfo.get_all():
        if visible_only and not info.should_show():
            continue
        if not hasattr(info, "get_filename"):
            continue  # not a .desktop entry
        icon = info.get_icon()
        url = APP_URL.search(info.get_commandline() or "")
        apps.append(DesktopApp(
            id=info.get_id() or "",
            name=info.get_display_name() or info.get_name() or "",
            icon=icon.to_string() or "" if icon else "",
            path=info.get_filename() or "",
            wm_class=info.get_startup_wm_class() or "",
            url=url.group(1) if url else "",
        ))
    return apps


def find(app_id: str) -> DesktopApp | None:
    return next((app for app in installed() if app.id == app_id), None)


def app_for_window(window_class: str, apps: list[DesktopApp]) -> DesktopApp | None:
    return next((app for app in apps if app.owns(window_class)), None)


def window_of(app: DesktopApp, clients: list[dict[str, Any]]) -> str | None:
    """Address of the app's most recently focused window, if it has one open."""
    mine = [client for client in clients if app.owns(str(client.get("class", "")))]
    if not mine:
        return None
    return str(min(mine, key=lambda client: client.get("focusHistoryID", 1 << 30))["address"])


def launch(hypr: Hyprland, app_id: str, command: str) -> None:
    """Focus the app's window, or start it with `command` ({id}, {path} placeholders)."""
    app = find(app_id)
    if app is None:
        log.warning("launch:%s: no such desktop entry", app_id)
        return
    try:
        clients = hypr.json("clients")
    except (OSError, ValueError) as error:
        log.warning("cannot list windows: %s", error)
        clients = []
    address = window_of(app, clients)
    if address:
        hypr.focus_window(address)
    else:
        spawn(command.format(id=shlex.quote(app.id), path=shlex.quote(app.path)))
