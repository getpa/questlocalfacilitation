#!/usr/bin/env bash
# quest_multi_scrcpy.sh — 最大30台の Quest 3 を scrcpy で同時起動/監視するエントリーポイント
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
source "${SCRIPT_DIR}/_env_utils.sh"

usage() {
  cat <<'USAGE'
Usage: quest_multi_scrcpy.sh <command> [options]

Commands:
  start    複数 scrcpy インスタンスを起動（デフォルトでバッテリー状態も定期出力）
  status   端末バッテリー残量と給電状態を継続監視

start options:
  --record              各ウィンドウで scrcpy 録画を有効化 (デフォルト: 無効)
  --no-record           録画を明示的に無効化
  --audio               各ウィンドウで scrcpy 音声転送を有効化（= --audio-mode=dup）
  --no-audio            各ウィンドウで scrcpy 音声転送を無効化
  --audio-mode MODE     音声モード: dup | output | off (デフォルト: dup)
  --audio-fallback MODE dup 非対応時フォールバック: off | output (デフォルト: off)
  --dry-run             実行内容を表示するだけ（接続/起動はしない）
  --no-status           バッテリー監視を無効化
  --status-interval SEC  監視更新間隔（秒）
  --status-skip-connect  監視時に adb connect を行わない（デフォルト）
  --status-connect       監視時にも毎回 adb connect を実施
  --quest-tweaks         Quest OS 向けワークアラウンドを適用（デフォルト: 有効）
  --no-quest-tweaks      Quest OS ワークアラウンドを無効化
  --quest-no-guardian    Quest ワークアラウンド時に guardian 操作を行わない
  --quest-no-prox        Quest ワークアラウンド時に prox_close を送らない
  --quest-capture-width PX    Quest capture width を setprop で指定
  --quest-capture-height PX   Quest capture height を setprop で指定
  --quest-capture-bitrate BPS Quest capture bitrate を setprop で指定
  --quest-awake-timeout SEC   ディスプレイ点灯待ちのタイムアウト秒数
  --quest-awake-poll SEC      点灯チェックのポーリング間隔（少数可）
  --quest-skip-awake-check    ディスプレイ点灯確認を省略
  --quest-no-restore          終了時に setprop / prox_open を戻さない

status options:
  --interval SEC         更新間隔（秒）
  --skip-connect         監視時に adb connect を行わない

Environment overrides (必要に応じて export してください):
  CONFIG             = ./scripts/quest_devices.tsv 以外の端末リスト
  MAX_DEVICES        = 起動上限 (デフォルト: 30)
  GRID_COLUMNS       = 自動配置時の列数 (デフォルト: 5)
  GRID_ROWS          = 自動配置時の行数 (デフォルト: 6)
  DISPLAY_WIDTH      = メイン表示領域の幅 px (デフォルト: 2560)
  DISPLAY_HEIGHT     = メイン表示領域の高さ px (デフォルト: 1440)
  WINDOW_MARGIN      = ウィンドウ間マージン px (デフォルト: 16)
  BIT_RATE           = scrcpy ビットレート (デフォルト: 10M)
  MAX_SIZE           = scrcpy 最大長辺 px (デフォルト: 1600)
  RECORD_DIR         = 録画ファイル保存先 (デフォルト: ./recordings)
  STATUS_INTERVAL    = バッテリー監視の更新間隔（秒、デフォルト: 60）
  SCRCPY_BASE_PORT   = ローカルポートの開始番号 (デフォルト: 27183)
  SCRCPY_LAUNCH_DELAY = scrcpy 起動間隔 (秒、デフォルト: 0.4)
  SCRCPY_BASE_PORT   = ローカルポートの開始番号 (デフォルト: 27183)
  SCRCPY_AUDIO_MODE  = 音声モード: dup | output | off (デフォルト: dup)
  SCRCPY_AUDIO_FALLBACK = dup 非対応時フォールバック: off | output (デフォルト: off)
  QUEST_TWEAKS_ENABLED  = Quest ワークアラウンド適用可否 (デフォルト: true)
  QUEST_TWEAK_GUARDIAN  = guardian_pause setprop 実行可否 (デフォルト: true)
  QUEST_TWEAK_PROX      = prox_close/prox_open ブロードキャスト可否 (デフォルト: true)
  QUEST_CAPTURE_WIDTH   = debug.oculus.capture.width のデフォルト値
  QUEST_CAPTURE_HEIGHT  = debug.oculus.capture.height のデフォルト値
  QUEST_CAPTURE_BITRATE = debug.oculus.capture.bitrate のデフォルト値
  QUEST_CAPTURE_FULL_RATE = debug.oculus.fullRateCapture のデフォルト値
  QUEST_CAPTURE_EYE    = debug.oculus.screenCaptureEye のデフォルト値
  QUEST_REQUIRE_AWAKE   = scrcpy 起動前に Display ON を待つか (デフォルト: true)
  QUEST_AWAKE_TIMEOUT   = Display ON 待機タイムアウト秒 (デフォルト: 10)
  QUEST_AWAKE_POLL      = Display ON ポーリング間隔 (デフォルト: 0.5)
  QUEST_RESTORE_ON_EXIT = 終了時に setprop と prox_open を戻すか (デフォルト: true)
USAGE
}

