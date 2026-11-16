"""Synthetic Responses messages only; no API calls or home devices."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from vector_puck.assistant import answer, request_body
from vector_puck.config import Config
from vector_puck.home_control import HomeControl
from vector_puck.state import Cancelled

from test_home_control import CONFIG


class Response:
    status_code = 200
    def __init__(self, output):
        self.raw = json.dumps({"status": "completed", "output": output}).encode()
    async def __aenter__(self):
        return self
    async def __aexit__(self, *unused):
        return False
    async def aiter_bytes(self):
        yield self.raw


class Client:
    def __init__(self, output):
        self.output, self.requests = output, []
    def __call__(self, **options):
        self.options = options
        return self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *unused):
        return False
    def stream(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return Response(self.output)


def call(name="home_action", arguments=None):
    return {"type": "function_call", "name": name, "arguments": arguments or json.dumps({
        "target": "audio", "action": "volume_down", "value": None, "option": None,
    })}


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def home(self, message="Dry run: no command was sent."):
        backend = HomeControl(CONFIG, "synthetic-bearer")
        return SimpleNamespace(tools=backend.tools, execute=AsyncMock(return_value={"message": message}))

    async def test_one_natural_request_speaks_executor_message_without_second_model_pass(self):
        home = self.home()
        client = Client([call()])
        text = "The audio is loud, make it quieter please"
        reply = await answer(Config(), "synthetic", text, [], lambda: None, client, home)
        self.assertEqual(reply, "Dry run: no command was sent.")
        self.assertEqual(len(client.requests), 1)
        home.execute.assert_awaited_once()
        self.assertEqual(home.execute.call_args.kwargs["utterance"], text)
        body = client.requests[0][2]["json"]
        self.assertFalse(body["parallel_tool_calls"])
        self.assertTrue(body["tools"][0]["strict"])
        schema = json.dumps(body["tools"])
        for private in (CONFIG["base_url"], CONFIG["token_file"], "media_player.example_audio",
                        "climate.example_zone", "synthetic-bearer"):
            self.assertNotIn(private, schema)

    async def test_unsupported_and_multiple_calls_rejected_before_any_dispatch(self):
        for output in ([call(), call()], [call("shell")], [call(), call("shell")]):
            home, client = self.home(), Client(output)
            reply = await answer(Config(), "synthetic", "turn it down", [], lambda: None, client, home)
            self.assertIn("Nothing was changed", reply)
            home.execute.assert_not_awaited()

    async def test_disabled_integration_has_no_tool_and_never_dispatches(self):
        self.assertEqual(request_body(Config(), "hello", [])["tools"], [])
        reply = await answer(Config(), "synthetic", "turn it down", [], lambda: None, Client([call()]))
        self.assertIn("Nothing was changed", reply)

    async def test_duplicate_nonobject_or_oversized_arguments_rejected(self):
        for arguments in ('{"target":"a","target":"b"}', '[]', '{bad', ' ' * 4097):
            home = self.home()
            reply = await answer(Config(), "synthetic", "turn it down", [], lambda: None,
                                 Client([call(arguments=arguments)]), home)
            self.assertIn("Nothing was changed", reply)
            home.execute.assert_not_awaited()

    async def test_cancel_before_dispatch_prevents_tool(self):
        home, checks = self.home(), [0]
        def guard():
            checks[0] += 1
            if checks[0] >= 4:
                raise Cancelled("cancelled")
        with self.assertRaises(Cancelled):
            await answer(Config(), "synthetic", "turn it down", [], guard, Client([call()]), home)
        home.execute.assert_not_awaited()

    async def test_ambiguous_write_outcome_is_never_rewritten_as_success(self):
        message = "The request outcome is unverified. Check Home Assistant before trying again."
        reply = await answer(Config(), "synthetic", "open dashboard", [], lambda: None,
                             Client([call()]), self.home(message))
        self.assertEqual(reply, message)


if __name__ == "__main__":
    unittest.main()
