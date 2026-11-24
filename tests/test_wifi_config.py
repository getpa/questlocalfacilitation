"""Joining USB headsets to a Wi-Fi network (cmd wifi connect-network)."""
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from questhub.core.events import EventBus  # noqa: E402
from questhub.devices import wifi_config  # noqa: E402
from questhub.devices.manager import DeviceManager  # noqa: E402
from questhub.devices.registry import Registry  # noqa: E402
from questhub.devices.wifi_config import WifiConfigError, WifiTarget  # noqa: E402
from questhub.services.provisioning import ProvisioningService  # noqa: E402
from tests.fakes import FakeAdbWorld, FakeHeadset, TrackFeed, until  # noqa: E402

SECRET = "pa ss'w$rd\"`x"


class ValidationTests(unittest.TestCase):
    def test_validation(self):
        WifiTarget("lab", "wpa2", "12345678").validate()
        WifiTarget("cafe", "open").validate()
        for bad in (WifiTarget("", "wpa2", "12345678"), WifiTarget("x" * 33, "wpa2", "12345678"),
                    WifiTarget("lab", "wpa2", "short"), WifiTarget("lab", "wep", "12345678"),
                    WifiTarget("lab", "open", "12345678"), WifiTarget("lab\nx", "wpa2", "12345678")):
            with self.assertRaises(WifiConfigError, msg=bad):
                bad.validate()

    def test_script_quotes_for_sh(self):
        import shlex
        script = wifi_config.connect_script(WifiTarget("my lab's AP", "wpa3", SECRET))
        self.assertEqual(shlex.split(script), ["cmd", "wifi", "connect-network", "my lab's AP", "wpa3", SECRET])
        self.assertEqual(shlex.split(wifi_config.connect_script(WifiTarget("cafe", "open"))),
                         ["cmd", "wifi", "connect-network", "cafe", "open"])

    def test_saved_creators(self):
        dump = ('ID: 4 SSID: "lab" PROVIDER-NAME: null\n cuid=10067 cname=com.oculus.panelapp.settings luid=10067\n'
                '* ID: 5 SSID: "current" PROVIDER-NAME: null\n cuid=10059 cname=com.oculus.q4b.mdm luid=10059\n'
                'ID: 6 SSID: "mine" PROVIDER-NAME: null\n cuid=2000 cname=com.android.shell luid=2000\n')
        self.assertEqual(wifi_config.parse_saved_creators(dump), {
            "lab": (10067, "com.oculus.panelapp.settings"), "current": (10059, "com.oculus.q4b.mdm"),
            "mine": (2000, "com.android.shell")})

    def test_command_failure_detection(self):
        self.assertIsNone(wifi_config.command_failed(""))
        self.assertIsNone(wifi_config.command_failed("Connection initiated with netId 12\n"))
        self.assertTrue(wifi_config.command_failed("Exception occurred while executing 'connect-network':"))


class ServiceTests(unittest.TestCase):
    def test_join_network_then_enable_adb_without_ssid_warning(self):
        async def scenario():
            headset = FakeHeadset("SER1", "10.0.0.5", ssid="old-net")
            headset.networks["new-net"] = ("wpa2", SECRET)
            world = FakeAdbWorld([headset])
            bus = EventBus()
            reg = Registry(":memory:", bus)
            feed = TrackFeed()
            mgr = DeviceManager(reg, bus, world.adb(), track_source=feed.source, auto_reconnect=False)
            await mgr.start()
            feed.push(("SER1", "device"))
            await until(lambda: mgr.status.get("SER1") and mgr.status["SER1"].state == "usb")
            service = ProvisioningService(mgr, reg)
            reg.observe("SER1", "usb", ip="10.0.0.5", wifi=wifi_config.WifiInfo(ssid="old-net"))
            ok = await service.configure_wifi(["SER1"], WifiTarget("new-net", "wpa2", SECRET), enable_adb=True)
            headset.networks["other-net"] = ("wpa2", "rightpass1")
            wrong = await service.configure_wifi(["SER1"], WifiTarget("other-net", "wpa2", "wrongpass1"))
            headset.shell_output["Configured networks"] = (
                'ID: 3 SSID: "mdm-net" PROVIDER-NAME: null\n cuid=10059 cname=com.oculus.q4b.mdm luid=10059\n')
            headset.networks["mdm-net"] = ("wpa2", "rightpass1")
            foreign = await service.configure_wifi(["SER1"], WifiTarget("mdm-net", "wpa2", "rightpass1"))
            self.assertFalse(any("mdm-net" in s for s in headset.stdin_scripts), "nothing sent for a foreign config")
            await mgr.stop()
            self.assertFalse(foreign[0]["ok"])
            self.assertIn("com.oculus.q4b.mdm", foreign[0]["error"])
            self.assertEqual(reg.get("SER1")["ssid_baseline"], "new-net", "baseline restored after refusal")
            return ok, wrong, reg, bus, world, headset
        from unittest import mock
        with mock.patch.object(wifi_config, "CONNECT_TIMEOUT", 0.3), mock.patch.object(wifi_config, "POLL_INTERVAL", 0.05):
            ok, wrong, reg, bus, world, headset = asyncio.run(scenario())
        self.assertTrue(ok[0]["ok"], ok)
        self.assertTrue(ok[0]["adb"]["ok"], "switched to Wi-Fi ADB after joining")
        self.assertTrue(headset.tcpip)
        self.assertEqual(reg.get("SER1")["ssid"], "new-net")
        self.assertEqual(reg.get("SER1")["ssid_baseline"], "new-net")
        self.assertEqual([w for w in reg.warnings() if w["kind"] == "ssid_changed"], [],
                         "an intentional change is not an SSID warning")
        self.assertFalse(wrong[0]["ok"])
        self.assertIn("接続できませんでした", wrong[0]["error"])
        # The passphrase only travels on stdin: never in adb argv, events or results.
        import shlex
        self.assertTrue(any(SECRET in shlex.split(s) for s in headset.stdin_scripts))
        self.assertFalse(any(SECRET in " ".join(map(str, args)) for _, args in world.log))
        self.assertNotIn(SECRET, repr(bus.recent(500)))
        self.assertNotIn(SECRET, repr(ok) + repr(wrong))


if __name__ == "__main__":
    unittest.main()
