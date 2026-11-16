#!/usr/bin/env bash
# Build a source-only override. This script never connects to a physical device.
set -euo pipefail
umask 077
# Remote wake uses our local controller models, not upstream's optional LFS assets.
export GIT_LFS_SKIP_SMUDGE=1
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
work_dir="$repo_dir/.hardware"
upstream_dir="$work_dir/echo-dot-assist"
upstream_commit=8646a4f2f4aeafde2d7aca7359d9eff555a169c0
ndk_sha=ad7ce5467e18d40050dc51b8e7affc3e635c85bd8c59be62de32352328ed467e
download_ndk=0
if [[ ${1:-} == --download-ndk && $# == 1 ]]; then
  download_ndk=1
elif [[ $# != 0 ]]; then
  echo 'Usage: scripts/prepare_puck.sh [--download-ndk]' >&2
  exit 2
fi
for tool in git make sha256sum; do command -v "$tool" >/dev/null || { echo "Missing: $tool" >&2; exit 1; }; done
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || { echo 'Build host must be Linux x86_64.' >&2; exit 1; }
mkdir -p "$work_dir"
if [[ ! -d "$upstream_dir/.git" ]]; then
  [[ ! -e "$upstream_dir" ]] || { echo 'Upstream destination exists but is not a Git checkout.' >&2; exit 1; }
  git clone --no-checkout https://github.com/Gamer92000/echo-dot-assist.git "$upstream_dir"
  git -C "$upstream_dir" checkout --detach "$upstream_commit"
fi
if [[ -f "$work_dir/patched-commit" ]]; then
  [[ $(cat "$work_dir/patched-commit") == "$upstream_commit" ]] || { echo 'Unexpected prior source marker.' >&2; exit 1; }
  [[ $(git -C "$upstream_dir" rev-parse HEAD) == "$upstream_commit" ]] || exit 1
  git -C "$upstream_dir" apply --reverse --check "$repo_dir/patches/wyoming-vector-interrupt.patch"
else
  [[ -z $(git -C "$upstream_dir" status --porcelain) ]] || { echo 'Preserve changes in the upstream checkout before continuing.' >&2; exit 1; }
  git -C "$upstream_dir" checkout --detach "$upstream_commit"
  git -C "$upstream_dir" apply --check "$repo_dir/patches/wyoming-vector-interrupt.patch"
  git -C "$upstream_dir" apply "$repo_dir/patches/wyoming-vector-interrupt.patch"
  printf '%s\n' "$upstream_commit" > "$work_dir/patched-commit"
fi
ndk_dir=${ANDROID_NDK_HOME:-$work_dir/android-ndk-r21e}
if [[ ! -x "$ndk_dir/toolchains/llvm/prebuilt/linux-x86_64/bin/armv7a-linux-androideabi24-clang" ]]; then
  [[ $download_ndk == 1 ]] || { echo 'Set ANDROID_NDK_HOME to NDK r21e, or pass --download-ndk (about 1.2 GB).' >&2; exit 1; }
  [[ -z ${ANDROID_NDK_HOME:-} ]] || { echo 'ANDROID_NDK_HOME is invalid; fix it or unset it before downloading.' >&2; exit 1; }
  for tool in curl unzip; do command -v "$tool" >/dev/null || { echo "Missing: $tool" >&2; exit 1; }; done
  [[ ! -e "$ndk_dir" ]] || { echo 'Incomplete NDK directory exists; preserve or move it first.' >&2; exit 1; }
  archive="$work_dir/android-ndk-r21e-linux-x86_64.zip"
  if [[ ! -f "$archive" ]]; then
    curl --fail --location --proto '=https' --proto-redir '=https' \
      --output "$archive.part" https://dl.google.com/android/repository/android-ndk-r21e-linux-x86_64.zip
    mv "$archive.part" "$archive"
  fi
  printf '%s  %s\n' "$ndk_sha" "$archive" | sha256sum --check -
  unzip -q "$archive" -d "$work_dir"
  ndk_dir="$work_dir/android-ndk-r21e"
fi
grep -q 'Pkg.Revision = 21.4.7075529' "$ndk_dir/source.properties" || { echo 'Use Android NDK r21e (21.4.7075529).' >&2; exit 1; }
make -C "$upstream_dir" DEVICE=biscuit STUBS=1 NDK="$ndk_dir" build/biscuit/hassmic
sha256sum "$upstream_dir/build/biscuit/hassmic"
echo 'Built the override. No USB device, firmware, bootloader, or system partition was accessed.'
