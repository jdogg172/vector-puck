# Privacy and network boundaries

## Data flow

1. The puck sends 16 kHz mono microphone audio across your LAN to the controller.
2. Vosk detects the wake phrase locally. Whisper transcribes locally. No microphone-upload API is implemented here.
3. For a model-generated answer, recognized request text and bounded recent text context go to OpenAI with Responses `store:false`. The game guide selects topical history as described below.
4. Generated answer text goes to OpenAI Realtime for Cedar speech. The returned audio plays on the puck.
5. With optional home control enabled, the Responses request also contains configured target aliases, labels, script descriptions, source aliases, and thermostat units/limits. HA URLs, tokens, entity IDs and raw attributes stay local. Selected sanitized results are spoken directly through Cedar, so those spoken readings/results also go to OpenAI's voice API.

`store:false` is a request setting, not a claim of zero provider retention. Review your OpenAI project's data controls and applicable service terms. This is an AI-generated voice; people using the device should know that.

Raw audio is held in bounded memory and is not written to recordings by this controller. Conversation history stays in memory, holds at most six messages, and expires after 20 minutes by default. Mute, cancellation, and disconnect clear it. Health and quota files persist operational counters/state, not transcripts. Logs contain enumerated event names without household requests, API response bodies, or keys.

During output, Vosk and Whisper process a separate, bounded microphone stream locally. An addressed stop cancels playback locally. On a focus-capable satellite, a wake phrase alone can light the ring while Cedar keeps speaking. If the listener recognizes the start of a new question, it passes the opening audio and continuation to the normal local listener. Once the complete transcription passes the handoff checks, its **text can go to OpenAI or enter ordinary configured tool routing**, just like a fresh request. The microphone audio stays local. Physical mute and Action cancel pending capture. Health exposes capability, listener state and numeric counters. Physical ring direction and overlapping-speech recognition are experimental. [Spoken stop and focus](SPOKEN_STOP.md)

### Zelda and Pokémon lookup

For a recognized game question or topical follow-up, the Responses request includes the current utterance, selected recent game conversation and the room's spoiler instruction (`ask` by default). Routing filters prior turns by topic. Any personal detail spoken as part of the current game question is still part of that text. This request omits the household directory, home-device mappings and cooking context. Its only tool is hosted, read-only web search, limited to two tool calls.

Search is restricted to `nintendo.com`, `nintendo.co.jp`, `pokemon.com`, `pokemon.co.jp`, `pokemongolive.com` and `pokemontcgpocket.com`, including their subdomains. Recognized time-sensitive questions such as current Mystery Gifts require search. Before speaking a searched answer, the controller checks for a search call and HTTPS citations on the allowed domains. If that check fails, it says it could not verify the information. Game lookup has no account, redemption, device or household action tools.

The upstream device program still uses stock Amazon firmware and proprietary audio components. Complete the upstream firewall/service lockdown; this source patch alone is not proof that every stock service is contained. Do not register or reconnect stock Alexa services as part of this setup.

## Credentials and limits

Keep your dedicated key outside the checkout in an operator-owned 0600/0400 regular file. Local setup stores its path, not its contents. Compose mounts it read-only at `/run/secrets/openai_key` and uses the operator UID/GID. Do not commit `.env`, local config, state, recordings, model weights, firmware, or toolchains.

Optional HA setup uses a second external private token file and an ignored local mapping. Its Compose override mounts both read-only. Setup backups contain private addresses/entity mappings and file paths, and remain in ignored `.cache/`; retain or delete them according to your own needs. Never publish these files. HA access over HTTP sends a bearer token without encryption on the LAN; use network isolation or HTTPS with a trusted matching certificate. TLS verification is not disabled.

Default quotas are 30 request attempts/hour and 100/day, including failures. Hourly timestamps and the UTC daily count survive restarts. One spoken request can use answer generation, hosted game search and Cedar speech. The quotas count request attempts. Use your provider project's billing controls for cost limits.

## LAN trust

Wyoming port 16700 on the puck is unencrypted and unauthenticated. Run it on a trusted or segmented LAN, preserve upstream firewall rules, and permit only your controller where practical. Never forward it to the Internet. Device isolation is an operator networking task; this kit does not change your router.

The Docker controller exposes no inbound port, runs as a non-root UID with a read-only root filesystem, drops capabilities, and uses a bounded temporary filesystem. It requires outbound access to the puck and OpenAI. Model downloads happen explicitly before runtime; Hugging Face/Transformers automatic downloads and telemetry are disabled in the image.

Home Assistant device access requires an owner-configured mapping. Its bounded `home_action` function permits selected media controls, thermostat targets and fixed HA scripts, with fresh state validation, one dispatch per request and no automatic retries. The separate game lookup cannot invoke this function. The HA token may carry broader privileges than the controller uses. A stop after dispatch cannot undo a command already received by HA. [Home Assistant controls](HOME_ASSISTANT.md)

## Optional family portal

The portal is a separate browser service. It defaults to loopback on port 17868. For household phones, use a LAN-only HTTPS endpoint and HTTPS for Home Assistant sign-in. Plain HTTP exposes bearer cookies or sign-in traffic to anyone able to observe that link. Exclude callback query strings, cookies and request bodies from proxy logs.

HA owners, admins and explicitly allowlisted HA users can manage the configured rooms. Only the HA owner can issue a single-use, five-minute pairing code for a caregiver phone. A paired phone can manage **every configured room** for 30 days unless revoked earlier. Pairing grants portal access without creating an HA account. The portal keeps HA tokens and short sessions in memory. Its private SQLite database persists phone labels, grant IDs, issue and expiry times, and credential hashes. Owner revocation takes effect on the phone's next protected request.

The resident management API is disabled by default. When enabled, each resident exposes a mode-`0600` Unix socket in a mode-`0700` directory and checks the connecting process's numeric UID. The browser authenticates to the portal. The resident trusts any same-UID process with socket access. Mount only the dedicated socket directory into the portal. The API exposes room health, enabled alarm names and schedules, and brightness, color and spoiler preferences. It accepts alarm and preference changes. Physical mute is read-only here. This interface provides no microphone stream, transcripts, HA controls or shell access. [Portal access and revocation](PORTAL.md) · [Room API and health freshness](ROOM_MANAGEMENT.md)

## Contributions and publication

Run `python3 scripts/scan_public.py` and review `git diff --cached` before publishing. The scanner checks the Git candidate set for likely secrets, private addresses/paths, and prohibited artifacts without printing matched data. It is heuristic and does not cover ignored files or existing Git history. Start new public history from reviewed source; do not copy private deployment history.


## Optional household mode

Enabling `household_file` shares room registration, response leases, shopping items, announcement text, call state and saved feature ideas through one private local directory. Room-local schedules and reminders remain in each controller's state. Room names are not speaker identity. Accepted call PCM uses private Unix datagrams, sequence/freshness checks and no recording or cloud conversation upload. Only a recipient's explicit answer opens a call. Mute or Action ends forwarding.

Optional caller prompts are static synthesized speech stored under the private shared directory, not microphone captures. Spoken notifications require Cedar; the incoming ringtone and already-started native alarm do not. Schedules and shopping contents are not added to unrelated model requests; current room/time and the registered room names are supplied for household routing. Feature-idea details are saved locally and never automatically executed. The limited `public/status.json` projection contains feature summaries/status/notes; publish it only if the owner wants those displayed.
