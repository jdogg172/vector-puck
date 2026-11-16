# Privacy and network boundaries

## Data flow

1. The puck sends 16 kHz mono microphone audio across your LAN to the controller.
2. Vosk detects the wake phrase locally. Whisper transcribes locally. No microphone-upload API is implemented here.
3. Recognized request text and bounded recent text context go to OpenAI for an answer, with Responses `store:false`.
4. Generated answer text goes to OpenAI Realtime for Cedar speech. The returned audio plays on the puck.
5. With optional home control enabled, the Responses request also contains configured target aliases, labels, script descriptions, source aliases, and thermostat units/limits. HA URLs, tokens, entity IDs and raw attributes stay local. Selected sanitized results are spoken directly through Cedar, so those spoken readings/results also go to OpenAI's voice API.

`store:false` is a request setting, not a claim of zero provider retention. Review your OpenAI project's data controls and applicable service terms. This is an AI-generated voice; people using the device should know that.

Raw audio is held in bounded memory and is not written to recordings by this controller. Conversation history stays in memory, holds at most six messages, and expires after 20 minutes by default. Mute, cancellation, and disconnect clear it. Health and quota files persist operational counters/state, not transcripts. Logs contain enumerated event names without household requests, API response bodies, or keys.

During output, the experimental stop listener receives a separate bounded microphone stream and confirms addressed stop requests locally with Vosk and Whisper. It sends no microphone audio or interruption transcript to OpenAI and never runs home actions from that stream. Stale recognition results cannot cancel a newer reply. Health exposes only stop-worker state and numeric counters. The physical mute and Action paths remain independent of cloud requests.

The upstream device program still uses stock Amazon firmware and proprietary audio components. Complete the upstream firewall/service lockdown; this source patch alone is not proof that every stock service is contained. Do not register or reconnect stock Alexa services as part of this setup.

## Credentials and limits

Keep your dedicated key outside the checkout in an operator-owned 0600/0400 regular file. Local setup stores its path, not its contents. Compose mounts it read-only at `/run/secrets/openai_key` and uses the operator UID/GID. Do not commit `.env`, local config, state, recordings, model weights, firmware, or toolchains.

Optional HA setup uses a second external private token file and an ignored local mapping. Its Compose override mounts both read-only. Setup backups contain private addresses/entity mappings and file paths, and remain in ignored `.cache/`; retain or delete them according to your own needs. Never publish these files. HA access over HTTP sends a bearer token without encryption on the LAN; use network isolation or HTTPS with a trusted matching certificate. TLS verification is not disabled.

Default quotas are 30 request attempts/hour and 100/day, including failures. Hourly timestamps and the UTC daily count survive restarts. One conversational request can make two billable API calls. These caps are not a spending cap; model prices and answer lengths affect cost. Set your provider project controls appropriately.

## LAN trust

Wyoming port 16700 on the puck is unencrypted and unauthenticated. Run it on a trusted or segmented LAN, preserve upstream firewall rules, and permit only your controller where practical. Never forward it to the Internet. Device isolation is an operator networking task; this kit does not change your router.

The Docker controller exposes no inbound port, runs as a non-root UID with a read-only root filesystem, drops capabilities, and uses a bounded temporary filesystem. It requires outbound access to the puck and OpenAI. Model downloads happen explicitly before runtime; Hugging Face/Transformers automatic downloads and telemetry are disabled in the image.

The default assistant has no device tools. The optional integration exposes one bounded `home_action` function for owner-configured aliases. It permits selected media controls, thermostat targets, and fixed HA scripts, with fresh state validation, one dispatch per request, no automatic retries, and explicit unverified outcomes. It never exposes browsing, shell commands, arbitrary URLs, generic services, or entity discovery. A token can have broader privileges than this client uses; choose its account and scripts carefully. Spoken stop can prevent an action before dispatch, but cannot undo a command already received by HA. See [Home Assistant controls](HOME_ASSISTANT.md).

## Contributions and publication

Run `python3 scripts/scan_public.py` and review `git diff --cached` before publishing. The scanner checks the Git candidate set for likely secrets, private addresses/paths, and prohibited artifacts without printing matched data. It is heuristic and does not cover ignored files or existing Git history. Start new public history from reviewed source; do not copy private deployment history.
