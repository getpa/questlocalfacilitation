"""LAN scan for headsets listening on Wi-Fi ADB (port from discover_quest3.py)."""
from __future__ import annotations

import asyncio
import ipaddress
import subprocess
import sys


def mask_to_prefix(mask: str) -> int:
    if mask.startswith("0x"):
        return bin(int(mask, 16)).count("1")
    return sum(bin(int(octet)).count("1") for octet in mask.split("."))


def _usable(iface: ipaddress.IPv4Interface) -> bool:
    return not (iface.ip.is_loopback or iface.ip.is_link_local)


def parse_ifconfig(out: str) -> list[ipaddress.IPv4Interface]:
    result = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "inet" and parts[2] == "netmask":
            iface = ipaddress.ip_interface(f"{parts[1]}/{mask_to_prefix(parts[3])}")
            if _usable(iface):
                result.append(iface)
    return result


# Locale-independent (unlike ipconfig, whose labels are translated on Japanese Windows).
WINDOWS_IP_COMMAND = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                      "Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress,PrefixLength "
                      "| ConvertTo-Json -Compress"]


def parse_windows_ip(out: str) -> list[ipaddress.IPv4Interface]:
    import json
    try:
        data = json.loads(out or "[]")
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    result = []
    for item in data:
        try:
            iface = ipaddress.ip_interface(f"{item['IPAddress']}/{int(item['PrefixLength'])}")
        except (KeyError, TypeError, ValueError):
            continue
        if _usable(iface):
            result.append(iface)
    return result


def local_networks() -> list[ipaddress.IPv4Interface]:
    """IPv4 interfaces of this computer, excluding loopback and link-local."""
    windows = sys.platform == "win32"
    command = WINDOWS_IP_COMMAND if windows else ["ifconfig"]
    try:
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if windows else {}
        out = subprocess.run(command, capture_output=True, text=True, timeout=10, **kwargs).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_windows_ip(out) if windows else parse_ifconfig(out)


def hosts_to_scan(network: ipaddress.IPv4Network, exclude: set[str], limit: int = 4096) -> list[str]:
    """Hosts of `network`, but at most `limit` (a /16 is narrowed to the /22 around us)."""
    hosts = []
    for host in network.hosts():
        if str(host) not in exclude:
            hosts.append(str(host))
        if len(hosts) >= limit:
            break
    return hosts


def default_scan_ranges() -> list[ipaddress.IPv4Network]:
    """Our own subnets; very large ones are narrowed to the /22 that contains us."""
    ranges = []
    for iface in local_networks():
        net = iface.network
        if net.prefixlen < 22:
            net = ipaddress.ip_network(f"{iface.ip}/22", strict=False)
        ranges.append(net)
    return ranges


async def port_open(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True


async def scan(hosts: list[str], port: int = 5555, timeout: float = 0.4, concurrency: int = 256,
               progress=None) -> list[str]:
    sem = asyncio.Semaphore(concurrency)
    found: list[str] = []
    done = 0

    async def probe(host: str) -> None:
        nonlocal done
        async with sem:
            if await port_open(host, port, timeout):
                found.append(host)
        done += 1
        if progress and done % 64 == 0:
            progress(done, len(hosts), len(found))

    await asyncio.gather(*(probe(h) for h in hosts))
    if progress:
        progress(len(hosts), len(hosts), len(found))
    return sorted(found, key=lambda h: ipaddress.ip_address(h))
