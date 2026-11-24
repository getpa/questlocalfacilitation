"""Media WebSocket adapter (docs/GUI_DESIGN.md §3.3): per-viewer policy on top of FrameHub.

Binary message (big-endian):
  u8 kind | u8 flags | u64 pts_us | payload
  kind: 1 session (payload u32 width, u32 height), 2 video config (Annex B SPS/PPS),
        3 video frame (flags bit0 = key frame), 4 audio config (OpusHead), 5 audio frame
"""
from __future__ import annotations

import asyncio
import struct

from fastapi import WebSocket, WebSocketDisconnect

from questhub.stream import hub as hubmod
from questhub.stream.hub import FrameHub

KIND_SESSION, KIND_VIDEO_CONFIG, KIND_VIDEO, KIND_AUDIO_CONFIG, KIND_AUDIO = 1, 2, 3, 4, 5


def encode_event(event: hubmod.Event) -> bytes:
    if event.kind == hubmod.SESSION:
        return struct.pack(">BBQII", KIND_SESSION, 0, 0, event.session.width, event.session.height)
    kind = {
        hubmod.VIDEO_CONFIG: KIND_VIDEO_CONFIG,
        hubmod.VIDEO: KIND_VIDEO,
        hubmod.AUDIO_CONFIG: KIND_AUDIO_CONFIG,
        hubmod.AUDIO: KIND_AUDIO,
    }[event.kind]
    packet = event.packet
    return struct.pack(">BBQ", kind, 1 if packet.key_frame else 0, packet.pts or 0) + packet.data


class ViewerFilter:
    """Full rate, or every Nth key frame (low-fps grids)."""

    def __init__(self, keyframes_only: bool, every: int = 1):
        self.keyframes_only = keyframes_only
        self.every = max(1, every)
        self._key_count = 0

    def accept(self, event: hubmod.Event) -> bool:
        if event.kind != hubmod.VIDEO or not self.keyframes_only:
            return True
        if not event.packet.key_frame:
            return False
        self._key_count += 1
        return (self._key_count - 1) % self.every == 0


async def serve_media(ws: WebSocket, hub: FrameHub, video: str = "full", every: int = 1,
                      audio: bool = False, maxsize: int = 256) -> None:
    """Pump one hub to one accepted WebSocket until either side closes."""
    viewer = ViewerFilter(keyframes_only=(video == "keyframes"), every=every)
    sub = hub.subscribe(video=True, audio=audio, maxsize=maxsize)

    async def watch_disconnect():
        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        sub.close()

    watcher = asyncio.create_task(watch_disconnect())
    try:
        async for event in sub:
            if viewer.accept(event):
                await ws.send_bytes(encode_event(event))
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        pass
    finally:
        sub.close()
        watcher.cancel()
