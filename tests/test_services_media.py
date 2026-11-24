"""M3/M4/M6: supervisor restarts, streaming service, recording sessions, control messages."""
import asyncio
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from questhub.stream import control  # noqa: E402
from tests.media_fixtures import FakeScrcpyServer, make_h264, make_opus, requires_av  # noqa: E402


class ControlTests(unittest.TestCase):
    def test_layouts_match_server_reader(self):
        key = control.keycode(control.ACTION_DOWN, control.KEYCODES["HOME"])
        self.assertEqual(len(key), 14)
        self.assertEqual(struct.unpack(">BBiii", key), (0, 0, 3, 0, 0))
        touch = control.touch(control.ACTION_DOWN, 10, 20, 1280, 684)
        self.assertEqual(len(touch), 32)
        fields = struct.unpack(">BBqiiHHHii", touch)
        self.assertEqual(fields[:7], (2, 0, 0, 10, 20, 1280, 684))
        self.assertEqual(fields[7], 0xFFFF)  # pressure 1.0
        self.assertEqual(struct.unpack(">BBqiiHHHii", control.touch(control.ACTION_UP, 1, 1, 2, 2))[7], 0)
        self.assertEqual(control.text("hé"), b"\x01\x00\x00\x00\x03h\xc3\xa9")
        self.assertEqual(len(control.tap(1, 1, 10, 10)), 64)
        self.assertEqual(len(control.swipe([(0, 0), (5, 5), (9, 9)], 10, 10)), 3)
        with self.assertRaises(ValueError):
            control.text("x" * 400)


class FakeFactory:
    """Server factory for StreamSupervisor: a fresh fake scrcpy server per start."""

    def __init__(self, video, audio, fail_first: int = 0, realtime: bool = True):
        self.video, self.audio = video, audio
        self.fail_first = fail_first
        self.realtime = realtime
        self.started_targets: list[str] = []
        self.options_seen = []
        self.fakes: list[FakeScrcpyServer] = []
        self.control: list[bytes] = []

    def __call__(self, sup):
        factory = self

        class Adapter:
            connection = None

            async def start(self_inner):
                factory.started_targets.append(sup.target_fn())
                factory.options_seen.append(sup.options)
                if factory.fail_first > 0:
                    factory.fail_first -= 1
                    raise ConnectionError("simulated start failure")
                from questhub.stream.scrcpy_server import ScrcpyConnection
                fake = FakeScrcpyServer(factory.video, factory.audio, realtime=factory.realtime)
                await fake.__aenter__()
                factory.fakes.append(fake)
                self_inner.fake = fake
                self_inner.connection = ScrcpyConnection()
                await self_inner.connection.open("127.0.0.1", fake.port, timeout=5)
                return self_inner.connection

            async def run(self_inner, on_video, on_audio):
                from questhub.stream.scrcpy_server import read_packets
                async def pump(name, handler, session):
                    async for item in read_packets(self_inner.connection.reader(name), session):
                        handler(item)
                await asyncio.gather(pump("video", on_video, True), pump("audio", on_audio, False))

            async def request_key_frame(self_inner):
                factory.control.append(b"\x11")

            async def stop(self_inner):
                if self_inner.connection:
                    await self_inner.connection.close()

        return lambda target: Adapter()

    async def close(self):
        for fake in self.fakes:
            await fake.__aexit__(None, None, None)


class StreamingAutoStartTests(unittest.TestCase):
    def test_auto_start_on_connect_and_suspended_after_end_of_class(self):
        from questhub.config import Settings
        from questhub.core import events as ev
        from questhub.core.events import EventBus
        from questhub.devices.manager import DeviceManager
        from questhub.devices.registry import Registry
        from questhub.services.streaming import StreamingService
        from tests.fakes import FakeAdbWorld, TrackFeed

        class DummySup:
            def __init__(self):
                self.starts = 0
                self.locked_reason = None

            def start(self):
                self.starts += 1

            async def stop(self):
                pass

        async def scenario():
            bus = EventBus()
            reg = Registry(":memory:", bus)
            mgr = DeviceManager(reg, bus, FakeAdbWorld([]).adb(), track_source=TrackFeed().source, auto_reconnect=False)
            sups = {}
            streaming = StreamingService(mgr, reg, Settings(), supervisor_factory=lambda s: sups.setdefault(s, DummySup()))
            await streaming.start()
            bus.publish(ev.CONNECTION_CHANGED, "S1", state="wifi")
            await asyncio.sleep(0.05)
            first = sups["S1"].starts
            await streaming.stop_all(suspend_auto=True)
            bus.publish(ev.CONNECTION_CHANGED, "S1", state="usb")
            bus.publish(ev.CONNECTION_CHANGED, "tsv:1.2.3.4:5555", state="wifi")
            await asyncio.sleep(0.05)
            suspended = sups["S1"].starts
            streaming.start_stream("S1")
            bus.publish(ev.CONNECTION_CHANGED, "S2", state="wifi")
            await asyncio.sleep(0.05)
            return first, suspended, streaming.auto_suspended, sorted(sups)
        first, suspended, flag, serials = asyncio.run(scenario())
        self.assertEqual((first, suspended), (1, 1))
        self.assertFalse(flag)
        self.assertEqual(serials, ["S1", "S2"], "placeholders never stream")


