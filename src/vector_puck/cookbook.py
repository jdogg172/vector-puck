"""Private, versioned recipes and resumable room cooking sessions.

Recipe text is data. It cannot execute tools, timers, appliances, or network calls.
Generated recipes are explicitly drafts; this module does not certify food safety.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import time


def text(value, limit):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError("Invalid recipe text")
    if any(ord(c) < 32 for c in value):
        raise ValueError("Control characters are not recipe text")
    return value.strip()


def amount(value, *, nullable=False):
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Invalid quantity")
    result = Decimal(str(value))
    if not result.is_finite() or not 0 < result <= 100000:
        raise ValueError("Quantity outside bounds")
    return result


def number(value):
    return format(value.quantize(Decimal("0.001")), "f").rstrip("0").rstrip(".")


def validate(recipe):
    if not isinstance(recipe, dict) or set(recipe) != {
        "title",
        "servings",
        "ingredients",
        "steps",
        "notes",
    }:
        raise ValueError("Invalid recipe fields")
    title = text(recipe["title"], 80)
    servings = amount(recipe["servings"])
    if servings > 100:
        raise ValueError("Too many servings")
    ingredients = recipe["ingredients"]
    if not isinstance(ingredients, list) or not 1 <= len(ingredients) <= 40:
        raise ValueError("Use one to forty ingredients")
    cleaned = []
    for item in ingredients:
        if not isinstance(item, dict) or set(item) != {"name", "quantity", "unit"}:
            raise ValueError("Invalid ingredient")
        quantity = amount(item["quantity"], nullable=True)
        unit = item["unit"]
        if not isinstance(unit, str) or len(unit) > 30 or any(ord(c) < 32 for c in unit):
            raise ValueError("Invalid unit label")
        cleaned.append(
            {
                "name": text(item["name"], 80),
                "quantity": None if quantity is None else str(quantity),
                "unit": unit.strip(),
            }
        )
    steps = recipe["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
        raise ValueError("Use one to thirty steps")
    notes = recipe["notes"]
    if not isinstance(notes, str) or len(notes) > 800 or any(ord(c) < 32 for c in notes):
        raise ValueError("Invalid recipe notes")
    result = dict(
        title=title,
        servings=str(servings),
        ingredients=cleaned,
        steps=[text(s, 600) for s in steps],
        notes=notes,
    )
    if len(json.dumps(result)) > 16000:
        raise ValueError("Recipe too large")
    return result


class Cookbook:
    def __init__(self, path: Path, room="default", clock=time.time):
        self.path, self.room, self.clock = Path(path), text(room, 40), clock
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS recipes (
                  title TEXT,version INTEGER,body TEXT,created REAL,
                  PRIMARY KEY(title,version));
                CREATE TABLE IF NOT EXISTS cooking (
                  room TEXT PRIMARY KEY,title TEXT,version INTEGER,step INTEGER,servings TEXT);
            """)

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def save(self, recipe, guard=lambda: None):
        value = validate(recipe)
        key = value["title"].casefold()
        with self.tx() as db:
            count = db.execute("SELECT count(*) FROM recipes").fetchone()[0]
            if count >= 1000:
                raise ValueError("Recipe book full; export before pruning")
            version = db.execute(
                "SELECT coalesce(max(version),0)+1 FROM recipes WHERE title=?", (key,)
            ).fetchone()[0]
            guard()
            db.execute(
                "INSERT INTO recipes VALUES(?,?,?,?)",
                (key, version, json.dumps(value), self.clock()),
            )
        return f"Saved {value['title']}, version {version}, for {number(Decimal(value['servings']))} servings as an AI recipe draft. Review its ingredients and cooking guidance before using it."

    def titles(self):
        with self.tx() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT title FROM recipes GROUP BY title ORDER BY max(created) DESC LIMIT 20"
                )
            ]

    def current(self):
        with self.tx() as db:
            row = db.execute(
                "SELECT r.body,c.* FROM cooking c JOIN recipes r ON c.title=r.title AND c.version=r.version WHERE c.room=?",
                (self.room,),
            ).fetchone()
        return None if row is None else {**dict(row), "recipe": json.loads(row["body"])}

    def start(self, title, guard=lambda: None):
        key = text(title, 80).casefold()
        with self.tx() as db:
            row = db.execute(
                "SELECT version,body FROM recipes WHERE title=? ORDER BY version DESC LIMIT 1",
                (key,),
            ).fetchone()
            if row is None:
                return (
                    "I don't have a saved recipe by that exact name. Ask what's in the recipe book."
                )
            value = json.loads(row["body"])
            guard()
            db.execute(
                "INSERT OR REPLACE INTO cooking VALUES(?,?,?,?,?)",
                (self.room, key, row["version"], 0, value["servings"]),
            )
        return self.step()

    def step(self, move=0, guard=lambda: None):
        with self.tx() as db:
            row = db.execute(
                "SELECT c.*,r.body FROM cooking c JOIN recipes r ON r.title=c.title AND r.version=c.version WHERE room=?",
                (self.room,),
            ).fetchone()
            if row is None:
                return "Start a saved recipe first."
            value = json.loads(row["body"])
            index = min(max(row["step"] + move, 0), len(value["steps"]) - 1)
            if move:
                guard()
                db.execute("UPDATE cooking SET step=? WHERE room=?", (index, self.room))
            prefix = "That was the last step. " if move > 0 and index == row["step"] else ""
        return f"{prefix}{value['title']}, step {index + 1} of {len(value['steps'])}: {value['steps'][index]}"

    def scale(self, servings, guard=lambda: None):
        value = amount(servings)
        if value > 100:
            raise ValueError("Too many servings")
        with self.tx() as db:
            guard()
            changed = db.execute(
                "UPDATE cooking SET servings=? WHERE room=?", (str(value), self.room)
            ).rowcount
        if not changed:
            return "Start a saved recipe first."
        return (
            "Ingredient quantities are now scaled for "
            + number(value)
            + " servings. Cooking times and oven temperatures have not been scaled. Ask for the ingredients."
        )

    def ingredients(self):
        session = self.current()
        if session is None:
            return []
        recipe = session["recipe"]
        factor = Decimal(session["servings"]) / Decimal(recipe["servings"])
        return [
            " ".join(
                filter(
                    None,
                    [
                        number(Decimal(i["quantity"]) * factor)
                        if i["quantity"] is not None
                        else "to taste",
                        i["unit"],
                        i["name"],
                    ],
                )
            )
            for i in recipe["ingredients"]
        ]
