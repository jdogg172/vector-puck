"""Weather status and bounded native LED leases, independent of speech or microphones."""
from __future__ import annotations

import math
import time


WEATHER_KINDS = frozenset({
    "warning", "watch", "advisory", "flood-warning", "flood-watch", "flood-advisory",
})


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def weather_indicator(snapshot, *, now=None):
    """Select one currently effective alert from a freshness-labelled local snapshot.

    Feed validity and each alert's own expiration are independent bounds. A warning
    outranks a watch, regardless of hazard; tornado wins ties, followed by severe
    thunderstorm, flood, and other hazards. This never fetches or speaks anything.
    """
    now = time.time() if now is None else now
    none = {"kind": "none"}
    if not isinstance(snapshot, dict) or not _number(now):
        return none
    valid = snapshot.get("valid_until")
    if (snapshot.get("available") is not True or snapshot.get("stale") is not False
            or not _number(valid) or valid <= now):
        return none
    data = snapshot.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return none
    candidates = []
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        start, end, event = item.get("effective_at"), item.get("expires_at"), item.get("event")
        if (not _number(start) or not _number(end) or not start <= now < end
                or not isinstance(event, str) or len(event) > 120):
            continue
        event = " ".join(event.lower().split())
        level = next((kind for kind in ("warning", "watch", "advisory")
                      if event.endswith(" " + kind)), None)
        if level is None:
            continue
        flood = "flood" in event.split()
        hazard = 3 if "tornado" in event else 2 if "severe thunderstorm" in event else int(flood)
        kind = "flood-" + level if flood else level
        candidates.append(({"warning": 3, "watch": 2, "advisory": 1}[level], hazard, kind, end))
    if not candidates:
        return none
    _, _, kind, end = max(candidates)
    # The local device also expires the light if the controller disconnects.
    return {"kind": kind, "duration_ms": max(1, math.ceil(min(60, valid - now, end - now) * 1000))}


def native_weather(indicator, *, capable):
    """Older satellites keep their generic severity colour, without new sequences."""
    if capable or not indicator["kind"].startswith("flood-"):
        return indicator
    return dict(indicator, kind=indicator["kind"].removeprefix("flood-"))


class IndicatorLease:
    """Renew before native expiry; do not restart an animation every half second."""

    def __init__(self):
        self.kind = None
        self.renew_at = 0.0

    def due(self, indicator, *, now=None):
        now = time.monotonic() if now is None else now
        return indicator["kind"] != self.kind or ("duration_ms" in indicator and now >= self.renew_at)

    def sent(self, indicator, *, now=None):
        now = time.monotonic() if now is None else now
        self.kind = indicator["kind"]
        self.renew_at = now + min(20, indicator.get("duration_ms", 60000) / 2000)
