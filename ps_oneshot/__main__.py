"""ps-oneshot CLI: `run` (default), `check`, `devices`."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__
from .preload import ensure_layer_shell_preload


def _devices(cfg) -> int:
    from .linux import dualsense

    devices = cfg.section("devices")
    controllers = dualsense.discover(devices["match"], devices["ignore"])
    if not controllers:
        print("no controller found (match %r, ignore %r)" % (devices["match"], devices["ignore"]))
        return 1
    for controller in controllers:
        capacity, status = controller.battery_state()
        print(controller.name)
        print(f"  gamepad   {controller.gamepad.path}")
        print(f"  touchpad  {controller.touchpad.path if controller.touchpad else '-'}")
        print(f"  hidraw    {controller.hidraw or '-'}  (mic button: {_access(controller.hidraw, os.R_OK)})")
        led_access = _access(controller.lightbar and os.path.join(controller.lightbar, "brightness"), os.W_OK)
        print(f"  lightbar  {controller.lightbar or '-'}  (writable: {led_access})")
        if led_access == "NO":
            print("            install packaging/70-ps-oneshot.rules (./install.sh) and reconnect the controller")
        print(f"  battery   {'-' if capacity is None else f'{capacity}% {status}'}")
        controller.close()
    return 0


def _access(path: str | None, mode: int) -> str:
    if not path:
        return "n/a"
    return "yes" if os.access(path, mode) else "NO"


def _check(cfg) -> int:
    from .herd import HerdProvider
    from .hypr import Hyprland, HyprlandError

    ok = True
    print(f"config    {' → '.join(cfg.sources)} (preset {cfg.preset})")
    try:
        hypr = Hyprland()
        print(f"hyprland  ok ({'Lua' if hypr.lua else 'legacy'} dispatch, monitor {hypr.focused_monitor()})")
    except (HyprlandError, OSError) as error:
        print(f"hyprland  FAIL {error}")
        ok = False
    uinput = os.access("/dev/uinput", os.R_OK | os.W_OK)
    print(f"uinput    {'ok' if uinput else 'NO access to /dev/uinput (menus/touchpad need it)'}")
    ok &= uinput
    print(f"herd      {'on' if HerdProvider.available(cfg.section('herd')) else 'off (Omaherd not found)'}")
    return (0 if ok else 1) | _devices(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ps-oneshot", description="Drive Hyprland with a DualSense controller.")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "check", "devices"))
    parser.add_argument("-c", "--config", help="config file (default ~/.config/ps-oneshot/config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"ps-oneshot {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    from .config import ConfigError, load

    try:
        cfg = load(args.config)
    except ConfigError as error:
        print(f"config error: {error}", file=sys.stderr)
        return 2
    if args.command == "devices":
        return _devices(cfg)
    if args.command == "check":
        return _check(cfg)

    ensure_layer_shell_preload()
    from .app import run
    from .hypr import HyprlandError

    try:
        run(cfg)
    except HyprlandError as error:
        print(f"ps-oneshot: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