log_info() {
  printf '[launcher][INFO] %s\n' "$*"
}

log_warn() {
  printf '[launcher][WARN] %s\n' "$*" >&2
}

log_error() {
  printf '[launcher][ERROR] %s\n' "$*" >&2
}

format_command() {
  local formatted="" arg
  for arg in "$@"; do
    formatted+=" $(printf '%q' "$arg")"
  done
  printf '%s' "${formatted# }"
}

dry_run_command() {
  [[ ${DRY_RUN:-false} == true ]] || return 0
  printf '[dry-run] %s\n' "$(format_command "$@")"
}

run_cmd() {
  dry_run_command "$@"
  if [[ ${DRY_RUN:-false} == true ]]; then
    return 0
  fi
  "$@"
}

run_cmd_quiet() {
  dry_run_command "$@"
  if [[ ${DRY_RUN:-false} == true ]]; then
    return 0
  fi
  "$@" >/dev/null 2>&1
}

ensure_positive_integer() {
  local value="$1"
  if [[ ! ${value} =~ ^[0-9]+$ ]] || (( value <= 0 )); then
    log_error "Invalid interval: ${value}"
    exit 1
  fi
}

ensure_positive_number() {
  local value="$1"
  if ! printf '%s\n' "${value}" | awk '/^[0-9]*\.?[0-9]+$/ { if ($0+0>0) ok=1 } END { exit ok?0:1 }'; then
    log_error "Invalid value: ${value}"
    exit 1
  fi
}

ACTION=${1:-}
if [[ -n ${ACTION} ]]; then
  shift || true
fi

CONFIG_DEFAULT="${SCRIPT_DIR}/quest_devices.tsv"
CONFIG=${CONFIG:-"${CONFIG_DEFAULT}"}

STATUS_INTERVAL=${STATUS_INTERVAL:-60}
STATUS_ENABLED=true
STATUS_SKIP_CONNECT=false
RECORD_MODE="off"
DRY_RUN=false
BASE_PORT=${SCRCPY_BASE_PORT:-27183}
QUEST_TWEAKS_ENABLED=${QUEST_TWEAKS_ENABLED:-true}
QUEST_TWEAK_GUARDIAN=${QUEST_TWEAK_GUARDIAN:-true}
QUEST_TWEAK_PROX=${QUEST_TWEAK_PROX:-true}
QUEST_CAPTURE_WIDTH=${QUEST_CAPTURE_WIDTH:-}
QUEST_CAPTURE_HEIGHT=${QUEST_CAPTURE_HEIGHT:-}
QUEST_CAPTURE_BITRATE=${QUEST_CAPTURE_BITRATE:-}
QUEST_CAPTURE_FULL_RATE=${QUEST_CAPTURE_FULL_RATE:-}
QUEST_CAPTURE_EYE=${QUEST_CAPTURE_EYE:-}
QUEST_REQUIRE_AWAKE=${QUEST_REQUIRE_AWAKE:-true}
QUEST_AWAKE_TIMEOUT=${QUEST_AWAKE_TIMEOUT:-10}
QUEST_AWAKE_POLL=${QUEST_AWAKE_POLL:-0.5}
QUEST_RESTORE_ON_EXIT=${QUEST_RESTORE_ON_EXIT:-true}
SCRCPY_AUDIO_MODE=${SCRCPY_AUDIO_MODE:-dup}
SCRCPY_AUDIO_FALLBACK=${SCRCPY_AUDIO_FALLBACK:-off}

if [[ -n ${SCRCPY_EXTRA_ARGS:-} ]]; then
  read -r -a SCRCPY_EXTRA_ARRAY <<<"${SCRCPY_EXTRA_ARGS}"
else
  SCRCPY_EXTRA_ARRAY=(--no-clipboard )
fi

ensure_audio_mode() {
  local mode="$1"
  case "${mode}" in
    dup|output|off) ;;
    *)
      log_error "Invalid audio mode: ${mode} (expected: dup | output | off)"
      exit 1
      ;;
  esac
}

ensure_audio_fallback_mode() {
  local mode="$1"
  case "${mode}" in
    off|output) ;;
    *)
      log_error "Invalid audio fallback mode: ${mode} (expected: off | output)"
      exit 1
      ;;
  esac
}

strip_managed_audio_args() {
  local arg
  local -a filtered=()
  for arg in "${SCRCPY_EXTRA_ARRAY[@]}"; do
    case "${arg}" in
      --no-audio|--audio-dup|--audio-source=*) ;;
      *) filtered+=("${arg}") ;;
    esac
  done
  SCRCPY_EXTRA_ARRAY=("${filtered[@]}")
}

get_device_sdk() {
  local endpoint="$1" sdk=""
  if [[ ${DRY_RUN:-false} == true ]]; then
    dry_run_command "${ADB_BIN}" -s "${endpoint}" shell getprop ro.build.version.sdk
    printf '33'
    return 0
  fi

  sdk=$("${ADB_BIN}" -s "${endpoint}" shell getprop ro.build.version.sdk 2>/dev/null | tr -d '\r')
  if [[ ${sdk} =~ ^[0-9]+$ ]]; then
    printf '%s' "${sdk}"
    return 0
  fi
  return 1
}

