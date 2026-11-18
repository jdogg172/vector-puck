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
        async def check_pulse(kind, colour, **options):
            before = len(lines())
            name = "hassmic_pulse_" + colour
            await peer.send("vector-indicator", dict(kind=kind, duration_ms=300, **options))
            await until(lambda: "-s " + name in lines()[before:])
            await until(lambda: "-u " + name in lines()[before:])

        for kind, colour in [("message", "w0"), ("watch", "02f0"), ("warning", "00f0"),
                             ("advisory", "04f0"), ("flood-advisory", "16f0")]:
            await check_pulse(kind, colour)
        for color, colour in [("white", "w0"), ("cyan", "12f0"), ("green", "08f0"),
                              ("violet", "18f0"), ("amber", "03f0"), ("unknown", "w0")]:
            await check_pulse("message", colour, color=color)
        for pct, level in [(20, 2), (39, 2), (40, 1), (74, 1), (75, 0), (100, 0),
                           (19, 0), (101, 0), (39.5, 0), ("39", 0)]:
            await check_pulse("watch", f"02f{level}", brightness_pct=pct, color="violet")
        await check_pulse("message", "12f1", color="cyan", brightness_pct=55)
        await check_pulse("updated", "08f2", color="violet", brightness_pct=100)

        for kind, first in [("flood-watch", "02f0"), ("flood-warning", "00f0")]:
            before = len(lines())
            await peer.send("vector-indicator", dict(kind=kind, duration_ms=6200, color="violet"))
            await until(lambda: "-s hassmic_pulse_" + first in lines()[before:])
            await until(lambda: "-s hassmic_pulse_16f0" in lines()[before:])
            # Renew while blue: the pulse phase must not reset to the first colour.
            renewed = len(lines())
            await peer.send("vector-indicator", dict(kind=kind, duration_ms=3500, brightness_pct=50))
            await peer.barrier()
            await until(lambda: "-s hassmic_pulse_16f1" in lines()[renewed:])
            await asyncio.sleep(0.3)
            renewed_first = first[:-1] + "1"
            assert "-s hassmic_pulse_" + renewed_first not in lines()[renewed:]
            await until(lambda: "-s hassmic_pulse_" + renewed_first in lines()[renewed:])
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
    print("PASS: five message colors, white fallback, brightness boundaries, fixed weather colors, flood sequence/renewal, update isolation and mute priority")


if __name__ == "__main__":
    if len(sys.argv) == 2:
        os.execvp("unshare", ["unshare", "-Urn", "sh", "-c", 'ip link set lo up && exec "$@"',
                             "sh", sys.executable, str(Path(__file__).resolve()), sys.argv[1], "isolated"])
    asyncio.run(main(Path(sys.argv[1]).resolve()))
