# Kitchen essentials and update lights

These features use the existing voice connection. They do not require a new daily-use portal or another music subscription. The controller's writable state directory must be backed up; household mode also needs its shared directory.

## Sleep sounds

Try:

- “Hey Vector, play thunderstorms for ten hours.”
- “Hey Vector, play brown noise for an hour.”
- “Hey Vector, pause the sleep sounds.”
- “Hey Vector, resume the sleep sounds.”
- “Hey Vector, stop the sleep sounds.”

The thunderstorm uses a local field recording when installed. Rain, white noise, brown noise and the fallback storm are synthesized locally. The default sleep session lasts ten hours, with a one-minute minimum and twelve-hour maximum. The starting audio level is 30 on the 5–60 scale.

To install the field recording, run the following on the Compose host from the repository root. Use a Python environment with NumPy and put FFmpeg on the executable path. The two standard room services share the same read-only models folder. Run the preparation before starting the services, or restart them when idle if they have already cached a sound.

```bash
set -euo pipefail
mkdir -p models/ambient
curl --fail --location --retry 2 --output models/ambient/baltic-storm.mp3 \
  https://cdn.freesound.org/previews/870/870414_801011-hq.mp3
printf '%s  %s\n' 29ba38de1cfadfd4bca6f4d7aa4a9877745ad4d370840b4873838cbd4922569b models/ambient/baltic-storm.mp3 | sha256sum --check -
python3 scripts/prepare_storm.py models/ambient/baltic-storm.mp3 models/ambient/thunderstorm.wav --start-seconds 50
```

