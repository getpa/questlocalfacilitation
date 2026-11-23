"""Locations of the project environment and bundled binaries."""
from __future__ import annotations

import os
from pathlib import Path
import shutil

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PREFIX = Path(os.environ.get("QH_ENV_DIR") or os.environ.get("ENV_PREFIX") or PROJECT_ROOT / ".mamba-env")
DATA_DIR = Path(os.environ.get("QH_DATA_DIR") or Path.home() / ".questhub")


def binary(name: str, env_var: str | None = None) -> str:
    """Prefer an explicit override, then the project env, then PATH (like _env_utils.sh)."""
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    for candidate in (ENV_PREFIX / "bin" / name, ENV_PREFIX / "vendor/platform-tools" / name):
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"{name} not found; run ./install.sh")


def scrcpy_server_jar() -> Path:
    override = os.environ.get("QH_SCRCPY_SERVER")
    path = Path(override) if override else ENV_PREFIX / "bin/scrcpy-server"
    if not path.exists():
        raise FileNotFoundError(f"scrcpy-server not found at {path}; run ./install.sh")
    return path.resolve()
