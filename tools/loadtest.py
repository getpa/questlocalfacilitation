"""M1 load test (docs/GUI_DESIGN.md §7, P2): N replayed headsets -> browser grid.

Each simulated headset is a TCP server speaking the scrcpy protocol with a
pre-encoded Quest-like clip (default 1280x684, 30 fps, 4 Mbps, key frame every
2 s, plus Opus), paced in real time. The real client path is used:
ScrcpyConnection -> FrameHub -> /ws/media/{i} -> WebCodecs tiles.

  ./.mamba-env/bin/python tools/loadtest.py --devices 30
  open http://127.0.0.1:8790/?n=30&mode=full      (mode=keyframes&every=1 for low fps)
  optionally add a real headset as tile 0:  --real 192.168.22.201:5555
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from questhub.poc.app import ViewerFilter, encode_event  # noqa: E402
from questhub.stream import protocol as p  # noqa: E402
from questhub.stream.hub import FrameHub  # noqa: E402
from questhub.stream.scrcpy_server import ScrcpyConnection, read_packets  # noqa: E402

log = logging.getLogger("loadtest")
STATIC = Path(__file__).resolve().parents[1] / "questhub/poc/static"
PAGE = Path(__file__).with_name("loadtest.html")


class ReplayDevice:
    """Loops a clip forever with increasing PTS, paced to wall-clock time."""

    def __init__(self, index: int, video, audio, width: int, height: int):
        self.index = index
        self.config, self.frames = video
        self.audio_head, self.audio = audio
        self.width, self.height = width, height
        self.clip_us = self.frames[-1][0] + (self.frames[1][0] - self.frames[0][0])
        self.hub = FrameHub(video_codec="h264", audio_codec="opus")
        self._sockets = []
        self._server = None
        self._tasks = []
        self.sent_bytes = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        conn = ScrcpyConnection()
        await conn.open("127.0.0.1", port, timeout=10)

        async def pump(name, handler, session):
            async for item in read_packets(conn.reader(name), session):
                handler(item)
        self._tasks += [asyncio.create_task(pump("video", self.hub.publish_video, True)),
                        asyncio.create_task(pump("audio", self.hub.publish_audio, False))]
        self.conn = conn

    async def _accept(self, reader, writer):
        self._sockets.append((reader, writer))
        if len(self._sockets) == 1:
            writer.write(b"\0")
        if len(self._sockets) == 3:
            self._tasks.append(asyncio.create_task(self._serve()))

    async def _serve(self):
        video_w, audio_w = self._sockets[0][1], self._sockets[1][1]
        video_w.write(p.encode_device_name(f"Replay {self.index:02d}") + p.encode_codec_id("h264"))
        audio_w.write(p.encode_codec_id("opus"))
        video_w.write(p.encode_session(self.width, self.height))
        video_w.write(p.encode_packet(p.MediaPacket(self.config, None, config=True)))
        audio_w.write(p.encode_packet(p.MediaPacket(self.audio_head, None, config=True)))
        # Stagger devices so their key frames are not synchronised (like real headsets).
        offset_us = (self.index * 137_000) % self.clip_us
        start = time.monotonic() - offset_us / 1e6
        cycle = 0
        audio_clip_us = self.audio[-1][0] + 20_000
        audio_index, audio_cycle = 0, 0
        while True:
            for pts, key, data in self.frames:
                abs_pts = cycle * self.clip_us + pts
                if cycle == 0 and pts < offset_us:
                    continue
                delay = start + abs_pts / 1e6 - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                packet = p.encode_packet(p.MediaPacket(data, abs_pts, key_frame=key))
                video_w.write(packet)
                self.sent_bytes += len(packet)
                while True:  # audio up to the current video time
                    apts, adata = self.audio[audio_index]
                    abs_apts = audio_cycle * audio_clip_us + apts
                    if abs_apts > abs_pts:
                        break
                    audio_w.write(p.encode_packet(p.MediaPacket(adata, abs_apts)))
                    audio_index += 1
                    if audio_index == len(self.audio):
                        audio_index, audio_cycle = 0, audio_cycle + 1
                await video_w.drain()
            cycle += 1


def process_usage(pid: int) -> dict:
    out = subprocess.run(["ps", "-o", "%cpu=,rss=", "-p", str(pid)], capture_output=True, text=True).stdout.split()
    return {"cpu_percent": float(out[0]), "rss_mb": round(int(out[1]) / 1024, 1)} if len(out) == 2 else {}


def create_app(devices: list) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        for device in devices:
            await device.start()
        app.state.started = time.monotonic()
        yield

    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def page():
        return FileResponse(PAGE, headers={"Cache-Control": "no-store"})

    @app.get("/api/load")
    async def load():
        elapsed = time.monotonic() - app.state.started
        return {
            "devices": len(devices),
            "elapsed_s": round(elapsed, 1),
            "server": process_usage(os.getpid()),
            "per_device": [
                {"packets": d.hub.video_stats.packets, "audio_packets": d.hub.audio_stats.packets,
                 "mbps_in": round(d.hub.video_stats.bytes * 8 / 1e6 / max(elapsed, 0.1), 2),
                 "viewers": d.hub.subscriber_count}
                for d in devices
            ],
        }

    @app.websocket("/ws/media/{index}")
    async def media(ws: WebSocket, index: int, video: str = "full", every: int = 1, audio: int = 0):
        await ws.accept()
        hub = devices[index].hub
        viewer = ViewerFilter(keyframes_only=(video == "keyframes"), every=every)
        sub = hub.subscribe(video=True, audio=bool(audio), maxsize=256)

        async def watch():
            try:
                while True:
                    await ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                pass
            sub.close()
        watcher = asyncio.create_task(watch())
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


class RealDevice:
    """Wrap a PoC DeviceSession so a real headset can be tile 0."""

    def __init__(self, serial: str):
        from questhub.poc.session import DeviceSession
        self.session = DeviceSession(serial)
        self.hub = self.session.hub

    async def start(self):
        await self.session.start(wait=20)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--devices", type=int, default=30)
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=684)
    parser.add_argument("--bit-rate", type=int, default=4_000_000)
    parser.add_argument("--gop", type=int, default=60, help="frames between key frames (Quest: ~60 at 30 fps)")
    parser.add_argument("--seconds", type=int, default=10, help="clip length before looping")
    parser.add_argument("--profile", default="high", help="H.264 profile of the clip (Quest: high); 'baseline' for ultrafast")
    parser.add_argument("--real", help="adb serial of a real headset to use as tile 0")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    from questhub.testing.media import FPS, make_h264, make_opus
    print(f"[loadtest] encoding {args.seconds}s clip {args.width}x{args.height} @ {args.bit_rate/1e6:g} Mbps…", file=sys.stderr)
    video = make_h264(frames=args.seconds * FPS, width=args.width, height=args.height, gop=args.gop,
                      bit_rate=args.bit_rate, detail=True,
                      profile=None if args.profile == "baseline" else args.profile)
    audio = make_opus(packets=args.seconds * 50)
    clip_mbps = sum(len(d) for _, _, d in video[1]) * 8 / args.seconds / 1e6
    print(f"[loadtest] clip: {len(video[1])} frames, {clip_mbps:.2f} Mbps actual", file=sys.stderr)
    devices: list = []
    if args.real:
        devices.append(RealDevice(args.real))
    while len(devices) < args.devices:
        devices.append(ReplayDevice(len(devices), video, audio, args.width, args.height))

    import uvicorn
    print(f"[loadtest] http://127.0.0.1:{args.port}/?n={args.devices}", file=sys.stderr)
    uvicorn.run(create_app(devices), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