@requires_av
class SupervisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video = make_h264(frames=60, gop=15)
        cls.audio = make_opus(packets=100)

    def make(self, factory, target=lambda: "10.0.0.5:5555"):
        from questhub.core.events import EventBus
        from questhub.stream import supervisor as supmod
        from questhub.stream.scrcpy_server import StreamOptions
        supmod.MIN_BACKOFF = 0.05
        bus = EventBus()
        sup = supmod.StreamSupervisor("S1", target, bus, StreamOptions.from_preset("standard"), wake=False)
        sup._server_factory = factory(sup)
        return sup, bus

    def test_waits_for_target_restarts_after_failure_and_changes_quality(self):
        async def scenario():
            factory = FakeFactory(self.video, self.audio, fail_first=1)
            target = {"value": None}
            sup, bus = self.make(factory, lambda: target["value"])
            sup.start()
            await asyncio.sleep(0.2)
            waiting = sup.state
            target["value"] = "10.0.0.5:5555"
            await sup.wait_streaming(10)
            restarts = sup.restarts
            await sup.set_quality("low")
            await sup.wait_streaming(10)
            await asyncio.sleep(0.1)
            sup.locked_reason = "録画中"
            with self.assertRaises(RuntimeError):
                await sup.set_quality("high")
            await sup.stop()
            await factory.close()
            return waiting, restarts, sup.restarts, [o.quality for o in factory.options_seen], \
                [e["data"]["state"] for e in bus.recent(100) if e["type"] == "media_state_changed"]
        waiting, restarts, final_restarts, qualities, states = asyncio.run(scenario())
        self.assertEqual(waiting, "waiting")
        self.assertEqual(restarts, 1)
        self.assertEqual(final_restarts, 1, "a quality change is not a failure")
        self.assertEqual(qualities[-1], "low")
        self.assertIn("backoff", states)
        self.assertIn("reconfiguring", states)

    def test_pause_and_resume(self):
        async def scenario():
            factory = FakeFactory(self.video, self.audio)
            sup, _ = self.make(factory)
            sup.start()
            await sup.wait_streaming(10)
            await sup.pause()
            await asyncio.sleep(0.2)
            paused_state, starts = sup.state, len(factory.started_targets)
            sup.resume()
            await sup.wait_streaming(10)
            await sup.stop()
            await factory.close()
            return paused_state, starts, len(factory.started_targets)
        paused, before, after = asyncio.run(scenario())
        self.assertEqual(paused, "paused")
        self.assertEqual(after, before + 1)


