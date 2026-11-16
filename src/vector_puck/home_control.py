"""Optional semantic HA allowlist. No generic service, shell, discovery, or cloud token path."""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from .state import Cancelled

ACTIONS = (
    "status",
    "volume_down",
    "volume_up",
    "volume_set",
    "mute",
    "unmute",
    "source",
    "cool_set",
    "heat_set",
    "cool_adjust",
    "heat_adjust",
    "run",
)
MEDIA_ACTIONS = {"status", "volume_down", "volume_up", "volume_set", "mute", "unmute", "source"}
CLIMATE_ACTIONS = {"status", "cool_set", "heat_set", "cool_adjust", "heat_adjust"}
ALIAS = re.compile(r"[a-z][a-z0-9_]{0,31}")
# RFC 1918 prefixes plus TEST-NET-1, as canonical packed IPv4 integers.
PRIVATE_NETS = tuple(
    ipaddress.ip_network((address, prefix))
    for address, prefix in ((0x0A000000, 8), (0xAC100000, 12), (0xC0A80000, 16), (0xC0000200, 24))
)
DOC_NET = ipaddress.ip_network("192.0.2.0/24")
UNITS = {"F": "F", "C": "C", "°F": "F", "°C": "C"}


class HomeError(ValueError):
    """Fixed local diagnostic codes only, never remote bodies or private configuration."""

    def __init__(self, code: str, *, accepted=False):
        super().__init__(code)
        self.accepted = accepted


def _pairs(entries):
    result = {}
    for key, value in entries:
        if key in result:
            raise HomeError("duplicate_field")
        result[key] = value
    return result


def _constant(_value):
    raise HomeError("nonfinite_json")


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, TypeError, RecursionError):
        raise HomeError("invalid_json") from None


def _number(value):
    return type(value) in (int, float) and abs(value) <= 1_000_000 and math.isfinite(value)


def _keys(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or set(value) - set(required) - set(optional)
        or set(required) - set(value)
    ):
        raise HomeError("invalid_fields")


def _public_text(value, limit):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= limit
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ,()'&.!?-]*", value)
        or re.search(r"\b[a-z_]+\.[a-z0-9_]+\b", value, re.I)
    ):
        raise HomeError("invalid_public_label")


def read_token(path: Path) -> str:
    """Read one external private bearer token; no OpenAI-key format or circular import."""
    try:
        path = Path(path)
        repository = Path(__file__).resolve().parents[2]
        if not path.is_absolute() or path.resolve().is_relative_to(repository):
            raise HomeError("token_must_be_external")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="ascii") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o777 not in (0o600, 0o400)
                or info.st_uid != os.geteuid()
                or not 20 <= info.st_size <= 4097
            ):
                raise HomeError("token_permissions")
            token = stream.read(4098).strip()
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{20,4096}", token):
            raise HomeError("invalid_token_file")
        return token
    except (OSError, UnicodeError, ValueError):
        raise HomeError("private_token_unavailable") from None


def _guard(guard):
    if guard() is False:
        raise Cancelled("Request cancelled")


def _write_intent(utterance):
    if not isinstance(utterance, str) or not 1 <= len(utterance.strip()) <= 800:
        return False
    text = utterance.lower().replace("’", "'")
    # Deliberately conservative: polite "could you turn it down" remains valid.
    blocked = (
        r"\b(?:not|never|no|without|avoid|stop|cancel|unless|if|suppose|imagine|"
        r"hypothetical|hypothetically|pretend|simulate|example|explain|describe)\b|"
        r"\b(?:don'?t|can'?t|won'?t|shouldn'?t|wouldn'?t|couldn'?t)\b|"
        r"\bwhat (?:would|happens|if)\b|\b(?:how|why) (?:do|would|can|does|to)\b|"
        r"\b(?:would|could|should) (?:it|that|i|we)\b|\bused to\b"
    )
    affirmative = (
        r"\b(?:turn|set|change|lower|raise|make|mute|unmute|switch|open|show|launch|run|"
        r"put|bring|go|play|watch|heat|cool|warmer|colder|hotter|quieter|louder|less|more|"
        r"up|down|drop|bump|back|see)\b"
    )
    return re.search(blocked, text) is None and re.search(affirmative, text) is not None


