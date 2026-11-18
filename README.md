# Vector Puck

Turn a rooted **Echo Dot 2 (RS03QR / biscuit)** or **Echo 2 (XC56PY / radar)** into a GPT voice companion with local wake detection and speech recognition, Cedar replies, and short follow-up conversations.

This is a community experiment for hardware owners. The controller and patches are MIT licensed. Rooting is a separate, manual prerequisite and can erase or brick the device. This is not an Amazon or OpenAI product.

## What is included

- “Hey Vector” detection with local Vosk and local Whisper transcription.
- GPT answers through the Responses API and natural Cedar speech through Realtime.
- An original, locally synthesized working tune while the answer is prepared.
- An eight-second follow-up window after playback, bounded by turn and session limits.
- Physical microphone mute and Action-button cancellation, including queued speaker audio.
- Experimental local spoken stop during the working tune and Cedar replies, with a short confirmation tone.
- Optional, allowlisted Home Assistant audio, thermostat, and TV-script controls, starting in dry-run mode.
- Optional shared shopping lists, named timers/reminders, persistent alarms, single-responder coordination, accepted room calls and announcements.
- Local rain/thunderstorm/white-noise/brown-noise playback, saved recipes with room-specific cooking progress, and deterministic conversions.
- Expiring RGB update status and a verified-completion green pulse, subordinate to mute and urgent indications.
- A status-only delivery inbox foundation; delivery account adapters are not included.
- A restricted Docker controller, checksum-pinned model downloads, tests, and rollback instructions.

**The current source adds [kitchen essentials and update lights](docs/KITCHEN.md) to the optional household mode and full-size Echo 2 support.** Start with [the household guide](docs/HOUSEHOLD.md) for two or more rooms, alarms, calling and shared lists. [The changelog](CHANGELOG.md) records features and fixes. Home control remains disabled until an owner configures it; generic shell and HA-service tools are never exposed. Web search, calendars, live weather sources, and personal deployment settings are not included. Software validation is separate from real hardware acceptance; see [validation](docs/VALIDATION.md).

## Requirements

- Echo Dot **second generation**, model **RS03QR**, already unlocked, rooted, and configured with the pinned [upstream satellite](docs/HARDWARE.md). Echo 2 XC56PY/radar uses the additional instructions in the household guide; other Echo generations and Echo Shows are unsupported here.
- A data-capable micro-USB cable. USB is needed for setup and updating; normal operation uses Wi-Fi and power.
- Linux x86_64 for the documented build/install path, with Git, Make, `curl`, `unzip`, `sha256sum`, Android `adb`, Python 3.12+, Docker Engine, and Docker Compose v2.
- A Linux Docker host with at least 4 GB RAM available and four CPU threads recommended. CPU recognition may be slower than a GPU-equipped system. The container has a 2 GB memory limit.
- Your own OpenAI API project/key with access to the configured models. API charges are separate from a ChatGPT subscription. Defaults are `gpt-6-astra` for answers and `gpt-realtime-2.1-mini` for Cedar speech; both requests can incur charges.

Only one Wyoming controller should connect to the puck. Disconnect an existing Home Assistant Wyoming integration before using this controller.

Looking for inexpensive room hardware? See [device choices and exact model numbers](docs/DEVICE_CHOICES.md). Only the exact biscuit/radar models and pinned builds described here are supported; upstream support for another model does not establish compatibility.

## 1. Prepare the hardware

Follow [the hardware guide](docs/HARDWARE.md) first. This repository does not automate bootloader unlocking, flashing, or rooting, and distributes no firmware or proprietary libraries.

```bash
git clone https://github.com/jdogg172/vector-puck.git
cd vector-puck
bash scripts/prepare_puck.sh --download-ndk
bash scripts/install_puck.sh --check
```

The builder downloads the pinned upstream source and a checksum-verified Android NDK r21e (about 1.2 GB) into ignored `.hardware/`. If NDK r21e is already installed, set `ANDROID_NDK_HOME` and omit `--download-ndk`.

The install check is read-only. After it verifies the model, pinned firmware, root, and existing upstream installation, install the data-directory override:

```bash
bash scripts/install_puck.sh --apply
```

