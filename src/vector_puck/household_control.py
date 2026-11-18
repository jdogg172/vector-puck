"""Owner-configured, same-host household tools. No shell or device discovery."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

from .alarms import Alarms, parse as alarm_parse, tools as alarm_tools
from .household import (
    Household,
    SharedKitchen,
    parse as room_parse,
    tools as room_tools,
    validate_rooms,
)
from .kitchen import Kitchen, parse as kitchen_parse, tools as kitchen_tools


def read_config(path: Path) -> dict:
    """Room names are configuration, never an authenticated speaker identity."""
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("Use an absolute regular private household config")
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size > 16384
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("Household config must be private and owned by this user")

    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("Duplicate household config field")
            result[key] = value
        return result

    data = json.loads(path.read_text(), object_pairs_hook=pairs)
    if not isinstance(data, dict) or set(data) != {"root", "room", "rooms", "timezone"}:
        raise ValueError("Invalid household config fields")
    if not isinstance(data["root"], str) or not Path(data["root"]).is_absolute():
        raise ValueError("Use an absolute same-host private household directory")
    if not isinstance(data["rooms"], list) or not 2 <= len(data["rooms"]) <= 16:
        raise ValueError("Register two to sixteen rooms")
    if not isinstance(data["timezone"], str):
        raise ValueError("Use an IANA timezone")
    ZoneInfo(data["timezone"])
    validate_rooms(data["room"], data["rooms"])
    if len(Path(data["root"]).parts) < 2 or Path(data["root"]) == Path("/tmp"):
        raise ValueError("Choose a dedicated private household directory")
    return data


def write_intent(text: str) -> bool:
    """Conservative independent gate before a model-proposed mutation."""
    if not isinstance(text, str) or not 1 <= len(text) <= 800:
        return False
    text = text.lower().replace("’", "'")
    blocked = (
        r"\b(?:not|never|without|avoid|unless|if|suppose|imagine|hypothetical|"
        r"hypothetically|pretend|simulate|example|explain|describe)\b|"
        r"\b(?:don't|can't|won't|shouldn't|wouldn't|couldn't)\b|"
        r"\b(?:how|why) (?:do|would|can|does|to)\b|\b(?:would|could|should) (?:it|i|we)\b"
    )
    return re.search(blocked, text) is None and bool(
        re.search(
            r"\b(?:set|start|add|remove|delete|cancel|stop|off|dismiss|snooze|remind|"
            r"give|extend|more|wake|need|want|check off|cross|got|call|ring|answer|accept|pick up|end|hang up|"
            r"decline|reject|tell|notify|announce|broadcast|save|queue|request|build|create)\b",
            text,
        )
    )


class HouseholdControl:
    def __init__(self, data, state: Path):
        self.household = Household(Path(data["root"]), data["room"], data["rooms"])
        self.zone = ZoneInfo(data["timezone"])
        local = Kitchen(state / "kitchen.sqlite3", timezone=str(self.zone))
        self.kitchen = SharedKitchen(local, self.household.root / "shopping.sqlite3")
        self.alarms = Alarms(state / "alarms.sqlite3", timezone=str(self.zone))
        self.alarm_ready = False
        self.call_ready = False

    @classmethod
    def load(cls, path: Path, state: Path):
        return cls(read_config(path), state)

    def tools(self):
        return kitchen_tools() + alarm_tools() + room_tools()

    def context(self):
        return {
            "local_time": datetime.now(self.zone).isoformat(),
            "room": self.household.rooms[self.household.room],
            "registered_rooms": list(self.household.rooms.values()),
        }

    async def cache_call_prompts(self, dialogue):
        """Explicit preparation only: static Cedar caller names, never a recording."""
        from .cloud_voice import cedar

        directory = self.household.root / "call-prompts"
        if directory.is_symlink():
            raise ValueError("Call prompt directory cannot be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        for room, name in self.household.rooms.items():
            path = directory / (room + ".pcm")
            if path.exists() or path.is_symlink():
                raise ValueError("Preserve existing cached prompts before replacing them")
            dialogue.quota.reserve()
            text = f"Incoming call from {name}."
            pcm = await cedar(dialogue.config, dialogue.key, text, lambda: None)
            if not pcm or len(pcm) % 2 or len(pcm) > 24000 * 2 * 8:
                raise ValueError("Call prompt exceeds playback bounds")
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(pcm)
            await __import__("asyncio").sleep(2.1)  # Respect the per-resident request-rate gate.

    def fast_path(self, text):
        alarm = alarm_parse(text, bool(self.alarms.ringing()))
        if alarm is not None:
            return "alarm_clock", alarm
        return kitchen_parse(text) or room_parse(text)

    async def execute(self, name, args, guard, utterance=""):
        guard()
        if not isinstance(args, dict):
            return {"message": "I couldn't validate that household request. Nothing was changed."}
        action = args.get("action")
        reading = action in {"status", "read"}
        if not reading and not write_intent(utterance):
            return {"message": "Please ask directly for the household change. Nothing was changed."}
        try:
            if name == "room_call" and action == "start" and not self.call_ready:
                return {
                    "message": "Calling needs the patched microphone stream and spoken call controls enabled. No call was opened."
                }
            if name == "alarm_clock":
                if not self.alarm_ready and action != "status":
                    return {
                        "message": "This satellite needs the native alarm patch before setting alarms."
                    }
                message = self.alarms.handle(args, guard)
            elif name in {"room_call", "room_announcement", "feature_request"}:
                message = self.household.handle(name, args, guard)
            else:
                if name == "named_timer":
                    if set(args) != {"action", "name", "seconds"}:
                        raise ValueError("Invalid timer fields")
                    name, args = (
                        "kitchen_task",
                        {**args, "kind": "timer", "when": None, "message": None},
                    )
                elif name == "reminder":
                    if set(args) != {"action", "name", "seconds", "when", "message"}:
                        raise ValueError("Invalid reminder fields")
                    name, args = "kitchen_task", {**args, "kind": "reminder"}
                message = self.kitchen.handle(name, args, guard)
            guard()
            return {"message": message}
        except (TypeError, ValueError):
            # Do not turn a storage failure into an automatic mutation retry.
            return {
                "message": "That household request needs a clearer time, name, or room. Nothing further was changed."
            }
