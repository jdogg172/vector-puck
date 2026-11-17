"""Same-host household coordination. No network listeners or stored microphone audio."""

from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid

from .kitchen import Kitchen, clean


class SharedKitchen:
    """Keep existing local timers; route every shopping operation to one database."""

    def __init__(self, local: Kitchen, path: Path):
        self.local = local
        self.shared = Kitchen(path)

    def __getattr__(self, name):
        return getattr(self.local, name)

    def items(self):
        return self.shared.items()

    def shopping(self, args, guard=lambda: None):
        return self.shared.shopping(args, guard)

    def handle(self, name, args, guard=lambda: None):
        return (self.shared if name == "shopping_list" else self.local).handle(name, args, guard)


def validate_rooms(room, rooms):
    if not isinstance(rooms, list) or not 2 <= len(rooms) <= 16 or not isinstance(room, str):
        raise ValueError("Invalid room directory")
    names = {}
    aliases = {}
    for entry in rooms:
        if set(entry) != {"id", "name", "aliases"}:
            raise ValueError("Invalid room directory")
        key = entry["id"]
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", key):
            raise ValueError("Invalid room id")
        name = clean(entry["name"], 40)
        if key in names or not isinstance(entry["aliases"], list) or len(entry["aliases"]) > 12:
            raise ValueError("Invalid room directory")
        names[key] = name
        for alias in [key.replace("-", " "), name, *entry["aliases"]]:
            alias = clean(alias, 60).casefold()
            if alias in aliases and aliases[alias] != key:
                raise ValueError("Ambiguous room alias")
            aliases[alias] = key
    if not 2 <= len(names) <= 16 or room not in names:
        raise ValueError("Unknown resident room")
    return names, aliases


