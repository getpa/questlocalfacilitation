"""Behavioral regressions using fake adb/scrcpy; no headset or network required."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'scripts/quest_multi_scrcpy.sh'


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.log = self.dir / 'calls.jsonl'
        self.config = self.dir / 'devices.tsv'
        self.config.write_text('Quest-A\t192.0.2.1:5555\n')
        self.adb = self.binary('adb', '''
import os, sys
from pathlib import Path
a = sys.argv[1:]
if a[-1:] == ['get-state']:
    if a[1] == os.getenv('OFFLINE_ENDPOINT'):
        sys.exit(1)
    print('device')
elif 'ro.build.version.sdk' in a:
    print(os.getenv('FAKE_SDK', '33'))
elif 'battery' in a:
    sys.exit(1)  # Status failures must never create a second worker.
elif 'power' in a:
    print('Display Power: state=ON')
''')
        self.scrcpy = self.binary('scrcpy', '''
import json, os, signal, sys, time
from pathlib import Path
if '--help' in sys.argv:
    print('--hwdec=mode' if not os.getenv('OLD_SCRCPY') else 'old help')
    sys.exit(0)
entry = {'args': sys.argv[1:], 'pid': os.getpid(), 'time': time.monotonic()}
with open(os.environ['CALL_LOG'], 'a') as f:
    f.write(json.dumps(entry) + '\\n')
if os.getenv('FAIL_ONCE'):
    marker = Path(os.environ['CALL_LOG'] + '.failed')
    if not marker.exists():
        marker.touch()
        sys.exit(1)
if os.getenv('HOLD'):
    time.sleep(60)
''')
        self.env = dict(os.environ, ADB_BIN=str(self.adb), SCRCPY_BIN=str(self.scrcpy),
                        CONFIG=str(self.config), CALL_LOG=str(self.log),
                        ENV_PREFIX=str(self.dir / 'env'), SCRCPY_LAUNCH_DELAY='0.01',
                        QUEST_REQUIRE_AWAKE='false')

    def binary(self, name, code):
        p = self.dir / name
        p.write_text('#!' + sys.executable + '\n' + code)
        p.chmod(0o755)
        return p

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def run_launcher(self, *args, **env):
        return subprocess.run(['/bin/bash', str(LAUNCHER), 'start', *args],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=15)

    def start_launcher(self, *args, **env):
        output = open(self.dir / 'output', 'w+')
        self.addCleanup(output.close)
        p = subprocess.Popen(['/bin/bash', str(LAUNCHER), 'start', *args],
                             env=dict(self.env, **env), stdout=output, stderr=output)
        def stop():
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        self.addCleanup(stop)
        return p, output

    def wait_calls(self, n, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.calls()) >= n:
                return self.calls()
            time.sleep(.05)
        self.fail(f'Expected {n} launches, got {self.calls()}')

    def test_defaults_and_audio(self):
        result = self.run_launcher('--no-status')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.calls()[0]['args']
        for flag in ['--max-size=1280', '--max-fps=30', '--video-bit-rate=4M',
                     '--video-buffer=0', '--hwdec=auto', '--no-clipboard-autosync',
                     '--audio-dup', '--audio-source=playback']:
            self.assertIn(flag, args)

    def test_overrides_and_legacy_audio(self):
        result = self.run_launcher('--no-status', '--audio-mode', 'output',
                                   MAX_SIZE='1600', MAX_FPS='60', BIT_RATE='10M',
                                   VIDEO_BUFFER='50', SCRCPY_HWDEC='disabled')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.calls()[0]['args']
        for flag in ['--max-size=1600', '--max-fps=60', '--video-bit-rate=10M',
                     '--video-buffer=50', '--hwdec=disabled']:
            self.assertIn(flag, args)
        self.assertNotIn('--audio-dup', args)

    def test_dry_run_has_clean_port_and_sdk_without_device_calls(self):
        result = self.run_launcher('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--port=27183', result.stderr)
        self.assertIn('--audio-dup', result.stderr)
        self.assertFalse(self.log.exists())

    def test_old_scrcpy_and_audio_fallback(self):
        result = self.run_launcher('--no-status', OLD_SCRCPY='1', FAKE_SDK='32')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--no-audio', self.calls()[0]['args'])
        self.assertFalse(any(x.startswith('--hwdec') for x in self.calls()[0]['args']))
        result = self.run_launcher('--no-status', OLD_SCRCPY='1', SCRCPY_HWDEC='videotoolbox')
        self.assertNotEqual(result.returncode, 0)

    def test_offline_first_device_does_not_block_second(self):
        self.config.write_text('Quest-A\t192.0.2.1:5555\nQuest-B\t192.0.2.2:5555\n')
        p, output = self.start_launcher('--no-status', OFFLINE_ENDPOINT='192.0.2.1:5555', HOLD='1')
        calls = self.wait_calls(1, timeout=3)
        self.assertIn('--serial=192.0.2.2:5555', calls[0]['args'])
        self.assertIn('--port=27283', calls[0]['args'])
        p.terminate()
        p.wait(timeout=10)

    def test_failure_retries_recording_and_cleanup_without_status_duplicates(self):
        p, output = self.start_launcher('--record', '--status-interval', '1',
                                        FAIL_ONCE='1', HOLD='1', RECORD_DIR=str(self.dir / 'records'))
        calls = self.wait_calls(2)
        time.sleep(2.1)  # More than two failed status polls.
        self.assertEqual(len(self.calls()), 2)
        paths = [next(x for x in call['args'] if x.startswith('--record=')) for call in calls]
        self.assertNotEqual(paths[0], paths[1])
        self.assertIn('_retry02.mp4', paths[1])
        p.terminate()
        p.wait(timeout=10)
        for call in calls:
            with self.assertRaises(ProcessLookupError):
                os.kill(call['pid'], 0)

    def test_validation_before_launch(self):
        for env in [dict(MAX_FPS='0'), dict(VIDEO_BUFFER='-1'), dict(SCRCPY_HWDEC='invalid'),
                    dict(SCRCPY_BASE_PORT='65500')]:
            result = self.run_launcher('--no-status', **env)
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()
