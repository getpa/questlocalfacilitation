#!/bin/bash
# install.sh — Set up the Quest toolkit on a bare macOS (no conda, Homebrew, or
# Xcode Command Line Tools required). Everything is installed inside this
# repository; no sudo, and no shell rc files are modified.
#
# Must stay compatible with macOS's /bin/bash 3.2: no associative arrays,
# no ${var,,}, no mapfile, and no `local -n`.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd -P)

MICROMAMBA_VERSION=${QH_MICROMAMBA_VERSION:-"2.10.0-0"}
MICROMAMBA_SHA256_OSX_ARM64="c547639f5caf742236641c362746abb4a3505774753ecd08fe341643670c4c01"
MICROMAMBA_SHA256_OSX_64="8421462a1ef1df9f7acd114f9e2a24c1332ce4f4e29f44465b4acb44e2755172"
MICROMAMBA_BASE_URL=${QH_MICROMAMBA_BASE_URL:-"https://github.com/mamba-org/micromamba-releases/releases/download"}

# Pinned platform-tools for --strict (latest is used otherwise).
STRICT_PLATFORM_TOOLS_URL="https://dl.google.com/android/repository/platform-tools_r37.0.3-darwin.zip"
STRICT_PLATFORM_TOOLS_SHA256="df21339b9371fef9b19264404338dc686f59a88a1dce634b963da344909001d6"

TOOLS_DIR=${QH_TOOLS_DIR:-"${ROOT}/.tools"}
ENV_DIR=${QH_ENV_DIR:-"${ROOT}/.mamba-env"}
DATA_DIR=${QH_DATA_DIR:-"${HOME}/.questhub"}
LOCK_DIR="${ROOT}/locks"
SPEC_STAMP="${ENV_DIR}/.questhub-spec.sha256"
EXPECTED_SCRCPY_VERSION=${SCRCPY_VERSION:-"5.0.1"}
MIN_FREE_KB=$((2 * 1024 * 1024))
GUI_PORTS="8765 8443 8766"

MODE="install"
STRICT=false
BUILD_FROM_SOURCE=false
ASSUME_YES=false
PURGE_DATA=false
RELOCK_PLATFORM=""

if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_FAIL=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_FAIL=""; C_DIM=""; C_OFF=""
fi

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install][WARN] %s\n' "$*" >&2; }
die()  { printf '[install][ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: ./install.sh [options]

Sets up ./.mamba-env (Python, adb, scrcpy) with a project-local micromamba.
Re-running is safe: unchanged parts are skipped.

Options:
  --doctor              Only run the environment checks
  --strict              Require the lock file and the pinned platform-tools
  --build-from-source   Also install environment-build.yml and build scrcpy from git
                        (needs Xcode Command Line Tools)
  --relock [PLATFORM]   Re-solve environment.yml and rewrite locks/conda-PLATFORM.txt
                        (PLATFORM: osx-arm64 | osx-64 | win-64; default: this Mac)
  --uninstall           Remove .tools/ and .mamba-env/
  --purge-data          With --uninstall, also remove ~/.questhub (registry, certificates)
  -y, --yes             Do not ask for confirmation
  -h, --help            Show this help
USAGE
}

while (($#)); do
  case "$1" in
    --doctor) MODE="doctor" ;;
    --strict) STRICT=true ;;
    --build-from-source) BUILD_FROM_SOURCE=true ;;
    --relock)
      MODE="relock"
      if [[ ${2:-} == osx-* || ${2:-} == win-* ]]; then
        RELOCK_PLATFORM="$2"
        shift
      fi
      ;;
    --uninstall) MODE="uninstall" ;;
    --purge-data) PURGE_DATA=true ;;
    -y|--yes) ASSUME_YES=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "Unknown option: $1" ;;
  esac
  shift
done

# ---------------------------------------------------------------- helpers

sha256_of_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

download() {
  local url="$1" dest="$2"
  log "Downloading ${url}"
  # curl honours HTTPS_PROXY / https_proxy on its own.
  curl --fail --location --retry 3 --connect-timeout 15 --silent --show-error "${url}" -o "${dest}" \
    || die "Download failed: ${url} (check the network or HTTPS_PROXY)"
}

