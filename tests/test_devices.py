"""M2: registry (SSID warnings, TSV), tracker parsing, manager, provisioning, telemetry."""
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from questhub.adb.tracker import parse_device_list, read_frames  # noqa: E402
from questhub.core import events as ev  # noqa: E402
from questhub.core.events import EventBus  # noqa: E402
from questhub.devices import discovery, provisioning  # noqa: E402
from questhub.devices.manager import DeviceManager  # noqa: E402
from questhub.devices.registry import Registry  # noqa: E402
from questhub.devices.telemetry import parse_battery  # noqa: E402
from questhub.devices.wifi_parse import WifiInfo  # noqa: E402
from questhub.services.provisioning import ProvisioningService  # noqa: E402
from questhub.services.telemetry import TelemetryService  # noqa: E402
from tests.fakes import FakeAdbWorld, FakeHeadset, TrackFeed, until  # noqa: E402


def wifi(ssid, n=1):
    return WifiInfo(ssid=ssid, bssid=f"6e:00:00:00:00:{n:02x}", rssi_dbm=-40, freq_mhz=5180, link_mbps=1200)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.reg = Registry(":memory:", self.bus)

    def test_observe_assigns_alias_and_tracks_ip_changes(self):
        row = self.reg.observe("S1", "adb", ip="10.0.0.5", port=5555, model="Quest 3")
        self.assertEqual((row["alias"], row["ip"], row["model"]), ("Quest-01", "10.0.0.5", "Quest 3"))
        self.assertEqual(self.reg.observe("S2", "adb", ip="10.0.0.6")["alias"], "Quest-02")
        self.reg.observe("S1", "scan", ip="10.0.0.9")
        self.assertEqual(self.reg.get("S1")["ip"], "10.0.0.9")
        self.assertIn(ev.IP_CHANGED, [e["type"] for e in self.bus.recent()])

    def test_endpoint_collision_releases_old_owner(self):
        self.reg.observe("S1", "adb", ip="10.0.0.5")
        self.reg.observe("S2", "adb", ip="10.0.0.5")
        self.assertIsNone(self.reg.get("S1")["ip"])
        self.assertEqual(self.reg.get("S2")["ip"], "10.0.0.5")

    def test_manual_ip_is_locked_against_scans_but_not_usb(self):
        self.reg.observe("S1", "adb", ip="10.0.0.5")
        self.reg.update("S1", ip="10.0.0.50")
        self.reg.observe("S1", "scan", ip="10.0.0.6")
        self.assertEqual(self.reg.get("S1")["ip"], "10.0.0.50")
        self.reg.observe("S1", "usb", ip="10.0.0.7")
        row = self.reg.get("S1")
        self.assertEqual((row["ip"], row["ip_locked"]), ("10.0.0.7", 0))

    def test_any_ssid_baseline_then_change_warns_until_acked(self):
        self.reg.observe("S1", "telemetry", wifi=wifi("guest-net"))
        self.assertEqual(self.reg.warnings(), [], "any SSID is accepted")
        self.reg.observe("S1", "telemetry", wifi=wifi("guest-net"))
        self.reg.observe("S1", "telemetry", wifi=wifi("phone-hotspot"))
        warnings = self.reg.warnings()
        self.assertEqual([(w["kind"], w["data"]["old"], w["data"]["new"]) for w in warnings],
                         [("ssid_changed", "guest-net", "phone-hotspot")])
        self.reg.observe("S1", "telemetry", wifi=wifi("phone-hotspot"))
        self.assertEqual(len(self.reg.warnings()), 1, "no duplicate warning")
        self.reg.ack_warning(warnings[0]["id"])
        self.assertEqual(self.reg.get("S1")["ssid_baseline"], "phone-hotspot")
        self.reg.observe("S1", "telemetry", wifi=wifi("phone-hotspot"))
        self.assertEqual(self.reg.warnings(), [])

    def test_returning_to_baseline_clears_warning_and_new_run_resets(self):
        self.reg.observe("S1", "telemetry", wifi=wifi("a"))
        self.reg.observe("S1", "telemetry", wifi=wifi("b"))
        self.reg.observe("S1", "telemetry", wifi=wifi("a"))
        self.assertEqual(self.reg.warnings(), [])
        self.reg.start_run()
        self.reg.observe("S1", "telemetry", wifi=wifi("b"))
        self.assertEqual(self.reg.warnings(), [], "a new run takes the first SSID as baseline")

    def test_wifi_unavailable_warning(self):
        self.reg.observe("S1", "telemetry", wifi=wifi("a"))
        self.reg.observe("S1", "telemetry", wifi=WifiInfo(ssid=None))
        self.assertEqual([w["kind"] for w in self.reg.warnings()], ["wifi_unavailable"])
        self.reg.observe("S1", "telemetry", wifi=wifi("a"))
        self.assertEqual(self.reg.warnings(), [])

    def test_tsv_round_trip_and_placeholder_merge(self):
        n = self.reg.import_tsv("# alias\tendpoint\nSeat-01\t10.0.0.21:5555\t2\t0\nSeat-02\t10.0.0.22\n")
        self.assertEqual(n, 2)
        self.assertIsNotNone(self.reg.get("tsv:10.0.0.21:5555"))
        self.reg.observe("REAL1", "adb", ip="10.0.0.21", port=5555)
        self.assertIsNone(self.reg.get("tsv:10.0.0.21:5555"))
        real = self.reg.get("REAL1")
        self.assertEqual((real["alias"], real["grid_col"], real["grid_row"]), ("Seat-01", 2, 0))
        tsv = self.reg.export_tsv()
        self.assertIn("Seat-01\t10.0.0.21:5555\t2\t0", tsv)
        self.assertIn("Seat-02\t10.0.0.22:5555", tsv)
        self.assertIn("lab", Registry(":memory:").export_network_csv() + "lab")

    def test_observation_history_is_bounded(self):
        from questhub.devices import registry as regmod
        old, regmod.MAX_OBSERVATIONS_PER_DEVICE = regmod.MAX_OBSERVATIONS_PER_DEVICE, 5
        try:
            for i in range(12):
                self.reg.observe("S1", "scan", ip=f"10.0.0.{i}")
            self.assertEqual(len(self.reg.observations("S1", 100)), 5)
        finally:
            regmod.MAX_OBSERVATIONS_PER_DEVICE = old


