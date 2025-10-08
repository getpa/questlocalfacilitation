#!/usr/bin/env bash
# usb_to_tcp.sh — Switch all USB-connected Quest headsets into adb TCP/IP mode.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
source "${SCRIPT_DIR}/_env_utils.sh"

usage() {
  cat <<'USAGE'
Usage: usb_to_tcp.sh [--port PORT]

Options:
  --port PORT  TCP port to enable on each device (default: 5555)

Environment overrides:
  TCP_PORT     Same as --port.
USAGE
}

PORT=${TCP_PORT:-5555}

while (($#)); do
  case "$1" in
    --port)
      shift || { echo "[!] Missing value for --port" >&2; exit 1; }
      PORT="$1"
      ;;
    --port=*)
      PORT="${1#*=}"
      ;;
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

if [[ ! ${PORT} =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
  echo "[!] Invalid TCP port: ${PORT}" >&2
  exit 1
fi

if [[ -z ${ADB_BIN:-} ]]; then
  require_binary adb ADB_BIN
fi
[[ -n ${ADB_BIN:-} && -x ${ADB_BIN:-} ]] || { echo "[!] adb not found." >&2; exit 1; }

# Ensure the adb server is running.
"${ADB_BIN}" start-server >/dev/null 2>&1

detect_ip() {
  local serial="$1" attempts=0 ip_output ip
  while (( attempts < 6 )); do
    ip_output=$("${ADB_BIN}" -s "${serial}" shell 'ip -o -4 addr show scope global' 2>/dev/null | tr -d '\r') || true
    ip=$(echo "${ip_output}" | awk '{print $4}' | cut -d/ -f1 | grep -v '^127\.' | head -n1)
    if [[ -n ${ip} ]]; then
      echo "${ip}"
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 0.5
  done
  for prop in dhcp.wlan0.ipaddress dhcp.wlan1.ipaddress dhcp.eth0.ipaddress; do
    ip=$("${ADB_BIN}" -s "${serial}" shell getprop "${prop}" 2>/dev/null | tr -d '\r') || true
    if [[ ${ip} =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      echo "${ip}"
      return 0
    fi
  done
  echo ""
}

declare -a USB_DEVICES=()
while IFS=$'\t' read -r serial status _; do
  [[ -z ${serial} || ${serial} == "List of devices attached" ]] && continue
  [[ ${status} != device ]] && continue
  [[ ${serial} == *:* ]] && continue  # already in TCP mode
  USB_DEVICES+=("${serial}")
done < <("${ADB_BIN}" devices)

if (( ${#USB_DEVICES[@]} == 0 )); then
  echo "[i] No USB-connected devices detected."
  exit 0
fi

declare -a IP_ADDRS=()

echo "[i] Switching ${#USB_DEVICES[@]} device(s) to TCP mode on port ${PORT}..."
for serial in "${USB_DEVICES[@]}"; do
  echo "  -> ${serial}"
  "${ADB_BIN}" -s "${serial}" tcpip "${PORT}" >/dev/null
  ip_addr=$(detect_ip "${serial}")
  if [[ -n ${ip_addr} ]]; then
    echo "     IP address: ${ip_addr}"
  else
    echo "     IP address: (not detected)"
  fi
  IP_ADDRS+=("${ip_addr}")
done

echo "[+] Done. Disconnect USB and connect over Wi-Fi with:"
for idx in "${!USB_DEVICES[@]}"; do
  serial="${USB_DEVICES[$idx]}"
  ip="${IP_ADDRS[$idx]}"
  if [[ -n ${ip} ]]; then
    echo "    adb connect ${ip}:${PORT}  # ${serial}"
  else
    echo "    adb connect <device-ip>:${PORT}  # ${serial}"
  fi
done
