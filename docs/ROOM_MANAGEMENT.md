# Room management over a local Unix socket

Each enabled Linux resident serves HTTP/JSON on one Unix stream socket named
`<room_id>.sock`. A portal running under the same numeric UID can manage that
room's alarm clock, message light preferences and spoiler setting. The resident
checks the kernel peer UID on every connection. Every process running under
that UID with access to the socket has the same authority.

## Enable a room

The controller option `management_socket_dir` defaults to the empty string.
Set it to an absolute path in the room's local configuration, alongside the
existing `household_file`. The resident takes its room ID, alarm store and
timezone from that household setup. Room IDs match `[a-z][a-z0-9-]{0,23}`.

With the generated household configuration, use `/household/management` as
`management_socket_dir`. All residents then see the same directory through
their existing household mount. From the host checkout, prepare it as the
resident UID and query the configured office room after its next startup.

```bash
install -d -m 0700 ./state/household/management
curl --fail-with-body --unix-socket ./state/household/management/office.sock http://localhost/state
```

Mount just that management directory into the portal. The portal can connect
through a read-only directory mount. Keep each room's database and health
directory on the resident side. Use a trusted parent directory on a local
Linux filesystem. The resident creates a missing socket directory with mode
`0700`, verifies its owner and mode, and creates each socket with mode `0600`.
The absolute socket path inside the resident must fit in 107 bytes.
Startup recovers a stale socket left by a stopped resident. A live owner,
symlink or ordinary file at that path causes startup to fail.

## HTTP contract

| Request | Result |
| --- | --- |
| `GET /state` | `online`, `muted`, `version`, enabled `alarms` ordered by due time, and `preferences`. |
| `POST /alarms` | Calls the same room-local alarm handler used by voice. Returns its `message` and the updated `alarms` array. |
| `POST /preferences` | Accepts one or more preference fields, validates the whole change and returns the resulting `preferences` object. |

POST bodies are UTF-8 JSON objects, up to 4096 bytes, sent with
`Content-Type: application/json` and `Content-Length`. Duplicate and unknown
fields are rejected. The only writable resources are alarms and preferences.

A state response for an online room can look like this.

```json
{
  "online": true,
  "muted": false,
  "version": "0.14.0",
  "alarms": [],
  "preferences": {"brightness": 80, "color": "white", "spoilers": "ask"}
}
```

Health reflects the most recent resident sample. A connected sample must have
a boolean mute reading and a timestamp between now and ten seconds ago.
Disconnected, missing, invalid, expired or future samples report
`{"online":false,"muted":true,"version":"unknown"}`. In that case `muted:true`
is a conservative fallback. The API still returns saved schedules and
preferences while the resident is reconnecting. The native device must be
reachable and support alarms to begin ringing.

Alarm rows contain exactly `id`, `name`, `at`, `days`, `date`, `status` and
`due`. The due time is Unix seconds. Weekdays run from Monday (0) to Sunday
(6). For a one-off alarm, supply `at` with an optional ISO date, or a relative
`minutes` value from 1 to 1440. For a recurring alarm, supply a local 24-hour
`HH:MM` and distinct weekdays. The existing alarm handler determines the next
occurrence in the room's timezone.

```bash
room_socket=./state/household/management/office.sock
curl --fail-with-body --unix-socket "$room_socket" http://localhost/alarms \
  -H 'If-None-Match: *' --json '{"action":"set","name":"School","at":"07:00","days":[0,1,2,3,4],"date":null,"minutes":null}'
curl --fail-with-body --unix-socket "$room_socket" http://localhost/preferences \
  --json '{"brightness":50,"color":"cyan","spoilers":"avoid"}'
```

`set` replaces an existing same-name schedule. `cancel` removes a schedule.
`dismiss` ends a ringing occurrence and advances a recurring alarm to its next
day. `snooze` accepts 1–60 minutes and defaults to nine. A portal should send
the selected alarm's name for all edits. Omitted `days` defaults to `[]` and
omitted `at`, `date` and `minutes` default to null.

For create-only requests, use `If-None-Match: *`. For edits and cancellation,
send `If-Match` with the selected alarm's quoted ID, or the quoted revision
of its full row. A full-row revision also detects a transition to ringing
that retains the ID. For conditional preference changes, use the revision of
the complete preferences object. Compute revisions from the raw `/state`
objects before adding display fields.

```python
import hashlib
import json

def revision(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return '"' + hashlib.sha256(encoded).hexdigest() + '"'
```

A failed precondition returns HTTP 412. Refresh the room before resubmitting.
Without a precondition, writes apply to the current room state. HTTP 400
indicates invalid values. HTTP 409 indicates an alarm conflict, such as a
snooze after dismissal. All scheduling mutations use the resident's existing
`Alarms` instance so voice, native dismissal and portal edits share its lock.
Run one resident per room-local state directory.

| Preference | Accepted values | Default | Used by |
| --- | --- | --- | --- |
| `brightness` | 20, 50, 80 (integer percent) | 80 | Native message and weather lights |
| `color` | white, cyan, green, violet, amber | white | Native message lights |
| `spoilers` | ask, allow, avoid | ask | Spoken game-guide prompt |

Accepted preference changes replace `room-preferences.json` atomically in the
room's state directory with mode `0600`. The running resident sees the change
immediately. Weather keeps its hazard colours. The next indicator check sends
a change in effective brightness or message colour during an active light lease.

## Reusing the module

Copy `src/vector_puck/room_management.py` as a flat module. It uses the Python
standard library. Pass the existing room-local `Alarms` instance and its state
directory. The service owns its socket for the resident process lifetime,
across satellite reconnects.

```python
from room_management import make_room_api

api = make_room_api(state_dir=state_dir, alarms=alarms, room_id=room_id,
                    socket_dir=socket_dir, health_filename="health.json")
if api:
    api.start()
try:
    await run_resident()
finally:
    if api:
        api.close()
```

An empty or null `socket_dir` returns `None`. A custom `health_filename` must
be a single filename inside the room's state directory. Alternatively, pass
`health=callable`, a synchronous zero-argument reader returning the same
sample fields (`updated` in Unix seconds, `connected`, `muted`, and
`native_version` or `version`). The service calls it from its HTTP thread.
The public resident exposes the service as `dialogue.room_api`.
Read `api.preferences()["spoilers"]` for each voice turn. Apply
`decorate_indicator(indicator, api.preferences())` to the selected native
indicator before comparing its lease or sending it.
