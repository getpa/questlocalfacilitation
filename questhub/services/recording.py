"""Use case: recording sessions (docs/GUI_DESIGN.md §4.7).

recordings/<session>/<alias>_<YYYYmmdd-HHMMSS>[_partNN].mkv plus session.json.
Quality changes are locked while a headset is being recorded. Free disk space
is watched; below the threshold every recording stops.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import shutil
import time

from questhub.config import Settings
from questhub.core import events as ev
from questhub.devices.registry import Registry
from questhub.record.recorder import Recorder
from questhub.services.streaming import StreamingService

log = logging.getLogger(__name__)

LOCK_REASON = "録画中"
DISK_CHECK_INTERVAL = 10.0
PROGRESS_INTERVAL = 5.0


def safe_name(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE).strip("_") or "session"


class RecordingService:
    def __init__(self, streaming: StreamingService, registry: Registry, settings: Settings):
        self.streaming = streaming
        self.registry = registry
        self.settings = settings
        self.bus = streaming.bus
        self.active: dict[str, Recorder] = {}
        self.session: dict | None = None
        self._task: asyncio.Task | None = None
        self._disk_warned = False

    @property
    def root(self) -> Path:
        return Path(self.settings.record_dir).expanduser()

    async def start_task(self) -> None:
        self._task = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        last_progress = 0.0
        while True:
            await asyncio.sleep(DISK_CHECK_INTERVAL if not self.active else 1.0)
            if not self.active:
                continue
            free = self.free_gb()
            limit = self.settings.disk_min_free_gb
            if free is not None and free < limit:
                self.bus.publish(ev.NOTICE, None, level="error",
                                 message=f"ディスクの空きが {free:.1f}GB になったため録画を停止しました")
                await self.stop(None, reason="disk_full")
                continue
            if free is not None and free < limit * 2 and not self._disk_warned:
                self._disk_warned = True
                self.bus.publish(ev.NOTICE, None, level="warning", message=f"ディスクの空きが少なくなっています（{free:.1f}GB）")
            if time.monotonic() - last_progress >= PROGRESS_INTERVAL:
                last_progress = time.monotonic()
                self.bus.publish(ev.RECORDING_PROGRESS, None, recordings=self.status()["active"])

    def free_gb(self) -> float | None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            return shutil.disk_usage(self.root).free / 1e9
        except OSError:
            return None

    async def start(self, serials: list[str] | None, name: str | None = None) -> dict:
        free = self.free_gb()
        if free is not None and free < self.settings.disk_min_free_gb:
            raise RuntimeError(f"ディスクの空きが不足しています（{free:.1f}GB）")
        targets = serials or [s for s, sup in self.streaming.supervisors.items() if sup.running]
        if not targets:
            raise RuntimeError("録画できる端末がありません（表示中の端末がない）")
        if self.session is None:
            stamp = datetime.now()
            label = safe_name(name) if name else stamp.strftime("%Y%m%d-%H%M%S")
            directory = self.root / label
            directory.mkdir(parents=True, exist_ok=True)
            self.session = {"name": label, "dir": str(directory), "started_at": time.time(),
                            "devices": {}, "stopped_at": None}
            self._disk_warned = False
        started = []
        for serial in targets:
            if serial in self.active:
                continue
            sup = self.streaming.supervisor(serial) or self.streaming.start_stream(serial)
            row = self.registry.get(serial) or {}
            alias = safe_name(row.get("alias") or serial)
            path = Path(self.session["dir"]) / f"{alias}_{datetime.now():%Y%m%d-%H%M%S}.mkv"
            recorder = Recorder(path, sup.hub, audio=True)
            recorder.start()
            sup.locked_reason = LOCK_REASON
            self.active[serial] = recorder
            self.session["devices"].setdefault(serial, []).append({
                "alias": row.get("alias"), "ip": row.get("ip"), "ssid": row.get("ssid"),
                "quality": sup.options.quality, "file": str(path), "started_at": time.time()})
            try:
                await sup.request_key_frame()  # start the file now instead of at the next GOP
            except Exception as exc:
                log.debug("key frame request failed: %s", exc)
            started.append(serial)
        self._write_session()
        return {"session": self.session["name"], "started": started}

    async def stop(self, serials: list[str] | None = None, reason: str = "user") -> dict:
        targets = serials or list(self.active)
        stopped = {}
        for serial in targets:
            recorder = self.active.pop(serial, None)
            if recorder is None:
                continue
            stats = await recorder.stop()
            sup = self.streaming.supervisor(serial)
            if sup:
                sup.locked_reason = None
            entry = self.session["devices"][serial][-1] if self.session else {}
            entry.update(stopped_at=time.time(), parts=stats.parts, bytes=stats.bytes,
                         duration_s=round(stats.duration_us / 1e6, 1), video_packets=stats.video_packets,
                         audio_packets=stats.audio_packets, error=stats.error, reason=reason)
            stopped[serial] = entry
            self.bus.publish(ev.RECORDING_STOPPED, serial, **{k: v for k, v in entry.items() if k != "parts"})
        if self.session:
            if not self.active:
                self.session["stopped_at"] = time.time()
            self._write_session()
            if not self.active:
                self.session = None
        return stopped

    def _write_session(self) -> None:
        if not self.session:
            return
        path = Path(self.session["dir"]) / "session.json"
        path.write_text(json.dumps(self.session, indent=2, ensure_ascii=False))

    def status(self) -> dict:
        active = []
        for serial, recorder in self.active.items():
            size = sum(Path(p).stat().st_size for p in recorder.stats.parts if Path(p).exists())
            active.append({"serial": serial, "file": recorder.stats.parts[-1] if recorder.stats.parts else str(recorder.path),
                           "parts": len(recorder.stats.parts), "bytes": size,
                           "elapsed_s": round(time.time() - (recorder.stats.started_at or time.time()), 1),
                           "video_packets": recorder.stats.video_packets, "error": recorder.stats.error})
        return {"session": self.session["name"] if self.session else None, "active": active,
                "free_gb": round(self.free_gb() or 0, 1), "dir": str(self.root)}

    def list_sessions(self, limit: int = 50) -> list[dict]:
        sessions = []
        for path in sorted(self.root.glob("*/session.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            files = [e for entries in data.get("devices", {}).values() for e in entries]
            sessions.append({"name": data.get("name"), "dir": str(path.parent), "started_at": data.get("started_at"),
                             "stopped_at": data.get("stopped_at"), "devices": len(data.get("devices", {})),
                             "bytes": sum(e.get("bytes") or 0 for e in files)})
        return sessions

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
        await self.stop(None, reason="shutdown")
