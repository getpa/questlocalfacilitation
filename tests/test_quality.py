"""Quality presets: values, bandwidth estimates, CLI parity, runtime switching."""
import asyncio
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from questhub.stream.quality import PRESETS, bandwidth_table, get_preset  # noqa: E402
from questhub.stream.scrcpy_server import StreamOptions  # noqa: E402
from tests.media_fixtures import FakeScrcpyServer, make_h264, make_opus, requires_av  # noqa: E402


class PresetTests(unittest.TestCase):
    def test_three_presets_and_estimates(self):
        self.assertEqual(list(PRESETS), ["low", "standard", "high"])
        self.assertEqual(get_preset("low").mbps(), 1.68)
        self.assertEqual(get_preset("standard").mbps(), 4.25)
        self.assertEqual(get_preset("high").mbps(), 10.43)
        self.assertEqual(get_preset("standard").total_mbps(50), 212.5)
        self.assertLess(get_preset("low").mbps(audio=False), get_preset("low").mbps())
        with self.assertRaises(ValueError):
            get_preset("ultra")

    def test_from_preset_and_overrides(self):
        low = StreamOptions.from_preset("low")
        self.assertEqual((low.quality, low.max_size, low.max_fps, low.video_bit_rate), ("low", 960, 15, 1_500_000))
        self.assertIn("video_bit_rate=1500000", low.server_args(1))
        self.assertEqual(StreamOptions.from_preset("high", max_fps=30).quality, "custom")
        self.assertEqual(StreamOptions.from_preset("standard", max_fps=30, audio=False).quality, "standard")

    def test_cli_uses_the_same_values(self):
        script = (ROOT / "legacy/quest_multi_scrcpy.sh").read_text()
        units = {"K": 1_000, "M": 1_000_000}
        for name, size, fps, rate in re.findall(r"(\w+)\) size=(\d+); fps=(\d+); rate=(\d+[KM]) ;;", script):
            preset = get_preset(name)
            self.assertEqual((preset.max_size, preset.max_fps), (int(size), int(fps)), name)
            self.assertEqual(preset.video_bit_rate, int(rate[:-1]) * units[rate[-1]], name)

    def test_docs_contain_the_bandwidth_table(self):
        for doc in ("README.md", "docs/GUI_DESIGN.md"):
            self.assertIn(bandwidth_table(), (ROOT / doc).read_text(), doc)


@requires_av
class RuntimeSwitchTests(unittest.TestCase):
    def test_set_quality_restarts_without_backoff_and_blocks_while_recording(self):
        from questhub.poc.session import DeviceSession
        from questhub.stream.scrcpy_server import ScrcpyConnection, read_packets
        video, audio = make_h264(frames=90, gop=15), make_opus(packets=150)
        started_with = []
        fakes = []

        class Adapter:
            def __init__(self, options):
                self.options = options

            async def start(self):
                started_with.append(self.options)
                fake = FakeScrcpyServer(video, audio, realtime=True)
                await fake.__aenter__()
                fakes.append(fake)
                self.connection = ScrcpyConnection()
                await self.connection.open("127.0.0.1", fake.port, timeout=5)
                return self.connection

            async def run(self, on_video, on_audio):
                async def pump(name, handler, session):
                    async for item in read_packets(self.connection.reader(name), session):
                        handler(item)
                await asyncio.gather(pump("video", on_video, True), pump("audio", on_audio, False))

            async def request_key_frame(self):
                pass

            async def stop(self):
                await self.connection.close()

        class NoAdb:
            binary, serial = "adb", "fake"

            async def shell(self, *a, **k):
                from questhub.adb.client import AdbError
                raise AdbError(a, 1, "none")

        async def scenario():
            session = DeviceSession("fake", StreamOptions.from_preset("standard"), adb=NoAdb())
            session._server_factory = lambda: Adapter(session.options)
            await session.start(wait=5)
            status = await session.set_quality("low")
            for _ in range(100):
                if session.state == "streaming" and len(started_with) == 2:
                    break
                await asyncio.sleep(0.05)
            result = (status["current"], session.state, session.restarts, [o.max_size for o in started_with])
            await session.start_recording(Path(self.tmp.name) / "r.mkv")
            with self.assertRaises(RuntimeError):
                await session.set_quality("high")
            await session.stop()
            for fake in fakes:
                await fake.__aexit__(None, None, None)
            return result

        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        current, state, restarts, sizes = asyncio.run(scenario())
        self.assertEqual(current, "low")
        self.assertEqual(state, "streaming")
        self.assertEqual(restarts, 0, "a quality change is not a failure")
        self.assertEqual(sizes, [1280, 960])


if __name__ == "__main__":
    unittest.main()
