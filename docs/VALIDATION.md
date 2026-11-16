# Validation and development notes

## Release evidence

Portable software checks remain separate from physical device acceptance. A working household prototype motivated this kit; that is not proof that a new operator's speaker, firmware, network, models, API account, and room acoustics work together.

Offline unit tests exercise framing limits, key-file ownership/permissions, model verification/tamper rejection, config boundaries, quotas, mute/Action cancellation, stale-output rejection, wake-only pauses, connection watchdogs, and a synthetic complete conversation through reply playback and follow-up. They do not make paid API calls or connect to a real microphone/device.

GitHub CI builds the image and runs these tests without networking or credentials. It also scans public files and checks shell syntax. Successful CI is software evidence, not acoustic or cloud integration acceptance.

Initial release checks on 2026-11-16:

| Check | Observed result |
|---|---|
| Final controller image | Built successfully with pinned base digest and runtime versions |
| Unit/integration harness | 35 tests passed with networking disabled, read-only repository/root filesystem, and `noexec` temporary storage |
| Source patch | Applied to the exact upstream commit; ARM build and repeat build passed |
| Actual host C daemon | Stop flush, confirmation earcon queue, and normal playback continuation passed with synthetic audio |
| Recognition models | Pinned downloads and offline checksum verification passed; Vosk and Whisper both loaded in the final image with networking disabled |
| Installer recovery | Simulated wrong model/build, checksum failure, failed service start, system fallback, and incomplete recovery reporting passed |
| Publication candidates | 37 source/documentation/config files reviewed; custom privacy scan and Gitleaks found no leaks |
| Portable real-device/cloud flow | Not separately exercised; operator acceptance above remains required |

These checks used no personal API credential or household recording. Compiled programs, model weights, build caches, and toolchains remain excluded from the published tree.

Version 0.2 adds a local spoken-stop worker and optional owner-configured HA controls. Offline coverage additionally checks independent Whisper stop confirmation after a Vosk miss, stale recognition across mute/new streams, fail-closed worker failure, bounded stop-only queues, one-tool routing, malformed/multiple proposal rejection, cancellation before dispatch, dry-run behavior, fixed HA paths, thermostat range preservation, source/volume limits, ambiguous write outcomes, sanitized readings, and setup restoration after a write failure.

Version 0.2 checks on 2026-11-16:

| Check | Observed result |
|---|---|
| Controller image | Python 3.12 image built with the existing pinned runtime/base |
| Unit/integration harness | 76 tests passed in the image with networking disabled, read-only root/repository and `noexec` temporary storage; includes a real httpx client using an in-memory mock transport |
| Actual host C daemon | Synthetic microphone-during-output, stop/queue flush, confirmation earcon queue and normal completion regression passed; the C patch is unchanged |
| Recognition models | Offline Vosk stop grammar and Whisper loaded and processed synthetic silence in the image |
| Compose | Base configuration works without HA settings; optional merge retains OpenAI secret and adds read-only HA mapping/token mounts |
| Publication | 50 source/documentation/config candidates; privacy scan, lint, shell syntax and whitespace checks passed; staged/history secret scanning is required before publication |
| Live HA / portable acoustic acceptance | Not exercised; no household service, credential, microphone recording or paid API was used for these checks |

## Actual C program regression

The standalone `tests/c_protocol_regression.py` harness drives a host build of the patched upstream C daemon with synthetic audio. It checks microphone streaming while reply audio is queued, prompt discard of queued audio on stop, and normal reply completion without accidentally cancelling the next pipeline.

After building upstream's `build/hassmic-host` with its documented host prerequisites:

```bash
mkdir -p .cache
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python tests/c_protocol_regression.py .hardware/echo-dot-assist/build/hassmic-host
```

This is separate from the ARM build; building the ARM program alone does not run the host harness. The harness uses fake audio and does not establish real microphone pickup, hardware mute, or audible tone quality.

## Required operator acceptance

After installation:

1. Check container health and the correct device/model/build.
2. Ask a question from the normal seating position, hear an intelligible Cedar answer, and check that the waiting tune stops before speech.
3. Ask a follow-up without the wake phrase inside eight seconds, then check that the window closes afterward.
4. Press physical microphone mute. Confirm the red indicator, no new requests, and no continuing speaker answer. Unmute only when ready.
5. During a long answer, press Action and confirm queued speech stops. Test after a restart too.
6. With experimental spoken stop enabled, interrupt both the waiting tune and actual spoken answer from the normal seat. Confirm output stops and the short tone is heard. Verify Action/mute still work if speech recognition misses.
7. If opting into home control, test status and dry-run proposals first, then one bounded change at a time. Independently confirm media, temperature and TV results; HA readback or script acceptance alone is not physical acceptance. Preserve the previous settings for restoration.
8. Leave it near ordinary room/TV audio and measure both false wakes and missed requests before treating it as an always-on appliance.

Do not claim a passed health check proves any of those physical outcomes.

## Controls version 2

The source patch adds `vector_controls:2` capability metadata, `vector-mute`, `vector-cancel`, `vector-listen`, `vector-stop`, and a `vector_barge_in:true` marker on stop-only microphone streams. The portable controller requires controls version 2. Stop-only streams must never become normal conversation requests.

`vector-listen` prepares capture during output. `vector-stop` flushes queued speaker chunks and emits cancellation; when appropriate it also previews a short stock earcon. Normal `audio-stop`/`played` completion must not emit an extra cancellation that would destroy the follow-up window.

Version 0.2's experimental spoken-stop worker confirms addressed commands locally, checks both cancellation epochs and microphone-stream serials, and exposes numeric health counters without transcripts. See [spoken stop](SPOKEN_STOP.md) for acoustic and latency limits. The hardware Action/mute paths remain available independently of cloud response time.

## Contributor boundaries

Keep hardware ports, firmware assumptions, and library expectations explicit. Do not bundle stock binaries or firmware. Keep new features optional and avoid hidden cloud uploads. Add behavioral tests for cancellation and follow-up state transitions; do not replace real acceptance with mocked success claims.