confirm() {
  [[ ${ASSUME_YES} == true ]] && return 0
  [[ -t 0 ]] || die "$1 Re-run with --yes to confirm non-interactively."
  local answer
  printf '%s [y/N] ' "$1"
  read -r answer
  [[ ${answer} == y || ${answer} == Y || ${answer} == yes ]]
}

# Refuse to rm -rf anything that is not clearly ours.
safe_remove() {
  local target="$1"
  [[ -n ${target} ]] || die "Refusing to remove an empty path"
  case "${target}" in
    /|"${HOME}"|"${HOME}/"|"${ROOT}"|"${ROOT}/") die "Refusing to remove ${target}" ;;
  esac
  if [[ -e ${target} || -L ${target} ]]; then
    rm -rf "${target}"
    log "Removed ${target}"
  fi
}

detect_platform() {
  if [[ -n ${QH_PLATFORM:-} ]]; then
    PLATFORM="${QH_PLATFORM}"
    return
  fi
  [[ $(uname -s) == Darwin ]] || die "This installer supports macOS only."
  local machine
  machine=$(uname -m)
  # A Terminal running under Rosetta reports x86_64 on Apple Silicon.
  if [[ ${machine} == x86_64 && $(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0) == 1 ]]; then
    log "Rosetta detected; installing native Apple Silicon packages"
    machine=arm64
  fi
  case "${machine}" in
    arm64) PLATFORM="osx-arm64" ;;
    x86_64) PLATFORM="osx-64" ;;
    *) die "Unsupported CPU architecture: ${machine}" ;;
  esac
}

scrcpy_release_arch() {
  case "$1" in
    osx-arm64) echo "macos-aarch64" ;;
    *) echo "macos-x86_64" ;;
  esac
}

check_prerequisites() {
  if [[ -z ${QH_PLATFORM:-} ]]; then
    local version major
    version=$(sw_vers -productVersion 2>/dev/null || echo 0)
    major=${version%%.*}
    if [[ ${major} =~ ^[0-9]+$ ]] && (( major < 13 )); then
      die "macOS 13 (Ventura) or later is required (found ${version})."
    fi
  fi
  local free_kb
  free_kb=$(df -Pk "${ROOT}" | awk 'NR==2 {print $4}')
  if [[ ${free_kb} =~ ^[0-9]+$ ]] && (( free_kb < MIN_FREE_KB )); then
    warn "Less than 2 GB free on this volume; the installation may fail."
  fi
}

# ---------------------------------------------------------------- micromamba

MM=""

ensure_micromamba() {
  if [[ -n ${QH_MICROMAMBA_BIN:-} ]]; then
    MM="${QH_MICROMAMBA_BIN}"
    [[ -x ${MM} ]] || die "QH_MICROMAMBA_BIN is not executable: ${MM}"
    return
  fi
  MM="${TOOLS_DIR}/bin/micromamba"
  local stamp="${TOOLS_DIR}/bin/micromamba.version"
  if [[ -x ${MM} && -f ${stamp} && $(cat "${stamp}") == "${MICROMAMBA_VERSION}" ]]; then
    log "micromamba ${MICROMAMBA_VERSION} already installed"
    return
  fi
  local expected=""
  if [[ ${MICROMAMBA_VERSION} == 2.10.0-0 ]]; then
    case "${PLATFORM}" in
      osx-arm64) expected="${MICROMAMBA_SHA256_OSX_ARM64}" ;;
      osx-64) expected="${MICROMAMBA_SHA256_OSX_64}" ;;
    esac
  fi
  local tmp
  tmp=$(mktemp -d)
  local url="${MICROMAMBA_BASE_URL}/${MICROMAMBA_VERSION}/micromamba-${PLATFORM}"
  download "${url}" "${tmp}/micromamba"
  if [[ -z ${expected} ]]; then
    # Non-default version: fall back to the checksum published with the release.
    download "${url}.sha256" "${tmp}/micromamba.sha256"
    expected=$(awk '{print $1}' "${tmp}/micromamba.sha256")
  fi
  if [[ $(sha256_of_file "${tmp}/micromamba") != "${expected}" ]]; then
    rm -rf "${tmp}"
    die "micromamba checksum mismatch"
  fi
  mkdir -p "${TOOLS_DIR}/bin"
  chmod 755 "${tmp}/micromamba"
  mv "${tmp}/micromamba" "${MM}"
  printf '%s\n' "${MICROMAMBA_VERSION}" > "${stamp}"
  rm -rf "${tmp}"
  log "Installed micromamba ${MICROMAMBA_VERSION} (${PLATFORM})"
}

