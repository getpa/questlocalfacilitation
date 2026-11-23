"""Fake adb and helpers for service-level tests (no headset, no adb binary)."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from questhub.adb.client import AdbError  # noqa: E402
from questhub.adb.tracker import AdbDevice  # noqa: E402

WIFI_STATUS = ('Wifi is connected to "{ssid}"\nWifiInfo: SSID: "{ssid}", BSSID: 6e:00:00:00:00:{n:02x}, '
               'IP: /{ip}, Supplicant state: COMPLETED, RSSI: -40, Link speed: 1200Mbps, Frequency: 5180MHz, Net ID: 1')
BATTERY = "Current Battery Service state:\n  AC powered: false\n  USB powered: {usb}\n  status: 3\n  level: {level}\n"


class FakeHeadset:
    def __init__(self, serial: str, ip: str, ssid: str = "lab-net", level: int = 80, n: int = 1):
        self.serial, self.ip, self.ssid, self.level, self.n = serial, ip, ssid, level, n
        self.tcpip = False
        self.commands: list[tuple] = []


class FakeAdbWorld:
    """Shared state: which headsets exist and how `adb connect` behaves."""

    def __init__(self, headsets: list[FakeHeadset]):
        self.headsets = {h.serial: h for h in headsets}
        self.by_endpoint = {f"{h.ip}:5555": h for h in headsets}
        self.connect_result: dict[str, str] = {}      # endpoint -> adb connect output override
        self.connected: set[str] = set()
        self.log: list[tuple] = []

    def adb(self, serial=None) -> "FakeAdb":
        return FakeAdb(self, serial)


class FakeAdb:
    binary = "fake-adb"

    def __init__(self, world: FakeAdbWorld, serial=None):
        self.world = world
        self.serial = serial

    def for_serial(self, serial):
        return FakeAdb(self.world, serial)

    def _headset(self) -> FakeHeadset:
        h = self.world.headsets.get(self.serial) or self.world.by_endpoint.get(self.serial)
        if h is None:
            raise AdbError(("-s", str(self.serial)), 1, "device not found")
        return h

    async def run(self, *args, timeout=30, check=True):
        self.world.log.append((self.serial, args))
        if args[0] == "connect":
            endpoint = args[1]
            out = self.world.connect_result.get(endpoint)
            if out is None:
                out = f"connected to {endpoint}" if endpoint in self.world.by_endpoint else \
                    f"failed to connect to '{endpoint}': Operation timed out"
            if out.startswith("connected"):
                self.world.connected.add(endpoint)
            return out
        if args[0] == "disconnect":
            self.world.connected.discard(args[1])
            return "disconnected"
        h = self._headset()
        if args[0] == "tcpip":
            h.tcpip = True
            return f"restarting in TCP mode port: {args[1]}"
        if args[0] == "usb":
            h.tcpip = False
            return "restarting in USB mode"
        if args[0] == "shell":
            return self._shell(h, args[1])
        return ""

    def _shell(self, h: FakeHeadset, cmd: str) -> str:
        h.commands.append(cmd)
        if cmd.startswith("getprop ro.serialno"):
            return h.serial + "\n"
        if cmd.startswith("getprop ro.product.model"):
            return "Quest 3\n"
        if cmd.startswith("getprop"):
            return "\n"
        if cmd.startswith("ip -o -4 addr"):
            return f"15: wlan0    inet {h.ip}/24 brd 0.0.0.0 scope global wlan0\n"
        if cmd.startswith("cmd wifi status"):
            return WIFI_STATUS.format(ssid=h.ssid, ip=h.ip, n=h.n)
        if cmd.startswith("dumpsys battery"):
            return BATTERY.format(level=h.level, usb="false")
        return ""

    async def shell(self, command, timeout=30, check=True):
        return await self.run("shell", command, timeout=timeout, check=check)

    async def getprop(self, name, timeout=10):
        return (await self.shell(f"getprop {name}")).strip()


class TrackFeed:
    """Push device lists into DeviceManager like `adb track-devices` would."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()

    def push(self, *devices: tuple[str, str]) -> None:
        self.queue.put_nowait([AdbDevice(s, st, {"model": "Quest_3"}) for s, st in devices])

    async def source(self):
        while True:
            yield await self.queue.get()


async def until(predicate, timeout: float = 5.0, step: float = 0.02):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    raise AssertionError("condition not met in time")
