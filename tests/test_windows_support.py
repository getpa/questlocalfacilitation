"""Windows support: portable helpers (run everywhere) and the Windows installer files."""
import importlib
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from questhub.core.procs import pid_alive  # noqa: E402
from questhub.devices import discovery  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class PortableHelpersTests(unittest.TestCase):
    def test_pid_alive_never_kills(self):
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(2 ** 22 + 12345))      # beyond the usual pid range
        self.assertTrue(pid_alive(os.getpid()), "checking must not terminate the process (Windows os.kill)")

    def test_windows_ip_parsing(self):
        out = ('[{"IPAddress":"192.168.22.5","PrefixLength":16},{"IPAddress":"127.0.0.1","PrefixLength":8},'
               '{"IPAddress":"169.254.3.4","PrefixLength":16},{"IPAddress":"10.0.0.7","PrefixLength":24}]')
        self.assertEqual([str(i) for i in discovery.parse_windows_ip(out)], ["192.168.22.5/16", "10.0.0.7/24"])
        self.assertEqual([str(i) for i in discovery.parse_windows_ip('{"IPAddress":"10.1.2.3","PrefixLength":24}')],
                         ["10.1.2.3/24"], "a single adapter is a JSON object, not a list")
        self.assertEqual(discovery.parse_windows_ip(""), [])
        self.assertEqual(discovery.parse_windows_ip("not json"), [])

    def test_ifconfig_parsing(self):
        out = ("lo0: flags=8049\n\tinet 127.0.0.1 netmask 0xff000000\n"
               "en0: flags=8863\n\tinet 192.168.22.10 netmask 0xffff0000 broadcast 192.168.255.255\n")
        self.assertEqual([str(i) for i in discovery.parse_ifconfig(out)], ["192.168.22.10/16"])

    def test_binary_lookup_uses_windows_layout(self):
        import questhub.paths as paths
        with tempfile.TemporaryDirectory() as tmp:
            scrcpy = Path(tmp) / "vendor/scrcpy"
            scrcpy.mkdir(parents=True)
            (scrcpy / "adb.exe").write_text("", encoding="utf-8")
            (scrcpy / "scrcpy-server").write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"QH_ENV_DIR": tmp}), mock.patch.object(sys, "platform", "win32"):
                try:
                    win = importlib.reload(paths)
                    self.assertEqual(Path(win.binary("adb")), scrcpy / "adb.exe")
                    self.assertEqual(win.scrcpy_server_jar(), (scrcpy / "scrcpy-server").resolve())
                    self.assertEqual(win.installer_hint(), "install.cmd")
                finally:
                    importlib.reload(paths)


class WindowsInstallerFilesTests(unittest.TestCase):
    def test_lock_and_spec(self):
        lock = (ROOT / "locks/conda-win-64.txt").read_text(encoding="utf-8")
        self.assertIn("@EXPLICIT", lock)
        urls = [l for l in lock.splitlines() if l.startswith("https://")]
        self.assertTrue(urls and all(re.search(r"/(win-64|noarch)/.+#[0-9a-f]{32}$", u) for u in urls))
        for pkg in ("python-3.11", "/av-", "/fastapi-", "/uvicorn-", "/cryptography-", "/numpy-", "/qrcode-"):
            self.assertIn(pkg, lock)
        spec = (ROOT / "environment-windows.yml").read_text(encoding="utf-8")
        deps = [l.strip() for l in spec.splitlines() if l.strip().startswith("- ")]
        self.assertNotIn("- coreutils", deps, "Unix helper tools are not needed on Windows")

    def test_installer_pins_match_the_mac_bootstrap(self):
        ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
        bootstrap = (ROOT / "scripts/bootstrap_binaries.sh").read_text(encoding="utf-8")
        server_sha = re.search(r"\$ScrcpyServerSha256 = '([0-9a-f]{64})'", ps1).group(1)
        self.assertIn(server_sha, bootstrap, "same scrcpy-server 5.0.1 on macOS and Windows")
        self.assertRegex(ps1, r"\$MicromambaSha256 = '[0-9a-f]{64}'")
        self.assertIn("scrcpy-win64-v$ScrcpyVersion.zip", ps1)
        for switch in ("$Doctor", "$Strict", "$Uninstall", "$PurgeData", "$Yes"):
            self.assertIn(switch, ps1)

    def test_installer_handles_a_locked_or_missing_adb(self):
        ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("'adb.exe'", re.search(r"\$ScrcpyRequiredFiles = @\(([^)]*)\)", ps1).group(1),
                      "a missing adb.exe makes the bundle incomplete, so a re-run repairs it")
        self.assertIn("foreach ($exe in $ScrcpyRequiredFiles)", ps1)
        # adb kill-server returns before adb.exe is unlocked: wait for it before every removal of the bundle
        self.assertEqual(ps1.count("Stop-BundledAdb\n") + ps1.count("Stop-BundledAdb\r\n"), 2)
        self.assertIn("function Stop-BundledAdb", ps1)

    def test_launchers_and_line_endings(self):
        attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        for rule in ("*.cmd text eol=crlf", "*.ps1 text eol=crlf", "*.sh text eol=lf"):
            self.assertIn(rule, attrs)
        launcher = (ROOT / "questhub.cmd").read_text(encoding="utf-8")
        self.assertIn(r'"%ENV_DIR%\python.exe" -m questhub %*', launcher)
        self.assertIn("install.ps1", (ROOT / "install.cmd").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
