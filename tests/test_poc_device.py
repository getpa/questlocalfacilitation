"""PoC acceptance on a real headset (docs/GUI_DESIGN.md §7, P1/P3/P6/P9).

Skipped unless QH_TEST_DEVICE names an adb serial that is already connected:

  QH_TEST_DEVICE=192.168.22.201:5555 ./.mamba-env/bin/python -m unittest tests.test_poc_device -v
"""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.media_fixtures import HAVE_AV  # noqa: E402

SERIAL = os.environ.get("QH_TEST_DEVICE")
requires_device = unittest.skipUnless(SERIAL and HAVE_AV, "set QH_TEST_DEVICE=<adb serial> (and use .mamba-env)")

if HAVE_AV:
    import av
    from questhub.adb.client import Adb
    from questhub.devices.telemetry import read_wifi
    from questhub.poc import session as session_mod
    from questhub.poc.session import DeviceSession
    from questhub.stream.protocol import SessionPacket
    from questhub.stream.scrcpy_server import ScrcpyServer, StreamOptions


@requires_device
class DeviceTests(unittest.TestCase):
    def test_p1_p3_p6_stream_key_frames_and_audio(self):
        async def scenario():
            server = ScrcpyServer(Adb(serial=SERIAL), StreamOptions(key_frame_interval=1))
            conn = await server.start()
            t0 = time.monotonic()
            seen = {"video": 0, "keys": [], "audio": 0, "audio_config": None, "session": None}

            def on_video(item):
                if isinstance(item, SessionPacket):
                    seen["session"] = (item.width, item.height)
                elif not item.config:
                    seen["video"] += 1
                    if item.key_frame:
                        seen["keys"].append(time.monotonic() - t0)

            def on_audio(item):
                if item.config:
                    seen["audio_config"] = item.data
                else:
                    seen["audio"] += 1

            task = asyncio.create_task(server.run(on_video, on_audio))
            await asyncio.sleep(6)
            keys_before = len(seen["keys"])
            requested = time.monotonic() - t0
            await server.request_key_frame()
            await asyncio.sleep(1.5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await server.stop()
            return conn, seen, keys_before, requested, time.monotonic() - t0

        conn, seen, keys_before, requested, elapsed = asyncio.run(scenario())
        # P1: the official 5.0.1 server speaks the protocol we implemented.
        self.assertEqual(conn.video_codec, "h264")
        self.assertTrue(conn.device_name)
        self.assertIsNotNone(seen["session"])
        self.assertGreaterEqual(seen["video"] / elapsed, 15, "video frame rate too low")
        # P3: periodic key frames (requested 1 s; Quest encoders round up to ~2 s) and RESET_VIDEO.
        self.assertGreaterEqual(keys_before, 3)
        gaps = [b - a for a, b in zip(seen["keys"], seen["keys"][1:keys_before])]
        self.assertLessEqual(max(gaps), 3.5, f"key frame gaps {gaps}")
        after = [t for t in seen["keys"][keys_before:] if t >= requested]
        self.assertTrue(after and after[0] - requested < 1.0, "RESET_VIDEO did not yield a key frame within 1 s")
        # P6: playback capture with audio_dup (Android 13+).
        self.assertEqual(conn.audio_codec, "opus")
        self.assertTrue(seen["audio_config"].startswith(b"OpusHead"))
        self.assertGreater(seen["audio"], 100)

    def test_p9_wifi_ssid(self):
        info = asyncio.run(read_wifi(Adb(serial=SERIAL)))
        self.assertIsNotNone(info)
        self.assertTrue(info.ssid, "SSID is hidden or empty")
        self.assertIn(info.band, ("2.4GHz", "5GHz", "6GHz"))
        self.assertIsNotNone(info.rssi_dbm)

    def test_recording_and_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        async def scenario():
            session = DeviceSession(SERIAL, StreamOptions(), record_dir=tmp.name)
            old_delay, session_mod.RETRY_DELAY = session_mod.RETRY_DELAY, 1.0
            try:
                await session.start(wait=20)
                path = await session.start_recording()
                await asyncio.sleep(5)
                stats = await session.stop_recording()
                # Kill the server on the headset: the session must restart on its own.
                await Adb(serial=SERIAL).shell("pkill -f com.genymobile.scrcpy.Server", check=False)
                deadline = time.monotonic() + 20
                packets = session.hub.video_stats.packets
                while time.monotonic() < deadline:
                    if session.restarts >= 1 and session.state == "streaming" \
                            and session.hub.video_stats.packets > packets + 10:
                        break
                    await asyncio.sleep(0.25)
                return path, stats, session.restarts, session.state
            finally:
                session_mod.RETRY_DELAY = old_delay
                await session.stop()

        path, stats, restarts, state = asyncio.run(scenario())
        self.assertIsNone(stats["error"])
        with av.open(path) as c:
            self.assertEqual(sorted(s.type for s in c.streams), ["audio", "video"])
            duration = c.duration / 1e6
        with av.open(path) as c:
            frames = sum(1 for _ in c.decode(video=0))
        # The recording requests a key frame, so it should cover almost all 5 s.
        self.assertGreater(duration, 4.0)
        self.assertGreater(frames, 4 * 15)
        self.assertGreaterEqual(restarts, 1)
        self.assertEqual(state, "streaming")


if __name__ == "__main__":
    unittest.main()