DEVICE_AUDIO_MODE_EFFECTIVE=""
DEVICE_AUDIO_SDK=""
DEVICE_AUDIO_FALLBACK_USED=false
DEVICE_AUDIO_FALLBACK_REASON=""
DEVICE_AUDIO_ARGS=()

build_device_audio_args() {
  local alias="$1" endpoint="$2"
  DEVICE_AUDIO_MODE_EFFECTIVE="${SCRCPY_AUDIO_MODE}"
  DEVICE_AUDIO_SDK=""
  DEVICE_AUDIO_FALLBACK_USED=false
  DEVICE_AUDIO_FALLBACK_REASON=""
  DEVICE_AUDIO_ARGS=()

  case "${SCRCPY_AUDIO_MODE}" in
    off)
      DEVICE_AUDIO_ARGS+=("--no-audio")
      ;;
    output)
      ;;
    dup)
      if DEVICE_AUDIO_SDK=$(get_device_sdk "${endpoint}"); then
        if (( DEVICE_AUDIO_SDK >= 33 )); then
          DEVICE_AUDIO_ARGS+=("--audio-source=playback" "--audio-dup")
        else
          DEVICE_AUDIO_MODE_EFFECTIVE="${SCRCPY_AUDIO_FALLBACK}"
          DEVICE_AUDIO_FALLBACK_USED=true
          DEVICE_AUDIO_FALLBACK_REASON="sdk=${DEVICE_AUDIO_SDK} (<33)"
        fi
      else
        DEVICE_AUDIO_MODE_EFFECTIVE="${SCRCPY_AUDIO_FALLBACK}"
        DEVICE_AUDIO_FALLBACK_USED=true
        DEVICE_AUDIO_FALLBACK_REASON="sdk=unknown"
      fi
      ;;
  esac

  if [[ ${DEVICE_AUDIO_MODE_EFFECTIVE} == "off" ]]; then
    DEVICE_AUDIO_ARGS=("--no-audio")
  elif [[ ${DEVICE_AUDIO_MODE_EFFECTIVE} == "output" ]]; then
    DEVICE_AUDIO_ARGS=()
  fi

  if [[ ${DEVICE_AUDIO_FALLBACK_USED} == true ]]; then
    log_warn "${alias}: audio mode fallback ${SCRCPY_AUDIO_MODE} -> ${DEVICE_AUDIO_MODE_EFFECTIVE} (${DEVICE_AUDIO_FALLBACK_REASON})"
  fi
}

is_port_in_use() {
  local port="$1"
  if [[ ${DRY_RUN:-false} == true ]]; then
    dry_run_command lsof -PiTCP:"${port}" -sTCP:LISTEN -n
    return 1
  fi
  lsof -PiTCP:"${port}" -sTCP:LISTEN -n >/dev/null 2>&1
}

find_free_port() {
  local port="$1" attempts=0
  while (( attempts < 100 )); do
    if ! is_port_in_use "${port}"; then
      echo "${port}"
      return 0
    fi
    port=$((port + 1))
    attempts=$((attempts + 1))
  done
  return 1
}

wait_for_device() {
  local endpoint="$1" attempts=0 state
  if [[ ${DRY_RUN:-false} == true ]]; then
    dry_run_command "${ADB_BIN}" connect "${endpoint}"
    dry_run_command "${ADB_BIN}" -s "${endpoint}" get-state
    dry_run_command sleep 0.5
    return 0
  fi
  while (( attempts < 12 )); do
    "${ADB_BIN}" connect "${endpoint}" >/dev/null 2>&1 || true
    state=$("${ADB_BIN}" -s "${endpoint}" get-state 2>/dev/null | tr -d '\r') || state=""
    if [[ ${state} == device ]]; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 0.5
  done
  return 1
}

restart_scrcpy() {
  local endpoint="$1"
  if command -v pkill >/dev/null 2>&1; then
    run_cmd_quiet pkill -f "scrcpy --serial=${endpoint}" || true
  fi
}

