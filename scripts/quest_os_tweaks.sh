#!/usr/bin/env bash
# quest_os_tweaks.sh — Apply Meta Quest OS tweaks (proximity + guardian + capture props) across configured devices
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: quest_os_tweaks.sh <command> [options]

Commands:
  engage   Apply keep-awake tweaks (guardian pause, prox close, capture props)
  restore  Revert tweaks (guardian resume, prox open, clear capture props)

Options:
  --config FILE         Override device list (default: scripts/quest_devices.tsv)
  --no-guardian         Skip guardian pause/resume
  --no-prox             Skip proximity broadcast
  --capture-width PX    Set debug.oculus.capture.width (engage only)
  --capture-height PX   Set debug.oculus.capture.height (engage only)
  --capture-bitrate BPS Set debug.oculus.capture.bitrate (engage only)
  --capture-full-rate X Set debug.oculus.fullRateCapture (engage only)
  --capture-eye N       Set debug.oculus.screenCaptureEye (engage only)
  --help                Show this help

Environment overrides:
  CONFIG                  Same as --config
  QUEST_CAPTURE_WIDTH     Default for --capture-width
  QUEST_CAPTURE_HEIGHT    Default for --capture-height
  QUEST_CAPTURE_BITRATE   Default for --capture-bitrate
USAGE
}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONFIG=${CONFIG:-"${SCRIPT_DIR}/quest_devices.tsv"}
ACTION=${1:-}
[[ -n ${ACTION} ]] || { usage >&2; exit 1; }
shift || true

SKIP_GUARDIAN=false
SKIP_PROX=false
CAP_WIDTH=${QUEST_CAPTURE_WIDTH:-}
CAP_HEIGHT=${QUEST_CAPTURE_HEIGHT:-}
CAP_BITRATE=${QUEST_CAPTURE_BITRATE:-}
CAP_FULL_RATE=${QUEST_CAPTURE_FULL_RATE:-}
CAP_EYE=${QUEST_CAPTURE_EYE:-}

while (($#)); do
  case "$1" in
    --config)
      shift || { echo "[tweak][ERROR] Missing value for --config" >&2; exit 1; }
      CONFIG="$1"
      ;;
    --config=*)
      CONFIG="${1#*=}"
      ;;
    --no-guardian)
      SKIP_GUARDIAN=true
      ;;
    --no-prox)
      SKIP_PROX=true
      ;;
    --capture-width)
      shift || { echo "[tweak][ERROR] Missing value for --capture-width" >&2; exit 1; }
      CAP_WIDTH="$1"
      ;;
    --capture-width=*)
      CAP_WIDTH="${1#*=}"
      ;;
    --capture-height)
      shift || { echo "[tweak][ERROR] Missing value for --capture-height" >&2; exit 1; }
      CAP_HEIGHT="$1"
      ;;
    --capture-height=*)
      CAP_HEIGHT="${1#*=}"
      ;;
    --capture-bitrate)
      shift || { echo "[tweak][ERROR] Missing value for --capture-bitrate" >&2; exit 1; }
      CAP_BITRATE="$1"
      ;;
    --capture-bitrate=*)
      CAP_BITRATE="${1#*=}"
      ;;
    --capture-full-rate)
      shift || { echo "[tweak][ERROR] Missing value for --capture-full-rate" >&2; exit 1; }
      CAP_FULL_RATE="$1"
      ;;
    --capture-full-rate=*)
      CAP_FULL_RATE="${1#*=}"
      ;;
    --capture-eye)
      shift || { echo "[tweak][ERROR] Missing value for --capture-eye" >&2; exit 1; }
      CAP_EYE="$1"
      ;;
    --capture-eye=*)
      CAP_EYE="${1#*=}"
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "[tweak][ERROR] Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift || true
done

[[ -f ${CONFIG} ]] || { echo "[tweak][ERROR] CONFIG not found: ${CONFIG}" >&2; exit 1; }

case "${ACTION}" in
  engage|restore) : ;;
  *)
    echo "[tweak][ERROR] Unknown command: ${ACTION}" >&2
    usage >&2
    exit 1
    ;;
esac

ADB_BIN=${ADB_BIN:-$(command -v adb)}
[[ -n ${ADB_BIN:-} && -x ${ADB_BIN:-} ]] || { echo "[tweak][ERROR] adb not found." >&2; exit 1; }

mapfile -t DEVICES < <(grep -vE '^[[:space:]]*(#|$)' "${CONFIG}")
(( ${#DEVICES[@]} )) || { echo "[tweak][ERROR] No devices in ${CONFIG}" >&2; exit 1; }

echo "[tweak][INFO] ${ACTION} tweaks for ${#DEVICES[@]} device(s)"

for entry in "${DEVICES[@]}"; do
  IFS=$'\t' read -r alias endpoint _ <<<"${entry}"
  [[ -n ${alias} && -n ${endpoint} ]] || continue
  echo "  -> ${alias} (${endpoint})"
  "${ADB_BIN}" connect "${endpoint}" >/dev/null 2>&1 || true

  if [[ ${ACTION} == engage ]]; then
    if [[ ${SKIP_GUARDIAN} == false ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.guardian_pause 0 >/dev/null 2>&1 || true
    fi
    if [[ ${SKIP_PROX} == false ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell am broadcast -a com.oculus.vrpowermanager.prox_close >/dev/null 2>&1 || true
    fi
    if [[ -n ${CAP_WIDTH} ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.capture.width "${CAP_WIDTH}" >/dev/null 2>&1 || true
    fi
    if [[ -n ${CAP_HEIGHT} ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.capture.height "${CAP_HEIGHT}" >/dev/null 2>&1 || true
    fi
    if [[ -n ${CAP_BITRATE} ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.capture.bitrate "${CAP_BITRATE}" >/dev/null 2>&1 || true
    fi
    if [[ -n ${CAP_FULL_RATE} ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.fullRateCapture "${CAP_FULL_RATE}" >/dev/null 2>&1 || true
    fi
    if [[ -n ${CAP_EYE} ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.screenCaptureEye "${CAP_EYE}" >/dev/null 2>&1 || true
    fi
    "${ADB_BIN}" -s "${endpoint}" shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 || true
    "${ADB_BIN}" -s "${endpoint}" shell input keyevent KEYCODE_HOME >/dev/null 2>&1 || true
  else
    if [[ ${SKIP_GUARDIAN} == false ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.guardian_pause 1 >/dev/null 2>&1 || true
    fi
    if [[ ${SKIP_PROX} == false ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell am broadcast -a com.oculus.vrpowermanager.prox_open >/dev/null 2>&1 || true
    fi
    if [[ -n ${CAP_WIDTH} ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.capture.width "" >/dev/null 2>&1 || true
    fi
    if [[ -n ${CAP_HEIGHT} ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.capture.height "" >/dev/null 2>&1 || true
    fi
    if [[ -n ${CAP_BITRATE} ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.capture.bitrate "" >/dev/null 2>&1 || true
    fi
    if [[ -n ${CAP_FULL_RATE} ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.fullRateCapture "" >/dev/null 2>&1 || true
    fi
    if [[ -n ${CAP_EYE} ]]; then
      "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.screenCaptureEye "" >/dev/null 2>&1 || true
    fi
  fi
done

echo "[tweak][INFO] Done."
