#!/usr/bin/env python3
"""Prepare private multi-room files; never access devices, microphones or keys."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vector_puck.config import Config  # noqa: E402


def prepare(root: Path, rooms: list[str], timezone: str):
    ZoneInfo(timezone)
    if not 2 <= len(rooms) <= 16:
        raise ValueError("Configure two to sixteen rooms")
    entries, configs = [], {}
    models = {"biscuit": "echo-dot2-rs03qr-biscuit", "radar": "echo2-xc56py-radar"}
    for value in rooms:
        parts = value.split(":")
        if len(parts) != 3 or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", parts[0]):
            raise ValueError("Each --room takes id:local-ipv4:biscuit-or-radar")
        room, host, model = parts
        if (
            room in configs
            or model not in models
            or any(c.puck_host == host for c in configs.values())
        ):
            raise ValueError("Each room needs a unique id and satellite address")
        configs[room] = Config(
            hardware=models[model],
            puck_host=host,
            household_file=f"/config/{room}.household.local.json",
        )
        entries.append({"id": room, "name": room.replace("-", " ").title(), "aliases": []})
    outputs = {}
    services = {}
    for room, config in configs.items():
        outputs[root / "config" / (room + ".local.json")] = asdict(config)
        outputs[root / "config" / (room + ".household.local.json")] = {
            "root": "/household",
            "room": room,
            "rooms": entries,
            "timezone": timezone,
        }
        services["vector-" + room] = {
            "build": ".",
            "image": "vector-puck:local",
            "restart": "unless-stopped",
            "init": True,
            "user": "${LOCAL_UID:?Run scripts/setup_local.py}:${LOCAL_GID:?Run scripts/setup_local.py}",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": "2g",
            "pids_limit": 128,
            "stop_grace_period": "10s",
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=64m"],
            "secrets": ["openai_key"],
            "volumes": [
                f"./config/{room}.local.json:/config/local.json:ro",
                f"./config/{room}.household.local.json:/config/{room}.household.local.json:ro",
                "./models:/models:ro",
                f"./state/rooms/{room}:/state:rw",
                "./state/household:/household:rw",
            ],
            "healthcheck": {
                "test": [
                    "CMD",
                    "python",
                    "-c",
                    "import json,time;from pathlib import Path;p=Path('/state/health.json');x=json.loads(p.read_text());assert time.time()-p.stat().st_mtime<30 and x['connected'] and (not x.get('spoken_stop') or x.get('interrupt_worker_running'))",
                ],
                "interval": "15s",
                "timeout": "5s",
                "start_period": "60s",
                "retries": 2,
            },
            "logging": {"driver": "json-file", "options": {"max-size": "2m", "max-file": "2"}},
        }
    outputs[root / "compose.household.local.json"] = {
        "services": services,
        "secrets": {
            "openai_key": {
                "file": "${OPENAI_KEY_FILE:?Run scripts/setup_local.py with an external private key file}"
            }
        },
    }
    if any(path.exists() or path.is_symlink() for path in outputs):
        raise ValueError(
            "Private household files already exist; preserve/edit them instead of overwriting"
        )
    directories = [root / "config", root / "state" / "household"]
    directories += [root / "state" / "rooms" / room for room in configs]
    for path in directories:
        if path.is_symlink():
            raise ValueError("Private directories cannot be symlinks")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    created = []
    try:
        for path, data in outputs.items():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, indent=2)
                stream.write("\n")
            created.append(path)
    except BaseException:
        for path in created:
            path.unlink()
        raise
    return len(configs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--room",
        action="append",
        required=True,
        help="id:local-ipv4:biscuit-or-radar; repeat for every room",
    )
    parser.add_argument("--timezone", required=True, help="IANA timezone, e.g. Europe/London")
    args = parser.parse_args()
    try:
        count = prepare(ROOT, args.room, args.timezone)
    except (ValueError, OSError, KeyError):
        print("Setup failed: check room syntax, timezone, permissions and existing private files.")
        raise SystemExit(1) from None
    print(f"Prepared {count} private room configurations and compose.household.local.json.")
    print("No devices, microphones, credentials or paid API were accessed. Review before starting.")


if __name__ == "__main__":
    main()
