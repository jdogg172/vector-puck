import asyncio
import os
import sys
from pathlib import Path
from native_mute_regression import daemon


async def main(binary):
    async with daemon(binary, muted=True) as (device, peer):
        await peer.send("describe")
        _, e = await peer.expect("info")
        assert e[1]["satellite"]["vector_indicator"] and e[1]["satellite"]["vector_alarm"]
        assert e[1]["satellite"]["vector_update_indicator"]
        await peer.send("run-satellite")
        ident = "a" * 32
        for action in ["button", "voice"]:
            after = len(peer.events)
            await peer.send(
                "vector-alarm", {"action": "start", "id": ident, "name": "SYNTHETIC TEST"}
            )
            await peer.expect("vector-alarm-state", after, lambda e: e[1]["ringing"])
            await peer.quiet(after, "native alarm must not bypass microphone mute")
            if action == "button":
                device.key(138)
            else:
                await peer.send("vector-stop")
            await peer.expect("vector-alarm-state", after, lambda e: not e[1]["ringing"])
            assert (device.root / "state/last-alarm-dismissed").read_text() == ident
            assert (device.root / "privacy/state").read_text().strip() == "1"
            after = len(peer.events)
            await peer.send("describe")
            _, e = await peer.expect("info", after)
            assert e[1]["satellite"]["vector_alarm_dismissed"] == ident
        for kind in ["message", "watch", "warning", "advisory", "update", "updated", "none", "invalid"]:
            await peer.send("vector-indicator", {"kind": kind})
        await peer.barrier()
        await peer.quiet(len(peer.events), "indicator must not enable microphone")
    print(
        "PASS: actual native alarm state, repeat-until-stopped configuration, Action/stop dismissal persistence, indicator messages and hardware microphone mute"
    )


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
                str(Path(__file__).resolve()),
                sys.argv[1],
                "isolated",
            ],
        )
    asyncio.run(main(Path(sys.argv[1]).resolve()))
