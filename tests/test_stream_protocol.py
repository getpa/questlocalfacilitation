"""scrcpy wire format, Wi-Fi parsing and hub behaviour (pure Python)."""
import asyncio
import struct
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from questhub.devices.wifi_parse import band_for, parse_wifi_info  # noqa: E402
from questhub.stream import hub as hubmod  # noqa: E402
from questhub.stream import protocol as p  # noqa: E402

# Same format as a Quest 3 (Android 14) `cmd wifi status`, with made-up identifiers.
QUEST_WIFI_STATUS = '''Wifi is enabled
==== Primary ClientModeManager instance ====
Wifi is connected to "lab-net"
WifiInfo: SSID: "lab-net", BSSID: 6E:00:00:11:22:33, MAC: 5a:00:00:00:00:01, IP: /192.168.22.10, Security type: 4, Supplicant state: COMPLETED, Wi-Fi standard: 6, RSSI: -32, Link speed: 2401Mbps, Tx Link speed: 2401Mbps, Max Supported Tx Link speed: 2401Mbps, Rx Link speed: 1200Mbps, Max Supported Rx Link speed: 2401Mbps, Frequency: 6375MHz, Net ID: 5, Metered hint: false
successfulTxPackets: 62062
'''


class ProtocolTests(unittest.TestCase):
    def test_media_header_round_trip(self):
        for packet in [p.MediaPacket(b"abc", 123456, key_frame=True), p.MediaPacket(b"x" * 5, 99),
                       p.MediaPacket(b"cfg", None, config=True)]:
            raw = p.encode_packet(packet)
            header = p.parse_header(raw[:12])
            self.assertFalse(header.session)
            self.assertEqual((header.pts, header.config, header.key_frame, header.size),
                             (packet.pts, packet.config, packet.key_frame, len(packet.data)))

    def test_bit_layout_matches_server(self):
        # Streamer.java: PACKET_FLAG_CONFIG = 1<<62, KEY_FRAME = 1<<61, big-endian.
        raw = struct.pack(">QI", (1 << 61) | 1000, 7)
        header = p.parse_header(raw)
        self.assertTrue(header.key_frame)
        self.assertEqual(header.pts, 1000)
        header = p.parse_header(struct.pack(">QI", 1 << 62, 3))
        self.assertTrue(header.config)
        self.assertIsNone(header.pts)

    def test_session_packet(self):
        header = p.parse_header(p.encode_session(1280, 684, client_resized=True))
        self.assertTrue(header.session)
        self.assertEqual((header.width, header.height, header.client_resized), (1280, 684, True))

    def test_device_name_and_codecs(self):
        self.assertEqual(p.parse_device_name(p.encode_device_name("Quest 3")), "Quest 3")
        self.assertEqual(len(p.encode_device_name("x" * 100)), 64)
        self.assertEqual(p.parse_codec_id(b"h264"), "h264")
        self.assertEqual(p.parse_codec_id(b"opus"), "opus")
        with self.assertRaises(p.StreamDisabled):
            p.parse_codec_id(b"\0\0\0\0")
        with self.assertRaises(p.ProtocolError):
            p.parse_codec_id(b"\0\0\0\1")
        with self.assertRaises(p.ProtocolError):
            p.parse_codec_id(b"zzzz")

    def test_reset_video_control_message(self):
        self.assertEqual(p.encode_reset_video(), b"\x11")


class WifiParseTests(unittest.TestCase):
    def test_quest_cmd_wifi_status(self):
        info = parse_wifi_info(QUEST_WIFI_STATUS)
        self.assertEqual(info.ssid, "lab-net")
        self.assertEqual(info.bssid, "6e:00:00:11:22:33")
        self.assertEqual((info.rssi_dbm, info.freq_mhz, info.link_mbps, info.band), (-32, 6375, 2401, "6GHz"))

    def test_dumpsys_line_and_bands(self):
        info = parse_wifi_info('mWifiInfo SSID: "x y", BSSID: 02:00:00:00:00:00, RSSI: -71, Link speed: 72Mbps, Frequency: 2437MHz')
        self.assertEqual((info.ssid, info.bssid, info.band, info.rssi_dbm), ("x y", None, "2.4GHz", -71))
        self.assertEqual(band_for(5180), "5GHz")

    def test_unknown_and_disconnected(self):
        info = parse_wifi_info('WifiInfo: SSID: <unknown ssid>, BSSID: <none>, Supplicant state: COMPLETED, RSSI: -50')
        self.assertIsNone(info.ssid)
        self.assertIsNone(parse_wifi_info('WifiInfo: SSID: <unknown ssid>, Supplicant state: DISCONNECTED, RSSI: -127'))
        self.assertIsNone(parse_wifi_info("Wifi is disabled\n"))


class HubTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_late_subscriber_gets_state_then_waits_for_key_frame(self):
        async def scenario():
            hub = hubmod.FrameHub(video_codec="h264")
            hub.publish_video(p.SessionPacket(320, 180))
            hub.publish_video(p.MediaPacket(b"cfg", None, config=True))
            hub.publish_video(p.MediaPacket(b"k1", 0, key_frame=True))
            sub = hub.subscribe(audio=False)
            hub.publish_video(p.MediaPacket(b"d1", 1))
            hub.publish_video(p.MediaPacket(b"k2", 2, key_frame=True))
            hub.publish_video(p.MediaPacket(b"d2", 3))
            sub.close()
            return [(e.kind, e.packet.data if e.packet else None) async for e in sub]
        events = self.run_async(scenario())
        self.assertEqual(events, [("session", None), ("video_config", b"cfg"), ("video", b"k2"), ("video", b"d2")])

    def test_overflow_resyncs_at_next_key_frame(self):
        async def scenario():
            hub = hubmod.FrameHub(video_codec="h264")
            hub.publish_video(p.MediaPacket(b"cfg", None, config=True))
            sub = hub.subscribe(audio=False, maxsize=4)
            hub.publish_video(p.MediaPacket(b"k1", 0, key_frame=True))
            for i in range(10):
                hub.publish_video(p.MediaPacket(b"d", i + 1))
            hub.publish_video(p.MediaPacket(b"k2", 20, key_frame=True))
            sub.close()
            return [e.packet.data for e in [e async for e in sub] if e.kind == "video"], sub.dropped
        frames, dropped = self.run_async(scenario())
        self.assertEqual(frames[0], b"k2")
        self.assertGreater(dropped, 0)

    def test_audio_only_subscription_and_stats(self):
        async def scenario():
            hub = hubmod.FrameHub(audio_codec="opus")
            sub = hub.subscribe(video=False, audio=True)
            hub.publish_video(p.MediaPacket(b"k", 0, key_frame=True))
            hub.publish_audio(p.MediaPacket(b"head", None, config=True))
            hub.publish_audio(p.MediaPacket(b"a", 10))
            sub.close()
            return [e.kind async for e in sub], hub
        kinds, hub = self.run_async(scenario())
        self.assertEqual(kinds, ["audio_config", "audio"])
        self.assertEqual((hub.video_stats.key_frames, hub.audio_stats.packets), (1, 1))


if __name__ == "__main__":
    unittest.main()