# Run micromamba isolated from any user configuration.
mm() {
  MAMBA_ROOT_PREFIX="${TOOLS_DIR}/mamba-root" CONDARC="" MAMBARC="" \
    "${MM}" --no-rc "$@"
}

# ---------------------------------------------------------------- environment

lock_file_for() {
  printf '%s/conda-%s.txt' "${LOCK_DIR}" "$1"
}

spec_digest() {
  local lock
  lock=$(lock_file_for "${PLATFORM}")
  {
    printf 'micromamba=%s\n' "${MICROMAMBA_VERSION}"
    if [[ -f ${lock} ]]; then
      cat "${lock}"
    else
      cat "${ROOT}/environment.yml"
    fi
    if [[ ${BUILD_FROM_SOURCE} == true ]]; then
      cat "${ROOT}/environment-build.yml"
    fi
  } | shasum -a 256 | awk '{print $1}'
}

ensure_environment() {
  local lock digest
  lock=$(lock_file_for "${PLATFORM}")
  if [[ ${STRICT} == true && ! -f ${lock} ]]; then
    die "--strict requires ${lock} (generate it with ./install.sh --relock ${PLATFORM})"
  fi
  digest=$(spec_digest)
  if [[ -x ${ENV_DIR}/bin/python && -f ${SPEC_STAMP} && $(cat "${SPEC_STAMP}") == "${digest}" ]]; then
    log "Environment is up to date (${ENV_DIR})"
    return
  fi

  # Rebuild from scratch so removed packages do not linger, but keep the
  # downloaded adb/scrcpy binaries in vendor/ to avoid fetching them again.
  local saved_vendor=""
  if [[ -d ${ENV_DIR}/vendor ]]; then
    saved_vendor=$(mktemp -d)
    mv "${ENV_DIR}/vendor" "${saved_vendor}/vendor"
  fi
  safe_remove "${ENV_DIR}"

  if [[ -f ${lock} ]]; then
    log "Creating environment from $(basename "${lock}")"
    mm create --yes --prefix "${ENV_DIR}" --file "${lock}"
  else
    warn "No lock file for ${PLATFORM}; solving environment.yml (versions may drift)"
    mm create --yes --prefix "${ENV_DIR}" --file "${ROOT}/environment.yml"
  fi
  if [[ ${BUILD_FROM_SOURCE} == true ]]; then
    log "Adding source-build packages (environment-build.yml)"
    mm install --yes --prefix "${ENV_DIR}" --file "${ROOT}/environment-build.yml"
  fi

  if [[ -n ${saved_vendor} ]]; then
    mv "${saved_vendor}/vendor" "${ENV_DIR}/vendor"
    rm -rf "${saved_vendor}"
  fi
  printf '%s\n' "${digest}" > "${SPEC_STAMP}"
}

install_binaries() {
  if [[ ${QH_SKIP_BINARIES:-0} == 1 ]]; then
    log "Skipping adb/scrcpy (QH_SKIP_BINARIES=1)"
    return
  fi
  if [[ ${BUILD_FROM_SOURCE} == true ]] && ! xcode-select -p >/dev/null 2>&1; then
    die "--build-from-source needs Xcode Command Line Tools: run 'xcode-select --install' first."
  fi
  local -a env_args=(
    "PATH=${ENV_DIR}/bin:${PATH}"
    "SCRCPY_RELEASE_ARCH=$(scrcpy_release_arch "${PLATFORM}")"
  )
  if [[ ${STRICT} == true ]]; then
    env_args+=("PLATFORM_TOOLS_URL=${STRICT_PLATFORM_TOOLS_URL}" "PLATFORM_TOOLS_SHA256=${STRICT_PLATFORM_TOOLS_SHA256}")
  fi
  if [[ ${BUILD_FROM_SOURCE} == true ]]; then
    env_args+=("SCRCPY_BUILD_FROM_GIT=1")
  fi
  env "${env_args[@]}" /bin/bash "${ROOT}/scripts/bootstrap_binaries.sh" "${ENV_DIR}"
}

