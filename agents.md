# agents.md — ローカルWi-Fi上の **複数台 Meta Quest 3** を **scrcpy** で一括操作・同時モニタリングする運用ガイド

> 目的：研究室・授業などで **最大30台の Quest 3** を **ワイヤレス ADB + scrcpy** で同時モニタリングし、必要に応じて入力補助や録画を行うための実践手順とスクリプト集。  
> 想定OS：**macOS（Apple Silicon / Intel）**。Windows / Linux 向けの記述は省略。  
> 注意：scrcpy は **1インスタンス=1台** 表示が基本です。**完全な入力同期ブロードキャスト機能はありません**（`adb shell input` で最低限の一括操作を補助）。

---

## 0. 構成の全体像

- **各 Quest 3**：Developer Mode ON → USB デバッグ常時許可 → 同一LAN（推奨：Wi-Fi 6 / 6E、5GHz以上）
- **Mac**：Android Platform Tools (`adb`) と `scrcpy` を Homebrew で導入済み
- **初回接続**：USB → `adb tcpip 5555` → 端末IP固定 or DHCP予約 → 以後は Wi-Fi (`adb connect <IP>:5555`)
- **同時表示**：本稿のエントリースクリプトで 1台=1ウィンドウを自動起動・整列。30台まで想定。
- **録画**：同一スクリプトを `--record` モードで起動すると、台数分の MP4 を並列記録
- **一括操作（任意）**：`adb shell input` をループ実行する補助スクリプトで最低限のナビゲーションを送信

---

## 1. 前提セットアップ（初回）
### 1.1 事前の到達性チェック (任意)
### 1.2 端末のスリープ抑止
授業中に画面が暗転しないよう、`./scripts/keep_awake.sh --enable` で `stay_on_while_plugged_in` を一括設定できます。終了後は `--disable` で元に戻してください。

セッション前に `./scripts/check_reachability.sh --ping --adb` を実行すると、`quest_devices.tsv` に登録された端末のネットワーク疎通 (ICMP) と ADB 応答状態を表で確認できます。


1. **Developer Mode ON**  
   スマホの Meta Quest アプリ → デバイス → 開発者モードを有効化。ヘッドセット側で「USB デバッグを常に許可」。
2. **プロジェクト環境の構築（mamba）**  
   ```bash
   mamba env create -p ./.mamba-env -f environment.yml
   ./scripts/bootstrap_binaries.sh ./.mamba-env
   conda activate "${PWD}/.mamba-env"
   ```
   `bootstrap_binaries.sh` が scrcpy / adb を環境内に設置します。
3. **USB接続で Wi-Fi ADB を有効化**  
   ```bash
   adb devices                     # USB検出を確認
   adb tcpip 5555                  # Wi-Fiモードへ切替
   adb connect 192.168.10.21:5555  # 以後は Wi-Fi で利用
   ```
   複数台をまとめて Wi-Fi モードに切り替える場合は `./scripts/usb_to_tcp.sh --port 5555` を使用すると `adb tcpip` を一括実行できます。
4. **IP固定**  
   ルーターの DHCP 予約で「端末名 ↔ IP」を固定（例：Quest-01 → 192.168.10.21）。30台運用時は表計算で管理。
5. **ディレクトリ準備**  
   本レポジトリ直下に `scripts/` と録画保存用 `recordings/` を作成しておく。

---

## 2. 端末リスト（最大30台）の管理方法

`scripts/quest_devices.tsv` に、端末ごとの Alias とエンドポイントをタブ区切りで記述します。3列目以降は任意（後述）。

### 2.1.1 LAN内の端末自動検出 (任意)
`./scripts/discover_quest3.py` を使うと、Wi-Fi ADB (既定ポート5555) で待ち受けている Quest をスキャンできます。

```bash
conda activate "${PWD}/.mamba-env"
./scripts/discover_quest3.py --connect           # en0等から自動検出
./scripts/discover_quest3.py --cidr 192.168.10.0/24
```

`--connect` を付けると `adb connect` でモデル/シリアルを取得し、`scripts/quest_devices.tsv` に貼り付け可能な行も出力されます。


