"""Use case: which headsets stream, at what quality; native scrcpy window."""
from __future__ import annotations

import asyncio
import logging

from questhub import paths
from questhub.adb.client import Adb
from questhub.config import Settings
from questhub.core import events as ev
from questhub.devices.manager import DeviceManager
from questhub.devices.registry import Registry
from questhub.stream.quality import DEFAULT_PRESET, PRESETS
from questhub.stream.scrcpy_server import StreamOptions
from questhub.stream.supervisor import StreamSupervisor

log = logging.getLogger(__name__)


class StreamingService:
    def __init__(self, manager: DeviceManager, registry: Registry, settings: Settings,
                 supervisor_factory=None):
        self.manager = manager
        self.registry = registry
        self.settings = settings
        self.bus = manager.bus
        self.supervisors: dict[str, StreamSupervisor] = {}
        self._factory = supervisor_factory or self._make_supervisor
        self._watch: asyncio.Task | None = None
        self._native: dict[str, asyncio.subprocess.Process] = {}
        self.auto_suspended = False   # after "end class": no auto start until streams are started again

    def _options_for(self, serial: str) -> StreamOptions:
        row = self.registry.get(serial) or {}
        quality = row.get("quality") or self.settings.default_quality or DEFAULT_PRESET
        if quality not in PRESETS:
            quality = DEFAULT_PRESET
        return StreamOptions.from_preset(quality, audio=self.settings.audio, audio_dup=self.settings.audio_dup)

    def _make_supervisor(self, serial: str) -> StreamSupervisor:
        return StreamSupervisor(serial, lambda: self.manager.target(serial), self.bus, self._options_for(serial),
                                adb=self.manager.adb, wake=self.settings.wake_on_start,
                                tweaks=self.settings.quest_tweaks)

    async def start(self) -> None:
        events = self.bus.subscribe(ev.CONNECTION_CHANGED)
        self._watch = asyncio.create_task(self._auto(events))

    async def _auto(self, events) -> None:
        async for event in events:
            serial = event.serial
            if not serial or serial.startswith("tsv:"):
                continue
            if self.settings.auto_stream and not self.auto_suspended and event.data.get("state") in ("wifi", "usb"):
                self.start_stream(serial)

    def supervisor(self, serial: str) -> StreamSupervisor | None:
        return self.supervisors.get(serial)

    def start_stream(self, serial: str) -> StreamSupervisor:
        self.auto_suspended = False
        sup = self.supervisors.get(serial)
        if sup is None:
            sup = self.supervisors[serial] = self._factory(serial)
        sup.start()
        return sup

    async def stop_stream(self, serial: str, force: bool = False) -> None:
        """Stop one capture. Refused while it is being recorded (the file would end silently)."""
        sup = self.supervisors.get(serial)
        if sup:
            if sup.locked_reason and not force:
                raise RuntimeError(f"表示を止められません（{sup.locked_reason}）。先に録画を停止してください")
            await sup.stop()

    async def start_all(self) -> list[str]:
        started = []
        for serial, status in self.manager.status.items():
            if status.target and not serial.startswith("tsv:"):
                self.start_stream(serial)
                started.append(serial)
        return started

    async def stop_all(self, suspend_auto: bool = False, force: bool = False) -> None:
        locked = [s for s, sup in self.supervisors.items() if sup.locked_reason]
        if locked and not force:
            raise RuntimeError(f"録画中の端末が {len(locked)} 台あります。先に録画を停止してください")
        self.auto_suspended = self.auto_suspended or suspend_auto
        await asyncio.gather(*(s.stop() for s in self.supervisors.values()))

    async def set_quality(self, serial: str, preset: str) -> None:
        if preset not in PRESETS:
            raise ValueError(f"unknown quality preset {preset!r}")
        sup = self.supervisors.get(serial)
        if sup:
            await sup.set_quality(preset)   # raises while recording
        self.registry.update(serial, quality=preset)

    async def set_quality_all(self, preset: str) -> dict:
        result = {}
        for serial in list(self.supervisors):
            try:
                await self.set_quality(serial, preset)
                result[serial] = "ok"
            except RuntimeError as exc:
                result[serial] = str(exc)
        self.settings.default_quality = preset
        return result

    async def open_native(self, serial: str) -> None:
        """Show one headset in a native scrcpy window (keyboard/mouse control).

        Our capture is paused meanwhile so the headset does not encode twice.
        """
        target = self.manager.target(serial)
        if not target:
            raise RuntimeError("headset is not connected")
        if serial in self._native:
            raise RuntimeError("already open")
        sup = self.supervisors.get(serial)
        if sup and sup.locked_reason:
            raise RuntimeError(f"ネイティブ表示は使えません（{sup.locked_reason}）")
        if sup:
            await sup.pause()
        row = self.registry.get(serial) or {}
        options = sup.options if sup else self._options_for(serial)
        try:
            proc = await asyncio.create_subprocess_exec(
                paths.binary("scrcpy", "SCRCPY_BIN"), f"--serial={target}",
                f"--window-title={row.get('alias') or serial} (Quest Hub)",
                f"--max-size={options.max_size}", f"--max-fps={options.max_fps}",
                f"--video-bit-rate={options.video_bit_rate}", "--no-audio", "--stay-awake",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        except Exception:
            if sup:  # the window never opened: capture must not stay paused
                sup.resume()
            raise
        self._native[serial] = proc

        async def wait_exit():
            await proc.wait()
            self._native.pop(serial, None)
            if sup:
                sup.resume()
            self.bus.publish(ev.NOTICE, serial, message="ネイティブ表示を閉じました")
        asyncio.create_task(wait_exit())

    def native_open(self, serial: str) -> bool:
        return serial in self._native

    async def shutdown(self) -> None:
        if self._watch:
            self._watch.cancel()
        for proc in self._native.values():
            if proc.returncode is None:
                proc.terminate()
        await self.stop_all(force=True)
        if self.settings.quest_tweaks:
            from questhub.devices import quest
            for serial in self.supervisors:
                adb = self.manager.adb_for(serial)
                if adb:
                    await quest.restore_tweaks(adb)
        for sup in self.supervisors.values():
            sup.close()
