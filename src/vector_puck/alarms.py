"""Room-local alarm clock with durable schedules, ringing state and repeated snooze."""

from __future__ import annotations
from datetime import datetime, date, timedelta, time as daytime
import json
from pathlib import Path
import re
import time
import uuid
from zoneinfo import ZoneInfo

from .kitchen import Kitchen, clean

TZ = ZoneInfo("UTC")


def next_due(at, days, on, now, zone=TZ):
    if not isinstance(at, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", at):
        raise ValueError("Alarm time must have an unambiguous hour and minute")
    if (
        not isinstance(days, list)
        or len(days) > 7
        or any(type(v) is not int or not 0 <= v <= 6 for v in days)
    ):
        raise ValueError("Invalid alarm days")
    today = datetime.fromtimestamp(now, zone).date()
    chosen = date.fromisoformat(on) if on is not None else None
    if chosen is not None and (days or not today <= chosen <= today + timedelta(days=366)):
        raise ValueError("Choose one date or recurring weekdays")
    for offset in range(367):
        day = chosen if chosen is not None else today + timedelta(days=offset)
        if days and day.weekday() not in days:
            continue
        local = datetime.combine(day, daytime.fromisoformat(at), zone)
        # Skip nonexistent spring-forward times; use the first fall-back occurrence.
        due = local.timestamp()
        if datetime.fromtimestamp(due, zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            if chosen:
                raise ValueError("That local alarm time does not exist on the selected date")
            continue
        if due > now + 1:
            return due
        if chosen:
            break
    raise ValueError("Alarm must be in the future")


class Alarms:
    def __init__(self, path: Path, clock=time.time, *, timezone="UTC"):
        self.zone = ZoneInfo(timezone)
        self.store = Kitchen(path, timezone=timezone)
        self.clock = clock
        with self.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS alarms(id TEXT PRIMARY KEY,name TEXT UNIQUE COLLATE NOCASE,at TEXT,days TEXT,on_date TEXT,
              enabled INTEGER,due REAL,status TEXT,snoozes INTEGER DEFAULT 0,updated REAL)""")

    def ringing(self):
        with self.store.transaction() as db:
            return [
                dict(r)
                for r in db.execute("SELECT * FROM alarms WHERE enabled=1 AND status='ringing'")
            ]

    def tick(self):
        now = self.clock()
        with self.store.transaction() as db:
            rows = list(
                db.execute(
                    "SELECT * FROM alarms WHERE enabled=1 AND status='scheduled' AND due<=?", (now,)
                )
            )
            for row in rows:
                if now - row["due"] > 7200:
                    self._dismiss(db, row, now)
                else:
                    db.execute(
                        "UPDATE alarms SET status='ringing',updated=? WHERE id=?", (now, row["id"])
                    )
        return self.ringing()

    def _dismiss(self, db, row, now):
        days = json.loads(row["days"])
        if days:
            due = next_due(row["at"], days, None, now, self.zone)
            db.execute(
                "UPDATE alarms SET id=?,status='scheduled',due=?,snoozes=0,updated=? WHERE id=?",
                (uuid.uuid4().hex, due, now, row["id"]),
            )
        else:
            db.execute(
                "UPDATE alarms SET enabled=0,status='dismissed',updated=? WHERE id=?",
                (now, row["id"]),
            )

    def dismiss_ringing(self, ident=None):
        with self.store.transaction() as db:
            rows = list(db.execute("SELECT * FROM alarms WHERE status='ringing' AND enabled=1"))
            for row in rows:
                if ident is None or row["id"] == ident:
                    self._dismiss(db, row, self.clock())

    def handle(self, args, guard=lambda: None):
        if not isinstance(args, dict) or set(args) != {
            "action",
            "name",
            "at",
            "days",
            "date",
            "minutes",
        }:
            raise ValueError("Invalid alarm fields")
        action = args["action"]
        name = clean(args["name"], 60) if args["name"] is not None else None
        guard()
        now = self.clock()
        if action == "set":
            at, days, on = args["at"], args["days"], args["date"]
            minutes = args["minutes"]
            if minutes is not None:
                if type(minutes) is not int or not 1 <= minutes <= 1440:
                    raise ValueError("A relative alarm must be one to 1440 minutes from now")
                if at is not None or days != [] or on is not None:
                    raise ValueError("Choose a time of day or minutes from now, not both")
                due = now + minutes * 60
                local = datetime.fromtimestamp(due, self.zone)
                at, on = local.strftime("%H:%M"), local.date().isoformat()
            else:
                due = next_due(at, days, on, now, self.zone)
            name = name or "alarm"
            with self.store.transaction() as db:
                if (
                    db.execute("SELECT count(*) FROM alarms WHERE enabled=1").fetchone()[0] >= 16
                    and not db.execute("SELECT 1 FROM alarms WHERE name=?", (name,)).fetchone()
                ):
                    return "There are already sixteen enabled alarms. Cancel one first."
                guard()
                db.execute(
                    "INSERT INTO alarms(id,name,at,days,on_date,enabled,due,status,snoozes,updated) VALUES(?,?,?,?,?,1,?,'scheduled',0,?) ON CONFLICT(name) DO UPDATE SET id=excluded.id,at=excluded.at,days=excluded.days,on_date=excluded.on_date,enabled=1,due=excluded.due,status='scheduled',snoozes=0,updated=excluded.updated",
                    (uuid.uuid4().hex, name, at, json.dumps(days), on, due, now),
                )
            when = (
                "in " + str(minutes) + " " + ("minute" if minutes == 1 else "minutes")
                if minutes is not None
                else "for " + datetime.fromtimestamp(due, self.zone).strftime("%A at %I:%M %p")
            )
            return (
                "Set the "
                + name
                + " alarm "
                + when
                + ". It will keep ringing until stopped, with a nine-minute default snooze."
            )
        if args["at"] is not None or args["date"] is not None or args["days"] != []:
            raise ValueError("Only setting an alarm takes a schedule")
        if action != "snooze" and args["minutes"] is not None:
            raise ValueError("Only snooze takes minutes")
        with self.store.transaction() as db:
            rows = list(db.execute("SELECT * FROM alarms WHERE enabled=1 ORDER BY due"))
            if name:
                rows = [r for r in rows if r["name"].casefold() == name.casefold()]
            if action == "status":
                return (
                    (
                        "Your alarms: "
                        + "; ".join(
                            r["name"]
                            + ", "
                            + (
                                "ringing"
                                if r["status"] == "ringing"
                                else datetime.fromtimestamp(r["due"], self.zone).strftime(
                                    "%A %I:%M %p"
                                )
                            )
                            for r in rows[:5]
                        )
                        + "."
                    )
                    if rows
                    else "There are no enabled alarms in this room."
                )
            if action in {"snooze", "dismiss"}:
                rows = [r for r in rows if r["status"] == "ringing"]
                if not rows:
                    return "There is no ringing alarm in this room."
                guard()
                if action == "dismiss":
                    for row in rows:
                        self._dismiss(db, row, now)
                    return "Alarm off. Recurring alarms remain scheduled for their next day."
                minutes = 9 if args["minutes"] is None else args["minutes"]
                if type(minutes) is not int or not 1 <= minutes <= 60:
                    raise ValueError("Snooze must be one to sixty minutes")
                for row in rows:
                    db.execute(
                        "UPDATE alarms SET id=?,status='scheduled',due=?,snoozes=snoozes+1,updated=? WHERE id=?",
                        (uuid.uuid4().hex, now + minutes * 60, now, row["id"]),
                    )
                return "Snoozed for " + str(minutes) + " minutes."
            if action == "cancel":
                if len(rows) != 1:
                    return (
                        "Which alarm should I cancel? Say its name."
                        if rows
                        else "I could not find that enabled alarm."
                    )
                guard()
                db.execute(
                    "UPDATE alarms SET enabled=0,status='cancelled',updated=? WHERE id=?",
                    (now, rows[0]["id"]),
                )
                return "Cancelled the " + rows[0]["name"] + " alarm."
        raise ValueError("Invalid alarm action")


def parse(text, ringing=False):
    text = re.sub(r"^hey[ ,]+(?:vector|victor)[ ,]*", "", text, flags=re.I).strip(" .!?").lower()
    if ringing and text in {
        "off",
        "stop",
        "alarm off",
        "turn it off",
        "turn off the alarm",
        "stop the alarm",
        "dismiss the alarm",
    }:
        return dict(action="dismiss", name=None, at=None, days=[], date=None, minutes=None)
    m = re.fullmatch(r"snooze(?: (?:for )?([0-9]+) minutes?)?", text)
    if m:
        return dict(
            action="snooze",
            name=None,
            at=None,
            days=[],
            date=None,
            minutes=int(m[1]) if m[1] else None,
        )
    return None


def tools():
    from .tool_schema import tool, choice

    return [
        tool(
            "alarm_clock",
            "Room-local persistent wake-up alarm, for daily wake-ups and one-off events. Ring repeatedly until dismissed with alarm off or Action. Set a relative alarm such as in two minutes using minutes=2, at/date=null, days=[]. Set an absolute or recurring alarm using HH:MM in 24-hour local time and either weekdays (0 Monday to 6 Sunday) or one date; minutes=null. [] and date=null means next occurrence. Ask about ambiguous AM/PM or missing time instead of guessing. Snooze can repeat; default nine minutes, explicit minutes 1-60. Non-set actions take at/date=null and days=[]. A recurring alarm off dismisses today; cancel removes its schedule. Room name is not voice identity.",
            {
                "action": choice(["set", "status", "cancel", "snooze", "dismiss"]),
                "name": {"type": ["string", "null"], "maxLength": 60},
                "at": {
                    "type": ["string", "null"],
                    "description": "HH:MM 24-hour local time, set only",
                },
                "days": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": 6},
                    "maxItems": 7,
                },
                "date": {
                    "type": ["string", "null"],
                    "description": "YYYY-MM-DD for a one-off alarm, otherwise null",
                },
                "minutes": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 1440,
                    "description": "For set: minutes from now (1-1440), with at/date null and days []. For snooze: 1-60 minutes. Otherwise null.",
                },
            },
        )
    ]
