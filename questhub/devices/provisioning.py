"""One headset's provisioning steps (USB -> TCP/IP). No registry writes here."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import re

from questhub.adb.client import Adb, AdbError


@dataclass
class Identity:
    serial: str
    model: str | None


async def read_identity(adb: Adb) -> Identity:
    serial = await adb.getprop("ro.serialno")
    model = await adb.getprop("ro.product.model")
    return Identity(serial=serial or adb.serial, model=model or None)


_INET = re.compile(r"\binet (\d+\.\d+\.\d+\.\d+)/(\d+)")


def parse_ip(text: str) -> str | None:
    """First global IPv4 address from `ip -o -4 addr show scope global` (wlan preferred)."""
    candidates = []
    for line in text.splitlines():
        m = _INET.search(line)
        if not m:
            continue
        addr = m.group(1)
        if ipaddress.ip_address(addr).is_loopback:
            continue
        candidates.append((0 if "wlan" in line else 1, addr))
    return sorted(candidates)[0][1] if candidates else None


async def detect_ip(adb: Adb, attempts: int = 6) -> str | None:
    for _ in range(attempts):
        try:
            ip = parse_ip(await adb.shell("ip -o -4 addr show scope global", timeout=10))
        except AdbError:
            ip = None
        if ip:
            return ip
        await asyncio.sleep(0.5)
    for prop in ("dhcp.wlan0.ipaddress", "dhcp.wlan1.ipaddress"):
        value = await adb.getprop(prop)
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value or ""):
            return value
    return None


async def enable_tcpip(adb: Adb, port: int = 5555) -> None:
    out = await adb.run("tcpip", str(port), timeout=20)
    if "error" in out.lower():
        raise AdbError(("tcpip", str(port)), 1, out)


def classify_connect(output: str) -> str:
    """Map `adb connect` output to connected | refused | unreachable | failed."""
    text = output.lower()
    if "connected to" in text and "cannot" not in text and "failed" not in text:
        return "connected"
    if "refused" in text:
        return "refused"          # host is up but adbd is not listening: needs USB again
    if any(s in text for s in ("no route", "timed out", "unreachable", "operation timed out", "host is down")):
        return "unreachable"
    return "failed"


async def connect(adb: Adb, endpoint: str, timeout: float = 10) -> tuple[str, str]:
    try:
        out = await adb.for_serial(None).run("connect", endpoint, timeout=timeout, check=False)
    except AdbError as exc:
        return "unreachable", str(exc)
    return classify_connect(out), out.strip()


async def disconnect(adb: Adb, endpoint: str) -> None:
    await adb.for_serial(None).run("disconnect", endpoint, timeout=10, check=False)


async def back_to_usb(adb: Adb) -> None:
    """Turn Wi-Fi ADB off on the headset (`adb usb`)."""
    await adb.run("usb", timeout=15, check=False)
