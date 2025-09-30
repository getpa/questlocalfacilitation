#!/usr/bin/env bash
# bootstrap_binaries.sh — Download scrcpy and Android platform tools into the active conda/mamba env
set -euo pipefail

SCRCPY_VERSION=${SCRCPY_VERSION:-"3.3.3"}
PLATFORM_TOOLS_URL=${PLATFORM_TOOLS_URL:-"https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"}
SCRCPY_BASE_URL=${SCRCPY_BASE_URL:-"https://github.com/Genymobile/scrcpy/releases/download"}

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

bin_dir="${prefix}/bin"
vendor_dir="${prefix}/vendor"
pt_dir="${vendor_dir}/platform-tools"
scrcpy_dir="${vendor_dir}/scrcpy-v${SCRCPY_VERSION}"
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

# Install scrcpy binaries
if [[ ! -x "${scrcpy_dir}/scrcpy" ]]; then
  tmp_dir=$(mktemp -d)
  tar_path="${tmp_dir}/scrcpy.tar.gz"
  scrcpy_filename="scrcpy-macos-aarch64-v${SCRCPY_VERSION}.tar.gz"
  download "${SCRCPY_BASE_URL}/v${SCRCPY_VERSION}/${scrcpy_filename}" "${tar_path}"
  tar -xzf "${tar_path}" -C "${tmp_dir}"
  rm -rf "${scrcpy_dir}"
  mv "${tmp_dir}/scrcpy-macos-aarch64-v${SCRCPY_VERSION}" "${scrcpy_dir}"
  rm -rf "${tmp_dir}"
else
  echo "[=] scrcpy v${SCRCPY_VERSION} already present, skipping download" >&2
fi

ln -sf "${scrcpy_dir}/scrcpy" "${bin_dir}/scrcpy"
ln -sf "${scrcpy_dir}/scrcpy-server" "${bin_dir}/scrcpy-server"

cat <<INFO
[+] Binaries ready
    adb:        ${bin_dir}/adb
    fastboot:   ${bin_dir}/fastboot
    scrcpy:     ${bin_dir}/scrcpy
INFO
