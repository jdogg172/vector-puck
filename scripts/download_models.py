#!/usr/bin/env python3
"""Fetch only pinned local ASR assets; never contacts OpenAI or a puck."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def safe_target(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Unsafe model path")
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError("Model paths must not be symlinks")
        if part == root:
            break
    return path


def valid(path: Path, spec: dict) -> bool:
    return (not path.is_symlink() and path.is_file() and path.stat().st_size == spec["size"]
            and digest(path) == spec["sha256"])


def fetch(spec: dict, target: Path):
    if valid(target, spec):
        return
    if urlsplit(spec["url"]).scheme != "https":
        raise ValueError("Downloads require HTTPS")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".part")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        total = 0
        with os.fdopen(fd, "wb") as output:
            with build_opener(ProxyHandler({})).open(spec["url"], timeout=60) as response:
                if urlsplit(response.url).scheme != "https":
                    raise ValueError("Refusing non-HTTPS redirect")
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > spec["size"]:
                        raise ValueError("Model download exceeds pinned size")
                    output.write(block)
        if not valid(temp, spec):
            raise ValueError("Model checksum mismatch")
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)


def verify(destination: Path, lock: dict):
    for model in ("vosk", "whisper"):
        root = safe_target(destination, lock[model]["directory"])
        for spec in lock[model]["files"]:
            if not valid(safe_target(root, spec["path"]), spec):
                raise ValueError(f"Missing or mismatched local {model} artifact: {spec['path']}")


def download(destination: Path, lock: dict):
    destination.mkdir(parents=True, exist_ok=True)
    archive = safe_target(destination, ".vosk-download.zip")
    vosk = lock["vosk"]
    vosk_root = safe_target(destination, vosk["directory"])
    if not all(valid(safe_target(vosk_root, f["path"]), f) for f in vosk["files"]):
        print("Downloading pinned Vosk wake/endpoint model", flush=True)
        fetch(vosk, archive)
        try:
            with zipfile.ZipFile(archive) as source:
                for spec in vosk["files"]:
                    member = source.getinfo(vosk["directory"] + "/" + spec["path"])
                    if stat.S_ISLNK(member.external_attr >> 16) or member.file_size != spec["size"]:
                        raise ValueError("Unsafe Vosk archive entry")
                    target = safe_target(vosk_root, spec["path"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as output:
                        output.write(source.read(member))
                    if not valid(target, spec):
                        raise ValueError("Extracted Vosk checksum mismatch")
        finally:
            archive.unlink(missing_ok=True)
    whisper_root = safe_target(destination, lock["whisper"]["directory"])
    for spec in lock["whisper"]["files"]:
        print(f"Checking pinned Whisper artifact: {spec['path']}", flush=True)
        fetch(spec, safe_target(whisper_root, spec["path"]))
    verify(destination, lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "models")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        destination = args.destination.absolute()
        if any(p.is_symlink() for p in [destination, *destination.parents]):
            raise ValueError("Model destination must not contain symlinks")
        lock = json.loads((ROOT / "config/models.lock.json").read_text())
        if args.verify_only:
            verify(destination, lock)
        else:
            download(destination, lock)
        print("Local model checksums verified. No microphone or API access occurred.")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        parser.exit(1, "Model verification/download failed; inspect destination and pinned manifest.\n")


if __name__ == "__main__":
    main()
