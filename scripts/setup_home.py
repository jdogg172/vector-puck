#!/usr/bin/env python3
"""Prepare optional Home Assistant mapping in dry-run mode; never read a token."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vector_puck.config import Config  # noqa: E402
from vector_puck.home_control import HomeControl  # noqa: E402


def private_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o777 not in (0o600, 0o400) or info.st_size > 65_536):
            raise ValueError("Local setup files must be private regular files owned by the operator")
        return stream.read(65_537)


def atomic_write(path: Path, data: bytes):
    fd, temporary = tempfile.mkstemp(prefix=".home-setup-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def setup(root: Path, url: str, token_file: Path) -> Path:
    root = root.resolve()
    if (not token_file.is_absolute() or token_file.is_symlink()
            or token_file.resolve().is_relative_to(root)
            or token_file.resolve().is_relative_to(ROOT)
            or any(c in str(token_file) for c in "\n\r'$\\")):
        raise ValueError("Use an external absolute token path without special characters or symlinks")
    # Metadata only: the credential is never opened by this helper.
    info = token_file.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o777 not in (0o600, 0o400)):
        raise ValueError("Token file must be owned by the operator with mode 0600 or 0400")
    config_dir = root / "config"
    if config_dir.is_symlink() or (root / ".cache").is_symlink():
        raise ValueError("Setup directories must not be symlinks")
    local, env = config_dir / "local.json", root / ".env"
    old = {local: private_file(local), env: private_file(env)}
    config = Config.load(local)
    mapping = config_dir / "homeassistant.local.json"
    env_text = old[env].decode()
    if (config.home_assistant_file or mapping.exists() or mapping.is_symlink()
            or any(line.lstrip().startswith("HOME_ASSISTANT_TOKEN_FILE=")
                   for line in env_text.splitlines())):
        raise ValueError("Home Assistant setup already exists; edit the local files directly")
    home = HomeControl.read_config(ROOT / "config/homeassistant.example.json")
    home["base_url"] = url
    HomeControl.validate_config(home)
    config = replace(config, home_assistant_file="/config/homeassistant.local.json")
    outputs = {
        mapping: (json.dumps(home, indent=2) + "\n").encode(),
        local: (json.dumps(asdict(config), indent=2) + "\n").encode(),
        env: (env_text.rstrip() + f"\nHOME_ASSISTANT_TOKEN_FILE='{token_file}'\n").encode(),
    }
    cache = root / ".cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="home-setup-backup-", dir=cache))
    for path, data in old.items():
        atomic_write(backup / path.name, data)
    changed = []
    try:
        for path, data in outputs.items():
            # Recheck the reviewed inputs immediately before replacing them.
            if path in old and private_file(path) != old[path]:
                raise ValueError("Configuration changed during setup")
            if path == mapping and (path.exists() or path.is_symlink()):
                raise ValueError("Mapping appeared during setup")
            atomic_write(path, data)
            changed.append(path)
    except Exception:
        try:
            for path in reversed(changed):
                if path in old:
                    atomic_write(path, old[path])
                else:
                    path.unlink(missing_ok=True)
        except OSError:
            raise RuntimeError(f"Restoration incomplete; private backups retained at {backup}") from None
        raise
    print("Prepared an ignored Home Assistant mapping in dry-run mode.")
    print("Review its example entity IDs, aliases, sources, units and limits before starting.")
    print(f"Previous local files retained at {backup}")
    print("No token contents, network, devices, microphone or paid API were accessed.")
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        setup(args.root, args.url, args.token_file)
    except RuntimeError as exc:
        parser.exit(1, str(exc) + "\n")
    except (OSError, ValueError):
        parser.exit(1, "Setup failed: check existing private local config, the LAN origin and external token-file permissions.\n")


if __name__ == "__main__":
    main()
