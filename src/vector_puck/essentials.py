"""Bounded voice tools for cooking, local sleep audio and private notifications."""

from __future__ import annotations

from pathlib import Path
import re

from .ambient import SOUNDS, SleepState
from .cookbook import Cookbook
from .notices import Notices
from .units import convert


def tool(name, description, properties):
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


def tools():
    nullable = {"type": ["string", "null"]}
    recipe = {
        "type": ["object", "null"],
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "maxLength": 80},
            "servings": {"type": "number", "minimum": 1, "maximum": 100},
            "ingredients": {
                "type": "array",
                "minItems": 1,
                "maxItems": 40,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string", "maxLength": 80},
                        "quantity": {"type": ["number", "null"]},
                        "unit": {"type": "string", "maxLength": 30},
                    },
                    "required": ["name", "quantity", "unit"],
                },
            },
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": 30,
                "items": {"type": "string", "maxLength": 600},
            },
            "notes": {"type": "string", "maxLength": 800},
        },
        "required": ["title", "servings", "ingredients", "steps", "notes"],
    }
    return [
        tool(
            "sleep_sound",
            "Play local rain, thunderstorm, white noise or brown noise on THIS speaker. Use for bedtime/sleep sounds, never research or pretend to play them. Default duration ten hours and level 30. No music service needed. start/resume/pause/stop/status; inactive fields null. Duration 60-43200 seconds, level 5-60 is a bounded digital level, not device volume.",
            {
                "action": {
                    "type": "string",
                    "enum": ["start", "resume", "pause", "stop", "status"],
                },
                "sound": {"type": ["string", "null"], "enum": [None, *SOUNDS]},
                "seconds": {"type": ["integer", "null"]},
                "level": {"type": ["integer", "null"]},
            },
        ),
        tool(
            "cooking_recipe",
            "Use for explicitly requested SAVED or GUIDED cooking workflows. For 'create/save a recipe', construct a coherent ingredient/quantity/step draft in recipe and action save; never claim imported or tested. Honor stated allergies; do not change them. title and servings null for save. start selects an existing title; next/previous/repeat/resume retain room progress; ingredients reads scaled amounts; scale changes servings but NEVER temperature or cooking times. add_ingredients explicitly adds scaled quantities to the shared shopping list (max ten ingredients per request). list reads titles. Don't start appliances. Ordinary cooking advice can remain conversational. Recipe only for save; other fields null unless needed.",
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "save",
                        "start",
                        "next",
                        "previous",
                        "repeat",
                        "resume",
                        "ingredients",
                        "scale",
                        "list",
                        "add_ingredients",
                    ],
                },
                "title": nullable,
                "servings": {"type": ["number", "null"]},
                "recipe": recipe,
            },
        ),
        tool(
            "unit_conversion",
            "Deterministic local length, mass, US volume, temperature and other unit conversions. Mass ounces differ from fluid ounces. Cups to grams needs ingredient density; ask rather than guess. No cloud lookup required.",
            {
                "amount": {"type": "number"},
                "from_unit": {"type": "string", "maxLength": 40},
                "to_unit": {"type": "string", "maxLength": 40},
            },
        ),
        tool(
            "notification_status",
            "Read or dismiss status-only delivery notices, or explain the light ring. No order contents, account access, or product details. Do not dismiss weather warnings with this tool.",
            {"action": {"type": "string", "enum": ["read", "dismiss", "ring"]}},
        ),
    ]


NAMES = {t["name"] for t in tools()}


def direct_change(text):
    """A local gate; a tool proposal is never its own mutation authorization."""
    text = text.lower().replace("’", "'")
    if re.search(
        r"\b(?:don't|do not|never|not|without|unless|if|pretend|imagine|example|hypothetical|explain how)\b",
        text,
    ):
        return False
    if re.search(r"\b(?:what happens|how (?:do|can|would|to)|would (?:it|i|we))\b", text):
        return False
    return bool(
        re.search(
            r"\b(?:play|start|stop|pause|resume|turn off|quiet|save|create|make|write|cook|next|previous|back|scale|double|halve|add|dismiss|clear|continue|put on|need|want)\b",
            text,
        )
    )


