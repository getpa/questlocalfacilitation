"""Locations of the project environment and bundled binaries (macOS and Windows)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

IS_WINDOWS = sys.platform == "win32"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PREFIX = Path(os.environ.get("QH_ENV_DIR") or os.environ.get("ENV_PREFIX") or PROJECT_ROOT / ".mamba-env")
DATA_DIR = Path(os.environ.get("QH_DATA_DIR") or Path.home() / ".questhub")

# Where install.sh / install.ps1 put things inside the environment.
#   macOS:   bin/{adb,scrcpy,scrcpy-server} (symlinks into vendor/)
#   Windows: vendor/scrcpy/{adb.exe,scrcpy.exe,scrcpy-server} (the official scrcpy-win64 zip,
#            which bundles a matching adb)
_SEARCH_DIRS = (("vendor/scrcpy", "vendor/platform-tools", "Library/bin", "Scripts") if IS_WINDOWS
                else ("bin", "vendor/platform-tools"))


def _exe(name: str) -> str:
    return name + ".exe" if IS_WINDOWS and not name.endswith(".exe") else name


def binary(name: str, env_var: str | None = None) -> str:
    """Prefer an explicit override, then the project env, then PATH (like _env_utils.sh)."""
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    for folder in _SEARCH_DIRS:
        candidate = ENV_PREFIX / folder / _exe(name)
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"{name} not found; run {installer_hint()}")


def scrcpy_server_jar() -> Path:
    override = os.environ.get("QH_SCRCPY_SERVER")
    if override:
        path = Path(override)
    else:
        path = ENV_PREFIX / ("vendor/scrcpy/scrcpy-server" if IS_WINDOWS else "bin/scrcpy-server")
    if not path.exists():
        raise FileNotFoundError(f"scrcpy-server not found at {path}; run {installer_hint()}")
    return path.resolve()


def installer_hint() -> str:
    return "install.cmd" if IS_WINDOWS else "./install.sh"