class Household:
    """Transactional coordination shared by residents on one local NAS filesystem.

    Wall-clock leases are safe here because both residents run on the same host.
    The private bind mount is the access boundary; never put this database on NFS
    or expose a writable copy to an untrusted container.
    """

    def __init__(self, root: Path, room: str, rooms: list[dict], clock=time.time):
        self.root, self.room, self.clock = Path(root), room, clock
        self.rooms, self.aliases = validate_rooms(room, rooms)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("Household directory cannot be a symlink")
        self.root.chmod(0o700)
        self.path = self.root / "household.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.lock = threading.RLock()
        self.token = None
        self.valid_until = 0.0
        with self.tx() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS rooms(id TEXT PRIMARY KEY,name TEXT,seen REAL,muted INTEGER,busy INTEGER);
              CREATE TABLE IF NOT EXISTS turn(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT,token TEXT,until REAL);
              CREATE TABLE IF NOT EXISTS election(id INTEGER PRIMARY KEY CHECK(id=1),token TEXT,deadline REAL);
              CREATE TABLE IF NOT EXISTS candidates(election TEXT,room TEXT,quality REAL,PRIMARY KEY(election,room));
              CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,sender TEXT,text TEXT,created REAL,expires REAL,urgent INTEGER DEFAULT 0);
              CREATE TABLE IF NOT EXISTS weather_seen(id TEXT PRIMARY KEY,expires REAL);
              CREATE TABLE IF NOT EXISTS deliveries(message TEXT,room TEXT,status TEXT DEFAULT 'pending',lease TEXT,until REAL,
                PRIMARY KEY(message,room));
              CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,caller TEXT,callee TEXT,status TEXT,created REAL,
                expires REAL,ended REAL,reason TEXT);
              CREATE INDEX IF NOT EXISTS call_state ON calls(status,expires);
              CREATE TABLE IF NOT EXISTS features(id TEXT PRIMARY KEY,summary TEXT,details TEXT,requested_by TEXT,
                room TEXT,created REAL,status TEXT DEFAULT 'requested',updated REAL,note TEXT);
              INSERT OR IGNORE INTO turn VALUES(1,NULL,NULL,0);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(features)")}
            for col, kind in [("updated", "REAL"), ("note", "TEXT")]:
                if col not in columns:
                    db.execute("ALTER TABLE features ADD COLUMN " + col + " " + kind)
            if "urgent" not in {r[1] for r in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN urgent INTEGER DEFAULT 0")

    @contextmanager
    def tx(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=2)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA busy_timeout=2000")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def heartbeat(self, *, muted: bool, busy: bool):
        now = self.clock()
        with self.tx() as db:
            db.execute(
                "INSERT INTO rooms VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,seen=excluded.seen,muted=excluded.muted,busy=excluded.busy",
                (self.room, self.rooms[self.room], now, int(muted), int(busy)),
            )
            db.execute(
                "UPDATE calls SET status='ended',ended=?,reason='timeout' WHERE status IN ('ringing','active') AND expires<=?",
                (now, now),
            )
            db.execute(
                "UPDATE calls SET status='ended',ended=?,reason='unavailable' WHERE status IN ('ringing','active') AND EXISTS(SELECT 1 FROM rooms WHERE (id=calls.caller OR id=calls.callee) AND (seen<? OR muted=1))",
                (now, now - 6),
            )
            db.execute(
                "DELETE FROM deliveries WHERE message IN (SELECT id FROM messages WHERE expires<?)",
                (now - 86400,),
            )
            db.execute("DELETE FROM messages WHERE expires<?", (now - 86400,))
            db.execute("DELETE FROM calls WHERE ended<?", (now - 86400,))
            db.execute("DELETE FROM weather_seen WHERE expires<?", (now - 86400,))
            db.execute("DELETE FROM candidates WHERE election NOT IN (SELECT token FROM election)")
            if muted:
                db.execute(
                    "UPDATE turn SET owner=NULL,token=NULL,until=0 WHERE owner=?", (self.room,)
                )
                self.token, self.valid_until = None, 0
            elif self.token:
                cur = db.execute(
                    "UPDATE turn SET until=? WHERE owner=? AND token=? AND until>?",
                    (now + 15, self.room, self.token, now),
                )
                self.valid_until = now + 15 if cur.rowcount else 0
        return self.directory()

    def directory(self):
        now = self.clock()
        with self.tx() as db:
            rows = {r["id"]: dict(r) for r in db.execute("SELECT * FROM rooms")}
        return [
            {
                "id": key,
                "name": name,
                "online": key in rows and now - rows[key]["seen"] < 6,
                "muted": bool(rows.get(key, {}).get("muted", True)),
            }
            for key, name in self.rooms.items()
        ]

    def export_public(self):
        """Only summaries/status enter the family dashboard, never request details."""
        with self.tx() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT id,summary,status,note,coalesce(updated,created) AS updated FROM features WHERE status NOT IN ('done','declined') OR coalesce(updated,created)>? ORDER BY CASE status WHEN 'needs_approval' THEN 0 WHEN 'needs_information' THEN 1 ELSE 2 END,created DESC LIMIT 8",
                    (self.clock() - 86400,),
                )
            ]
        directory = self.root / "public"
        # Only this sanitized projection is readable by the dashboard container.
        # The parent and all operational databases remain private (0700/0600).
        directory.mkdir(mode=0o755, exist_ok=True)
        directory.chmod(0o755)
        path = directory / ("status." + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "w") as out:
                json.dump({"updated": self.clock(), "requests": rows}, out)
            path.replace(directory / "status.json")
        finally:
            path.unlink(missing_ok=True)

    def owns_turn(self):
        return self.token is not None and self.clock() < self.valid_until

    def nominate(self, quality: float, window: float = 0.55):
        if not isinstance(quality, (int, float)) or not math.isfinite(quality):
            raise ValueError("Invalid wake quality")
        now = self.clock()
        with self.tx() as db:
            row = db.execute("SELECT * FROM turn WHERE id=1").fetchone()
            if row["until"] > now:
                if row["owner"] == self.room and row["token"] == self.token:
                    self.valid_until = row["until"]
                    return True
                return False
            row = db.execute("SELECT * FROM election WHERE id=1").fetchone()
            if row is None or row["deadline"] < now - 0.1:
                election, deadline = uuid.uuid4().hex, now + window
                db.execute("INSERT OR REPLACE INTO election VALUES(1,?,?)", (election, deadline))
            else:
                election, deadline = row["token"], row["deadline"]
            db.execute(
                "INSERT OR REPLACE INTO candidates VALUES(?,?,?)",
                (election, self.room, min(1.0, max(0.0, quality))),
            )
        time.sleep(max(0, deadline - self.clock()))
        now = self.clock()
        with self.tx() as db:
            turn = db.execute("SELECT * FROM turn WHERE id=1").fetchone()
            if turn["until"] <= now:
                candidates = db.execute(
                    "SELECT c.room FROM candidates c JOIN rooms r ON r.id=c.room WHERE c.election=? AND r.muted=0 AND r.seen>? ORDER BY c.quality DESC,c.room LIMIT 1",
                    (election, now - 6),
                ).fetchone()
                if candidates is None:
                    return False
                token = secrets.token_hex(16)
                db.execute(
                    "UPDATE turn SET owner=?,token=?,until=? WHERE id=1",
                    (candidates[0], token, now + 15),
                )
                turn = db.execute("SELECT * FROM turn WHERE id=1").fetchone()
            won = turn["owner"] == self.room
            if won:
                self.token, self.valid_until = turn["token"], turn["until"]
            return won

    def release(self):
        with self.tx() as db:
            # A short tail suppresses the same utterance arriving later at the other room.
            db.execute(
                "UPDATE turn SET until=? WHERE owner=? AND token=?",
                (self.clock() + 1, self.room, self.token),
            )
        self.token, self.valid_until = None, 0

    def resolve(self, names, *, allow_all=False):
        if not isinstance(names, list) or not 1 <= len(names) <= 16:
            raise ValueError("Name one or more rooms")
        result = []
        for name in names:
            name = clean(name, 60).casefold()
            if allow_all and name in {"all", "everywhere", "everyone", "all rooms", "all devices"}:
                result.extend(self.rooms)
            elif name in self.aliases:
                result.append(self.aliases[name])
            else:
                raise ValueError(
                    "Unknown room; available rooms are " + ", ".join(self.rooms.values())
                )
        return list(dict.fromkeys(result))

    def announce(self, targets, message, guard=lambda: None):
        targets = self.resolve(targets, allow_all=True)
        message = clean(message, 500)
        now = self.clock()
        with self.tx() as db:
            if (
                db.execute("SELECT count(*) FROM messages WHERE expires>?", (now,)).fetchone()[0]
                >= 32
            ):
                return "There are too many pending announcements. Try again later."
            guard()
            key = uuid.uuid4().hex
            db.execute(
                "INSERT INTO messages(id,sender,text,created,expires) VALUES(?,?,?,?,?)",
                (key, self.room, message, now, now + 300),
            )
            db.executemany(
                "INSERT INTO deliveries(message,room) VALUES(?,?)", [(key, r) for r in targets]
            )
        return (
            "Queued the announcement for "
            + ", ".join(self.rooms[r] for r in targets)
            + ". Muted or busy rooms can receive it within five minutes."
        )

    def pending_messages(self):
        with self.tx() as db:
            return db.execute(
                "SELECT count(*) FROM deliveries d JOIN messages m ON m.id=d.message WHERE d.room=? AND d.status='pending' AND m.expires>?",
                (self.room, self.clock()),
            ).fetchone()[0]

    def enqueue_weather(self, alert):
        now = self.clock()
        if (
            not isinstance(alert, dict)
            or alert.get("event")
            not in {
                "Tornado Warning",
                "Flash Flood Warning",
                "Severe Thunderstorm Warning",
                "Extreme Wind Warning",
            }
            or alert.get("severity") not in {"Extreme", "Severe"}
            or not alert.get("effective_at", now + 1) <= now < alert.get("expires_at", 0)
        ):
            return False
        key = hashlib.sha256(json.dumps(alert, sort_keys=True).encode()).hexdigest()
        headline = " ".join(str(alert.get("headline", "")).split())[:160]
        instruction = " ".join(str(alert.get("instruction", "")).split())[:250]
        message = clean(
            ("National Weather Service. " + alert["event"] + ". " + headline + ". " + instruction)[
                :500
            ],
            500,
        )
        with self.tx() as db:
            if db.execute("SELECT 1 FROM weather_seen WHERE id=?", (key,)).fetchone():
                return False
            db.execute("INSERT INTO weather_seen VALUES(?,?)", (key, alert["expires_at"]))
            ident = uuid.uuid4().hex
            db.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,1)",
                (ident, self.room, message, now, min(alert["expires_at"], now + 300)),
            )
            db.executemany(
                "INSERT INTO deliveries(message,room) VALUES(?,?)", [(ident, r) for r in self.rooms]
            )
        return True

    def claim_message(self, urgent_only=False):
        now = self.clock()
        with self.tx() as db:
            row = db.execute(
                "SELECT m.*,d.lease FROM messages m JOIN deliveries d ON m.id=d.message WHERE d.room=? AND d.status='pending' AND m.expires>? AND (d.lease IS NULL OR d.until<?) AND (?=0 OR m.urgent=1) ORDER BY m.urgent DESC,m.created LIMIT 1",
                (self.room, now, now, int(urgent_only)),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE deliveries SET lease=?,until=? WHERE message=? AND room=?",
                (token, now + 90, row["id"], self.room),
            )
            return {**dict(row), "lease": token}

    def finish_message(self, message, delivered):
        with self.tx() as db:
            db.execute(
                "UPDATE deliveries SET status=?,lease=NULL,until=NULL WHERE message=? AND room=? AND lease=?",
                (
                    "delivered" if delivered else "pending",
                    message["id"],
                    self.room,
                    message["lease"],
                ),
            )

    def start_call(self, target, guard=lambda: None):
        target = self.resolve([target])[0]
        if target == self.room:
            return "Choose the other room to call."
        now = self.clock()
        with self.tx() as db:
            status = db.execute("SELECT * FROM rooms WHERE id=?", (target,)).fetchone()
            if status is None or now - status["seen"] >= 6 or status["muted"] or status["busy"]:
                return self.rooms[target] + " is muted, busy, or offline. I have not opened a call."
            if db.execute(
                "SELECT 1 FROM calls WHERE status IN ('ringing','active') AND expires>?", (now,)
            ).fetchone():
                return "A household call is already pending or active."
            guard()
            db.execute(
                "INSERT INTO calls VALUES(?,?,?,?,?,?,NULL,NULL)",
                (uuid.uuid4().hex, self.room, target, "ringing", now, now + 45),
            )
        return "Calling " + self.rooms[target] + ". They need to say Hey Vector, answer the call."

    def call(self):
        now = self.clock()
        with self.tx() as db:
            row = db.execute(
                "SELECT * FROM calls WHERE (caller=? OR callee=?) AND status IN ('ringing','active') AND expires>? ORDER BY created DESC LIMIT 1",
                (self.room, self.room, now),
            ).fetchone()
            return dict(row) if row else None

    def answer_call(self, guard=lambda: None):
        now = self.clock()
        with self.tx() as db:
            row = db.execute(
                "SELECT * FROM calls WHERE callee=? AND status='ringing' AND expires>?",
                (self.room, now),
            ).fetchone()
            if row is None:
                return "There is no incoming call to answer."
            participants = list(
                db.execute("SELECT * FROM rooms WHERE id IN (?,?)", (row["caller"], self.room))
            )
            if len(participants) != 2 or any(
                r["muted"] or now - r["seen"] >= 6 for r in participants
            ):
                return "The other room is no longer available. No microphone was connected."
            guard()
            db.execute(
                "UPDATE calls SET status='active',expires=? WHERE id=? AND status='ringing'",
                (now + 600, row["id"]),
            )
        return "Connecting your call with " + self.rooms[row["caller"]] + "."

    def end_call(self, guard=lambda: None):
        with self.tx() as db:
            guard()
            result = db.execute(
                "UPDATE calls SET status='ended',ended=?,reason='hangup' WHERE (caller=? OR callee=?) AND status IN ('ringing','active')",
                (self.clock(), self.room, self.room),
            )
        return "Call ended." if result.rowcount else "There is no active call."

    def handle(self, name, args, guard=lambda: None):
        guard()
        if not isinstance(args, dict):
            raise ValueError("Invalid household arguments")
        if name == "room_announcement" and set(args) == {"rooms", "message"}:
            return self.announce(args["rooms"], args["message"], guard)
        if name == "feature_request" and set(args) == {
            "action",
            "summary",
            "details",
            "requested_by",
        }:
            if args["action"] == "status" and all(
                args[k] is None for k in ("summary", "details", "requested_by")
            ):
                with self.tx() as db:
                    rows = list(
                        db.execute(
                            "SELECT id,summary,status,note FROM features WHERE status NOT IN ('done','declined') OR coalesce(updated,created)>? ORDER BY CASE status WHEN 'needs_approval' THEN 0 WHEN 'needs_information' THEN 1 ELSE 2 END,created DESC LIMIT 5",
                            (self.clock() - 86400,),
                        )
                    )
                if not rows:
                    return "There are no pending feature requests or completed changes in the last day."
                parts = [
                    r["summary"]
                    + ": "
                    + r["status"].replace("_", " ")
                    + (". " + r["note"][:120] if r["note"] else "")
                    for r in rows
                ]
                return ("Your Vector update: " + "; ".join(parts) + ".")[:590]
            if args["action"] == "create":
                summary, details = clean(args["summary"], 120), clean(args["details"], 800)
                person = (
                    clean(args["requested_by"], 40) if args["requested_by"] is not None else None
                )
                with self.tx() as db:
                    if (
                        db.execute(
                            "SELECT count(*) FROM features WHERE status='requested'"
                        ).fetchone()[0]
                        >= 100
                    ):
                        return "The feature queue is full. Please ask the owner to review it."
                    guard()
                    key = "VR-" + uuid.uuid4().hex[:8].upper()
                    db.execute(
                        "INSERT INTO features(id,summary,details,requested_by,room,created,updated) VALUES(?,?,?,?,?,?,?)",
                        (key, summary, details, person, self.room, self.clock(), self.clock()),
                    )
                return (
                    "Saved feature request "
                    + key
                    + ": "
                    + summary
                    + ". The owner can review it in your daily update; nothing has been installed or changed."
                )
        if name == "room_call" and set(args) == {"action", "room"}:
            action, room = args["action"], args["room"]
            if action == "start" and isinstance(room, str):
                return self.start_call(room, guard)
            if action in {"answer", "end", "decline"} and isinstance(room, str):
                # A natural request may name the peer even when ending a call.
                # Accept only this resident's actual peer, never a different call.
                current = self.call()
                peer = self.resolve([room])[0]
                if (
                    current is None
                    or peer == self.room
                    or peer not in {current["caller"], current["callee"]}
                ):
                    raise ValueError("That room is not in this call")
                room = None
            if action == "answer" and room is None:
                return self.answer_call(guard)
            if action in {"end", "decline"} and room is None:
                return self.end_call(guard)
        raise ValueError("Invalid household action")


