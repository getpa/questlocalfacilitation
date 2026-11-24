# Legacy CLI (scrcpy windows)

The original command-line tools: one scrcpy window per headset, arranged in a grid. The Quest Hub GUI (`../questhub.command`) replaces them for most uses. Do not run both on the same headsets at the same time; `quest_multi_scrcpy.sh` warns when the GUI is running.

Run the scripts from the repository root after `./install.sh`. They use `.mamba-env` automatically.

| Script | Purpose |
|--------|---------|
| `quest_multi_scrcpy.sh` | Mirror or record all headsets in `quest_devices.tsv` (`start`, `status`, `--dry-run`, `--quality`) |
| `usb_to_tcp.sh` | Switch USB-connected headsets to Wi-Fi ADB |
| `discover_quest3.py` | Scan the LAN for headsets listening on port 5555 |
| `check_reachability.sh` | Ping and ADB check for every listed headset |
| `keep_awake.sh` | Turn `stay_on_while_plugged_in` on or off |
| `quest_os_tweaks.sh` | Guardian, proximity and capture tweaks (`engage` / `restore`) |
| `restart_env_adb.sh` | Restart the adb daemon from `.mamba-env` |
| `runner.sh` | Example launcher |
| `quest_devices.tsv`, `devices.env` | Device lists (the GUI imports and exports the same TSV format) |

See `../agents.md` and `../README.md` for details.
