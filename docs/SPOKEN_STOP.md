# Spoken stop

Version 0.2 adds experimental interruption during the working tune and Cedar's reply. Say **“Hey Vector, stop”** or **“Vector, be quiet.”** The configured wake name is used; the default also recognizes “Victor” as a local transcription variant. Bare “stop” is intentionally insufficient, to reduce television and conversational interruptions.

The controller sends `vector-listen` before output. The patched satellite marks its separate microphone stream `vector_barge_in:true`. That audio enters a bounded stop-only queue, never the normal conversation or home-action path. A small Vosk grammar proposes stops; an independent periodic check also runs so a missed Vosk candidate does not prevent interruption. Local Whisper must confirm an addressed phrase before cancellation.

On confirmation, the controller invalidates the request, clears follow-up/history, cancels pending generation/output and sends `vector-stop`. The satellite flushes queued speaker audio and requests a short confirmation earcon. A delayed recognition result is ignored if mute, cancellation, disconnect or a newer microphone stream changed its epoch/serial. There are no cloud recognition calls or stored recordings in this worker.

New configurations set `experimental_spoken_stop` to `true`. If upgrading a local configuration that explicitly uses `false`, change it yourself and restart after reviewing the feature. Set it back to `false` to disable spoken interruption while retaining Action/mute.

## Limits and acceptance

- Recognition uses a 3.5-second rolling buffer, a bounded 30-frame queue and at most one local confirmation about every 1.5 seconds. CPU transcription and microphone processing add latency; this is not guaranteed instantaneous or deterministic interruption.
- The speaker's own voice, television audio, room placement and recognition model can cause misses or false matches. A nearby person or recording saying the addressed phrase can also stop playback. Voice is not authentication.
- Using an external speaker changes the acoustic path and may defeat the puck's original echo cancellation. Recheck real interruption after moving or replacing a speaker.
- Physical Action stops an answer without waiting for recognition. Physical mute stops new listening and cancels the active request. Use these if speech interruption misses.
- Stopping after a home-control command was dispatched cannot reverse it. Check HA before repeating an uncertain command.

Health includes `spoken_stop`, `interrupt_worker_running`, `interrupt_listening` and numeric `interrupt_checks`. It contains no audio or recognized interruption text. If an enabled stop worker fails, the controller closes the satellite session and reconnects rather than reporting a healthy session without interruption support.

On your own hardware, request a long answer, wait until the voice actually starts, then say the stop phrase from your normal seat. Confirm speech ends and the tone is audible. Repeat during the waiting tune, then verify Action and red microphone mute. A passing synthetic test or health check is not this physical acceptance test.


With optional household mode enabled, addressed “Hey Vector, hang up” and “Hey Vector, end the call” also use this local stop path. Action, physical mute or confirmed addressed stop ends the current room call and closes microphone forwarding. A ringing alarm accepts “alarm off” through the normal request path when unmuted; native Action dismissal remains available without cloud access.
