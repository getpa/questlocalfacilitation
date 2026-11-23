"""Quest-specific device actions: wake, display state, OS tweaks (quest_os_tweaks.sh)."""
from __future__ import annotations

from questhub.adb.client import Adb


async def wake(adb: Adb) -> None:
    await adb.shell("input keyevent KEYCODE_WAKEUP", timeout=10, check=False)


async def engage_tweaks(adb: Adb, guardian: bool = True, prox: bool = True) -> None:
    if guardian:
        await adb.shell("setprop debug.oculus.guardian_pause 0", check=False)
    if prox:
        await adb.shell("am broadcast -a com.oculus.vrpowermanager.prox_close", check=False)


async def restore_tweaks(adb: Adb, guardian: bool = True, prox: bool = True) -> None:
    if guardian:
        await adb.shell("setprop debug.oculus.guardian_pause 1", check=False)
    if prox:
        await adb.shell("am broadcast -a com.oculus.vrpowermanager.prox_open", check=False)