apply_quest_tweaks() {
  local alias="$1" endpoint="$2"
  [[ ${QUEST_TWEAKS_ENABLED} == true ]] || return 0
  if [[ ${QUEST_TWEAK_GUARDIAN} == true ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.guardian_pause 0 || true
  fi
  if [[ ${QUEST_TWEAK_PROX} == true ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell am broadcast -a com.oculus.vrpowermanager.prox_close || true
  fi
  if [[ -n ${QUEST_CAPTURE_WIDTH} ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.capture.width "${QUEST_CAPTURE_WIDTH}" || true
  fi
  if [[ -n ${QUEST_CAPTURE_HEIGHT} ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.capture.height "${QUEST_CAPTURE_HEIGHT}" || true
  fi
  if [[ -n ${QUEST_CAPTURE_BITRATE} ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.capture.bitrate "${QUEST_CAPTURE_BITRATE}" || true
  fi
  if [[ -n ${QUEST_CAPTURE_FULL_RATE} ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.fullRateCapture "${QUEST_CAPTURE_FULL_RATE}" || true
  fi
  if [[ -n ${QUEST_CAPTURE_EYE} ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.screenCaptureEye "${QUEST_CAPTURE_EYE}" || true
  fi
  run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell input keyevent KEYCODE_WAKEUP || true
  run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell input keyevent KEYCODE_HOME || true
  log_info "${alias}: applied Quest keep-awake tweaks"
}

restore_quest_tweaks() {
  local alias="$1" endpoint="$2"
  [[ ${QUEST_TWEAKS_ENABLED} == true && ${QUEST_RESTORE_ON_EXIT} == true ]] || return 0
  if [[ ${QUEST_TWEAK_GUARDIAN} == true ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.guardian_pause 1 || true
  fi
  if [[ ${QUEST_TWEAK_PROX} == true ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell am broadcast -a com.oculus.vrpowermanager.prox_open || true
  fi
  if [[ -n ${QUEST_CAPTURE_WIDTH} ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.capture.width "" || true
  fi
  if [[ -n ${QUEST_CAPTURE_HEIGHT} ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.capture.height "" || true
  fi
  if [[ -n ${QUEST_CAPTURE_BITRATE} ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.capture.bitrate "" || true
  fi
  if [[ -n ${QUEST_CAPTURE_FULL_RATE} ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.fullRateCapture "" || true
  fi
  if [[ -n ${QUEST_CAPTURE_EYE} ]]; then
    run_cmd_quiet "${ADB_BIN}" -s "${endpoint}" shell setprop debug.oculus.screenCaptureEye "" || true
  fi
  log_info "${alias}: restored Quest OS tweaks"
}

get_display_state() {
  local endpoint="$1" state_line="" state=""
  if [[ ${DRY_RUN:-false} == true ]]; then
    dry_run_command "${ADB_BIN}" -s "${endpoint}" shell dumpsys power
    dry_run_command "${ADB_BIN}" -s "${endpoint}" shell dumpsys display
    dry_run_command "${ADB_BIN}" -s "${endpoint}" shell dumpsys window policy
    printf 'ON'
    return 0
  fi

  state_line=$("${ADB_BIN}" -s "${endpoint}" shell dumpsys power 2>/dev/null | awk '/Display Power:/ {print; exit}') || state_line=""
  if [[ ${state_line} =~ state=([A-Za-z_]+) ]]; then
    state=${BASH_REMATCH[1]}
  elif [[ ${state_line} =~ STATE_([A-Za-z_]+) ]]; then
    state=${BASH_REMATCH[1]}
  elif [[ ${state_line} =~ STATE=([A-Za-z_]+) ]]; then
    state=${BASH_REMATCH[1]}
  fi

  if [[ -z ${state} ]]; then
    state_line=$("${ADB_BIN}" -s "${endpoint}" shell dumpsys display 2>/dev/null | awk '/mPowerState=/ {print; exit}') || state_line=""
    if [[ ${state_line} =~ mPowerState=([A-Za-z_]+) ]]; then
      state=${BASH_REMATCH[1]}
    fi
  fi

  if [[ -z ${state} ]]; then
    state_line=$("${ADB_BIN}" -s "${endpoint}" shell dumpsys window policy 2>/dev/null | awk '/mScreenOnFully/ {print; exit}') || state_line=""
    if [[ ${state_line} =~ mScreenOnFully=([A-Za-z_]+) ]]; then
      if [[ ${BASH_REMATCH[1]} == "true" ]]; then
        state="ON"
      fi
    elif [[ ${state_line} =~ mScreenOnFully[[:space:]]+([A-Za-z_]+) ]]; then
      if [[ ${BASH_REMATCH[1]} == "true" ]]; then
        state="ON"
      fi
    fi
  fi

  if [[ -z ${state} ]]; then
    state="UNKNOWN"
  fi

  state=$(printf '%s' "${state}" | tr '[:lower:]' '[:upper:]')
  printf '%s' "${state}"
}

wait_for_display_awake() {
  local alias="$1" endpoint="$2"
  [[ ${QUEST_REQUIRE_AWAKE} == true ]] || return 0
  local start now state
  start=$(date +%s)
  while true; do
    state=$(get_display_state "${endpoint}")
    if [[ ${state} == "UNKNOWN" ]]; then
      log_info "${alias}: display state could not be determined (continuing)"
      return 0
    fi
    if [[ ${state} == "ON" || ${state} == "ONLINE" || ${state} == "STATE_ON" || ${state} == "DISPLAY_STATE_ON" ]]; then
      return 0
    fi
    if [[ ${state} == *"ON"* && ${state} != "UNKNOWN" ]]; then
      return 0
    fi
    now=$(date +%s)
    if (( now - start >= QUEST_AWAKE_TIMEOUT )); then
      log_warn "${alias}: display did not report ON within ${QUEST_AWAKE_TIMEOUT}s (state=${state:-unknown})"
      return 1
    fi
    sleep "${QUEST_AWAKE_POLL}"
  done
}

case "${ACTION:-}" in
  start)
    STATUS_SKIP_CONNECT=true
    while (($#)); do
      case "$1" in
        --record) RECORD_MODE="on" ;;
        --no-record) RECORD_MODE="off" ;;
        --audio) SCRCPY_AUDIO_MODE="dup" ;;
        --no-audio) SCRCPY_AUDIO_MODE="off" ;;
        --audio-mode)
          shift || { log_error "Missing value for --audio-mode"; exit 1; }
          ensure_audio_mode "$1"
          SCRCPY_AUDIO_MODE="$1"
          ;;
        --audio-mode=*)
          value=${1#*=}
          ensure_audio_mode "${value}"
          SCRCPY_AUDIO_MODE="${value}"
          ;;
        --audio-fallback)
          shift || { log_error "Missing value for --audio-fallback"; exit 1; }
          ensure_audio_fallback_mode "$1"
          SCRCPY_AUDIO_FALLBACK="$1"
          ;;
        --audio-fallback=*)
          value=${1#*=}
          ensure_audio_fallback_mode "${value}"
          SCRCPY_AUDIO_FALLBACK="${value}"
          ;;
        --dry-run) DRY_RUN=true ;;
        --no-status) STATUS_ENABLED=false ;;
        --status-interval)
          shift || { log_error "Missing value for --status-interval"; exit 1; }
          ensure_positive_integer "$1"
          STATUS_INTERVAL="$1"
          ;;
        --status-interval=*)
          value=${1#*=}
          ensure_positive_integer "${value}"
          STATUS_INTERVAL="${value}"
          ;;
        --status-skip-connect) STATUS_SKIP_CONNECT=true ;;
        --status-connect) STATUS_SKIP_CONNECT=false ;;
        --quest-tweaks) QUEST_TWEAKS_ENABLED=true ;;
        --no-quest-tweaks) QUEST_TWEAKS_ENABLED=false ;;
        --quest-no-guardian) QUEST_TWEAK_GUARDIAN=false ;;
        --quest-no-prox) QUEST_TWEAK_PROX=false ;;
        --quest-capture-width)
          shift || { log_error "Missing value for --quest-capture-width"; exit 1; }
          ensure_positive_integer "$1"
          QUEST_CAPTURE_WIDTH="$1"
          ;;
        --quest-capture-width=*)
          value=${1#*=}
          ensure_positive_integer "${value}"
          QUEST_CAPTURE_WIDTH="${value}"
          ;;
        --quest-capture-height)
          shift || { log_error "Missing value for --quest-capture-height"; exit 1; }
          ensure_positive_integer "$1"
          QUEST_CAPTURE_HEIGHT="$1"
          ;;
        --quest-capture-height=*)
          value=${1#*=}
          ensure_positive_integer "${value}"
          QUEST_CAPTURE_HEIGHT="${value}"
          ;;
        --quest-capture-bitrate)
          shift || { log_error "Missing value for --quest-capture-bitrate"; exit 1; }
          ensure_positive_integer "$1"
          QUEST_CAPTURE_BITRATE="$1"
          ;;
        --quest-capture-bitrate=*)
          value=${1#*=}
          ensure_positive_integer "${value}"
          QUEST_CAPTURE_BITRATE="${value}"
          ;;
        --quest-skip-awake-check) QUEST_REQUIRE_AWAKE=false ;;
        --quest-awake-timeout)
          shift || { log_error "Missing value for --quest-awake-timeout"; exit 1; }
          ensure_positive_integer "$1"
          QUEST_AWAKE_TIMEOUT="$1"
          ;;
        --quest-awake-timeout=*)
          value=${1#*=}
          ensure_positive_integer "${value}"
          QUEST_AWAKE_TIMEOUT="${value}"
          ;;
        --quest-awake-poll)
          shift || { log_error "Missing value for --quest-awake-poll"; exit 1; }
          ensure_positive_number "$1"
          QUEST_AWAKE_POLL="$1"
          ;;
        --quest-awake-poll=*)
          value=${1#*=}
          ensure_positive_number "${value}"
          QUEST_AWAKE_POLL="${value}"
          ;;
        --quest-no-restore) QUEST_RESTORE_ON_EXIT=false ;;
        --record=*)
          RECORD_MODE="on"
          ;;
        --no-record=*)
          RECORD_MODE="off"
          ;;
        *) log_error "Unknown option for start: $1"; usage >&2; exit 1 ;;
      esac
      shift || true
    done
    ;;
  status)
    STATUS_SKIP_CONNECT=false
    while (($#)); do
      case "$1" in
        --interval)
          shift || { log_error "Missing value for --interval"; exit 1; }
          ensure_positive_integer "$1"
          STATUS_INTERVAL="$1"
          ;;
        --interval=*)
          value=${1#*=}
          ensure_positive_integer "${value}"
          STATUS_INTERVAL="${value}"
          ;;
        --skip-connect) STATUS_SKIP_CONNECT=true ;;
        *) log_error "Unknown option for status: $1"; usage >&2; exit 1 ;;
      esac
      shift || true
    done
    ;;
  "")
    usage >&2
    exit 1
    ;;
  *)
    log_error "Unknown command: ${ACTION}"
    usage >&2
    exit 1
    ;;
