"""Test fixtures: synthetic media (questhub.testing.media) and a fake scrcpy server."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import unittest

try:
    from questhub.testing.media import FPS, make_h264, make_opus  # noqa: F401
    HAVE_AV = True
except ImportError:  # the CI job without the project env
    HAVE_AV = False
    FPS = 30
    make_h264 = make_opus = None

requires_av = unittest.skipUnless(HAVE_AV, "PyAV not installed (run with ./.mamba-env/bin/python)")


class FakeScrcpyServer:
    """A TCP server speaking the scrcpy forward-tunnel protocol.

    `refuse_first` closes the first connection without the dummy byte, as adb
    does when the device socket is not listening yet.
    """

    def __init__(self, video, audio=None, refuse_first: int = 1, device_name: str = "Fake Quest",
                 realtime: bool = False, audio_codec_id: bytes | None = None):
        from questhub.stream import protocol
        self.protocol = protocol
        self.config, self.frames = video
        self.audio = audio
        self.refuse_first = refuse_first
        self.device_name = device_name
        self.realtime = realtime
        self.audio_codec_id = audio_codec_id
        self.control_messages: list[bytes] = []
        self.connections = 0
        self._sockets = []
        self._server = None
        self.port = None
        self._writers = []
        self.done = asyncio.Event()

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        for writer in self._writers:
            writer.close()
        self._server.close()
        await self._server.wait_closed()

    async def _accept(self, reader, writer):
        self.connections += 1
        self._writers.append(writer)
        if self.refuse_first > 0:
            self.refuse_first -= 1
            writer.close()
            return
        self._sockets.append((reader, writer))
        if len(self._sockets) == 1:
            writer.write(b"\0")  # DesktopConnection: dummy byte right after the first accept
        expected = 1 + (self.audio is not None or self.audio_codec_id is not None) + 1
        if len(self._sockets) == expected:
            asyncio.create_task(self._serve())

    async def _serve(self):
        try:
            await self._serve_inner()
        except ConnectionError:  # the client closed first (restart / stop)
            pass

    async def _serve_inner(self):
        p = self.protocol
        sockets = list(self._sockets)
        video_w = sockets[0][1]
        audio_w = sockets[1][1] if len(sockets) == 3 else None
        control_r = sockets[-1][0]
        video_w.write(p.encode_device_name(self.device_name) + p.encode_codec_id("h264"))
        if audio_w is not None:
            audio_w.write(self.audio_codec_id if self.audio_codec_id is not None else p.encode_codec_id("opus"))
        asyncio.create_task(self._read_control(control_r))
        video_w.write(p.encode_session(320, 180))
        video_w.write(p.encode_packet(p.MediaPacket(self.config, None, config=True)))
        if audio_w is not None and self.audio and self.audio_codec_id is None:
            audio_w.write(p.encode_packet(p.MediaPacket(self.audio[0], None, config=True)))
        audio_packets = list(self.audio[1]) if (self.audio and audio_w is not None and self.audio_codec_id is None) else []
        for pts, key, data in self.frames:
            video_w.write(p.encode_packet(p.MediaPacket(data, pts, key_frame=key)))
            while audio_packets and audio_packets[0][0] <= pts:
                apts, adata = audio_packets.pop(0)
                audio_w.write(p.encode_packet(p.MediaPacket(adata, apts)))
            await video_w.drain()
            if self.realtime:
                await asyncio.sleep(1 / FPS)
        self.done.set()

    async def _read_control(self, reader):
        while True:
            data = await reader.read(64)
            if not data:
                return
            self.control_messages.append(data)

    def close_streams(self):
        for _, writer in self._sockets:
            writer.close()
