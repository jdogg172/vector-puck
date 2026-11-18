"""Expiring visual update status. Only the trusted updater marks completion."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid

from .ring_signals import WEATHER_KINDS


class UpdateIndicator:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "update-indicator.json"

    def read(self, *, now=None):
        now = time.time() if now is None else now
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
                    return None
                value = json.load(stream)
            if not isinstance(value, dict) or set(value) != {"id", "phase", "started", "until"}:
                return None
            if value["phase"] not in {"updating", "complete"}:
                return None
            if not isinstance(value["id"], str) or len(value["id"]) != 32:
                return None
            if any(type(value[k]) not in (float, int) or not math.isfinite(value[k]) for k in ("started", "until")):
                return None
            limit = 900 if value["phase"] == "updating" else 15
            if not value["started"] <= now < value["until"] <= value["started"] + limit:
                return None
            return value
        except (OSError, ValueError, TypeError):
            return None

    def _write(self, value):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def begin(self, seconds=300, *, now=None):
        if type(seconds) is not int or not 1 <= seconds <= 900:
            raise ValueError("Update status must expire within 15 minutes")
        now = time.time() if now is None else now
        ident = uuid.uuid4().hex
        self._write(dict(id=ident, phase="updating", started=now, until=now + seconds))
        return ident

    def complete(self, ident, *, now=None):
        now = time.time() if now is None else now
        previous = self.read(now=now)
        if not previous or previous["id"] != ident or previous["phase"] != "updating":
            raise ValueError("No matching active update; completion refused")
        self._write(dict(id=ident, phase="complete", started=now, until=now + 8))

    def clear(self):
        self.path.unlink(missing_ok=True)

    def indicator(self, *, now=None):
        now = time.time() if now is None else now
        value = self.read(now=now)
        if value is None:
            return {"kind": "none"}
        return {"kind": "update" if value["phase"] == "updating" else "updated",
                "duration_ms": max(1, math.ceil((value["until"] - now) * 1000))}


def choose_indicator(*, weather="none", call=False, alarm=False, messages=False, update=None):
    """Urgent and actionable states win; physical mute is enforced on the device."""
    if isinstance(weather, dict):
        if weather.get("kind") in WEATHER_KINDS:
            return weather
    elif weather in WEATHER_KINDS:
        return {"kind": weather}
    if call or alarm:
        return {"kind": "none"}  # Native call/listen/alarm animation owns the LEDs.
    if messages:
        return {"kind": "message", "duration_ms": 60000}
    return update or {"kind": "none"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["begin", "complete", "clear", "status"])
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--id")
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    state = UpdateIndicator(args.state)
    if args.action == "begin":
        print(state.begin(args.seconds))
    elif args.action == "complete":
        state.complete(args.id)
    elif args.action == "clear":
        state.clear()
    else:
        print(json.dumps(state.indicator()))


if __name__ == "__main__":
    main()
