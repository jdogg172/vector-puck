# Changelog

## Unreleased

- The listening ring requires a stable direction measured during speech. It gives a neutral cue when the device’s own audio or conflicting microphone readings make the direction ambiguous.
- During an answer, the wake detector still hears the original microphone signal. A second local recognizer uses the outgoing audio to reduce speaker echo and retains words spoken while it verifies the wake. A recovered factual question can receive a read-only answer using the room’s spoiler preference.

- On supported Echo rings, saying the wake phrase during a reply briefly shows a listening cue while the answer continues. An actual follow-up question takes the turn. An addressed stop still stops playback.
- Notification pulses now use the brightest of the three available hardware levels by default. Each room can choose a message color and brightness. Weather keeps its watch and warning colors.
- The local mobile portal shares the speaker’s alarm clock. Select a room to add a one-time or recurring alarm, edit or cancel it, or snooze or dismiss a ringing alarm. Room settings include message color, notification brightness and game spoilers. Caregiver pairing gives another phone access to Vector room controls.

- Open the Pokédex or the Hyrule field guide by voice for hints, quizzes and lore. Time-sensitive game questions use a read-only search of official publisher sites. Vector asks about spoilers by default and keeps household data out of game searches.

- Ordinary and household replies now share guidance for warm, everyday speech. Cedar's verbatim reading instructions call for playful timing for family jokes, gentle bedtime speech and plain, clear alerts.
- Sleep sounds get a short start, pause or resume reply. Durations omit empty units, and the one-minute and ten-hour thunderstorm acknowledgments keep their exact wording for cached speech.
- Thunderstorm sleep sessions can play a locally stored CC0 field recording with a crossfaded loop and bounded peaks. The player keeps audio packets full across the file boundary. “Play storm sounds for one minute” starts a short audition. Installation and recording credits are in [Kitchen](docs/KITCHEN.md#sleep-sounds).

### Weather and notification ring signals

- Orange watch, red warning, white unread-notification and yellow advisory pulses; flood watches alternate orange/blue, flood warnings red/blue, and flood advisories pulse blue. Green stays a brief verified-update success signal.
- Native flood sequencing preserves its phase across lease renewal and yields to microphone privacy and foreground call/listen/alarm patterns. Weather and notification lights expire locally if the controller disconnects.
- Local weather selection rejects stale, future and expired alerts, prioritizes warning over watch over advisory, and bounds the lease by feed validity and alert expiration. The portable runtime accepts a trusted local snapshot; it does not introduce a cloud weather account or automatic external requests.
- Spoken ring explanations reflect the current weather, notification, update or privacy state. Visual signals do not change the existing emergency-speech policy or dismiss official warnings.

### Kitchen and light-ring features

- Original local rain, thunderstorm, white-noise and brown-noise playback with a durable deadline, ten-hour default, bounded level and one-minute-to-twelve-hour limit. No long-form paid API audio or downloaded sound recordings.
- Background sound yields to voice, calls, messages, timers and alarms. Physical mute, Action and spoken stop pause it durably; restart/reconnect preserves only the original active deadline.
- Versioned saved AI-draft recipes, ingredient/serving scaling, persistent room-specific cooking steps and explicit additions to the shared shopping list. Scaling never multiplies temperatures/times or invents ingredient density.
- Local deterministic conversions, including unambiguous US cooking volume and temperature, with mass/fluid-ounce separation and density refusal.
- Private status-only delivery inbox foundation, with strict ingress, hashed event IDs, deduplication, expiry and dismissal. No retailer/email/account adapter is connected.
- RGB/rainbow application-update animation and an eight-second green completion pulse. Both controller and native LED timeouts prevent stale status. Mute, native calls/alarms, weather status and messages retain priority; software updates are never exposed as a voice tool.

### Fixes and improvements

- A released conversation turn now lets the same room accept its next wake immediately; the other room still suppresses a delayed duplicate.
- Normal-listener progress is monitored separately from network audio. An idle listener stuck waiting for a pipeline or retaining an obsolete cancellation state reconnects without unmuting.
- Stale queued microphone packets are discarded, and physical mute/cancel clears queued audio. Follow-up speech retains turn ownership while it is being captured.

- Both call participants now hear repeating local signalling: an incoming ringtone at the destination and quieter ringback at the caller.
- The local microphone/echo-reference stream stays open across the silent ring gaps so explicitly addressed answer and decline commands work while ringing, independently of the caller's conversation lease. Only the called room can accept.
- Accepted calls finish their ringtone with normal playback completion. They no longer send the native cancellation command that could immediately hang up the new connection.
- Caller-name prompts no longer speak a wake phrase or an answer command into the room.
- Calling is refused clearly when the required native microphone stream or spoken controls are disabled.
- Added family-friendly joke, riddle and story guidance for shared-room conversation.

These changes have passed isolated software and native-program tests. Physical incoming/ringback audibility, two-way acceptance, normal-seat wake/stop over ambience and overnight sleep-audio endurance remain operator checks; no hardware acceptance is declared from those tests alone.

## 0.3.0 — 2026-11-17

### Features

- Optional same-host household mode with owner-configured rooms, aliases and IANA timezone; disabled for existing single-room installations.
- One durable shopping list shared across rooms, with add/read/check-off/remove operations and atomic updates.
- Persistent named timers and reminders, room-local delivery, bounded tasks and playback acknowledgments.
- Persistent room-local alarm schedules: named one-off/weekday alarms, repeated snooze, nine-minute default snooze, and native ringing until dismissed.
- Single-responder election and renewable turn leases so overlapping Echos do not intentionally answer the same request.
- Explicitly accepted two-way local room calls, selected-room/all-room announcements, a repeating local incoming ringtone and optional cached Cedar caller prompts.
- A durable feature-idea queue and spoken daily status; requests are saved for owner review and never run code automatically.
- Echo 2 XC56PY/radar build and guarded data-override installation alongside Echo Dot 2 RS03QR/biscuit.
- Private multi-room setup helper, configuration schema, complete generated Compose configuration and recovery/acceptance guide.

### Fixes

- Relative alarms such as “set a test alarm for two minutes from now” use an exact elapsed delay instead of being rejected as a snooze duration.
- Incoming calls ring repeatedly until answered, declined or timed out; notification playback does not depend on an expired conversation lease.
- Announcement output uses a physical playback acknowledgment before marking delivery. Mute defers ordinary messages; Action/spoken stop dismisses the current output.
- Mute, Action, disconnect and spoken stop close call forwarding and invalidate stale work. Addressed “hang up”/“end call” join the local stop vocabulary.
- A fresh alarm occurrence ID prevents an older persisted native dismissal from silencing a later recurrence, snooze or same-name reset after reconnect.
- Native remote pipeline restarts preserve microphone mute; alarm dismissal is persisted on the satellite and reported on reconnect.
- Household worker failure forces a reconnect rather than leaving a healthy-looking listener with a dead notification worker.

### Boundaries and validation

- Household databases and call sockets require one local filesystem/host and one shared numeric UID. No NFS/SMB, cross-host coordinator, silent drop-in or authenticated speaker identity.
- Calls do not upload or record conversation PCM. Ordinary assistant requests and Cedar notification generation still use the owner's API account.
- Alarms need a reachable powered controller to start; native ringing can continue after a controller disconnect. No battery-backed guarantee.
- Existing optional Home Assistant controls retain their allowlist, dry-run default, bounded changes and independent intent gate.
- Generic automatic weather delivery, personal dashboard deployment, household credentials, media services, private recordings, firmware, model weights and signing keys are not included.
- Offline software, synthetic native-program and ARM build validation are documented in `docs/VALIDATION.md`. Physical shared-room acoustics, incoming call audibility, two-way calling and snooze acceptance remain operator checks.

## 0.2.0 — 2026-11-16

- Added local addressed spoken stop during working audio and Cedar replies, with independent transcription confirmation and cancellation gating.
- Added optional allowlisted Home Assistant receiver, thermostat and TV-script controls, disabled until configured and initially dry-run.
- Added bounded one-tool natural-language routing, sanitized device readback, cancellation and setup-recovery tests.

## 0.1.0

- Initial source-only Echo Dot 2 voice companion with local wake/recognition, GPT answers, Cedar replies, working tune and bounded follow-up conversations.
- Restricted controller container, checksum-pinned recognition models, guarded data-override installer, physical mute/Action control and rollback documentation.