prepare_app() {
  chmod 755 "${ROOT}/questhub.command" 2>/dev/null || true
  mkdir -p "${DATA_DIR}/tls"
  chmod 700 "${DATA_DIR}" "${DATA_DIR}/tls"
}

# ---------------------------------------------------------------- doctor

DOCTOR_FAILS=0
DOCTOR_WARNS=0

report() {
  local status="$1" name="$2" detail="$3" color=""
  case "${status}" in
    OK) color="${C_OK}" ;;
    WARN) color="${C_WARN}"; DOCTOR_WARNS=$((DOCTOR_WARNS + 1)) ;;
    FAIL) color="${C_FAIL}"; DOCTOR_FAILS=$((DOCTOR_FAILS + 1)) ;;
    *) color="${C_DIM}" ;;
  esac
  printf '  %s%-5s%s %-22s %s\n' "${color}" "${status}" "${C_OFF}" "${name}" "${detail}"
}

port_in_use() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

chrome_app() {
  local app
  for app in "/Applications/Google Chrome.app" "${HOME}/Applications/Google Chrome.app"; do
    if [[ -d ${app} ]]; then
      printf '%s' "${app}"
      return 0
    fi
  done
  return 1
}

run_doctor() {
  DOCTOR_FAILS=0
  DOCTOR_WARNS=0
  log "Environment check"
  local mm_bin="${QH_MICROMAMBA_BIN:-${TOOLS_DIR}/bin/micromamba}" out
  if [[ -x ${mm_bin} ]] && out=$("${mm_bin}" --version 2>/dev/null); then
    report OK micromamba "${out}"
  else
    report FAIL micromamba "missing (run ./install.sh)"
  fi

  local py="${ENV_DIR}/bin/python"
  if [[ -x ${py} ]] && out=$("${py}" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null); then
    report OK python "${out} (${ENV_DIR})"
    local module
    for module in av fastapi uvicorn cryptography qrcode requests; do
      if out=$("${py}" -c "import ${module} as m; print(getattr(m, '__version__', 'ok'))" 2>/dev/null); then
        report OK "import ${module}" "${out}"
      else
        report FAIL "import ${module}" "not importable"
      fi
    done
  else
    report FAIL python "environment missing (run ./install.sh)"
  fi

  local adb="${ENV_DIR}/bin/adb"
  if [[ -x ${adb} ]] && out=$("${adb}" version 2>/dev/null | head -n1); then
    report OK adb "${out}"
  else
    report FAIL adb "missing (run ./install.sh)"
  fi

  local scrcpy="${ENV_DIR}/bin/scrcpy"
  if [[ -x ${scrcpy} ]] && out=$("${scrcpy}" --version 2>/dev/null | head -n1); then
    if [[ ${out} == *"${EXPECTED_SCRCPY_VERSION}"* ]]; then
      report OK scrcpy "${out}"
    else
      report WARN scrcpy "${out} (expected ${EXPECTED_SCRCPY_VERSION})"
    fi
  else
    report FAIL scrcpy "missing (run ./install.sh)"
  fi

  local chrome
  if chrome=$(chrome_app); then
    report OK "Google Chrome" "${chrome}"
  else
    report WARN "Google Chrome" "not found; install it from https://www.google.com/chrome/ (required for the GUI)"
  fi

  local port
  for port in ${GUI_PORTS}; do
    if port_in_use "${port}"; then
      report WARN "port ${port}" "already in use (another Quest Hub or app?)"
    else
      report OK "port ${port}" "free"
    fi
  done

  if [[ -z ${QH_PLATFORM:-} ]]; then
    local major
    major=$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)
    if [[ ${major} =~ ^[0-9]+$ ]] && (( major >= 15 )); then
      report INFO "local network" "macOS asks once per app; allow it in System Settings > Privacy & Security > Local Network, or 'adb connect' fails with 'No route to host'"
    fi
  fi

  if [[ ${QH_DOCTOR_OFFLINE:-0} != 1 && -x ${adb} ]]; then
    local usb=0 wifi=0 serial state
    while read -r serial state _; do
      [[ ${state:-} == device ]] || continue
      if [[ ${serial} == *:* ]]; then wifi=$((wifi + 1)); else usb=$((usb + 1)); fi
    done < <("${adb}" devices 2>/dev/null | tail -n +2)
    report INFO "adb devices" "${usb} USB, ${wifi} Wi-Fi"
  fi

  if (( DOCTOR_FAILS > 0 )); then
    log "${DOCTOR_FAILS} check(s) failed, ${DOCTOR_WARNS} warning(s)"
    return 1
  fi
  log "All required checks passed (${DOCTOR_WARNS} warning(s))"
}

