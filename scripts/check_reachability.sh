#!/usr/bin/env bash
# check_reachability.sh — Verify reachability of Quest devices listed in quest_devices.tsv
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: check_reachability.sh [--config FILE] [--ping] [--adb]

Options:
  --config FILE  Path to TSV with alias and endpoint columns (default: scripts/quest_devices.tsv)
  --ping         Test reachability via ICMP ping
  --adb          Test reachability via adb connect/get-state

At least one of --ping or --adb must be specified.
USAGE
}

CONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/quest_devices.tsv"
DO_PING=false
DO_ADB=false

while (($#)); do
  case "$1" in
    --config)
      shift || { echo "[!] Missing value for --config" >&2; exit 1; }
      CONFIG="$1"
      ;;
    --config=*)
      CONFIG="${1#*=}"
      ;;
    --ping) DO_PING=true ;;
    --adb) DO_ADB=true ;;
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

if [[ ${DO_PING} == false && ${DO_ADB} == false ]]; then
  echo "[!] Specify at least one of --ping or --adb." >&2
  usage >&2
  exit 1
fi

if [[ ! -f ${CONFIG} ]]; then
  echo "[!] CONFIG file not found: ${CONFIG}" >&2
  exit 1
fi

ADB_BIN=${ADB_BIN:-$(command -v adb)}
if [[ ${DO_ADB} == true ]]; then
  [[ -n ${ADB_BIN:-} && -x ${ADB_BIN:-} ]] || { echo "[!] adb not found." >&2; exit 1; }
  "${ADB_BIN}" start-server >/dev/null 2>&1
fi

DEVICES=()
while IFS= read -r line || [[ -n ${line} ]]; do
  [[ ${line} =~ ^[[:space:]]*$ ]] && continue
  [[ ${line} =~ ^[[:space:]]*# ]] && continue
  DEVICES+=("${line}")
done < "${CONFIG}"

(( ${#DEVICES[@]} > 0 )) || { echo "[!] No devices found in ${CONFIG}" >&2; exit 1; }

echo "Alias           Endpoint               Ping  ADB"
echo "-------------- ---------------------- ----- -----"

for entry in "${DEVICES[@]}"; do
  IFS=$'\t' read -r alias endpoint _ <<<"${entry}"
  [[ -n ${alias} && -n ${endpoint} ]] || continue

  host=${endpoint%:*}
  ping_status="-"
  adb_status="-"

  if [[ ${DO_PING} == true ]]; then
    if ping -c1 -W1 "${host}" >/dev/null 2>&1; then
      ping_status="ok"
    else
      ping_status="fail"
    fi
  fi

  if [[ ${DO_ADB} == true ]]; then
    "${ADB_BIN}" connect "${endpoint}" >/dev/null 2>&1 || true
    state=$("${ADB_BIN}" -s "${endpoint}" get-state 2>/dev/null | tr -d '\r' || true)
    if [[ ${state} == device ]]; then
      adb_status="ok"
    else
      adb_status="fail"
    fi
  fi

  printf ' %-14s %-22s %-5s %-5s\n' "${alias}" "${endpoint}" "${ping_status}" "${adb_status}"
done
