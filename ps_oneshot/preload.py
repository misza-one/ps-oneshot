"""gtk4-layer-shell must be linked before libwayland-client; from Python that means LD_PRELOAD."""

from __future__ import annotations

import os
import sys

_LIBRARY = "libgtk4-layer-shell.so"
_SEARCH = ("/usr/lib", "/usr/lib64", "/usr/local/lib", "/usr/lib/x86_64-linux-gnu")


def _library_path() -> str | None:
    for directory in _SEARCH:
        for name in (_LIBRARY, _LIBRARY + ".0"):
            path = os.path.join(directory, name)
            if os.path.exists(path):
                return path
    return None


def ensure_layer_shell_preload() -> None:
    preload = os.environ.get("LD_PRELOAD", "")
    if _LIBRARY in preload:
        return
    path = _library_path()
    if path is None:
        return
    env = dict(os.environ)
    env["LD_PRELOAD"] = f"{path} {preload}".strip()
    os.execve(sys.executable, [sys.executable, *_argv()], env)


def _argv() -> list[str]:
    # `python -m pkg` leaves sys.argv[0] as the module's file path; re-run it as a module so
    # relative imports keep working.
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if spec is not None and spec.name:
        name = spec.name.removesuffix(".__main__")
        return ["-m", name, *sys.argv[1:]]
    return list(sys.argv)
