#!/usr/bin/env bash
# keep_awake.sh — Toggle stay-awake/sleep settings on Quest devices listed in quest_devices.tsv
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
source "${SCRIPT_DIR}/_env_utils.sh"

usage() {
  cat <<'USAGE'
Usage: keep_awake.sh [--config FILE] [--enable|--disable]

Options:
  --config FILE  Quest device list (default: scripts/quest_devices.tsv)
  --enable       Keep device awake (default)
  --disable      Restore default sleep behaviour

The script applies:
  - "settings put global stay_on_while_plugged_in 7" to keep awake
  - "settings put global stay_on_while_plugged_in 0" to disable
  - "svc power stayon true/false" for immediate effect
USAGE
}

CONFIG="${SCRIPT_DIR}/quest_devices.tsv"
MODE="enable"

while (($#)); do
  case "$1" in
    --config)
      shift || { echo "[!] Missing value for --config" >&2; exit 1; }
      CONFIG="$1"
      ;;
    --config=*)
      CONFIG="${1#*=}"
      ;;
    --enable) MODE="enable" ;;
    --disable) MODE="disable" ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "[!] Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift || true
done

if [[ ! -f ${CONFIG} ]]; then
  echo "[!] CONFIG not found: ${CONFIG}" >&2
  exit 1
fi

if [[ -z ${ADB_BIN:-} ]]; then
  require_binary adb ADB_BIN
fi
[[ -n ${ADB_BIN:-} && -x ${ADB_BIN:-} ]] || { echo "[!] adb not found." >&2; exit 1; }
"${ADB_BIN}" start-server >/dev/null 2>&1

readarray=()
while IFS= read -r line || [[ -n ${line} ]]; do
  [[ ${line} =~ ^[[:space:]]*$ ]] && continue
  [[ ${line} =~ ^[[:space:]]*# ]] && continue
  readarray+=("${line}")

done < "${CONFIG}"

(( ${#readarray[@]} > 0 )) || { echo "[!] No devices in ${CONFIG}" >&2; exit 1; }

if [[ ${MODE} == "enable" ]]; then
  stay_value=7
  stay_cmd="stayon true"
  action_label="Enable"
else
  stay_value=0
  stay_cmd="stayon false"
  action_label="Disable"
fi

echo "[i] ${action_label} stay-awake for ${#readarray[@]} device(s)"

for entry in "${readarray[@]}"; do
  IFS=$'\t' read -r alias endpoint _ <<<"${entry}"
  [[ -n ${alias} && -n ${endpoint} ]] || continue
  echo "  -> ${alias} (${endpoint})"
  "${ADB_BIN}" connect "${endpoint}" >/dev/null 2>&1 || true
  "${ADB_BIN}" -s "${endpoint}" shell settings put global stay_on_while_plugged_in "${stay_value}" || true
  "${ADB_BIN}" -s "${endpoint}" shell svc power ${stay_cmd} || true
  "${ADB_BIN}" -s "${endpoint}" shell dumpsys power | grep -m1 "mStayOn" || true

done

echo "[+] Done."
