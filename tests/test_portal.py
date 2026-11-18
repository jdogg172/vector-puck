"""Portal HTTP, real resident Unix sockets, disposable SQLite and synthetic HA identities."""
import asyncio
import base64
from contextlib import closing
import hashlib
import ipaddress
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from websockets.asyncio.server import serve

from vector_puck.alarms import Alarms
from vector_puck.portal import Config, DEVICE_TTL, HomeAssistant, PairedPhones, Portal, Problem, local_ip
from vector_puck.room_management import make_room_api


def config(**overrides):
    return Config.parse(dict(portal_origin="http://127.0.0.1:17868", ha_origin="http://127.0.0.1:8123",
                             rooms=[{"id": "den", "label": "Den"}, {"id": "study", "label": "Study"}])
                        | overrides)


class FakeHA(HomeAssistant):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.user = {"id": "a" * 32, "is_owner": True, "is_admin": True}
        self.revoked = []
        self.exchanges = []
        self.token = secrets.token_urlsafe(32)
        self.refresh = secrets.token_urlsafe(32)

    async def identity(self, token):
        assert token == self.token
        return self.user.copy()

    async def exchange(self, code, verifier):
        self.exchanges.append((code, verifier))
        return self.token, self.refresh, 1800

    async def revoke(self, token):
        self.revoked.append(token)


