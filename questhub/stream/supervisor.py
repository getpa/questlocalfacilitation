"""One headset's media lifecycle: start, restart with back-off, quality, pause.

Owns the media state (docs/GUI_DESIGN.md §3.4): idle -> waiting (no adb
transport) -> starting -> streaming -> backoff -> starting ... The hub lives as
long as the supervisor, so viewers and recorders survive restarts.
"""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime
import logging
from pathlib import Path
import time

from questhub.adb.client import Adb
from questhub.core import events as ev
from questhub.core.events import EventBus
from questhub.devices import quest
from questhub.stream import blackscreen
from questhub.stream.hub import FrameHub
from questhub.stream.protocol import MediaPacket
from questhub.stream.quality import get_preset
from questhub.stream.scrcpy_server import ScrcpyServer, StreamOptions

log = logging.getLogger(__name__)

MIN_BACKOFF = 3.0
MAX_BACKOFF = 30.0


class StreamSupervisor:
    def __init__(self, serial: str, target_fn, bus: EventBus, options: StreamOptions,
                 adb: Adb | None = None, server_factory=None, wake: bool = True, tweaks: bool = False,
                 black_detection: bool = False, black_recovery: bool = True, prox_nudge: bool = True,
                 diag_dir: str | Path | None = None, probe_factory=None):
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
        # Black capture handling (blackscreen.py)
        self.black_detection = black_detection
        self.black_recovery = black_recovery
        self.prox_nudge = prox_nudge
        self.diag_dir = Path(diag_dir) if diag_dir else None
        self._probe_factory = probe_factory or blackscreen.KeyFrameProbe
        self._probe = None
        self._probing = False
        self._last_probe = 0.0
        self.black = blackscreen.BlackState()
        self.black_events = 0
        self.black_cause: str | None = None
        self.black_diag: dict | None = None
        self.black_diag_file: str | None = None
        self.last_recovery: str | None = None
        self._recovery_task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._cancel_recovery()
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
                await server.run(self._on_video, self.hub.publish_audio)
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

    # ------------------------------------------------------------ black capture

    def _on_video(self, item) -> None:
        self.hub.publish_video(item)
        if (self.black_detection and isinstance(item, MediaPacket) and item.key_frame
                and self.hub.video_config and not self._probing
                and time.monotonic() - self._last_probe >= 1.0):
            self._probing = True
            self._last_probe = time.monotonic()
            asyncio.get_running_loop().create_task(self._measure(self.hub.video_config.data, item.data))

    async def _measure(self, config: bytes, key_frame: bytes) -> None:
        try:
            if self._probe is None:
                self._probe = self._probe_factory()
            luma = await asyncio.to_thread(self._probe.measure, config, key_frame)
        except Exception as exc:  # detection must never disturb streaming
            log.debug("black probe failed: %s", exc)
            luma = None
        finally:
            self._probing = False
        if luma is None:
            return
        change = self.black.update(luma)
        if change == "black":
            self.black_events += 1
            self.last_recovery = None
            self.bus.publish(ev.MEDIA_BLACK, self.serial, black=True, luma=round(luma.mean, 1))
            asyncio.get_running_loop().create_task(self._diagnose())
            if self.black_recovery:
                self._recovery_task = asyncio.get_running_loop().create_task(self._recover())
        elif change == "visible":
            self._cancel_recovery()
            self.bus.publish(ev.MEDIA_BLACK, self.serial, black=False, recovered_by=self.last_recovery)

    def _cancel_recovery(self) -> None:
        if self._recovery_task and not self._recovery_task.done():
            self._recovery_task.cancel()
        self._recovery_task = None

    async def _recover(self) -> None:
        while self.black.black:
            started = time.monotonic()
            for delay, step in blackscreen.RECOVERY_STEPS:
                await asyncio.sleep(max(0.0, started + delay - time.monotonic()))
                if not self.black.black:
                    return
                try:
                    await self._recovery_step(step)
                    self.last_recovery = step
                    self.bus.publish(ev.MEDIA_BLACK, self.serial, black=True, recovery=step)
                except Exception as exc:
                    log.warning("%s: recovery %s failed: %s", self.serial, step, exc)
            await asyncio.sleep(max(0.0, started + blackscreen.RECOVERY_REPEAT - time.monotonic()))

    async def _recovery_step(self, step: str) -> None:
        target = self.target_fn()
        adb = self.adb.for_serial(target) if target else None
        if step == "reset_video":
            await self.request_key_frame()
        elif step == "wake" and adb:
            await quest.wake(adb)
            if self.prox_nudge:
                await adb.shell("am broadcast -a com.oculus.vrpowermanager.prox_close", check=False)
        elif step == "restart" and self._server and self.state == "streaming":
            self._reconfigure = True
            self._streaming.clear()
            await self._server.stop()

    async def diagnose(self) -> dict:
        """Manual snapshot (e.g. the operator sees black that detection missed)."""
        await self._diagnose(manual=True)
        return {"cause": self.black_cause, "summary": self.black_diag, "file": self.black_diag_file}

    async def _diagnose(self, manual: bool = False) -> None:
        """Snapshot the compositor state while black, to tell the causes apart."""
        target = self.target_fn()
        if not target:
            return
        adb = self.adb.for_serial(target)
        sections = {}
        for name, command in blackscreen.DIAG_COMMANDS:
            try:
                sections[name] = await adb.shell(command, timeout=15, check=False)
            except Exception as exc:
                sections[name] = f"<failed: {exc}>"
        summary = blackscreen.summarize_diagnostics(sections)
        cause = blackscreen.likely_cause(summary)
        self.black_diag, self.black_cause = summary, cause
        path = None
        if self.diag_dir:
            self.diag_dir.mkdir(parents=True, exist_ok=True)
            kind = "manual" if manual else "black"
            path = self.diag_dir / f"{kind}_{self.serial}_{datetime.now():%Y%m%d-%H%M%S}.txt"
            body = [f"serial: {self.serial}", f"quality: {self.options.quality}", f"luma: {self.black.last_luma}",
                    f"likely cause: {cause}", f"summary: {summary}", ""]
            for name, text in sections.items():
                body += [f"===== {name}", text.rstrip(), ""]
            path.write_text("\n".join(body))
        self.black_diag_file = str(path) if path else None
        if not manual:
            self.bus.publish(ev.MEDIA_BLACK, self.serial, black=True, cause=cause, diagnostics=summary,
                             file=self.black_diag_file)

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
            "black": self.black.black,
            "black_s": round(time.monotonic() - self.black.since, 1) if self.black.black and self.black.since else None,
            "black_cause": self.black_cause if self.black.black else None,
            "black_events": self.black_events,
            "last_recovery": self.last_recovery,
            "luma": round(self.black.last_luma.mean, 1) if self.black.last_luma else None,
        }