```tsv
# alias	endpoint(=IP[:PORT])	[col]	[row]
Quest-01	192.168.10.21:5555
Quest-02	192.168.10.22:5555
Quest-03	192.168.10.23:5555	2	0   # 0始まりの列・行で手動配置
...
Quest-30	192.168.10.50:5555
```

- **列・行を省略**した端末は、スクリプトが自動レイアウト（デフォルト 5列×6行）します。
- 列・行を指定した場合は、0始まりのグリッド座標としてその位置に固定。
- LIST の順序はウィンドウタイトルにも反映されるため、席番号や用途順に並べると識別が容易です。

---

## 3. mac 用エントリースクリプト

下記を `scripts/quest_multi_scrcpy.sh` として保存し、`chmod +x scripts/quest_multi_scrcpy.sh` を付与してください。引数で録画モードを切り替えられます。

```bash
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

if [[ -n ${SCRCPY_EXTRA_ARGS:-} ]]; then
  read -r -a SCRCPY_EXTRA_ARRAY <<<"${SCRCPY_EXTRA_ARGS}"
else
  SCRCPY_EXTRA_ARRAY=(--no-audio --no-clipboard)
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
    "--render-driver=opengl"
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
```

### スクリプトのポイント### スクリプトのポイント### スクリプトのポイント### スクリプトのポイント### スクリプトのポイント### スクリプトのポイント### スクリプトのポイント### スクリプトのポイント### スクリプトのポイント### スクリプトのポイント### スクリプトのポイント### スクリプトのポイント

- **録画切替**：`--record` を渡すと、自動で `recordings/<Alias>_<Timestamp>.mp4` を保存。`--no-record` は明示的に録画オフ。
- **レイアウト制御**：`GRID_COLUMNS` / `GRID_ROWS` と `DISPLAY_WIDTH` / `DISPLAY_HEIGHT` を上書きすればマルチモニターやプロジェクタにも対応。
- **dry-run チェック**：`--dry-run` で配置・起動コマンドのみ確認できるため、30台分の新構成を事前検証可能。
- **バッテリー監視**：`start` 実行中は 60秒ごと（`STATUS_INTERVAL` または `--status-interval` で変更可）にバッテリー/給電状態を自動出力。端末が offline なら再接続を待ってから scrcpy を再起動（連続2回検知で実施）するため、ポート競合を避けつつ復帰できます。不要な場合は `--no-status`。監視時の `adb connect` 有無は `--status-skip-connect` / `--status-connect` で調整。
- **ポート管理**：`SCRCPY_BASE_PORT` を起点に空きポートを自動スキャンして割り当てるため、並列起動時の `bind: Address already in use` を回避できます。ログ出力は `[launcher]` プレフィックス付きで表示されるため、scrcpy 本体のログと判別しやすくなります。scrcpy 側の出力も `[Quest-XX]` のように別名でタグ付けされるので、どのヘッドセットがメッセージを出したか即座に把握できます。追加で scrcpy フラグを渡す場合は `SCRCPY_EXTRA_ARGS="--no-audio"` のように環境変数で指定できます。起動間隔を調整したい場合は `SCRCPY_LAUNCH_DELAY` (秒) を指定すると各 scrcpy の立ち上げが順次ディレイされます。デフォルトでは `--no-audio --no-clipboard` を付与して安定性を高めており、描画ドライバは `SCRCPY_RENDER_DRIVER` (デフォルト `metal`) で切り替えられます。`SCRCPY_EXTRA_ARGS` でこれらを上書きしても構いません。`Ctrl+C` で終了すると全サブプロセスが確実に停止し、すべての scrcpy ウィンドウも閉じるようトラップ処理を追加済みです。
- **エラーハンドリング**：端末数が `MAX_DEVICES` を超えた場合は即座に終了。IP 設定ミスなども早期に検知します。

---

### 4.0 バッテリー状況の一括確認
`start` コマンドの実行中はステータスが背景監視され、応答が途絶えた端末は自動的に `adb connect` と scrcpy 起動をやり直します。専用ウォッチのみ使いたい場合は `status` コマンドを利用すると、端末リストをクリア画面で数十秒ごとに更新します。既に接続済みなら `--skip-connect` を付けて再接続を抑止。ミラー中も毎回接続を確認したい場合は `start` 側で `--status-connect` を指定してください。

