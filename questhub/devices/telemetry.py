"""One-shot device measurements (battery, Wi-Fi). Periodic polling is a service concern."""
from __future__ import annotations

from questhub.adb.client import Adb, AdbError
from questhub.devices.wifi_parse import WifiInfo, parse_wifi_info


async def read_wifi(adb: Adb) -> WifiInfo | None:
    for command in ("cmd wifi status", "dumpsys wifi | grep -m1 mWifiInfo"):
        try:
            info = parse_wifi_info(await adb.shell(command, timeout=10))
        except AdbError:
            continue
        if info is not None:
            return info
    return None