esac

ensure_audio_mode "${SCRCPY_AUDIO_MODE}"
ensure_audio_fallback_mode "${SCRCPY_AUDIO_FALLBACK}"
strip_managed_audio_args

ensure_positive_integer "${STATUS_INTERVAL}"

if [[ -z ${ADB_BIN:-} ]]; then
  if ! ADB_BIN=$(resolve_binary adb); then
    ADB_BIN=""
  fi
fi
[[ -n ${ADB_BIN:-} && -x ${ADB_BIN:-} ]] || { log_error "adb not found."; exit 1; }

if [[ ${ACTION} == "start" ]]; then
  extra_scrcpy_paths=()
  if [[ -n ${ENV_PREFIX:-} ]]; then
    extra_scrcpy_paths+=(
      "${ENV_PREFIX}/vendor/scrcpy-client-crop/bin/scrcpy"
    )
    if [[ -d "${ENV_PREFIX}/vendor" ]]; then
      while IFS= read -r path; do
        extra_scrcpy_paths+=("${path}")
      done < <(find "${ENV_PREFIX}/vendor" -maxdepth 3 -type f -name scrcpy -print 2>/dev/null)
    fi
  fi
  if [[ -z ${SCRCPY_BIN:-} ]]; then
    if ! SCRCPY_BIN=$(resolve_binary scrcpy "${extra_scrcpy_paths[@]}"); then
      SCRCPY_BIN=""
    fi
  fi
  [[ -n ${SCRCPY_BIN:-} && -x ${SCRCPY_BIN:-} ]] || { log_error "scrcpy not found."; exit 1; }
