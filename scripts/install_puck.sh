#!/usr/bin/env bash
# Only for a rooted, already configured upstream Echo Dot 2. No flashing.
set -euo pipefail
umask 077
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
device=${VECTOR_DEVICE:-biscuit}
case "$device" in
  biscuit) product=biscuit_puffin; build=NS65741 ;;
  radar) product=radar_puffin; build=NS6572 ;;
  *) echo 'VECTOR_DEVICE must be biscuit or radar.' >&2; exit 2 ;;
esac
binary="$repo_dir/.hardware/echo-dot-assist/build/$device/hassmic"
apply=0
case ${1:---check} in
  --check) ;;
  --apply) apply=1 ;;
  *) echo 'Usage: scripts/install_puck.sh [--check|--apply]' >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || exit 2
for tool in adb sha256sum; do command -v "$tool" >/dev/null || { echo "Missing: $tool" >&2; exit 1; }; done
mapfile -t devices < <(adb devices | awk 'NR>1 && $2=="device" && $1 !~ /:/ {print $1}')
[[ ${#devices[@]} == 1 ]] || { echo 'Connect exactly one authorized physical USB device.' >&2; exit 1; }
adb_cmd=(adb -s "${devices[0]}")
read_prop() { "${adb_cmd[@]}" shell getprop "$1" | tr -d '\r'; }
[[ $(read_prop ro.product.device) == "$product" ]] || { echo 'Connected model does not match VECTOR_DEVICE; refusing it.' >&2; exit 1; }
[[ $(read_prop ro.build.display.id) == "$build" ]] || { echo 'Install the documented upstream pinned firmware first.' >&2; exit 1; }
[[ $("${adb_cmd[@]}" shell id -u | tr -d '\r') == 0 ]] || { echo 'A working root ADB session is required.' >&2; exit 1; }
"${adb_cmd[@]}" shell 'test -f /system/etc/init/hassmic.rc && test -f /data/local/hassmic/hassmic.conf'
echo 'Verified supported model, pinned build, root, and existing upstream installation.'
if [[ $apply == 0 ]]; then echo 'Read-only check complete. Use --apply to install the built data override.'; exit 0; fi
[[ -f "$binary" && ! -L "$binary" ]] || { echo 'Build the override with scripts/prepare_puck.sh first.' >&2; exit 1; }
expected=$(sha256sum "$binary" | awk '{print $1}')
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="/data/local/hassmic/vector-backups/$stamp"
"${adb_cmd[@]}" shell "test ! -e '$backup' && mkdir -p '$backup' && chmod 700 '$backup' && cp /data/local/hassmic/hassmic.conf '$backup/hassmic.conf' && if [ -f /data/local/hassmic/hassmic ]; then cp /data/local/hassmic/hassmic '$backup/hassmic'; else touch '$backup/no-override'; fi"
restore() {
  "${adb_cmd[@]}" shell "set -e; stop hassmic; if [ -f '$backup/hassmic' ]; then cp '$backup/hassmic' /data/local/hassmic/hassmic; else rm -f /data/local/hassmic/hassmic; fi; cp '$backup/hassmic.conf' /data/local/hassmic/hassmic.conf; start hassmic"
}
on_error() {
  local code=$?
  trap - ERR
  echo 'Install failed; restoring the previous data override and config.' >&2
  if restore; then
    echo 'Previous override and config restored.' >&2
  else
    echo 'Automatic rollback failed. Keep USB connected and restore the backup manually.' >&2
  fi
  echo "Backup directory: $backup" >&2
  exit "$code"
}
trap on_error ERR
"${adb_cmd[@]}" push "$binary" /data/local/hassmic/vector-hassmic.next
"${adb_cmd[@]}" shell 'set -e; stop hassmic; chown 0:0 /data/local/hassmic/vector-hassmic.next; chmod 755 /data/local/hassmic/vector-hassmic.next; mv /data/local/hassmic/vector-hassmic.next /data/local/hassmic/hassmic; printf '\''PROTO=wyoming\nARGS="-w remote -z 0 -a 0 -i 0 -B -o 0 -W 0"\n'\'' > /data/local/hassmic/hassmic.conf; chmod 600 /data/local/hassmic/hassmic.conf; start hassmic'
actual=$("${adb_cmd[@]}" exec-out cat /data/local/hassmic/hassmic | sha256sum | awk '{print $1}')
[[ $actual == "$expected" ]] || { echo 'Installed checksum did not match.' >&2; false; }
trap - ERR
echo "Installed checksum: $actual"
echo "Backup directory: $backup"
echo 'Firmware, bootloader, Wi-Fi credentials, and system partition were not modified.'
echo 'Verify health, hardware mute, speaker playback, and a reboot before relying on this installation.'
