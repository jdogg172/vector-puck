"""Actual C daemon with synthetic PCM, input events and radar privacy latch only.

Re-executes in a private network namespace with loopback only.
Each daemon also gets an empty /dev and a synthetic /sys through bubblewrap.
No Echo, microphone, existing listener, ASR model or update key is used.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time


AUDIO = {"rate": 24000, "width": 2, "channels": 1}
PRIVACY = "/sys/devices/soc/10010000.keypad/amz_privacy"


class Peer:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.events = []
        self.changed = asyncio.Condition()
        self.error = None
        self.task = asyncio.create_task(self.collect())

    async def collect(self):
        try:
            while True:
                header = json.loads(await self.reader.readline())
                data = header.get("data", {})
                if header.get("data_length"):
                    data = json.loads(await self.reader.readexactly(header["data_length"]))
                payload = await self.reader.readexactly(header.get("payload_length", 0))
                async with self.changed:
                    self.events.append((header["type"], data, payload))
                    self.changed.notify_all()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            async with self.changed:
                self.error = error
                self.changed.notify_all()

    async def send(self, kind, data=None, payload=b""):
        header = {"type": kind}
        if data:
            header["data"] = data
        if payload:
            header["payload_length"] = len(payload)
        self.writer.write(json.dumps(header).encode() + b"\n" + payload)
        await self.writer.drain()

    async def expect(self, kind, after=0, predicate=lambda event: True, timeout=3):
        async def wait():
            async with self.changed:
                while True:
                    for index in range(after, len(self.events)):
                        event = self.events[index]
                        if event[0] == kind and predicate(event):
                            return index, event
                    if self.error:
                        raise self.error
                    await self.changed.wait()

        return await asyncio.wait_for(wait(), timeout)

    async def barrier(self):
        after = len(self.events)
        await self.send("ping")
        index, _ = await self.expect("pong", after)
        return index + 1

    async def quiet(self, after, label):
        await asyncio.sleep(0.3)
        end = await self.barrier()
        leaked = [
            event[0]
            for event in self.events[after:end]
            if event[0] in {"audio-chunk", "run-pipeline"}
        ]
        assert not leaked, f"{label}: microphone traffic while muted/paused: {leaked}"

    async def microphone(self, after):
        await self.expect("audio-chunk", after, lambda event: any(event[2]))

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.writer.close()
        await self.writer.wait_closed()


class Daemon:
    def __init__(self, root, proc, keys):
        self.root, self.proc, self.keys = root, proc, keys

    def latch(self, muted):
        temporary = self.root / "privacy" / "state.new"
        temporary.write_text("1\n" if muted else "0\n")
        temporary.replace(temporary.with_name("state"))

    def key(self, code):
        for value in (1, 0):
            os.write(self.keys, struct.pack("@llHHi", 0, 0, 1, code, value))

    async def mute(self, peer, muted):
        after = len(peer.events)
        self.latch(muted)
        self.key(113)  # KEY_MUTE: real buttons.c reads the synthetic latch.
        index, _ = await peer.expect("vector-mute", after, lambda event: event[1]["muted"] is muted)
        return index + 1


def prepare_test_ring(binary, root, env):
    """Generate a ring in an empty synthetic tree; never touch system LEDs."""
    import shutil
    etc = root / "led-root"
    resources = etc / "led-resources"
    resources.mkdir(parents=True)
    layers = {"5": ["volume_step-05"], "4": ["active-end", "active-start", "anim_start_error_short", "active-thinking", "active-talking"],
              "3": ["ready-timer"], "2": ["call_incoming"], "1": ["mics-off_on"], "0": ["zzz_rainbow"]}
    for names in layers.values():
        for name in names:
            (resources / (name + ".animation")).write_text("loop\n1000:" + ",".join(["F00"] * 12) + "\n")
    config = json.dumps(dict(numberOfLayers=6, Layers=layers))
    (resources / "layer_config_common.json").write_text(config)
    (etc / "layer_config_common.json.pre-hassmic").write_text(config)
    env.update(HASSMIC_LED_ROOT=str(etc), HASSMIC_FAKE_LED=str(root / "ledctrl.log"))
    generated = root / "generated-ring"
    generated.mkdir()
    subprocess.run([str(binary), "-G", str(generated)], env=env, check=True, capture_output=True)
    for file in generated.iterdir():
        shutil.copy2(file, (etc / "hassmic-leds.digest") if file.name == "digest" else (resources / file.name))
    (root / "state/adopted").write_text("ha\n")


@asynccontextmanager
async def daemon(binary, *, muted=True, soft=False, led_ring=False):
    with tempfile.TemporaryDirectory(prefix="vector-mute-") as temporary:
        root = Path(temporary)
        (root / "state").mkdir()
        (root / "privacy").mkdir()
        (root / "privacy" / "state").write_text("1\n" if muted else "0\n")
        (root / "state" / "config").write_text(f"mute={'on' if soft else 'off'}\neven_volume=off\n")
        # Continuous nonzero synthetic audio: a leaking sender cannot hide behind silence.
        pcm = b"".join(
            struct.pack("<h", int(2000 * math.sin(2 * math.pi * 440 * i / 16000)))
            for i in range(16000)
        )
        (root / "capture.raw").write_bytes(pcm)
        fifo = root / "keypad"
        os.mkfifo(fifo, 0o600)
        keys = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        env = {
            "PATH": os.defpath,
            "HASSMIC_STATE": str(root / "state"),
            "HASSMIC_CAP": str(root / "capture.raw"),
            "HASSMIC_PLAY": str(root / "play.raw"),
            "HASSMIC_FAKE_AFE": "0:100:10",
        }
        if led_ring:
            prepare_test_ring(binary, root, env)
        command = [
            "bwrap",
            "--die-with-parent",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--tmpfs",
            "/sys",
            "--proc",
            "/proc",
            "--bind",
            str(root),
            str(root),
            "--dir",
            PRIVACY,
            "--ro-bind",
            str(root / "privacy"),
            PRIVACY,
            "--",
            str(binary),
            "-P",
            "wyoming",
            "-p",
            str(port),
            "-w",
            "remote",
            "-b",
            str(fifo),
            "-n",
            "Vector mute test",
            "-z",
            "0",
            "-a",
            "0",
            "-i",
            "0",
            "-B",
            "-W",
            "0",
            "-o",
            "0",
        ]
        peer = None
        with (root / "daemon.log").open("w") as log:
            proc = subprocess.Popen(command, env=env, stdout=log, stderr=log)
            device = Daemon(root, proc, keys)
            try:
                for _ in range(100):
                    if proc.poll() is not None:
                        raise AssertionError((root / "daemon.log").read_text())
                    try:
                        reader, writer = await asyncio.open_connection("127.0.0.1", port)
                        peer = Peer(reader, writer)
                        break
                    except ConnectionRefusedError:
                        await asyncio.sleep(0.05)
                else:
                    raise AssertionError("Synthetic daemon did not listen")
                yield device, peer
            except Exception:
                print((root / "daemon.log").read_text(), file=sys.stderr)
                raise
            finally:
                if peer:
                    await peer.close()
                if proc.poll() is None:
                    proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    await asyncio.to_thread(proc.wait, timeout=5)
                os.close(keys)


async def resumed(peer, after):
    index, event = await peer.expect("run-pipeline", after)
    assert not event[1].get("vector_barge_in"), "Unmute resumed an old reply"
    await peer.microphone(index + 1)
    await asyncio.sleep(0.15)
    end = await peer.barrier()
    assert sum(event[0] == "run-pipeline" for event in peer.events[after:end]) == 1, (
        "Duplicate resume"
    )


async def begin_reply(peer, chunks=100):
    await peer.send("detection", {"name": "hey_vector"})
    await peer.send("transcript", {"text": ""})
    await peer.send("vector-listen")
    after = len(peer.events)
    await peer.send("audio-start", AUDIO)
    _, event = await peer.expect("run-pipeline", after)
    assert event[1].get("vector_barge_in") is True
    await peer.microphone(after)
    for _ in range(chunks):
        await peer.send("audio-chunk", AUDIO, bytes(4800))


async def main(binary):
    assert os.readlink("/proc/self/ns/net") != sys.argv[2], "Network isolation failed"
    async with daemon(binary) as (device, peer):
        await peer.send("describe")
        _, event = await peer.expect("info")
        assert event[1]["satellite"]["vector_muted"] is True
        await peer.send("run-satellite")
        await peer.send("detection", {"name": "hey_vector"})
        device.key(138)  # KEY_HELP: the real action button handler, while muted.
        await peer.quiet(0, "muted startup and remote run/detection")
        after = await device.mute(peer, False)
        await resumed(peer, after)
        print(
            "PASS: muted startup blocks PCM/pipelines; physical unmute starts one live mic pipeline"
        )

        # No key event: the send boundary must read the latch before its 1 s poll callback.
        device.latch(True)
        after = await peer.barrier()
        await peer.quiet(after, "latch changed before button callback")
        device.key(113)
        await peer.expect("vector-mute", 0, lambda event: event[1]["muted"] is True)
        after = await peer.barrier()
        for kind, data in (
            ("run-satellite", None),
            ("detection", {"name": "hey_vector"}),
            ("transcript", {"text": ""}),
            ("vector-listen", None),
            ("error", {"code": "stt-no-text-recognized"}),
            ("audio-start", AUDIO),
            ("vector-listen", None),
            ("audio-stop", None),
            ("vector-stop", None),
        ):
            await peer.send(kind, data)
        device.key(138)
        await peer.quiet(after, "muted remote events, action and playback completion")
        after = await device.mute(peer, False)
        await resumed(peer, after)
        print(
            "PASS: idle mute and stale remote/playback events cannot transmit PCM or restart capture"
        )

        await begin_reply(peer)
        started = time.monotonic()
        after = await device.mute(peer, True)
        await peer.expect("played", after)
        assert time.monotonic() - started < 1.5, "Mute waited for queued ten-second reply"
        await peer.quiet(after, "mute during reply/Q_STOP race")
        after = await device.mute(peer, False)
        await resumed(peer, after)
        print("PASS: mute cancels a queued reply; playback completion stays muted; unmute resumes")

        await begin_reply(peer, chunks=0)
        played = device.root / "play.raw"
        before = played.stat().st_size if played.exists() else 0
        await peer.send("audio-chunk", AUDIO, bytes(48000))  # A whole second inside play_write.
        for _ in range(100):
            if played.exists() and played.stat().st_size >= before + 40000:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("Playback did not enter the synthetic one-second write")
        after = await device.mute(peer, True)
        await peer.barrier()
        assert not any(event[0] == "played" for event in peer.events[after:]), (
            "Race setup missed playback"
        )
        after = await device.mute(peer, False)
        await peer.barrier()
        assert not any(event[0] in {"played", "run-pipeline"} for event in peer.events[after:]), (
            "Unmute started a second pipeline before pending Q_STOP"
        )
        await peer.expect("played", after)
        await resumed(peer, after)
        print("PASS: unmute before Q_STOP completion resumes exactly once without a lock stall")

        for stop in ("action", "spoken"):
            await begin_reply(peer)
            after = len(peer.events)
            started = time.monotonic()
            if stop == "action":
                device.key(138)
            else:
                await peer.send("vector-stop")
            await peer.expect("vector-cancel", after)
            await peer.expect("played", after)
            await resumed(peer, after)
            assert time.monotonic() - started < 1.5, f"{stop} stop waited for queued reply"
        print(
            "PASS: real Action input and spoken vector-stop still cancel promptly and resume listening"
        )

        after = await device.mute(peer, True)
        await peer.quiet(after, "mute before pause")
        await peer.send("pause-satellite")
        after = await device.mute(peer, False)
        await peer.quiet(after, "unmute must respect pause-satellite")
        after = len(peer.events)
        await peer.send("run-satellite")
        await resumed(peer, after)
        print("PASS: physical unmute respects server pause; run-satellite restores listening")

    async with daemon(binary, muted=False, soft=True) as (device, peer):
        await peer.send("describe")
        _, event = await peer.expect("info")
        assert event[1]["satellite"]["vector_muted"] is True
        await peer.send("run-satellite")
        await peer.send("detection", {"name": "hey_vector"})
        await peer.quiet(0, "software mute with hardware latch off")
        device.latch(True)
        device.key(113)
        await asyncio.sleep(0.2)
        after = await device.mute(peer, False)
        await resumed(peer, after)
        print("PASS: software mute blocks PCM with live synthetic input; physical unmute clears it")


if __name__ == "__main__":
    if len(sys.argv) == 2:
        os.execvp(
            "unshare",
            [
                "unshare",
                "-Urn",
                "sh",
                "-c",
                'ip link set lo up && exec "$@"',
                "sh",
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                sys.argv[1],
                os.readlink("/proc/self/ns/net"),
            ],
        )
    asyncio.run(main(Path(sys.argv[1]).resolve(strict=True)))