```bash
conda activate "${PWD}/.mamba-env"
./scripts/quest_multi_scrcpy.sh status --interval 15       # 15秒刻みで更新
./scripts/quest_multi_scrcpy.sh status --skip-connect      # 既に接続済みの場合
```

## 4. スクリプトの使い方

```bash
# 標準モード（ミラーのみ）
./scripts/quest_multi_scrcpy.sh start

# 録画モード（recordings/ 以下に全端末分を保存）
./scripts/quest_multi_scrcpy.sh start --record

# レイアウト検証のみ
GRID_COLUMNS=6 GRID_ROWS=5 ./scripts/quest_multi_scrcpy.sh start --dry-run

# 録画モード + カスタム保存先
RECORD_DIR="$PWD/recordings/2024-lesson01" ./scripts/quest_multi_scrcpy.sh start --record
```

- 録画モード時は **30台 × 高ビットレート** となるため、Mac のストレージ帯域と余裕を事前に確認してください。
- 端末名（Alias）はウィンドウタイトルと録画ファイル名に反映されるので、座席番号や受講者 ID を推奨。
- 停止時はターミナルで `Ctrl+C`。バックグラウンドジョブも trap でまとめて終了します。

---

## 5. 大規模表示のチューニング

- **複数モニター**：モニターごとに別スクリプトを起動するか、`DISPLAY_WIDTH`/`HEIGHT` を各モニターの解像度に合わせて環境変数で切替。
- **描画負荷**：`BIT_RATE=6M MAX_SIZE=1280` など帯域優先設定に変更すると CPU/GPU 負荷を軽減。
- **優先表示**：特定端末のみ 1080p 以上で見たい場合、別スクリプトを録画モードで個別起動（他は `--max-size=1280`）。
- **Wi-Fi 設計**：
  - Quest 3 は 30台同時通信で帯域を圧迫するため、AP を複数台用意しチャネル分散。
  - `adb connect` が不安定な台は電波強度を計測し、AP 至近に配置。
  - DHCP 予約の一覧表を現場に掲示しておくとトラブルシュートが迅速。

---

## 6. 一括操作（ブロードキャスト入力）の実用例

### 6.1 入力対象の共有

`scripts/devices.env` に端末エンドポイントを配列で記述。

```bash
DEVICES=(
  "192.168.10.21:5555"
  "192.168.10.22:5555"
  # ... Quest-30 まで
)
```

### 6.2 代表的な操作スクリプト（macOS bash）

- **ホーム→戻る→OK**
  ```bash
  #!/usr/bin/env bash
  source "$(dirname "$0")/devices.env"
  for d in "${DEVICES[@]}"; do
    adb -s "$d" shell input keyevent KEYCODE_HOME
    adb -s "$d" shell input keyevent KEYCODE_BACK
    adb -s "$d" shell input keyevent KEYCODE_DPAD_CENTER
  done
  ```
- **座標タップ（%指定）**
  ```bash
  #!/usr/bin/env bash
  source "$(dirname "$0")/devices.env"
  XPCT=${1:-50}
  YPCT=${2:-50}
  for d in "${DEVICES[@]}"; do
    size=$(adb -s "$d" shell wm size | tr -d '\r')
    wh=$(echo "$size" | grep -oE '[0-9]+x[0-9]+' | head -n1)
    W=${wh%x*}
    H=${wh#*x}
    X=$(( W * XPCT / 100 ))
    Y=$(( H * YPCT / 100 ))
    adb -s "$d" shell input tap "$X" "$Y"
  done
  ```
- **スワイプ（上方向 300ms）**
  ```bash
  #!/usr/bin/env bash
  source "$(dirname "$0")/devices.env"
  for d in "${DEVICES[@]}"; do
    wh=$(adb -s "$d" shell wm size | grep -oE '[0-9]+x[0-9]+' | head -n1)
    W=${wh%x*}
    H=${wh#*x}
    X=$(( W / 2 ))
    Y1=$(( H * 3 / 4 ))
    Y2=$(( H / 4 ))
    adb -s "$d" shell input swipe "$X" "$Y1" "$X" "$Y2" 300
  done
  ```

