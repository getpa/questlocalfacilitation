"""Use case: poll battery and Wi-Fi for connected headsets; the registry raises SSID warnings."""
from __future__ import annotations

import asyncio
import logging

from questhub.core import events as ev
from questhub.devices.manager import DeviceManager
from questhub.devices.registry import Registry
from questhub.devices.telemetry import read_battery, read_wifi

log = logging.getLogger(__name__)


class TelemetryService:
    def __init__(self, manager: DeviceManager, registry: Registry, interval: float = 60.0):
        self.manager = manager
        self.registry = registry
        self.bus = manager.bus
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._events = None

    async def start(self) -> None:
        self._events = self.bus.subscribe(ev.CONNECTION_CHANGED)
        self._task = asyncio.create_task(self._run())
        self._watch = asyncio.create_task(self._on_connect())

    async def stop(self) -> None:
        for task in (self._task, getattr(self, "_watch", None)):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self._task, getattr(self, "_watch", None)) if t), return_exceptions=True)
        if self._events:
            self._events.close()

    async def _run(self) -> None:
        while True:
            await self.poll_all()
            await asyncio.sleep(self.interval)

    async def _on_connect(self) -> None:
        # Measure right after a headset connects (baseline SSID, battery).
        async for event in self._events:
            if event.data.get("state") in ("wifi", "usb") and event.serial:
                asyncio.create_task(self.poll(event.serial))

    async def poll_all(self) -> None:
        serials = [s for s, st in self.manager.status.items() if st.target and not s.startswith("tsv:")]
        await asyncio.gather(*(self.poll(s) for s in serials), return_exceptions=True)

    async def poll(self, serial: str) -> None:
        adb = self.manager.adb_for(serial)
        if adb is None:
            return
        battery = await read_battery(adb)
        wifi = await read_wifi(adb)
        self.registry.observe(serial, "telemetry", wifi=wifi,
                              battery=battery.level if battery else None,
                              charging=battery.charging if battery else None)
        self.bus.publish(ev.TELEMETRY_UPDATED, serial,
                         battery=battery.level if battery else None,
                         ssid=wifi.ssid if wifi else None, rssi=wifi.rssi_dbm if wifi else None)