# ---------------------------------------------------------------- modes

do_install() {
  detect_platform
  check_prerequisites
  log "Platform: ${PLATFORM}; installing into ${ENV_DIR}"
  ensure_micromamba
  ensure_environment
  install_binaries
  prepare_app
  echo
  run_doctor || die "Installation finished with failed checks (see above)."
  cat <<DONE

[install] Done.
  CLI:  ./legacy/quest_multi_scrcpy.sh start --dry-run
  GUI:  ./questhub.command
DONE
}

do_relock() {
  detect_platform
  local target="${RELOCK_PLATFORM:-${PLATFORM}}"
  local spec="${ROOT}/environment.yml"
  [[ ${target} == win-* ]] && spec="${ROOT}/environment-windows.yml"
  ensure_micromamba
  local tmp
  tmp=$(mktemp -d)
  log "Solving $(basename "${spec}") for ${target}"
  # A dry run only solves (nothing is installed), so it works for either
  # architecture. `env export` cannot read a foreign-platform prefix.
  mm create --dry-run --json --yes --prefix "${tmp}/never-created" --platform "${target}" \
    --file "${spec}" > "${tmp}/solution.json" \
    || { rm -rf "${tmp}"; die "Solving $(basename "${spec}") failed"; }
  mkdir -p "${LOCK_DIR}"
  local lock
  lock=$(lock_file_for "${target}")
  {
    printf '# Generated by ./install.sh --relock %s from %s\n' "${target}" "$(basename "${spec}")"
    printf '# platform: %s\n@EXPLICIT\n' "${target}"
    # JavaScript for Automation ships with macOS, unlike python3 (a CLT shim).
    osascript -l JavaScript - "${tmp}/solution.json" <<'JXA'
ObjC.import('Foundation');
function run(argv) {
  const text = $.NSString.stringWithContentsOfFileEncodingError(argv[0], $.NSUTF8StringEncoding, null).js;
  return JSON.parse(text).actions.LINK.map(p => p.url + '#' + p.md5).join('\n');
}
JXA
  } > "${lock}.tmp" || { rm -rf "${tmp}" "${lock}.tmp"; die "Could not write the lock file"; }
  rm -rf "${tmp}"
  grep -q '^https://.*#[0-9a-f]\{32\}$' "${lock}.tmp" || { rm -f "${lock}.tmp"; die "Lock export produced no packages"; }
  mv "${lock}.tmp" "${lock}"
  log "Wrote ${lock} ($(grep -c '^https://' "${lock}") packages)"
}

do_uninstall() {
  local what="${TOOLS_DIR} and ${ENV_DIR}"
  [[ ${PURGE_DATA} == true ]] && what="${what} and ${DATA_DIR}"
  confirm "Remove ${what}?" || { log "Cancelled"; exit 0; }
  safe_remove "${TOOLS_DIR}"
  safe_remove "${ENV_DIR}"
  if [[ ${PURGE_DATA} == true ]]; then
    safe_remove "${DATA_DIR}"
  fi
  log "Uninstalled"
}

case "${MODE}" in
  install) do_install ;;
  doctor) run_doctor ;;
  relock) do_relock ;;
  uninstall) do_uninstall ;;
esac
