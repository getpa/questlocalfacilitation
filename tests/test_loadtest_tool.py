"""Smoke test for tools/loadtest.py (replay devices feed hubs in real time)."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.media_fixtures import HAVE_AV, make_h264, make_opus, requires_av  # noqa: E402


def load_tool():
    spec = importlib.util.spec_from_file_location("loadtest", ROOT / "tools/loadtest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@requires_av
class LoadToolTests(unittest.TestCase):
    def test_replay_devices_stream_in_real_time(self):
        tool = load_tool()
        video = make_h264(frames=30, gop=15)
        audio = make_opus(packets=50)

        async def scenario():
            devices = [tool.ReplayDevice(i, video, audio, 320, 180) for i in range(3)]
            for device in devices:
                await device.start()
            await asyncio.sleep(1.2)
            return [(d.hub.video_stats.packets, d.hub.video_stats.key_frames, d.hub.audio_stats.packets,
                     d.hub.session.width) for d in devices]

        for packets, keys, audio_packets, width in asyncio.run(scenario()):
            self.assertTrue(25 <= packets <= 45, packets)  # ~30 fps, paced, wraps the 1 s clip
            self.assertGreaterEqual(keys, 1)
            self.assertGreater(audio_packets, 30)
            self.assertEqual(width, 320)


if __name__ == "__main__":
    unittest.main()
