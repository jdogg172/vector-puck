"""Actual LED-ring C implementation with fake LEDs/PCM and an isolated network."""
import asyncio
import os
from pathlib import Path
import sys
from native_mute_regression import daemon


async def until(condition):
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError('Expected LED animation was not emitted')


async def main(binary):
    async with daemon(binary, muted=False, led_ring=True) as (device, peer):
        log = device.root / 'ledctrl.log'
        def lines():
            return log.read_text().splitlines() if log.exists() else []
        await peer.send('run-satellite')
        await peer.send('describe')
        _, info = await peer.expect('info')
        assert info[1]['satellite']['vector_update_indicator']
        await peer.send('vector-indicator', {'kind': 'update', 'duration_ms': 1000})
        await peer.barrier()
        await until(lambda: '-s hassmic_rainbow_2' in lines())
        await until(lambda: '-u hassmic_rainbow_2' in lines())
        # The daemon expires the animation even if the controller sends nothing else.
        before = len(lines())
        await peer.send('vector-indicator', {'kind': 'updated', 'duration_ms': 1000})
        await until(lambda: any('-s hassmic_pulse_' in x for x in lines()[before:]))
        await until(lambda: any('-u hassmic_pulse_' in x for x in lines()[before:]))
        await peer.send('vector-indicator', {'kind': 'update', 'duration_ms': 5000})
        before = len(lines())
        await until(lambda: '-s hassmic_rainbow_2' in lines()[before:])
        after = await device.mute(peer, True)
        await until(lambda: '-u hassmic_rainbow_2' in lines()[before:])
        await peer.quiet(after, 'RGB must yield to the physical microphone mute')
        before = len(lines())
        await peer.send('vector-indicator', {'kind': 'updated', 'duration_ms': 500})
        await asyncio.sleep(0.8)
        assert not any('-s hassmic_pulse_' in x or '-s hassmic_rainbow_' in x for x in lines()[before:])
        assert (device.root / 'privacy/state').read_text().strip() == '1'
    print('PASS: actual native RGB cycle, green pulse, independent device expiry and physical mute priority')


if __name__ == '__main__':
    if len(sys.argv) == 2:
        os.execvp('unshare', ['unshare', '-Urn', 'sh', '-c', 'ip link set lo up && exec "$@"',
                             'sh', sys.executable, str(Path(__file__).resolve()), sys.argv[1], 'isolated'])
    asyncio.run(main(Path(sys.argv[1]).resolve()))