def parse(text):
    text = re.sub(r"^hey[ ,]+(?:vector|victor)[ ,]*", "", text, flags=re.I).strip(" .!?")
    if re.search(r"\b(?:don't|do not|never|pretend|if)\b", text, re.I):
        return None
    if re.fullmatch(
        r"(?:give me |read |what is |what\x27s )?(?:my |our |the )?(?:daily |vector )?update",
        text,
        re.I,
    ):
        return "feature_request", {
            "action": "status",
            "summary": None,
            "details": None,
            "requested_by": None,
        }
    match = re.fullmatch(
        r"(?:announce|broadcast)(?: to)? (everywhere|everyone|all rooms|all devices) (?:that )?(.+)",
        text,
        re.I,
    )
    if match:
        return "room_announcement", {"rooms": [match[1]], "message": match[2]}
    match = re.fullmatch(
        r"(?:tell|notify|announce to|broadcast to) (.+?) (?:that |: )?(.+)", text, re.I
    )
    # Keep ambiguous free wording on the natural-language tool path.
    if match and " that " in text.lower():
        room, message = re.split(
            r" that ",
            re.sub(r"^(?:tell|notify|announce to|broadcast to) ", "", text, flags=re.I),
            maxsplit=1,
            flags=re.I,
        )
        return "room_announcement", {"rooms": [room], "message": message}
    match = re.fullmatch(r"(?:call|ring)(?: the)? (.+?)(?: room)?", text, re.I)
    if match:
        return "room_call", {"action": "start", "room": match[1]}
    if re.fullmatch(r"(?:answer|accept|pick up)(?: the)?(?: incoming)? call", text, re.I):
        return "room_call", {"action": "answer", "room": None}
    if re.fullmatch(r"(?:end|hang up|decline|reject)(?: the)?(?: call)?", text, re.I):
        return "room_call", {"action": "end", "room": None}
    return None