Borgory recorded the 27-minute storm on the Baltic coast and dedicated it to the public domain under CC0. The recipe uses the publisher’s high-quality MP3 preview. It converts the recording to mono PCM, limits peaks, overlaps both ends for eight seconds, and starts near an early thunder roll. Playback reads the WAV locally, including on subsequent nights. [Source and credit](https://freesound.org/people/Borgory/sounds/870414/) · [CC0 dedication](https://creativecommons.org/publicdomain/zero/1.0/)


The playback deadline and selection survive controller restarts. Reconnecting resumes an active session only until its original deadline. Wake requests pause the background stream while Vector answers. Calls, messages, alarms and due timers take priority. Physical mute, Action and spoken stop pause it durably; a new explicit resume/start is needed. Unmuting alone does not restart stopped sleep audio.

The player fills each speaker packet across the end of the WAV, with one continuous audio stream. Use “play storm sounds for one minute” to audition the sound and “play storm sounds for ten hours” for bedtime.

## Saved cooking

Try:

- “Hey Vector, create and save a pancake recipe for four people.”
- “Hey Vector, what recipes have we saved?”
- “Hey Vector, start the pancake recipe.”
- “Hey Vector, read the ingredients.”
- “Hey Vector, next step.”
- “Hey Vector, repeat that step.”
- “Hey Vector, scale this recipe to six servings.”
- “Hey Vector, add this recipe's ingredients to our shopping list.”

Generated recipes are explicitly AI drafts. State dietary restrictions when creating them; the app does not infer an authenticated cook or certify an allergy-safe kitchen. Ordinary cooking questions remain conversational. Generated recipes are not represented as imported, tested, or checked against an external recipe source.

Named recipes and their versions live in a private SQLite database. Household mode shares the recipe book and shopping list, while each room keeps its own selected recipe, version, serving count and step. A saved revision does not silently change an active cooking session. Progress survives a restart.

Scaling changes ingredient amounts only. It does not multiply oven temperatures or cooking times, operate appliances, or infer cup-to-gram density. A recipe can contain up to 40 ingredients and 30 steps. Shopping additions are limited to ten ingredients per request; larger additions are refused without a partial mutation.

## Conversions

Simple conversions run locally, for example “convert 180 Celsius to Fahrenheit” or “how many teaspoons are in two tablespoons?” US cooking volume, mass, length, temperature and other supported unit families use deterministic decimal calculations. Mass ounces and fluid ounces remain separate. Cups cannot become grams without ingredient density; Vector should ask rather than invent it. Currency rates and live weather sources are outside this portable module.

## Delivery notices and privacy

A small status-only inbox can hold “out for delivery,” “delivered” or “delayed.” Its ingress accepts only an opaque event ID, status and expiry. It rejects product descriptions and other extra fields rather than hiding them only at speech time. IDs are hashed, repeated events are deduplicated, notices expire, and dismissal does not relight the same event.

**No retailer, email or delivery account is connected by this feature.** An owner must supply a separate reviewed adapter that emits status-only events. It does not scrape a shopping account or reveal gift contents. Voice can read/dismiss notices and explain ring states. Clearing delivery notices never dismisses a weather warning.

## RGB update status

The source patch adds `vector_update_indicator` to the native capability report on the two supported LED-ring models. A controller with an older satellite simply does not send the new animation.

| State | Ring behavior |
|---|---|
| Application update in progress | Dim rotating RGB/rainbow cycle |
| Updater verifies the application and controller have reconnected | Brief green pulse, eight seconds |
| Update stalls | Controller marker expires; native timeout also stops the animation without another network message |
| Physical microphone mute | Red privacy indicator wins; RGB never unmutes the microphone |
| Call or alarm | Native foreground animation wins |
| Weather warning/watch/advisory or pending message | Actionable status takes precedence over the update indicator |

Status is visual only; no unsolicited completion speech plays. It is not a percentage/progress meter. A service restart can briefly show the device's normal disconnected/startup animation. Green is requested only by the trusted updater after version, connection and mute readback, never simply because a timer elapsed. Mute may hide the green pulse too.

The local updater API is `UpdateIndicator(state_dir).begin(seconds)`, `complete(update_id)` and `clear()`. An expired or mismatched update ID cannot complete. There is no voice tool for software installation, arbitrary commands, or self-modification.

To check the visual behavior without installing software, while the speaker is otherwise idle and physically unmuted, the standard single-room Compose service supports:

```bash
update_id=$(docker compose exec -T vector python -m vector_puck.update_indicator begin --state /state --seconds 30)
sleep 5
docker compose exec -T vector python -m vector_puck.update_indicator complete --state /state --id "$update_id"
```

This is a labeled visual demonstration; `complete` here simulates an updater's success signal and does not install or verify a release. For household deployments, use the generated room service name and its own state directory. A real updater must verify the signed application, intended model, reconnect and unchanged mute state before calling `complete`. Failed updates call `clear`; they never signal success. Public installation still uses the documented guarded USB override; this module does not expose or enable a network firmware flasher or distribute signing keys.

## Weather and notification signals

| Status | Background ring signal |
|---|---|
| Weather watch | Pulsing orange |
| Weather warning | Pulsing red |
| Flood watch | Alternating orange and blue pulses |
| Flood warning | Alternating red and blue pulses |
| Flood advisory | Pulsing blue |
| Other weather advisory | Pulsing yellow |
| Unread room messages or status-only delivery notices | Pulsing white |
| Verified update completed | Brief green pulse |
| Physical microphone mute | The existing steady red privacy indication; it covers background status |

Flood sequences alternate complete 2.4-second pulses on the satellite, rather than issuing a network request for every colour. Lease renewals preserve the sequence phase. These are gentle pulses, not rapid flashes. Weather and notices do not play unsolicited audio through this visual feature. Any separately configured emergency-speech policy remains independent.

Weather warnings outrank watches, which outrank advisories. Tornado, severe thunderstorm, then flood break ties within the same severity. The chosen weather state wins over ordinary notifications and update status. Native listening, speaking, calls, alarms and microphone privacy retain their foreground priority. White clears after messages are consumed/dismissed/expired; acknowledging a delivery never dismisses weather.

The portable kit does **not** fetch weather automatically. A trusted local adapter supplies `RoomRuntime.set_weather_snapshot(snapshot)`; no arbitrary URL or account access is added. The snapshot uses `available: true`, `stale: false`, a Unix `valid_until`, and `data.items` containing public `event`, Unix `effective_at` and `expires_at` values. Only current, fresh watch/warning/advisory events are shown. The household integration uses its existing local cached National Weather Service feed.

Each native weather/notification lease is at most 60 seconds and is renewed while its source remains valid. Weather validity is bounded by both the feed deadline and the selected alert's expiry. Stale/unavailable data clears the weather light; an unlit ring does **not** certify that conditions are safe. A disconnected controller cannot leave a persistent old warning behind. Older satellites without `vector_weather_indicator` fall back to generic severity colours and cannot provide the new native lease/sequence guarantees until patched.

Ask “Hey Vector, why is your ring on?” for the current status explanation. Green is reserved for short verified success; unread messages never use it. No real or simulated emergency broadcast is triggered by installing this feature.
