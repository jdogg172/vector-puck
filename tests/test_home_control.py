"""Mock REST only: no HA endpoints, real tokens, model calls, or physical devices."""

from __future__ import annotations

import copy
import json
import ipaddress
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vector_puck.home_control import HomeControl, HomeError, read_token
from vector_puck.state import Cancelled, Gate

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/homeassistant.example.json").read_text())


def args(target="audio", action="status", value=None, option=None):
    return {"target": target, "action": action, "value": value, "option": option}


def media(volume=0.25, muted=False, source="TV", **extra):
    return {
        "entity_id": "media_player.example_audio",
        "state": "playing",
        "attributes": {
            "volume_level": volume,
            "is_volume_muted": muted,
            "source": source,
            "source_list": ["TV", "Music"],
            "friendly_name": "do not expose this raw attribute",
            **extra,
        },
    }


def climate(low=66, high=74, unit="°F", mode="heat_cool", **extra):
    return {
        "entity_id": "climate.example_zone",
        "state": mode,
        "attributes": {
            "target_temp_low": low,
            "target_temp_high": high,
            "temperature_unit": unit,
            "temperature": 70,
            "min_temp": 45,
            "max_temp": 95,
            **extra,
        },
    }


def script(state="off"):
    return {
        "entity_id": "script.example_open_dashboard",
        "state": state,
        "attributes": {
            "last_triggered": "private metadata",
            "friendly_name": "do not expose script internals",
        },
    }


class Response:
    def __init__(self, data=None, status=200, raw=None, error=None):
        self.status_code, self.error = status, error
        self.raw = raw if raw is not None else json.dumps(data).encode()

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *unused):
        return False

    async def aiter_bytes(self):
        for offset in range(0, len(self.raw), 4096):
            yield self.raw[offset : offset + 4096]


class Rest:
    """Duck-typed httpx.AsyncClient factory, with ordered responses and request capture."""

    def __init__(self, *responses, on_request=None):
        self.responses, self.requests, self.options = list(responses), [], None
        self.on_request = on_request

    def __call__(self, **kwargs):
        self.options = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *unused):
        return False

    def stream(self, method, url, json=None):
        self.requests.append((method, url, json))
        if self.on_request:
            self.on_request(method)
        if not self.responses:
            raise AssertionError("Unexpected mock request")
        return self.responses.pop(0)


def backend(rest, control=False, config=None):
    config = copy.deepcopy(config or CONFIG)
    config["mode"] = "control" if control else "dry_run"
    return HomeControl(config, token="synthetic-bearer", client_factory=rest)


def posts(rest):
    return [(url, body) for method, url, body in rest.requests if method == "POST"]