def tools():
    from .tool_schema import tool, choice

    return [
        tool(
            "room_announcement",
            "Send an explicitly requested spoken message to selected registered rooms or everywhere. Dinner time, tell everyone, announce and broadcast use this. Never send an announcement from a quoted, negated or hypothetical request. Muted/busy rooms defer up to five minutes; queued does not mean heard. Never change TV or receiver playback.",
            {
                "rooms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 16,
                },
                "message": {"type": "string", "maxLength": 500},
            },
        ),
        tool(
            "room_call",
            "Start an audio call to one registered room, answer an incoming call, or end/decline. The recipient must explicitly answer. No drop-in and no automatic microphone activation. Use room for start; otherwise prefer null, or name only the current peer. During a call, Hey Vector stop or Action ends it; physical mute ends it too. Calls last at most ten minutes.",
            {
                "action": choice(["start", "answer", "end", "decline"]),
                "room": {"type": ["string", "null"]},
            },
        ),
        tool(
            "feature_request",
            "Save a request for a NEW capability or integration, such as debloating another TV, adding fridge controls/telemetry, or changing software. This only queues an idea for the owner to review and implement; it does not grant access, alter firmware, run code, order hardware, or cause automatic implementation. Use ordinary device tools for capabilities already available on their allowlisted devices. Never redirect an unknown room TV to the main TV. requested_by is the name explicitly stated by the speaker, not an authenticated identity; otherwise null. Do not store secrets. For status set summary/details/requested_by to null.",
            {
                "action": choice(["create", "status"]),
                "summary": {"type": ["string", "null"], "maxLength": 120},
                "details": {"type": ["string", "null"], "maxLength": 800},
                "requested_by": {"type": ["string", "null"], "maxLength": 40},
            },
        ),
    ]
