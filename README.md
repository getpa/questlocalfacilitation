# Quest Local Facilitation

Toolkit for mirroring up to 30 Meta Quest 3 headsets over local Wi-Fi using scrcpy and adb from a macOS host.

## Prerequisites
- macOS (Apple Silicon or Intel)
- [Mambaforge](https://github.com/conda-forge/miniforge#mambaforge) or another conda-compatible installation that bundles `mamba`
- USB access to each Quest for the initial developer-mode pairing

## Host Build Dependencies
The bootstrap script compiles `scrcpy` from the `client-crop-option` fork by default. Install the following host tools once per Mac before running any bootstrap commands:

```bash
brew install git curl unzip meson ninja pkg-config sdl2 ffmpeg libusb openjdk@17
```

Then expose the JDK for Gradle during builds:

```bash
export JAVA_HOME="$(/usr/libexec/java_home -v 17)"
export PATH="$JAVA_HOME/bin:$PATH"
```

> Tip: If you prefer to use the official scrcpy release binaries instead, set `SCRCPY_BUILD_FROM_GIT=0` when invoking `bootstrap_binaries.sh`.

## Set Up the Environment
Create the project-local environment and install the required binaries into it:

```bash
mamba env create -p ./.mamba-env -f environment.yml
./scripts/bootstrap_binaries.sh ./.mamba-env
```

Activate it when working in the repo:

```bash
conda activate "${PWD}/.mamba-env"
```

The environment installs:
- CLI helpers (`jq`, GNU `sed`, `coreutils`, `curl`, `unzip`)
- Python runtime and `requests`
- Latest Android platform tools (`adb`, `fastboot`) and scrcpy binaries via `bootstrap_binaries.sh`

Verify the binaries inside the environment:

```bash
which adb
which scrcpy
scrcpy --version
adb version
```

## Configure Devices
1. Enable developer mode on each Quest 3 and authorize USB debugging.
2. (Optional) With devices still tethered over USB, run `./scripts/usb_to_tcp.sh --port 5555` to switch every detected headset into Wi-Fi ADB mode.
3. Populate `scripts/quest_devices.tsv` with aliases, IPs, and optional grid positions (up to 30 entries).
4. Mirror the same endpoints in `scripts/devices.env` if you plan to use the broadcast-input helper scripts.


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
conda activate "${PWD}/.mamba-env"
./scripts/discover_quest3.py          # auto-detect primary network
./scripts/discover_quest3.py --cidr 192.168.10.0/24 --connect
```

Pass `--connect` to run `adb connect` and collect model/serial metadata for each hit. The script also prints ready-to-paste rows for `scripts/quest_devices.tsv`.

## Running the Multi-Scrcpy Launcher
Activate the environment, then run:

```bash
./scripts/quest_multi_scrcpy.sh start                 # mirror + periodic battery table
./scripts/quest_multi_scrcpy.sh start --record       # add recording per device
./scripts/quest_multi_scrcpy.sh start --no-status    # disable battery polling during mirror
./scripts/quest_multi_scrcpy.sh start --status-interval 120
./scripts/quest_multi_scrcpy.sh status --interval 15 # standalone monitor (clears screen)
```

Environment variables such as `GRID_COLUMNS`, `DISPLAY_WIDTH`, and `RECORD_DIR` can be set inline to adjust window layout or recording targets. See `agents.md` for full documentation and best practices. Battery polling defaults to 60 s; tweak via `STATUS_INTERVAL` or `--status-interval`. If a headset becomes unreachable during a run, the watcher waits for it to come back online, retries `adb connect`, and relaunches scrcpy without spawning duplicate instances. Set `SCRCPY_BASE_PORT` if you need to shift the local port range used by scrcpy when multiple windows start in parallel; ports are auto-scanned for availability starting from that value. Use `SCRCPY_LAUNCH_DELAY` (seconds) if you need extra spacing between launches. By default the launcher feeds `--no-audio --no-clipboard` to each scrcpy instance; override with `SCRCPY_EXTRA_ARGS` if you want additional scrcpy flags (for example, `--display-id=0` or `--render-driver=opengl`). Launcher messages are prefixed with `[launcher]` to distinguish them from raw scrcpy/adb logs. Offline devices are retried automatically after two consecutive failed status checks to avoid thrashing scrcpy restarts. Individual scrcpy output lines are tagged with `[Alias]` so you can see which headset emitted a warning. Exiting the launcher (Ctrl+C) now shuts down all scrcpy/status subprocesses cleanly, closing every mirroring window.

### Recording filename pattern
Each headset produces unique filenames even if it disconnects and reconnects during a session. Examples:

```text
recordings/Quest-01_20240601-120000.mp4                # initial launch
recordings/Quest-01_20240601-120000_retry02.mp4        # internal retry during the same launch
recordings/Quest-01_20240601-120000_attempt02.mp4      # reconnect-triggered relaunch
recordings/Quest-01_20240601-120000_attempt02_retry03.mp4
```

Recent Quest OS builds gate video capture behind the headset proximity sensor and sometimes glitch when Meta UI overlays are hovered. The launcher now applies Meta-specific workarounds by default before each scrcpy instance starts: it pauses Guardian, fakes the proximity sensor (`prox_close` broadcast), replays `KEYCODE_WAKEUP`, and can set `debug.oculus.capture.*` properties if you provide values. Disable or customise this behaviour with:

```bash
./scripts/quest_multi_scrcpy.sh start --no-quest-tweaks          # skip all setprop/broadcast tweaks
./scripts/quest_multi_scrcpy.sh start --quest-no-guardian        # keep Guardian active
./scripts/quest_multi_scrcpy.sh start --quest-capture-width 1920 # set capture props when needed
QUEST_CAPTURE_BITRATE=30000000 \
QUEST_CAPTURE_FULL_RATE=1 QUEST_CAPTURE_EYE=2 \
./scripts/quest_multi_scrcpy.sh start --record
```

Tune the wake guard if you prefer to start scrcpy only after the wearer has the headset on:

```bash
./scripts/quest_multi_scrcpy.sh start --quest-awake-timeout 20 --quest-awake-poll 1
./scripts/quest_multi_scrcpy.sh start --quest-skip-awake-check   # fire immediately (legacy behaviour)
```

Use `scripts/quest_os_tweaks.sh engage` / `restore` outside the launcher when you need to toggle these flags manually (for example, before a supervised session or after a crash that left Guardian paused).

Flicker that appears when the controller laser hovers Meta UI elements is a known Horizon OS regression (v76+). The safest mitigations today are to keep cropping/angle flags disabled, wait until the headset is awake before mirroring, and restart the affected scrcpy window (Ctrl+C → relaunch or rerun the launcher for the specific alias) when the UI feed corrupts.

## Updating Dependencies
To refresh conda packages (CLI tooling) and re-fetch binaries:

```bash
conda activate "${PWD}/.mamba-env"
mamba update --all
./scripts/bootstrap_binaries.sh "${PWD}/.mamba-env"
```

## Cleaning Up
If you need to remove the environment entirely:

```bash
conda deactivate
rm -rf .mamba-env
```

## Additional Resources
- Detailed operational guide: `agents.md`
- Launch script: `scripts/quest_multi_scrcpy.sh`
- Sample device lists: `scripts/quest_devices.tsv`, `scripts/devices.env`
- Binary bootstrapper: `scripts/bootstrap_binaries.sh`