class Essentials:
    def __init__(self, state: Path, *, root=None, room="default", kitchen=None, clock=None):
        kwargs = {} if clock is None else {"clock": clock}
        state = Path(state)
        root = state if root is None else Path(root)
        self.recipes = Cookbook(root / "cookbook.sqlite3", room, **kwargs)
        self.sleep = SleepState(state / "sleep.sqlite3", **kwargs)
        self.notices = Notices(root / "notices.sqlite3", **kwargs)
        self.kitchen = kitchen
        self.ring_info = None
        self.last_tool = None
        self.playback_ready = False

    def tools(self):
        return tools()

    def context(self):
        current = self.recipes.current()
        return {
            "saved_recipes": self.recipes.titles(),
            "cooking": None
            if current is None
            else {
                "title": current["recipe"]["title"],
                "step": current["step"] + 1,
                "servings": current["servings"],
            },
            "sleep": self.sleep.get(),
            "delivery_provider_connected": False,
        }

    def fast_path(self, text):
        if not isinstance(text, str):
            return None
        words = re.sub(r"^hey[ ,]+(?:vector|victor)[ ,]*", "", text.casefold()).strip(" .!?")
        words = re.sub(r"^please\s+|\s+please$", "", words)
        words = re.sub(r"\b(?:thunderstorms?|storm sounds?)\b", "thunderstorm", words)
        if words in {"play thunderstorm for a minute", "play thunderstorm for one minute",
                     "play thunderstorm sounds for a minute", "play thunderstorm sounds for one minute"}:
            return "sleep_sound", dict(action="start", sound="thunderstorm", seconds=60, level=30)
        for name in SOUNDS:
            if words in {
                f"play {name}",
                f"start {name}",
                f"play {name} sounds",
                f"put on {name}",
                f"play {name} for ten hours",
                f"play {name} for 10 hours",
            }:
                return "sleep_sound", dict(action="start", sound=name, seconds=36000, level=30)
        controls = {
            "stop sleep sounds": "stop",
            "stop the sleep sounds": "stop",
            "turn off sleep sounds": "stop",
            "pause sleep sounds": "pause",
            "resume sleep sounds": "resume",
            "sleep sound status": "status",
        }
        if self.sleep.get():
            controls.update({"stop": "stop", "pause": "pause", "stop the sounds": "stop"})
        if words in controls:
            return "sleep_sound", dict(action=controls[words], sound=None, seconds=None, level=None)
        recipes = {
            "next step": "next",
            "next recipe step": "next",
            "previous step": "previous",
            "repeat that step": "repeat",
            "repeat the step": "repeat",
            "resume cooking": "resume",
            "read the ingredients": "ingredients",
            "what are the ingredients": "ingredients",
            "what recipes do we have": "list",
            "what's in the recipe book": "list",
            "add the recipe ingredients to our shopping list": "add_ingredients",
        }
        if words in recipes:
            return "cooking_recipe", dict(
                action=recipes[words], title=None, servings=None, recipe=None
            )
        if words.startswith("start cooking "):
            return "cooking_recipe", dict(
                action="start", title=words[len("start cooking ") :], servings=None, recipe=None
            )
        for phrase, action in {
            "read my notifications": "read",
            "read our notifications": "read",
            "clear delivery notifications": "dismiss",
            "why are you lit up": "ring",
            "why is your ring on": "ring",
        }.items():
            if words == phrase:
                return "notification_status", {"action": action}
        match = re.fullmatch(
            r"(?:convert |what is |what's )?(-?\d+(?:\.\d+)?) ([a-z ]+?) (?:in|to) ([a-z ]+)", words
        )
        if match:
            return "unit_conversion", dict(
                amount=float(match[1]), from_unit=match[2], to_unit=match[3]
            )
        return None

    def handle(self, name, args, guard=lambda: None, utterance=""):
        if name not in NAMES or not isinstance(args, dict):
            raise ValueError("Invalid essentials tool")
        expected = set(next(t["parameters"]["properties"] for t in tools() if t["name"] == name))
        if set(args) != expected:
            raise ValueError("Invalid essentials fields")
        action = args.get("action")
        reading = (
            name == "unit_conversion"
            or (name == "notification_status" and action in {"read", "ring"})
            or (name == "sleep_sound" and action == "status")
            or (name == "cooking_recipe" and action in {"repeat", "resume", "ingredients", "list"})
        )
        guard()
        if not reading and not direct_change(utterance):
            return "Please ask directly for that change. Nothing was changed."
        self.last_tool = name
        if name == "unit_conversion":
            result = convert(args["amount"], args["from_unit"], args["to_unit"])
            return result["spoken"]
        if name == "sleep_sound":
            if action != "start" and any(
                args[k] is not None for k in ("sound", "seconds", "level")
            ):
                raise ValueError("Unused sleep fields must be null")
            if action == "start":
                if not self.playback_ready:
                    return "Local sleep playback isn't connected to this speaker. No sound was started."
                seconds = args["seconds"] if args["seconds"] is not None else 36000
                self.sleep.start(
                    args["sound"],
                    seconds,
                    args["level"] if args["level"] is not None else 30,
                    guard,
                )
                return f"Queued {args['sound']} for {seconds // 3600} hours and {(seconds % 3600) // 60} minutes on this speaker. It starts after this reply. Say stop sleep sounds to end it."
            if action in {"stop", "pause", "resume"}:
                if action == "resume" and not self.playback_ready:
                    return "Local sleep playback isn't connected to this speaker. Nothing resumed."
                value = self.sleep.change(action, guard)
                if not value:
                    return "No sleep sound is active."
                return {
                    "stop": "Stopped the sleep sounds.",
                    "pause": "Paused the sleep sounds. The original end time is unchanged.",
                    "resume": "Queued the sleep sounds to resume after this reply, until the original end time.",
                }[action]
            if action != "status":
                raise ValueError("Invalid sleep action")
            value = self.sleep.get()
            return (
                "No sleep sound is active."
                if not value
                else ("Paused " if value["paused"] else "Scheduled ")
                + value["sound"]
                + f" with about {max(0, round((value['ends'] - self.sleep.clock()) / 60))} minutes left."
            )
        if name == "notification_status":
            if action == "ring":
                return (
                    self.ring_info()
                    if self.ring_info
                    else "Solid red means the microphone is physically muted. Pulsing red is a weather warning; orange is a watch. Alternating blue and orange is a flood watch; blue and red is a flood warning. Blue alone is a flood advisory; yellow is another advisory. White means unread notifications. A brief green pulse means an update was verified. RGB means an update is running. Weather takes priority over notifications; clearing deliveries never clears a weather warning."
                )
            if action == "read":
                return self.notices.read()
            if action == "dismiss":
                return self.notices.dismiss(guard)
            raise ValueError("Invalid notification action")
        if name == "cooking_recipe":
            if action == "save":
                if args["title"] is not None or args["servings"] is not None:
                    raise ValueError("Unused recipe fields must be null")
                return self.recipes.save(args["recipe"], guard)
            if (
                args["recipe"] is not None
                or (action != "start" and args["title"] is not None)
                or (action != "scale" and args["servings"] is not None)
            ):
                raise ValueError("Unused recipe fields must be null")
            if action == "list":
                return (
                    "Saved recipes: " + "; ".join(self.recipes.titles()) + "."
                    if self.recipes.titles()
                    else "No recipes saved yet. Ask me to create and save a recipe."
                )
            if action == "start":
                return self.recipes.start(args["title"], guard)
            if action in {"next", "previous", "repeat", "resume"}:
                return self.recipes.step({"next": 1, "previous": -1}.get(action, 0), guard)
            if action == "scale":
                return self.recipes.scale(args["servings"], guard)
            if action in {"ingredients", "add_ingredients"}:
                values = self.recipes.ingredients()
                if not values:
                    return "Start a saved recipe first."
                if action == "ingredients":
                    return "Ingredients: " + "; ".join(values) + "."
                if self.kitchen is None or len(values) > 10 or any(len(v) > 80 for v in values):
                    return (
                        "These ingredients need to be added in smaller groups. Nothing was added."
                    )
                return self.kitchen.shopping(dict(action="add", items=values, offset=0), guard)
            raise ValueError("Invalid cooking action")
        raise ValueError("Unsupported essentials tool")

    async def execute(self, name, args, guard, utterance=""):
        import asyncio

        try:
            reply = await asyncio.to_thread(self.handle, name, args, guard, utterance)
        except (ValueError, TypeError, ArithmeticError):
            reply = "I need clearer recipe quantities, compatible units, or a sleep duration between one minute and twelve hours. Nothing further was changed."
        return {"message": reply}