class HomeControl:
    def __init__(self, config: dict, token: str, *, client_factory=None):
        self._config = self.validate_config(config)
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,4096}", token):
            raise HomeError("invalid_token")
        self._token = token
        self._base = self._config["base_url"].rstrip("/")
        self._targets = {}
        for category in ("media_players", "thermostats", "tv_actions"):
            for alias, entry in self._config[category].items():
                self._targets[alias] = (category, entry)
        self._client_factory = client_factory
        self._lock = asyncio.Lock()

    @staticmethod
    def validate_config(data: dict) -> dict:
        _keys(
            data, {"base_url", "token_file"}, {"mode", "media_players", "thermostats", "tv_actions"}
        )
        data = copy.deepcopy(data)
        url = data["base_url"]
        try:
            if (
                not isinstance(url, str)
                or len(url) > 100
                or "?" in url
                or "#" in url
                or any(ord(c) <= 32 for c in url)
            ):
                raise ValueError
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ("", "/")
                or not re.fullmatch(r"[0-9.]+(?::[0-9]{1,5})?", parsed.netloc)
            ):
                raise ValueError
            ip = ipaddress.ip_address(parsed.hostname)
            if ip.version != 4 or not any(ip in net for net in PRIVATE_NETS):
                raise ValueError
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError
        except (ValueError, TypeError):
            raise HomeError("base_url_must_be_local_ipv4") from None
        path = data["token_file"]
        if (
            not isinstance(path, str)
            or len(path) > 512
            or not Path(path).is_absolute()
            or any(ord(c) < 32 for c in path)
        ):
            raise HomeError("invalid_token_path")
        data.setdefault("mode", "dry_run")
        if data["mode"] not in ("dry_run", "control"):
            raise HomeError("invalid_mode")
        aliases, entities = set(), set()
        fields = {
            "media_players": {"entity_id", "label", "max_volume", "volume_step", "sources"},
            "thermostats": {"entity_id", "label", "unit", "minimum", "maximum", "max_delta"},
            "tv_actions": {"entity_id", "label", "description"},
        }
        domains = {
            "media_players": "media_player",
            "thermostats": "climate",
            "tv_actions": "script",
        }
        for category in fields:
            data.setdefault(category, {})
            entries = data[category]
            if not isinstance(entries, dict) or len(entries) > 16:
                raise HomeError("invalid_allowlist")
            for alias, entry in entries.items():
                if not isinstance(alias, str) or not ALIAS.fullmatch(alias) or alias in aliases:
                    raise HomeError("invalid_or_duplicate_alias")
                aliases.add(alias)
                _keys(entry, fields[category])
                entity = entry["entity_id"]
                if (
                    not isinstance(entity, str)
                    or not re.fullmatch(domains[category] + r"\.[a-z0-9_]{1,100}", entity)
                    or entity in entities
                ):
                    raise HomeError("invalid_or_duplicate_entity")
                entities.add(entity)
                _public_text(entry["label"], 60)
                if category == "media_players":
                    if (
                        not _number(entry["max_volume"])
                        or not 0 < entry["max_volume"] <= 1
                        or not _number(entry["volume_step"])
                        or not 0 < entry["volume_step"] <= min(0.2, entry["max_volume"])
                    ):
                        raise HomeError("invalid_volume_bounds")
                    if not isinstance(entry["sources"], dict) or len(entry["sources"]) > 16:
                        raise HomeError("invalid_sources")
                    for source_alias, source in entry["sources"].items():
                        if (
                            not isinstance(source_alias, str)
                            or not ALIAS.fullmatch(source_alias)
                            or not isinstance(source, str)
                            or not 1 <= len(source) <= 100
                            or any(ord(c) < 32 for c in source)
                        ):
                            raise HomeError("invalid_source")
                    if len(set(entry["sources"].values())) != len(entry["sources"]):
                        raise HomeError("duplicate_source")
                elif category == "thermostats":
                    if (
                        entry["unit"] not in ("F", "C")
                        or any(not _number(entry[k]) for k in ("minimum", "maximum", "max_delta"))
                        or not -100 <= entry["minimum"] < entry["maximum"] <= 200
                        or not 0 < entry["max_delta"] <= entry["maximum"] - entry["minimum"]
                    ):
                        raise HomeError("invalid_temperature_bounds")
                else:
                    _public_text(entry["description"], 160)
        if not 1 <= len(aliases) <= 32:
            raise HomeError("allowlist_requires_1_to_32_targets")
        return data

    @classmethod
    def read_config(cls, path: Path) -> dict:
        try:
            with Path(path).open("rb") as stream:
                raw = stream.read(65_537)
            if len(raw) > 65_536:
                raise HomeError("config_bound")
            return cls.validate_config(_json(raw))
        except (OSError, ValueError, TypeError):
            raise HomeError("invalid_home_config") from None

    @classmethod
    def load(cls, path: Path) -> HomeControl:
        config = cls.read_config(path)
        return cls(config, read_token(Path(config["token_file"])))

    def tools(self) -> list[dict]:
        descriptions = []
        options = set()
        for alias, (category, entry) in self._targets.items():
            if category == "media_players":
                detail = "media volume/mute/status"
                if entry["sources"]:
                    detail += "; source options " + ", ".join(entry["sources"])
                options.update(entry["sources"])
            elif category == "thermostats":
                detail = (
                    f"heat/cool targets in {entry['unit']}; {entry['minimum']}..{entry['maximum']}; "
                    f"maximum change {entry['max_delta']}"
                )
            else:
                detail = "run/status; " + entry["description"]
            descriptions.append(f"{alias} ({entry['label']}): {detail}")
        return [
            {
                "type": "function",
                "name": "home_action",
                "strict": True,
                "description": "Propose one owner-allowlisted home action. "
                + "; ".join(descriptions),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "target": {"type": "string", "enum": sorted(self._targets)},
                        "action": {"type": "string", "enum": list(ACTIONS)},
                        "value": {
                            "type": ["number", "null"],
                            "description": "volume_set: normalized 0..1; volume_up/down: positive increment no larger "
                            "than configured step, or null for default step. Heat/cool set: target in "
                            "configured F/C; adjust: signed delta. Otherwise null.",
                        },
                        "option": {
                            "type": ["string", "null"],
                            "enum": [None, *sorted(options)],
                            "description": "Source alias for source; null for every other action.",
                        },
                    },
                    "required": ["target", "action", "value", "option"],
                },
            }
        ]

    def _arguments(self, name, args, utterance):
        _keys(args, {"target", "action", "value", "option"})
        if (
            name != "home_action"
            or not isinstance(args["target"], str)
            or args["target"] not in self._targets
            or not isinstance(args["action"], str)
            or args["action"] not in ACTIONS
        ):
            raise HomeError("invalid_proposal")
        target, action, value, option = (args[k] for k in ("target", "action", "value", "option"))
        category, entry = self._targets[target]
        allowed = (
            MEDIA_ACTIONS
            if category == "media_players"
            else CLIMATE_ACTIONS
            if category == "thermostats"
            else {"status", "run"}
        )
        if action not in allowed or (value is not None and not _number(value)):
            raise HomeError("action_not_allowed")
        numeric = {"volume_set", "cool_set", "heat_set", "cool_adjust", "heat_adjust"}
        if (action in numeric and value is None) or (
            action not in numeric | {"volume_up", "volume_down"} and value is not None
        ):
            raise HomeError("invalid_action_value")
        if action == "source":
            if not isinstance(option, str) or option not in entry["sources"]:
                raise HomeError("source_not_allowed")
        elif option is not None:
            raise HomeError("unexpected_option")
        if action != "status" and not _write_intent(utterance):
            raise HomeError("write_intent_not_clear")
        return category, entry

    @staticmethod
    def _result(args, status, message, *, submitted=False, observed=False, state=None):
        return {
            "status": status,
            "target": args.get("target"),
            "action": args.get("action"),
            "submitted": submitted,
            "observed": observed,
            "message": message,
            "state": state or {},
        }

    async def _request(self, client, method, path, body=None):
        accepted = False
        try:
            async with asyncio.timeout(8):
                async with client.stream(method, self._base + path, json=body) as response:
                    if response.status_code != 200:
                        raise HomeError("ha_http_error")
                    accepted = method == "POST"
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 65_536:
                            raise HomeError("ha_response_bound", accepted=accepted)
            parsed = _json(raw)
            if method == "GET":
                if (
                    not isinstance(parsed, dict)
                    or parsed.get("entity_id") != path.rsplit("/", 1)[1]
                    or not isinstance(parsed.get("state"), str)
                    or not isinstance(parsed.get("attributes"), dict)
                ):
                    raise HomeError("invalid_ha_state")
                return parsed
            if not isinstance(parsed, (list, dict)):
                raise HomeError("invalid_service_response", accepted=True)
            return None  # Never use or return changed-entity attributes from service responses.
        except HomeError as exc:
            if accepted:
                exc.accepted = True
            raise
        except Exception:
            raise HomeError("ha_request_unavailable", accepted=accepted) from None

    @staticmethod
    def _unit(attrs):
        values = [attrs[key] for key in ("temperature_unit", "unit_of_measurement") if key in attrs]
        if not values or any(not isinstance(v, str) or v not in UNITS for v in values):
            return None
        units = {UNITS[v] for v in values}
        return units.pop() if len(units) == 1 else None

    def _snapshot(self, category, entry, raw):
        attrs, reported = raw["attributes"], raw["state"]
        known = {
            "on",
            "off",
            "idle",
            "playing",
            "paused",
            "buffering",
            "heat",
            "cool",
            "heat_cool",
            "auto",
            "dry",
            "fan_only",
            "unknown",
            "unavailable",
        }
        result = {"status": reported if reported in known else "unknown"}
        if category == "media_players":
            value = attrs.get("volume_level")
            if _number(value) and 0 <= value <= 1:
                result["volume"] = round(value, 6)
            if type(attrs.get("is_volume_muted")) is bool:
                result["muted"] = attrs["is_volume_muted"]
            for alias, source in entry["sources"].items():
                if attrs.get("source") == source:
                    result["source"] = alias
        elif category == "thermostats":
            unit = self._unit(attrs)
            if unit == entry["unit"]:
                result["unit"] = unit
                for source, key in (
                    ("temperature", "target"),
                    ("target_temp_low", "heat_target"),
                    ("target_temp_high", "cool_target"),
                    ("current_temperature", "current"),
                ):
                    if _number(attrs.get(source)) and -100 <= attrs[source] <= 200:
                        result[key] = attrs[source]
        return result

    def _plan(self, category, entry, args, raw):
        action, value, attrs = args["action"], args["value"], raw["attributes"]
        body = {"entity_id": entry["entity_id"]}
        if raw["state"] in ("unknown", "unavailable"):
            raise HomeError("target_unavailable")
        if category == "media_players":
            if raw["state"] not in {"on", "off", "idle", "playing", "paused", "buffering"}:
                raise HomeError("media_state_invalid")
            if action.startswith("volume_"):
                current = attrs.get("volume_level")
                if not _number(current) or not 0 <= current <= 1:
                    raise HomeError("volume_state_missing")
                if action == "volume_set":
                    target = value
                else:
                    step = entry["volume_step"] if value is None else value
                    if not 0 < step <= entry["volume_step"]:
                        raise HomeError("volume_step_limit")
                    target = current + step * (1 if action == "volume_up" else -1)
                target = round(target, 6)
                if not 0 <= target <= entry["max_volume"]:
                    raise HomeError("volume_limit")
                body["volume_level"] = target
                return "media_player/volume_set", body, {"volume": target}
            if action in ("mute", "unmute"):
                body["is_volume_muted"] = action == "mute"
                return "media_player/volume_mute", body, {"muted": action == "mute"}
            available = attrs.get("source_list")
            source = entry["sources"][args["option"]]
            if not isinstance(available, list) or source not in available:
                raise HomeError("source_unavailable")
            body["source"] = source
            return "media_player/select_source", body, {"source": args["option"]}
        if category == "tv_actions":
            if raw["state"] not in ("on", "off"):
                raise HomeError("script_state_invalid")
            return "script/turn_on", body, {}
        if self._unit(attrs) != entry["unit"]:
            raise HomeError("temperature_unit_unverified")
        mode = raw["state"]
        side = "cool" if action.startswith("cool_") else "heat"
        range_mode = mode == "heat_cool"
        if not range_mode and mode != side:
            raise HomeError("temperature_mode_mismatch")
        key = "target_temp_high" if side == "cool" else "target_temp_low"
        current = attrs.get(key if range_mode else "temperature")
        low, high = attrs.get("target_temp_low"), attrs.get("target_temp_high")
        if not _number(current) or (
            range_mode and (not _number(low) or not _number(high) or not low < high)
        ):
            raise HomeError("temperature_state_missing")
        target = current + value if action.endswith("_adjust") else value
        if (
            not _number(target)
            or not entry["minimum"] <= target <= entry["maximum"]
            or abs(target - current) > entry["max_delta"] + 1e-9
        ):
            raise HomeError("temperature_limit")
        if any(not _number(attrs.get(k)) for k in ("min_temp", "max_temp")):
            raise HomeError("device_temperature_bounds_missing")
        if not attrs["min_temp"] <= target <= attrs["max_temp"]:
            raise HomeError("device_temperature_limit")
        if range_mode:
            if side == "cool":
                high = target
            else:
                low = target
            if not low < high:
                raise HomeError("temperature_range_crossing")
            if not (
                entry["minimum"] <= low < high <= entry["maximum"]
                and attrs["min_temp"] <= low < high <= attrs["max_temp"]
            ):
                raise HomeError("opposite_temperature_outside_bounds")
            body.update(target_temp_low=low, target_temp_high=high)
            expected = {
                "heat_target": low,
                "cool_target": high,
                "unit": entry["unit"],
                "status": mode,
            }
        else:
            body["temperature"] = target
            expected = {"target": target, "unit": entry["unit"], "status": mode}
        return "climate/set_temperature", body, expected

    async def execute(self, name, args, guard, utterance="") -> dict:
        _guard(guard)
        try:
            category, entry = self._arguments(name, args, utterance)
        except (HomeError, TypeError):
            # Do not reflect arbitrary model arguments, even on validation errors.
            return self._result(
                {}, "rejected", "That home-control proposal was rejected. No command was sent."
            )
        if self._client_factory is None:
            if ipaddress.ip_address(urlsplit(self._base).hostname) in DOC_NET:
                return self._result(
                    args, "rejected", "Replace the documentation-only Home Assistant address first."
                )
            import httpx

            factory = httpx.AsyncClient
            timeout = httpx.Timeout(5.0, connect=3.0)
        else:
            factory, timeout = self._client_factory, 5.0
        async with self._lock:
            _guard(guard)
            async with factory(
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
                headers={"Authorization": "Bearer " + self._token},
            ) as client:
                path = "/api/states/" + entry["entity_id"]
                try:
                    raw = await self._request(client, "GET", path)
                except HomeError:
                    _guard(guard)
                    return self._result(
                        args, "unavailable", "I could not read that target. No command was sent."
                    )
                _guard(guard)
                before = self._snapshot(category, entry, raw)
                if args["action"] == "status":
                    return self._result(
                        args,
                        "observed",
                        self._status_message(entry, before),
                        observed=True,
                        state=before,
                    )
                try:
                    service, body, expected = self._plan(category, entry, args, raw)
                except HomeError:
                    return self._result(
                        args,
                        "rejected",
                        "That change could not be validated against the current state and limits. No command was sent.",
                        state=before,
                    )
                if self._config["mode"] == "dry_run":
                    return self._result(
                        args,
                        "dry_run",
                        "Dry run: the proposed change passed validation. No command was sent.",
                        state=before,
                    )
                if expected and all(before.get(k) == v for k, v in expected.items()):
                    return self._result(
                        args,
                        "observed",
                        "Home Assistant already reports the requested setting. No command was sent.",
                        observed=True,
                        state=before,
                    )
                if category == "tv_actions" and raw["state"] == "on":
                    return self._result(
                        args,
                        "rejected",
                        "That script is already running. No new command was sent.",
                        state=before,
                    )
                _guard(guard)  # Immediately before the one fixed service dispatch; no retries.
                try:
                    await self._request(client, "POST", "/api/services/" + service, body)
                except HomeError as exc:
                    _guard(guard)
                    return self._result(
                        args,
                        "submitted" if exc.accepted else "unverified",
                        "The request outcome is unverified. Check Home Assistant before trying again.",
                        submitted=True if exc.accepted else None,
                    )
                _guard(guard)
                try:
                    after_raw = await self._request(client, "GET", path)
                    _guard(guard)
                    after = self._snapshot(category, entry, after_raw)
                except HomeError:
                    return self._result(
                        args,
                        "submitted",
                        "Home Assistant accepted the command, but I could not verify the resulting state.",
                        submitted=True,
                    )
                if category == "tv_actions":
                    return self._result(
                        args,
                        "submitted",
                        "Home Assistant accepted the script command. The TV screen result is not verified.",
                        submitted=True,
                        state=after,
                    )
                verified = all(after.get(k) == v for k, v in expected.items())
                if verified:
                    return self._result(
                        args,
                        "observed",
                        "Home Assistant now reports the requested setting. "
                        + self._status_message(entry, after),
                        submitted=True,
                        observed=True,
                        state=after,
                    )
                return self._result(
                    args,
                    "submitted",
                    "Home Assistant accepted the command, but its reported state does not yet confirm the change.",
                    submitted=True,
                    state=after,
                )

    @staticmethod
    def _status_message(entry, state):
        parts = [entry["label"] + " reports " + state["status"].replace("_", " ") + "."]
        if "volume" in state:
            parts.append(f"Volume {round(state['volume'] * 100)} percent.")
        if "muted" in state:
            parts.append("Muted." if state["muted"] else "Unmuted.")
        if "source" in state:
            parts.append("Source " + state["source"].replace("_", " ") + ".")
        for key, name in (
            ("current", "Current temperature"),
            ("target", "Target"),
            ("heat_target", "Heat target"),
            ("cool_target", "Cool target"),
        ):
            if key in state:
                parts.append(f"{name} {state[key]:g} degrees {state['unit']}.")
        return " ".join(parts)
