"""Bounded local call controls; no general assistant or home actions."""

from __future__ import annotations

import re


def call_control(text: str, wake_phrase: str = "hey vector") -> str | None:
    """Require an addressed explicit answer/decline, never just overheard 'yes'."""
    if not isinstance(text, str) or len(text) > 300:
        return None
    names = [wake_phrase, wake_phrase.split()[-1]]
    if names[-1] == "vector":
        names.extend(["hey victor", "victor"])
    address = "|".join(r"[ ,.!?]+".join(map(re.escape, name.split())) for name in names)
    match = re.fullmatch(
        r"\s*(?:" + address + r")[ ,.!?]+(?:(?:can|could|would) you )?(?:please )?"
        r"(answer|accept|pick up|decline|reject|hang up|end)"
        r"(?: (?:the |this |incoming )?call)?(?: please)?[ .!?]*",
        text,
        re.I,
    )
    if not match:
        return None
    return "answer" if match[1].lower() in {"answer", "accept", "pick up"} else "end"
