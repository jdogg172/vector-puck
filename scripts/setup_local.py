#!/usr/bin/env python3
"""Write local operator configuration; never read, create or copy a credential."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vector_puck.config import Config  # noqa: E402


def setup(root: Path, puck_host: str, key_file: Path):
    root = root.resolve()
    if not key_file.is_absolute() or key_file.is_symlink():
        raise ValueError("Key file must be an absolute path without a symlink")
    key_file = key_file.resolve()
    if key_file.is_relative_to(ROOT) or key_file.is_relative_to(root):
        raise ValueError("Keep the key file outside the repository")
    # Metadata only. The key contents are never opened by setup.
    info = key_file.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
            or info.st_uid != os.geteuid()):
        raise ValueError("Key file must be owned by the operator and have mode 0600 or 0400")
    if any(c in str(key_file) for c in "\n\r'$\\"):
        raise ValueError("Use a simple key path without quotes, dollar signs or backslashes")
    config = Config(puck_host=puck_host)
    config_dir = root / "config"
    if config_dir.is_symlink():
        raise ValueError("Config directory must not be a symlink")
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    targets = {config_dir / "local.json": json.dumps(asdict(config), indent=2) + "\n",
               root / ".env": f"OPENAI_KEY_FILE='{key_file}'\nLOCAL_UID={os.getuid()}\nLOCAL_GID={os.getgid()}\n"}
    if any(p.exists() or p.is_symlink() for p in targets):
        raise ValueError("Local configuration exists; edit it directly instead of overwriting")
    created = []
    try:
        for path, content in targets.items():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created.append(path)
            with os.fdopen(fd, "w") as output:
                output.write(content)
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    print("Wrote config/local.json and .env with the external key path and operator UID/GID.")
    print("No key contents, device, microphone or API were accessed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--puck-host", required=True)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        setup(args.root, args.puck_host, args.key_file)
    except (OSError, ValueError):
        parser.exit(1, "Setup failed: use a local IPv4, an external private key file, and absent local config.\n")


if __name__ == "__main__":
    main()
