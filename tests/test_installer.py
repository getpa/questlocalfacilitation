"""install.sh behaviour with a fake micromamba; no network or real packages."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

if sys.platform == "win32":
    raise unittest.SkipTest("bash scripts (macOS installer / legacy CLI) are not used on Windows")

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'install.sh'

FAKE_MICROMAMBA = '#!' + sys.executable + r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['MM_LOG'], 'a') as f:
    f.write(json.dumps({'args': args, 'root': os.environ.get('MAMBA_ROOT_PREFIX')}) + '\n')
if args == ['--version']:
    print('9.9.9')
    sys.exit(0)
if 'create' in args or 'install' in args:
    prefix = Path(args[args.index('--prefix') + 1])
    (prefix / 'bin').mkdir(parents=True, exist_ok=True)
    tools = {
        'python': '#!/bin/sh\necho 3.11.0\n',
        'adb': '#!/bin/sh\n[ "$1" = devices ] && { echo "List of devices attached"; exit 0; }\necho "Android Debug Bridge version 1.0.41"\n',
        'scrcpy': '#!/bin/sh\necho "scrcpy 5.0.1 <https://github.com/Genymobile/scrcpy>"\n',
    }
    for name, body in tools.items():
        path = prefix / 'bin' / name
        path.write_text(body)
        path.chmod(0o755)
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.home = self.dir / 'home'
        self.home.mkdir()
        self.tools = self.dir / 'tools'
        self.env_dir = self.dir / 'env'
        self.data = self.home / '.questhub'
        self.log = self.dir / 'mm.jsonl'
        self.mm = self.dir / 'micromamba'
        self.mm.write_text(FAKE_MICROMAMBA)
        self.mm.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), MM_LOG=str(self.log),
                        QH_MICROMAMBA_BIN=str(self.mm), QH_PLATFORM='osx-arm64',
                        QH_TOOLS_DIR=str(self.tools), QH_ENV_DIR=str(self.env_dir),
                        QH_DATA_DIR=str(self.data), QH_SKIP_BINARIES='1', QH_DOCTOR_OFFLINE='1')

    def run_installer(self, *args, **env):
        return subprocess.run(['/bin/bash', str(INSTALLER), *args], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def solver_calls(self):
        return [c for c in self.calls() if 'create' in c['args'] or 'install' in c['args']]

    def test_help(self):
        result = self.run_installer('--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('--uninstall', result.stdout)

    def test_install_uses_lock_isolated_root_and_is_idempotent(self):
        result = self.run_installer('--strict')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        creates = self.solver_calls()
        self.assertEqual(len(creates), 1)
        args = creates[0]['args']
        self.assertIn('--no-rc', args)
        self.assertEqual(args[args.index('--file') + 1], str(ROOT / 'locks/conda-osx-arm64.txt'))
        self.assertEqual(creates[0]['root'], str(self.tools / 'mamba-root'))
        self.assertTrue((self.data / 'tls').is_dir())
        self.assertIn('All required checks passed', result.stdout)

        result = self.run_installer('--strict')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.solver_calls()), 1, 'second run must not touch the environment')
        self.assertIn('Environment is up to date', result.stdout)

    def test_spec_change_rebuilds_and_keeps_vendor_binaries(self):
        self.assertEqual(self.run_installer().returncode, 0)
        marker = self.env_dir / 'vendor/platform-tools/adb'
        marker.parent.mkdir(parents=True)
        marker.write_text('cached')
        stale = self.env_dir / 'stale-file'
        stale.write_text('old')
        result = self.run_installer('--build-from-source')
        self.assertEqual(result.returncode, 0, result.stderr)
        files = [c['args'][c['args'].index('--file') + 1] for c in self.solver_calls()]
        self.assertEqual(files[-1], str(ROOT / 'environment-build.yml'))
        self.assertEqual(marker.read_text(), 'cached')
        self.assertFalse(stale.exists())

    def test_strict_requires_lock_for_platform(self):
        result = self.run_installer('--strict', QH_PLATFORM='osx-none')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--strict requires', result.stderr)
        self.assertEqual(self.solver_calls(), [])

    def test_doctor_reports_missing_environment(self):
        result = self.run_installer('--doctor')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('environment missing', result.stdout)

    def test_uninstall_keeps_data_unless_purged(self):
        self.assertEqual(self.run_installer().returncode, 0)
        self.tools.mkdir(exist_ok=True)
        result = self.run_installer('--uninstall')
        self.assertNotEqual(result.returncode, 0, 'non-interactive uninstall needs --yes')
        self.assertTrue(self.env_dir.exists())
        self.assertEqual(self.run_installer('--uninstall', '--yes').returncode, 0)
        self.assertFalse(self.env_dir.exists())
        self.assertFalse(self.tools.exists())
        self.assertTrue(self.data.exists())
        self.assertEqual(self.run_installer('--uninstall', '--purge-data', '--yes').returncode, 0)
        self.assertFalse(self.data.exists())

    def test_uninstall_refuses_home(self):
        result = self.run_installer('--uninstall', '--yes', QH_TOOLS_DIR=str(self.home))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Refusing to remove', result.stderr)
        self.assertTrue(self.home.exists())

    def test_locks_are_explicit_and_hashed(self):
        for platform in ['osx-arm64', 'osx-64']:
            lines = (ROOT / f'locks/conda-{platform}.txt').read_text().splitlines()
            self.assertIn('@EXPLICIT', lines)
            packages = [line for line in lines if line.startswith('https://')]
            self.assertTrue(packages)
            for line in packages:
                self.assertRegex(line, rf'^https://conda\.anaconda\.org/conda-forge/({platform}|noarch)/.+#[0-9a-f]{{32}}$')


if __name__ == '__main__':
    unittest.main()
