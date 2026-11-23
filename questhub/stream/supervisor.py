"""One headset's media lifecycle: start, restart with back-off, quality, pause.

Owns the media state (docs/GUI_DESIGN.md §3.4): idle -> waiting (no adb
transport) -> starting -> streaming -> backoff -> starting ... The hub lives as
long as the supervisor, so viewers and recorders survive restarts.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time

from questhub.adb.client import Adb
from questhub.core import events as ev
from questhub.core.events import EventBus
from questhub.devices import quest
from questhub.stream.hub import FrameHub
from questhub.stream.quality import get_preset
from questhub.stream.scrcpy_server import ScrcpyServer, StreamOptions

log = logging.getLogger(__name__)

MIN_BACKOFF = 3.0
MAX_BACKOFF = 30.0


class StreamSupervisor:
    def __init__(self, serial: str, target_fn, bus: EventBus, options: StreamOptions,
                 adb: Adb | None = None, server_factory=None, wake: bool = True, tweaks: bool = False):
        self.serial = serial
        self.target_fn = target_fn            # () -> adb target or None
        self.bus = bus
        self.options = options
        self.adb = adb or Adb()
        self.wake = wake
        self.tweaks = tweaks
        self.hub = FrameHub()
        self.state = "idle"
        self.error: str | None = None
        self.restarts = 0
        self.device_name = ""
        self.locked_reason: str | None = None  # e.g. "recording": quality cannot change
        self._server_factory = server_factory or (lambda target: ScrcpyServer(self.adb.for_serial(target), self.options))
        self._server = None
        self._task: asyncio.Task | None = None
        self._reconfigure = False
        self._paused = asyncio.Event()
        self._paused.set()                     # set = running, clear = paused
        self._backoff = MIN_BACKOFF
        self._streaming = asyncio.Event()
        self.started_at: float | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._set_state("stopped")

    def close(self) -> None:
        self.hub.close()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _set_state(self, state: str) -> None:
        if state != self.state:
            old, self.state = self.state, state
            self.bus.publish(ev.MEDIA_STATE_CHANGED, self.serial, old=old, state=state, error=self.error,
                             quality=self.options.quality)

    async def wait_streaming(self, timeout: float) -> None:
        await asyncio.wait_for(self._streaming.wait(), timeout)

    async def _run(self) -> None:
        while True:
            await self._paused.wait()
            target = self.target_fn()
            if not target:
                self._set_state("waiting")
                await asyncio.sleep(1.0)
                continue
            self._set_state("starting")
            server = self._server_factory(target)
            self._server = server
            try:
                if self.wake:
                    await quest.wake(self.adb.for_serial(target))
                if self.tweaks:
                    await quest.engage_tweaks(self.adb.for_serial(target))
                conn = await server.start()
                self.device_name = conn.device_name
                self.hub.video_codec = conn.video_codec
                self.hub.audio_codec = conn.audio_codec
                self.error = None
                self._backoff = MIN_BACKOFF
                self.started_at = time.monotonic()
                self._set_state("streaming")
                self._streaming.set()
                await server.run(self.hub.publish_video, self.hub.publish_audio)
                raise ConnectionError("stream ended")
            except asyncio.CancelledError:
                await server.stop()
                self._streaming.clear()
                raise
            except Exception as exc:
                self._streaming.clear()
                await server.stop()
                if self._reconfigure or not self._paused.is_set():
                    self._reconfigure = False
                    continue
                self.error = f"{type(exc).__name__}: {exc}"
                self.restarts += 1
                self._set_state("backoff")
                log.warning("%s: %s; restarting in %.0fs", self.serial, self.error, self._backoff)
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    # ------------------------------------------------------------ controls

    async def set_quality(self, name: str) -> None:
        if self.locked_reason:
            raise RuntimeError(f"品質を変更できません（{self.locked_reason}）")
        preset = get_preset(name)
        self.options = dataclasses.replace(self.options, quality=preset.name, max_size=preset.max_size,
                                           max_fps=preset.max_fps, video_bit_rate=preset.video_bit_rate)
        self.bus.publish(ev.QUALITY_CHANGED, self.serial, quality=preset.name)
        if self.state == "streaming" and self._server:
            self._reconfigure = True
            self._streaming.clear()
            self._set_state("reconfiguring")
            await self._server.stop()

    async def pause(self) -> None:
        """Stop capturing (e.g. while a native scrcpy window uses the device)."""
        self._paused.clear()
        self._set_state("paused")
        if self._server:
            self._reconfigure = True
            await self._server.stop()

    def resume(self) -> None:
        self._paused.set()

    async def send_control(self, message: bytes) -> None:
        if not self._server or self.state != "streaming" or not self._server.connection:
            raise RuntimeError("not streaming")
        await self._server.connection.send_control(message)

    async def request_key_frame(self) -> None:
        if self._server and self.state == "streaming":
            await self._server.request_key_frame()

    def status(self) -> dict:
        video, audio = self.hub.video_stats, self.hub.audio_stats
        session = self.hub.session
        return {
            "state": self.state,
            "error": self.error,
            "restarts": self.restarts,
            "quality": self.options.quality,
            "width": session.width if session else None,
            "height": session.height if session else None,
            "video_packets": video.packets,
            "audio_codec": self.hub.audio_codec,
            "viewers": self.hub.subscriber_count,
            "locked": self.locked_reason,
        }
