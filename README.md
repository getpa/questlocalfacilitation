# Quest Local Facilitation

Toolkit for mirroring up to 30 Meta Quest 3 headsets over local Wi-Fi using scrcpy and adb from a macOS host.

## Prerequisites
- macOS 13 (Ventura) or later, Apple Silicon or Intel
- Google Chrome (for the upcoming Quest Hub GUI)
- USB access to each Quest for the initial developer-mode pairing

Nothing else is required: no Mambaforge/conda, Homebrew, or Xcode Command Line Tools. The installer downloads a pinned, checksum-verified [micromamba](https://github.com/mamba-org/micromamba-releases) into `.tools/` and builds the environment in `.mamba-env/`, entirely inside this repository (no sudo, no changes to your shell profile).

> The bootstrap installs the official `scrcpy` **v5.0.1** client and its matching bundled server, checked against the published SHA-256 checksums. Building scrcpy from source is optional (`./install.sh --build-from-source`, which adds `environment-build.yml` and needs Xcode Command Line Tools); custom repositories/refs can be selected with `SCRCPY_GIT_REPO` / `SCRCPY_GIT_REF`.

## Set Up the Environment

```bash
./install.sh            # first install, or update after pulling changes (idempotent)
./install.sh --doctor   # check python / adb / scrcpy 5.0.1 / Chrome / ports
```

| Option | Purpose |
|--------|---------|
| `--strict` | Install only from `locks/conda-<platform>.txt` and a pinned platform-tools archive (identical classroom Macs) |
| `--build-from-source` | Also install build tools and compile scrcpy from git |
| `--relock [osx-arm64\|osx-64]` | Re-solve `environment.yml` and rewrite the lock file (maintainers) |
| `--uninstall [--purge-data] --yes` | Remove `.tools/` and `.mamba-env/` (and `~/.questhub` with `--purge-data`) |

On macOS 15 or later, the first `adb connect` to a headset triggers the "Local Network" permission prompt for your terminal app. If it was denied, `adb connect` fails with `No route to host`; allow it in System Settings > Privacy & Security > Local Network.

If you already use Mambaforge, the manual route still works:

```bash
mamba env create -p ./.mamba-env -f environment.yml
./scripts/bootstrap_binaries.sh ./.mamba-env
```

Scripts in this repository find `.mamba-env` automatically, so no activation is needed. To call `adb` / `scrcpy` directly from your shell:

```bash
export PATH="${PWD}/.mamba-env/bin:${PATH}"
```

Before running any script in this repository, restart the adb daemon with the repo-local binary:

```bash
./scripts/restart_env_adb.sh
```

This script always executes:
1. `./.mamba-env/bin/adb kill-server`
2. `./.mamba-env/bin/adb start-server`

The environment installs:
- CLI helpers (`jq`, GNU `sed`, `coreutils`, `curl`, `unzip`)
- Python runtime and `requests`
- Latest Android platform tools (`adb`, `fastboot`) and scrcpy binaries via `bootstrap_binaries.sh`

All helper scripts in `scripts/` automatically prefer binaries inside this environment (falling back to `PATH` only if necessary), so you can run them without activating anything. Run Python helpers with `./.mamba-env/bin/python` (on a Mac without Command Line Tools, a bare `python3` only opens the CLT install dialog).

Verify the binaries inside the environment:

```bash
which adb
which scrcpy
adb version
scrcpy --version
```

## Configure Devices
1. Enable developer mode on each Quest 3 and authorize USB debugging.
2. Run `./scripts/restart_env_adb.sh` first.
3. (Optional) With devices still tethered over USB, run `./scripts/usb_to_tcp.sh --port 5555` to switch every detected headset into Wi-Fi ADB mode.
   - `usb_to_tcp.sh` also runs `./scripts/restart_env_adb.sh` automatically at startup to enforce the same adb-daemon reset sequence.
4. Populate `scripts/quest_devices.tsv` with aliases, IPs, and optional grid positions (up to 30 entries).
5. Mirror the same endpoints in `scripts/devices.env` if you plan to use the broadcast-input helper scripts.


## Discover Quest Endpoints Automatically

### Quick Reachability Check
### Keep Displays Awake
To keep every headset up during instruction, toggle stay-awake across the roster:

```bash
./scripts/keep_awake.sh --enable   # prevent sleep
./scripts/keep_awake.sh --disable  # restore defaults
```

Before a session, run the helper to ensure every headset responds over Wi‑Fi:

```bash
./scripts/check_reachability.sh --ping --adb
```

Use `--ping` for network reachability, `--adb` to verify ADB state, or both for a full sweep.

Use the bundled scanner to probe your LAN for Quest headsets listening on Wi-Fi ADB (port 5555 by default):

```bash
./.mamba-env/bin/python scripts/discover_quest3.py          # auto-detect primary network
./.mamba-env/bin/python scripts/discover_quest3.py --cidr 192.168.10.0/24 --connect
```

Pass `--connect` to run `adb connect` and collect model/serial metadata for each hit. The script also prints ready-to-paste rows for `scripts/quest_devices.tsv`.

## Running the Multi-Scrcpy Launcher
Run:

```bash
./scripts/quest_multi_scrcpy.sh start                 # mirror + periodic battery table
./scripts/quest_multi_scrcpy.sh start --no-audio     # disable Quest audio forwarding
./scripts/quest_multi_scrcpy.sh start --audio-mode output   # forward audio (Quest speaker may mute)
./scripts/quest_multi_scrcpy.sh start --record       # add recording per device
./scripts/quest_multi_scrcpy.sh start --no-status    # disable battery polling during mirror
./scripts/quest_multi_scrcpy.sh start --status-interval 120
./scripts/quest_multi_scrcpy.sh status --interval 15 # standalone monitor (clears screen)
```

Environment variables such as `GRID_COLUMNS`, `DISPLAY_WIDTH`, and `RECORD_DIR` can be set inline to adjust window layout or recording targets. See `agents.md` for full documentation and best practices. Battery polling defaults to 60 s; tweak via `STATUS_INTERVAL` or `--status-interval`. Each headset has one background worker which retries `adb connect` and relaunches scrcpy after an abnormal exit. An offline headset does not block other launches; battery polling only reports status and never spawns additional workers. Set `SCRCPY_BASE_PORT` if you need to shift the local port range used by scrcpy when multiple windows start in parallel; each headset scans a separate 100-port range starting at `SCRCPY_BASE_PORT + index * 100`. Use `SCRCPY_LAUNCH_DELAY` (seconds) if you need extra spacing between launches. Audio mode defaults to `dup` (copy audio to Mac while keeping Quest playback) and falls back to `off` by default when duplication is unsupported (`--audio-fallback output` to keep host audio in that case). Use `--audio-mode output` if you explicitly want legacy full-forwarding behavior, or `--no-audio` to silence host capture. Launcher messages are prefixed with `[launcher]` to distinguish them from raw scrcpy/adb logs. Failed connections and abnormal scrcpy exits are retried after a 3-second pause, even with `--no-status`. Closing a scrcpy window normally ends that device’s worker. Individual scrcpy output lines are tagged with `[Alias]` so you can see which headset emitted a warning. Exiting the launcher (Ctrl+C) now shuts down all scrcpy/status subprocesses cleanly, closing every mirroring window.

### Quest 3 performance settings

The default monitoring stream is limited to **1280px / 30fps / 4Mbps** per headset (previously 1600px / uncapped fps / 10Mbps). This reduces capture, decoding and Wi-Fi load for multi-headset grids; it trades fine detail and frame rate for capacity. Thirty 4Mbps video streams target roughly 120Mbps before audio and protocol overhead, so usable capacity still depends on the access point and Mac.

Scrcpy 5.0.1 defaults to hardware decoding when available. The launcher uses `--hwdec=auto`, allowing VideoToolbox on macOS with software fallback. Older scrcpy versions remain usable with a warning, without the new flag. Keep the default H.264 codec for low latency; no crop or capture filters are added. Actual throughput and Horizon OS capture behavior must be checked on your headsets.

```bash
# Keep more detail / motion when mirroring fewer devices (also affects recordings).
MAX_SIZE=1600 MAX_FPS=60 BIT_RATE=10M ./scripts/quest_multi_scrcpy.sh start

# Add 50ms buffering to smooth Wi-Fi jitter; increases display latency.
VIDEO_BUFFER=50 ./scripts/quest_multi_scrcpy.sh start

# Diagnose host decoder issues with software decoding.
SCRCPY_HWDEC=disabled ./scripts/quest_multi_scrcpy.sh start

# Reduce audio/network load for a large monitoring grid.
./scripts/quest_multi_scrcpy.sh start --no-audio
```

`SCRCPY_HWDEC=videotoolbox` forces the Mac decoder and fails if unavailable; `auto` is preferred for normal sessions. `BIT_RATE`, `MAX_SIZE`, `MAX_FPS`, and `VIDEO_BUFFER` control streaming; recording uses the same encoded stream. Existing `SCRCPY_EXTRA_ARGS` can still supply encoder/renderer options.

### Recording filename pattern
Each headset produces unique filenames even if it disconnects and reconnects during a session. Examples:

```text
recordings/Quest-01_20240601-120000.mp4                # initial launch
recordings/Quest-01_20240601-120000_retry02.mp4        # internal retry during the same launch
recordings/Quest-01_20240601-120000_attempt02.mp4      # reconnect-triggered relaunch
recordings/Quest-01_20240601-120000_attempt02_retry03.mp4
```

Recent Quest OS builds can gate video capture behind the headset proximity sensor and sometimes glitch when Meta UI overlays are hovered. The launcher sends `KEYCODE_WAKEUP` before each launch and waits for the display. Additional Quest setprop/broadcast tweaks are **opt-in** with `--quest-tweaks`; they are not a guaranteed fix for Horizon OS capture issues. Customise them with:

```bash
./scripts/quest_multi_scrcpy.sh start --no-quest-tweaks          # skip all setprop/broadcast tweaks
./scripts/quest_multi_scrcpy.sh start --quest-tweaks --quest-no-guardian # enable proximity tweak, keep Guardian active
./scripts/quest_multi_scrcpy.sh start --quest-tweaks --quest-capture-width 1920 # set capture props when needed
QUEST_CAPTURE_BITRATE=30000000 \
QUEST_CAPTURE_FULL_RATE=1 QUEST_CAPTURE_EYE=2 \
./scripts/quest_multi_scrcpy.sh start --quest-tweaks --record
```

Tune the wake guard if you prefer to start scrcpy only after the wearer has the headset on:

```bash
./scripts/quest_multi_scrcpy.sh start --quest-awake-timeout 20 --quest-awake-poll 1
./scripts/quest_multi_scrcpy.sh start --quest-skip-awake-check   # fire immediately (legacy behaviour)
```

Use `scripts/quest_os_tweaks.sh engage` / `restore` outside the launcher when you need to toggle these flags manually (for example, before a supervised session or after a crash that left Guardian paused).

If flicker appears when the controller laser hovers Meta UI elements, keep cropping/angle flags disabled and restart the affected session. Automatic recovery handles process/connection failures; it cannot detect flicker or a frozen image while scrcpy is still running.

## Quest Hub PoC (GUI preview)

A single-headset proof of concept for the upcoming GUI (`docs/GUI_DESIGN.md` §7.1). It talks to the official scrcpy 5.0.1 server directly, shows the stream in the browser (WebCodecs), plays audio, and records Matroska (H.264 + Opus) without re-encoding.

```bash
./.mamba-env/bin/python -m questhub.poc                                  # first adb device -> http://127.0.0.1:8765
./.mamba-env/bin/python -m questhub.poc --serial 192.168.22.201:5555
./.mamba-env/bin/python -m questhub.poc --record-seconds 10                # headless recording into recordings/poc/
```

Open the page in Chrome. Use "録画開始" to start or stop recording, "音声を再生" to play audio, and the display menu to switch to key-frame-only (low fps) mode.

Load test (30 simulated headsets, optionally plus a real one; see `docs/GUI_DESIGN.md` §7.2):

```bash
./.mamba-env/bin/python tools/loadtest.py --devices 30 [--real <adb serial>]   # then open http://127.0.0.1:8790/?n=30&mode=full
```

Use `mode=keyframes` to test the low-fps viewer mode.

Tests:

```bash
./.mamba-env/bin/python -m unittest discover -s tests -v                    # offline, includes the media pipeline
QH_TEST_DEVICE=<adb serial> ./.mamba-env/bin/python -m unittest tests.test_poc_device -v   # real headset
```

## Updating Dependencies
Edit `environment.yml`, then regenerate both lock files and reinstall:

```bash
./install.sh --relock osx-arm64
./install.sh --relock osx-64
./install.sh
```

## Cleaning Up

```bash
./install.sh --uninstall --yes
```

## Additional Resources
- Detailed operational guide: `agents.md`
- Launch script: `scripts/quest_multi_scrcpy.sh`
- Sample device lists: `scripts/quest_devices.tsv`, `scripts/devices.env`
- Binary bootstrapper: `scripts/bootstrap_binaries.sh`

## Validation

```bash
python3 -m unittest discover -s tests -v
```

Tests simulate adb/scrcpy to check offline-device launch isolation, abnormal-exit retries, unique recordings, status-monitor isolation, settings, and child cleanup. For a real session, verify `scrcpy --version` reports 5.0.1, check the hardware decoder log, compare FPS with `SCRCPY_EXTRA_ARGS="--print-fps"`, and test disconnect/reconnect plus Ctrl+C with your actual Quest 3 devices.

Upstream references: [5.0.1 release](https://github.com/Genymobile/scrcpy/releases/tag/v5.0.1), [video and hardware decoding](https://github.com/Genymobile/scrcpy/blob/v5.0.1/doc/video.md), [macOS builds](https://github.com/Genymobile/scrcpy/blob/v5.0.1/doc/macos.md).
