"""Use case: switch USB headsets to Wi-Fi ADB and register them; LAN scan."""
from __future__ import annotations

import asyncio
import ipaddress
import logging

from questhub.adb.client import AdbError
from questhub.core import events as ev
from questhub.devices import discovery, provisioning
from questhub.devices.manager import DeviceManager
from questhub.devices.registry import Registry
from questhub.devices.telemetry import read_battery, read_wifi

log = logging.getLogger(__name__)


class ProvisioningService:
    def __init__(self, manager: DeviceManager, registry: Registry):
        self.manager = manager
        self.registry = registry
        self.bus = manager.bus
        self.scanning = False

    def _progress(self, usb_id: str, step: str, **data) -> None:
        self.bus.publish(ev.PROVISIONING_PROGRESS, data.pop("serial", None), usb_id=usb_id, step=step, **data)

    async def enable_wifi(self, usb_ids: list[str], port: int = 5555) -> list[dict]:
        return list(await asyncio.gather(*(self._enable_one(u, port) for u in usb_ids)))

    async def _enable_one(self, usb_id: str, port: int) -> dict:
        adb = self.manager.adb.for_serial(usb_id)
        result = {"usb_id": usb_id, "ok": False}
        try:
            self._progress(usb_id, "identify")
            identity = await provisioning.read_identity(adb)
            serial = result["serial"] = identity.serial
            wifi = await read_wifi(adb)
            battery = await read_battery(adb)
            ip = await provisioning.detect_ip(adb)
            if not ip:
                raise RuntimeError("Wi-Fi の IP アドレスを取得できません（ヘッドセットが Wi-Fi に接続されているか確認）")
            self._progress(usb_id, "tcpip", serial=serial, ip=ip)
            await provisioning.enable_tcpip(adb, port)
            endpoint = f"{ip}:{port}"
            state, output = "failed", ""
            for _ in range(10):  # adbd restarts in TCP mode; give it a moment
                await asyncio.sleep(1.0)
                state, output = await provisioning.connect(self.manager.adb, endpoint)
                if state == "connected":
                    break
            self.registry.observe(serial, "usb", ip=ip, port=port, wifi=wifi, model=identity.model,
                                  battery=battery.level if battery else None,
                                  charging=battery.charging if battery else None)
            self.manager.paused.discard(serial)
            if state != "connected":
                raise RuntimeError(f"adb connect {endpoint} に失敗: {output}")
            result.update(ok=True, ip=ip, endpoint=endpoint, ssid=wifi.ssid if wifi else None)
            self._progress(usb_id, "done", serial=serial, ip=ip, ssid=result["ssid"])
        except (AdbError, RuntimeError, OSError) as exc:
            result["error"] = str(exc)
            self._progress(usb_id, "error", serial=result.get("serial"), error=str(exc))
        return result

    async def scan(self, cidr: str | None = None, port: int = 5555) -> dict:
        if self.scanning:
            raise RuntimeError("scan already running")
        self.scanning = True
        try:
            if cidr:
                ranges = [ipaddress.ip_network(cidr, strict=False)]
            else:
                ranges = discovery.default_scan_ranges()
            own = {str(i.ip) for i in discovery.local_networks()}
            hosts = [h for net in ranges for h in discovery.hosts_to_scan(net, own)]
            self.bus.publish(ev.SCAN_PROGRESS, None, step="start", hosts=len(hosts),
                             ranges=[str(r) for r in ranges])
            found = await discovery.scan(hosts, port, progress=lambda done, total, n: self.bus.publish(
                ev.SCAN_PROGRESS, None, step="probe", done=done, total=total, found=n))
            identified = []
            for host in found:
                endpoint = f"{host}:{port}"
                state, _ = await provisioning.connect(self.manager.adb, endpoint)
                if state != "connected":
                    continue
                adb = self.manager.adb.for_serial(endpoint)
                try:
                    identity = await provisioning.read_identity(adb)
                    wifi = await read_wifi(adb)
                except AdbError:
                    continue
                self.registry.observe(identity.serial, "scan", ip=host, port=port, wifi=wifi, model=identity.model)
                identified.append({"serial": identity.serial, "ip": host, "model": identity.model})
            self.bus.publish(ev.SCAN_PROGRESS, None, step="done", found=len(found), identified=len(identified))
            return {"ranges": [str(r) for r in ranges], "hosts": len(hosts), "open": found, "devices": identified}
        finally:
            self.scanning = False

    async def all_back_to_usb(self) -> list[str]:
        """End of class: disable Wi-Fi ADB on every connected headset (`adb usb`)."""
        done = []
        for serial, status in list(self.manager.status.items()):
            adb = self.manager.adb_for(serial)
            if adb is None:
                continue
            self.manager.paused.add(serial)
            try:
                await provisioning.back_to_usb(adb)
                if status.endpoint:
                    await provisioning.disconnect(self.manager.adb, status.endpoint)
                done.append(serial)
            except AdbError as exc:
                log.warning("adb usb %s failed: %s", serial, exc)
        return done
