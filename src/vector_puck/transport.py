"""Bounded Wyoming framing, compatible with the pinned hassmic C implementation."""
from __future__ import annotations

import asyncio
import json

MIC_FORMAT = {"rate": 16000, "width": 2, "channels": 1}
REPLY_FORMAT = {"rate": 24000, "width": 2, "channels": 1}


async def read_event(reader: asyncio.StreamReader) -> tuple[str, dict, bytes]:
    line = await reader.readline()
    if not line:
        raise EOFError("Satellite disconnected")
    if len(line) > 4096 or not line.endswith(b"\n"):
        raise ValueError("Invalid Wyoming header")
    header = json.loads(line)
    if not isinstance(header, dict) or not isinstance(header.get("type"), str):
        raise ValueError("Invalid Wyoming event")
    if not 1 <= len(header["type"]) <= 100:
        raise ValueError("Invalid event type")
    data_length, payload_length = header.get("data_length", 0), header.get("payload_length", 0)
    for size, maximum in ((data_length, 8192), (payload_length, 65536)):
        if type(size) is not int or not 0 <= size <= maximum:
            raise ValueError("Invalid Wyoming length")
    data = header.get("data", {})
    if data_length:
        data = json.loads(await reader.readexactly(data_length))
    if not isinstance(data, dict):
        raise ValueError("Invalid Wyoming data")
    return header["type"], data, await reader.readexactly(payload_length)


async def send_event(writer, kind: str, data: dict | None = None, payload: bytes = b""):
    if len(payload) > 65536:
        raise ValueError("Oversized outgoing payload")
    header = {"type": kind}
    if data:
        header["data"] = data
    if payload:
        header["payload_length"] = len(payload)
    writer.write(json.dumps(header, separators=(",", ":")).encode() + b"\n" + payload)
    await asyncio.wait_for(writer.drain(), 5)


def check_audio(data: dict, payload: bytes):
    if any(type(data.get(k)) is not int or data[k] != v for k, v in MIC_FORMAT.items()):
        raise ValueError("Expected 16 kHz mono signed 16-bit little-endian microphone audio")
    if not payload or len(payload) % 2:
        raise ValueError("Invalid PCM samples")