The installer backs up the previous program/config, verifies the installed program checksum, and attempts automatic restoration if installation fails. It reports incomplete recovery and the retained backup path if restoration also fails. It disables the optional satellite web/update listeners. Keep its printed backup location. [Manual rollback](docs/HARDWARE.md#rollback) is documented.

## 2. Store your own API key privately

Create a dedicated key in your OpenAI project. Save it outside this checkout; never paste it into chat, a command argument, or a Git file. This local prompt writes a new private file without echoing the key or overwriting an existing one:

```bash
python3 - <<'PY'
import getpass
import os
from pathlib import Path
import re

directory = Path.home() / '.config' / 'vector-puck'
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
path = directory / 'openai.key'
key = getpass.getpass('Dedicated OpenAI API key (hidden): ').strip()
if not re.fullmatch(r'sk-[A-Za-z0-9_-]{20,600}', key):
    raise SystemExit('Key format was rejected; nothing was written.')
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'w') as output:
    output.write(key + '\n')
print('Saved a private key file outside the checkout.')
PY
```

The controller checks that this is a regular private file owned by its user. Compose runs with your numeric user/group and mounts the key read-only as a Docker secret.

## 3. Configure and download recognition models

Find the puck's IPv4 address in your router, then enter it at this prompt. Setup checks key-file permissions without reading the key.

```bash
read -r -p 'Puck local IPv4 address: ' puck_ip
python3 scripts/setup_local.py \
  --puck-host "$puck_ip" \
  --key-file "$HOME/.config/vector-puck/openai.key"
mkdir -p models state .cache
chmod 700 state
python3 scripts/download_models.py --destination models
python3 scripts/download_models.py --destination models --verify-only
```

Setup writes ignored `config/local.json` and `.env` with the external key path and your UID/GID. It refuses to overwrite existing local config. Edit `config/local.json` to adjust settings; [the schema](config/config.schema.json) documents all supported fields. `config/config.example.json` uses a documentation-only address and cannot connect until configured.

Model downloads total about 190 MB. URLs, immutable revisions, sizes, hashes, and provenance are in [models.lock.json](config/models.lock.json). No model weights are distributed in this repository. Runtime downloads are disabled.

## 4. Run

```bash
docker compose build
docker compose run --rm --no-deps vector --check-config
docker compose up -d
docker compose ps
docker compose logs --tail=30 vector
```

The logs contain enumerated events, not transcripts or keys. Local health is in `state/health.json`:

```bash
python3 -m json.tool state/health.json
```

Unmute the puck, ask “Hey Vector, why does thunder happen?”, and wait for its reply. After the ready chime, say “Explain that more simply” within eight seconds. Check the physical mute button and Action cancellation before relying on it. Follow-ups are limited to three turns and a three-minute session by default.

During a long reply, say “Hey Vector, stop.” The local worker confirms an addressed stop, discards queued output, and requests the puck's short confirmation tone. Physical Action and mute remain available. New configurations enable `experimental_spoken_stop`; existing configurations that explicitly set it to `false` stay disabled. Read [spoken-stop behavior and limits](docs/SPOKEN_STOP.md) before changing it.

The controller has no published inbound port. The puck's Wyoming port **16700 is an unauthenticated LAN service**; keep it on a trusted or isolated network and never forward it to the Internet. Retain the upstream device firewall. See [privacy and network boundaries](docs/PRIVACY.md).

## 5. Optional Home Assistant and TV controls

Skip this section for conversation only. Use a dedicated HA user and a long-lived access token appropriate for that account. The client limits its own requests; those limits do not reduce the token's server-side authority. Create the token privately in HA, then use this hidden local prompt to store it outside the checkout:

```bash
python3 - <<'PY'
import getpass
import os
from pathlib import Path
import re

directory = Path.home() / '.config' / 'vector-puck'
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
token = getpass.getpass('Home Assistant access token (hidden): ').strip()
if not re.fullmatch(r'[A-Za-z0-9._~+/=-]{20,4096}', token):
    raise SystemExit('Token format rejected; nothing was written.')
path = directory / 'homeassistant.token'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'w') as output:
    output.write(token + '\n')
print('Saved an external private token file.')
PY
read -r -p 'Home Assistant LAN origin, including http(s) and port: ' ha_url
python3 scripts/setup_home.py \
  --url "$ha_url" \
  --token-file "$HOME/.config/vector-puck/homeassistant.token"
```

Setup checks file metadata without reading the token, backs up existing local files under ignored `.cache/`, and creates ignored `config/homeassistant.local.json` in **dry-run mode**. It also updates `config/local.json` and `.env` for the optional Compose override. Repeat setup refuses to overwrite an existing mapping.

**Review and edit that private mapping before running.** Replace every example entity with your own HA entity, remove unused targets, map source names, and set sensible volume and temperature limits. TV actions are your reviewed HA `script.*` entities for opening an app, channel or dashboard. [The complete guide](docs/HOME_ASSISTANT.md) explains unit validation, aliases, permitted operations, and result verification.

```bash
docker compose -f compose.yaml -f compose.homeassistant.yaml config --quiet
docker compose -f compose.yaml -f compose.homeassistant.yaml run --rm --no-deps vector --check-config
docker compose -f compose.yaml -f compose.homeassistant.yaml up -d --build
```

Ask for a configured device's status, then request one small change. In dry-run mode it reads the target and validates the proposed change but sends **no control command**. After reviewing the results, set `mode` to `control` in the private mapping and restart using the same two Compose files. Verify actual hardware outcomes before relying on them. Stop using the same two-file command with `stop`; to remove this integration, clear `home_assistant_file` and recreate the service with only `compose.yaml`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Container cannot read its key | Use a regular 0600/0400 file owned by the operator; rerun setup only after preserving/removing its existing local outputs. Do not solve this by making the key world-readable. |
| Disconnected / reconnecting | Check power, Wi-Fi, the configured IP, port 16700, and whether another Wyoming controller is connected. |
| No response with a red ring | The physical microphone mute is on. Unmute only when ready to listen. |
| Slow answers | CPU Whisper, answer generation, and Cedar generation are sequential. The working tune indicates progress; it does not mean the Internet connection has stalled. |
| Model load failure | Run the offline verification command and check RAM. Download models before starting. |
| Cloud request failure | Check API project billing/model access and the configured model names. Logs intentionally omit provider response bodies and credentials. |
| Too many requests | Defaults allow 30 attempts/hour and 100/day. Failed attempts count too; these are request limits, not a dollar spending cap. |
| Voice will not stop on spoken command | Check `experimental_spoken_stop`, `interrupt_worker_running` in health, placement, and CPU load. Use an addressed phrase such as “Hey Vector, stop.” Press Action or physical mute if it misses. |
| Home command is rejected or unchanged | Check dry-run mode, configured alias/limits, device availability, and thermostat unit metadata. A submitted TV script is not confirmation of a visible screen change. |
| False wakes or poor room pickup | Place the puck away from TV speakers and test from the actual seating position. Recognition settings are in local config; validate changes against both false wakes and missed requests. |

Stop the controller with `docker compose stop`. Do not delete the satellite config as a way to disable it: upstream uses that file as part of its startup behavior.

## Development and verification

```bash
docker build -t vector-puck:test .
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m --cap-drop ALL \
  --security-opt no-new-privileges:true \
  -v "$PWD:/repo:ro" -w /repo --entrypoint python \
  vector-puck:test -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/scan_public.py
bash -n scripts/prepare_puck.sh scripts/install_puck.sh
```

See [validation and protocol notes](docs/VALIDATION.md) for the C regression harness and release boundaries. The publication scanner is heuristic; review the actual staged files as well. It does not inspect ignored files or historical commits.

The container base digest and tested Python runtime dependency versions are pinned. Update `requirements.lock` intentionally and rerun the image/model checks when upgrading. The lock records versions, not package signature verification.

## Credits and license

The device program builds on [Gamer92000/echo-dot-assist](https://github.com/Gamer92000/echo-dot-assist), pinned to commit `8646a4f2f4aeafde2d7aca7359d9eff555a169c0`. Its MIT notice is retained in [patches/UPSTREAM-LICENSE](patches/UPSTREAM-LICENSE). See [NOTICE](NOTICE) for third-party boundaries and model licenses.
