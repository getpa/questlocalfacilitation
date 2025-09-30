#!/usr/bin/env python3
"""Discover Meta Quest devices reachable via Wi-Fi ADB (port 5555 by default).

The script scans a CIDR range (auto-detected from the primary interface when
possible) and reports hosts that accept TCP connections on the target port.
Optionally it can invoke `adb connect` for each match and fetch model/serial
metadata using `adb shell getprop`.
"""
from __future__ import annotations

import argparse
import ipaddress
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from shutil import which
from typing import Iterable, List, Optional

DEFAULT_PORT = 5555
DEFAULT_TIMEOUT = 0.6
DEFAULT_WORKERS = 64
DEFAULT_INTERFACES = ("en0", "en1", "en2")


@dataclass
class HostResult:
    host: str
    port: int
    is_open: bool
    model: Optional[str] = None
    serial: Optional[str] = None
    notes: Optional[str] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cidr",
        help="CIDR to scan, e.g. 192.168.10.0/24. Auto-detected if omitted (macOS en*).",
    )
    parser.add_argument(
        "--interface",
        help="Interface to inspect when auto-detecting the CIDR (default: en0→en1→en2).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"TCP port to test (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Socket timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Concurrent worker threads (default: {DEFAULT_WORKERS}).",
    )
    parser.add_argument(
        "--connect",
        action="store_true",
        help="Run 'adb connect' for discovered hosts and fetch model/serial metadata.",
    )
    parser.add_argument(
        "--adb-path",
        dest="adb_path",
        help="Path to adb binary (defaults to resolving via $PATH).",
    )
    return parser.parse_args()


def detect_cidr(interface: Optional[str] = None) -> tuple[ipaddress.IPv4Interface, ipaddress.IPv4Network]:
    """Return the active IPv4 interface and network for the given interface."""
    candidates = [interface] if interface else list(DEFAULT_INTERFACES)
    for iface in candidates:
        if not iface:
            continue
        try:
            ip = subprocess.check_output(["ipconfig", "getifaddr", iface], text=True).strip()
            if not ip:
                continue
            mask = subprocess.check_output(["ipconfig", "getoption", iface, "subnet_mask"], text=True).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        if mask:
            prefix = mask_to_prefix(mask)
            iface_obj = ipaddress.ip_interface(f"{ip}/{prefix}")
            return iface_obj, iface_obj.network
    raise RuntimeError("Unable to auto-detect network. Please supply --cidr explicitly.")


def mask_to_prefix(mask: str) -> int:
    bits = sum(bin(int(octet)).count("1") for octet in mask.split("."))
    return bits


def iter_hosts(network: ipaddress.IPv4Network, local_ip: Optional[str]) -> Iterable[str]:
    for host in network.hosts():
        host_str = str(host)
        if local_ip and host_str == local_ip:
            continue
        yield host_str


def check_port(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def adb_connect(adb_path: str, endpoint: str) -> Optional[str]:
    result = subprocess.run(
        [adb_path, "connect", endpoint],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return result.stdout.strip()


def adb_getprop(adb_path: str, endpoint: str, prop: str) -> Optional[str]:
    result = subprocess.run(
        [adb_path, "-s", endpoint, "shell", "getprop", prop],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    value = result.stdout.strip()
    return value or None


def enrich_with_adb(adb_path: str, host: str, port: int) -> tuple[Optional[str], Optional[str]]:
    endpoint = f"{host}:{port}"
    adb_connect(adb_path, endpoint)
    model = adb_getprop(adb_path, endpoint, "ro.product.model")
    serial = adb_getprop(adb_path, endpoint, "ro.serialno")
    return model, serial


def main() -> int:
    args = parse_args()

    adc = args.adb_path or which("adb")
    if args.connect and not adc:
        print("[!] --connect specified but adb not found. Install or omit --connect.", file=sys.stderr)
        return 2

    if args.cidr:
        network = ipaddress.ip_network(args.cidr, strict=False)
        local_ip = None
    else:
        iface_obj, network = detect_cidr(args.interface)
        local_ip = str(iface_obj.ip)
        print(f"[i] Auto-detected interface {iface_obj} on network {network}")

    hosts = list(iter_hosts(network, local_ip))
    if not hosts:
        print("[!] No scan targets generated (check CIDR or interface).", file=sys.stderr)
        return 1

    total = len(hosts)
    print(f"[i] Scanning {total} hosts on {network} (port {args.port})")

    results: List[HostResult] = []
    checked = 0
    last_report = -1
    report_step = max(1, total // 20)  # ~5% increments
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {executor.submit(check_port, host, args.port, args.timeout): host for host in hosts}
        for future in as_completed(future_map):
            host = future_map[future]
            is_open = future.result()
            checked += 1
            if checked - last_report >= report_step or checked == total:
                last_report = checked
                percent = checked * 100 // total if total else 100
                print(f"    progress: {checked}/{total} ({percent}%)")
            if is_open:
                model = serial = None
                notes = None
                if args.connect and adc:
                    try:
                        model, serial = enrich_with_adb(adc, host, args.port)
                    except Exception as exc:
                        notes = f"adb error: {exc!s}"
                meta_parts = []
                if model:
                    meta_parts.append(model)
                if serial:
                    meta_parts.append(serial)
                if notes:
                    meta_parts.append(notes)
                meta_str = f" ({' | '.join(meta_parts)})" if meta_parts else ""
                print(f"    [+] Found {host}:{args.port}{meta_str}")
                results.append(HostResult(host=host, port=args.port, is_open=True, model=model, serial=serial, notes=notes))

    if not results:
        print("[i] No Quest endpoints discovered.")
        return 0

    print("\nDiscovered endpoints:")
    for item in sorted(results, key=lambda x: x.host):
        endpoint = f"{item.host}:{item.port}"
        meta = []
        if item.model:
            meta.append(item.model)
        if item.serial:
            meta.append(item.serial)
        if item.notes:
            meta.append(item.notes)
        meta_str = f" ({'; '.join(meta)})" if meta else ""
        print(f" - {endpoint}{meta_str}")

    print("\nSuggested quest_devices.tsv rows:")
    for idx, item in enumerate(sorted(results, key=lambda x: x.host), start=1):
        alias = f"Quest-{idx:02d}"
        print(f"{alias}\t{item.host}:{item.port}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
