"""Black capture on Quest: detection, diagnostics parsing, recovery escalation."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from questhub.stream import blackscreen  # noqa: E402
from questhub.stream.blackscreen import BlackState, FrameLuma  # noqa: E402
from tests.media_fixtures import make_h264, make_opus, requires_av  # noqa: E402

STRATA_NORMAL = """--------Strata composer--------
Current strata layer: S2
layer count: 3
 -  S4: 'vr_compositor_with_depth_test_4128x2208' (size: 4128x2208 haveBuffer:false, zOrder: 0, flags: 0x1400, pid: 2696)
 -  S3: 'vr_compositor_protected_4128x2208' (size: 4128x2208 haveBuffer:false, zOrder: 0, flags: 0x5400, pid: 2696)
 -> S2: 'vr_compositor_4128x2208' (size: 4128x2208 haveBuffer:true, zOrder: 2147483646, flags: 0x0, pid: 2696)
ovrState: 0x0 (passthrough: 0, scaling: 0, cac: 0, scaleFactor: 0)
"""


class DetectionLogicTests(unittest.TestCase):
    def test_hysteresis(self):
        state = BlackState(confirm_s=3.0)
        dark, bright = FrameLuma(0.0, 0.0), FrameLuma(110.0, 230.0)
        self.assertIsNone(state.update(dark, now=0.0))
        self.assertIsNone(state.update(dark, now=2.0))
        self.assertEqual(state.update(dark, now=3.1), "black")
        self.assertIsNone(state.update(dark, now=5.0))
        self.assertEqual(state.update(bright, now=6.0), "visible")
        self.assertIsNone(state.update(dark, now=7.0), "a single dark frame is not a black screen")
        self.assertIsNone(state.update(bright, now=8.0))

    def test_dark_scene_is_not_black(self):
        self.assertFalse(FrameLuma(mean=6.0, p99=45.0).black)   # night scene with highlights
        self.assertTrue(FrameLuma(mean=0.4, p99=3.0).black)

    def test_diagnostics_summary_and_cause(self):
        sections = {"compositor": STRATA_NORMAL,
                    "resumed": "topResumedActivity=ActivityRecord{1 u0 com.oculus.browser/.BrowserActivity t1}",
                    "power": "  mWakefulness=Awake\n"}
        s = blackscreen.summarize_diagnostics(sections)
        self.assertEqual((s["strata_layer"], s["passthrough"], s["protected"], s["wakefulness"]), ("S2", False, False, "Awake"))
        self.assertEqual(s["resumed"], ["com.oculus.browser/.BrowserActivity"])
        self.assertIn("描画していない", blackscreen.likely_cause(s))
        passthrough = STRATA_NORMAL.replace("passthrough: 0", "passthrough: 1")
        self.assertIn("パススルー", blackscreen.likely_cause(blackscreen.summarize_diagnostics({"compositor": passthrough})))
        protected = STRATA_NORMAL.replace(" -> S2: 'vr_compositor_4128x2208'", " -  S2: 'vr_compositor_4128x2208'") \
            .replace(" -  S3: 'vr_compositor_protected", " -> S3: 'vr_compositor_protected")
        self.assertIn("保護", blackscreen.likely_cause(blackscreen.summarize_diagnostics({"compositor": protected})))
        self.assertIn("スリープ", blackscreen.likely_cause({"wakefulness": "Asleep"}))
        s = blackscreen.summarize_diagnostics({"vrpower": "Virtual proximity state: OPEN\nisAutosleepDisabled: false"})
        self.assertEqual(s["proximity"], "OPEN")
        self.assertIn("装着していない", blackscreen.likely_cause(s))


@requires_av
class ProbeTests(unittest.TestCase):
    def test_key_frame_luma(self):
        config, frames = make_h264(frames=2, gop=15)          # frame 0 is all black
        luma = blackscreen.KeyFrameProbe().measure(config, frames[0][2])
        self.assertTrue(luma.black, luma)
        bright_config, bright = make_h264(frames=2, gop=15, detail=True)
        luma = blackscreen.KeyFrameProbe().measure(bright_config, bright[0][2])
        self.assertFalse(luma.black, luma)
        self.assertIsNone(blackscreen.KeyFrameProbe().measure(b"", b"garbage"))


@requires_av
class SupervisorRecoveryTests(unittest.TestCase):
    def test_black_detected_diagnosed_recovered(self):
        from questhub.core.events import EventBus
        from questhub.stream import supervisor as supmod
        from questhub.stream.scrcpy_server import StreamOptions
        from tests.fakes import FakeAdbWorld, FakeHeadset
        from tests.test_services_media import FakeFactory

        video, audio = make_h264(frames=60, gop=5), make_opus(packets=100)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        brightness = {"value": FrameLuma(0.0, 0.0)}

        class FakeProbe:
            def measure(self, config, key_frame):
                return brightness["value"]

        async def scenario():
            world = FakeAdbWorld([FakeHeadset("S1", "10.0.0.1")])
            bus = EventBus()
            factory = FakeFactory(video, audio, realtime=True)
            sup = supmod.StreamSupervisor("S1", lambda: "10.0.0.1:5555", bus, StreamOptions.from_preset("standard"),
                                          adb=world.adb(), wake=False, black_detection=True,
                                          diag_dir=Path(tmp.name), probe_factory=FakeProbe)
            sup.black.confirm_s = 0.2
            sup._server_factory = factory(sup)
            with mock.patch.object(blackscreen, "RECOVERY_STEPS", ((0.0, "reset_video"), (0.2, "wake"), (0.4, "restart"))):
                sup.start()
                await sup.wait_streaming(10)
                for _ in range(100):
                    if sup.last_recovery == "restart":
                        break
                    await asyncio.sleep(0.05)
                await sup.wait_streaming(10)
                status_black = sup.status()
                brightness["value"] = FrameLuma(100.0, 220.0)
                for _ in range(100):
                    if not sup.black.black:
                        break
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.2)
            await sup.stop()
            await factory.close()
            events = [e for e in bus.recent(300) if e["type"] == "media_black"]
            shell = [cmd for h in world.headsets.values() for cmd in h.commands]
            return status_black, events, shell, len(factory.started_targets), list(Path(tmp.name).glob("black_*.txt")), \
                factory.control
        status, events, shell, starts, diag_files, control = asyncio.run(scenario())
        self.assertTrue(status["black"])
        self.assertEqual(status["black_events"], 1)
        steps = [e["data"].get("recovery") for e in events if e["data"].get("recovery")]
        self.assertEqual(steps[:3], ["reset_video", "wake", "restart"])
        self.assertTrue(control, "RESET_VIDEO sent")
        self.assertIn("am broadcast -a com.oculus.vrpowermanager.prox_close", shell)
        self.assertIn("input keyevent KEYCODE_WAKEUP", shell)
        self.assertGreaterEqual(starts, 2, "server restarted")
        self.assertEqual(len(diag_files), 1)
        self.assertIn("likely cause", diag_files[0].read_text())
        self.assertTrue(any(e["data"].get("cause") for e in events))
        self.assertEqual(events[-1]["data"].get("black"), False)
        self.assertEqual(events[-1]["data"].get("recovered_by"), "restart")
        self.assertIn("am broadcast -a com.oculus.vrpowermanager.automation_disable", shell,
                      "the forced 'worn' state is released when the stream stops")


if __name__ == "__main__":
    unittest.main()
