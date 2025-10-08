#!/usr/bin/env bash
# bootstrap_binaries.sh — Provision adb/platform-tools and build scrcpy into the active conda/mamba env
set -euo pipefail

PLATFORM_TOOLS_URL=${PLATFORM_TOOLS_URL:-"https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"}
SCRCPY_VERSION=${SCRCPY_VERSION:-"3.3.3"}
SCRCPY_BASE_URL=${SCRCPY_BASE_URL:-"https://github.com/Genymobile/scrcpy/releases/download"}
# Build from the client-crop branch by default; set SCRCPY_BUILD_FROM_GIT=0 to use the prebuilt release instead.
SCRCPY_BUILD_FROM_GIT=${SCRCPY_BUILD_FROM_GIT:-1}
SCRCPY_GIT_REPO=${SCRCPY_GIT_REPO:-"https://github.com/kevinagnes/scrcpy.git"}
SCRCPY_GIT_REF=${SCRCPY_GIT_REF:-"client-crop-option"}
SCRCPY_INSTALL_DIR_NAME=${SCRCPY_INSTALL_DIR_NAME:-"scrcpy-client-crop"}

log() {
  local level="$1"
  shift
  printf '[bootstrap][%s] %s\n' "${level}" "$*" >&2
}

prefix=""
if [[ $# -ge 1 ]]; then
  prefix="$1"
else
  prefix="${CONDA_PREFIX:-}"
fi

if [[ -z "${prefix}" ]]; then
  echo "[!] Could not determine environment prefix. Activate the env or pass it as an argument." >&2
  echo "    Usage: SCRCPY_VERSION=3.3.3 ./scripts/bootstrap_binaries.sh /path/to/env" >&2
  exit 1
fi

prefix="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "${prefix}")"

if [[ ! -d "${prefix}" ]]; then
  echo "[!] Prefix not found: ${prefix}" >&2
  exit 1
fi

command -v git >/dev/null 2>&1 || { echo "[!] git is required." >&2; exit 1; }

bin_dir="${prefix}/bin"
vendor_dir="${prefix}/vendor"
pt_dir="${vendor_dir}/platform-tools"
mkdir -p "${bin_dir}" "${vendor_dir}"

# download helper
download() {
  local url="$1"
  local dest="$2"
  echo "[+] Downloading ${url}" >&2
  curl -L --progress-bar "$url" -o "$dest"
}

# Install Android platform tools
if [[ ! -x "${pt_dir}/adb" ]]; then
  tmp_dir=$(mktemp -d)
  zip_path="${tmp_dir}/platform-tools.zip"
  download "${PLATFORM_TOOLS_URL}" "${zip_path}"
  unzip -q "${zip_path}" -d "${tmp_dir}"
  rm -rf "${pt_dir}"
  mv "${tmp_dir}/platform-tools" "${pt_dir}"
  rm -rf "${tmp_dir}"
else
  echo "[=] Platform tools already present, skipping download" >&2
fi

ln -sf "${pt_dir}/adb" "${bin_dir}/adb"
ln -sf "${pt_dir}/fastboot" "${bin_dir}/fastboot"

is_truthy() {
  local value="${1:-}"
  value=$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]')
  case "${value}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

install_scrcpy_from_release() {
  local scrcpy_dir="${vendor_dir}/scrcpy-v${SCRCPY_VERSION}"
  if [[ ! -x "${scrcpy_dir}/scrcpy" ]]; then
    log INFO "Downloading scrcpy v${SCRCPY_VERSION} release tarball"
    local tmp_dir
    tmp_dir=$(mktemp -d)
    local tar_path="${tmp_dir}/scrcpy.tar.gz"
    local scrcpy_filename="scrcpy-macos-aarch64-v${SCRCPY_VERSION}.tar.gz"
    download "${SCRCPY_BASE_URL}/v${SCRCPY_VERSION}/${scrcpy_filename}" "${tar_path}"
    tar -xzf "${tar_path}" -C "${tmp_dir}"
    rm -rf "${scrcpy_dir}"
    mv "${tmp_dir}/scrcpy-macos-aarch64-v${SCRCPY_VERSION}" "${scrcpy_dir}"
    rm -rf "${tmp_dir}"
  else
    log INFO "scrcpy v${SCRCPY_VERSION} already present, skipping download"
  fi
  ln -sf "${scrcpy_dir}/scrcpy" "${bin_dir}/scrcpy"
  ln -sf "${scrcpy_dir}/scrcpy-server" "${bin_dir}/scrcpy-server"
  SCRCPY_FINAL_DIR="${scrcpy_dir}"
}

install_scrcpy_from_git() {
  command -v meson >/dev/null 2>&1 || { echo "[!] meson is required to build scrcpy from source." >&2; exit 1; }
  command -v ninja >/dev/null 2>&1 || { echo "[!] ninja is required to build scrcpy from source." >&2; exit 1; }

  local src_dir="${vendor_dir}/scrcpy-src"
  local build_dir="${vendor_dir}/scrcpy-build"
  local install_dir="${vendor_dir}/${SCRCPY_INSTALL_DIR_NAME}"
  local commit_file="${install_dir}/.git-ref"

  if [[ ! -d "${src_dir}/.git" ]]; then
    log INFO "Cloning ${SCRCPY_GIT_REPO} (${SCRCPY_GIT_REF})"
    rm -rf "${src_dir}"
    git clone --branch "${SCRCPY_GIT_REF}" --single-branch "${SCRCPY_GIT_REPO}" "${src_dir}"
  else
    log INFO "Updating scrcpy source checkout"
    git -C "${src_dir}" fetch origin "${SCRCPY_GIT_REF}"
    git -C "${src_dir}" checkout "${SCRCPY_GIT_REF}"
    git -C "${src_dir}" reset --hard "origin/${SCRCPY_GIT_REF}"
  fi

  local commit
  commit=$(git -C "${src_dir}" rev-parse HEAD)

  local needs_build=0
  if [[ ! -x "${install_dir}/bin/scrcpy" ]]; then
    needs_build=1
  elif [[ ! -f "${commit_file}" ]] || [[ "$(cat "${commit_file}")" != "${commit}" ]]; then
    needs_build=1
  fi

  if [[ "${needs_build}" -eq 1 ]]; then
    log INFO "Building scrcpy from source (commit ${commit})"
    rm -rf "${build_dir}" "${install_dir}"
    mkdir -p "${build_dir}"
    (cd "${src_dir}" && meson setup "${build_dir}" \
      --prefix "${install_dir}" \
      --buildtype release \
      --strip \
      -Db_lto=true)
    meson compile -C "${build_dir}"
    meson install -C "${build_dir}"
    echo "${commit}" > "${commit_file}"
  else
    log INFO "scrcpy already built at commit ${commit}, skipping rebuild"
  fi

  ln -sf "${install_dir}/bin/scrcpy" "${bin_dir}/scrcpy"
  ln -sf "${install_dir}/share/scrcpy/scrcpy-server" "${bin_dir}/scrcpy-server"
  SCRCPY_FINAL_DIR="${install_dir}"
}

SCRCPY_FINAL_DIR=""

if is_truthy "${SCRCPY_BUILD_FROM_GIT}"; then
  install_scrcpy_from_git
else
  install_scrcpy_from_release
fi

cat <<INFO
[+] Binaries ready
    adb:        ${bin_dir}/adb
    fastboot:   ${bin_dir}/fastboot
    scrcpy:     ${bin_dir}/scrcpy
    source:     ${SCRCPY_FINAL_DIR}
INFO
