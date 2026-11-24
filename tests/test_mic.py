"""Headset microphone as a separate recording track."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from questhub.stream.mic import MIC_SOURCES, MicCapture, mic_options  # noqa: E402
from questhub.stream.scrcpy_server import StreamOptions  # noqa: E402
from tests.media_fixtures import make_h264, make_opus, requires_av  # noqa: E402


class MicOptionsTests(unittest.TestCase):
    def test_second_session_is_audio_only_and_leaves_the_device_alone(self):
        args = dict(a.split("=", 1) for a in mic_options("mic").server_args(0x1234))
        self.assertEqual(args["video"], "false")
        self.assertEqual(args["control"], "false")
        self.assertEqual(args["audio"], "true")
        self.assertEqual(args["audio_source"], "mic")
        self.assertNotIn("audio_dup", args, "audio_dup is only valid for the playback source")
        self.assertEqual((args["stay_awake"], args["power_on"], args["cleanup"]), ("false", "false", "false"))
        self.assertIn("mic-voice-communication", MIC_SOURCES)
        with self.assertRaises(ValueError):
            mic_options("output")

    def test_display_session_is_unchanged(self):
        args = dict(a.split("=", 1) for a in StreamOptions().server_args(1))
        self.assertEqual((args["video"], args["audio_source"], args["audio_dup"]), ("true", "playback", "true"))

    def test_reconnects_and_reports_errors(self):
        attempts = []

        class Failing:
            async def start(self):
                attempts.append(1)
                raise ConnectionError("device offline")

            async def stop(self):
                pass

        import questhub.stream.mic as micmod

        async def scenario():
            with mock.patch.object(micmod, "MAX_BACKOFF", 0.01), mock.patch.object(micmod.asyncio, "sleep",
                                                                                    _fast_sleep):
                cap = MicCapture(lambda: "adb", server_factory=lambda adb: Failing())
                cap.start()
                for _ in range(100):
                    if len(attempts) >= 3:
                        break
                    await _real_sleep(0.01)
                return await cap.stop()

        stats = asyncio.run(scenario())
        self.assertGreaterEqual(len(attempts), 3)
        self.assertEqual(stats.error, "device offline")


_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *args):
    await _real_sleep(min(delay, 0.001))


@requires_av
class RecorderMicTrackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video = make_h264(frames=60, gop=15)
        cls.audio = make_opus(packets=100)

    def _record(self, mic_delay: float | None, mic_wait: float = 6.0):
        import av
        from questhub.record import recorder as recmod
        from questhub.stream.hub import FrameHub
        from questhub.stream.protocol import MediaPacket, SessionPacket
        config, frames = self.video
        head, packets = self.audio
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "seat.mkv"

        async def scenario():
            hub, mic = FrameHub(video_codec="h264", audio_codec="opus"), FrameHub(audio_codec="opus")
            with mock.patch.object(recmod, "MIC_WAIT_S", mic_wait):
                rec = recmod.Recorder(path, hub, audio=True, mic=mic)
                rec.start()
                hub.publish_video(SessionPacket(320, 180, False))
                hub.publish_video(MediaPacket(config, None, config=True))
                hub.publish_audio(MediaPacket(head, None, config=True))
                loop = asyncio.get_running_loop()
                start = loop.time()
                mic_started = False
                for i, (pts, key, data) in enumerate(frames):
                    if mic_delay is not None and not mic_started and loop.time() - start >= mic_delay:
                        mic.publish_audio(MediaPacket(head, None, config=True))
                        mic_started = True
                    hub.publish_video(MediaPacket(data, pts, key_frame=key))
                    for apts, adata in packets:
                        if pts <= apts < pts + 33_334:
                            hub.publish_audio(MediaPacket(adata, apts))
                            if mic_started:
                                mic.publish_audio(MediaPacket(adata, apts))
                    await asyncio.sleep(1 / 30)
                return await rec.stop()

        stats = asyncio.run(scenario())
        with av.open(str(path)) as c:
            kinds = [(s.type, s.metadata.get("title")) for s in c.streams]
        return stats, kinds

    def test_late_microphone_becomes_a_third_track(self):
        stats, kinds = self._record(mic_delay=0.5)
        self.assertIsNone(stats.error)
        self.assertTrue(stats.mic_track)
        self.assertGreater(stats.mic_packets, 10)
        self.assertEqual(kinds, [("video", None), ("audio", "Headset audio"), ("audio", "Microphone")])
        self.assertGreater(stats.video_packets, 30, "video waiting for the microphone is kept, not dropped")

    def test_missing_microphone_does_not_block_the_recording(self):
        stats, kinds = self._record(mic_delay=None, mic_wait=0.5)
        self.assertIsNone(stats.error)
        self.assertFalse(stats.mic_track)
        self.assertEqual([k for k, _ in kinds], ["video", "audio"])
        self.assertGreater(stats.video_packets, 30)


if __name__ == "__main__":
    unittest.main()
