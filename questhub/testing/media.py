"""Synthetic scrcpy-like media (needs PyAV from .mamba-env).

Video mimics the Quest stream: Annex B H.264, SPS/PPS sent once as a config
packet, key frames without in-band parameter sets. Audio is Opus with the
same OpusHead layout the device sends.
"""
from __future__ import annotations

from fractions import Fraction

import av
import numpy as np

FPS = 30


def make_h264(frames: int = 60, width: int = 320, height: int = 180, gop: int = 15,
              bit_rate: int | None = None, detail: bool = False, fps: int = FPS,
              profile: str | None = None):
    """Return (config, [(pts_us, key_frame, data)]).

    `detail=True` adds moving noise so the encoder actually spends `bit_rate`
    (closer to a real headset than flat colours).
    """
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = width, height, "yuv420p"
    enc.time_base = Fraction(1, fps)
    # ultrafast disables CABAC (Constrained Baseline); the Quest sends High profile.
    options = {"tune": "zerolatency", "preset": "veryfast" if profile else "ultrafast", "g": str(gop),
               "bf": "0", "keyint_min": str(gop), "sc_threshold": "0"}
    if profile:
        options["profile"] = profile
    if bit_rate:
        enc.bit_rate = bit_rate
        options.update({"maxrate": str(bit_rate), "bufsize": str(bit_rate // 2)})
    enc.options = options
    enc.open()
    rng = np.random.default_rng(1)
    noise = rng.integers(0, 255, (height + 64, width + 64, 3), dtype=np.uint8) if detail else None
    packets = []
    for i in range(frames):
        if detail:
            dx, dy = (i * 3) % 64, (i * 2) % 64
            rgb = noise[dy:dy + height, dx:dx + width].copy()
            rgb[:, : (i * 9) % width] //= 2
        else:
            rgb = np.zeros((height, width, 3), np.uint8)
            rgb[:, : (i * 7) % width] = (200, 80, 40)
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        packets += list(enc.encode(frame))
    packets += list(enc.encode(None))
    out, config = [], None
    for i, pkt in enumerate(packets):
        nals = split_annexb(bytes(pkt))
        params = b"".join(n for n in nals if _nal_type(n) in (6, 7, 8))
        payload = b"".join(n for n in nals if _nal_type(n) not in (6, 7, 8))
        if config is None and params:
            config = params
        out.append((int(i * 1_000_000 / fps), pkt.is_keyframe, payload))
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


def _nal_type(nal: bytes) -> int:
    return nal[4 if nal.startswith(b"\0\0\0\1") else 3] & 0x1F


def split_annexb(data: bytes) -> list[bytes]:
    starts, i = [], 0
    while True:
        i = data.find(b"\0\0\1", i)
        if i < 0:
            break
        starts.append(i - 1 if i > 0 and data[i - 1] == 0 else i)
        i += 3
    return [data[s:e] for s, e in zip(starts, starts[1:] + [len(data)])]
