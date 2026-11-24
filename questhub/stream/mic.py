"""Headset microphone capture for recordings (docs/GUI_DESIGN.md §4.12).

scrcpy captures one audio source per session, and the display session already captures the headset's
output (what the wearer hears). The microphone therefore comes from a second scrcpy session with no video
and no control socket, started only while a headset is being recorded. Its Opus packets go to a FrameHub of
their own; the recorder writes them as a separate track.

Both sessions stamp packets with the headset's monotonic clock (microseconds), so the microphone track
lines up with the video and the headset audio without any extra synchronisation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Callable

from questhub.adb.client import Adb
from questhub.stream.hub import FrameHub
from questhub.stream.scrcpy_server import ScrcpyServer, StreamOptions

log = logging.getLogger(__name__)

MIC_SOURCES = ("mic", "mic-unprocessed", "mic-camcorder", "mic-voice-recognition", "mic-voice-communication")
MAX_BACKOFF = 10.0


def mic_options(source: str = "mic", bit_rate: int = 128_000) -> StreamOptions:
    if source not in MIC_SOURCES:
        raise ValueError(f"unknown microphone source: {source}")
    # The display session owns the device state: this one must not wake the headset, keep it awake or
    # "clean up" (restore) settings the display session changed.
    return StreamOptions(video=False, control=False, audio=True, audio_dup=False, audio_source=source,
                         extra={"audio_bit_rate": str(int(bit_rate)), "stay_awake": "false",
                                "power_on": "false", "cleanup": "false"})


@dataclass
class MicStats:
    restarts: int = 0
    error: str | None = None
    connected: bool = False


class MicCapture:
    """Keep a microphone session running for one headset until stop(); reconnects after a loss."""

    def __init__(self, adb_fn: Callable[[], Adb | None], source: str = "mic", serial: str = "",
                 server_factory=None):
        self.adb_fn = adb_fn
        self.serial = serial
        self.options = mic_options(source)
        self.hub = FrameHub()
        self.stats = MicStats()
        self._server_factory = server_factory or (lambda adb: ScrcpyServer(adb, self.options))
        self._server = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        backoff = 1.0
        first = True
        while True:
            try:
                adb = self.adb_fn()
            except Exception as exc:   # the device left the registry, etc.: keep trying until stop()
                self.stats.error = str(exc) or type(exc).__name__
                adb = None
            if adb is None:
                await asyncio.sleep(backoff)
                continue
            self._server = self._server_factory(adb)
            try:
                conn = await self._server.start()
                if conn.audio_disabled_reason or not conn.audio_codec:
                    raise RuntimeError(conn.audio_disabled_reason or "no microphone stream")
                if not first:
                    self.stats.restarts += 1
                first = False
                self.hub.audio_codec = conn.audio_codec
                self.stats.connected = True
                self.stats.error = None
                backoff = 1.0
                await self._server.run(lambda item: None, self.hub.publish_audio)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.error = str(exc) or type(exc).__name__
                log.info("%s: microphone capture stopped: %s", self.serial, self.stats.error)
            finally:
                self.stats.connected = False
                server, self._server = self._server, None
                if server is not None:
                    await server.stop()
            await asyncio.sleep(backoff)
            backoff = min(MAX_BACKOFF, backoff * 2)

    async def stop(self) -> MicStats:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._server is not None:
            await self._server.stop()
            self._server = None
        self.hub.close()
        return self.stats
