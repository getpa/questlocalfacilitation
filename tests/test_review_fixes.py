"""Regression tests for the PR #5 review findings."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from questhub.config import Settings  # noqa: E402
from questhub.core.events import EventBus  # noqa: E402
from questhub.devices.manager import DeviceManager  # noqa: E402
from questhub.devices.registry import Registry  # noqa: E402
from questhub.services.streaming import StreamingService  # noqa: E402
from questhub.services.telemetry import TelemetryService  # noqa: E402
from tests.fakes import FakeAdbWorld, FakeHeadset, TrackFeed  # noqa: E402
from tests.media_fixtures import requires_av  # noqa: E402


class DummySup:
    def __init__(self):
        self.locked_reason = None
        self.stopped = self.paused = self.resumed = 0
        self.options = None

    def start(self):
        pass

    async def stop(self):
        self.stopped += 1

    async def pause(self):
        self.paused += 1

    def resume(self):
        self.resumed += 1

    def close(self):
        pass


def make_streaming(world=None):
    bus = EventBus()
    reg = Registry(":memory:", bus)
    mgr = DeviceManager(reg, bus, (world or FakeAdbWorld([])).adb(), track_source=TrackFeed().source,
                        auto_reconnect=False)
    sups = {}
    streaming = StreamingService(mgr, reg, Settings(), supervisor_factory=lambda s: sups.setdefault(s, DummySup()))
    return streaming, sups, mgr, reg


class StopWhileRecordingTests(unittest.TestCase):
    def test_stop_and_stop_all_refused_while_recording(self):
        async def scenario():
            streaming, sups, _, _ = make_streaming()
            streaming.start_stream("S1")
            streaming.start_stream("S2")
            sups["S1"].locked_reason = "録画中"
            with self.assertRaises(RuntimeError):
                await streaming.stop_stream("S1")
            with self.assertRaises(RuntimeError):
                await streaming.stop_all()
            await streaming.stop_stream("S2")
            stopped_before_force = sups["S1"].stopped
            await streaming.stop_all(force=True)
            return stopped_before_force, sups["S1"].stopped, sups["S2"].stopped
        before, after, other = asyncio.run(scenario())
        self.assertEqual((before, after), (0, 1))
        self.assertEqual(other, 2)


class NativeLaunchFailureTests(unittest.TestCase):
    def test_capture_resumes_when_scrcpy_cannot_start(self):
        async def scenario():
            world = FakeAdbWorld([FakeHeadset("S1", "10.0.0.1")])
            streaming, sups, mgr, _ = make_streaming(world)
            st = mgr.get("S1")
            st.endpoint, st.wifi_state = "10.0.0.1:5555", "device"
            streaming.start_stream("S1")
            with mock.patch("questhub.services.streaming.paths.binary", side_effect=FileNotFoundError("scrcpy")):
                with self.assertRaises(FileNotFoundError):
                    await streaming.open_native("S1")
            return sups["S1"], streaming.native_open("S1")
        sup, native = asyncio.run(scenario())
        self.assertEqual((sup.paused, sup.resumed), (1, 1))
        self.assertFalse(native)


class TelemetryUnavailableTests(unittest.TestCase):
    def test_unreadable_wifi_clears_ssid_and_warns(self):
        async def scenario():
            headset = FakeHeadset("S1", "10.0.0.1", ssid="lab")
            world = FakeAdbWorld([headset])
            bus = EventBus()
            reg = Registry(":memory:", bus)
            mgr = DeviceManager(reg, bus, world.adb(), track_source=TrackFeed().source, auto_reconnect=False)
            st = mgr.get("S1")
            st.endpoint, st.wifi_state = "10.0.0.1:5555", "device"
            telemetry = TelemetryService(mgr, reg)
            await telemetry.poll("S1")
            first = reg.get("S1")["ssid"]
            from tests import fakes
            with mock.patch.object(fakes, "WIFI_STATUS", "Wifi is disabled"):
                await telemetry.poll("S1")
            return first, reg.get("S1"), [w["kind"] for w in reg.warnings()]
        first, row, kinds = asyncio.run(scenario())
        self.assertEqual(first, "lab")
        self.assertIsNone(row["ssid"])
        self.assertEqual(row["battery"], 80)
        self.assertEqual(kinds, ["wifi_unavailable"])


@requires_av
class RecordingNamesTests(unittest.TestCase):
    def test_same_alias_and_reused_session_name(self):
        from questhub.services.recording import RecordingService
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        class Hub:
            audio_codec = None
            video_codec = "h264"

            def subscribe(self, **kwargs):
                class Sub:
                    def __aiter__(self):
                        return self

                    async def __anext__(self):
                        await asyncio.sleep(3600)

                    def close(self):
                        pass
                return Sub()

        async def scenario():
            streaming, sups, _, reg = make_streaming()
            for s in ("SERIALAAA111", "SERIALBBB222"):
                reg.observe(s, "adb")
                reg.update(s, alias="Same Name")
                sup = streaming.start_stream(s)
                sup.hub = Hub()
                sup.running = True
                from questhub.stream.scrcpy_server import StreamOptions
                sup.options = StreamOptions.from_preset("standard")

                async def request_key_frame():
                    pass
                sup.request_key_frame = request_key_frame
            settings = Settings(record_dir=tmp.name, disk_min_free_gb=0.0)
            rec = RecordingService(streaming, reg, settings)
            first = await rec.start(None, "lesson")
            paths1 = [str(r.path) for r in rec.active.values()]
            for r in rec.active.values():
                r._sub.close()
            rec.active.clear()
            rec._write_session()
            first_json = Path(rec.session["dir"]) / "session.json"
            rec.session = None
            second = await rec.start(None, "lesson")
            for r in rec.active.values():
                r._sub.close()
            return first, second, paths1, first_json.exists()
        first, second, paths1, kept = asyncio.run(scenario())
        self.assertEqual(len(set(paths1)), 2, "equal aliases must not share a file")
        self.assertEqual((first["session"], second["session"]), ("lesson", "lesson_2"))
        self.assertTrue(kept, "the earlier session.json is preserved")


if __name__ == "__main__":
    unittest.main()
