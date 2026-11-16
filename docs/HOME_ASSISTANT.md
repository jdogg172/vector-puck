# Optional Home Assistant and TV control

The conversation baseline works without Home Assistant. This extension adds a small, owner-maintained allowlist for audio controls, thermostat targets, and TV scripts. It is disabled when the controller's `home_assistant_file` setting is empty. Start with `mode: "dry_run"`.

The model proposes one `home_action`; the local backend checks it against your configuration and fresh HA state. The controller speaks the returned message directly. No generic service, shell, entity-discovery, arbitrary URL, or TV-credential tool is exposed.

## Configuration and token

The complete example is [homeassistant.example.json](../config/homeassistant.example.json); [homeassistant.schema.json](../config/homeassistant.schema.json) describes its structure. The example uses documentation-only `192.0.2.20` and example entities.

Follow the optional setup instructions in the [main README](../README.md) and use [setup_home.py](../scripts/setup_home.py). The helper creates the ignored `config/homeassistant.local.json` mapping in dry-run mode, updates the main controller configuration, and records the external token's path without opening its contents. Review and replace the example entity IDs, aliases, sources, units and limits in that generated mapping.

If preparing the mapping manually instead of using the helper:

```bash
cp config/homeassistant.example.json config/homeassistant.local.json
chmod 600 config/homeassistant.local.json
```

Do not precreate the mapping before running the helper; it refuses to overwrite an existing one. [compose.homeassistant.yaml](../compose.homeassistant.yaml), used together with the base Compose file as described in the README, mounts this mapping at `/config/homeassistant.local.json` and the token at `/run/secrets/homeassistant_token`. The main `home_assistant_file` must point to `/config/homeassistant.local.json`; the HA config's `token_file` must point to `/run/secrets/homeassistant_token`.

Keep the actual long-lived HA access token in a regular file **outside the checkout**, owned by the numeric UID used by Compose, with mode `0600` or `0400`. Mount it read-only. The file contains the raw token on one line, without `Bearer` or an environment-variable assignment. Symlink token files, group/world permissions, other ownership, and malformed tokens are rejected. Setup can validate the configuration without opening the token; the runtime loads it when the optional integration starts. No token is sent to OpenAI.

`base_url` must be a numeric RFC 1918 private IPv4 HTTP(S) origin, optionally with a port. DNS names, credentials in URLs, paths, queries, fragments, loopback, link-local metadata addresses, IPv6, and public destinations are rejected. The TEST-NET example is accepted for configuration review but cannot make production requests. HTTP sends the bearer token unencrypted on your LAN; use an appropriate isolated network or HTTPS with a trusted certificate matching the IP address. TLS verification stays enabled, environment proxies are ignored, and redirects are not followed.

## Explicit allowlists

All aliases use lowercase letters, digits, and underscores and start with a letter. Target aliases are unique across categories. Entity IDs must belong to the correct domain and are never taken from a model argument. Omit unused categories or leave them empty. At least one target is required; the maximum is 32 targets, with at most 16 per category. Unknown and duplicate JSON keys are rejected.

| Category | Local configuration | Allowed actions |
|---|---|---|
| `media_players` | One `media_player.*` entity, short public label, `max_volume`, `volume_step`, and source alias map | `status`, `volume_down`, `volume_up`, `volume_set`, `mute`, `unmute`, `source` |
| `thermostats` | One `climate.*` entity, label, F/C unit, minimum, maximum, maximum change per command | `status`, `cool_set`, `heat_set`, `cool_adjust`, `heat_adjust` |
| `tv_actions` | One `script.*` entity, short public label and description | `status`, `run` |

Aliases, labels, script descriptions, source **aliases**, and thermostat units/limits are included in the model's tool definition. Choose these as information you are willing to send to OpenAI. Keep addresses, people, accounts, precise locations, and private details out of them. The HA origin, token, entity IDs, actual HA source names, and raw state attributes stay local.

A TV alias invokes an owner-written HA script through `script.turn_on`, such as a reviewed dashboard, channel, or application launcher. This package stores no direct LG, Jellyfin, receiver, or application credentials. Review the script's actual actions: an alias is not a security review of the script, and this client allowlist does not restrict what other clients can do with the token. Use a dedicated HA user with only the privileges your deployment requires.

## Value and state rules

Every proposal has exactly these fields, all required:

```json
{
  "target": "audio",
  "action": "volume_down",
  "value": null,
  "option": null
}
```