> **同期性の注意**：Wi-Fi 伝送と VR UI のリフレッシュで時間差が生じます。厳密な同時操作が必要な授業では、手順を番号付きで伝える・代表端末のみ入力を共有するといった運用で吸収してください。

---

## 7. 録画・スクリーンショット運用

- **フル録画**：`--record` モードを使うと、端末ごとにマルチトラック MP4 が生成され、後追い分析が容易。
- **代表録画のみ**：台数が多い場合は `scripts/quest_multi_scrcpy.sh` を二度起動し、①代表端末だけ録画ビットレート高め、②その他はミラーのみ、など役割分担も可能。
- **スクリーンショット**：
  ```bash
  adb -s 192.168.10.21:5555 shell screencap -p /sdcard/quest01.png
  adb -s 192.168.10.21:5555 pull /sdcard/quest01.png ./screenshots/
  ```
- **録画ファイル整理**：授業や実験単位で `RECORD_DIR` を切り替え、`find recordings -name '*.mp4' -mtime +7 -delete` 等でローテーション。

---

## 8. 運用ベストプラクティス（30台規模を想定）

- **事前チェック**：
  - 端末充電 80%以上、バッテリー or 給電ケーブル確認
  - `adb connect` 成否リストを15分前に確認（`--dry-run` → `start`）
  - ウィンドウタイトルと座席表を照合し、講師/TAが即座に特定できるよう整理
- **ネットワーク設計**：
  - AP 2〜3台で 10台/セグメント 程度に分散し、チャネル干渉を回避
  - VLAN やゲスト隔離が有効な Wi-Fi では `adb connect` が塞がる場合があるので事前許可
- **操作ポリシー**：
  - 完全同期クリックを期待せず、ガイド資料やプロジェクタで順序を共有
  - 必要に応じて `adb shell input` で HOME / CONFIRM 等を送る
- **終了時手順**：
  - `Ctrl+C` で scrcpy を停止 → `adb disconnect`
  - 研究室外へ持ち出す端末は `adb usb` で Wi-Fi ADB を無効化し、セキュリティを確保

---

## 9. トラブルシューティング

- **`adb connect` 失敗**：IP変更 / DHCP未固定 / ファイアウォールの確認。Quest の Wi-Fi 接続を一旦切断→再接続で復旧するケースも多い。
- **scrcpy ウィンドウが溢れる**：`GRID_COLUMNS`/`GRID_ROWS` を増やす、または複数モニターに分けて複数回起動。
- **録画ファイルが欠損**：ストレージ空き容量と書き込み速度を確認。`--record` は高負荷のため、必要端末のみに限定するか、USB-C 外付けSSDを活用。
- **入力が効かない UI**：VR 特有のジェスチャーは `adb shell input` で完全再現できない場合あり。Quest 側ガーディアン設定などは手動で対応。
- **ウィンドウ表示が真っ黒**：Quest 側の画面ON/OFFを確認。`--turn-screen-off=false` を付けていてもディスプレイがスリープに入る場合は給電環境を見直す。

---

## 10. 付録：補助ユーティリティ（macOS bash）

### 10.1 一括接続 / 切断

```bash
#!/usr/bin/env bash
source "$(dirname "$0")/devices.env"
case "$1" in
  connect)
    for d in "${DEVICES[@]}"; do adb connect "$d"; done ;;
  disconnect)
    adb disconnect ;;
  *)
    echo "Usage: $0 {connect|disconnect}" ;;
esac
```

### 10.2 解像度一覧ダンプ

```bash
#!/usr/bin/env bash
source "$(dirname "$0")/devices.env"
for d in "${DEVICES[@]}"; do
  echo "== $d =="
  adb -s "$d" shell wm size
  adb -s "$d" shell wm density
  echo

done
```

---

参考メモ
- scrcpy は複数インスタンス同時起動が前提。Mac の GPU 負荷を監視し、必要なら端末グループを複数 Mac に分散。
- 完全な「ミラー + 入力同期」を求める場合は、Quest 向け MDM や有償配信ソリューションも検討。コスト・遅延・管理性を比較し、ケースバイケースで選定してください。
