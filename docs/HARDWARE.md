# Supported hardware and recovery

## Exact target

This kit targets **Echo Dot 2, 2016, RS03QR, `biscuit_puffin`**, with a working upstream `hassmic` install and root ADB. Version 0.3 also supports Echo 2 XC56PY/radar with pinned build NS6572; see [the household hardware instructions](HOUSEHOLD.md#supported-hardware-and-host). Other Dot generations and Echo Shows are unsupported.

Read the [pinned upstream device guide](https://github.com/Gamer92000/echo-dot-assist/blob/8646a4f2f4aeafde2d7aca7359d9eff555a169c0/devices/biscuit/README.md) and the [original unlock/root instructions](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/). Follow the unlock author's current manual procedure. We do not wrap the unlock in an installer or repeat its flashing commands.

Unlocking/flashing is the destructive part of this project. Use a device you own and are prepared to recover or lose. Keep it offline until the upstream guide tells you to configure networking. Use a native Linux host and a USB data cable for the documented path.

The pinned upstream guide specifies:

| Item | Required value |
|---|---|
| Product | `biscuit_puffin` |
| Fire OS build | `NS65741` / 6574.1 |
| Firmware filename | `update-kindle-biscuit_puffin-NS65741_user_8146_0013222531716.bin` |
| Firmware SHA-256 | `90832e86498c5e803974c30359aea71c1129757be0ddc59d831a94b27f81487f` |
| Upstream source commit | `8646a4f2f4aeafde2d7aca7359d9eff555a169c0` |

Obtain firmware and unlock material through those upstream instructions. None is included or downloaded by this kit. The proprietary audio front end remains part of the stock firmware; this is not a fully open firmware replacement.

## Installation boundary

First complete the upstream manual unlock, pinned firmware/root steps, and `scripts/setup.sh biscuit` as directed by that project. That installs the service, firewall, and network setup. Only then use this repository's `prepare_puck.sh` and `install_puck.sh`.

The builder clones upstream into ignored `.hardware/`, checks out the exact commit, applies our source patch, and builds the ARM program using Android NDK r21e. The optional NDK archive comes from Google's HTTPS download host and is checked against the recorded SHA-256. This is artifact integrity checking, not a separate publisher signature verification.

The installer requires exactly one authorized physical USB device, root, the matching product/build, and an existing upstream config/init service. `--check` is read-only. `--apply` changes only the override/config under `/data/local/hassmic`; it does not flash firmware, touch the bootloader/system partition, or rewrite Wi-Fi credentials. It disables the optional web/OTA listeners with `-W 0 -o 0`. The Wyoming listener remains on port 16700.

## Rollback

The installer prints a timestamped directory below `/data/local/hassmic/vector-backups/`. List them without exposing config contents:

```bash
adb shell ls /data/local/hassmic/vector-backups
```

With exactly one intended USB device attached, enter the timestamp of the backup you want to restore. This checks the timestamp and backup before changing anything:

```bash
read -r -p 'Backup timestamp to restore: ' backup_stamp
[[ "$backup_stamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || { echo 'Invalid timestamp'; exit 1; }
backup_dir="/data/local/hassmic/vector-backups/$backup_stamp"
adb shell "test -f '$backup_dir/hassmic.conf' && { test -f '$backup_dir/hassmic' || test -f '$backup_dir/no-override'; }" || exit 1
adb shell "set -e; stop hassmic; if [ -f '$backup_dir/hassmic' ]; then cp '$backup_dir/hassmic' /data/local/hassmic/hassmic; chmod 755 /data/local/hassmic/hassmic; else rm -f /data/local/hassmic/hassmic; fi; cp '$backup_dir/hassmic.conf' /data/local/hassmic/hassmic.conf; start hassmic"
```

That removes only the override if none existed before, allowing the pre-existing system program to run. It does not delete the config. Verify service startup, hardware mute, and playback afterward. Restoring stock firmware is a different operation: consult the original unlock/recovery guide rather than improvising partition writes.

## Patch behavior

`patches/wyoming-vector-interrupt.patch` changes four upstream source files. It fixes remote-wake continuation, reports controls version 2, carries physical mute/cancel events, keeps the microphone stream available for a separate stop recognizer, and flushes queued output on cancellation. A `vector-stop` control message also queues a short stock confirmation earcon when stopping active unmuted output. The updated patch also adds native persistent-dismissal alarm controls, status-light indications and a mute-safe remote pipeline restart. No stock sound recording is distributed.

Since version 0.2 the portable Python controller sends `vector-listen` and runs an experimental local spoken-stop recognizer when `experimental_spoken_stop` is true. The independent physical Action and microphone-mute paths remain available. See [spoken-stop behavior](SPOKEN_STOP.md) and perform the required physical acceptance tests after installation.
