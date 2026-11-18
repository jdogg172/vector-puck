"""Content-free progress checks for the normal wake listener.

Transport packets alone do not prove that the consumer can accept another wake.
Recovery closes the connection; it never unmutes or executes buffered speech.
"""
from __future__ import annotations

import time


class ListenerState:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.epoch = 0
        self.awaiting_pipeline = True
        self.stage = "starting"
        self.updated = clock()
        self.blocked_since = None
        self.audio_packets = self.stale_packets = 0
        self.confirmed_wakes = self.rejected_wakes = 0

    def progress(self, stage, epoch, awaiting_pipeline=False):
        self.stage, self.epoch = stage, epoch
        self.awaiting_pipeline = awaiting_pipeline
        self.updated = self.clock()

    def stalled(self, epoch, *, idle, transport_fresh, owns_turn=True):
        blocked = idle and transport_fresh and (
            self.awaiting_pipeline or self.epoch != epoch or not owns_turn
            or self.clock() - self.updated > 4
        )
        if not blocked:
            self.blocked_since = None
            return False
        if self.blocked_since is None:
            self.blocked_since = self.clock()
        return self.clock() - self.blocked_since >= 3

    def snapshot(self):
        return {"stage": self.stage, "awaiting_pipeline": self.awaiting_pipeline,
                "progress_age_seconds": round(self.clock() - self.updated, 1),
                "audio_packets": self.audio_packets, "stale_packets": self.stale_packets,
                "confirmed_wakes": self.confirmed_wakes, "rejected_wakes": self.rejected_wakes}
