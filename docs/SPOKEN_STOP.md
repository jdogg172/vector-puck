# Spoken stop and focus during a reply

With `experimental_spoken_stop` enabled, the controller can recognize an addressed stop during the working tune or Cedar playback. A satellite advertising `vector_focus: true` supports the directional listening cue. Taking a new question during speech also requires `vector_question: true`. The default wake phrase is “Hey Vector.” The local recognizer also accepts “Hey Victor” as a transcription variant.

| While Vector is speaking | Intended behavior |
| --- | --- |
| “Hey Vector, stop” or “Vector, be quiet” | Local confirmation cancels the request, clears follow-up and conversation history, flushes queued speaker audio and requests a short confirmation tone. A bare “stop” does not meet the addressed-stop check. |
| “Hey Vector” by itself | On a focus-capable satellite, the listening light appears for up to eight seconds while the answer keeps playing. Repeating the wake during that window does not extend it. If you say nothing else, the answer can finish normally. |
| “Hey Vector, what causes thunder?” | The controller can recognize the wake and the start of a question in one breath. It cuts off the old reply and captures the rest of the question. There is no need to wait for the light. |
| “Hey Vector,” then “What causes thunder?” | During the focus window you can start the question without repeating the wake phrase. On a satellite that supports addressed stop alone, wait for playback to finish and use the usual wake or follow-up. |

Focus applies to an ordinary Cedar reply. The working tune supports addressed stop. Calls, call signalling, a ringing alarm, announcements, emergency speech and ambient playback take precedence over focus. In household mode the room must still own the conversation turn. During a call, “Hey Vector, hang up” or “Hey Vector, end the call” uses the local call controls. A ringing alarm accepts “alarm off” through the normal request path when unmuted. The Action button can dismiss it locally.

## How a question takes over

The controller requests a separate microphone stream with `vector-listen`. The satellite marks it `vector_barge_in:true`. Vosk checks a small local grammar, and local Whisper checks a rolling window of roughly 3.5 seconds. The worker also checks voiced audio periodically in case Vosk misses a candidate. On a focus-capable peer it compares recognized words with the current reply to help reject Cedar's own voice.

For a new question, the full wake phrase must start the utterance or have opened the current focus window. When the controller recognizes enough of a question to start a new turn, it drops the interrupted reply from pending history and passes the question's opening audio and continuation to the normal local listener. At the end of speech, the final transcription must still match the confirmed opening. A cutoff at the recording limit, stale stream or lost room turn discards the question. An accepted question follows ordinary request routing and can use the tools available to that route. Merely lighting the ring submits nothing.

Physical Action or mute cancels the turn independently of recognition. Mute also stops microphone input. Neither a spoken stop nor a physical cancellation can undo a home command already dispatched. Check the target before repeating an uncertain command.

## Configuration and health

`experimental_spoken_stop` defaults to `true`. An explicit `false` disables both spoken stop and focus handoff. Restart the controller after changing the setting. The health file reports `spoken_stop`, `focus_capable`, `question_capable`, `interrupt_worker_running`, `interrupt_listening` and numeric `interrupt_checks`. The controller reconnects if the enabled worker fails. These fields describe the software session. They contain no recording or recognized text.

The interruption queue holds up to 100 frames on a focus-capable session and 30 on a legacy session. Local confirmation checks normally run at least 1.5 seconds apart. After a confirmed wake, the next check can run sooner to catch a short follow-on question. CPU load and room audio affect when recognition finishes.

## Calibrate one ring

Use the same stationary puck and speak from two seats roughly a quarter turn apart. If the bright spot has the same angular error from both seats, write the correction in degrees to `vector-ring-offset-deg` in that puck's satellite state directory. The default directory is `/data/local/hassmic/state`. Zero is the default. Positive degrees move the light clockwise when viewed from above. The range is −180° to +180°. The satellite reads the value at the next `run-satellite`. A room controller can also supply the integer `vector_ring_offset_deg` in that event. Each device has its own calibration. If the spot points toward a nearby speaker or wall at only one seat, check from that seat with the background audio quiet before changing the offset.

## Check it in the room

**Physical direction and overlap tuning are experimental.** During ordinary listening and reply focus, the patched biscuit and radar satellites use the microphone array's reported direction to position a cyan pair on the ring when available. An unknown direction calls for a neutral listening indication. A nearby TV, a reflected voice, external speakers or the puck's own playback can affect detection. Local recognition adds latency. Anyone within earshot can speak the phrase.

1. From your normal seat, ask for a long answer. During audible speech, say only “Hey Vector.” Check that the answer keeps playing. Stay silent and let the light expire or the answer finish. Repeat from different sides and check where the cyan pair appears.
2. During another reply, say “Hey Vector,” pause briefly, then ask “What time is it in Tokyo?” Repeat with the wake and question in one breath. Listen for the old reply to end once and the new answer to include the final detail, “Tokyo.”
3. Say “Hey Vector, stop” during a reply and again during the waiting tune. Check that queued sound stops and the confirmation tone is audible. Test Action separately, then press physical mute during an open focus window. Confirm the red mute indication and that speaking a follow-on produces no answer.

Synthetic audio and protocol tests cover state transitions, playback continuity and stale-input rejection. Direction from the actual seat, missed or false handoffs, and interruption latency require listening tests on each physical setup.
