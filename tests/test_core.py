"""Offline behavioral tests. All audio, keys, API messages and peers are synthetic."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from vector_puck.assistant import CloudError, request_body, unpack
from vector_puck.cloud_voice import Dialogue, Quota, cedar, read_key
from vector_puck.config import Config
from vector_puck.resident import Satellite, paced, respond, session
from vector_puck.speech import Speech, endpoint_ready, question
from vector_puck.state import Cancelled, FollowUp, Gate
from vector_puck.transport import MIC_FORMAT, check_audio, read_event, send_event
from vector_puck.tune import working_audio

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Writer:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    def close(self):
        self.closed = True

    async def drain(self):
        await asyncio.sleep(0)


class FramingTests(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip_inline_and_separate_data(self):
        writer = Writer()
        await send_event(writer, "audio-chunk", MIC_FORMAT, b"\x01\x02")
        reader = asyncio.StreamReader()
        for byte in writer.data:
            reader.feed_data(bytes([byte]))
        self.assertEqual(await read_event(reader), ("audio-chunk", MIC_FORMAT, b"\x01\x02"))
        data = json.dumps(MIC_FORMAT).encode()
        reader.feed_data(json.dumps({"type": "audio-chunk", "data_length": len(data),
                                    "payload_length": 2}).encode() + b"\n" + data + b"\x03\x04")
        self.assertEqual(await read_event(reader), ("audio-chunk", MIC_FORMAT, b"\x03\x04"))

    async def test_invalid_lengths_rejected_before_reading(self):
        for field, size in [("payload_length", -1), ("payload_length", True),
                            ("payload_length", 65537), ("data_length", 8193)]:
            reader = asyncio.StreamReader()
            reader.feed_data(json.dumps({"type": "audio-chunk", field: size}).encode() + b"\n")
            with self.assertRaises(ValueError):
                await read_event(reader)

    async def test_truncation_and_wrong_event_shape(self):
        for value in [b'[]\n', b'{"type":3}\n', b'{"type":"x","data":[]}\n']:
            reader = asyncio.StreamReader()
            reader.feed_data(value)
            with self.assertRaises(ValueError):
                await read_event(reader)
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type":"audio-chunk","payload_length":2}\nX')
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await read_event(reader)

    def test_wrong_microphone_format_and_partial_samples(self):
        check_audio(MIC_FORMAT, bytes(2))
        for data, payload in [({**MIC_FORMAT, "rate": 24000}, bytes(2)),
                              ({**MIC_FORMAT, "channels": True}, bytes(2)),
                              (MIC_FORMAT, bytes(1)), (MIC_FORMAT, b"")]:
            with self.assertRaises(ValueError):
                check_audio(data, payload)


class ConfigAndStateTests(unittest.TestCase):
    def test_default_models_and_hardware(self):
        config = Config()
        self.assertEqual(config.conversation_model, "gpt-6-astra")
        self.assertEqual(config.voice_model, "gpt-realtime-2.1-mini")
        self.assertEqual(config.voice, "cedar")
        self.assertTrue(config.experimental_spoken_stop)
        self.assertEqual(config.home_assistant_file, "")

    def test_reject_invalid_config(self):
        for kwargs in [{"hardware": "echo-dot3"}, {"puck_host": "8.8.8.8"},
                       {"puck_host": "remote.example"}, {"puck_host": "239.1.1.1"},
                       {"followup_seconds": 16}, {"followup_turns": 6},
                       {"cpu_threads": True}, {"key_file": "relative"},
                       {"experimental_spoken_stop": "yes"}, {"working_sound": 1},
                       {"home_assistant_file": "relative"},
                       {"voice_model": "model?key=leak"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, TypeError)):
                Config(**kwargs)

    def test_unknown_and_duplicate_config_fields_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for value in ['{"unknown":1}', '{"voice":"cedar","voice":"cedar"}', '[]']:
                path.write_text(value)
                with self.assertRaises(ValueError):
                    Config.load(path)

    def test_gate_cancellation_mute_disconnect(self):
        gate = Gate()
        self.assertFalse(gate.allowed(0))
        gate.muted = False
        gate.guard(0)
        gate.cancel()
        with self.assertRaises(Cancelled):
            gate.guard(0)
        gate.connected = False
        self.assertFalse(gate.allowed(1))

    def test_followup_requires_matching_played_and_has_bounds(self):
        now = [10.0]
        follow = FollowUp(8, 2, 30, lambda: now[0])
        follow.begin()
        follow.expect(3)
        self.assertFalse(follow.open())
        follow.played(2, True)
        self.assertFalse(follow.open())
        follow.played(3, False)
        self.assertFalse(follow.open())
        follow.played(3, True)
        self.assertTrue(follow.open())
        follow.consume()
        self.assertEqual(follow.remaining, 1)
        follow.expect(3)
        follow.played(3, True)
        self.assertTrue(follow.open())
        now[0] += 9
        self.assertFalse(follow.active())
        with self.assertRaises(ValueError):
            follow.consume()
        follow.clear()
        self.assertFalse(follow.open())

    def test_wake_only_pause_does_not_endpoint_then_command_does(self):
        self.assertFalse(endpoint_ready("hey vector", 1.4, True, "hey vector"))
        self.assertFalse(endpoint_ready("hey vector unk", 2.1, True, "hey vector"))
        self.assertFalse(endpoint_ready("hey vector tell me a joke", 2.4, False, "hey vector"))
        self.assertTrue(endpoint_ready("hey vector tell me a joke", 3.0, True, "hey vector"))
        self.assertTrue(endpoint_ready("hey vector", 30.0, False, "hey vector"))
        self.assertEqual(question("Hey, Vector! Explain rain.", "hey vector"), "Explain rain.")
        self.assertIsNone(question("a TV said hey vector", "hey vector"))

    def test_vosk_wake_pause_state_preserves_later_command_words(self):
        speech = Speech.__new__(Speech)
        speech.config = Config()
        speech.endpoint_text = ""
        results = iter(['{"text":"hey vector"}', '{"text":"tell me a joke"}'])
        speech.endpoint = SimpleNamespace(AcceptWaveform=lambda _: True,
                                          Result=lambda: next(results))
        self.assertFalse(speech.endpoint_ready(bytes(3200), 1.4))
        self.assertTrue(speech.endpoint_ready(bytes(3200), 3.0))

    def test_tune_is_bounded_quiet_and_has_no_file_dependency(self):
        pcm = working_audio()
        self.assertEqual(len(pcm), 24000 * 2 * 3.2)
        from array import array
        samples = array("h", pcm)
        self.assertLess(max(abs(x) for x in samples), 2500)
        self.assertGreater(max(samples), 500)

    def test_private_key_file_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic-key"
            key = "sk-" + "x" * 32
            path.write_text(key + "\n")
            path.chmod(0o600)
            self.assertEqual(read_key(path), key)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_key(path)
            link = Path(tmp) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                read_key(link)

    def test_quota_survives_restart_and_counts_failed_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(Config(), daily_requests=1)
            Quota(config, Path(tmp)).reserve()
            with self.assertRaises(CloudError):
                Quota(config, Path(tmp)).reserve()

    def test_request_has_no_audio_tools_or_household_context(self):
        body = request_body(Config(), "Explain rain", [("user", "Explain clouds")])
        self.assertFalse(body["store"])
        self.assertEqual(body["tools"], [])
        self.assertEqual(body["input"][-1]["content"], "Explain rain")
        self.assertNotIn("audio", json.dumps(body))
        self.assertEqual(unpack({"status":"completed", "output":[{"type":"message", "content":[
            {"type":"output_text", "text":"Water falls."}]}]}), "Water falls.")
        with self.assertRaises(CloudError):
            unpack({"status": "incomplete"})


class FakeConnection:
    def __init__(self, events):
        self.events, self.sent = iter(events), []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        try:
            return json.dumps(next(self.events))
        except StopIteration:
            raise StopAsyncIteration

    async def send(self, data):
        self.sent.append(json.loads(data))


def voice_events(text="Water falls."):
    return [{"type":"session.created"}, {"type":"session.updated"},
            {"type":"response.output_audio.delta", "delta":base64.b64encode(bytes(4800)).decode()},
            {"type":"response.output_audio_transcript.delta", "delta":text},
            {"type":"response.done", "response":{"status":"completed"}}]


class CloudAndCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_exact_text_input_and_cedar_output(self):
        connection = FakeConnection(voice_events())
        pcm = await cedar(Config(), "synthetic", "Water falls.", lambda: None,
                          lambda *a, **k: connection)
        self.assertEqual(len(pcm), 4800)
        self.assertEqual(connection.sent[0]["session"]["audio"]["output"]["voice"], "cedar")
        response = connection.sent[1]["response"]
        self.assertEqual(response["conversation"], "none")
        self.assertEqual(response["input"][0]["content"], [{"type":"input_text", "text":"Water falls."}])
        self.assertNotIn("input_audio_buffer", json.dumps(connection.sent))
        self.assertTrue(connection.closed)

    async def test_voice_cancellation_closes_connection_and_discards_result(self):
        gate = Gate()
        gate.muted = False
        connection = FakeConnection(voice_events())
        calls = [0]
        def guard():
            calls[0] += 1
            if calls[0] == 4:
                gate.cancel()
            gate.guard(0)
        with self.assertRaises(Cancelled):
            await cedar(Config(), "synthetic", "Water falls.", guard, lambda *a, **k: connection)
        self.assertTrue(connection.closed)

    async def test_changed_spoken_answer_rejected(self):
        connection = FakeConnection(voice_events("Something else."))
        with self.assertRaises(CloudError):
            await cedar(Config(), "synthetic", "Water falls.", lambda: None, lambda *a, **k: connection)

    async def test_stale_connection_or_microphone_closes_and_invalidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            for stale_mic in (False, True):
                reader, writer = asyncio.StreamReader(), Writer()
                satellite = Satellite(reader, writer, Config(),
                                      SimpleNamespace(clear=lambda: None, expire=lambda: None),
                                      asyncio.Event())
                satellite.gate.muted = False
                if stale_mic:
                    satellite.last_audio = time.monotonic() - 21
                else:
                    satellite.last_event = time.monotonic() - 21
                await asyncio.wait_for(satellite.monitor(Path(tmp)), 1)
                self.assertTrue(writer.closed)
                self.assertFalse(satellite.gate.connected)
                self.assertFalse(satellite.gate.allowed(0))
                self.assertEqual((await satellite.events.get())[0], "disconnected")

    async def test_action_cancels_active_lookup_without_followup(self):
        dialogue = SimpleNamespace(clear=lambda: None)
        reader, writer = asyncio.StreamReader(), Writer()
        satellite = Satellite(reader, writer, Config(), dialogue, asyncio.Event())
        satellite.gate.muted = False
        satellite.followup.begin()
        satellite.active = asyncio.create_task(asyncio.sleep(60))
        reader.feed_data(b'{"type":"vector-cancel"}\n')
        pump = asyncio.create_task(satellite.pump())
        kind, _, _ = await asyncio.wait_for(satellite.events.get(), 1)
        self.assertEqual(kind, "vector-cancel")
        await asyncio.sleep(0)
        self.assertTrue(satellite.active.cancelled())
        self.assertFalse(satellite.gate.allowed(0))
        self.assertEqual(satellite.followup.remaining, 0)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)

    async def test_mute_cancels_and_requests_pause(self):
        dialogue = SimpleNamespace(clear=lambda: None)
        reader, writer = asyncio.StreamReader(), Writer()
        satellite = Satellite(reader, writer, Config(), dialogue, asyncio.Event())
        satellite.gate.muted = False
        reader.feed_data(b'{"type":"vector-mute","data":{"muted":true}}\n')
        pump = asyncio.create_task(satellite.pump())
        await asyncio.wait_for(satellite.events.get(), 1)
        self.assertTrue(satellite.gate.muted)
        self.assertIn(b'pause-satellite', writer.data)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)

    async def test_stop_only_pipeline_never_becomes_a_request(self):
        reader, writer = asyncio.StreamReader(), Writer()
        satellite = Satellite(reader, writer, Config(), SimpleNamespace(clear=lambda: None), asyncio.Event())
        satellite.gate.muted = False
        reader.feed_data(b'{"type":"run-pipeline","data":{"vector_barge_in":true}}\n')
        reader.feed_data(b'{"type":"audio-chunk","data":{"rate":16000,"width":2,"channels":1},"payload_length":2}\nXX')
        pump = asyncio.create_task(satellite.pump())
        await asyncio.sleep(0.03)
        self.assertTrue(satellite.events.empty())
        self.assertTrue(satellite.barge_stream)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)

    async def test_pacing_stops_after_epoch_changes(self):
        satellite = SimpleNamespace(gate=Gate(), send=AsyncMock())
        satellite.gate.muted = False
        def cancel_on_first(*args):
            satellite.gate.cancel()
        satellite.send.side_effect = cancel_on_first
        with self.assertRaises(Cancelled):
            await paced(satellite, bytes(9600), 0)
        self.assertEqual(satellite.send.call_count, 1)

    async def test_tune_is_joined_before_reply_and_history_waits_for_played(self):
        with tempfile.TemporaryDirectory() as tmp:
            dialogue = Dialogue(Config(), "synthetic", Path(tmp))
            dialogue.respond = AsyncMock(return_value=("Water falls.", bytes(4800)))
            satellite = Satellite(asyncio.StreamReader(), Writer(), Config(), dialogue, asyncio.Event())
            satellite.gate.muted = False
            satellite.followup.begin()
            await respond(satellite, "Explain rain", 0)
            self.assertEqual(dialogue.history, [])
            self.assertIsNotNone(satellite.pending_history)
            satellite.reader.feed_data(b'{"type":"played"}\n')
            pump = asyncio.create_task(satellite.pump())
            await asyncio.sleep(0.02)
            self.assertEqual(dialogue.history[-1], ("assistant", "Water falls."))
            self.assertTrue(satellite.followup.open())
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_wake_to_reply_then_playback_gated_followup(self):
        class FakeSpeech:
            def __init__(self):
                self.transcripts = iter(["hey vector explain rain", "hey vector explain rain",
                                         "And clouds?"])
            def reset(self):
                pass
            def wake_candidate(self, pcm):
                return True
            def transcribe(self, pcm):
                return next(self.transcripts)
            def endpoint_ready(self, pcm, seconds):
                return seconds >= 1.2
            def speech_present(self, pcm):
                return True
        config = replace(Config(), puck_host="127.0.0.1", working_sound=False,
                         experimental_spoken_stop=False)
        stop, complete = asyncio.Event(), asyncio.Event()
        peer_errors = []
        calls = []
        async def peer(reader, writer):
            replies = 0
            try:
                while True:
                    kind, data, _ = await read_event(reader)
                    if kind == "describe":
                        await send_event(writer, "info", {"satellite": {
                            "vector_controls": 2, "vector_muted": False}})
                    elif kind == "run-satellite":
                        await send_event(writer, "run-pipeline", {"start_stage":"wake"})
                        await send_event(writer, "audio-chunk", MIC_FORMAT, bytes(3200))
                    elif kind == "detection":
                        await send_event(writer, "audio-chunk", MIC_FORMAT, bytes(19200))
                        await send_event(writer, "audio-chunk", MIC_FORMAT, bytes(19200))
                    elif kind == "audio-stop":
                        replies += 1
                        await send_event(writer, "played")
                        if replies == 1:
                            await send_event(writer, "run-pipeline", {"start_stage":"wake"})
                            # The client must accept this only after played opens its follow-up.
                            await send_event(writer, "audio-chunk", MIC_FORMAT, bytes(3200))
                        elif replies == 2:
                            stop.set()
                            complete.set()
                    elif kind == "pause-satellite":
                        return
            except Exception as exc:
                peer_errors.append(type(exc).__name__)
                complete.set()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(peer, "127.0.0.1", 0)
        config = replace(config, puck_port=server.sockets[0].getsockname()[1])
        with tempfile.TemporaryDirectory() as tmp:
            dialogue = Dialogue(config, "synthetic", Path(tmp))
            async def fake_respond(query, guard):
                guard()
                calls.append(query)
                return "Synthetic answer.", bytes(4800)
            dialogue.respond = fake_respond
            task = asyncio.create_task(session(config, FakeSpeech(), dialogue, stop, Path(tmp)))
            try:
                await asyncio.wait_for(complete.wait(), 3)
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                server.close()
                await server.wait_closed()
        self.assertEqual(peer_errors, [])
        self.assertEqual(calls, ["explain rain", "And clouds?"])


class ScriptTests(unittest.TestCase):
    def test_public_scan_rejects_ha_tokens_and_local_mapping_artifacts(self):
        scanner = load_script("scan_public")
        synthetic = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 20
        self.assertIn("jwt", scanner.inspect("config.json", synthetic.encode()))
        for path in ("config/homeassistant.local.json", "config/local.json.backup"):
            self.assertIn("prohibited_artifact", scanner.inspect(path, b"{}"))

    def test_scan_detects_realistic_leaks_but_not_documentation_addresses(self):
        scanner = load_script("scan_public")
        for value in ["sk-" + "x" * 35, "/home/" + "operator/private/config.json",
                      "192." + "168.42.9", "wopr" + "net"]:
            self.assertTrue(scanner.inspect("test.txt", value.encode()))
        self.assertFalse(scanner.inspect("config.json", b'{"puck_host":"192.0.2.10"}'))
        self.assertTrue(scanner.inspect("recording.wav", bytes(10)))

    def test_model_verifier_rejects_tamper_and_unsafe_path(self):
        models = load_script("download_models")
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "model"
            path.write_bytes(b"synthetic")
            spec = {"sha256":hashlib.sha256(b"synthetic").hexdigest(), "size":9}
            self.assertTrue(models.valid(path, spec))
            path.write_bytes(b"tampered!")
            self.assertFalse(models.valid(path, spec))
            with self.assertRaises(ValueError):
                models.safe_target(root, "../escape")
            (root / "link").symlink_to(root)
            with self.assertRaises(ValueError):
                models.safe_target(root, "link/model")

    def test_setup_external_key_metadata_only_and_no_overwrite(self):
        setup = load_script("setup_local")
        key_path = Path("/nonexistent-vector-puck-synthetic/key")
        real_stat = Path.stat
        def fake_stat(path, *args, **kwargs):
            if path == key_path:
                return SimpleNamespace(st_mode=0o100600, st_uid=os.geteuid())
            return real_stat(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, "stat", fake_stat):
            root = Path(tmp)
            setup.setup(root, "192.0.2.10", key_path)
            config = Config.load(root / "config/local.json")
            self.assertEqual(config.key_file, "/run/secrets/openai_key")
            self.assertIn(f"LOCAL_UID={os.getuid()}", (root / ".env").read_text())
            self.assertIn(str(key_path), (root / ".env").read_text())
            with self.assertRaises(ValueError):
                setup.setup(root, "192.0.2.10", key_path)


if __name__ == "__main__":
    unittest.main()
