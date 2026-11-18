"""Room-local family portal API. This Linux module also works outside a package."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import socket
from socketserver import UnixStreamServer
import stat
import struct
import tempfile
import threading
import time


_DEFAULTS = {"brightness": 80, "color": "white", "spoilers": "ask"}
_CHOICES = {
    "brightness": (20, 50, 80),
    "color": ("white", "cyan", "green", "violet", "amber"),
    "spoilers": ("ask", "allow", "avoid"),
}
_WEATHER = {"watch", "warning", "advisory", "flood-watch", "flood-warning", "flood-advisory"}
_MAX_BODY = 4096
_HEALTH_MAX_AGE = 10


class _Rejected(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("Duplicate JSON field")
    return obj


def _revision(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return '"' + hashlib.sha256(canonical).hexdigest() + '"'


def _preferences(fields, *, partial):
    if not isinstance(fields, dict) or not fields or fields.keys() - _CHOICES.keys():
        raise ValueError("Invalid preference fields")
    if not partial and fields.keys() != _CHOICES.keys():
        raise ValueError("Incomplete room preferences")
    for key, value in fields.items():
        if type(value) is not type(_DEFAULTS[key]) or value not in _CHOICES[key]:
            raise ValueError("Invalid " + key)
    return fields


def decorate_indicator(indicator: dict, preferences: dict) -> dict:
    """Apply room brightness to messages/weather and room colour only to messages."""
    kind = indicator["kind"]
    if kind != "message" and kind not in _WEATHER:
        return indicator
    result = dict(indicator, brightness_pct=preferences["brightness"])
    if kind == "message":
        result["color"] = preferences["color"]
    return result


def _read_object(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Invalid room state file")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValueError("Oversized room state file")
    return json.loads(data, object_pairs_hook=_object)


def _alarm_args(data):
    args = dict(action=None, name=None, at=None, days=[], date=None, minutes=None)
    if not isinstance(data, dict) or data.keys() - args.keys():
        raise ValueError("Invalid alarm fields")
    args.update(data)
    action = args["action"]
    if action not in ("set", "cancel", "dismiss", "snooze"):
        raise ValueError("Invalid alarm action")
    name = args["name"]
    if name is not None:
        if not isinstance(name, str) or not name.isprintable() or not 1 <= len(name.strip()) <= 60:
            raise ValueError("Invalid alarm name")
        args["name"] = " ".join(name.split())
    days = args["days"]
    if (
        not isinstance(days, list)
        or len(days) > 7
        or any(type(day) is not int or not 0 <= day <= 6 for day in days)
        or len(days) != len(set(days))
    ):
        raise ValueError("Use distinct weekdays from 0 to 6")
    at, date, minutes = args["at"], args["date"], args["minutes"]
    if at is not None and (
        not isinstance(at, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", at)
    ):
        raise ValueError("Use HH:MM for the alarm time")
    if date is not None and (
        not isinstance(date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date)
    ):
        raise ValueError("Use YYYY-MM-DD for the alarm date")
    if minutes is not None:
        maximum = {"set": 1440, "snooze": 60}.get(action, 0)
        if type(minutes) is not int or not 1 <= minutes <= maximum:
            raise ValueError("Invalid minutes for alarm action")
    if action != "set" and (at is not None or date is not None or days):
        raise ValueError("Only setting an alarm takes a schedule")
    if action == "set":
        args["name"] = args["name"] or "alarm"
        if (at is None) == (minutes is None) or (date and days) or (minutes and (days or date)):
            raise ValueError("Choose a local schedule or a relative delay")
    return args


class _HTTP(BaseHTTPRequestHandler):
    timeout = 2

    def log_message(self, *args):
        pass  # Requests may contain room-local information.

    def reply(self, status, obj):
        data = json.dumps(obj, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        self.reply(code, {"error": "Invalid HTTP request"})

    def dispatch(self):
        api = self.server.api
        try:
            if self.requestline.split()[1] != self.path or self.path not in (
                "/state",
                "/alarms",
                "/preferences",
            ):
                raise _Rejected(404, "Unknown route")
            if (self.path == "/state") != (self.command == "GET"):
                raise _Rejected(405, "Invalid method for route")
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") is not None or len(lengths) > 1:
                raise ValueError("Invalid request framing")
            if self.command == "GET":
                if lengths and lengths != ["0"]:
                    raise ValueError("GET takes no body")
                result = api.state()
            else:
                if self.headers.get_content_type() != "application/json":
                    raise _Rejected(415, "Use application/json")
                if not lengths or not re.fullmatch(r"[0-9]{1,9}", lengths[0]):
                    raise ValueError("Expected Content-Length")
                length = int(lengths[0])
                if length > _MAX_BODY:
                    raise _Rejected(413, "Request is too large")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Incomplete JSON body")
                data = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
                matches = self.headers.get_all("If-Match", [])
                absent = self.headers.get_all("If-None-Match", [])
                if self.path == "/preferences":
                    if absent:
                        raise ValueError("Room preferences already exist")
                    result = {"preferences": api.update_preferences(data, matches)}
                else:
                    result = api.change_alarm(data, matches, absent)
            self.reply(200, result)
        except _Rejected as exc:
            self.reply(exc.status, {"error": str(exc)})
        except (ValueError, RecursionError):
            self.reply(400, {"error": "Invalid request values"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self.reply(500, {"error": "Room state unavailable"})

    do_GET = do_POST = dispatch


class _Server(UnixStreamServer):
    def __init__(self, path, api):
        super().__init__(path, _HTTP, bind_and_activate=False)
        self.api = api
        self._lock = threading.Lock()
        self._closing = False
        self._client = None

    def verify_request(self, request, address):
        credentials = struct.Struct("iII")
        _, uid, _ = credentials.unpack(
            request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, credentials.size)
        )
        return uid == os.geteuid()

    def process_request(self, request, address):
        with self._lock:
            if self._closing:
                self.shutdown_request(request)
                return
            self._client = request
        try:
            super().process_request(request, address)
        finally:
            with self._lock:
                self._client = None

    def shutdown(self):
        with self._lock:
            self._closing = True
            if self._client:
                with contextlib.suppress(OSError):
                    self._client.shutdown(socket.SHUT_RDWR)
        super().shutdown()

    def handle_error(self, request, client_address):
        pass  # A local client may disconnect before reading the response.


@contextlib.contextmanager
def _socket_directory(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Management socket directory must be owned by this UID and mode 0700")
        # Serialize stale socket recovery between residents starting together.
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _remove_stale(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("Unexpected management socket path")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()
            return
    raise ValueError("Room management socket is already active")


class RoomAPI:
    def __init__(self, state_dir, alarms, socket_path, health_filename, health):
        self.socket_path = socket_path
        self.alarms = alarms
        self.health = health
        self.health_path = state_dir / health_filename
        self.prefs_path = state_dir / "room-preferences.json"
        self._prefs = _DEFAULTS.copy()
        self._lock = threading.Lock()
        self._server = self._thread = self._identity = None

    def start(self) -> None:
        if self._server is not None:
            return
        directory = self.socket_path.parent
        for path in (directory, self.prefs_path.parent):
            if path.resolve() != path:
                raise ValueError("Room directories cannot traverse symlinks")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _socket_directory(directory):
            _remove_stale(self.socket_path)
            try:
                self._prefs = _preferences(_read_object(self.prefs_path, _MAX_BODY), partial=False)
            except FileNotFoundError:
                self._prefs = _DEFAULTS.copy()
            server = _Server(str(self.socket_path), self)
            try:
                server.server_bind()
                self._identity = self.socket_path.lstat()
                self.socket_path.chmod(0o600)
                server.server_activate()
                thread = threading.Thread(
                    target=server.serve_forever, args=(0.05,), name="room-management", daemon=True
                )
                thread.start()
                self._server, self._thread = server, thread
            except BaseException:
                server.server_close()
                self._unlink_owned()
                raise

    def _unlink_owned(self):
        try:
            if self._identity and os.path.samestat(self._identity, self.socket_path.lstat()):
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._identity = None

    def close(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._thread.join()
        try:
            with _socket_directory(self.socket_path.parent):
                self._unlink_owned()
        except FileNotFoundError:
            pass
        finally:
            self._server.server_close()
            self._server = self._thread = self._identity = None

    def preferences(self) -> dict:
        """Return a detached snapshot for this turn or indicator decision."""
        # Writers replace this dict as a unit. Voice and LEDs never wait on fsync.
        return self._prefs.copy()

    def update_preferences(self, fields, matches) -> dict:
        fields = _preferences(fields, partial=True)
        with self._lock:
            if matches and matches != [_revision(self._prefs)]:
                raise _Rejected(412, "Preferences have changed; refresh room state")
            value = self._prefs | fields
            directory = self.prefs_path.parent
            with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as temp:
                try:
                    json.dump(value, temp)
                    temp.flush()
                    os.fsync(temp.fileno())
                    os.replace(temp.name, self.prefs_path)
                    self._prefs = value
                    fd = os.open(directory, os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    Path(temp.name).unlink(missing_ok=True)
            return value.copy()

    def _health(self):
        try:
            data = self.health() if self.health else _read_object(self.health_path, 16384)
            if isinstance(data, dict):
                stamp = data.get("updated")
                if (
                    type(stamp) in (float, int)
                    and 0 <= time.time() - stamp <= _HEALTH_MAX_AGE
                    and data.get("connected") is True
                    and type(data.get("muted")) is bool
                ):
                    version = data.get("native_version", data.get("version"))
                    if (
                        not isinstance(version, str)
                        or not 1 <= len(version) <= 80
                        or not version.isprintable()
                        or version != version.strip()
                    ):
                        version = "unknown"
                    return dict(online=True, muted=data["muted"], version=version)
        except (ValueError, OSError, OverflowError, RecursionError):
            pass
        return dict(online=False, muted=True, version="unknown")

    def _schedules(self):
        with self.alarms.store.transaction() as db:
            rows = db.execute("""SELECT id,name,at,days,on_date AS date,status,due
                                 FROM alarms WHERE enabled=1 ORDER BY due,id""")
            return [dict(row, days=json.loads(row["days"])) for row in rows]

    def state(self) -> dict:
        return dict(self._health(), alarms=self._schedules(), preferences=self.preferences())

    def change_alarm(self, data, matches, absent):
        args = _alarm_args(data)
        if matches and (
            len(matches) != 1
            or not re.fullmatch(r'"(?:[0-9a-f]{32}|[0-9a-f]{64})"', matches[0])
            or not args["name"]
        ):
            raise ValueError("If-Match requires a quoted alarm ID or revision and a name")
        if absent and (absent != ["*"] or matches or args["action"] != "set"):
            raise ValueError("Use If-None-Match: * only to create an alarm")
        # Alarms.handle/tick and native dismissal share this reentrant store lock.
        # Hold it across the ID check and handle so a voice update cannot interleave.
        with self.alarms.store.lock:
            rows = self._schedules()
            name = args["name"]
            selected = [r for r in rows if name is None or r["name"].casefold() == name.casefold()]
            if (
                matches
                and (
                    len(selected) != 1
                    or matches[0] not in ('"' + selected[0]["id"] + '"', _revision(selected[0]))
                )
            ) or (absent and selected):
                raise _Rejected(412, "Alarm has changed; refresh room state")
            if name and len(selected) > 1:
                raise _Rejected(409, "Ambiguous alarm name")
            if name and selected:
                args["name"] = selected[0]["name"]
            if args["action"] == "set" and not selected and len(rows) >= 16:
                raise _Rejected(409, "Room alarm limit reached")
            if args["action"] == "cancel" and len(selected) != 1:
                raise _Rejected(409, "Choose one enabled alarm")
            if args["action"] in ("dismiss", "snooze") and not any(
                r["status"] == "ringing" for r in selected
            ):
                raise _Rejected(409, "No matching ringing alarm")
            message = self.alarms.handle(args)
            return {"message": message, "alarms": self._schedules()}


def make_room_api(
    *,
    state_dir: Path | str,
    alarms,
    room_id: str,
    socket_dir: Path | str | None,
    health_filename: str = "health.json",
    health: Callable[[], dict] | None = None,
) -> RoomAPI | None:
    """Build an optional same-UID service. Start and close it around the resident loop."""
    if socket_dir is None or socket_dir == "":
        return None
    if not isinstance(room_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", room_id):
        raise ValueError("Invalid room ID")
    if not isinstance(health_filename, str) or not re.fullmatch(
        r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,95}", health_filename
    ):
        raise ValueError("Health filename must be a simple local filename")
    directory, state = Path(socket_dir), Path(state_dir)
    for path in (directory, state):
        if not path.is_absolute() or path == Path("/") or path.resolve() != path:
            raise ValueError("Use absolute dedicated directories without traversal or symlinks")
    socket_path = directory / (room_id + ".sock")
    if len(os.fsencode(socket_path)) > 107:
        raise ValueError("Management socket path exceeds Linux AF_UNIX limit")
    return RoomAPI(state, alarms, socket_path, health_filename, health)
