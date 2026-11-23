"""Follow `adb track-devices -l`: every change in the device list, pushed by adb."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import logging

from questhub.adb.client import Adb

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdbDevice:
    serial: str                 # USB serial, or "ip:port" for Wi-Fi transports
    state: str                  # device | unauthorized | offline | authorizing | ...
    props: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def is_wifi(self) -> bool:
        return ":" in self.serial

    @property
    def model(self) -> str | None:
        return self.props.get("model")


def parse_device_list(text: str) -> list[AdbDevice]:
    """Parse `adb devices -l` / track-devices payload lines."""
    devices = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        props = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append(AdbDevice(parts[0], parts[1], props))
    return devices


async def read_frames(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """track-devices output: 4 hex digits of length, then that many bytes."""
    while True:
        try:
            header = await stream.readexactly(4)
            size = int(header.decode("ascii"), 16)
            payload = await stream.readexactly(size) if size else b""
        except (asyncio.IncompleteReadError, ValueError):
            return
        yield payload.decode("utf-8", errors="replace")


async def track_devices(adb: Adb, restart_delay: float = 1.0) -> AsyncIterator[list[AdbDevice]]:
    """Yield the full device list on every change; restarts adb if it exits."""
    while True:
        proc = await adb.spawn("track-devices", "-l")
        try:
            async for payload in read_frames(proc.stdout):
                yield parse_device_list(payload)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        log.warning("adb track-devices exited; restarting")
        await asyncio.sleep(restart_delay)
