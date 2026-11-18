"""Real native C ring scheduling with isolated fake LEDs, PCM and privacy latch."""
import asyncio
import os
from pathlib import Path
import sys

from native_mute_regression import daemon


async def until(condition, seconds=5):
    for _ in range(int(seconds / 0.05)):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("Expected native LED pattern was not emitted")


async def main(binary):
    async with daemon(binary, muted=False, led_ring=True) as (device, peer):
        log = device.root / "ledctrl.log"

        def lines():
            return log.read_text().splitlines() if log.exists() else []

        def active_pulses():
            active = set()
            for line in lines():
                if line.startswith("-s hassmic_pulse_"):
                    active.add(line[3:])
                elif line.startswith("-u hassmic_pulse_"):
                    active.discard(line[3:])
            return active

        await peer.send("run-satellite")
        await peer.send("describe")
        _, info = await peer.expect("info")
        assert info[1]["satellite"]["vector_weather_indicator"]
        for kind, colour in [("message", "w2"), ("watch", "02f2"), ("warning", "00f2"),
                             ("advisory", "04f2"), ("flood-advisory", "16f2")]:
            before = len(lines())
            name = "hassmic_pulse_" + colour
            await peer.send("vector-indicator", dict(kind=kind, duration_ms=700))
            await until(lambda: "-s " + name in lines()[before:])
            await until(lambda: "-u " + name in lines()[before:])
        for kind, first in [("flood-watch", "02f2"), ("flood-warning", "00f2")]:
            before = len(lines())
            await peer.send("vector-indicator", dict(kind=kind, duration_ms=6200))
            await until(lambda: "-s hassmic_pulse_" + first in lines()[before:])
            await until(lambda: "-s hassmic_pulse_16f2" in lines()[before:])
            # Renew while blue: the pulse phase must not reset to the first colour.
            renewed = len(lines())
            await peer.send("vector-indicator", dict(kind=kind, duration_ms=3500))
            await peer.barrier()
            await asyncio.sleep(0.3)
            assert "-s hassmic_pulse_" + first not in lines()[renewed:]
            await until(lambda: "-s hassmic_pulse_" + first in lines()[renewed:])
            await until(lambda: not active_pulses())
            await peer.send("vector-indicator", {"kind": "none"})
            await peer.barrier()
        # Any status light must yield immediately to the real privacy latch.
        await peer.send("vector-indicator", dict(kind="flood-watch", duration_ms=90000))
        await device.mute(peer, True)
        before = len(lines())
        await peer.send("vector-indicator", dict(kind="flood-warning", duration_ms=3000))
        await asyncio.sleep(2.7)
        assert not any(x.startswith("-s hassmic_pulse_") for x in lines()[before:])
        assert (device.root / "privacy/state").read_text().strip() == "1"
        await peer.quiet(len(peer.events), "Weather status must never enable microphones")
    print("PASS: native white/orange/red/advisory colours, blue flood sequences, lease expiry/renewal and physical mute priority")


if __name__ == "__main__":
    if len(sys.argv) == 2:
        os.execvp("unshare", ["unshare", "-Urn", "sh", "-c", 'ip link set lo up && exec "$@"',
                             "sh", sys.executable, str(Path(__file__).resolve()), sys.argv[1], "isolated"])
    asyncio.run(main(Path(sys.argv[1]).resolve()))
