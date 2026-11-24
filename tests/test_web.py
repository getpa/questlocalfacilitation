"""M2/M5: TLS, access control, admin and viewer APIs, frontend syntax."""
import asyncio
import os
from pathlib import Path
import plistlib
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import fastapi  # noqa: F401
    from cryptography import x509  # noqa: F401
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False
requires_web = unittest.skipUnless(HAVE_WEB, "FastAPI/cryptography not installed (use .mamba-env)")


@requires_web
class TlsTests(unittest.TestCase):
    def test_ca_server_cert_chain_and_profile(self):
        from cryptography import x509
        from questhub.web.tls import LocalCA
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ca = LocalCA(Path(tmp.name) / "tls")
        cert_path, key_path = ca.ensure_server_cert(["lab-mac.local", "localhost"], ["127.0.0.1", "192.168.1.10"])
        if sys.platform != "win32":   # POSIX modes; on Windows the key is protected by the profile ACL
            self.assertEqual(oct(os.stat(ca.ca_key_path).st_mode & 0o777), "0o600")
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        self.assertEqual(ca._san(cert), {"DNS:lab-mac.local", "DNS:localhost", "IP:127.0.0.1", "IP:192.168.1.10"})
        self.assertLessEqual((cert.not_valid_after_utc - cert.not_valid_before_utc).days, 398)
        # Same names: reused. New address: re-issued with the same CA.
        mtime = cert_path.stat().st_mtime_ns
        ca.ensure_server_cert(["lab-mac.local", "localhost"], ["127.0.0.1", "192.168.1.10"])
        self.assertEqual(cert_path.stat().st_mtime_ns, mtime)
        fp = ca.fingerprint()
        ca.ensure_server_cert(["lab-mac.local"], ["10.0.0.9"])
        self.assertEqual(ca.fingerprint(), fp)

        # A real TLS handshake that trusts only our CA.
        server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        server_ctx.load_cert_chain(cert_path, key_path)
        client_ctx = ssl.create_default_context(cafile=str(ca.ca_cert_path))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve():
            conn, _ = listener.accept()
            with server_ctx.wrap_socket(conn, server_side=True) as tls:
                tls.recv(1)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with socket.create_connection(("127.0.0.1", port)) as raw:
            with client_ctx.wrap_socket(raw, server_hostname="lab-mac.local") as tls:
                self.assertEqual(tls.version()[:3], "TLS")
                tls.send(b"x")
        thread.join(5)
        listener.close()

        profile = plistlib.loads(ca.mobileconfig())
        self.assertEqual(profile["PayloadType"], "Configuration")
        self.assertEqual(profile["PayloadContent"][0]["PayloadType"], "com.apple.security.root")
        self.assertEqual(profile["PayloadContent"][0]["PayloadContent"], ca.ca_der())


class AuthTests(unittest.TestCase):
    def test_access_code_sessions_and_brute_force_brake(self):
        from questhub.web.auth import ViewerAuth
        auth = ViewerAuth(max_sessions=2)
        self.assertRegex(auth.code, r"^\d{6}$")
        token = auth.login(auth.code, "a")
        self.assertTrue(auth.valid(token))
        self.assertIsNone(auth.login("000000" if auth.code != "000000" else "111111", "a"))
        for _ in range(10):
            auth.login("bad", "b")
        self.assertIsNone(auth.login(auth.code, "b"), "client b is throttled")
        auth.login(auth.code, "c")
        auth.login(auth.code, "d")
        self.assertEqual(auth.session_count, 2)
        auth.rotate()
        self.assertFalse(auth.valid(token))


