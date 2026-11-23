"""scrcpy 5.0.1 wire format (pure functions, no I/O).

Reference: server/src/main/java/com/genymobile/scrcpy/device/{Streamer,DesktopConnection}.java
at tag v5.0.1. Integers are big-endian.

Per socket (tunnel_forward=true):
  first socket only : 1 dummy byte (0x00), then the 64-byte NUL-padded device name
                      (sent after all sockets are connected)
  each media socket : u32 codec id (0 = stream disabled, 1 = configuration error)
  then packets, each with a 12-byte header:
    session packet  : byte0 MSB=1; u32 flags | u32 width | u32 height   (video only)
    media packet    : u64 pts_and_flags | u32 size, followed by `size` bytes
                      bit 62 = config packet (no pts), bit 61 = key frame,
                      low 61 bits = pts in microseconds
"""
from __future__ import annotations

from dataclasses import dataclass
import struct

DEVICE_NAME_LENGTH = 64
HEADER_LENGTH = 12

FLAG_SESSION = 1 << 63
FLAG_CONFIG = 1 << 62
FLAG_KEY_FRAME = 1 << 61
PTS_MASK = FLAG_KEY_FRAME - 1

CODEC_DISABLED = 0
CODEC_ERROR = 1
CODEC_IDS = {
    0x68323634: "h264",
    0x68323635: "h265",
    0x00617631: "av1",
    0x6F707573: "opus",
    0x00616163: "aac",
    0x664C6163: "flac",
    0x00726177: "raw",
}


class ProtocolError(Exception):
    """The stream does not follow the scrcpy protocol."""


@dataclass(frozen=True)
class SessionPacket:
    width: int
    height: int
    client_resized: bool = False


@dataclass(frozen=True)
class MediaPacket:
    data: bytes
    pts: int | None  # microseconds; None for config packets
    config: bool = False
    key_frame: bool = False


@dataclass(frozen=True)
class PacketHeader:
    session: bool
    config: bool = False
    key_frame: bool = False
    pts: int | None = None
    size: int = 0
    width: int = 0
    height: int = 0
    client_resized: bool = False


def parse_device_name(raw: bytes) -> str:
    if len(raw) != DEVICE_NAME_LENGTH:
        raise ProtocolError(f"device name field must be {DEVICE_NAME_LENGTH} bytes, got {len(raw)}")
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def parse_codec_id(raw: bytes) -> str:
    """Return the codec name, or raise for a disabled/failed stream."""
    if len(raw) != 4:
        raise ProtocolError("codec id must be 4 bytes")
    (codec_id,) = struct.unpack(">I", raw)
    if codec_id == CODEC_DISABLED:
        raise StreamDisabled("device disabled this stream (e.g. audio capture unavailable)")
    if codec_id == CODEC_ERROR:
        raise ProtocolError("device reported a configuration error")
    try:
        return CODEC_IDS[codec_id]
    except KeyError:
        raise ProtocolError(f"unknown codec id 0x{codec_id:08x}") from None


class StreamDisabled(Exception):
    """The device explicitly disabled the stream (scrcpy continues without it)."""


def parse_header(raw: bytes) -> PacketHeader:
    if len(raw) != HEADER_LENGTH:
        raise ProtocolError(f"packet header must be {HEADER_LENGTH} bytes")
    (value,) = struct.unpack(">Q", raw[:8])
    if value & FLAG_SESSION:
        flags, width, height = struct.unpack(">III", raw)
        return PacketHeader(session=True, width=width, height=height, client_resized=bool(flags & 1))
    (size,) = struct.unpack(">I", raw[8:])
    config = bool(value & FLAG_CONFIG)
    return PacketHeader(
        session=False,
        config=config,
        key_frame=bool(value & FLAG_KEY_FRAME),
        pts=None if config else value & PTS_MASK,
        size=size,
    )


# Encoders used by the fake server in tests (inverse of the parsers above).

def encode_device_name(name: str) -> bytes:
    data = name.encode("utf-8")[: DEVICE_NAME_LENGTH - 1]
    return data.ljust(DEVICE_NAME_LENGTH, b"\0")


def encode_codec_id(codec: str) -> bytes:
    for codec_id, value in CODEC_IDS.items():
        if value == codec:
            return struct.pack(">I", codec_id)
    raise ValueError(codec)


def encode_session(width: int, height: int, client_resized: bool = False) -> bytes:
    return struct.pack(">III", 0x80000000 | int(client_resized), width, height)


def encode_packet(packet: MediaPacket) -> bytes:
    if packet.config:
        value = FLAG_CONFIG
    else:
        value = (packet.pts or 0) & PTS_MASK
        if packet.key_frame:
            value |= FLAG_KEY_FRAME
    return struct.pack(">QI", value, len(packet.data)) + packet.data


# Control messages (ControlMessageReader.java). Only what the PoC needs.
CONTROL_RESET_VIDEO = 17


def encode_reset_video() -> bytes:
    return bytes([CONTROL_RESET_VIDEO])
