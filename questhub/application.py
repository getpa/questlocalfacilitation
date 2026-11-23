"""Composition root: builds every service and wires them together (DI)."""
from __future__ import annotations

import asyncio
from collections import Counter
import logging
import os
from pathlib import Path
import socket

from questhub import paths
from questhub.adb import client as adb_client
from questhub.adb.client import Adb
from questhub.config import Settings
from questhub.core.events import EventBus
from questhub.devices import discovery
from questhub.devices.manager import DeviceManager
from questhub.devices.registry import Registry
from questhub.services.input import InputService
from questhub.services.provisioning import ProvisioningService
from questhub.services.recording import RecordingService
from questhub.services.streaming import StreamingService
from questhub.services.telemetry import TelemetryService
from questhub.stream.quality import PRESETS
from questhub.web.auth import ViewerAuth, new_token
from questhub.web.tls import LocalCA

log = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """One Quest Hub per Mac; the CLI launcher warns when this lock is held."""

    def __init__(self, path: Path):
        self.path = path

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip())
                os.kill(pid, 0)
                if pid != os.getpid():
                    raise AlreadyRunning(f"Quest Hub is already running (pid {pid})")
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        self.path.write_text(str(os.getpid()))

    def release(self) -> None:
        try:
            if self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


class Application:
    def __init__(self, data_dir: str | Path | None = None, settings: Settings | None = None,
                 adb: Adb | None = None, track_source=None, auto_reconnect: bool = True,
                 supervisor_factory=None, start_background: bool = True):
        self.data_dir = Path(data_dir or paths.DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data_dir / "settings.json"
        self.settings = settings or Settings.load(self.settings_path)
        adb_client.set_concurrency(self.settings.adb_concurrency)
        self.bus = EventBus()
        self.registry = Registry(self.data_dir / "devices.sqlite", self.bus)
        self.adb = adb or Adb()
        self.manager = DeviceManager(self.registry, self.bus, self.adb, self.settings.reconnect_max_backoff,
                                     track_source=track_source, auto_reconnect=auto_reconnect)
        self.provisioning = ProvisioningService(self.manager, self.registry)
        self.telemetry = TelemetryService(self.manager, self.registry, self.settings.telemetry_interval)
        self.streaming = StreamingService(self.manager, self.registry, self.settings, supervisor_factory,
                                          diag_dir=self.data_dir / "diagnostics")
        self.recording = RecordingService(self.streaming, self.registry, self.settings)
        self.input = InputService(self.streaming)
        self.ca = LocalCA(self.data_dir / "tls")
        self.viewer_auth = ViewerAuth()
        self.admin_token = new_token()
        self.lock = InstanceLock(self.data_dir / "questhub.lock")
        self.start_background = start_background
        self.servers = None  # set by questhub.web.servers

    async def start(self) -> None:
        self.registry.start_run()
        await self.manager.start()
        await self.streaming.start()
        await self.recording.start_task()
        if self.start_background:
            await self.telemetry.start()

    async def stop(self) -> None:
        await self.recording.shutdown()
        await self.streaming.shutdown()
        await self.telemetry.stop()
        await self.manager.stop()
        self.registry.close()

    def save_settings(self) -> None:
        self.settings.save(self.settings_path)
        self.telemetry.interval = self.settings.telemetry_interval
        self.streaming.apply_settings()

    # ------------------------------------------------------------ read models for the UI

    def devices(self) -> list[dict]:
        rows = {r["serial"]: r for r in self.registry.list()}
        for serial in self.manager.status:
            rows.setdefault(serial, {"serial": serial})
        result = []
        for serial, row in rows.items():
            status = self.manager.status.get(serial)
            sup = self.streaming.supervisor(serial)
            result.append({
                **row,
                "connection": status.to_dict() if status else {"state": "offline", "target": None},
                "stream": sup.status() if sup else {"state": "idle", "quality": row.get("quality")},
                "recording": serial in self.recording.active,
                "native": self.streaming.native_open(serial),
                "placeholder": serial.startswith("tsv:"),
            })
        return result

    def viewer_devices(self) -> list[dict]:
        """What viewers may see: no IP / SSID / BSSID (docs §4.8)."""
        out = []
        for d in self.devices():
            if d["placeholder"]:
                continue
            stream = d["stream"]
            out.append({"serial": d["serial"], "alias": d.get("alias") or d["serial"],
                        "grid_col": d.get("grid_col"), "grid_row": d.get("grid_row"),
                        "state": stream.get("state"), "quality": stream.get("quality"),
                        "width": stream.get("width"), "height": stream.get("height"),
                        "recording": d["recording"], "black": bool(stream.get("black"))})
        return out

    def network_summary(self) -> dict:
        devices = [d for d in self.registry.list() if not d["serial"].startswith("tsv:")]
        return {
            "ssids": Counter(d["ssid"] or "（不明）" for d in devices if d["wifi_seen_at"]),
            "bssids": Counter(d["bssid"] or "（不明）" for d in devices if d["wifi_seen_at"]),
            "bands": Counter(d["band"] or "（不明）" for d in devices if d["wifi_seen_at"]),
            "weak": [d["serial"] for d in devices if d["rssi"] is not None and d["rssi"] < -70],
            "slow_band": [d["serial"] for d in devices if d["band"] == "2.4GHz"],
            "warnings": self.registry.warnings(),
        }

    def presets(self) -> list[dict]:
        return [p.to_dict() for p in PRESETS.values()]

    # ------------------------------------------------------------ viewer addresses

    @staticmethod
    def lan_addresses() -> list[str]:
        ips = [str(i.ip) for i in discovery.local_networks()]
        return ips or ["127.0.0.1"]

    @staticmethod
    def hostname() -> str:
        name = socket.gethostname().split(".")[0]
        return f"{name}.local"

    def viewer_urls(self) -> list[str]:
        scheme = "http" if self.settings.viewer_insecure_http else "https"
        port = self.settings.viewer_port
        return [f"{scheme}://{ip}:{port}/" for ip in self.lan_addresses()] + \
               [f"{scheme}://{self.hostname()}:{port}/"]

    async def end_session(self, back_to_usb: bool) -> dict:
        stopped = await self.recording.stop(None, reason="end_session")
        if self.servers:
            await self.servers.set_viewer(False)
        self.settings.viewer_enabled = False
        self.save_settings()
        self.viewer_auth.rotate()
        await self.streaming.stop_all(suspend_auto=True)
        usb = await self.provisioning.all_back_to_usb() if back_to_usb else []
        return {"recordings_stopped": list(stopped), "back_to_usb": usb}


async def run_forever(app: Application) -> None:
    await asyncio.Event().wait()
