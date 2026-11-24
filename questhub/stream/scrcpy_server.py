"""One scrcpy-server session: push, start, forward, and read the 3 sockets.

This module does not retry. Restarts and back-off belong to the stream
supervisor (docs/GUI_DESIGN.md §3.3).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import logging
import random
import socket
import time
from typing import Optional, Union

from questhub import paths
from questhub.adb.client import Adb
from questhub.stream import protocol
from questhub.stream.quality import DEFAULT_PRESET, get_preset
from questhub.stream.protocol import MediaPacket, ProtocolError, SessionPacket, StreamDisabled

log = logging.getLogger(__name__)

SERVER_VERSION = "5.0.1"
REMOTE_JAR = "/data/local/tmp/scrcpy-server.jar"

# typing.Union keeps these runtime aliases importable on Python < 3.10.
VideoHandler = Callable[[Union[SessionPacket, MediaPacket]], Optional[Awaitable[None]]]
AudioHandler = Callable[[MediaPacket], Optional[Awaitable[None]]]


@dataclass
class StreamOptions:
    quality: str = DEFAULT_PRESET  # name of the preset the values came from ("custom" if edited)
    max_size: int = 1280
    max_fps: int = 30
    video_bit_rate: int = 4_000_000
    key_frame_interval: float = 1.0  # seconds; scrcpy's default is 10
    audio: bool = True
    audio_dup: bool = True
    control: bool = True
    video: bool = True
    audio_source: str | None = None   # None: headset output (playback/output); e.g. "mic" for the microphone
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_preset(cls, name: str, **overrides) -> "StreamOptions":
        preset = get_preset(name)
        values = {"quality": preset.name, "max_size": preset.max_size, "max_fps": preset.max_fps,
                  "video_bit_rate": preset.video_bit_rate}
        changed = {k: v for k, v in overrides.items() if v is not None}
        if any(k in changed and changed[k] != values[k] for k in ("max_size", "max_fps", "video_bit_rate")):
            values["quality"] = "custom"
        values.update(changed)
        return cls(**values)

    def server_args(self, scid: int) -> list[str]:
        args = {
            "scid": f"{scid:08x}",
            "log_level": "info",
            "tunnel_forward": "true",
            "video": str(self.video).lower(),
            "audio": str(self.audio).lower(),
            "control": str(self.control).lower(),
            "cleanup": "true",
            "max_size": str(self.max_size),
            "max_fps": str(self.max_fps),
            "video_bit_rate": str(self.video_bit_rate),
            "stay_awake": "true",
            "clipboard_autosync": "false",
            "power_on": "true",
        }
        interval = self.key_frame_interval
        # CodecOption syntax "key[:type]=value"; float keeps sub-second intervals possible.
        args["video_codec_options"] = (
            f"i-frame-interval={int(interval)}" if float(interval).is_integer()
            else f"i-frame-interval:float={interval}")
        if self.audio and self.audio_source:
            args["audio_source"] = self.audio_source
        elif self.audio:
            args["audio_source"] = "playback" if self.audio_dup else "output"
            if self.audio_dup:
                args["audio_dup"] = "true"
        args.update(self.extra)
        return [f"{key}={value}" for key, value in args.items()]


async def _read_exact(reader: asyncio.StreamReader, size: int) -> bytes:
    try:
        return await reader.readexactly(size)
    except asyncio.IncompleteReadError as exc:
        raise ConnectionError("stream closed") from exc


async def read_packets(reader: asyncio.StreamReader, allow_session: bool):
    """Yield SessionPacket / MediaPacket until the socket closes."""
    while True:
        try:
            raw = await reader.readexactly(protocol.HEADER_LENGTH)
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                raise ConnectionError("stream closed mid-header") from exc
            return
        header = protocol.parse_header(raw)
        if header.session:
            if not allow_session:
                raise ProtocolError("session packet on a non-video stream")
            yield SessionPacket(header.width, header.height, header.client_resized)
            continue
        data = await _read_exact(reader, header.size)
        yield MediaPacket(data=data, pts=header.pts, config=header.config, key_frame=header.key_frame)


class ScrcpyConnection:
    """Client side of the forward tunnel (works against a real or fake server)."""

    def __init__(self, video: bool = True, audio: bool = True, control: bool = True):
        self.want_video, self.want_audio, self.want_control = video, audio, control
        self.device_name = ""
        self.video_codec: str | None = None
        self.audio_codec: str | None = None
        self.audio_disabled_reason: str | None = None
        self._streams: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}

    async def open(self, host: str, port: int, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        order = [name for name, wanted in
                 (("video", self.want_video), ("audio", self.want_audio), ("control", self.want_control)) if wanted]
        if not order:
            raise ValueError("at least one socket is required")
        first = order[0]
        # adb accepts the TCP connection before the device listens; the dummy
        # byte proves that the server side is really there.
        while True:
            reader, writer = await asyncio.open_connection(host, port)
            try:
                dummy = await asyncio.wait_for(reader.readexactly(1), max(0.1, deadline - time.monotonic()))
                if dummy != b"\0":
                    raise ProtocolError(f"unexpected dummy byte {dummy!r}")
                break
            except (asyncio.IncompleteReadError, ConnectionError):
                writer.close()
                if time.monotonic() > deadline:
                    raise TimeoutError("scrcpy server did not accept the connection") from None
                await asyncio.sleep(0.1)
            except asyncio.TimeoutError:
                writer.close()
                raise TimeoutError("scrcpy server did not accept the connection") from None
        self._streams[first] = (reader, writer)
        for name in order[1:]:
            self._streams[name] = await asyncio.open_connection(host, port)

        first_reader = self._streams[first][0]
        self.device_name = protocol.parse_device_name(
            await asyncio.wait_for(_read_exact(first_reader, protocol.DEVICE_NAME_LENGTH), timeout))
        if self.want_video:
            self.video_codec = protocol.parse_codec_id(await _read_exact(self._streams["video"][0], 4))
        if self.want_audio:
            try:
                self.audio_codec = protocol.parse_codec_id(await _read_exact(self._streams["audio"][0], 4))
            except StreamDisabled as exc:
                self.audio_disabled_reason = str(exc)
                _, writer = self._streams.pop("audio")
                writer.close()

    def reader(self, name: str) -> asyncio.StreamReader | None:
        stream = self._streams.get(name)
        return stream[0] if stream else None

    async def send_control(self, message: bytes) -> None:
        stream = self._streams.get("control")
        if not stream:
            raise RuntimeError("control socket not open")
        stream[1].write(message)
        await stream[1].drain()

    async def drain_control(self) -> None:
        """Discard device messages (clipboard, ack) so the socket never blocks."""
        reader = self.reader("control")
        if reader is None:
            return
        while await reader.read(4096):
            pass

    async def close(self) -> None:
        for _, writer in self._streams.values():
            writer.close()
        for _, writer in self._streams.values():
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._streams.clear()


def _free_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _call(handler, item) -> None:
    result = handler(item)
    if asyncio.iscoroutine(result):
        await result


class ScrcpyServer:
    """Run scrcpy-server on one device and dispatch its packets."""

    def __init__(self, adb: Adb, options: StreamOptions | None = None, jar: str | None = None):
        self.adb = adb
        self.options = options or StreamOptions()
        self.jar = jar or str(paths.scrcpy_server_jar())
        self.scid = random.randrange(0, 0x7FFFFFFF)
        self.port: int | None = None
        self.connection: ScrcpyConnection | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._log_task: asyncio.Task | None = None
        self.server_log: list[str] = []

    @property
    def socket_name(self) -> str:
        return f"scrcpy_{self.scid:08x}"

    async def push_if_needed(self) -> bool:
        with open(self.jar, "rb") as f:
            local = hashlib.sha256(f.read()).hexdigest()
        remote = await self.adb.shell(f"sha256sum {REMOTE_JAR} 2>/dev/null", check=False)
        if remote.split()[:1] == [local]:
            return False
        await self.adb.run("push", self.jar, REMOTE_JAR, timeout=60)
        return True

    async def start(self, timeout: float = 15.0) -> ScrcpyConnection:
        await self.push_if_needed()
        self.port = _free_local_port()
        await self.adb.run("forward", f"tcp:{self.port}", f"localabstract:{self.socket_name}")
        command = " ".join([f"CLASSPATH={REMOTE_JAR}", "app_process", "/", "com.genymobile.scrcpy.Server",
                            SERVER_VERSION, *self.options.server_args(self.scid)])
        self._process = await self.adb.spawn("shell", command)
        self._log_task = asyncio.create_task(self._collect_log())
        self.connection = ScrcpyConnection(video=self.options.video, audio=self.options.audio,
                                           control=self.options.control)
        try:
            await self.connection.open("127.0.0.1", self.port, timeout=timeout)
        except BaseException:
            await self.stop()
            raise
        if self.connection.audio_disabled_reason:
            log.warning("%s: %s", self.adb.serial, self.connection.audio_disabled_reason)
        return self.connection

    async def _collect_log(self) -> None:
        assert self._process and self._process.stdout
        async for raw in self._process.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            self.server_log.append(line)
            del self.server_log[:-200]
            log.info("[%s] %s", self.adb.serial, line)

    async def run(self, on_video: VideoHandler, on_audio: AudioHandler | None = None) -> None:
        """Dispatch packets until a stream ends. Raises ConnectionError on loss."""
        conn = self.connection
        if conn is None:
            raise RuntimeError("start() first")

        async def pump(name: str, handler, allow_session: bool) -> None:
            reader = conn.reader(name)
            if reader is None:
                return
            async for item in read_packets(reader, allow_session):
                await _call(handler, item)
            raise ConnectionError(f"{name} stream ended")

        tasks = [asyncio.create_task(pump("video", on_video, True))]
        if on_audio and conn.reader("audio"):
            tasks.append(asyncio.create_task(pump("audio", on_audio, False)))
        if conn.reader("control"):
            tasks.append(asyncio.create_task(conn.drain_control()))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def request_key_frame(self) -> None:
        if self.connection:
            await self.connection.send_control(protocol.encode_reset_video())

    async def stop(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._log_task:
            self._log_task.cancel()
            await asyncio.gather(self._log_task, return_exceptions=True)
            self._log_task = None
        if self.port:
            await self.adb.run("forward", "--remove", f"tcp:{self.port}", check=False)
            self.port = None
