#!/usr/bin/env python3
"""Heuristic secret/privacy scan of the Git publication set; never prints matched data."""
from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "credential": re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}"),
    "operator_path": re.compile(r"/(?:home|Users)/[A-Za-z0-9_.-]+/"),
    "household_marker": re.compile("wopr" + "net|Lean" + "der|Moon" + "fin", re.I),
    "coordinates": re.compile(r'''["'](?:latitude|longitude|street_address)["']\s*:\s*(?:[-0-9]|["'])'''),
}
BLOCKED_SUFFIXES = {".bin", ".img", ".so", ".wav", ".pcm", ".raw", ".mp3", ".apk", ".zip", ".onnx", ".tflite", ".sqlite3", ".sqlite", ".db"}


def inspect(relative: str, data: bytes) -> list[str]:
    reasons = []
    path = Path(relative)
    if path.suffix.lower() in BLOCKED_SUFFIXES or any(
        part in {"private", "secrets", "firmware", "toolchain", "models", "state", ".git"}
        for part in path.parts
    ) or path.name.startswith(("local.json", "homeassistant.local.json")) or ".local.json" in path.name or path.name == ".env":
        reasons.append("prohibited_artifact")
    if len(data) > 1_000_000 or b"\x00" in data:
        reasons.append("binary_or_oversized")
    text = data.decode("utf-8", errors="replace")
    for label, pattern in PATTERNS.items():
        if pattern.search(text):
            reasons.append(label)
    for value in re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text):
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            continue
        if ip.is_private and not (ip.is_loopback or ip in ipaddress.ip_network("192.0.2.0/24")
                                 or ip in ipaddress.ip_network("198.51.100.0/24")
                                 or ip in ipaddress.ip_network("203.0.113.0/24")
                                 or ip.is_unspecified):
            reasons.append("private_network_address")
            break
    return reasons


def scan(root: Path):
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
    ).decode().split("\0")
    count, failures = 0, []
    for relative in sorted(set(filter(None, paths))):
        path = root / relative
        if path.is_symlink():
            failures.append((relative, ["symlink"]))
            continue
        if not path.is_file():
            failures.append((relative, ["not_a_regular_file"]))
            continue
        count += 1
        reasons = inspect(relative, path.read_bytes())
        if reasons:
            failures.append((relative, reasons))
    return count, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    count, failures = scan(args.root)
    for relative, reasons in failures:
        print(f"FAIL {relative}: {', '.join(reasons)}")
    print(f"Scanned {count} publication candidates; {len(failures)} findings.")
    print("Ignored files and Git history are outside this scan; review the release file list separately.")
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
