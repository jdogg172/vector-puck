# Choosing room hardware

Checked 2026-11-16. **This repository's patch and guarded installer support only Echo Dot 2 RS03QR.** The upstream satellite project supports additional models, but they need separate integration and acceptance before running this portable kit. Do not bypass the installer's model or firmware checks.

## Used devices worth inspecting

| Device | Exact model | Published entry path | Fit for this kit |
|---|---|---|---|
| Echo Dot 2, 2016 | **RS03QR**, biscuit | Micro-USB, no soldering | Best match; the only model implemented here |
| Echo Input, 2018 | **C1125P**, cupcake | Micro-USB, no soldering | Upstream candidate for an external speaker; no internal speaker; needs a port here |
| Echo Show 5, first generation | **H23K37**, checkers | Micro-USB, no soldering | Upstream screen candidate; needs a port here |
| Echo Show 8, first generation | **C7H6N3**, crown | Micro-USB, no soldering | Upstream candidate for a larger display; needs a port here |
| Echo Show 5, second generation | **C76N8S**, cronos | Micro-USB, no soldering | Upstream screen candidate; needs a port here |
| Echo Show 8, second generation | **A8H3N2**, athena | Micro-USB serial unlock, no soldering reported upstream | Newer upstream path; independently review firmware and recovery requirements before buying |

The [current upstream supported table](https://github.com/Gamer92000/echo-dot-assist#supported-echos) links each model's prerequisites and firmware requirements. Support there is based on a small number of tested devices, not a vendor guarantee. Obtain a clear photo of the base model label, the current firmware version, included power supply and functioning screen/touch before purchase. Generation names and seller descriptions alone are insufficient. Have the seller remove account registration before handoff.

Echo Dot 3 **D9N29T** and Echo 2 **XC56PY** have upstream support but require internal USB wiring/soldering, so they are poor choices when avoiding hardware work. Dot 3 refresh **C78MP8** and clock variant **36EBT3** are different, unsupported hardware. Do not buy them based on a listing that only says “Dot 3.”

Show 8 third generation **R85SD6** and rotating Show 10 **T4E4AT** are not supported by that satellite matrix. This does not establish that no hardware-access research exists. An unverified serial/storage research report is not a working, recoverable voice satellite. Avoid these as expansion purchases until an exact firmware-specific path and usable audio/display stack are demonstrated.

## Hardware designed for this job

[Home Assistant Voice Preview Edition](https://www.home-assistant.io/voice-pe/) has dual microphones, XMOS echo cancellation, a physical switch that cuts microphone power, ESPHome firmware and a 3.5 mm output for an external speaker. It avoids bootloader exploits. It is a Home Assistant device, **not a drop-in target for this Dot 2 controller**; preserving this kit's Cedar voice and follow-up/stop behavior requires an adapter or an HA voice-pipeline integration. Check the current official product page for pricing and availability.

For a screen without a bootloader project, a used tablet running a supported operating system can display an HA or family dashboard in a browser. That is a display option; built-in microphones and browser audio do not establish reliable far-field voice, physical mute or acoustic echo cancellation. Review security-update support before buying an old tablet.

## External Bose speaker

The SoundLink Revolve family supports AUX and USB audio input; see [Bose's audio support guide](https://support.bose.ca/article/slrv-speakerwireless-no-audio-from-product---ka08c000001buhyaaq). A USB playback interface is not proof of a USB microphone interface. Inspect the actual device's input/output capabilities before selecting it as a voice endpoint.

Pair a suitable microphone/satellite with the speaker rather than attempting to install the assistant on the Bose. Prefer wired output for predictable latency. Changing the speaker changes the echo path, so retest spoken interruption, pickup and physical mute at normal seating distance. Keep the original output route available until the new combination passes.