fi

[[ -f "${CONFIG}" ]] || { log_error "CONFIG not found: ${CONFIG}"; exit 1; }

RAW_DEVICES=()
while IFS= read -r line || [[ -n ${line} ]]; do
  [[ ${line} =~ ^[[:space:]]*$ ]] && continue
  [[ ${line} =~ ^[[:space:]]*# ]] && continue
  RAW_DEVICES+=("${line}")
done < "${CONFIG}"

(( ${#RAW_DEVICES[@]} > 0 )) || { log_error "No devices defined in ${CONFIG}"; exit 1; }

ALIASES=()
ENDPOINTS=()
COLS=()
ROWS=()
UNREACHABLE_COUNTS=()
SCRCPY_PIDS=()
LAUNCH_ATTEMPTS=()
STATUS_PID=""

for entry in "${RAW_DEVICES[@]}"; do
  IFS=$'\t' read -r alias endpoint col row <<<"${entry}"
  [[ -n ${alias} && -n ${endpoint} ]] || {
    log_error "Invalid line in ${CONFIG}: ${entry}"
    exit 1
  }
  ALIASES+=("${alias}")
  ENDPOINTS+=("${endpoint}")
  COLS+=("${col:-}")
  ROWS+=("${row:-}")
  UNREACHABLE_COUNTS+=(0)
  LAUNCH_ATTEMPTS+=(0)

done

COUNT=${#ALIASES[@]}

if [[ ${ACTION} == "start" ]]; then
  MAX_DEVICES=${MAX_DEVICES:-30}
  GRID_COLUMNS=${GRID_COLUMNS:-5}
  GRID_ROWS=${GRID_ROWS:-6}
  DISPLAY_WIDTH=${DISPLAY_WIDTH:-2560}
  DISPLAY_HEIGHT=${DISPLAY_HEIGHT:-1440}
  WINDOW_MARGIN=${WINDOW_MARGIN:-16}
  BIT_RATE=${BIT_RATE:-10M}
  MAX_SIZE=${MAX_SIZE:-1600}
  RECORD_DIR=${RECORD_DIR:-"$(pwd)/recordings"}
  TIMESTAMP=$(date +%Y%m%d-%H%M%S)

  (( COUNT <= MAX_DEVICES )) || {
    log_error "Device count ${COUNT} exceeds MAX_DEVICES=${MAX_DEVICES}"
    exit 1
  }
  (( GRID_COLUMNS > 0 && GRID_ROWS > 0 )) || {
    log_error "GRID_COLUMNS and GRID_ROWS must be positive."
    exit 1
  }

  WINDOW_WIDTH=$(( (DISPLAY_WIDTH - (GRID_COLUMNS - 1) * WINDOW_MARGIN) / GRID_COLUMNS ))
  WINDOW_HEIGHT=$(( (DISPLAY_HEIGHT - (GRID_ROWS - 1) * WINDOW_MARGIN) / GRID_ROWS ))
  (( WINDOW_WIDTH > 0 && WINDOW_HEIGHT > 0 )) || {
    log_error "Computed window size is invalid. Adjust DISPLAY_* or GRID_* values."
    exit 1
  }

  if [[ ${RECORD_MODE} == "on" ]]; then
    run_cmd mkdir -p "${RECORD_DIR}"
  fi
fi

start_device() {
  local alias="$1" endpoint="$2" col="$3" row="$4" index="$5"
  local launch_count=${LAUNCH_ATTEMPTS[$index]}
  launch_count=$((launch_count + 1))
  LAUNCH_ATTEMPTS[$index]=$launch_count

  local x y
  if [[ -n ${col} && -n ${row} ]]; then
    x=$(( col * (WINDOW_WIDTH + WINDOW_MARGIN) ))
    y=$(( row * (WINDOW_HEIGHT + WINDOW_MARGIN) ))
  else
    local c=$(( index % GRID_COLUMNS ))
    local r=$(( index / GRID_COLUMNS ))
    x=$(( c * (WINDOW_WIDTH + WINDOW_MARGIN) ))
    y=$(( r * (WINDOW_HEIGHT + WINDOW_MARGIN) ))
  fi

  local title="scrcpy - ${alias} (${endpoint})"
  local sanitized_alias="" record_base=""
  if [[ ${RECORD_MODE} == "on" ]]; then
    sanitized_alias=$(echo "${alias}" | tr -cs '[:alnum:]_-' '_')
    local attempt_suffix=""
    if (( launch_count > 1 )); then
      attempt_suffix=$(printf '_attempt%02d' "${launch_count}")
    fi
    record_base="${RECORD_DIR}/${sanitized_alias}_${TIMESTAMP}${attempt_suffix}"
  fi

  local requested_port=$(( BASE_PORT + index ))
  local port
  if ! port=$(find_free_port "${requested_port}"); then
    log_error "${alias}: unable to find free port starting at ${requested_port}"
    return 1
  fi

  if [[ ${DRY_RUN:-false} == true ]]; then
    log_info "[dry-run] preparing launch for ${alias} (${endpoint}) at x=${x}, y=${y}, port=${port}"
  else
    log_info "Launching ${alias} (${endpoint}) at x=${x}, y=${y}, port=${port}"
  fi

  if ! wait_for_device "${endpoint}"; then
    log_warn "${alias}: device offline, skipping scrcpy launch"
    return 0
  fi

  build_device_audio_args "${alias}" "${endpoint}"
  log_info "${alias}: audio mode requested=${SCRCPY_AUDIO_MODE} effective=${DEVICE_AUDIO_MODE_EFFECTIVE}${DEVICE_AUDIO_SDK:+ (sdk=${DEVICE_AUDIO_SDK})}"

  apply_quest_tweaks "${alias}" "${endpoint}"
  wait_for_display_awake "${alias}" "${endpoint}" || true

  restart_scrcpy "${endpoint}"

  local -a cmd_base=(
    "${SCRCPY_BIN}"
    "--serial=${endpoint}"
    "--window-title=${title}"
    "--window-x=${x}" "--window-y=${y}"
    "--window-width=${WINDOW_WIDTH}" "--window-height=${WINDOW_HEIGHT}"
    "--video-bit-rate=${BIT_RATE}"
    "--max-size=${MAX_SIZE}"
    "--stay-awake"
    "--port=${port}"
  )
  if (( ${#SCRCPY_EXTRA_ARRAY[@]} )); then
    cmd_base+=("${SCRCPY_EXTRA_ARRAY[@]}")
  fi
  if (( ${#DEVICE_AUDIO_ARGS[@]} )); then
    cmd_base+=("${DEVICE_AUDIO_ARGS[@]}")
  fi

  if [[ ${DRY_RUN:-false} == true ]]; then
    local -a preview_cmd=("${cmd_base[@]}")
    if [[ ${RECORD_MODE} == "on" ]]; then
      preview_cmd+=("--record=${record_base}.mp4")
    fi
    dry_run_command "${preview_cmd[@]}"
    dry_run_command sleep "${SCRCPY_LAUNCH_DELAY:-0.4}"
    return 0
  fi
  (
    local retry_counter=0
    while true; do
      retry_counter=$((retry_counter + 1))
      local record_path="" retry_suffix=""
      local -a cmd=("${cmd_base[@]}")
      if [[ ${RECORD_MODE} == "on" ]]; then
        if (( retry_counter > 1 )); then
          retry_suffix=$(printf '_retry%02d' "${retry_counter}")
        fi
        record_path="${record_base}${retry_suffix}.mp4"
        cmd+=("--record=${record_path}")
      fi
      "${cmd[@]}" 2>&1 | while IFS= read -r line; do
        printf '[%s] %s\n' "${alias}" "${line}"
      done
      status=${PIPESTATUS[0]:-0}
      if (( status == 0 )); then
        log_info "scrcpy exited normally for ${alias}"
        break
      fi
      log_warn "${alias}: scrcpy exited with code ${status} (attempt ${retry_counter}), retrying in 3s"
      sleep 3
    done
  ) &
  SCRCPY_PIDS+=("$!")

  sleep "${SCRCPY_LAUNCH_DELAY:-0.4}"
}

status_device() {
  local alias="$1" endpoint="$2" index="$3"

  if [[ ${STATUS_SKIP_CONNECT} != true ]]; then
    "${ADB_BIN}" connect "${endpoint}" >/dev/null 2>&1 || true
  fi

  local output
  if ! output=$("${ADB_BIN}" -s "${endpoint}" shell dumpsys battery 2>/dev/null); then
    printf '    %-12s %-22s %s\n' "${alias}" "${endpoint}" "(unreachable)"
    if [[ ${ACTION} == "start" && ${DRY_RUN} == false ]]; then
      local count=${UNREACHABLE_COUNTS[$index]:-0}
      count=$((count + 1))
      UNREACHABLE_COUNTS[$index]=$count
      if (( count == 1 )); then
        log_warn "${alias}: unreachable (will retry next cycle)"
      elif (( count >= 2 )); then
        log_warn "${alias}: unreachable for ${count} checks, restarting scrcpy"
        restart_scrcpy "${endpoint}"
        start_device "${alias}" "${endpoint}" "${COLS[$index]}" "${ROWS[$index]}" "${index}"
        UNREACHABLE_COUNTS[$index]=0
      fi
    fi
    return
  fi

  UNREACHABLE_COUNTS[$index]=0

  local level status_code ac usb wireless
  level=$(printf '%s\n' "${output}" | awk -F': ' '/level:/ {print $2; exit}')
  status_code=$(printf '%s\n' "${output}" | awk -F': ' '/status:/ {print $2; exit}')
  ac=$(printf '%s\n' "${output}" | awk -F': ' '/AC powered:/ {print $2; exit}')
  usb=$(printf '%s\n' "${output}" | awk -F': ' '/USB powered:/ {print $2; exit}')
  wireless=$(printf '%s\n' "${output}" | awk -F': ' '/Wireless powered:/ {print $2; exit}')

  [[ -n ${level} ]] || level='?'
  [[ -n ${status_code} ]] || status_code='?'

  local status_label
  case ${status_code} in
    1) status_label='Unknown' ;;
    2) status_label='Charging' ;;
    3) status_label='Discharging' ;;
    4) status_label='Not charging' ;;
    5) status_label='Full' ;;
    *) status_label="Status ${status_code}" ;;
  esac

  local sources=()
  [[ ${ac} == 'true' ]] && sources+=("AC")
  [[ ${usb} == 'true' ]] && sources+=("USB")
  [[ ${wireless} == 'true' ]] && sources+=("Wireless")

  local source_label=""
  if [[ ${#sources[@]} -gt 0 ]]; then
    source_label=" via ${sources[*]}"
  fi

  if [[ ${level} != '?' ]]; then
    printf '    %-12s %-22s %4s%%  %s%s\n' "${alias}" "${endpoint}" "${level}" "${status_label}" "${source_label}"
  else
    printf '    %-12s %-22s %4s   %s%s\n' "${alias}" "${endpoint}" "?" "${status_label}" "${source_label}"
  fi
}

print_status_table() {
  printf '    %-12s %-22s %-6s %s\n' "Alias" "Endpoint" "Level" "State"
  printf '    %-12s %-22s %-6s %s\n' "------------" "----------------------" "------" "---------------------------"
  for idx in "${!ALIASES[@]}"; do
    status_device "${ALIASES[$idx]}" "${ENDPOINTS[$idx]}" "${idx}"
  done
}

status_loop() {
  local mode="$1"  # watch | append
  while true; do
    if [[ ${mode} == watch ]]; then
      printf '\033[2J\033[H'
    else
      printf '\n'
    fi
    printf '[launcher][STATUS] snapshot %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    print_status_table
    sleep "${STATUS_INTERVAL}" || break
  done
}

if [[ ${ACTION} == "status" ]]; then
  log_info "Starting status-only monitor (interval=${STATUS_INTERVAL}s)"
  status_loop watch
  exit 0
fi

cleanup() {
  trap - INT TERM EXIT
  log_info "Stopping all scrcpy/status subprocesses"
  if [[ ${#ENDPOINTS[@]} -gt 0 ]]; then
    for endpoint in "${ENDPOINTS[@]}"; do
      restart_scrcpy "${endpoint}"
    done
  fi
  if [[ -n ${SCRCPY_PIDS+x} ]]; then
    for pid in "${SCRCPY_PIDS[@]}"; do
      kill "${pid}" >/dev/null 2>&1 || true
    done
  fi
  if [[ -n ${STATUS_PID:-} ]]; then
    kill "${STATUS_PID}" >/dev/null 2>&1 || true
  fi
  if [[ ${QUEST_TWEAKS_ENABLED} == true && ${QUEST_RESTORE_ON_EXIT} == true ]]; then
    for idx in "${!ENDPOINTS[@]}"; do
      restore_quest_tweaks "${ALIASES[$idx]}" "${ENDPOINTS[$idx]}"
    done
  fi
  if [[ -n ${SCRCPY_PIDS+x} ]]; then
    for pid in "${SCRCPY_PIDS[@]}"; do
      wait "${pid}" >/dev/null 2>&1 || true
    done
  fi
  if [[ -n ${STATUS_PID:-} ]]; then
    wait "${STATUS_PID}" >/dev/null 2>&1 || true
  fi
}

if [[ ${DRY_RUN} == false ]]; then
  trap 'cleanup' INT TERM EXIT
fi

for idx in "${!ALIASES[@]}"; do
  start_device "${ALIASES[$idx]}" "${ENDPOINTS[$idx]}" "${COLS[$idx]}" "${ROWS[$idx]}" "${idx}"
done

if [[ ${DRY_RUN} == false && ${STATUS_ENABLED} == true ]]; then
  log_info "Starting background status monitor (interval=${STATUS_INTERVAL}s)"
  status_loop append &
  STATUS_PID=$!
fi

if [[ ${DRY_RUN} == false ]]; then
  wait
fi