- Volumes use HA's normalized **0..1** scale: `0.25` is 25 percent. `volume_set` requires a number. Up/down use the configured `volume_step` when value is null; an explicit increment must be positive and no larger than that step. A target above `max_volume` or below zero is rejected rather than silently clamped. The step cannot exceed `0.2`.
- `source` requires an allowlisted source alias in `option` and null `value`. Its mapped source must also appear in the selected entity's current `source_list`. Other actions require null `option`.
- Temperature `*_set` actions require an absolute target in the configured unit; `*_adjust` actions require a signed delta. Both absolute and relative changes obey `max_delta` against the current target, configured minimum/maximum, and the device's reported `min_temp`/`max_temp`.
- `heat_cool` mode requires two valid targets. Changing one preserves and resubmits the other, and the lower target must remain strictly below the upper target. Both targets must remain inside both sets of bounds. Single-target heat/cool modes accept only the matching heat/cool action. No action changes HVAC mode, power, schedules, presets, or fan mode.
- Temperature writes require `temperature_unit` or `unit_of_measurement` on the selected climate state to explicitly report `F`, `C`, `°F`, or `°C` matching configuration. Conflicting or missing unit metadata rejects writes. Many integrations do not expose this metadata. In that case, status works but this backend does not change temperature; configuring `unit` alone is insufficient. An operator may expose a verified unit through HA customization after checking HA's actual displayed/service unit. Never add a guessed unit merely to bypass this check.
- Mute, unmute, status, and script run require null `value`. Unavailable, unknown, or missing required state rejects writes. A script already running is not invoked again.

Natural requests such as “could you make the audio a little quieter?” or “bring up the dashboard please” can become bounded proposals. Negation, hypothetical/explanatory wording, and unclear write intent are rejected conservatively. This is a safety filter rather than a complete understanding of every English sentence; rephrase an incorrectly rejected command directly.

## Outcomes and cancellation

`dry_run` performs the selected state read and all applicable validation, and reports that **no command was sent**. It never calls a service. `status` reads only the selected entity; it never lists the installation's other entities.

A control write calls exactly one fixed service, with the configured entity ID. The guard is checked after reading state and immediately before dispatch. Cancellation before dispatch prevents a write. Once HA may have received a request, stopping speech cannot undo its effects; the backend never retries automatically.

Results distinguish:

| Result | Meaning |
|---|---|
| `rejected` / `unavailable` | Validation or the initial state read prevented dispatch. |
| `dry_run` | Proposal validated; no command sent. |
| `observed`, `submitted: false` | Status was read, or HA already reported the requested setting and no write was needed. |
| `observed`, `submitted: true` | HA accepted the service call and the selected entity's immediate readback matches. This is HA-reported state, not independent physical proof. |
| `submitted`, `observed: false` | HA acknowledged the call, but readback failed, differed, or cannot establish the requested outcome. |
| `unverified`, `submitted: null` | Dispatch encountered an error or timeout; whether a change happened is unknown. Check HA before repeating it. |

TV script calls always remain `submitted`, never verified screen success. A script's on/off state cannot prove that an app opened or a channel appeared. Immediate readback may precede an integration's eventual update, and external HA changes can race the read/write sequence. There is no transactional compare-and-set across devices.

Requests have finite connect/read timeouts plus an eight-second total bound per REST operation. Responses are bounded to 64 KiB; service response attributes are discarded. Remote error bodies, entity IDs, URLs, and token values are not included in tool results or spoken messages.

## Developer interface and offline checks

```python
from pathlib import Path
from vector_puck.home_control import HomeControl

# Configuration-only validation: no token read and no network access.
config = HomeControl.read_config(Path("private/homeassistant.json"))
HomeControl.validate_config(config)

# Runtime loading reads only the explicitly configured external private token.
home = HomeControl.load(Path("private/homeassistant.json"))
# home.tools() -> strict Responses function definitions
# await home.execute("home_action", args, guard, utterance=recognized_text) -> dict
```

`HomeControl(config, token, client_factory=...)` supports an injected async client factory for offline testing. Tests use synthetic tokens and mocked responses, including one `httpx.MockTransport` integration check when httpx is installed. Run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_home_control.py' -v
```

Test success does not establish that your HA integration or TV script controls a physical device correctly. Review dry-run results and verify each enabled action on your own hardware before relying on it. No private installation or hardware was contacted while developing these tests.

The fixed REST paths and service fields follow the official [HA REST API](https://contributors.home-assistant.io/core/internals/rest-api/), [media player actions](https://www.home-assistant.io/integrations/media_player/), and [climate actions](https://www.home-assistant.io/integrations/climate/) documentation.