class ParsingTests(unittest.TestCase):
    def test_track_devices_payload(self):
        devices = parse_device_list("2G0Y\tdevice usb:1X product:eureka model:Quest_3 transport_id:2\n"
                                    "192.168.0.5:5555 unauthorized\n")
        self.assertEqual([(d.serial, d.state, d.is_wifi, d.model) for d in devices],
                         [("2G0Y", "device", False, "Quest_3"), ("192.168.0.5:5555", "unauthorized", True, None)])

    def test_frames(self):
        async def scenario():
            reader = asyncio.StreamReader()
            reader.feed_data(b"0005A\tdev" + b"0000")
            reader.feed_eof()
            return [f async for f in read_frames(reader)]
        self.assertEqual(asyncio.run(scenario()), ["A\tdev", ""])

    def test_ip_and_connect_classification(self):
        text = "1: lo inet 127.0.0.1/8 scope host lo\n9: rmnet0 inet 10.1.1.1/24 scope global rmnet0\n15: wlan0 inet 192.168.22.201/16 scope global wlan0\n"
        self.assertEqual(provisioning.parse_ip(text), "192.168.22.201")
        self.assertEqual(provisioning.classify_connect("connected to 1.2.3.4:5555"), "connected")
        self.assertEqual(provisioning.classify_connect("already connected to 1.2.3.4:5555"), "connected")
        self.assertEqual(provisioning.classify_connect("failed to connect to '1.2.3.4:5555': Connection refused"), "refused")
        self.assertEqual(provisioning.classify_connect("failed to connect to '1.2.3.4:5555': Operation timed out"), "unreachable")
        self.assertEqual(provisioning.classify_connect("cannot connect to 1.2.3.4:5555: No route to host"), "unreachable")

    def test_battery_and_netmask(self):
        b = parse_battery("  AC powered: false\n  USB powered: true\n  status: 2\n  level: 57\n")
        self.assertEqual((b.level, b.charging, b.status), (57, True, "charging"))
        self.assertEqual(discovery.mask_to_prefix("0xffff0000"), 16)
        self.assertEqual(discovery.mask_to_prefix("255.255.255.0"), 24)


