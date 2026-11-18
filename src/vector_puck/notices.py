"""Status-only notices. Reject order contents at ingestion, before voice context."""

from __future__ import annotations

from contextlib import closing
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import time


class Notices:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY,kind TEXT,status TEXT,expires REAL,seen INTEGER DEFAULT 0)"
            )

    def ingest_delivery(self, event):
        # Do not accept a raw email/order and hope a speech prompt hides gifts.
        if not isinstance(event, dict) or set(event) != {"id", "status", "expires"}:
            raise ValueError("Only an opaque id, status and expiry may enter delivery notices")
        if not isinstance(event["id"], str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]{8,128}", event["id"]
        ):
            raise ValueError("Use an opaque event id")
        if event["status"] not in {"out_for_delivery", "delivered", "delayed"}:
            raise ValueError("Unsupported delivery status")
        until = event["expires"]
        if (
            type(until) not in {int, float}
            or not math.isfinite(until)
            or not self.clock() < until <= self.clock() + 7 * 86400
        ):
            raise ValueError("Invalid notice expiry")
        key = hashlib.sha256(event["id"].encode()).hexdigest()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM notices WHERE expires<=?", (self.clock(),))
            if (
                db.execute("SELECT count(*) FROM notices").fetchone()[0] >= 200
                and not db.execute("SELECT 1 FROM notices WHERE id=?", (key,)).fetchone()
            ):
                raise ValueError("Notice limit reached")
            db.execute(
                "INSERT INTO notices VALUES(?,'delivery',?,?,0) ON CONFLICT(id) DO UPDATE SET status=excluded.status,expires=excluded.expires,seen=CASE WHEN status=excluded.status THEN seen ELSE 0 END",
                (key, event["status"], until),
            )

    def pending(self):
        with closing(sqlite3.connect(self.path)) as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT status FROM notices WHERE seen=0 AND expires>? ORDER BY expires LIMIT 20",
                    (self.clock(),),
                )
            ]

    def read(self):
        values = self.pending()
        labels = {
            "delivered": "a package was delivered",
            "out_for_delivery": "a package is out for delivery",
            "delayed": "a delivery is delayed",
        }
        return (
            "; ".join(labels[s] for s in values) + "."
            if values
            else "There are no unread delivery notices. No delivery account is connected by this feature alone."
        )

    def dismiss(self, guard=lambda: None):
        with closing(sqlite3.connect(self.path)) as db, db:
            guard()
            db.execute("UPDATE notices SET seen=1 WHERE expires>?", (self.clock(),))
        return "Cleared the delivery notification light."
