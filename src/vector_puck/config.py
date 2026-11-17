"""Strict configuration; no arbitrary endpoints, commands, plugins, or secret values."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import ipaddress
import json
from pathlib import Path
import re


@dataclass(frozen=True)
class Config:
    hardware: str = "echo-dot2-rs03qr-biscuit"
    puck_host: str = "192.0.2.10"
    puck_port: int = 16700
    wake_phrase: str = "hey vector"
    vosk_model: str = "/models/vosk-model-small-en-us-0.15"
    whisper_model: str = "/models/faster-whisper-base.en"
    cpu_threads: int = 4
    conversation_model: str = "gpt-6-astra"
    voice_model: str = "gpt-realtime-2.1-mini"
    voice: str = "cedar"
    key_file: str = "/run/secrets/openai_key"
    followup_seconds: int = 8
    followup_turns: int = 3
    session_seconds: int = 180
    history_seconds: int = 1200
    max_request_chars: int = 800
    hourly_requests: int = 30
    daily_requests: int = 100
    working_sound: bool = True
    experimental_spoken_stop: bool = True
    home_assistant_file: str = ""
    household_file: str = ""

    def __post_init__(self):
        if self.hardware not in {"echo-dot2-rs03qr-biscuit", "echo2-xc56py-radar"}:
            raise ValueError("Use a documented biscuit or radar satellite")
        if not isinstance(self.puck_host, str):
            raise ValueError("puck_host must be an IPv4 string")
        ip = ipaddress.ip_address(self.puck_host)
        if ip.version != 4 or ip.is_global or ip.is_multicast or ip.is_unspecified:
            raise ValueError("puck_host must be an explicit local IPv4 address")
        for name, low, high in [
            ("puck_port", 1024, 65535), ("cpu_threads", 1, 16),
            ("followup_seconds", 0, 15), ("followup_turns", 0, 5),
            ("session_seconds", 10, 300), ("history_seconds", 0, 1200),
            ("max_request_chars", 1, 800), ("hourly_requests", 1, 60),
            ("daily_requests", 1, 500),
        ]:
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid {name}")
        for name in ("conversation_model", "voice_model"):
            if not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", getattr(self, name)):
                raise ValueError(f"Invalid {name}")
        if self.voice != "cedar":
            raise ValueError("This starter supports Cedar only")
        if not re.fullmatch(r"[a-z]+(?: [a-z]+){1,3}", self.wake_phrase):
            raise ValueError("wake_phrase must contain 2..4 lowercase English words")
        for name in ("vosk_model", "whisper_model", "key_file"):
            if not isinstance(getattr(self, name), str) or not Path(getattr(self, name)).is_absolute():
                raise ValueError(f"{name} must be an absolute path")
        if any(type(getattr(self, name)) is not bool
               for name in ("working_sound", "experimental_spoken_stop")):
            raise ValueError("Voice options must be booleans")
        if (not isinstance(self.home_assistant_file, str)
                or (self.home_assistant_file and not Path(self.home_assistant_file).is_absolute())):
            raise ValueError("home_assistant_file must be empty or an absolute path")
        if (not isinstance(self.household_file, str)
                or (self.household_file and not Path(self.household_file).is_absolute())):
            raise ValueError("household_file must be empty or an absolute path")

    @classmethod
    def load(cls, path: Path) -> Config:
        def pairs(entries):
            result = {}
            for key, value in entries:
                if key in result:
                    raise ValueError("Duplicate configuration field")
                result[key] = value
            return result
        data = json.loads(path.read_text(), object_pairs_hook=pairs)
        allowed = {f.name for f in fields(cls)}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("Unknown configuration field")
        try:
            return cls(**data)
        except TypeError:
            raise ValueError("Invalid configuration types") from None

    def validate_runtime(self):
        if ipaddress.ip_address(self.puck_host) in ipaddress.ip_network("192.0.2.0/24"):
            raise ValueError("Set the actual puck_host in config/local.json")
        for name in ("vosk_model", "whisper_model"):
            if not Path(getattr(self, name)).is_dir():
                raise ValueError(f"Download the local {name} before startup")


def example() -> dict:
    return asdict(Config())
