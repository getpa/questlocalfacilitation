"""PoC end to end without a headset: fake scrcpy server -> hub -> MKV / web API."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.media_fixtures import HAVE_AV, FakeScrcpyServer, make_h264, make_opus, requires_av  # noqa: E402

if HAVE_AV:
    import av
    from questhub.record.recorder import Recorder
    from questhub.stream import protocol
    from questhub.stream.hub import FrameHub
    from questhub.stream.scrcpy_server import ScrcpyConnection, read_packets


def decode_counts(path):
    with av.open(str(path)) as c:
        kinds = sorted(s.type for s in c.streams)
        video = sum(1 for _ in c.decode(video=0))
    audio = 0
    if "audio" in kinds:
        with av.open(str(path)) as c:
            audio = sum(1 for _ in c.decode(audio=0))
    return kinds, video, audio


class FakeServerAdapter:
    """Stand-in for ScrcpyServer (no adb) used by DeviceSession."""

    def __init__(self, fake):
        self.fake = fake
        self.connection = None

    async def start(self):
        self.connection = ScrcpyConnection()
        await self.connection.open("127.0.0.1", self.fake.port, timeout=5)
        return self.connection

    async def run(self, on_video, on_audio):
        async def pump(name, handler, session):
            async for item in read_packets(self.connection.reader(name), session):
                handler(item)
        await asyncio.gather(pump("video", on_video, True), pump("audio", on_audio, False))

    async def request_key_frame(self):
        await self.connection.send_control(protocol.encode_reset_video())

    async def stop(self):
        if self.connection:
            await self.connection.close()


@requires_av
class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video = make_h264(frames=90, gop=15)
        cls.audio = make_opus(packets=150)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_connection_retries_until_dummy_byte_and_reads_metadata(self):
        async def scenario():
            async with FakeScrcpyServer(self.video, self.audio, refuse_first=3) as fake:
                conn = ScrcpyConnection()
                await conn.open("127.0.0.1", fake.port, timeout=5)
                await conn.send_control(protocol.encode_reset_video())
                await asyncio.sleep(0.1)
                result = (conn.device_name, conn.video_codec, conn.audio_codec, fake.connections, fake.control_messages)
                await conn.close()
                return result
        name, vcodec, acodec, connections, control = asyncio.run(scenario())
        self.assertEqual((name, vcodec, acodec), ("Fake Quest", "h264", "opus"))
        self.assertEqual(connections, 6)  # 3 refused + video/audio/control
        self.assertEqual(control, [b"\x11"])

    def test_disabled_audio_keeps_video(self):
        async def scenario():
            async with FakeScrcpyServer(self.video, None, audio_codec_id=b"\0\0\0\0") as fake:
                conn = ScrcpyConnection()
                await conn.open("127.0.0.1", fake.port, timeout=5)
                result = (conn.audio_codec, conn.audio_disabled_reason is not None, conn.reader("audio"))
                await conn.close()
                return result
        codec, disabled, reader = asyncio.run(scenario())
        self.assertIsNone(codec)
        self.assertTrue(disabled)
        self.assertIsNone(reader)

    def test_hub_to_matroska_with_audio(self):
        path = self.dir / "out.mkv"

        async def scenario():
            async with FakeScrcpyServer(self.video, self.audio) as fake:
                conn = ScrcpyConnection()
                await conn.open("127.0.0.1", fake.port, timeout=5)
                hub = FrameHub(video_codec=conn.video_codec, audio_codec=conn.audio_codec)
                recorder = Recorder(path, hub)
                recorder.start()

                async def pump(name, handler, session):
                    async for item in read_packets(conn.reader(name), session):
                        handler(item)
                tasks = [asyncio.create_task(pump("video", hub.publish_video, True)),
                         asyncio.create_task(pump("audio", hub.publish_audio, False))]
                await fake.done.wait()
                await asyncio.sleep(0.3)
                stats = await recorder.stop()
                for t in tasks:
                    t.cancel()
                await conn.close()
                return stats
        stats = asyncio.run(scenario())
        self.assertIsNone(stats.error)
        kinds, video, audio = decode_counts(path)
        self.assertEqual(kinds, ["audio", "video"])
        self.assertEqual(video, 90)
        self.assertGreaterEqual(audio, 140)
        self.assertEqual(stats.video_packets, 90)
        with av.open(str(path)) as c:
            self.assertAlmostEqual(c.duration / 1e6, 3.0, delta=0.2)

    def test_recording_starts_at_key_frame_mid_stream(self):
        path = self.dir / "late.mkv"

        async def scenario():
            hub = FrameHub(video_codec="h264", audio_codec=None)
            config, frames = self.video
            hub.publish_video(protocol.SessionPacket(320, 180))
            hub.publish_video(protocol.MediaPacket(config, None, config=True))
            for pts, key, data in frames[:20]:
                hub.publish_video(protocol.MediaPacket(data, pts, key_frame=key))
            recorder = Recorder(path, hub, audio=False)
            recorder.start()
            for pts, key, data in frames[20:]:
                hub.publish_video(protocol.MediaPacket(data, pts, key_frame=key))
            await asyncio.sleep(0.2)
            return await recorder.stop()
        stats = asyncio.run(scenario())
        _, video, _ = decode_counts(path)
        self.assertEqual(video, 60)  # frames 30..89: the first key frame after start is frame 30
        self.assertEqual(stats.video_packets, 60)


@requires_av
class WebAppTests(unittest.TestCase):
    """FastAPI app + DeviceSession supervisor against the fake server."""

    @classmethod
    def setUpClass(cls):
        cls.video = make_h264(frames=150, gop=15)
        cls.audio = make_opus(packets=250)

    def test_status_websocket_and_recording_api(self):
        from fastapi.testclient import TestClient
        from questhub.poc.app import KIND_AUDIO, KIND_SESSION, KIND_VIDEO, KIND_VIDEO_CONFIG, create_app
        from questhub.poc.session import DeviceSession

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        holder = {}

        async def make_fake():
            fake = FakeScrcpyServer(self.video, self.audio, realtime=True)
            await fake.__aenter__()
            holder["fake"] = fake
            return fake

        class Session(DeviceSession):
            async def start(self, wait=None):
                fake = await make_fake()
                self._server_factory = lambda: FakeServerAdapter(fake)
                await super().start(wait=5)

        session = Session("fake:5555", record_dir=tmp.name, adb=_NoAdb())
        with TestClient(create_app(session)) as client:
            status = client.get("/api/status").json()
            self.assertEqual(status["state"], "streaming")
            self.assertEqual(status["device_name"], "Fake Quest")

            with client.websocket_connect("/ws/media") as ws:
                kinds = []
                while KIND_VIDEO not in kinds or KIND_AUDIO not in kinds:
                    msg = ws.receive_bytes()
                    kinds.append(msg[0])
                    if msg[0] == KIND_VIDEO:
                        self.assertEqual(msg[1] & 1, 1, "first video frame must be a key frame")
                        break
                self.assertLess(kinds.index(KIND_SESSION), kinds.index(KIND_VIDEO))
                self.assertLess(kinds.index(KIND_VIDEO_CONFIG), kinds.index(KIND_VIDEO))

            with client.websocket_connect("/ws/media?video=keyframes&audio=0") as ws:
                video_flags = []
                while len(video_flags) < 3:
                    msg = ws.receive_bytes()
                    if msg[0] == KIND_VIDEO:
                        video_flags.append(msg[1] & 1)
                self.assertEqual(video_flags, [1, 1, 1])

            start = client.post("/api/recording/start")
            self.assertEqual(start.status_code, 200, start.text)
            self.assertEqual(client.post("/api/recording/start").status_code, 409)
            import time
            time.sleep(1.5)   # Windows timers are coarser; 1 s gave exactly 10 packets there
            stop = client.post("/api/recording/stop").json()
            self.assertIsNone(stop["error"])
            self.assertGreater(stop["video_packets"], 10)
            self.assertIn(b"\x11", b"".join(holder["fake"].control_messages))  # key frame requested
        kinds, video, audio = decode_counts(stop["path"])
        self.assertEqual(kinds, ["audio", "video"])
        self.assertGreater(video, 10)
        self.assertGreater(audio, 10)

    def test_supervisor_restarts_after_stream_loss(self):
        from questhub.poc import session as session_mod
        from questhub.poc.session import DeviceSession

        async def scenario():
            fakes = []

            async def factory_fake():
                fake = FakeScrcpyServer(self.video, self.audio)
                await fake.__aenter__()
                fakes.append(fake)
                return fake

            first = await factory_fake()
            second = await factory_fake()
            queue = [first, second]
            session = DeviceSession("fake", adb=_NoAdb(),
                                    server_factory=lambda: FakeServerAdapter(queue.pop(0)))
            old_delay, session_mod.RETRY_DELAY = session_mod.RETRY_DELAY, 0.05
            try:
                await session.start(wait=5)
                await first.done.wait()
                first.close_streams()
                for _ in range(100):
                    if session.restarts >= 1 and session.state == "streaming":
                        break
                    await asyncio.sleep(0.05)
                result = (session.restarts, session.state)
                await session.stop()
            finally:
                session_mod.RETRY_DELAY = old_delay
                for fake in fakes:
                    await fake.__aexit__(None, None, None)
            return result
        restarts, state = asyncio.run(scenario())
        self.assertEqual(restarts, 1)
        self.assertEqual(state, "streaming")


class _NoAdb:
    """DeviceSession only calls adb for Wi-Fi info; fail fast so telemetry is skipped."""
    binary = "adb"
    serial = "fake"

    async def shell(self, *args, **kwargs):
        from questhub.adb.client import AdbError
        raise AdbError(args, 1, "no device")

    async def run(self, *args, **kwargs):
        return ""


if __name__ == "__main__":
    unittest.main()