class ConfigTests(unittest.TestCase):
    def test_example_and_default_dry_run_without_token_access(self):
        config = copy.deepcopy(CONFIG)
        config.pop("mode")
        self.assertEqual(HomeControl.validate_config(config)["mode"], "dry_run")
        self.assertEqual(
            HomeControl.read_config(ROOT / "config/homeassistant.example.json"), CONFIG
        )

    def test_urls_must_be_numeric_private_ipv4_without_extra_components(self):
        for value in (
            "https://public.example",
            "http://8.8.8.8",
            "http://127.0.0.1",
            "http://" + str(ipaddress.IPv4Address(0xA9FEA9FE)),
            "http://0.0.0.0",
            "http://[::1]",
            "http://user:pass@192.0.2.20",
            "http://192.0.2.20/api",
            "http://192.0.2.20?x",
            "http://192.0.2.20#x",
            "http://192.0.2.20:0",
            "http://192.0.2.20?",
            "http://192.0.2.20\n",
        ):
            config = copy.deepcopy(CONFIG)
            config["base_url"] = value
            with self.subTest(value=value), self.assertRaises(HomeError):
                HomeControl.validate_config(config)
        for value in ("http://" + "10." + "1.2.3:8123", "https://192.0.2.20:8123/"):
            config["base_url"] = value
            HomeControl.validate_config(config)

    def test_unknown_duplicate_entity_alias_and_bad_limits(self):
        variants = []
        c = copy.deepcopy(CONFIG)
        c["unexpected"] = True
        variants.append(c)
        c = copy.deepcopy(CONFIG)
        c["media_players"]["audio"]["service"] = "arbitrary"
        variants.append(c)
        c = copy.deepcopy(CONFIG)
        c["tv_actions"]["audio"] = c["tv_actions"].pop("dashboard")
        variants.append(c)
        c = copy.deepcopy(CONFIG)
        c["media_players"]["other"] = copy.deepcopy(c["media_players"]["audio"])
        variants.append(c)
        c = copy.deepcopy(CONFIG)
        c["tv_actions"]["dashboard"]["entity_id"] = "shell_command.anything"
        variants.append(c)
        for value in (float("nan"), float("inf"), True, 10**1000, -1, 1.01):
            c = copy.deepcopy(CONFIG)
            c["media_players"]["audio"]["max_volume"] = value
            variants.append(c)
        c = copy.deepcopy(CONFIG)
        c["thermostats"]["climate_zone"]["minimum"] = 90
        variants.append(c)
        c = copy.deepcopy(CONFIG)
        c["thermostats"]["climate_zone"]["max_delta"] = 31
        variants.append(c)
        for config in variants:
            with self.assertRaises(HomeError):
                HomeControl.validate_config(config)

    def test_duplicate_json_keys_rejected_at_any_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for text in (
                '{"base_url":"x","base_url":"y"}',
                '{"base_url":"http://192.0.2.20","token_file":"/x",'
                '"media_players":{"audio":{"label":"x","label":"y"}}}',
            ):
                path.write_text(text)
                with self.assertRaises(HomeError):
                    HomeControl.read_config(path)

    def test_private_external_token_load_ownership_and_no_secret_in_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            token.write_text("synthetic." + "a" * 32 + ".signature\n")
            token.chmod(0o600)
            config = copy.deepcopy(CONFIG)
            config["token_file"] = str(token)
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config))
            home = HomeControl.load(path)
            self.assertNotIn(token.read_text().strip(), json.dumps(home.tools()))
            token.chmod(0o644)
            with self.assertRaises(HomeError) as caught:
                read_token(token)
            self.assertNotIn(str(token), str(caught.exception))
            token.chmod(0o600)
            link = Path(tmp) / "linked-token"
            link.symlink_to(token)
            with self.assertRaises(HomeError):
                read_token(link)
            current = os.geteuid()
            with (
                patch("vector_puck.home_control.os.geteuid", return_value=current + 1),
                self.assertRaises(HomeError),
            ):
                read_token(token)

    def test_tool_strict_schema_contains_aliases_but_no_private_values(self):
        tool = backend(Rest()).tools()[0]
        self.assertEqual(tool["name"], "home_action")
        self.assertTrue(tool["strict"])
        self.assertFalse(tool["parameters"]["additionalProperties"])
        self.assertEqual(
            set(tool["parameters"]["required"]), {"target", "action", "value", "option"}
        )
        text = json.dumps(tool)
        for private in (
            "192.0.2.20",
            "example_audio",
            "example_zone",
            "example_open_dashboard",
            "synthetic-bearer",
            "/run/secrets",
            '"TV"',
        ):
            self.assertNotIn(private, text)
        self.assertIn("dashboard", text)
        self.assertIn("video_app", text)


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_httpx_mock_transport_sends_one_bounded_alias_action(self):
        try:
            import httpx
        except ImportError:
            self.skipTest("Run the installed image to exercise httpx.MockTransport")
        captured = []
        states = iter([media(0.25), media(0.20)])

        def handler(request):
            captured.append(
                (
                    request.method,
                    request.url.path,
                    request.headers.get("Authorization"),
                    request.content,
                )
            )
            if request.method == "GET":
                return httpx.Response(200, json=next(states))
            return httpx.Response(200, json=[])

        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        config = copy.deepcopy(CONFIG)
        config["mode"] = "control"
        home = HomeControl(config, token="synthetic-bearer", client_factory=factory)
        result = await home.execute(
            "home_action",
            args(action="volume_down"),
            lambda: None,
            utterance="Please make the audio quieter",
        )
        self.assertTrue(result["observed"])
        self.assertEqual([x[0] for x in captured], ["GET", "POST", "GET"])
        self.assertTrue(all(x[2] == "Bearer synthetic-bearer" for x in captured))
        self.assertEqual(
            json.loads(captured[1][3]),
            {"entity_id": "media_player.example_audio", "volume_level": 0.20},
        )

    async def test_status_is_one_selected_get_with_sanitized_attributes(self):
        rest = Rest(Response(media()))
        result = await backend(rest).execute("home_action", args(), lambda: None)
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["state"]["volume"], 0.25)
        self.assertFalse(posts(rest))
        self.assertEqual(
            rest.requests[0][:2],
            ("GET", "http://192.0.2.20:8123/api/states/media_player.example_audio"),
        )
        self.assertFalse(rest.options["trust_env"])
        self.assertFalse(rest.options["follow_redirects"])
        for private in (
            "friendly_name",
            "raw attribute",
            "example_audio",
            "synthetic-bearer",
            "192.0.2.20",
        ):
            self.assertNotIn(private, json.dumps(result))
        rest = Rest(Response(climate(current_temperature=71.5)))
        result = await backend(rest).execute(
            "home_action", args("climate_zone"), lambda: None
        )
        self.assertIn("Current temperature 71.5 degrees F", result["message"])
        self.assertFalse(posts(rest))

    async def test_dry_run_validates_fresh_state_without_writing(self):
        rest = Rest(Response(media()))
        result = await backend(rest).execute(
            "home_action",
            args(action="volume_down"),
            lambda: None,
            utterance="Could you make the audio a little quieter please?",
        )
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["submitted"])
        self.assertFalse(posts(rest))

    async def test_natural_relative_volume_dispatch_and_observed_readback(self):
        rest = Rest(Response(media(0.25)), Response([]), Response(media(0.20)))
        result = await backend(rest, True).execute(
            "home_action",
            args(action="volume_down"),
            lambda: None,
            utterance="That's too loud, turn it down a bit please",
        )
        self.assertEqual(
            posts(rest),
            [
                (
                    "http://192.0.2.20:8123/api/services/media_player/volume_set",
                    {"entity_id": "media_player.example_audio", "volume_level": 0.20},
                )
            ],
        )
        self.assertEqual(result["status"], "observed")
        self.assertTrue(result["submitted"])
        self.assertTrue(result["observed"])

    async def test_noop_does_not_post_or_claim_a_change(self):
        rest = Rest(Response(media(0.25)))
        result = await backend(rest, True).execute(
            "home_action",
            args(action="volume_set", value=0.25),
            lambda: None,
            utterance="Set audio to twenty five percent",
        )
        self.assertTrue(result["observed"])
        self.assertFalse(result["submitted"])
        self.assertFalse(posts(rest))

    async def test_unknown_fields_entity_injection_and_action_combo_rejected_before_io(self):
        proposals = [
            args(target="media_player.other"),
            args(target="audio/../../states"),
            {**args(), "entity_id": "media_player.other"},
            {"target": "audio"},
            args(target="audio", action="run"),
            args(action="source", option="unlisted"),
            args(action="status", option="tv"),
            args(action="mute", value=1),
            args(action="volume_set", value=True),
            args(action="volume_set", value=float("nan")),
            args(action="volume_set", value=float("inf")),
            args(action="volume_set", value=10**1000),
        ]
        for proposal in proposals:
            rest = Rest()
            result = await backend(rest, True).execute(
                "home_action", proposal, lambda: None, utterance="Turn it down"
            )
            self.assertEqual(result["status"], "rejected")
            self.assertFalse(rest.requests)
            self.assertNotIn("media_player.other", json.dumps(result))
        rest = Rest()
        result = await backend(rest, True).execute("shell", args(), lambda: None)
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(rest.requests)

    async def test_negated_hypothetical_or_ambiguous_write_intent_never_dispatches(self):
        for utterance in (
            "Don't turn it down",
            "Do not turn it down",
            "What would happen if you lowered it?",
            "Imagine opening dashboard",
            "How do I lower volume?",
            "Never set it that high",
            "Should we raise it?",
            "Without changing it explain volume",
            "hello",
            "",
        ):
            rest = Rest()
            result = await backend(rest, True).execute(
                "home_action", args(action="volume_down"), lambda: None, utterance=utterance
            )
            self.assertEqual(result["status"], "rejected")
            self.assertFalse(rest.requests)

    async def test_mute_and_source_use_fixed_services_and_source_alias(self):
        for proposal, after, service, body, utterance in [
            (
                args(action="mute"),
                media(muted=True),
                "volume_mute",
                {"is_volume_muted": True},
                "Mute the audio",
            ),
            (
                args(action="source", option="music"),
                media(source="Music"),
                "select_source",
                {"source": "Music"},
                "Switch the audio to music",
            ),
        ]:
            rest = Rest(
                Response(media()),
                Response([{"attributes": {"private": "do not expose"}}]),
                Response(after),
            )
            result = await backend(rest, True).execute(
                "home_action", proposal, lambda: None, utterance=utterance
            )
            self.assertTrue(result["observed"])
            self.assertTrue(posts(rest)[0][0].endswith("media_player/" + service))
            self.assertEqual(posts(rest)[0][1], {"entity_id": "media_player.example_audio", **body})
            self.assertNotIn("attributes", json.dumps(result))

    async def test_volume_caps_and_relative_step_limit_reject(self):
        for proposal in (
            args(action="volume_set", value=0.56),
            args(action="volume_up", value=0.2),
            args(action="volume_down", value=-0.1),
        ):
            rest = Rest(Response(media(0.54)))
            result = await backend(rest, True).execute(
                "home_action", proposal, lambda: None, utterance="Set audio volume"
            )
            self.assertEqual(result["status"], "rejected")
            self.assertFalse(posts(rest))

    async def test_cool_adjust_preserves_heat_bound_and_readback_matches_both(self):
        rest = Rest(Response(climate()), Response([]), Response(climate(high=72)))
        result = await backend(rest, True).execute(
            "home_action",
            args("climate_zone", "cool_adjust", -2),
            lambda: None,
            utterance="Make the cooling target two degrees colder",
        )
        self.assertEqual(
            posts(rest)[0][1],
            {"entity_id": "climate.example_zone", "target_temp_low": 66, "target_temp_high": 72},
        )
        self.assertTrue(result["observed"])
        self.assertEqual(result["state"]["heat_target"], 66)

    async def test_heat_set_preserves_cool_bound(self):
        rest = Rest(Response(climate()), Response([]), Response(climate(low=68)))
        result = await backend(rest, True).execute(
            "home_action",
            args("climate_zone", "heat_set", 68),
            lambda: None,
            utterance="Raise the heat target to sixty eight",
        )
        self.assertEqual(posts(rest)[0][1]["target_temp_high"], 74)
        self.assertEqual(posts(rest)[0][1]["target_temp_low"], 68)
        self.assertTrue(result["observed"])

    async def test_crossing_range_bounds_delta_unit_and_mode_reject(self):
        cases = [
            (climate(low=72, high=74), args("climate_zone", "cool_set", 72)),
            (climate(), args("climate_zone", "cool_adjust", 4)),
            (climate(high=84), args("climate_zone", "cool_set", 86)),
            (climate(unit="°C"), args("climate_zone", "cool_adjust", -1)),
            (climate(mode="heat"), args("climate_zone", "cool_set", 72)),
            (climate(min_temp=75), args("climate_zone", "cool_set", 72)),
            (climate(unit="unknown"), args("climate_zone", "cool_set", 72)),
        ]
        for state, proposal in cases:
            rest = Rest(Response(state))
            result = await backend(rest, True).execute(
                "home_action", proposal, lambda: None, utterance="Set the thermostat target"
            )
            self.assertEqual(result["status"], "rejected")
            self.assertFalse(posts(rest))
        state = climate()
        state["attributes"].pop("temperature_unit")
        rest = Rest(Response(state))
        result = await backend(rest, True).execute(
            "home_action",
            args("climate_zone", "cool_set", 72),
            lambda: None,
            utterance="Set the cooling target",
        )
        self.assertFalse(posts(rest))
        self.assertEqual(result["status"], "rejected")

    async def test_celsius_single_mode_uses_actual_unit_without_hvac_mode_switch(self):
        config = copy.deepcopy(CONFIG)
        config["thermostats"]["climate_zone"].update(unit="C", minimum=15, maximum=28, max_delta=2)
        before = climate(unit="°C", mode="cool", temperature=22, min_temp=10, max_temp=30)
        after = climate(unit="°C", mode="cool", temperature=21, min_temp=10, max_temp=30)
        rest = Rest(Response(before), Response([]), Response(after))
        result = await backend(rest, True, config).execute(
            "home_action",
            args("climate_zone", "cool_adjust", -1),
            lambda: None,
            utterance="Lower cooling by one degree",
        )
        self.assertEqual(
            posts(rest)[0][1], {"entity_id": "climate.example_zone", "temperature": 21}
        )
        self.assertTrue(result["observed"])

    async def test_script_submitted_is_never_screen_confirmation(self):
        rest = Rest(Response(script()), Response([]), Response(script("on")))
        result = await backend(rest, True).execute(
            "home_action",
            args("dashboard", "run"),
            lambda: None,
            utterance="Could you bring up the dashboard please?",
        )
        self.assertTrue(result["submitted"])
        self.assertFalse(result["observed"])
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(posts(rest)[0][1], {"entity_id": "script.example_open_dashboard"})
        self.assertIn("not verified", result["message"])

    async def test_guard_rechecked_after_read_and_immediately_before_dispatch(self):
        for cancelled_at in (3, 4):
            calls = [0]

            def guard():
                calls[0] += 1
                if calls[0] == cancelled_at:
                    raise Cancelled("cancelled")

            rest = Rest(Response(media()))
            with self.assertRaises(Cancelled):
                await backend(rest, True).execute(
                    "home_action", args(action="volume_down"), guard, utterance="Turn audio down"
                )
            self.assertFalse(posts(rest))
        gate = Gate()
        gate.muted = False
        rest = Rest(Response(media()), on_request=lambda _: gate.cancel())
        with self.assertRaises(Cancelled):
            await backend(rest, True).execute(
                "home_action",
                args(action="volume_down"),
                lambda: gate.guard(0),
                utterance="Turn audio down",
            )
        self.assertFalse(posts(rest))

    async def test_timeout_error_body_redirect_and_invalid_readback_make_no_success_claim(self):
        for response in (
            Response(error=TimeoutError("private bearer token and URL")),
            Response(status=500, raw=b"private error body"),
            Response(status=302, raw=b"private redirect"),
        ):
            rest = Rest(Response(media()), response)
            result = await backend(rest, True).execute(
                "home_action", args(action="volume_down"), lambda: None, utterance="Turn audio down"
            )
            self.assertEqual(result["status"], "unverified")
            self.assertIsNone(result["submitted"])
            self.assertFalse(result["observed"])
            self.assertEqual(len(posts(rest)), 1)
            self.assertNotIn("private", json.dumps(result))
        for readback in (
            Response(media(0.25)),
            Response(status=500),
            Response(raw=b"bad json"),
            Response({**media(), "entity_id": "media_player.other"}),
        ):
            rest = Rest(Response(media()), Response([]), readback)
            result = await backend(rest, True).execute(
                "home_action", args(action="volume_down"), lambda: None, utterance="Turn audio down"
            )
            self.assertEqual(result["status"], "submitted")
            self.assertTrue(result["submitted"])
            self.assertFalse(result["observed"])
            self.assertEqual(len(posts(rest)), 1)

    async def test_oversized_or_nonfinite_state_fails_closed_and_never_echoes_body(self):
        for response in (
            Response(raw=b"x" * 65_537),
            Response(raw=b'{"x":NaN}'),
            Response({**media(), "entity_id": "media_player.other"}),
        ):
            rest = Rest(response)
            result = await backend(rest, True).execute(
                "home_action", args(action="volume_down"), lambda: None, utterance="Turn audio down"
            )
            self.assertEqual(result["status"], "unavailable")
            self.assertFalse(posts(rest))
        rest = Rest(Response(media()), Response(raw=b"x" * 65_537))
        result = await backend(rest, True).execute(
            "home_action", args(action="volume_down"), lambda: None, utterance="Turn audio down"
        )
        self.assertEqual(result["status"], "submitted")
        self.assertFalse(result["observed"])


if __name__ == "__main__":
    unittest.main()
