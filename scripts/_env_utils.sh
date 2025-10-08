#!/usr/bin/env bash
# Shared helpers for resolving binaries within the project virtual environment.
# Intended to be sourced from other scripts (does not modify shell options).

# Determine project root if not already exported by caller.
if [[ -z ${PROJECT_ROOT:-} ]]; then
  if [[ -n ${SCRIPT_DIR:-} ]]; then
    PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
  else
    PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  fi
fi

# Determine the preferred environment prefix.
if [[ -z ${ENV_PREFIX:-} ]]; then
  if [[ -n ${CONDA_PREFIX:-} ]]; then
    ENV_PREFIX="${CONDA_PREFIX}"
  else
    ENV_PREFIX="${PROJECT_ROOT}/.mamba-env"
  fi
fi

# Ensure the environment bin directory is on PATH (without duplicating entries).
if [[ -d "${ENV_PREFIX}/bin" ]]; then
  case ":${PATH}:" in
    *":${ENV_PREFIX}/bin:"*) ;;
    *) export PATH="${ENV_PREFIX}/bin:${PATH}" ;;
  esac
fi

resolve_binary() {
  local name="$1"
  shift || true
  local -a extra_candidates=("$@")
  local candidate
  local -a candidates=()

  if [[ -n ${ENV_PREFIX:-} ]]; then
    candidates+=(
      "${ENV_PREFIX}/bin/${name}"
      "${ENV_PREFIX}/vendor/platform-tools/${name}"
    )
  fi

  candidates+=("${extra_candidates[@]}")

  for candidate in "${candidates[@]}"; do
    if [[ -n ${candidate} && -x ${candidate} ]]; then
      printf '%s' "${candidate}"
      return 0
    fi
  done

  if candidate=$(command -v "${name}" 2>/dev/null); then
    printf '%s' "${candidate}"
    return 0
  fi

  return 1
}

require_binary() {
  local name="$1"
  local __var="$2"
  shift 2 || true
  local path
  if ! path=$(resolve_binary "${name}" "$@"); then
    echo "[!] Required binary not found: ${name}" >&2
    echo "    Expected under ${ENV_PREFIX}/bin or on PATH. Activate the project environment." >&2
    exit 1
  fi
  printf -v "${__var}" '%s' "${path}"
}

