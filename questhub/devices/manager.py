"""Connection state of every headset, and automatic Wi-Fi reconnection.

Owns only connectivity (docs/GUI_DESIGN.md §3.4); media state lives in the
stream supervisor. A headset is identified by its serial (ro.serialno), which
may be reachable over USB, over Wi-Fi ADB (ip:port), or both.

States: usb (USB only), wifi, unauthorized, connecting, offline, needs_usb.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import logging
import time

from questhub.adb.client import Adb, AdbError
from questhub.adb.tracker import AdbDevice, track_devices
from questhub.core import events as ev
from questhub.core.events import EventBus
from questhub.devices import provisioning
from questhub.devices.registry import Registry

log = logging.getLogger(__name__)

RECONNECT_TICK = 2.0
MIN_BACKOFF = 3.0


@dataclass
class DeviceStatus:
    serial: str
    state: str = "offline"
    usb_id: str | None = None          # adb serial of the USB transport
    usb_state: str | None = None
    endpoint: str | None = None        # ip:port of the Wi-Fi transport
    wifi_state: str | None = None
    model: str | None = None
    last_error: str | None = None
    next_attempt: float = 0.0
    backoff: float = MIN_BACKOFF
    attempts: int = 0
    changed_at: float = 0.0

    @property
    def target(self) -> str | None:
        """adb -s target to use for this headset (Wi-Fi preferred)."""
        if self.wifi_state == "device" and self.endpoint:
            return self.endpoint
        if self.usb_state == "device" and self.usb_id:
            return self.usb_id
        return None

    def compute_state(self) -> str:
        if self.wifi_state == "device":
            return "wifi"
        if self.usb_state == "device":
            return "usb"
        if "unauthorized" in (self.usb_state, self.wifi_state):
            return "unauthorized"
        if self.state in ("needs_usb", "connecting"):
            return self.state
        return "offline"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["target"] = self.target
        return data


class DeviceManager:
    def __init__(self, registry: Registry, bus: EventBus, adb: Adb | None = None,
                 max_backoff: float = 30.0, track_source=None, auto_reconnect: bool = True):
        self.registry = registry
        self.bus = bus
        self.adb = adb or Adb()
        self.max_backoff = max_backoff
        self.auto_reconnect = auto_reconnect
        self._track_source = track_source or (lambda: track_devices(self.adb.for_serial(None)))
        self.status: dict[str, DeviceStatus] = {}
        self._endpoint_serial: dict[str, str] = {}   # ip:port -> serial
        self._usb_serial: dict[str, str] = {}        # usb id -> serial
        self._identifying: set[str] = set()
        self._last_list: list[AdbDevice] = []
        self._tasks: list[asyncio.Task] = []
        self.paused: set[str] = set()                # serials excluded from auto reconnect

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._track()))
        if self.auto_reconnect:
            self._tasks.append(asyncio.create_task(self._reconnect_loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------ queries

    def get(self, serial: str) -> DeviceStatus:
        if serial not in self.status:
            self.status[serial] = DeviceStatus(serial=serial)
        return self.status[serial]

    def target(self, serial: str) -> str | None:
        status = self.status.get(serial)
        return status.target if status else None

    def adb_for(self, serial: str) -> Adb | None:
        target = self.target(serial)
        return self.adb.for_serial(target) if target else None

    def usb_devices(self) -> list[dict]:
        """USB transports, including ones whose serial is not yet known (unauthorized)."""
        result = []
        for d in self._last_list:
            if d.is_wifi:
                continue
            serial = self._usb_serial.get(d.serial, d.serial)
            status = self.status.get(serial)
            result.append({"usb_id": d.serial, "serial": serial, "state": d.state, "model": d.model,
                           "wifi": bool(status and status.wifi_state == "device")})
        return result

    def snapshot(self) -> dict[str, dict]:
        return {s: st.to_dict() for s, st in self.status.items()}

    # ------------------------------------------------------------ tracking

    async def _track(self) -> None:
        async for devices in self._track_source():
            try:
                await self.apply_device_list(devices)
            except Exception:
                log.exception("device list update failed")

    async def apply_device_list(self, devices: list[AdbDevice]) -> None:
        self._last_list = devices
        seen_usb, seen_wifi = {}, {}
        for d in devices:
            (seen_wifi if d.is_wifi else seen_usb)[d.serial] = d
        # Map transports to serials (identify new ones in the background).
        for ident, d in {**seen_usb, **seen_wifi}.items():
            known = self._endpoint_serial.get(ident) if d.is_wifi else self._usb_serial.get(ident)
            if known is None and d.state == "device" and ident not in self._identifying:
                self._identifying.add(ident)
                asyncio.create_task(self._identify(ident, d))
            elif known is None and d.state == "unauthorized" and not d.is_wifi:
                self.bus.publish(ev.USB_DEVICE_ATTACHED, None, usb_id=ident, state=d.state)
        # Refresh every known status from the current list.
        for serial, status in self.status.items():
            usb = next((seen_usb[u] for u, s in self._usb_serial.items() if s == serial and u in seen_usb), None)
            status.usb_id = usb.serial if usb else status.usb_id
            status.usb_state = usb.state if usb else None
            wifi = seen_wifi.get(status.endpoint) if status.endpoint else None
            status.wifi_state = wifi.state if wifi else None
            if status.wifi_state == "device":
                status.backoff, status.attempts, status.last_error = MIN_BACKOFF, 0, None
            self._set_state(status, status.compute_state())

    async def _identify(self, ident: str, device: AdbDevice) -> None:
        try:
            identity = await provisioning.read_identity(self.adb.for_serial(ident))
        except AdbError as exc:
            log.warning("cannot identify %s: %s", ident, exc)
            self._identifying.discard(ident)
            return
        serial = identity.serial
        status = self.get(serial)
        status.model = identity.model or device.model
        ip = port = None
        if device.is_wifi:
            self._endpoint_serial[ident] = serial
            status.endpoint = ident
            ip, _, p = ident.rpartition(":")
            port = int(p)
        else:
            self._usb_serial[ident] = serial
            status.usb_id = ident
            if serial != ident:
                self.status.pop(ident, None)
            self.bus.publish(ev.USB_DEVICE_ATTACHED, serial, usb_id=ident, state=device.state)
        self.registry.observe(serial, "adb", ip=ip, port=port, model=status.model)
        if device.is_wifi:  # a TSV placeholder for this endpoint has become a real headset
            for key in [k for k, st in self.status.items() if k.startswith("tsv:") and st.endpoint == ident]:
                self.status.pop(key)
        self._identifying.discard(ident)
        await self.apply_device_list(self._last_list)

    def _set_state(self, status: DeviceStatus, state: str) -> None:
        if state == status.state:
            return
        old, status.state, status.changed_at = status.state, state, time.time()
        self.bus.publish(ev.CONNECTION_CHANGED, status.serial, old=old, state=state,
                         target=status.target, error=status.last_error)

    # ------------------------------------------------------------ reconnection

    async def _reconnect_loop(self) -> None:
        while True:
            try:
                await self.reconnect_due()
            except Exception:
                log.exception("reconnect pass failed")
            await asyncio.sleep(RECONNECT_TICK)

    async def reconnect_due(self, force: bool = False) -> None:
        now = time.monotonic()
        jobs = []
        for row in self.registry.list():
            if not row["ip"] or row["serial"] in self.paused:
                continue
            endpoint = f"{row['ip']}:{row['port'] or 5555}"
            serial = row["serial"]
            status = self.get(serial)
            if status.endpoint != endpoint:
                status.endpoint = endpoint
                if not serial.startswith("tsv:"):
                    self._endpoint_serial[endpoint] = serial
            if status.wifi_state == "device" or (not force and now < status.next_attempt):
                continue
            jobs.append(self._try_connect(status, endpoint))
        if jobs:
            await asyncio.gather(*jobs)

    async def _try_connect(self, status: DeviceStatus, endpoint: str) -> None:
        if status.state not in ("wifi", "usb"):
            self._set_state(status, "connecting")
        result, output = await provisioning.connect(self.adb, endpoint)
        status.attempts += 1
        if result == "connected":
            status.last_error = None
            status.backoff = MIN_BACKOFF
            status.next_attempt = time.monotonic() + MIN_BACKOFF
            return  # the tracker reports the new transport
        status.last_error = output[-200:]
        status.next_attempt = time.monotonic() + status.backoff
        status.backoff = min(status.backoff * 2, self.max_backoff)
        state = "needs_usb" if result == "refused" else "offline"
        if status.usb_state == "device":
            state = "usb"
        self._set_state(status, state)

    async def connect_now(self, serial: str) -> str:
        row = self.registry.get(serial)
        if not row or not row["ip"]:
            raise ValueError("no IP address registered for this headset")
        status = self.get(serial)
        status.next_attempt = 0
        status.backoff = MIN_BACKOFF
        self.paused.discard(serial)
        await self._try_connect(status, f"{row['ip']}:{row['port'] or 5555}")
        return status.state

    async def disconnect(self, serial: str) -> None:
        status = self.get(serial)
        self.paused.add(serial)
        if status.endpoint:
            await provisioning.disconnect(self.adb, status.endpoint)
