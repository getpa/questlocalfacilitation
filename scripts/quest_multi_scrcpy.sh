#!/usr/bin/env bash
# quest_multi_scrcpy.sh — 最大30台の Quest 3 を scrcpy で同時起動/監視するエントリーポイント
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: quest_multi_scrcpy.sh <command> [options]

Commands:
  start    複数 scrcpy インスタンスを起動（デフォルトでバッテリー状態も定期出力）
  status   端末バッテリー残量と給電状態を継続監視

start options:
  --record              各ウィンドウで scrcpy 録画を有効化 (デフォルト: 無効)
  --no-record           録画を明示的に無効化
  --dry-run             実行内容を表示するだけ（接続/起動はしない）
  --no-status           バッテリー監視を無効化
  --status-interval SEC  監視更新間隔（秒）
  --status-skip-connect  監視時に adb connect を行わない（デフォルト）
  --status-connect       監視時にも毎回 adb connect を実施

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

ensure_positive_integer() {
  local value="$1"
  if [[ ! ${value} =~ ^[0-9]+$ ]] || (( value <= 0 )); then
    log_error "Invalid interval: ${value}"
    exit 1
  fi
}

ACTION=${1:-}
if [[ -n ${ACTION} ]]; then
  shift || true
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONFIG_DEFAULT="${SCRIPT_DIR}/quest_devices.tsv"
CONFIG=${CONFIG:-"${CONFIG_DEFAULT}"}

STATUS_INTERVAL=${STATUS_INTERVAL:-60}
STATUS_ENABLED=true
STATUS_SKIP_CONNECT=false
RECORD_MODE="off"
DRY_RUN=false
BASE_PORT=${SCRCPY_BASE_PORT:-27183}
RENDER_DRIVER=${SCRCPY_RENDER_DRIVER:-metal}

if [[ -n ${SCRCPY_EXTRA_ARGS:-} ]]; then
  read -r -a SCRCPY_EXTRA_ARRAY <<<"${SCRCPY_EXTRA_ARGS}"
else
  SCRCPY_EXTRA_ARRAY=(--no-audio --no-clipboard )
fi

is_port_in_use() {
  local port="$1"
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
    pkill -f "scrcpy --serial=${endpoint}" >/dev/null 2>&1 || true
  fi
}

case "${ACTION:-}" in
  start)
    STATUS_SKIP_CONNECT=true
    while (($#)); do
      case "$1" in
        --record) RECORD_MODE="on" ;;
        --no-record) RECORD_MODE="off" ;;
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

ensure_positive_integer "${STATUS_INTERVAL}"

ADB_BIN=${ADB_BIN:-$(command -v adb)}
[[ -n ${ADB_BIN:-} && -x ${ADB_BIN:-} ]] || { log_error "adb not found."; exit 1; }

if [[ ${ACTION} == "start" ]]; then
  SCRCPY_BIN=${SCRCPY_BIN:-$(command -v scrcpy)}
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
    mkdir -p "${RECORD_DIR}"
  fi
fi

start_device() {
  local alias="$1" endpoint="$2" col="$3" row="$4" index="$5"

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
  local record_flag=()
  if [[ ${RECORD_MODE} == "on" ]]; then
    local sanitized_alias
    sanitized_alias=$(echo "${alias}" | tr -cs '[:alnum:]_-' '_')
    local record_path="${RECORD_DIR}/${sanitized_alias}_${TIMESTAMP}.mp4"
    record_flag=("--record=${record_path}")
  fi

  local requested_port=$(( BASE_PORT + index ))
  local port
  if ! port=$(find_free_port "${requested_port}"); then
    log_error "${alias}: unable to find free port starting at ${requested_port}"
    return 1
  fi

  local cmd=(
    "${SCRCPY_BIN}"
    "--serial=${endpoint}"
    "--window-title=${title}"
    "--window-x=${x}" "--window-y=${y}"
    "--window-width=${WINDOW_WIDTH}" "--window-height=${WINDOW_HEIGHT}"
    "--video-bit-rate=${BIT_RATE}"
    "--max-size=${MAX_SIZE}"
    "--stay-awake"
    "--render-driver=${RENDER_DRIVER}"
    "--port=${port}"
  )
  if (( ${#SCRCPY_EXTRA_ARRAY[@]} )); then
    cmd+=("${SCRCPY_EXTRA_ARRAY[@]}")
  fi
  if [[ ${#record_flag[@]} -gt 0 ]]; then
    cmd+=("${record_flag[@]}")
  fi

  if ${DRY_RUN}; then
    log_info "[dry-run] scrcpy command for ${alias} (${endpoint}) at x=${x}, y=${y}, port=${port}"
    printf '    %q\n' "${cmd[@]}"
    return 0
  fi

  log_info "Launching ${alias} (${endpoint}) at x=${x}, y=${y}, port=${port}"

  if ! wait_for_device "${endpoint}"; then
    log_warn "${alias}: device offline, skipping scrcpy launch"
    return 1
  fi

  restart_scrcpy "${endpoint}"
  (
    local attempt=0
    export SDL_RENDER_DRIVER="${RENDER_DRIVER}"
    while true; do
      attempt=$((attempt + 1))
      "${cmd[@]}" 2>&1 | while IFS= read -r line; do
        printf '[%s] %s\n' "${alias}" "${line}"
      done
      status=${PIPESTATUS[0]:-0}
      if (( status == 0 )); then
        log_info "scrcpy exited normally for ${alias}"
        break
      fi
      log_warn "${alias}: scrcpy exited with code ${status} (attempt ${attempt}), retrying in 3s"
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
  for pid in "${SCRCPY_PIDS[@]}"; do
    kill "${pid}" >/dev/null 2>&1 || true
  done
  if [[ -n ${STATUS_PID:-} ]]; then
    kill "${STATUS_PID}" >/dev/null 2>&1 || true
  fi
  for pid in "${SCRCPY_PIDS[@]}"; do
    wait "${pid}" >/dev/null 2>&1 || true
  done
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
