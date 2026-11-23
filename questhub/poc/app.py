"""PoC web server: status/recording API and the media WebSocket.

Binary WebSocket message (big-endian):
  u8 kind | u8 flags | u64 pts_us | payload
  kind: 1 session (payload u32 width, u32 height), 2 video config (Annex B SPS/PPS),
        3 video frame (flags bit0 = key frame), 4 audio config (OpusHead), 5 audio frame
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import struct

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from questhub.poc.session import DeviceSession
from questhub.stream import hub as hubmod

STATIC_DIR = Path(__file__).parent / "static"

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
    flags = 1 if packet.key_frame else 0
    return struct.pack(">BBQ", kind, flags, packet.pts or 0) + packet.data


class ViewerFilter:
    """Per-viewer policy (docs/GUI_DESIGN.md §4.6): full rate, or every Nth key frame."""

    def __init__(self, keyframes_only: bool, every: int):
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


def create_app(session: DeviceSession, start_session: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_session:
            await session.start(wait=None)
        try:
            yield
        finally:
            if start_session:
                await session.stop()

    app = FastAPI(title="Quest Hub PoC", lifespan=lifespan)
    app.state.session = session
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    async def status():
        return session.status()

    @app.post("/api/recording/start")
    async def recording_start():
        if session.state != "streaming":
            raise HTTPException(409, f"device is {session.state}")
        try:
            path = await session.start_recording()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"path": path}

    @app.post("/api/recording/stop")
    async def recording_stop():
        stats = await session.stop_recording()
        if stats is None:
            raise HTTPException(409, "not recording")
        return stats

    @app.post("/api/keyframe")
    async def keyframe():
        await session.request_key_frame()
        return {"ok": True}

    @app.websocket("/ws/media")
    async def media(ws: WebSocket, video: str = "full", every: int = 1, audio: int = 1):
        await ws.accept()
        viewer = ViewerFilter(keyframes_only=(video == "keyframes"), every=every)
        sub = session.hub.subscribe(video=True, audio=bool(audio), maxsize=256)

        async def watch_disconnect():
            try:
                while True:
                    await ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                pass
            sub.close()

        watcher = asyncio.create_task(watch_disconnect())
        try:
            async for event in sub:
                if viewer.accept(event):
                    await ws.send_bytes(encode_event(event))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            sub.close()
            watcher.cancel()

    return app
