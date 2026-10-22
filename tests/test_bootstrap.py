"""Release installation tests using local archives and no network access."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.prefix = self.dir / 'env with spaces'
        (self.prefix / 'bin').mkdir(parents=True)
        pt = self.prefix / 'vendor/platform-tools'
        pt.mkdir(parents=True)
        for name in ['adb', 'fastboot']:
            self.executable(pt / name, '#!/bin/sh\nexit 0\n')
        self.executable(self.prefix / 'bin/java', '#!/bin/sh\nexit 0\n')
        name = 'scrcpy-macos-aarch64-v9.9.9'
        release = self.dir / name
        release.mkdir()
        self.executable(release / 'scrcpy', '#!/bin/sh\necho scrcpy-fixture-9.9.9\n')
        (release / 'scrcpy-server').write_text('matching release server')
        self.archive = self.dir / 'release.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as tar:
            tar.add(release, arcname=name)
        self.executable(self.prefix / 'bin/curl', '#!' + sys.executable + '''
import os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
assert '--fail' in args
assert not any('tmp.rom1v.com' in a for a in args)
Path(os.environ['DOWNLOAD_LOG']).write_text('downloaded')
shutil.copyfile(os.environ['FIXTURE_ARCHIVE'], args[args.index('-o') + 1])
''')
        self.env = dict(os.environ, JAVA_HOME=str(self.prefix), SCRCPY_VERSION='9.9.9',
                        SCRCPY_RELEASE_ARCH='macos-aarch64', FIXTURE_ARCHIVE=str(self.archive),
                        DOWNLOAD_LOG=str(self.dir / 'downloaded'),
                        SCRCPY_ARCHIVE_SHA256=hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def executable(self, path, content):
        path.write_text(content)
        path.chmod(0o755)

    def run_bootstrap(self, **env):
        return subprocess.run(['/bin/bash', str(ROOT / 'scripts/bootstrap_binaries.sh'), str(self.prefix)],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=15)

    def test_release_preserves_matching_server_and_is_idempotent(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.prefix / 'bin/scrcpy-server').read_text(), 'matching release server')
        self.assertIn('scrcpy-fixture-9.9.9', subprocess.check_output([str(self.prefix / 'bin/scrcpy')], text=True))
        (self.dir / 'downloaded').unlink()
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.dir / 'downloaded').exists())

    def test_bad_archive_does_not_replace_existing_client(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        old_target = (self.prefix / 'bin/scrcpy').resolve()
        result = self.run_bootstrap(SCRCPY_VERSION='9.9.8', SCRCPY_ARCHIVE_SHA256='invalid')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum mismatch', result.stderr)
        self.assertEqual((self.prefix / 'bin/scrcpy').resolve(), old_target)

    def test_missing_cached_server_is_repaired(self):
        self.assertEqual(self.run_bootstrap().returncode, 0)
        (self.prefix / 'bin/scrcpy-server').resolve().unlink()
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.prefix / 'bin/scrcpy-server').read_text(), 'matching release server')


if __name__ == '__main__':
    unittest.main()
