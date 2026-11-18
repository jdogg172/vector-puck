# Kitchen essentials and update lights

These features use the existing voice connection. They do not require a new daily-use portal or another music subscription. The controller's writable state directory must be backed up; household mode also needs its shared directory.

## Sleep sounds

Try:

- “Hey Vector, play thunderstorms for ten hours.”
- “Hey Vector, play brown noise for an hour.”
- “Hey Vector, pause the sleep sounds.”
- “Hey Vector, resume the sleep sounds.”
- “Hey Vector, stop the sleep sounds.”

Rain, thunderstorm, white noise and brown noise are original locally synthesized loops. Playback stays local and does not generate ten hours of API speech or download a recording. Duration defaults to ten hours, with a one-minute minimum and twelve-hour maximum. The starting digital audio level is 30 on a bounded 5–60 scale; device volume is independent.

The playback deadline and selection survive controller restarts. Reconnecting resumes an active session only until its original deadline. Wake requests pause the background stream while Vector answers. Calls, messages, alarms and due timers take priority. Physical mute, Action and spoken stop pause it durably; a new explicit resume/start is needed. Unmuting alone does not restart stopped sleep audio.

The loop is seamless in generated PCM and starts/ends with short fades. Speaker acoustics, overnight stability, loudness, normal-seat wake recognition and spoken stop over the sound require physical testing on each installation. It is a synthesized ambience, not a field recording of a real storm.

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
