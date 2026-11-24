"""Record one device's hub to Matroska without re-encoding (PyAV).

The file starts at the first key frame after start(). Video and audio share
the device's monotonic clock, so both are offset by that key frame's PTS.
An optional microphone hub (a second scrcpy session, same clock) becomes a
third track. Matroska stays playable if the process dies mid-recording.
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from fractions import Fraction
import logging
from pathlib import Path
import queue
import threading
import time

import av
import numpy as np

from questhub.stream import hub as hubmod
from questhub.stream.hub import FrameHub

log = logging.getLogger(__name__)

MICROSECONDS = Fraction(1, 1_000_000)
OPUS_SAMPLE_RATE = 48_000
AUDIO_WAIT_S = 1.0  # how long the first key frame may wait for the audio config
MIC_WAIT_S = 6.0    # ... and for the microphone session, which starts with the recording

# Microphone events are tagged so they never mix with the headset audio of the display session.
MIC_CONFIG = "mic_config"
MIC = "mic"


_TIMEOUT = hubmod.Event("timeout")


@dataclass
class RecordingStats:
    path: str
    video_packets: int = 0
    audio_packets: int = 0
    mic_packets: int = 0
    mic_track: bool = False
    bytes: int = 0
    started_at: float | None = None
    stopped_at: float | None = None
    duration_us: int = 0
    error: str | None = None
    parts: list = field(default_factory=list)   # files written (a new part when resolution/SPS changes)


class Recorder:
    def __init__(self, path: str | Path, hub: FrameHub, audio: bool = True, mic: FrameHub | None = None):
        self.path = Path(path)
        self.hub = hub
        self.audio = audio and hub.audio_codec is not None
        self.mic = mic
        self.stats = RecordingStats(path=str(self.path))
        self._queue: queue.Queue = queue.Queue(maxsize=2048)
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task | None = None
        self._sub = None
        self._mic_sub = None
        self._mic_task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread:
            raise RuntimeError("already started")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sub = self.hub.subscribe(video=True, audio=self.audio, maxsize=4096)
        if self.mic is not None:
            self._mic_sub = self.mic.subscribe(video=False, audio=True, maxsize=4096)
        self._thread = threading.Thread(target=self._mux_loop, name=f"recorder:{self.path.name}", daemon=True)
        self._thread.start()
        self._task = asyncio.create_task(self._forward())
        if self._mic_sub is not None:
            self._mic_task = asyncio.create_task(self._forward_mic())
        self.stats.started_at = time.time()

    async def _forward(self) -> None:
        try:
            async for event in self._sub:
                await asyncio.to_thread(self._queue.put, event)
        finally:
            await asyncio.to_thread(self._queue.put, None)

    async def _forward_mic(self) -> None:
        async for event in self._mic_sub:
            if event.kind == hubmod.AUDIO_CONFIG:
                event = hubmod.Event(MIC_CONFIG, packet=event.packet)
            elif event.kind == hubmod.AUDIO:
                event = hubmod.Event(MIC, packet=event.packet)
            else:
                continue
            await asyncio.to_thread(self._queue.put, event)

    async def stop(self) -> RecordingStats:
        if self._mic_sub:
            self._mic_sub.close()
        if self._mic_task:
            await asyncio.gather(self._mic_task, return_exceptions=True)
        if self._sub:
            self._sub.close()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
        if self._thread:
            await asyncio.to_thread(self._thread.join, 15)
        self.stats.stopped_at = time.time()
        return self.stats

    # ---------------------------------------------------------------- thread

    def part_path(self, number: int) -> Path:
        if number <= 1:
            return self.path
        return self.path.with_name(f"{self.path.stem}_part{number:02d}{self.path.suffix}")

    def _mux_loop(self) -> None:
        part = 0
        written_bytes = 0
        container = template = None
        vstream = astream = mstream = None
        session = video_config = audio_config = mic_config = None
        record_mic = self.mic is not None
        base = None
        pending: list = []  # events from the first key frame while waiting for the audio config
        last_pts = {"v": -1, "a": -1, "m": -1}
        record_audio = self.audio and self.hub.audio_codec == "opus"
        if self.audio and not record_audio:
            log.warning("audio %s not recorded (only Opus is supported)", self.hub.audio_codec)

        def open_container():
            nonlocal container, template, vstream, astream, mstream, part
            part += 1
            path = self.part_path(part)
            self.stats.parts.append(str(path))
            container = av.open(str(path), "w", format="matroska")
            # PyAV cannot set extradata on a codec-less mux stream, but a stream
            # created from a template keeps an unopened codec context.
            template = av.open(io.BytesIO(video_config + pending[0].packet.data), format="h264")
            vstream = container.add_stream_from_template(template.streams.video[0])
            vstream.time_base = MICROSECONDS
            vstream.codec_context.extradata = video_config  # Annex B; Matroska stores it as avcC
            if record_audio and audio_config:
                astream = container.add_stream_from_template(_opus_template())
                astream.time_base = MICROSECONDS
                astream.codec_context.extradata = audio_config  # the device's OpusHead
            elif record_audio:
                log.warning("no audio config within %.1fs; recording video only", AUDIO_WAIT_S)
            if astream is not None:
                astream.metadata["title"] = "Headset audio"
            if record_mic and mic_config:
                mstream = container.add_stream_from_template(_opus_template())
                mstream.time_base = MICROSECONDS
                mstream.codec_context.extradata = mic_config
                mstream.metadata["title"] = "Microphone"
                self.stats.mic_track = True
            elif record_mic:
                log.warning("no microphone within %.1fs; recording without it", MIC_WAIT_S)

        def write(event) -> None:
            packet = event.packet
            if event.kind == hubmod.VIDEO:
                self._mux(container, vstream, packet, base, last_pts, "v")
                self.stats.video_packets += 1
            elif event.kind == hubmod.AUDIO and astream is not None and packet.pts is not None and packet.pts >= base:
                self._mux(container, astream, packet, base, last_pts, "a")
                self.stats.audio_packets += 1
            elif event.kind == MIC and mstream is not None and packet.pts is not None and packet.pts >= base:
                self._mux(container, mstream, packet, base, last_pts, "m")
                self.stats.mic_packets += 1

        def close_container():
            nonlocal container, template, vstream, astream, mstream, base, written_bytes
            if container is not None:
                container.close()
                written_bytes += self.part_path(part).stat().st_size
                self.stats.duration_us += max(last_pts["v"], 0)
            if template is not None:
                template.close()
            container = template = vstream = astream = mstream = None
            base = None
            last_pts["v"] = last_pts["a"] = last_pts["m"] = -1

        try:
            pending_since = None
            while True:
                timeout = None
                if pending_since is not None:
                    wait = MIC_WAIT_S if record_mic and mic_config is None else AUDIO_WAIT_S
                    timeout = max(0.0, pending_since + wait - time.monotonic())
                try:
                    event = self._queue.get(timeout=timeout)
                except queue.Empty:
                    event = _TIMEOUT
                if event is None:
                    if container is None and pending:
                        # stopped while waiting for the (microphone) audio config: keep what was buffered
                        base = pending[0].packet.pts
                        open_container()
                        for queued in pending:
                            write(queued)
                        pending = []
                    break
                kind = event.kind
                if container is not None and (
                        (kind == hubmod.SESSION and event.session != session) or
                        (kind == hubmod.VIDEO_CONFIG and event.packet.data != video_config)):
                    # Resolution or encoder parameters changed (e.g. quality change):
                    # Matroska cannot switch mid-file, so continue in a new part.
                    close_container()
                if kind == hubmod.SESSION:
                    session = event.session
                elif kind == hubmod.VIDEO_CONFIG:
                    video_config = event.packet.data
                elif kind == hubmod.AUDIO_CONFIG:
                    audio_config = event.packet.data
                elif kind == MIC_CONFIG:
                    mic_config = mic_config or event.packet.data   # a reconnect repeats the same OpusHead
                elif kind == MIC and container is None and not pending:
                    pass   # before the file starts
                elif container is not None:
                    write(event)
                    continue
                elif kind == _TIMEOUT.kind:
                    pass
                elif pending or (kind == hubmod.VIDEO and event.packet.key_frame and session and video_config):
                    pending.append(event)
                    if pending_since is None:
                        pending_since = time.monotonic()
                if container is None and pending:
                    waited = time.monotonic() - pending_since
                    if record_audio and audio_config is None and waited < AUDIO_WAIT_S:
                        continue
                    if record_mic and mic_config is None and waited < MIC_WAIT_S:
                        continue
                    pending_since = None
                    base = pending[0].packet.pts
                    open_container()
                    for queued in pending:
                        write(queued)
                    pending = []
        except Exception as exc:  # keep the process alive; report via stats
            log.exception("recording %s failed", self.path)
            self.stats.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                close_container()
            except Exception as exc:  # finalising the last file must not hide the earlier error
                self.stats.error = self.stats.error or f"{type(exc).__name__}: {exc}"
            self.stats.bytes = written_bytes

    @staticmethod
    def _mux(container, stream, packet, base, last_pts, key) -> None:
        pts = packet.pts - base
        if pts <= last_pts[key]:  # encoder restart (e.g. RESET_VIDEO) must not go backwards
            pts = last_pts[key] + 1
        last_pts[key] = pts
        out = av.Packet(packet.data)
        out.stream = stream
        out.time_base = MICROSECONDS
        out.pts = out.dts = pts
        out.is_keyframe = packet.key_frame
        container.mux(out)


_templates: dict[str, object] = {}


def _opus_template():
    if "opus" not in _templates:
        buffer = io.BytesIO()
        with av.open(buffer, "w", format="ogg") as out:
            stream = out.add_stream("libopus", rate=OPUS_SAMPLE_RATE, layout="stereo")
            frame = av.AudioFrame.from_ndarray(np.zeros((1, 960 * 2), np.int16), format="s16", layout="stereo")
            frame.sample_rate = OPUS_SAMPLE_RATE
            frame.pts = 0
            for pkt in [*stream.encode(frame), *stream.encode(None)]:
                out.mux(pkt)
        buffer.seek(0)
        container = av.open(buffer, format="ogg")
        _templates["opus"] = container
    return _templates["opus"].streams.audio[0]
