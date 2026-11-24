"""Point a USB-connected headset at a Wi-Fi network (`cmd wifi connect-network`).

Verified on Quest 3 (Horizon OS, Android 14) as the adb shell user (uid 2000):
`cmd wifi connect-network` is allowed, `add-network` is not (SecurityException).
The command saves the network and connects to it, like choosing it in Settings.

Only for headsets on USB: switching the network of a headset reached over Wi-Fi
ADB would cut that connection. The passphrase is sent to the headset's shell on
stdin, so it never appears in the adb argument list (visible in `ps`), in
AdbError messages, in logs or in events.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
import time

from questhub.adb.client import Adb, AdbError
from questhub.devices.telemetry import read_wifi
from questhub.devices.wifi_parse import WifiInfo

SECURITY_TYPES = ("wpa2", "wpa3", "open")
CONNECT_TIMEOUT = 30.0     # seconds to wait for the headset to report the new SSID
POLL_INTERVAL = 1.0


class WifiConfigError(ValueError):
    pass


@dataclass(frozen=True)
class WifiTarget:
    ssid: str
    security: str = "wpa2"
    passphrase: str = ""

    def validate(self) -> "WifiTarget":
        if not self.ssid or len(self.ssid.encode("utf-8")) > 32:
            raise WifiConfigError("SSID は 1〜32 バイトで指定してください")
        if any(c in self.ssid for c in "\r\n\0"):
            raise WifiConfigError("SSID に改行は使えません")
        if self.security not in SECURITY_TYPES:
            raise WifiConfigError(f"セキュリティは {', '.join(SECURITY_TYPES)} のいずれかです")
        if self.security == "open":
            if self.passphrase:
                raise WifiConfigError("オープンなネットワークにはパスワードを指定しません")
        else:
            if not 8 <= len(self.passphrase) <= 63:
                raise WifiConfigError("パスワードは 8〜63 文字です（WPA2/WPA3）")
            if any(c in self.passphrase for c in "\r\n\0"):
                raise WifiConfigError("パスワードに改行は使えません")
        return self


def sh_quote(value: str) -> str:
    """Single-quote for the headset's /system/bin/sh."""
    return "'" + value.replace("'", "'\\''") + "'"


def connect_script(target: WifiTarget) -> str:
    parts = ["cmd", "wifi", "connect-network", sh_quote(target.ssid), target.security]
    if target.security != "open":
        parts.append(sh_quote(target.passphrase))
    return " ".join(parts) + "\n"


SHELL_UID = 2000
SAVED_CONFIGS_CMD = ("dumpsys wifi | sed -n '/Configured networks Begin/,/Configured networks End/p'"
                     " | grep -E '(^|[^A-Za-z])ID: [0-9]+ SSID: |cuid='")
_SAVED_ID = re.compile(r'^(?:\* )?ID: \d+ SSID: "((?:[^"\\]|\\.)*)"')
_CREATOR = re.compile(r"\bcuid=(\d+) cname=(\S+)")


def parse_saved_creators(text: str) -> dict[str, tuple[int, str]]:
    """SSID -> (creator uid, creator package) of the networks saved on the headset."""
    creators: dict[str, tuple[int, str]] = {}
    ssid = None
    for line in text.splitlines():
        m = _SAVED_ID.match(line.strip())
        if m:
            ssid = m.group(1)
            continue
        c = _CREATOR.search(line)
        if c and ssid is not None:
            creators.setdefault(ssid, (int(c.group(1)), c.group(2)))
            ssid = None
    return creators


async def foreign_owner(adb: Adb, ssid: str) -> str | None:
    """Package that saved `ssid` if it is not the adb shell (shell may not update it)."""
    try:
        creators = parse_saved_creators(await adb.shell(SAVED_CONFIGS_CMD, timeout=20, check=False))
    except AdbError:
        return None
    owner = creators.get(ssid)
    if owner and owner[0] != SHELL_UID:
        return owner[1]
    return None


def command_failed(output: str) -> str | None:
    text = output.strip()
    low = text.lower()
    if any(s in low for s in ("exception", "invalid args", "error", "failed", "does not have access")):
        return text.splitlines()[0][:200] if text else "unknown error"
    return None


async def connect_network(adb: Adb, target: WifiTarget, timeout: float | None = None,
                          poll: float | None = None) -> WifiInfo:
    """Ask the headset to join `target` and wait until it reports that SSID."""
    target.validate()
    timeout = CONNECT_TIMEOUT if timeout is None else timeout
    poll = POLL_INTERVAL if poll is None else poll
    owner = await foreign_owner(adb, target.ssid)
    if owner:
        # Verified on Quest 3: WifiConfigManager rejects uid 2000 updating a network
        # another app saved ("does not have permission to update configuration").
        raise WifiConfigError(
            f"{target.ssid} はヘッドセットに保存済みです（保存元: {owner}）。Quest は adb からこの設定の変更・接続を"
            "許可しません。ヘッドセットの設定で接続するか、保存元（MDM など）で配布してください")
    out = await adb.run("shell", "sh", input=connect_script(target).encode("utf-8"), timeout=20, check=False)
    problem = command_failed(out)
    if problem:
        raise AdbError(("shell", "cmd wifi connect-network"), 1, problem)
    deadline = time.monotonic() + timeout
    last: WifiInfo | None = None
    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        last = await read_wifi(adb)
        if last and last.ssid == target.ssid:
            return last
    seen = last.ssid if last and last.ssid else "未接続"
    raise TimeoutError(f"{timeout:.0f} 秒以内に {target.ssid} へ接続できませんでした（現在: {seen}）。"
                       "SSID・パスワード・電波を確認してください")
