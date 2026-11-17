# Optional household features

Version 0.3 adds shared shopping lists, persistent named timers/reminders, room-local alarm clocks, single-responder coordination, accepted room calls, announcements and saved feature ideas. Nothing is enabled until the owner configures `household_file`. Existing single-room installations continue to work.

## Supported hardware and host

- Echo Dot 2, RS03QR, `biscuit_puffin`, upstream pinned build `NS65741`.
- Echo 2, XC56PY, `radar_puffin`, upstream pinned build `NS6572`.

Both must already be rooted and running the pinned upstream satellite. This project does not unlock devices or flash firmware. For radar, follow the [upstream model guide](https://github.com/Gamer92000/echo-dot-assist/blob/8646a4f2f4aeafde2d7aca7359d9eff555a169c0/devices/radar/README.md). It requires soldered USB data access for initial work; a normal USB socket is absent. Do not apply these instructions to other Echo generations or Shows.

Run one controller per Echo, all on **one Linux host**, using the same numeric UID/GID and the same local shared directory. SQLite transactions and private Unix datagram sockets coordinate them. NFS, SMB, multiple NAS hosts, and independently copied household databases are unsupported. Budget about 2 GB RAM and recognition CPU capacity per room. Ordinary conversation still uses the owner's OpenAI account and request caps.

## Prepare the native programs

Build and install one connected USB device at a time. The installer checks model, pinned build, root and the existing upstream installation before making any changes.

```bash
VECTOR_DEVICE=biscuit bash scripts/prepare_puck.sh
VECTOR_DEVICE=biscuit bash scripts/install_puck.sh --check
VECTOR_DEVICE=biscuit bash scripts/install_puck.sh --apply
```

For the full-size Echo 2:

```bash
VECTOR_DEVICE=radar bash scripts/prepare_puck.sh
VECTOR_DEVICE=radar bash scripts/install_puck.sh --check
VECTOR_DEVICE=radar bash scripts/install_puck.sh --apply
```

Use the NDK setup in the main README first. Builds use the model's upstream ABI stubs; stock libraries remain on the Echo. No proprietary libraries, firmware images, signing keys or compiled programs are included here.

An older `.hardware/` checkout with the 0.2 patch is deliberately rejected rather than overwritten. Preserve/move `.hardware/echo-dot-assist` and `.hardware/patched-commit` together before rebuilding. Keep existing device backups for rollback.

The updated patch advertises `vector_alarm` and `vector_indicator`. Alarms cannot be set through this controller until the satellite reports native alarm support. The physical mute state is read during connection, never automatically cleared.

## Prepare private room configuration

Complete the main README's key, model and `.env` setup first. The following prompts collect your actual addresses and timezone without publishing them:

```bash
read -r -p 'Office Echo local IPv4 address: ' office_ip
read -r -p 'Kitchen Echo local IPv4 address: ' kitchen_ip
read -r -p 'IANA timezone (for example Europe/London): ' household_timezone
python3 scripts/setup_household.py \
  --room "office:$office_ip:biscuit" \
  --room "kitchen:$kitchen_ip:radar" \
  --timezone "$household_timezone"
```

Room IDs are configurable. The example generates ignored, private files:

- `config/office.local.json` and `config/kitchen.local.json`: per-room controller settings.
- Corresponding `*.household.local.json` files: the same directory of room IDs/names/aliases, each resident's own ID, timezone and `/household` root.
- `compose.household.local.json`: a complete restricted Compose configuration with separate state volumes and one shared volume.
- `state/rooms/office`, `state/rooms/kitchen`, `state/household`: private local state directories.

Edit each household file to set preferred display names/aliases; keep the directory and timezone consistent. Review [the household schema](../config/household.schema.json) and [example](../config/household.example.json). Relative alarms mean elapsed minutes; absolute schedules and reminders use your configured IANA timezone. Reminders require a valid offset for that local date. Nonexistent spring-forward alarm times are skipped; repeated fall-back times use their first occurrence.

If migrating existing state, stop its controller, back up `state/` and private configuration, then **copy its existing room-local files into the matching room directory before starting**. A fresh shared shopping database starts empty; this setup does not automatically merge pre-existing local lists. Do not run two controllers against the same Echo or room-local state.

```bash
docker compose stop vector
docker compose -f compose.household.local.json build
docker compose -f compose.household.local.json run --rm --no-deps vector-office --check-config
docker compose -f compose.household.local.json run --rm --no-deps vector-kitchen --check-config
docker compose -f compose.household.local.json up -d
docker compose -f compose.household.local.json ps
```

If combining with optional Home Assistant, add the private HA mapping and token mounts from its documented Compose override **to each desired room service**, then set that room's `home_assistant_file`. Home controls remain separate, explicitly allowlisted and initially dry-run. The base `compose.homeassistant.yaml` targets the single-room service and is not automatically merged into these new room services.

## Using it

| Request | Behavior |
|---|---|
| “Add milk and eggs to our shopping list.” | One durable household list, read/check off/remove from either room. No purchasing. |
| “Set a pasta timer for ten minutes.” / “Give the pasta timer two more minutes.” | Named room-local timers; ambiguous names prompt a question. |
| “Remind me in half an hour to check the oven.” | Durable reminder, delivered in its originating room when available. |
| “Set an alarm called test for two minutes from now.” | One-off alarm with an exact elapsed delay. |
| “Wake me at seven AM on weekdays.” | Room-local recurring alarm; ambiguous AM/PM requires clarification. |
| “Snooze for one minute.” / “Alarm off.” | Repeat snooze, nine minutes by default. Off dismisses today's recurring alarm; cancel removes its schedule. |
| “Call Kitchen.” / “Answer the call.” / “Hey Vector, hang up.” | Explicit acceptance, up to ten minutes, one household call at a time. |
| “Tell Kitchen that dinner is ready.” / “Announce everywhere that dinner is ready.” | Queued announcements; muted/busy rooms defer for up to five minutes. Queued does not mean heard. |
| “Save a feature request to add a new integration.” / “Give me my daily update.” | Persistent owner-review queue and status; **no automatic installation or code execution**. |

When both rooms hear a wake phrase, a short election chooses one eligible room by observed microphone level, with a deterministic tie-break. It is a practical arbitration heuristic, not sound-source localization or speaker identification. The winner retains the lease during its bounded follow-up conversation. Physical room acoustics still need testing.

Incoming calls repeat an original local ringtone every six seconds until answered, declined or timed out after 45 seconds. Ringing never opens a microphone bridge. Optional caller-name speech can be prepared once while the room services are stopped:

```bash
docker compose -f compose.household.local.json stop
docker compose -f compose.household.local.json run --rm --no-deps vector-office --cache-call-prompts
docker compose -f compose.household.local.json up -d
```

This explicit step uses billed Cedar generation for static room-name prompts, stores them privately, and plays nothing. A local ringtone works even when no prompt is cached or OpenAI is unavailable. Retain or remove old `state/household/call-prompts` files deliberately before regenerating. The cache is not a microphone recording.

## Mute, stop and persistence

- Physical microphone mute blocks new requests and call forwarding, ends an active call, and defers ordinary notifications. Red remains authoritative. Action and addressed spoken stop cancel queued replies and end calls.
- Accepted call PCM stays on the local host via permission-restricted Unix sockets, with call IDs, sequence checks, freshness bounds and no recording. Stop recognition is local; the call stream is not uploaded for conversation. Use room separation to limit acoustic feedback.
- Once a native alarm has started, its local ring continues until dismissed, even if the controller disconnects. An alarm is intentionally audible while the microphone is muted; mute never reopens the microphone. Action stops it; speaking requires unmuting. **A powered/reachable host and Echo are required to start scheduled alarms. This is not a battery-backed or standalone alarm clock.**
- Schedules, snoozes, ringing state, shopping changes and queued requests survive controller restarts. Calls fail closed on disconnect/mute/lease loss; stale speech is not replayed. Timers/reminders wait while muted or occupied and require cloud voice for their spoken notification.
- Feature requests remain untrusted ideas. A spoken name or room ID is not authorization for purchases, account changes, firmware changes, or an autonomous worker. No execution worker is included in the public package.
- Automatic severe-weather delivery and the operator's existing family dashboard are not part of this generic integration. `public/status.json` is a deliberately limited feature-status projection for owners wishing to integrate their own display; do not expose the shared directory or databases.

## Acceptance and rollback

Test when every participating room is free. Confirm shared items from both rooms, only one reply to a shared wake, a real incoming ring, explicit call answer/two-way audio/hang-up, mic-mute cutoff, Action/spoken stop during speech, a two-minute alarm with repeated snooze/off, and behavior after controller/device restarts. Synthetic tests do not establish acoustic acceptance.

Back up the entire private shared directory and each room's state while stopped. To roll back, stop all household controllers, restore the previous image/config/native program as needed, and restart only the intended single controller per device. Preserve current lists/schedules rather than blindly restoring an older empty database. See the [hardware rollback](HARDWARE.md#rollback) and [validation guide](VALIDATION.md).
