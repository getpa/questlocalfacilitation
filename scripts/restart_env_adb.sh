#!/usr/bin/env bash
# restart_env_adb.sh — Always restart the adb daemon using the repo-local .mamba-env binary.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
ADB_BIN="${PROJECT_ROOT}/.mamba-env/bin/adb"

usage() {
  cat <<'USAGE'
Usage: restart_env_adb.sh

Restart adb daemon with the project-local binary:
  1) ./.mamba-env/bin/adb kill-server
  2) ./.mamba-env/bin/adb start-server
USAGE
}

if (($#)); then
  case "${1}" in
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "[!] Unknown option: ${1}" >&2
      usage >&2
      exit 1
      ;;
  esac
fi

if [[ ! -x "${ADB_BIN}" ]]; then
  echo "[!] adb not found at ${ADB_BIN}" >&2
  echo "    Build the local environment first: ./scripts/bootstrap_binaries.sh ./.mamba-env" >&2
  exit 1
fi

echo "[i] Restarting adb daemon with ${ADB_BIN}"
"${ADB_BIN}" kill-server >/dev/null 2>&1 || true
"${ADB_BIN}" start-server >/dev/null 2>&1
echo "[+] adb daemon restarted via .mamba-env/bin/adb"
