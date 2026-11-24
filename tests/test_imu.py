"""Headset IMU recording: helper output parsing, clock mapping, reconnects and the shipped DEX."""
import asyncio
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from questhub.imu import recorder as imu  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

HELPER_OUTPUT = """# questhub-imu 2
# sensor name=Syncboss_IMU_Android_Sensor type=65537 string_type=oculus.sensor.hmd_imu.2 vendor=Oculus_VR max_rate_hz=100
# columns t_us,n,temp_c,ax,ay,az,gx,gy,gz
WARNING: linker: something app_process prints
# sync t=1000000 mono_ns=1 wall_ms=2
1005000,8,30.7,0.27,-9.79,-0.86,-0.002,-0.0007,0.0003
1015000,8,30.7,0.28,-9.78,-0.85,-0.002,-0.0006,0.0004
"""


class FakeProcess:
    def __init__(self, lines: list[str]):
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data((line + "\n").encode())
        self.stdout.feed_eof()
        self.returncode = None

    async def wait(self):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class ParserTests(unittest.TestCase):
    def test_rows_wait_for_the_first_sync_and_use_this_computers_clock(self):
        stats = imu.ImuStats()
        parser = imu.ImuParser(imu.ClockMap())
        out = []
        out += parser.feed("# columns t_us,n,temp_c,ax,ay,az,gx,gy,gz", 0, stats)
        out += parser.feed("990000,8,30,0,-9.8,0,0,0,0", 100.0, stats)      # before any sync
        self.assertEqual(out, [])
        out += parser.feed("# sync t=1000000 mono_ns=0 wall_ms=0", 100.010, stats)   # offset 99.010
        out += parser.feed("1500000,8,30,0,-9.8,0,0,0,0", 100.6, stats)
        self.assertEqual([l.split(",")[0] for l in out], ["100.000000", "100.510000"])
        self.assertEqual(stats.rows, 2)
        self.assertEqual(stats.samples, 16)
        # a later sync that arrived faster lowers the offset; a slow one does not raise it
        parser.feed("# sync t=2000000 mono_ns=0 wall_ms=0", 101.005, stats)
        parser.feed("# sync t=3000000 mono_ns=0 wall_ms=0", 102.300, stats)
        self.assertAlmostEqual(parser.clock.to_host(4_000_000), 103.005)

    def test_errors_messages_and_unexpected_columns(self):
        stats = imu.ImuStats()
        parser = imu.ImuParser(imu.ClockMap())
        parser.feed("# error sensor not found: oculus.sensor.hmd_imu.2", 0, stats)
        self.assertEqual(stats.error, "sensor not found: oculus.sensor.hmd_imu.2")
        parser.feed("Aborted", 0, stats)
        self.assertEqual(stats.messages, ["Aborted"])
        stats = imu.ImuStats()
        parser = imu.ImuParser(imu.ClockMap())
        parser.feed("# columns t,n,values...", 0, stats)
        parser.feed("# sync t=1 mono_ns=0 wall_ms=0", 0, stats)
        self.assertEqual(parser.feed("5,1,1.0,2.0", 0, stats), [])
        self.assertIn("unexpected columns", stats.error)

    def test_clock_window_follows_drift(self):
        clock = imu.ClockMap(window=3)
        for i, received in enumerate([10.050, 11.001, 12.002, 13.003, 14.004]):
            clock.add(i * 1_000_000, received)
        self.assertAlmostEqual(clock.to_host(0), 10.002, places=6)   # the 10.001 offset left the window


class RecorderTests(unittest.TestCase):
    def test_writes_csv_and_reconnects_after_a_loss(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "seat01.imu.csv"
        spawned = []
        imu.MAX_BACKOFF = 0.05

        async def spawn(adb):
            spawned.append(adb)
            return FakeProcess(HELPER_OUTPUT.splitlines())

        async def scenario():
            rec = imu.ImuRecorder(path, lambda: "adb-for-S1", rate_hz=100, serial="S1", spawn=spawn,
                                  clock=lambda: 50.0)
            rec.start()
            for _ in range(200):
                if rec.stats.restarts >= 1 and rec.stats.rows >= 4:
                    break
                await asyncio.sleep(0.01)
            return await rec.stop()

        stats = asyncio.run(scenario())
        self.assertGreaterEqual(len(spawned), 2)
        self.assertGreaterEqual(stats.restarts, 1)
        self.assertIsNone(stats.error)
        self.assertIn("hmd_imu.2", stats.sensor)
        text = path.read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if not l.startswith("#")]
        self.assertEqual(lines[0], imu.CSV_COLUMNS)
        self.assertEqual(lines[1], "50.005000,1005000,8,30.7,0.27,-9.79,-0.86,-0.002,-0.0007,0.0003")
        self.assertIn("# restart 1", text)
        self.assertIn("# device S1", text)

    def test_waits_for_a_connection(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        async def scenario():
            rec = imu.ImuRecorder(Path(tmp.name) / "x.imu.csv", lambda: None, spawn=None)
            rec.start()
            await asyncio.sleep(0.05)
            return await rec.stop()

        stats = asyncio.run(scenario())
        self.assertEqual(stats.rows, 0)


class HelperBuildTests(unittest.TestCase):
    def test_shipped_dex_and_remote_name(self):
        data = imu.DEX.read_bytes()
        self.assertTrue(data.startswith(b"dex\n0"), "questhub-imu.dex must be a DEX file")
        self.assertIn(b"Lquesthub/ImuStream;", data)
        self.assertRegex(imu.remote_dex(), r"^/data/local/tmp/questhub-imu-[0-9a-f]{12}\.dex$")
        self.assertEqual(imu.helper_command(100),
                         f"CLASSPATH={imu.remote_dex()} app_process / questhub.ImuStream --rate 100")

    def test_build_script_is_pinned(self):
        script = (ROOT / "scripts/build_imu_helper.sh").read_text(encoding="utf-8")
        self.assertRegex(script, r'R8_SHA256=\$\{R8_SHA256:-"[0-9a-f]{64}"\}')
        self.assertRegex(script, r'OPENJDK_SPEC=\$\{OPENJDK_SPEC:-"openjdk=\d+\.\d+\.\d+')
        self.assertIn("--check", script)

    def test_java_source_matches_the_parser(self):
        source = (ROOT / "questhub/imu/java/questhub/ImuStream.java").read_text(encoding="utf-8")
        self.assertIn('"# columns ' + imu.IMU_COLUMNS + '"', source)
        self.assertIn('"# sync t="', source)
        self.assertIn("public final class ImuStream", source)
        self.assertTrue(re.search(r'HEADSET_IMU = "oculus\.sensor\.hmd_imu\.2"', source))


if __name__ == "__main__":
    unittest.main()
