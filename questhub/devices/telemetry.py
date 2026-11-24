"""One-shot device measurements (battery, Wi-Fi). Periodic polling is a service concern."""
from __future__ import annotations

from dataclasses import dataclass

from questhub.adb.client import Adb, AdbError
from questhub.devices.wifi_parse import WifiInfo, parse_wifi_info


@dataclass(frozen=True)
class Battery:
    level: int | None
    charging: bool | None
    status: str | None


_STATUS = {"1": "unknown", "2": "charging", "3": "discharging", "4": "not_charging", "5": "full"}


def parse_battery(text: str) -> Battery:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep:
            values.setdefault(key.strip(), value.strip())
    level = int(values["level"]) if values.get("level", "").isdigit() else None
    powered = [values.get(k) == "true" for k in ("AC powered", "USB powered", "Wireless powered")]
    status = _STATUS.get(values.get("status", ""))
    charging = status in ("charging", "full") or any(powered) if (status or values) else None
    return Battery(level=level, charging=charging, status=status)


async def read_battery(adb: Adb) -> Battery | None:
    try:
        return parse_battery(await adb.shell("dumpsys battery", timeout=10))
    except AdbError:
        return None


async def read_wifi(adb: Adb) -> WifiInfo | None:
    for command in ("cmd wifi status", "dumpsys wifi | grep -m1 mWifiInfo"):
        try:
            info = parse_wifi_info(await adb.shell(command, timeout=10))
        except AdbError:
            continue
        if info is not None:
            return info
    return None
