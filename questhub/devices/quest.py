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


async def release_proximity(adb: Adb) -> None:
    """Give the proximity sensor back to the hardware.

    `prox_close` forces a virtual "worn" state that survives until released.
    On current Horizon OS `prox_open` is not received (verified on build 207:
    the virtual state stayed CLOSE); `automation_disable` sets it to DISABLED,
    i.e. the real sensor decides again. `prox_open` is still sent for older OS.
    """
    await adb.shell("am broadcast -a com.oculus.vrpowermanager.prox_open", check=False)
    await adb.shell("am broadcast -a com.oculus.vrpowermanager.automation_disable", check=False)


async def restore_tweaks(adb: Adb, guardian: bool = True, prox: bool = True) -> None:
    if guardian:
        await adb.shell("setprop debug.oculus.guardian_pause 1", check=False)
    if prox:
        await release_proximity(adb)
