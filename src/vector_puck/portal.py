"""LAN-only Vector room portal. Run with python -m vector_puck.portal."""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import dataclass
from datetime import date
import hashlib
import ipaddress
import json
import logging
import math
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
import re
import secrets
import socket
import ssl
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import h11
import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

ROOM = r"[a-z][a-z0-9-]{0,23}"
USER = r"[0-9a-fA-F]{32}"
TIME = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
# IANA RFC 1918 private-use blocks and RFC 4193 unique-local IPv6.
NETWORKS = tuple(ipaddress.ip_network(spec) for spec in (
    (0x0A000000, 8), (0xAC100000, 12), (0xC0A80000, 16), "fc00::/7",
))
LIMIT = 65536
STATIC = {"/": ("portal_ui.html", "text/html; charset=utf-8"),
          "/portal.css": ("portal_ui.css", "text/css; charset=utf-8"),
          "/portal.js": ("portal_ui.js", "text/javascript; charset=utf-8")}
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
           "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
           "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"}


class Problem(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def require(ok, status=400, message="Invalid request."):
    if not ok:
        raise Problem(status, message)


def local_ip(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or any(ip in network for network in NETWORKS)


def object_json(raw):
    def unique(pairs):
        obj = dict(pairs)
        if len(obj) != len(pairs):
            raise ValueError
        return obj
    def invalid_constant(_):
        raise ValueError
    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
        require(isinstance(value, dict))
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise Problem(400, "Invalid JSON.") from None


def origin(value):
    require(isinstance(value, str) and re.fullmatch(r"https?://[a-z0-9.\[\]:-]+/?", value))
    parts = urlsplit(value)
    host = parts.hostname
    require(host and parts.port != 0 and not host.endswith(".") and "%" not in host)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        require(all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in host.split(".")))
        require(parts.scheme == "https" or host == "localhost" or host.endswith((".local", ".home.arpa")))
    else:
        require(local_ip(host))
    netloc = f"[{host}]" if ":" in host else host
    if parts.port and parts.port != (443 if parts.scheme == "https" else 80):
        netloc += f":{parts.port}"
    return parts.scheme + "://" + netloc


def label(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and value == value.strip() and value.isprintable()


@dataclass(frozen=True)
class Config:
    portal: str
    ha: str
    rooms: dict[str, str]
    allowed: frozenset[str]

    @classmethod
    def parse(cls, data):
        require(set(data) in ({"portal_origin", "ha_origin", "rooms"},
                              {"portal_origin", "ha_origin", "rooms", "allowed_user_ids"}))
        rooms = data["rooms"]
        require(isinstance(rooms, list) and 1 <= len(rooms) <= 16)
        names = {}
        for room in rooms:
            require(isinstance(room, dict) and set(room) == {"id", "label"})
            key = room["id"]
            require(isinstance(key, str) and re.fullmatch(ROOM, key) and key not in names)
            require(label(room["label"], 60))
            names[key] = room["label"]
        users = data.get("allowed_user_ids", [])
        require(isinstance(users, list) and len(users) <= 64 and
                all(isinstance(x, str) and re.fullmatch(USER, x) for x in users))
        portal, ha = origin(data["portal_origin"]), origin(data["ha_origin"])
        require(portal != ha)
        return cls(portal, ha, names, frozenset(x.lower() for x in users))


async def limited(response):
    body = bytearray()
    async for chunk in response.aiter_raw():
        body.extend(chunk)
        require(len(body) <= LIMIT, 502, "Service response is too large.")
    return bytes(body)


class HomeAssistant:
    def __init__(self, config):
        self.config = config
        self.url = urlsplit(config.ha)
        self.tls = ssl.create_default_context()
        self.client_id = config.portal + "/"
        self.callback = config.portal + "/auth/callback"
        # The websocket library may otherwise log message frames at debug level.
        self.wire_log = logging.Logger("portal-wire")
        self.wire_log.disabled = True

    async def address(self):
        port = self.url.port or (443 if self.url.scheme == "https" else 80)
        addresses = await asyncio.get_running_loop().getaddrinfo(
            self.url.hostname, port, type=socket.SOCK_STREAM)
        require(addresses and all(local_ip(x[4][0]) for x in addresses),
                503, "Home Assistant needs a local address.")
        return addresses[0][4][0], port

    def authorize(self, state, verifier):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return self.config.ha + "/auth/authorize?" + urlencode(dict(
            response_type="code", client_id=self.client_id, redirect_uri=self.callback, state=state,
            code_challenge=challenge, code_challenge_method="S256"))

    async def form(self, data):
        async with asyncio.timeout(6):
            ip, port = await self.address()
            authority = f"[{ip}]" if ":" in ip else ip
            async with httpx.AsyncClient(verify=self.tls, trust_env=False, timeout=4) as client:
                async with client.stream("POST", f"{self.url.scheme}://{authority}:{port}/auth/token",
                                         data=data, headers={"Host": self.url.netloc, "Accept-Encoding": "identity"},
                                         extensions={"sni_hostname": self.url.hostname}) as response:
                    require(response.status_code == 200, 403, "Home Assistant declined sign-in.")
                    return await limited(response)

    async def exchange(self, code, verifier):
        raw = await self.form(dict(grant_type="authorization_code", client_id=self.client_id,
                                   code=code, code_verifier=verifier))
        token = object_json(raw)
        access, refresh, ttl = (token.get(k) for k in ("access_token", "refresh_token", "expires_in"))
        require(isinstance(access, str) and 0 < len(access) <= 8192 and isinstance(refresh, str)
                and 0 < len(refresh) <= 8192 and type(ttl) is int and ttl > 0
                and str(token.get("token_type")).lower() == "bearer", 403, "Sign-in failed.")
        return access, refresh, ttl

    async def revoke(self, refresh):
        # Compatible with HA versions predating the dedicated revocation endpoint.
        await self.form({"action": "revoke", "token": refresh})

    async def identity(self, access):
        async with asyncio.timeout(6):
            ip, port = await self.address()
            secure = self.url.scheme == "https"
            options = dict(ssl=self.tls, server_hostname=self.url.hostname) if secure else {}
            # Explicit host/port pins the peer and prevents cross-origin redirects.
            async with connect(("wss://" if secure else "ws://") + self.url.netloc + "/api/websocket",
                               host=ip, port=port, proxy=None, logger=self.wire_log, open_timeout=3,
                               close_timeout=1, max_size=LIMIT, compression=None, **options) as ws:
                require(object_json(await ws.recv()).get("type") == "auth_required", 403, "Sign-in failed.")
                await ws.send(json.dumps({"type": "auth", "access_token": access}))
                require(object_json(await ws.recv()).get("type") == "auth_ok", 401, "Please sign in again.")
                await ws.send('{"id":1,"type":"auth/current_user"}')
                msg = object_json(await ws.recv())
                user = msg.get("result")
                require(msg.get("type") == "result" and type(msg.get("id")) is int and msg["id"] == 1
                        and msg.get("success") is True and isinstance(user, dict), 403, "Sign-in failed.")
                return user


def revision(value):
    return '"' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest() + '"'


def preferences(value):
    require(isinstance(value, dict) and set(value) == {"brightness", "color", "spoilers"})
    require(type(value["brightness"]) is int and value["brightness"] in (20, 50, 80)
            and value["color"] in ("white", "cyan", "green", "violet", "amber")
            and value["spoilers"] in ("ask", "allow", "avoid"))
    return value


def schedule(at, days, on):
    require(isinstance(at, str) and re.fullmatch(TIME, at))
    require(isinstance(days, list) and len(days) <= 7
            and all(type(d) is int and 0 <= d <= 6 for d in days) and len(days) == len(set(days)))
    if on is not None:
        require(not days and isinstance(on, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", on))
        try:
            date.fromisoformat(on)
        except ValueError:
            raise Problem(400, "Choose a valid calendar date.") from None


def alarm_command(data):
    require(set(data) == {"action", "name", "at", "days", "date", "minutes"} and label(data["name"], 60))
    act, minutes = data["action"], data["minutes"]
    if act == "set" and minutes is None:
        schedule(data["at"], data["days"], data["date"])
    else:
        require(data["at"] is None and data["days"] == [] and data["date"] is None)
        if act == "set":
            require(type(minutes) is int and 1 <= minutes <= 1440)
        elif act == "snooze":
            require(minutes is None or type(minutes) is int and 1 <= minutes <= 60)
        else:
            require(act in ("cancel", "dismiss") and minutes is None)
    return data


class Rooms:
    def __init__(self, config, root):
        self.root = Path(root)
        self.locks = {key: asyncio.Lock() for key in config.rooms}

    async def call(self, room, path, payload=None, conditions=None):
        require(room in self.locks, 404, "Unknown room.")
        transport = httpx.AsyncHTTPTransport(uds=str(self.root / (room + ".sock")), retries=0)
        writing = payload is not None
        try:
            async with asyncio.timeout(4), httpx.AsyncClient(transport=transport, trust_env=False) as client:
                async with client.stream("POST" if writing else "GET", "http://resident" + path,
                                         json=payload, headers=conditions) as response:
                    if response.status_code in (400, 409, 412, 422):
                        raise Problem(409, "The room declined this change. Refresh its schedule.")
                    require(200 <= response.status_code < 300, 503, "Room unavailable. Refresh before trying again.")
                    raw = await limited(response)
                    return {} if writing else object_json(raw)
        except (TimeoutError, OSError, httpx.HTTPError):
            raise Problem(503, "Room unavailable. Refresh to check whether any change took effect.") from None

    async def state(self, room):
        try:
            raw = await self.call(room, "/state")
            online, muted, version = (raw[k] for k in ("online", "muted", "version"))
            require(type(online) is bool and type(muted) is bool and (version is None or label(version, 80)))
            prefs = preferences({k: raw["preferences"][k] for k in ("brightness", "color", "spoilers")})
            alarms = []
            require(isinstance(raw["alarms"], list) and len(raw["alarms"]) <= 64)
            names = set()
            for row in raw["alarms"]:
                alarm = {k: row[k] for k in ("id", "name", "at", "days", "date", "status", "due")}
                require(label(alarm["name"], 60) and isinstance(alarm["id"], str) and re.fullmatch(r"[0-9a-f]{32}", alarm["id"])
                        and alarm["name"].casefold() not in names)
                require(alarm["status"] in ("scheduled", "ringing", "dismissed", "cancelled", "snoozed"))
                require(type(alarm["due"]) in (float, int) and math.isfinite(alarm["due"]))
                schedule(alarm["at"], alarm["days"], alarm["date"])
                names.add(alarm["name"].casefold())
                alarm["revision"] = revision(row)
                alarms.append(alarm)
            return dict(online=online, muted=muted, version=version or "unknown", alarms=alarms,
                        preferences=prefs, preferences_revision=revision(prefs))
        except (TypeError, ValueError, KeyError, OverflowError, Problem) as exc:
            if isinstance(exc, Problem) and exc.status >= 500:
                raise
            raise Problem(502, "The room returned an unreadable state.") from None

    async def change(self, room, resource, data, headers, guard):
        require(room in self.locks, 404, "Unknown room.")
        command = preferences(data) if resource == "preferences" else alarm_command(data)
        async with self.locks[room]:
            state = await self.state(room)
            require(state["online"], 503, "This room is offline.")
            match, missing = headers.get("if-match"), headers.get("if-none-match")
            if resource == "preferences":
                expected = state["preferences_revision"]
            else:
                old = next((a for a in state["alarms"] if a["name"].casefold() == data["name"].casefold()), None)
                expected = old["revision"] if old else None
                if data["action"] != "set":
                    require(old is not None, 409, "The alarm changed. Refresh and try again.")
            valid = (match == expected and match is not None and missing is None) if expected else (
                missing == "*" and match is None)
            require(valid, 409, "The room changed or that name already exists. Refresh before saving.")
            if resource == "preferences":
                fields = {k: v for k, v in command.items() if state["preferences"][k] != v}
                if not fields:
                    return state
            else:
                fields = command
            conditions = {"If-Match": expected} if expected is not None else {"If-None-Match": "*"}
            guard()
            await self.call(room, "/" + resource, fields, conditions)
            after = await self.state(room)
            if resource == "preferences":
                confirmed = all(after["preferences"][k] == v for k, v in fields.items())
            else:
                new = next((a for a in after["alarms"] if a["name"].casefold() == data["name"].casefold()), None)
                act = data["action"]
                if act == "set":
                    confirmed = new and new["status"] == "scheduled"
                    if confirmed and data["minutes"] is None:
                        confirmed = (new["at"] == data["at"] and sorted(new["days"]) == sorted(data["days"])
                                     and (new["date"] == data["date"] or data["date"] is None and not data["days"]))
                    elif confirmed:
                        confirmed = abs(new["due"] - time.time() - data["minutes"] * 60) <= 15
                elif act == "cancel":
                    confirmed = not new or new["status"] == "cancelled"
                elif act == "dismiss":
                    confirmed = old["status"] == "ringing" and (not new or new["status"] in ("dismissed", "scheduled"))
                else:
                    confirmed = (old["status"] == "ringing" and new and new["status"] in ("scheduled", "snoozed")
                                 and abs(new["due"] - time.time() - (data["minutes"] or 9) * 60) <= 15)
            require(confirmed, 409, "The room did not confirm the requested change. Refresh to check it.")
            return after


DEVICE_TTL = 30 * 86400


class PairedPhones:
    """Only credential digests, display labels, grant IDs and times are durable."""
    def __init__(self, directory, wall=time.time):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022,
                500, "Use an owner-controlled portal state directory.")
        self.path = directory / "paired-phones.sqlite3"
        self.wall = wall
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with closing(self.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS phones (id TEXT PRIMARY KEY, digest BLOB UNIQUE NOT NULL, "
                       "label TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=1)

    def enroll(self, name):
        now = int(self.wall())
        credential = secrets.token_urlsafe(32)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM phones WHERE expires<=?", (now,))
            require(db.execute("SELECT count(*) FROM phones").fetchone()[0] < 12,
                    409, "Twelve phones are paired. Ask the owner to revoke an old phone.")
            db.execute("INSERT INTO phones VALUES (?,?,?,?,?)",
                       (secrets.token_hex(12), hashlib.sha256(credential.encode()).digest(),
                        name, now, now + DEVICE_TTL))
        return credential

    def lookup(self, credential):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", credential):
            return None
        with closing(self.connect()) as db:
            row = db.execute("SELECT id FROM phones WHERE digest=? AND expires>?",
                             (hashlib.sha256(credential.encode()).digest(), self.wall())).fetchone()
            return row[0] if row else None

    def list(self):
        with closing(self.connect()) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT id,label,created,expires FROM phones "
                                                     "WHERE expires>? ORDER BY created,id", (self.wall(),))]

    def remove(self, ident):
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM phones WHERE id=?", (ident,))


@dataclass(repr=False)
class Session:
    csrf: str
    expires: float
    idle: float
    access: str = ""
    refresh: str = ""
    user: str = ""
    phone: str = ""


def response(data=None, status=200, headers=()):
    body = b"" if data is None else json.dumps(data, allow_nan=False).encode()
    return status, [("Content-Type", "application/json"), *headers], body


class Portal:
    def __init__(self, config, control=Path("/control"), *, state_dir=Path("/state"), ha=None, clock=time.monotonic):
        self.config, self.clock = config, clock
        self.ha = ha or HomeAssistant(config)
        self.rooms = Rooms(config, control)
        self.sessions = {}
        self.phones = PairedPhones(state_dir)
        self.flows = {}
        self.invites = {}
        self.attempts = []
        self.revocations = set()
        self.active = 0
        self.secure = config.portal.startswith("https://")
        self.prefix = "__Host-vector-" if self.secure else "vector-"
        self.assets = {path: (Path(__file__).with_name(name).read_bytes(), mime)
                       for path, (name, mime) in STATIC.items()}

    def cookie(self, name, value, seconds, site="Strict"):
        flags = "; Secure" if self.secure else ""
        return ("Set-Cookie", f"{self.prefix}{name}={value}; Path=/; HttpOnly; SameSite={site}; Max-Age={seconds}{flags}")

    def cookie_value(self, headers, name):
        matches = [s.strip().partition("=")[2] for s in headers.get("cookie", "").split(";")
                   if s.strip().partition("=")[0] == self.prefix + name]
        return matches[0] if len(matches) == 1 else ""

    async def revoke(self, token):
        try:
            await self.ha.revoke(token)
        except (Problem, TimeoutError, OSError, httpx.HTTPError):
            pass  # The local session has already ended.

    def forget(self, sid):
        session = self.sessions.pop(sid, None)
        if session and session.refresh:
            task = asyncio.create_task(self.revoke(session.refresh))
            self.revocations.add(task)
            task.add_done_callback(self.revocations.discard)
        self.invites = {k: v for k, v in self.invites.items() if v[1] != sid}

    def prune(self):
        now = self.clock()
        self.flows = {k: v for k, v in self.flows.items() if v[0] > now}
        self.invites = {k: v for k, v in self.invites.items() if v[0] > now}
        self.attempts = [(t, ip) for t, ip in self.attempts if t > now - 300]
        for sid, session in list(self.sessions.items()):
            if min(session.expires, session.idle) <= now:
                self.forget(sid)

    def role(self, identity):
        uid = identity.get("id")
        require(isinstance(uid, str) and re.fullmatch(USER, uid), 403, "Account is not authorized.")
        owner = identity.get("is_owner") is True
        require(owner or identity.get("is_admin") is True or uid.lower() in self.config.allowed,
                403, "Account is not authorized for Vector.")
        return uid.lower(), owner

    def start(self, ttl, headers, access="", refresh="", user="", phone=""):
        self.prune()
        self.forget(self.cookie_value(headers, "session"))
        require(len(self.sessions) < 64, 429, "Too many active sessions. Try again later.")
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = Session(secrets.token_urlsafe(32), self.clock() + ttl, self.clock() + 1800,
                                     access, refresh, user, phone)
        return sid, self.cookie("session", sid, ttl)

    def check_session(self, sid, session, credential):
        if session.phone:
            require(self.phones.lookup(credential) == session.phone, 401, "This phone needs pairing.")
        require(self.sessions.get(sid) is session and min(session.expires, session.idle) > self.clock(),
                401, "Please sign in again.")

    async def dispatch(self, method, target, headers, raw, peer):
        self.prune()
        require(local_ip(peer), 403, "Local access required.")
        # Forwarding headers may restrict access, never grant it. The reverse proxy
        # must reject public ingress even if it omits or rewrites these headers.
        for hint in ("x-forwarded-for", "x-real-ip"):
            if hint in headers:
                require(all(local_ip(ip.strip()) for ip in headers[hint].split(",")),
                        403, "Local access required.")
        if "forwarded" in headers:
            for hop in headers["forwarded"].split(","):
                sources = [p.strip()[4:].strip('"') for p in hop.split(";")
                           if p.strip().lower().startswith("for=")]
                require(len(sources) == 1 and local_ip(sources[0].strip("[]")), 403, "Local access required.")
        require(headers.get("host", "").lower() == urlsplit(self.config.portal).netloc, 403, "Invalid host.")
        require("origin" not in headers or headers["origin"] == self.config.portal, 403, "Invalid origin.")
        require(method in ("GET", "POST") and target.startswith("/") and not target.startswith("//")
                and "#" not in target and "\\" not in target)
        path, querymark, query = target.partition("?")
        require(not querymark or path == "/auth/callback")
        if method == "POST":
            require(headers.get("origin") == self.config.portal, 403, "Invalid origin.")
            require(headers.get("content-type", "").split(";")[0] == "application/json", 415, "JSON required.")
            data = object_json(raw)
        else:
            require(not raw)
            data = {}
        if method == "GET" and (path in self.assets or path == "/signin-failed"):
            body, mime = self.assets["/" if path == "/signin-failed" else path]
            return 200, [("Content-Type", mime)], body
        if method == "POST" and path == "/auth/login":
            require(len(self.flows) < 64, 429, "Too many sign-in attempts. Try again later.")
            flow, state, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            self.flows[flow] = (self.clock() + 300, state, verifier)
            return response({"location": self.ha.authorize(state, verifier)},
                            headers=[self.cookie("flow", flow, 300, "Lax")])
        if method == "GET" and path == "/auth/callback":
            flow = self.flows.pop(self.cookie_value(headers, "flow"), None)
            params = parse_qs(query, max_num_fields=8)
            require(flow and len(params.get("state", [])) == 1
                    and params["state"][0].isascii() and secrets.compare_digest(flow[1], params["state"][0])
                    and len(params.get("code", [])) == 1 and label(params["code"][0], 1024),
                    403, "Sign-in expired or was declined.")
            access, refresh, ttl = await self.ha.exchange(params["code"][0], flow[2])
            try:
                user, _ = self.role(await self.ha.identity(access))
                _, cookie = self.start(min(3600, ttl), headers, access, refresh, user)
            except BaseException:
                await self.revoke(refresh)
                raise
            return response(status=303, headers=[("Location", "/"), cookie, self.cookie("device", "", 0), self.cookie("flow", "", 0, "Lax")])
        if method == "POST" and path == "/auth/pair":
            require(len(self.attempts) < 40 and sum(ip == peer for _, ip in self.attempts) < 5,
                    429, "Too many attempts. Try again in five minutes.")
            self.attempts.append((self.clock(), peer))
            code = data.get("code")
            require(isinstance(code, str) and re.fullmatch(r"[0-9]{8}", code), 403, "Invalid or expired code.")
            digest = hashlib.sha256(code.encode()).digest()
            invite = self.invites.pop(digest, None)
            require(invite, 403, "Invalid or expired code.")
            credential = self.phones.enroll(invite[2])
            return response({"ok": True}, headers=[self.cookie("device", credential, DEVICE_TTL),
                                                   self.cookie("session", "", 0)])
        sid = self.cookie_value(headers, "session")
        session = self.sessions.get(sid)
        credential = self.cookie_value(headers, "device")
        outgoing = []
        if not session and path == "/api/session" and method == "GET" and credential:
            phone = self.phones.lookup(credential)
            if phone:
                sid, cookie = self.start(1800, headers, phone=phone)
                session = self.sessions[sid]
                outgoing.append(cookie)
        if not session and path == "/api/session" and method == "GET":
            return response({"signed_in": False})
        require(session, 401, "Please sign in again.")
        if method == "POST":
            token = headers.get("x-csrf-token", "")
            require(token.isascii() and secrets.compare_digest(token, session.csrf), 403, "Refresh and try again.")
        if method == "POST" and path == "/auth/logout":
            self.forget(sid)
            if session.phone and self.phones.lookup(credential) == session.phone:
                self.phones.remove(session.phone)
            return response({"ok": True}, headers=[self.cookie("session", "", 0), self.cookie("device", "", 0)])
        owner = False
        if session.access:
            try:
                user, owner = self.role(await self.ha.identity(session.access))
                require(user == session.user, 403, "Account changed. Sign in again.")
            except Problem as exc:
                if exc.status in (401, 403):
                    self.forget(sid)
                raise
        self.check_session(sid, session, credential)
        session.idle = self.clock() + 1800
        if method == "GET" and path == "/api/session":
            return response(dict(signed_in=True, csrf=session.csrf, can_pair=owner,
                                 rooms=[dict(id=k, label=v) for k, v in self.config.rooms.items()]), headers=outgoing)
        if path == "/api/phones" or path.startswith("/api/phones/"):
            require(owner, 403, "Only the Home Assistant owner can manage phones.")
            if method == "GET" and path == "/api/phones":
                return response({"phones": self.phones.list()})
            revoke = re.fullmatch(r"/api/phones/([a-f0-9]{24})/revoke", path)
            require(method == "POST" and revoke, 404, "Unknown page.")
            self.phones.remove(revoke[1])
            for key, entry in list(self.sessions.items()):
                if entry.phone == revoke[1]:
                    self.forget(key)
            return response({"phones": self.phones.list()})
        if method == "POST" and path == "/api/pairing":
            require(owner, 403, "Only the Home Assistant owner can pair a phone.")
            self.invites = {k: v for k, v in self.invites.items()
                            if self.sessions[v[1]].user != session.user}
            name = data.get("label", "Caregiver phone")
            require(label(name, 60))
            code = f"{secrets.randbelow(100_000_000):08d}"
            self.invites[hashlib.sha256(code.encode()).digest()] = (self.clock() + 300, sid, name)
            return response({"code": code, "expires_in": 300})
        route = re.fullmatch(r"/api/rooms/(" + ROOM + r")/(state|alarms|preferences)", path)
        require(route and route[1] in self.config.rooms, 404, "Unknown room or page.")
        if method == "GET" and route[2] == "state":
            return response(await self.rooms.state(route[1]))
        require(method == "POST" and route[2] != "state", 405, "Method not allowed.")
        return response(await self.rooms.change(route[1], route[2], data, headers,
                                                lambda: self.check_session(sid, session, credential)))

    async def handle(self, reader, writer):
        if self.active >= 48:
            writer.close()
            return
        self.active += 1
        wire = h11.Connection(h11.SERVER, max_incomplete_event_size=8192)
        target = ""
        try:
            headers, body, method = {}, bytearray(), ""
            async with asyncio.timeout(5):
                while True:
                    event = wire.next_event()
                    if event is h11.NEED_DATA:
                        wire.receive_data(await reader.read(8192))
                    elif isinstance(event, h11.Request):
                        method, target = event.method.decode("ascii"), event.target.decode("ascii")
                        require(len(target) <= 3072 and len(event.headers) <= 48)
                        for k, v in event.headers:
                            key = k.decode("ascii")
                            require(key not in headers)
                            headers[key] = v.decode("latin1")
                        require("transfer-encoding" not in headers)
                        require(int(headers.get("content-length", 0)) <= 8192, 413, "Request too large.")
                    elif isinstance(event, h11.Data):
                        body.extend(event.data)
                        require(len(body) <= 8192, 413, "Request too large.")
                    elif isinstance(event, h11.EndOfMessage):
                        break
                    else:
                        raise Problem(400, "Incomplete request.")
            async with asyncio.timeout(24):
                status, fields, payload = await self.dispatch(method, target, headers, body,
                                                             writer.get_extra_info("peername")[0])
        except Problem as exc:
            status, fields, payload = response({"error": exc.message}, exc.status)
        except (ValueError, UnicodeError, h11.ProtocolError):
            status, fields, payload = response({"error": "Invalid request."}, 400)
        except (TimeoutError, OSError, httpx.HTTPError, WebSocketException):
            status, fields, payload = response({"error": "Service unavailable. Refresh before retrying."}, 503)
        except Exception:
            status, fields, payload = response({"error": "Portal unavailable."}, 500)
        finally:
            self.active -= 1
        if target.partition("?")[0] == "/auth/callback" and status >= 400:
            status, fields, payload = response(status=303, headers=[("Location", "/signin-failed"),
                                                                   self.cookie("flow", "", 0, "Lax")])
        try:
            fields += list(HEADERS.items()) + [("Content-Length", str(len(payload))), ("Connection", "close")]
            writer.write(wire.send(h11.Response(status_code=status, headers=fields)))
            writer.write(wire.send(h11.Data(data=payload)) + wire.send(h11.EndOfMessage()))
            async with asyncio.timeout(2):
                await writer.drain()
        except (OSError, h11.ProtocolError, TimeoutError):
            pass
        finally:
            writer.close()

    async def serve(self, host, port):
        server = await asyncio.start_server(self.handle, host, port, limit=16384)
        try:
            async with server:
                while True:
                    await asyncio.sleep(30)
                    self.prune()
        finally:
            for sid in list(self.sessions):
                self.forget(sid)
            await asyncio.gather(*self.revocations)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/config/portal.json"))
    parser.add_argument("--control-dir", type=Path, default=Path("/control"))
    parser.add_argument("--state-dir", type=Path, default=Path("/state"))
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17868)
    args = parser.parse_args()
    try:
        with args.config.open("rb") as stream:
            raw = stream.read(LIMIT + 1)
        require(len(raw) <= LIMIT and 0 < args.port < 65536)
        app = Portal(Config.parse(object_json(raw)), args.control_dir, state_dir=args.state_dir)
        asyncio.run(app.serve(args.bind, args.port))
    except (ValueError, OSError, sqlite3.Error, Problem):
        parser.exit(1, "Portal configuration or listener unavailable.\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
