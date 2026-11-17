"""Durable, bounded household tasks. No network, media, or device-control access."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("UTC")


def clean(value: str, limit: int = 120) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid kitchen text")
    value = " ".join(value.split())
    if not value or len(value) > limit:
        raise ValueError("Kitchen text outside bounds")
    return value


def seconds(value, minimum=1, maximum=86400) -> int:
    if type(value) not in {int, float} or not math.isfinite(value) or int(value) != value:
        raise ValueError("Invalid duration")
    if not minimum <= value <= maximum:
        raise ValueError("Duration outside bounds")
    return int(value)


def duration(value: float) -> str:
    value = max(0, math.ceil(value))
    parts = []
    for size, unit in [(3600, "hour"), (60, "minute"), (1, "second")]:
        count, value = divmod(value, size)
        if count:
            parts.append(f"{count} {unit}{'s' if count != 1 else ''}")
    return " and ".join(parts) or "0 seconds"


def reminder_time(value: str, now: float, zone=ZONE) -> float:
    value = clean(value, 60)
    # An explicit offset avoids guessing during DST's repeated hour. Require that
    # the supplied local clock/offset actually exists in this household's zone.
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("A reminder needs an explicit local date, time and UTC offset")
    local = stamp.astimezone(zone)
    if (
        stamp.replace(tzinfo=None) != local.replace(tzinfo=None)
        or stamp.utcoffset() != local.utcoffset()
    ):
        raise ValueError("Reminder time does not exist in household timezone")
    due = stamp.timestamp()
    if not now + 1 <= due <= now + 366 * 86400:
        raise ValueError("Reminder must be in the next year")
    return due


class Kitchen:
    def __init__(self, path: Path, clock=time.time, *, room_label: str = "", timezone: str = "UTC"):
        self.path, self.clock = Path(path), clock
        self.room_label = room_label
        self.zone = ZoneInfo(timezone)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("Kitchen database cannot be a symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.lock = threading.RLock()
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL,
                    message TEXT NOT NULL, due REAL NOT NULL, created REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', lease TEXT, lease_until REAL,
                    retry_at REAL NOT NULL DEFAULT 0, finished REAL);
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY, text TEXT NOT NULL, canonical TEXT UNIQUE NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS task_due ON tasks(status,due);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def transaction(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA busy_timeout=5000")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def tasks(self) -> list[dict]:
        with self.transaction() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,kind,label,message,due,status FROM tasks WHERE status='active' ORDER BY due LIMIT 32"
                )
            ]

    def items(self) -> list[dict]:
        with self.transaction() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT text,done FROM items ORDER BY done,updated,id LIMIT 200"
                )
            ]

    def snapshot(self) -> dict:
        # Tasks are scoped to this database. No shopping contents or reminder messages
        # are sent with unrelated questions or web searches.
        return {
            "timezone": str(self.zone),
            "active_tasks": [
                {
                    "kind": t["kind"],
                    "name": t["label"],
                    "due": datetime.fromtimestamp(t["due"], self.zone).isoformat(),
                }
                for t in self.tasks()[:12]
            ],
        }

    def handle(self, name: str, args: dict, guard=lambda: None) -> str:
        guard()
        if not isinstance(args, dict):
            raise ValueError("Invalid kitchen arguments")
        if name == "shopping_list":
            return self.shopping(args, guard)
        if name != "kitchen_task" or set(args) != {
            "action",
            "kind",
            "name",
            "seconds",
            "when",
            "message",
        }:
            raise ValueError("Invalid kitchen tool")
        action, kind = args["action"], args["kind"]
        if kind not in {"timer", "reminder"} or action not in {
            "start",
            "status",
            "extend",
            "cancel",
        }:
            raise ValueError("Invalid task action")
        label = clean(args["name"], 60) if args["name"] is not None else None
        if label:
            # Spoken names commonly include the task type: “pasta timer”.
            # Store and look up the same name whether that suffix is present.
            label = re.sub(rf"\s+{kind}$", "", label, flags=re.IGNORECASE)
        now = self.clock()
        with self.transaction() as db:
            # Keep recent task outcomes for status questions; don't retain a
            # permanent history of household requests.
            db.execute(
                "DELETE FROM tasks WHERE finished IS NOT NULL AND finished<?", (now - 7 * 86400,)
            )
            if action == "start":
                if kind == "timer":
                    if args["when"] is not None or args["message"] is not None:
                        raise ValueError("Timer only takes a duration")
                    due, label, message = now + seconds(args["seconds"]), label or "timer", ""
                else:
                    message = clean(args["message"], 200)
                    label = label or message[:60]
                    if (args["seconds"] is None) == (args["when"] is None):
                        raise ValueError("Reminder needs exactly one time")
                    due = (
                        now + seconds(args["seconds"], maximum=366 * 86400)
                        if args["seconds"] is not None
                        else reminder_time(args["when"], now, self.zone)
                    )
                if (
                    db.execute("SELECT count(*) FROM tasks WHERE status='active'").fetchone()[0]
                    >= 32
                ):
                    return "There are already 32 active timers and reminders. Cancel one first."
                if db.execute(
                    "SELECT 1 FROM tasks WHERE status='active' AND kind=? AND lower(label)=lower(?)",
                    (kind, label),
                ).fetchone():
                    return f"There's already an active {label} {kind}. Use a different name, or change the existing one."
                guard()  # Immediately before the write, inside the transaction.
                db.execute(
                    "INSERT INTO tasks(id,kind,label,message,due,created) VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, kind, label, message, due, now),
                )
                if kind == "timer":
                    return f"Started your {label} timer for {duration(due - now)}."
                at = datetime.fromtimestamp(due, self.zone).strftime("%A, %B %d at %I:%M %p")
                return f"I'll remind you {at} to {message}."
            if any(args[k] is not None for k in ("when", "message")) or (
                action != "extend" and args["seconds"] is not None
            ):
                raise ValueError("Unexpected task arguments")
            rows = list(
                db.execute(
                    "SELECT * FROM tasks WHERE kind=? AND status='active' ORDER BY due", (kind,)
                )
            )
            if label:
                rows = [r for r in rows if r["label"].casefold() == label.casefold()]
            if action == "status":
                if not rows:
                    recent = db.execute(
                        "SELECT label FROM tasks WHERE kind=? AND status='delivered' AND finished>? ORDER BY finished DESC LIMIT 1",
                        (kind, now - 3600),
                    ).fetchone()
                    if recent and (not label or label.casefold() == recent[0].casefold()):
                        return f"Your {recent[0]} {kind} has finished."
                    return f"There are no active {kind}s" + (f" named {label}." if label else ".")
                results = []
                for r in rows[:4]:
                    if r["due"] <= now:
                        results.append(f"Your {r['label']} {kind} is due")
                    elif kind == "timer":
                        results.append(f"{r['label']} has {duration(r['due'] - now)} left")
                    else:
                        at = datetime.fromtimestamp(r["due"], self.zone).strftime("%A at %I:%M %p")
                        results.append(f"{r['label']} is set for {at}")
                if len(rows) > 4:
                    results.append(f"and {len(rows) - 4} more; ask for one by name")
                return ". ".join(results) + "."
            if len(rows) != 1:
                return (
                    f"Which {kind}? Say its name."
                    if rows
                    else f"I couldn't find that active {kind}."
                )
            task = rows[0]
            if action == "extend":
                if kind != "timer":
                    raise ValueError("Only timers can be extended")
                amount = seconds(args["seconds"], minimum=-86400)
                if amount == 0:
                    raise ValueError("Timer change cannot be zero")
                due = max(now, task["due"]) + amount
                if not now + 1 <= due <= now + 86400:
                    return "That would put the timer outside its one-second to 24-hour range."
                guard()
                db.execute(
                    "UPDATE tasks SET due=?,lease=NULL,lease_until=NULL,retry_at=0 WHERE id=?",
                    (due, task["id"]),
                )
                return f"Your {task['label']} timer now has {duration(due - now)} left."
            guard()
            db.execute(
                "UPDATE tasks SET status='cancelled',finished=?,lease=NULL,lease_until=NULL WHERE id=?",
                (now, task["id"]),
            )
            return f"Cancelled your {task['label']} {kind}."

    def shopping(self, args: dict, guard) -> str:
        if set(args) != {"action", "items", "offset"} or args["action"] not in {
            "add",
            "read",
            "complete",
            "remove",
        }:
            raise ValueError("Invalid shopping action")
        action, values, offset = args["action"], args["items"], args["offset"]
        if (
            type(offset) is not int
            or not 0 <= offset <= 200
            or not isinstance(values, list)
            or len(values) > 10
        ):
            raise ValueError("Shopping arguments outside bounds")
        values = list(dict.fromkeys(clean(v, 80) for v in values))
        if action == "read":
            if values:
                raise ValueError("Read takes no items")
            rows = [i["text"] for i in self.items() if not i["done"]]
            if not rows:
                return (
                    f"The {self.room_label} shopping list is empty."
                    if self.room_label
                    else "The shared shopping list is empty."
                )
            page = rows[offset : offset + 5]
            if not page:
                return "That's the end of the shopping list."
            remaining = len(rows) - offset - len(page)
            prefix = (
                f"On the {self.room_label} shopping list: "
                if self.room_label
                else "On our shopping list: "
            )
            return (
                prefix
                + "; ".join(page)
                + (f". There are {remaining} more; ask for the next five." if remaining else ".")
            )
        if not values or offset != 0:
            raise ValueError("A shopping change needs one to ten items and no offset")
        with self.transaction() as db:
            if action == "add":
                current = db.execute("SELECT count(*) FROM items").fetchone()[0]
                extra = sum(
                    not db.execute(
                        "SELECT 1 FROM items WHERE canonical=?", (v.casefold(),)
                    ).fetchone()
                    for v in values
                )
                if current + extra > 200:
                    prefix = (
                        f"The {self.room_label} shopping list"
                        if self.room_label
                        else "The shared list"
                    )
                    return prefix + " has reached its 200-item limit. Remove some items first."
                guard()
                for value in values:
                    db.execute(
                        "INSERT INTO items(text,canonical,done,updated) VALUES(?,?,0,?) ON CONFLICT(canonical) DO UPDATE SET done=0,updated=excluded.updated,text=excluded.text",
                        (value, value.casefold(), self.clock()),
                    )
            else:
                missing = [
                    v
                    for v in values
                    if not db.execute(
                        "SELECT 1 FROM items WHERE canonical=?", (v.casefold(),)
                    ).fetchone()
                ]
                if missing:
                    return "I couldn't find " + "; ".join(missing) + ". Nothing was changed."
                guard()
                for value in values:
                    if action == "remove":
                        db.execute("DELETE FROM items WHERE canonical=?", (value.casefold(),))
                    else:
                        db.execute(
                            "UPDATE items SET done=1,updated=? WHERE canonical=?",
                            (self.clock(), value.casefold()),
                        )
        verb = {"add": "Added", "complete": "Checked off", "remove": "Removed"}[action]
        list_name = (
            f"the {self.room_label} shopping list"
            if self.room_label
            else "our shared shopping list"
        )
        return f"{verb} " + "; ".join(values) + f" on {list_name}."

    def claim_due(self) -> dict | None:
        now = self.clock()
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE status='active' AND due<=? AND retry_at<=? AND (lease IS NULL OR lease_until<=?) ORDER BY due LIMIT 1",
                (now, now, now),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE tasks SET lease=?,lease_until=? WHERE id=?", (token, now + 90, row["id"])
            )
            return {**dict(row), "lease": token}

    def finish_delivery(self, task: dict, delivered: bool):
        with self.transaction() as db:
            if delivered:
                db.execute(
                    "UPDATE tasks SET status='delivered',finished=?,lease=NULL,lease_until=NULL WHERE id=? AND lease=? AND status='active'",
                    (self.clock(), task["id"], task["lease"]),
                )
            else:
                db.execute(
                    "UPDATE tasks SET lease=NULL,lease_until=NULL,retry_at=? WHERE id=? AND lease=? AND status='active'",
                    (self.clock() + 5, task["id"], task["lease"]),
                )

    @staticmethod
    def announcement(task: dict) -> str:
        return (
            f"Your {task['label']} timer is done."
            if task["kind"] == "timer"
            else f"Here's your reminder: {task['message']}."
        )


NUMBERS = dict(
    zip(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
        range(20),
    )
)
NUMBERS.update(
    dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
)


def spoken_number(text: str) -> int:
    if re.fullmatch(r"\d+", text):
        return int(text)
    words = text.replace("-", " ").split()
    if len(words) == 1 and words[0] in {"a", "an"}:
        return 1
    if not words or any(w not in NUMBERS for w in words) or len(words) > 2:
        raise ValueError("Unsupported number phrase")
    if len(words) == 2 and not (NUMBERS[words[0]] >= 20 and NUMBERS[words[1]] < 10):
        raise ValueError("Ambiguous number phrase")
    return sum(NUMBERS[w] for w in words)


def parse_duration(text: str) -> int:
    text = re.sub(r"\bmore\s+(?=(?:hours?|minutes?|seconds?)\b)", "", text)
    text = re.sub(r"(?<=\d)-(?=(?:hour|minute|second))", " ", text)
    total = 0
    while text:
        match = re.match(r"^([\w -]+?)\s+(hours?|minutes?|seconds?)(?:\s+(?:and\s+)?|$)", text)
        if not match:
            raise ValueError("Unsupported duration phrase")
        total += spoken_number(match[1]) * {"h": 3600, "m": 60, "s": 1}[match[2][0]]
        text = text[match.end() :]
    return seconds(total)


def parse(text: str) -> tuple[str, dict] | None:
    """Deterministic offline fast path; uncertain phrasing goes to the LLM tools."""
    if not isinstance(text, str):
        return None
    text = re.sub(r"^hey[ ,]+(?:vector|victor)[ ,]*", "", text.lower()).strip(" .!?")
    text = re.sub(r"^please\s+|\s+please$", "", text)
    if re.search(r"\b(?:don't|do not|never|would|could|if|pretend)\b", text):
        return None

    def task(action, kind="timer", name=None, amount=None, message=None):
        return "kitchen_task", dict(
            action=action, kind=kind, name=name, seconds=amount, when=None, message=message
        )

    try:
        m = re.fullmatch(
            r"(?:set|start) (?:a |an )?((?!a\b|an\b|timer\b).+?) timer (?:for )?(.+)", text
        )
        if m:
            return task("start", name=m[1], amount=parse_duration(m[2]))
        m = re.fullmatch(
            r"(?:set|start)(?: a| an)? timer for (.+?)(?: (?:called|named|for) (.+))?", text
        )
        if m:
            return task("start", name=m[2], amount=parse_duration(m[1]))
        m = re.fullmatch(
            r"(?:set|start)(?: a| an)? (.+?(?:hours?|minutes?|seconds?)) (.+?) timer", text
        )
        if m:
            return task("start", name=m[2], amount=parse_duration(m[1]))
        m = re.fullmatch(r"remind (?:me|us) in (.+?) to (.+)", text)
        if m:
            return task("start", kind="reminder", amount=parse_duration(m[1]), message=m[2])
        m = re.fullmatch(r"(?:cancel|delete|stop)(?: the| my)?(?: (.+?))? (timer|reminder)", text)
        if m:
            return task("cancel", kind=m[2], name=m[1])
        m = re.fullmatch(
            r"(?:how (?:much|long)(?: time)? is left (?:on|for)|check|status of)(?: the| my)?(?: (.+?))? timer",
            text,
        )
        if m:
            return task("status", name=m[1])
        if text in {
            "timer status",
            "list timers",
            "what timers are running",
            "what timers do i have",
        }:
            return task("status")
        if text in {"list reminders", "what reminders do i have", "reminder status"}:
            return task("status", kind="reminder")
        m = re.fullmatch(
            r"(?:add|give)(?: it)? (.+?(?:hours?|minutes?|seconds?))(?: to (?:the )?(.+?) timer)?",
            text,
        )
        if m:
            return task("extend", name=m[2], amount=parse_duration(m[1]))
        m = re.fullmatch(
            r"(add|remove|check off) (.+?) (?:to|from|on|off)(?: the| our| my)? (?:shopping|grocery|groceries) list",
            text,
        )
        if m:
            return "shopping_list", dict(
                action={"add": "add", "remove": "remove", "check off": "complete"}[m[1]],
                items=re.split(r"\s+and\s+|,\s*", m[2]),
                offset=0,
            )
        if text in {
            "read the shopping list",
            "read our shopping list",
            "what's on the shopping list",
            "what is on our shopping list",
            "read the grocery list",
        }:
            return "shopping_list", dict(action="read", items=[], offset=0)
    except ValueError:
        return None
    return None


def tools(*, room_label: str = "", timezone: str = "UTC"):
    from .tool_schema import tool, choice

    return [
        tool(
            "named_timer",
            "Start, check, extend or cancel a persistent named kitchen timer. 'Give it two more minutes' extends the recent timer. Null name selects the only active timer; ask if multiple timers are ambiguous. Do not route timers to research. Never cancel all timers.",
            {
                "action": choice(["start", "status", "extend", "cancel"]),
                "name": {
                    "type": ["string", "null"],
                    "maxLength": 60,
                    "description": "Task name, e.g. pasta, without the word timer. Null for unnamed timer or status of all timers.",
                },
                "seconds": {
                    "type": ["integer", "null"],
                    "description": "Duration for start; signed additional seconds for extend. Null for status/cancel. Ten minutes is 600, two more minutes is 120.",
                },
            },
        ),
        tool(
            "reminder",
            "Create/check/cancel a persistent reminder. Start needs message and exactly one of seconds or when; set the other to null. Resolve relative dates with local_time. Ask for clarification when time/AM-PM is ambiguous. Do not research reminders or cancel all reminders.",
            {
                "action": choice(["start", "status", "cancel"]),
                "name": {
                    "type": ["string", "null"],
                    "maxLength": 60,
                    "description": "Optional short reminder name. Null lets start use the message as its name.",
                },
                "seconds": {
                    "type": ["integer", "null"],
                    "description": "Relative delay for start, e.g. half an hour is 1800. Null for absolute time, status or cancel.",
                },
                "when": {
                    "type": ["string", "null"],
                    "maxLength": 60,
                    "description": "Absolute ISO datetime with the correct configured household timezone offset. Null for relative delay, status or cancel.",
                },
                "message": {
                    "type": ["string", "null"],
                    "maxLength": 200,
                    "description": "What to remind the user about. Required for start, null for status/cancel.",
                },
            },
        ),
        tool(
            "shopping_list",
            (
                f"Manage only the {room_label} resident's local shopping/grocery list. Lists in other rooms are not connected. "
                if room_label
                else "Manage the shared household shopping/grocery list. "
            )
            + "'We got milk, cross it off' means complete milk, including as a follow-up. add/read/complete/remove; never order or buy. Preserve quantities. items contains one to ten names for a change, [] for read. offset is 0 except paginated read. Ask when the requested item is ambiguous.",
            {
                "action": choice(["add", "read", "complete", "remove"]),
                "items": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 80},
                    "maxItems": 10,
                },
                "offset": {"type": "integer", "minimum": 0, "maximum": 200},
            },
        ),
    ]
