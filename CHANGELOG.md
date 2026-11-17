# Changelog

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
