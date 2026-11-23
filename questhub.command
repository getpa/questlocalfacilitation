#!/bin/bash
# questhub.command — Start the Quest Hub GUI with the project environment
# (no `conda activate` needed; double-clickable in Finder). Set up with ./install.sh.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd -P)
ENV_DIR=${QH_ENV_DIR:-"${ROOT}/.mamba-env"}
PY="${ENV_DIR}/bin/python"

if [[ ! -x ${PY} ]]; then
  echo "[questhub] Environment not found. Run ./install.sh first." >&2
  exit 1
fi
if [[ ! -f ${ROOT}/questhub/__main__.py ]]; then
  echo "[questhub] The GUI package is not implemented yet (see docs/GUI_DESIGN.md)." >&2
  exit 1
fi

export PATH="${ENV_DIR}/bin:${PATH}"
cd "${ROOT}"
exec "${PY}" -m questhub "$@"