class ManagerTests(unittest.TestCase):
    def test_usb_then_wifi_provisioning_and_telemetry(self):
        async def scenario():
            world = FakeAdbWorld([FakeHeadset("SER1", "10.0.0.5", ssid="lab")])
            bus = EventBus()
            reg = Registry(":memory:", bus)
            feed = TrackFeed()
            mgr = DeviceManager(reg, bus, world.adb(), track_source=feed.source, auto_reconnect=False)
            await mgr.start()
            feed.push(("SER1", "device"))
            await until(lambda: mgr.status.get("SER1") and mgr.status["SER1"].state == "usb")
            self.assertEqual(mgr.usb_devices()[0]["serial"], "SER1")
            service = ProvisioningService(mgr, reg)
            results = await service.enable_wifi(["SER1"])
            self.assertTrue(results[0]["ok"], results)
            self.assertTrue(world.headsets["SER1"].tcpip)
            feed.push(("SER1", "device"), ("10.0.0.5:5555", "device"))
            await until(lambda: mgr.status["SER1"].state == "wifi")
            self.assertEqual(mgr.target("SER1"), "10.0.0.5:5555")
            row = reg.get("SER1")
            self.assertEqual((row["ip"], row["ssid"], row["ssid_baseline"]), ("10.0.0.5", "lab", "lab"))
            telemetry = TelemetryService(mgr, reg, interval=3600)
            world.headsets["SER1"].ssid = "other"
            await telemetry.poll("SER1")
            self.assertEqual([w["kind"] for w in reg.warnings()], ["ssid_changed"])
            self.assertEqual(reg.get("SER1")["battery"], 80)
            await mgr.stop()
            return [e["type"] for e in bus.recent(200)]
        types = asyncio.run(scenario())
        self.assertIn(ev.USB_DEVICE_ATTACHED, types)
        self.assertIn(ev.PROVISIONING_PROGRESS, types)
        self.assertIn(ev.SSID_CHANGED, types)

    def test_reconnect_backoff_and_needs_usb(self):
        async def scenario():
            world = FakeAdbWorld([FakeHeadset("SER1", "10.0.0.5")])
            world.connect_result["10.0.0.5:5555"] = "failed to connect to '10.0.0.5:5555': Connection refused"
            bus = EventBus()
            reg = Registry(":memory:", bus)
            reg.observe("SER1", "adb", ip="10.0.0.5")
            feed = TrackFeed()
            mgr = DeviceManager(reg, bus, world.adb(), track_source=feed.source, auto_reconnect=False)
            await mgr.reconnect_due()
            st = mgr.status["SER1"]
            first = (st.state, st.backoff)
            await mgr.reconnect_due()          # not due yet: no second attempt
            attempts_after_second_pass = st.attempts
            del world.connect_result["10.0.0.5:5555"]
            state = await mgr.connect_now("SER1")
            return first, attempts_after_second_pass, state, world.connected
        first, attempts, state, connected = asyncio.run(scenario())
        self.assertEqual(first, ("needs_usb", 6.0))
        self.assertEqual(attempts, 1)
        self.assertIn("10.0.0.5:5555", connected)

    def test_tsv_placeholder_becomes_real_device(self):
        async def scenario():
            world = FakeAdbWorld([FakeHeadset("REAL", "10.0.0.8")])
            bus = EventBus()
            reg = Registry(":memory:", bus)
            reg.import_tsv("Seat-07\t10.0.0.8:5555\t1\t1\n")
            feed = TrackFeed()
            mgr = DeviceManager(reg, bus, world.adb(), track_source=feed.source, auto_reconnect=False)
            await mgr.start()
            await mgr.reconnect_due(force=True)
            feed.push(("10.0.0.8:5555", "device"))
            await until(lambda: mgr.status.get("REAL") and mgr.status["REAL"].state == "wifi")
            await mgr.stop()
            return reg.get("REAL"), [k for k in mgr.status if k.startswith("tsv:")]
        row, placeholders = asyncio.run(scenario())
        self.assertEqual((row["alias"], row["grid_col"]), ("Seat-07", 1))
        self.assertEqual(placeholders, [])


if __name__ == "__main__":
    unittest.main()
