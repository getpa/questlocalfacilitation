"""Parse Wi-Fi status from `cmd wifi status` / `dumpsys wifi` (pure functions).

Example line (Quest 3, Android 14):
  WifiInfo: SSID: "lab-net", BSSID: 6e:7f:f0:11:81:70, MAC: ..., RSSI: -32,
  Link speed: 2401Mbps, ..., Frequency: 6375MHz, ...
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re

UNKNOWN_SSIDS = {"", "<unknown ssid>", "0x"}


@dataclass(frozen=True)
class WifiInfo:
    ssid: str | None
    bssid: str | None = None
    rssi_dbm: int | None = None
    freq_mhz: int | None = None
    link_mbps: int | None = None

    @property
    def band(self) -> str | None:
        return band_for(self.freq_mhz)

    def to_dict(self) -> dict:
        return {**asdict(self), "band": self.band}


def band_for(freq_mhz: int | None) -> str | None:
    if not freq_mhz:
        return None
    if freq_mhz < 3000:
        return "2.4GHz"
    if freq_mhz < 5925:
        return "5GHz"
    return "6GHz"


_SSID = re.compile(r'SSID: (?:"((?:[^"\\]|\\.)*)"|([^,]*))')
_BSSID = re.compile(r"BSSID: ([0-9a-fA-F:]{17})")
_RSSI = re.compile(r"RSSI: (-?\d+)")
_FREQ = re.compile(r"Frequency: (\d+)\s*MHz")
_LINK = re.compile(r"(?:^|[:,] )Link speed: (-?\d+)\s*Mbps")
_CONNECTED = re.compile(r'connected to "((?:[^"\\]|\\.)*)"')


def _int(match: re.Match | None) -> int | None:
    return int(match.group(1)) if match else None


def parse_wifi_info(text: str) -> WifiInfo | None:
    """Return the primary WifiInfo, or None when Wi-Fi is not connected."""
    line = next((l for l in text.splitlines() if "WifiInfo" in l and "SSID:" in l), None)
    if line is None:
        connected = _CONNECTED.search(text)
        return WifiInfo(ssid=connected.group(1)) if connected else None
    ssid_match = _SSID.search(line)
    ssid = None
    if ssid_match:
        ssid = ssid_match.group(1) if ssid_match.group(1) is not None else ssid_match.group(2).strip()
        if ssid in UNKNOWN_SSIDS:
            ssid = None
    if "Supplicant state: DISCONNECTED" in line and ssid is None:
        return None
    bssid = _BSSID.search(line)
    bssid_value = bssid.group(1).lower() if bssid else None
    if bssid_value == "02:00:00:00:00:00":  # redacted / unavailable
        bssid_value = None
    link = _int(_LINK.search(line))
    return WifiInfo(
        ssid=ssid,
        bssid=bssid_value,
        rssi_dbm=_int(_RSSI.search(line)),
        freq_mhz=_int(_FREQ.search(line)),
        link_mbps=link if link is not None and link >= 0 else None,
    )