class ConfigTests(unittest.TestCase):
    def test_local_network_boundaries_and_origins(self):
        for start, end in [(0x0a000000, 0x0affffff), (0xac100000, 0xac1fffff), (0xc0a80000, 0xc0a8ffff)]:
            for addr in (start, end):
                self.assertTrue(local_ip(str(ipaddress.ip_address(addr))))
            for addr in (start - 1, end + 1):
                self.assertFalse(local_ip(str(ipaddress.ip_address(addr))))
        self.assertTrue(local_ip("::1"))
        self.assertTrue(local_ip("fd00::1"))
        self.assertFalse(local_ip("0.0.0.0"))
        self.assertTrue(config(portal_origin="http://" + str(ipaddress.ip_address(0xc0a80001))))
        for bad in [dict(ha_origin="https://user@ha.local"), dict(portal_origin="ftp://localhost"),
                    dict(portal_origin="https://ha.local/path"), dict(ha_origin="https://ha.local?x"),
                    dict(portal_origin="http://public.invalid"), dict(allowed_user_ids=["owner"]),
                    dict(allowed_user_ids=["a"*31]), dict(allowed_user_ids="a"*32),
                    dict(rooms=[dict(id="../den", label="den")]), dict(rooms=[dict(id="den", label="Den", ip="x")])]:
            with self.subTest(bad=bad), self.assertRaises((ValueError, Problem)):
                config(**bad)

    def test_device_limit_digest_storage_and_fixed_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [100.]
            phones = PairedPhones(Path(tmp), lambda: now[0])
            raw = phones.enroll("Parent phone")
            self.assertTrue(phones.lookup(raw))
            self.assertEqual(phones.path.stat().st_mode & 0o777, 0o600)
            with closing(sqlite3.connect(phones.path)) as db:
                self.assertEqual(db.execute("SELECT digest FROM phones").fetchone()[0],
                                 hashlib.sha256(raw.encode()).digest())
            self.assertNotIn(raw.encode(), phones.path.read_bytes())
            for _ in range(11):
                phones.enroll("Tablet")
            with self.assertRaises(Problem):
                phones.enroll("Extra")
            now[0] += DEVICE_TTL
            self.assertIsNone(phones.lookup(raw))
            self.assertEqual(phones.list(), [])
            self.assertTrue(phones.enroll("Replacement"))


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = time.monotonic()
        self.health = dict(connected=True, muted=False, version="test")
        self.alarms = Alarms(self.root / "alarms.sqlite3")
        self.resident = make_room_api(state_dir=self.root, alarms=self.alarms, room_id="den",
                                      socket_dir=self.root / "sockets",
                                      health=lambda: dict(self.health, updated=time.time()))
        self.resident.start()
        self.addCleanup(self.resident.close)
        self.cfg = config(allowed_user_ids=["b"*32])
        self.ha = FakeHA(self.cfg)
        self.app = self.new_app()
        self.listener = await asyncio.start_server(self.app.handle, "127.0.0.1", 0)
        self.addAsyncCleanup(self.stop_listener)
        self.url = "http://127.0.0.1:" + str(self.listener.sockets[0].getsockname()[1])
        self.client = self.browser()

    def new_app(self):
        return Portal(self.cfg, self.root / "sockets", state_dir=self.root / "portal",
                      ha=self.ha, clock=lambda: self.now)

    async def stop_listener(self):
        self.listener.close()
        await self.listener.wait_closed()
        await asyncio.gather(*self.app.revocations)

    def browser(self):
        c = httpx.AsyncClient(base_url=self.url, trust_env=False, headers={
            "Host":urlsplit(self.cfg.portal).netloc, "Origin":self.cfg.portal})
        self.addAsyncCleanup(c.aclose)
        return c

    async def login(self, client=None):
        c = client or self.client
        start = await c.post("/auth/login", json={})
        params = parse_qs(urlsplit(start.json()["location"]).query)
        self.assertEqual(params["client_id"], [self.cfg.portal + "/"])
        self.assertEqual(params["redirect_uri"], [self.cfg.portal + "/auth/callback"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        result = await c.get("/auth/callback?" + urlencode({"code":"synthetic", "state":params["state"][0]}))
        self.assertEqual(result.status_code, 303)
        verifier = self.ha.exchanges[-1][1]
        self.assertEqual(params["code_challenge"][0],
                         base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="))
        return result

    async def authenticated(self, client=None):
        c = client or self.client
        self.assertEqual((await self.login(c)).headers["location"], "/")
        data = (await c.get("/api/session")).json()
        c.headers["X-CSRF-Token"] = data["csrf"]
        return data

    async def test_auth_cookie_state_replay_origin_host_csrf_and_no_tokens_in_html(self):
        c = self.client
        start = await c.post("/auth/login", json={})
        path = "/auth/callback?code=synthetic&state=" + parse_qs(urlsplit(start.json()["location"]).query)["state"][0]
        stranger = self.browser()
        self.assertEqual((await stranger.get(path)).headers["location"], "/signin-failed")
        callback = await c.get(path)
        self.assertEqual(callback.headers["location"], "/")
        self.assertIn("HttpOnly", callback.headers["set-cookie"])
        self.assertIn("SameSite=Strict", callback.headers["set-cookie"])
        self.assertEqual((await c.get(path)).headers["location"], "/signin-failed")
        self.assertEqual(len(self.ha.exchanges), 1)
        for route in ("/", "/portal.js", "/api/session"):
            result = await c.get(route)
            self.assertEqual(result.status_code, 200)
            self.assertNotIn(self.ha.token, result.text)
            self.assertNotIn(self.ha.refresh, result.text)
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertEqual(result.headers["referrer-policy"], "no-referrer")
        self.assertEqual((await c.get("/api/session?session=x")).status_code, 400)
        self.assertEqual((await c.get("/api/session", headers={"Host": "other.invalid"})).status_code, 403)
        self.assertEqual((await c.post("/api/pairing", json={})).status_code, 403)
        for origin in ("null", "https://other.invalid"):
            self.assertEqual((await c.post("/auth/pair", headers={"Origin":origin}, json={})).status_code, 403)
        self.assertEqual((await c.get("/api/rooms/unlisted/state")).status_code, 404)
        with self.assertRaises(Problem):
            await self.app.dispatch("GET", "/", {"host": "127.0.0.1:17868"}, b"", "203.0.113.1")

    async def test_allowlisted_parent_admin_owner_only_delegation_and_live_demotion(self):
        for user, admin, ok in [("c"*32, False, False), ("b"*32, False, True), ("d"*32, True, True),
                                ("e"*32, "true", False)]:
            with self.subTest(user=user):
                c = self.browser()
                self.ha.user = dict(id=user, is_admin=admin, is_owner=False)
                if not ok:
                    self.assertEqual((await self.login(c)).headers["location"], "/signin-failed")
                    self.assertFalse((await c.get("/api/session")).json()["signed_in"])
                    continue
                self.assertFalse((await self.authenticated(c))["can_pair"])
                self.assertEqual((await c.get("/api/rooms/den/state")).status_code, 200)
                self.assertEqual((await c.post("/api/pairing", json={})).status_code, 403)
                self.assertEqual((await c.get("/api/phones")).status_code, 403)
                self.ha.user = dict(id="f"*32, is_owner=False, is_admin=False)
                self.assertEqual((await c.get("/api/rooms/den/state")).status_code, 403)

    async def test_pair_restart_and_revoke_open_caregiver_session(self):
        owner = self.client
        self.assertTrue((await self.authenticated(owner))["can_pair"])
        phone = self.browser()
        issued = await owner.post("/api/pairing", json={"label":"Second phone"})
        code = issued.json()["code"]
        paired = await phone.post("/auth/pair", json={"code":code})
        self.assertEqual(paired.status_code, 200)
        self.assertIn("Max-Age=2592000", paired.headers["set-cookie"])
        self.assertEqual((await self.browser().post("/auth/pair", json={"code":code})).status_code, 403)
        self.assertFalse((await phone.get("/api/session")).json()["can_pair"])
        await self.stop_listener()
        self.app = self.new_app()
        self.listener = await asyncio.start_server(self.app.handle, "127.0.0.1", urlsplit(self.url).port)
        self.assertFalse((await owner.get("/api/session")).json()["signed_in"])
        session = (await phone.get("/api/session")).json()
        self.assertTrue(session["signed_in"])
        phone.headers["X-CSRF-Token"] = session["csrf"]
        self.assertEqual((await phone.get("/api/rooms/den/state")).status_code, 200)
        self.assertEqual((await phone.post("/api/pairing", json={})).status_code, 403)
        await self.authenticated(owner)
        grant, = (await owner.get("/api/phones")).json()["phones"]
        self.assertEqual(grant["label"], "Second phone")
        revoke = "/api/phones/" + grant["id"] + "/revoke"
        self.assertEqual((await phone.post(revoke, json={})).status_code, 403)
        self.assertEqual((await owner.post(revoke, json={})).json()["phones"], [])
        self.assertEqual((await phone.get("/api/rooms/den/state")).status_code, 401)
        self.assertFalse((await phone.get("/api/session")).json()["signed_in"])

    async def test_code_expiry_throttle_and_expired_session(self):
        await self.authenticated()
        c = self.browser()
        code = (await self.client.post("/api/pairing", json={})).json()["code"]
        self.now += 301
        for _ in range(5):
            self.assertEqual((await c.post("/auth/pair", json={"code":code})).status_code, 403)
        self.assertEqual((await c.post("/auth/pair", json={"code":code})).status_code, 429)
        self.now += 1801
        self.assertFalse((await self.client.get("/api/session")).json()["signed_in"])

    async def test_portal_http_to_resident_unix_socket_edit_conflict_cancel_and_preferences(self):
        await self.authenticated()
        c = self.client
        route = "/api/rooms/den/alarms"
        cmd = dict(action="set", name="School", at="07:00", days=[0,1,2,3,4], date=None, minutes=None)
        create = {"If-None-Match":"*"}
        created = await c.post(route, json=cmd, headers=create)
        self.assertEqual(created.status_code, 200, created.text)
        old, = created.json()["alarms"]
        self.assertEqual((await c.post(route, json=cmd, headers=create)).status_code, 409)
        changed = await c.post(route, json=cmd | {"at":"07:15"}, headers={"If-Match":old["revision"]})
        updated, = changed.json()["alarms"]
        self.assertNotEqual(old["id"], updated["id"])
        self.assertEqual(self.resident.state()["alarms"][0]["at"], "07:15")
        cancel = cmd | dict(action="cancel", at=None, days=[])
        self.assertEqual((await c.post(route, json=cancel, headers={"If-Match":old["revision"]})).status_code, 409)
        self.assertEqual((await c.post(route, json=cancel, headers={"If-Match":updated["revision"]})).json()["alarms"], [])
        self.assertEqual(self.resident.state()["alarms"], [])
        prefs = created.json()["preferences"] | dict(color="cyan")
        header = {"If-Match":created.json()["preferences_revision"]}
        saved = await c.post("/api/rooms/den/preferences", json=prefs, headers=header)
        self.assertEqual(saved.json()["preferences"], prefs)
        self.assertEqual(self.resident.preferences(), prefs)
        self.assertEqual((await c.post("/api/rooms/den/preferences", json=prefs, headers=header)).status_code, 409)
        self.health["connected"] = False
        offline = (await c.get("/api/rooms/den/state")).json()
        self.assertFalse(offline["online"])
        self.assertEqual(offline["version"], "unknown")
        self.assertEqual((await c.post(route, json=cmd, headers=create)).status_code, 503)
        self.assertEqual((await c.get("/api/rooms/study/state")).status_code, 503)

    async def test_null_health_version_and_proxy_origin_boundaries(self):
        await self.authenticated()
        with patch.object(self.resident, "_health", return_value=dict(online=False, muted=True, version=None)):
            self.assertEqual((await self.client.get("/api/rooms/den/state")).json()["version"], "unknown")
        for headers in ({"X-Forwarded-For":"203.0.113.11"}, {"Forwarded":"for=203.0.113.11"}):
            self.assertEqual((await self.client.get("/api/session", headers=headers)).status_code, 403)
        cookie = Portal(config(portal_origin="https://portal.home.arpa"), state_dir=self.root / "tls").cookie(
            "device", "test", DEVICE_TTL)
        self.assertTrue(cookie[1].startswith("__Host-vector-device="))
        self.assertIn("; Secure", cookie[1])
        self.assertNotIn("Domain=", cookie[1])

    async def test_dated_alarm_snooze_and_dismiss_via_real_socket(self):
        from datetime import datetime, timedelta, timezone
        await self.authenticated()
        c = self.client
        route = "/api/rooms/den/alarms"
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        cmd = dict(action="set", name="Appointment", at="07:30", days=[], date=tomorrow, minutes=None)
        created = await c.post(route, json=cmd, headers={"If-None-Match":"*"})
        self.assertEqual(created.json()["alarms"][0]["date"], tomorrow)
        def ring():
            with self.alarms.store.transaction() as db:
                db.execute("UPDATE alarms SET due=? WHERE name=?", (time.time()-2, "Appointment"))
            self.alarms.tick()
        control = cmd | dict(at=None, date=None)
        for act, minutes in (("snooze", 7), ("dismiss", None)):
            ring()
            current, = (await c.get("/api/rooms/den/state")).json()["alarms"]
            self.assertEqual(current["status"], "ringing")
            result = await c.post(route, json=control | dict(action=act, minutes=minutes),
                                  headers={"If-Match":current["revision"]})
            self.assertEqual(result.status_code, 200, result.text)
            if act == "snooze":
                self.assertAlmostEqual(result.json()["alarms"][0]["due"], time.time()+420, delta=3)
            else:
                self.assertEqual(result.json()["alarms"], [])

    async def test_ringing_transition_rejects_stale_cancel_over_unix_socket(self):
        await self.authenticated()
        route = "/api/rooms/den/alarms"
        command = dict(action="set", name="Wake", at=None, days=[], date=None, minutes=1)
        created = await self.client.post(route, json=command, headers={"If-None-Match": "*"})
        self.assertEqual(created.status_code, 200, created.text)
        scheduled, = created.json()["alarms"]

        # Tick after the portal checks the schedule, immediately before the
        # resident checks the precondition received on its Unix socket.
        change_alarm = self.resident.change_alarm
        matches_received = []

        def ring_before_write(data, matches, absent):
            matches_received.extend(matches)
            with patch.object(self.alarms, "clock", return_value=scheduled["due"] + 1):
                self.alarms.tick()
            return change_alarm(data, matches, absent)

        cancel = command | dict(action="cancel", minutes=None)
        with patch.object(self.resident, "change_alarm", ring_before_write):
            stale = await self.client.post(route, json=cancel, headers={"If-Match": scheduled["revision"]})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(matches_received, [scheduled["revision"]])

        current, = (await self.client.get("/api/rooms/den/state")).json()["alarms"]
        self.assertEqual(current["id"], scheduled["id"])
        self.assertEqual(current["status"], "ringing")
        self.assertNotEqual(current["revision"], scheduled["revision"])
        fresh = await self.client.post(route, json=cancel, headers={"If-Match": current["revision"]})
        self.assertEqual(fresh.status_code, 200, fresh.text)
        self.assertEqual(self.resident.state()["alarms"], [])

    async def test_concurrent_resident_preference_write_is_retained(self):
        await self.authenticated()
        path = "/api/rooms/den/"
        state = (await self.client.get(path + "state")).json()
        original = self.app.rooms.call
        async def race(room, target, payload=None, conditions=None):
            if target == "/preferences":
                await original(room, target, {"color":"amber"})
            return await original(room, target, payload, conditions)
        with patch.object(self.app.rooms, "call", race):
            result = await self.client.post(path + "preferences", json=state["preferences"] | {"color":"cyan"},
                                            headers={"If-Match":state["preferences_revision"]})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.resident.preferences()["color"], "amber")


class HAProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_http_form_exchange_and_websocket_current_user(self):
        received = []
        token = secrets.token_urlsafe()
        async def http(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(int(x.split(b":",1)[1]) for x in head.splitlines() if x.lower().startswith(b"content-length:"))
            received.append(parse_qs((await reader.readexactly(length)).decode()))
            body = json.dumps(dict(access_token=token, refresh_token="synthetic", expires_in=45, token_type="Bearer")).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            writer.close()
        async with await asyncio.start_server(http, "127.0.0.1", 0) as srv:
            ha = HomeAssistant(config(ha_origin="http://127.0.0.1:" + str(srv.sockets[0].getsockname()[1])))
            self.assertEqual(await ha.exchange("sample-code", "sample-verifier"), (token, "synthetic", 45))
            self.assertEqual(received[0], dict(client_id=[ha.client_id], grant_type=["authorization_code"],
                                               code=["sample-code"], code_verifier=["sample-verifier"]))
        async def ws(connection):
            self.assertEqual(connection.request.path, "/api/websocket")
            await connection.send('{"type":"auth_required"}')
            self.assertEqual(json.loads(await connection.recv()), dict(type="auth", access_token=token))
            await connection.send('{"type":"auth_ok"}')
            self.assertEqual(json.loads(await connection.recv()), dict(type="auth/current_user", id=1))
            await connection.send(json.dumps(dict(type="result", success=True, id=1, result=dict(id="a"*32, is_admin=True))))
        async with serve(ws, "127.0.0.1", 0) as srv:
            ha = HomeAssistant(config(ha_origin="http://127.0.0.1:" + str(srv.sockets[0].getsockname()[1])))
            self.assertTrue((await ha.identity(token))["is_admin"])
