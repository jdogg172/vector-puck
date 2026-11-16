"""Run against the host-built actual C daemon, with synthetic file audio only."""
import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vector_puck.transport import read_event, send_event


async def expect(reader, wanted, seen=None):
    async def receive():
        while True:
            event = await read_event(reader)
            if seen is not None:
                seen.append(event[0])
            if event[0] == wanted:
                return event
    return await asyncio.wait_for(receive(), 3)


async def main(binary):
    with tempfile.TemporaryDirectory(prefix='vector-interrupt-') as temporary:
        root = Path(temporary)
        sounds = root / 'state' / 'sounds'
        sounds.mkdir(parents=True)
        with wave.open(str(sounds / 'volume.wav'), 'wb') as tone:
            tone.setnchannels(1)
            tone.setsampwidth(2)
            tone.setframerate(48000)
            tone.writeframes(bytes(4800))
        env = {**os.environ, 'HASSMIC_STATE':str(root/'state'),
               'HASSMIC_FAKE_AFE':'0:100:10', 'HASSMIC_PLAY':str(root/'play.raw')}
        with (root/'daemon.log').open('w') as log:
            proc = subprocess.Popen([str(binary),'-P','wyoming','-p','16801','-w','remote',
                '-n','Vector test','-z','0','-a','0','-i','0','-B','-W','0','-o','0'],
                env=env,stdout=log,stderr=log)
            writer = None
            try:
                for _ in range(100):
                    try:
                        reader,writer=await asyncio.open_connection('127.0.0.1',16801)
                        break
                    except OSError:
                        await asyncio.sleep(.05)
                else:
                    raise AssertionError((root/'daemon.log').read_text())
                await send_event(writer,'describe')
                _,info,_=await expect(reader,'info')
                assert info['satellite']['vector_controls']==2
                await send_event(writer,'run-satellite')
                await expect(reader,'run-pipeline')
                await send_event(writer,'detection',{'name':'hey_vector'})
                await send_event(writer,'transcript',{'text':''})
                await send_event(writer,'vector-listen')
                audio={'rate':24000,'width':2,'channels':1}
                await send_event(writer,'audio-start',audio)
                _,data,_=await expect(reader,'run-pipeline')
                assert data.get('vector_barge_in') is True
                await expect(reader,'audio-chunk')
                for _ in range(100):
                    await send_event(writer,'audio-chunk',audio,bytes(4800))
                started=time.monotonic()
                await send_event(writer,'vector-stop')
                await expect(reader,'vector-cancel')
                await expect(reader,'played')
                _,data,_=await expect(reader,'run-pipeline')
                assert not data.get('vector_barge_in')
                assert time.monotonic()-started<1.5,'Stop waited for the queued 10-second answer'
                assert (root/'play.raw').stat().st_size<48000*2
                for _ in range(30):
                    if 'earcon:' in (root / 'daemon.log').read_text():
                        break
                    await asyncio.sleep(.05)
                assert 'earcon:' in (root / 'daemon.log').read_text(), 'No stop confirmation earcon queued'
                print('PASS: microphone streams during reply; stop discards queued answer promptly')
                # A normal finish must not look like cancel and erase the follow-up.
                await send_event(writer,'detection',{'name':'hey_vector'})
                await send_event(writer,'transcript',{'text':''})
                await send_event(writer,'vector-listen')
                await send_event(writer,'audio-start',audio)
                _,data,_=await expect(reader,'run-pipeline')
                assert data.get('vector_barge_in')
                await send_event(writer,'audio-chunk',audio,bytes(4800))
                await send_event(writer,'audio-stop')
                seen=[]
                await expect(reader,'played',seen)
                _,data,_=await expect(reader,'run-pipeline',seen)
                assert not data.get('vector_barge_in')
                assert 'vector-cancel' not in seen
                await expect(reader,'audio-chunk')
                print('PASS: normal reply completes without cancellation; next mic pipeline resumes')
                await send_event(writer,'pause-satellite')
            finally:
                if writer:
                    writer.close()
                    await writer.wait_closed()
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__=='__main__':
    asyncio.run(main(Path(sys.argv[1]).resolve()))