@requires_av
class RecordingServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video = make_h264(frames=90, gop=15)
        cls.audio = make_opus(packets=150)

    def test_session_files_lock_and_disk_guard(self):
        import av
        from questhub.config import Settings
        from questhub.core.events import EventBus
        from questhub.devices.manager import DeviceManager
        from questhub.devices.registry import Registry
        from questhub.services.recording import RecordingService
        from questhub.services.streaming import StreamingService
        from questhub.stream import supervisor as supmod
        from tests.fakes import FakeAdbWorld, FakeHeadset, TrackFeed
        supmod.MIN_BACKOFF = 0.05
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        async def scenario():
            world = FakeAdbWorld([FakeHeadset("S1", "10.0.0.1"), FakeHeadset("S2", "10.0.0.2")])
            bus = EventBus()
            reg = Registry(":memory:", bus)
            for s, ip in (("S1", "10.0.0.1"), ("S2", "10.0.0.2")):
                reg.observe(s, "adb", ip=ip, wifi=None)
            reg.update("S1", alias="Seat 01/A")
            mgr = DeviceManager(reg, bus, world.adb(), track_source=TrackFeed().source, auto_reconnect=False)
            for s, ip in (("S1", "10.0.0.1"), ("S2", "10.0.0.2")):
                st = mgr.get(s)
                st.endpoint, st.wifi_state = f"{ip}:5555", "device"
            settings = Settings(record_dir=tmp.name, disk_min_free_gb=0.0, wake_on_start=False)
            factory = FakeFactory(self.video, self.audio)

            def make_sup(serial):
                sup = supmod.StreamSupervisor(serial, lambda: mgr.target(serial), bus,
                                              streaming._options_for(serial), wake=False)
                sup._server_factory = factory(sup)
                return sup
            streaming = StreamingService(mgr, reg, settings, supervisor_factory=make_sup)
            await streaming.start_all()
            for sup in streaming.supervisors.values():
                await sup.wait_streaming(10)
            rec = RecordingService(streaming, reg, settings)
            from questhub.imu.recorder import ImuRecorder
            from tests.test_imu import HELPER_OUTPUT, FakeProcess

            async def fake_helper(adb):
                return FakeProcess(HELPER_OUTPUT.splitlines())
            rec._imu_factory = lambda path, sup, serial: ImuRecorder(path, lambda: "adb", serial=serial,
                                                                     spawn=fake_helper)
            result = await rec.start(None, "lesson 01")
            locked = {s: sup.locked_reason for s, sup in streaming.supervisors.items()}
            with self.assertRaises(RuntimeError):
                await streaming.set_quality("S1", "low")
            await asyncio.sleep(1.5)
            status = rec.status()
            stopped = await rec.stop()
            unlocked = {s: sup.locked_reason for s, sup in streaming.supervisors.items()}
            settings.disk_min_free_gb = 1e9   # pretend the disk is full
            with self.assertRaises(RuntimeError):
                await rec.start(None)
            await streaming.shutdown()
            await factory.close()
            return result, locked, status, stopped, unlocked, rec.list_sessions(), factory.control
        result, locked, status, stopped, unlocked, sessions, control_msgs = asyncio.run(scenario())
        self.assertEqual(result["session"], "lesson_01")
        self.assertEqual(sorted(result["started"]), ["S1", "S2"])
        self.assertEqual(set(locked.values()), {"録画中"})
        self.assertEqual(set(unlocked.values()), {None})
        self.assertEqual(len(status["active"]), 2)
        self.assertEqual(len(control_msgs), 2, "a key frame is requested per recording")
        for serial, entry in stopped.items():
            self.assertIsNone(entry["error"])
            self.assertGreater(entry["video_packets"], 10)
            with av.open(entry["parts"][0]) as c:
                self.assertEqual(sorted(s.type for s in c.streams), ["audio", "video"])
        self.assertIn("Seat_01_A_", Path(stopped["S1"]["parts"][0]).name)
        data = json.loads((Path(sessions[0]["dir"]) / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(data["devices"]), ["S1", "S2"])
        for serial in ("S1", "S2"):
            entry = data["devices"][serial][0]
            self.assertTrue(entry["imu_file"].endswith(".imu.csv"), "the IMU is recorded next to the video")
            self.assertEqual(Path(entry["imu_file"]).stem, Path(entry["file"]).stem + ".imu")
            self.assertGreaterEqual(entry["imu_rows"], 2)
            self.assertIn("host_time,t_us", Path(entry["imu_file"]).read_text(encoding="utf-8"))
        self.assertIn("imu_rows", status["active"][0])
        self.assertIsNotNone(data["stopped_at"])

    def test_recorder_splits_parts_when_resolution_changes(self):
        import av
        from questhub.record.recorder import Recorder
        from questhub.stream import protocol as p
        from questhub.stream.hub import FrameHub
        small = make_h264(frames=30, width=320, height=180, gop=15)
        big = make_h264(frames=30, width=640, height=360, gop=15)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        async def scenario():
            hub = FrameHub(video_codec="h264")
            rec = Recorder(Path(tmp.name) / "r.mkv", hub, audio=False)
            rec.start()
            for (w, h), (config, frames), base in (((320, 180), small, 0), ((640, 360), big, 2_000_000)):
                hub.publish_video(p.SessionPacket(w, h))
                hub.publish_video(p.MediaPacket(config, None, config=True))
                for pts, key, data in frames:
                    hub.publish_video(p.MediaPacket(data, base + pts, key_frame=key))
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.2)
            return await rec.stop()
        stats = asyncio.run(scenario())
        self.assertEqual(len(stats.parts), 2)
        self.assertTrue(stats.parts[1].endswith("r_part02.mkv"))
        sizes = []
        for part in stats.parts:
            with av.open(part) as c:
                sizes.append((c.streams.video[0].codec_context.width, sum(1 for _ in c.decode(video=0))))
        self.assertEqual(sizes, [(320, 30), (640, 30)])


if __name__ == "__main__":
    unittest.main()
