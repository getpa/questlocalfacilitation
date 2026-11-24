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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from questhub.poc.session import DeviceSession
from questhub.stream import hub as hubmod

STATIC_DIR = Path(__file__).parent / "static"

from questhub.web.media_ws import (  # noqa: E402,F401  (re-exported for the PoC and load test)
    KIND_AUDIO, KIND_AUDIO_CONFIG, KIND_SESSION, KIND_VIDEO, KIND_VIDEO_CONFIG, ViewerFilter, encode_event,
)


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

    @app.get("/api/quality")
    async def quality():
        return session.quality_status()

    @app.post("/api/quality")
    async def set_quality(body: dict):
        try:
            return await session.set_quality(str(body.get("preset", "")))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None

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