@requires_web
class ApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from questhub.application import Application
        from questhub.web.admin import create_admin_app
        from questhub.web.viewer import create_ca_app, create_viewer_app
        from tests.fakes import FakeAdbWorld, FakeHeadset, TrackFeed
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        world = FakeAdbWorld([FakeHeadset("S1", "10.0.0.5", ssid="lab")])
        self.app = Application(data_dir=self.tmp.name, adb=world.adb(), track_source=TrackFeed().source,
                               auto_reconnect=False, start_background=False)
        self.addCleanup(self.app.registry.close)   # before the temp dir is removed (Windows locks open files)
        self.app.registry.observe("S1", "usb", ip="10.0.0.5")
        self.admin = TestClient(create_admin_app(self.app), base_url="http://127.0.0.1:8765")
        self.viewer = TestClient(create_viewer_app(self.app, secure=False), base_url="http://127.0.0.1:8443")
        self.ca = TestClient(create_ca_app(self.app))
        self.h = {"X-QH-Token": self.app.admin_token}

    def test_admin_token_required_for_changes(self):
        page = self.admin.get("/").text
        self.assertIn(self.app.admin_token, page)
        self.assertNotIn("{{ADMIN_TOKEN}}", page)
        self.assertEqual(self.admin.patch("/api/devices/S1", json={"alias": "X"}).status_code, 403)
        r = self.admin.patch("/api/devices/S1", json={"alias": "Seat-1", "grid_col": 0, "grid_row": 0}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        state = self.admin.get("/api/state").json()
        dev = next(d for d in state["devices"] if d["serial"] == "S1")
        self.assertEqual((dev["alias"], dev["ip"]), ("Seat-1", "10.0.0.5"))
        self.assertEqual([p["name"] for p in state["presets"]], ["low", "standard", "high"])

    def test_tsv_settings_warnings_and_network(self):
        r = self.admin.post("/api/registry/import", content="Seat-9\t10.0.0.9:5555\t3\t1\n", headers=self.h)
        self.assertEqual(r.json(), {"imported": 1})
        self.assertIn("Seat-9\t10.0.0.9:5555\t3\t1", self.admin.get("/api/registry/export.tsv").text)
        r = self.admin.put("/api/settings", json={"default_quality": "low", "telemetry_interval": 30}, headers=self.h)
        self.assertEqual(sorted(r.json()["changed"]), ["default_quality", "telemetry_interval"])
        self.assertTrue((Path(self.tmp.name) / "settings.json").exists())
        from tests.test_devices import wifi
        self.app.registry.observe("S1", "telemetry", wifi=wifi("a"))
        self.app.registry.observe("S1", "telemetry", wifi=wifi("b"))
        warnings = self.admin.get("/api/state").json()["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(self.admin.post(f"/api/warnings/{warnings[0]['id']}/ack", headers=self.h).status_code, 200)
        net = self.admin.get("/api/network").json()
        self.assertEqual(net["ssids"], {"b": 1})
        self.assertIn("ssid", self.admin.get("/api/network/export.csv").text)

    def test_viewer_login_and_sanitized_devices(self):
        self.assertIn("アクセスコード", self.viewer.get("/").text)
        self.assertEqual(self.viewer.get("/api/devices").status_code, 401)
        self.assertEqual(self.viewer.post("/login", json={"code": "nope"}).status_code, 403)
        self.assertEqual(self.viewer.post("/login", json={"code": self.app.viewer_auth.code}).status_code, 200)
        devices = self.viewer.get("/api/devices").json()
        self.assertEqual(devices[0]["serial"], "S1")
        for forbidden in ("ip", "ssid", "bssid", "rssi"):
            self.assertNotIn(forbidden, devices[0])
        self.assertNotIn("10.0.0.5", str(devices))
        self.assertIn("ログアウト", self.viewer.get("/").text)
        self.admin.post("/api/viewer/rotate-code", headers=self.h)
        self.assertEqual(self.viewer.get("/api/devices").status_code, 401)

    def test_ca_page_and_downloads(self):
        page = self.ca.get("/").text
        self.assertIn(self.app.ca.fingerprint(), page)
        self.assertIn("<svg", page)
        self.assertEqual(self.ca.get("/ca.cer").content, self.app.ca.ca_der())
        self.assertIn(b"BEGIN CERTIFICATE", self.ca.get("/ca.crt").content)
        self.assertEqual(plistlib.loads(self.ca.get("/ca.mobileconfig").content)["PayloadType"], "Configuration")

    def test_admin_websockets_need_token(self):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect):
            with self.admin.websocket_connect("/ws/events") as ws:
                ws.receive_json()
        with self.admin.websocket_connect(f"/ws/events?token={self.app.admin_token}") as ws:
            self.app.bus.publish("notice", None, message="hello")
            self.assertEqual(ws.receive_json()["data"]["message"], "hello")


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("osascript"), "JavaScriptCore check needs macOS")
class FrontendSyntaxTests(unittest.TestCase):
    """Parse every ES module with JavaScriptCore (no Node in the project env)."""

    def test_modules_parse(self):
        files = sorted((ROOT / "questhub").rglob("*.js"))
        self.assertTrue(files)
        script = ('ObjC.import("Foundation");'
                  'function run(argv){var s=$.NSString.stringWithContentsOfFileEncodingError(argv[0],$.NSUTF8StringEncoding,null).js;'
                  'try{new (Object.getPrototypeOf(async function(){}).constructor)(s);return "ok"}catch(e){return "ERR "+e}}')
        for path in files:
            src = "\n".join("" if line.startswith("import ") else
                            line.replace("export class", "class").replace("export function", "function")
                            .replace("export const", "const").replace("export async function", "async function")
                            for line in path.read_text(encoding="utf-8").splitlines())
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(src)
            try:
                out = subprocess.run(["osascript", "-l", "JavaScript", "-e", script, f.name],
                                     capture_output=True, text=True, timeout=30).stdout.strip()
            finally:
                os.unlink(f.name)
            self.assertEqual(out, "ok", f"{path.relative_to(ROOT)}: {out}")


if __name__ == "__main__":
    unittest.main()
