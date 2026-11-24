#!/usr/bin/env bash
# build_imu_helper.sh — rebuild questhub/imu/questhub-imu.dex from questhub/imu/java (maintainers).
#
#   ./scripts/build_imu_helper.sh           rebuild and replace the committed DEX
#   ./scripts/build_imu_helper.sh --check   rebuild and fail if it differs from the committed DEX (CI)
#
# The helper reaches Android only through reflection, so it needs a plain JDK (no Android SDK).
# Both tools are pinned: OpenJDK from conda-forge (via the project's micromamba) and Google's d8 (r8 jar).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OPENJDK_SPEC=${OPENJDK_SPEC:-"openjdk=17.0.21.1"}
R8_VERSION=${R8_VERSION:-"8.13.25"}
R8_SHA256=${R8_SHA256:-"0d4f2f68c4fcbc9434eff122f41feb6fb1d98efd2faa530cec1abacab1cea03d"}
R8_URL="https://dl.google.com/android/maven2/com/android/tools/r8/${R8_VERSION}/r8-${R8_VERSION}.jar"
SOURCE="${ROOT}/questhub/imu/java/questhub/ImuStream.java"
TARGET="${ROOT}/questhub/imu/questhub-imu.dex"
MICROMAMBA=${MICROMAMBA:-"${ROOT}/.tools/bin/micromamba"}

check=0
[[ "${1:-}" == "--check" ]] && check=1

log() { printf '[build-imu] %s\n' "$*" >&2; }
sha256() { shasum -a 256 "$1" | awk '{print $1}'; }

work=${IMU_BUILD_DIR:-"${ROOT}/.tools/imu-build"}
mkdir -p "${work}"

jdk="${work}/jdk"
javac=$(find "${jdk}" -type f -name javac -path '*/bin/*' 2>/dev/null | head -n 1 || true)
if [[ -z "${javac}" ]]; then
  [[ -x "${MICROMAMBA}" ]] || { log "micromamba not found (${MICROMAMBA}); run ./install.sh first"; exit 1; }
  log "Installing ${OPENJDK_SPEC} into ${jdk}"
  "${MICROMAMBA}" create -y -q -p "${jdk}" -c conda-forge --override-channels "${OPENJDK_SPEC}" >/dev/null
  javac=$(find "${jdk}" -type f -name javac -path '*/bin/*' | head -n 1)
fi
java="$(dirname "${javac}")/java"

r8="${work}/r8-${R8_VERSION}.jar"
if [[ ! -f "${r8}" ]] || [[ "$(sha256 "${r8}")" != "${R8_SHA256}" ]]; then
  log "Downloading d8 (r8 ${R8_VERSION})"
  curl -fsSL -o "${r8}.part" "${R8_URL}"
  [[ "$(sha256 "${r8}.part")" == "${R8_SHA256}" ]] || { rm -f "${r8}.part"; log "r8 checksum mismatch"; exit 1; }
  mv "${r8}.part" "${r8}"
fi

rm -rf "${work}/classes" "${work}/dex"
mkdir -p "${work}/classes" "${work}/dex"
"${javac}" --release 8 -nowarn -d "${work}/classes" "${SOURCE}"
classes=()
while IFS= read -r f; do classes+=("$f"); done < <(find "${work}/classes" -name '*.class' | LC_ALL=C sort)
"${java}" -cp "${r8}" com.android.tools.r8.D8 --release --min-api 29 --output "${work}/dex" "${classes[@]}"
built="${work}/dex/classes.dex"

if [[ ${check} -eq 1 ]]; then
  if [[ "$(sha256 "${built}")" != "$(sha256 "${TARGET}")" ]]; then
    log "The committed ${TARGET#"${ROOT}"/} does not match the source; run ./scripts/build_imu_helper.sh"
    exit 1
  fi
  log "questhub-imu.dex matches the source ($(sha256 "${TARGET}"))"
else
  cp "${built}" "${TARGET}"
  log "Wrote ${TARGET#"${ROOT}"/} ($(wc -c < "${TARGET}" | tr -d ' ') bytes, $(sha256 "${TARGET}"))"
fi
