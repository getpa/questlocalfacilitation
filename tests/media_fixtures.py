"""Synthetic scrcpy-like media for offline tests (needs PyAV from .mamba-env).

Video mimics the Quest stream: Annex B H.264, SPS/PPS sent once as a config
packet, key frames without in-band parameter sets. Audio is Opus with the
same OpusHead layout the device sends.
"""
from __future__ import annotations

import asyncio
from fractions import Fraction
import unittest

try:
    import av
    import numpy as np
    HAVE_AV = True
except ImportError:  # the CI job without the project env
    HAVE_AV = False

requires_av = unittest.skipUnless(HAVE_AV, "PyAV not installed (run with ./.mamba-env/bin/python)")

FPS = 30


def make_h264(frames: int = 60, width: int = 320, height: int = 180, gop: int = 15):
    """Return (config, [(pts_us, key_frame, data)])."""
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = width, height, "yuv420p"
    enc.time_base = Fraction(1, FPS)
    enc.options = {"tune": "zerolatency", "preset": "ultrafast", "g": str(gop), "bf": "0", "keyint_min": str(gop)}
    enc.open()
    packets = []
    for i in range(frames):
        rgb = np.zeros((height, width, 3), np.uint8)
        rgb[:, : (i * 7) % width] = (200, 80, 40)
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        packets += list(enc.encode(frame))
    packets += list(enc.encode(None))
    out, config = [], None
    for i, pkt in enumerate(packets):
        data = bytes(pkt)
        nals = _split_annexb(data)
        params = b"".join(n for n in nals if n[_start_len(n)] & 0x1F in (6, 7, 8))
        payload = b"".join(n for n in nals if n[_start_len(n)] & 0x1F not in (6, 7, 8))
        if config is None and params:
            config = params
        out.append((int(i * 1_000_000 / FPS), pkt.is_keyframe, payload))
    return config, out


def make_opus(packets: int = 100):
    """Return (opus_head, [(pts_us, data)]) with 20 ms packets of a tone."""
    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate, enc.layout, enc.format = 48000, "stereo", "s16"
    enc.open()
    out = []
    t = np.arange(960) / 48000
    for i in range(packets):
        tone = (np.sin(2 * np.pi * 440 * (t + i * 0.02)) * 4000).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(np.repeat(tone, 2).reshape(1, -1), format="s16", layout="stereo")
        frame.sample_rate, frame.pts = 48000, i * 960
        out += [bytes(p) for p in enc.encode(frame)]
    return bytes(enc.extradata), [(i * 20_000, data) for i, data in enumerate(out)]


def _start_len(nal: bytes) -> int:
    return 4 if nal.startswith(b"\0\0\0\1") else 3


def _split_annexb(data: bytes) -> list[bytes]:
    starts, i = [], 0
    while True:
        i = data.find(b"\0\0\1", i)
        if i < 0:
            break
        starts.append(i - 1 if i > 0 and data[i - 1] == 0 else i)
        i += 3
    return [data[s:e] for s, e in zip(starts, starts[1:] + [len(data)])]


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
