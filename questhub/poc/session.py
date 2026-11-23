"""PoC device session: one headset -> FrameHub -> (web viewers, recorder).

A deliberately small stand-in for services/streaming + stream/supervisor
(docs/GUI_DESIGN.md §3.3); it restarts the server after a failure.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from pathlib import Path
import re
import time

from questhub.adb.client import Adb
from questhub.devices.telemetry import read_wifi
from questhub.devices.wifi_parse import WifiInfo
from questhub.record.recorder import Recorder
from questhub.stream.hub import FrameHub
from questhub.stream.scrcpy_server import ScrcpyServer, StreamOptions

log = logging.getLogger(__name__)

RETRY_DELAY = 3.0


class DeviceSession:
    def __init__(self, serial: str, options: StreamOptions | None = None,
                 record_dir: str | Path = "recordings/poc", adb: Adb | None = None,
                 server_factory=None):
        self.serial = serial
        self.adb = adb or Adb(serial=serial)
        self.options = options or StreamOptions()
        self.record_dir = Path(record_dir)
        self.hub = FrameHub()
        self.wifi: WifiInfo | None = None
        self.device_name = ""
        self.state = "idle"
        self.error: str | None = None
        self.restarts = 0
        self.recorder: Recorder | None = None
        self.last_recording: dict | None = None
        self._server_factory = server_factory or (lambda: ScrcpyServer(self.adb, self.options))
        self._server: ScrcpyServer | None = None
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()

    async def start(self, wait: float | None = 20.0) -> None:
        if self._task:
            return
        try:
            self.wifi = await read_wifi(self.adb)
        except Exception as exc:  # telemetry must never block streaming
            log.warning("wifi read failed: %s", exc)
        self._task = asyncio.create_task(self._supervise())
        if wait:
            await asyncio.wait_for(self._connected.wait(), wait)

    async def _supervise(self) -> None:
        while True:
            self.state = "starting"
            server = self._server_factory()
            self._server = server
            try:
                conn = await server.start()
                self.device_name = conn.device_name
                self.hub.video_codec = conn.video_codec
                self.hub.audio_codec = conn.audio_codec
                self.state = "streaming"
                self.error = None
                self._connected.set()
                await server.run(self.hub.publish_video, self.hub.publish_audio)
                raise ConnectionError("stream ended")
            except asyncio.CancelledError:
                await server.stop()
                raise
            except Exception as exc:
                self.state = "backoff"
                self.error = f"{type(exc).__name__}: {exc}"
                self.restarts += 1
                log.warning("%s: %s; restarting in %.0fs", self.serial, self.error, RETRY_DELAY)
                await server.stop()
                await asyncio.sleep(RETRY_DELAY)
                if ":" in self.serial:  # Wi-Fi ADB may have dropped with the stream
                    try:
                        await Adb(binary=self.adb.binary).run("connect", self.serial, timeout=10, check=False)
                    except Exception as reconnect_error:
                        log.warning("adb connect %s failed: %s", self.serial, reconnect_error)

    async def request_key_frame(self) -> None:
        if self._server and self.state == "streaming":
            await self._server.request_key_frame()

    async def start_recording(self, path: str | Path | None = None) -> str:
        if self.recorder and self.recorder.running:
            raise RuntimeError("already recording")
        if path is None:
            alias = re.sub(r"[^A-Za-z0-9_-]+", "_", self.device_name or self.serial)
            path = self.record_dir / f"{alias}_{datetime.now():%Y%m%d-%H%M%S}.mkv"
        self.recorder = Recorder(path, self.hub, audio=True)
        self.recorder.start()
        # The file starts at a key frame; ask for one instead of waiting up to a GOP.
        try:
            await self.request_key_frame()
        except Exception as exc:
            log.warning("key frame request failed: %s", exc)
        return str(path)

    async def stop_recording(self) -> dict | None:
        if not self.recorder:
            return None
        stats = await self.recorder.stop()
        self.last_recording = stats.__dict__.copy()
        self.recorder = None
        return self.last_recording

    def status(self) -> dict:
        video, audio = self.hub.video_stats, self.hub.audio_stats
        now = time.monotonic()
        return {
            "serial": self.serial,
            "device_name": self.device_name,
            "state": self.state,
            "error": self.error,
            "restarts": self.restarts,
            "wifi": self.wifi.to_dict() if self.wifi else None,
            "video": {
                "codec": self.hub.video_codec,
                "width": self.hub.session.width if self.hub.session else None,
                "height": self.hub.session.height if self.hub.session else None,
                "packets": video.packets,
                "key_frames": video.key_frames,
                "bytes": video.bytes,
                "idle_s": round(now - video.last_at, 1) if video.last_at else None,
            },
            "audio": {
                "codec": self.hub.audio_codec,
                "packets": audio.packets,
                "bytes": audio.bytes,
            },
            "viewers": self.hub.subscriber_count - (1 if self.recorder else 0),
            "recording": self.recorder.stats.__dict__ if self.recorder else None,
            "last_recording": self.last_recording,
        }

    async def stop(self) -> None:
        if self.recorder:
            await self.stop_recording()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.hub.close()
        self.state = "stopped"
