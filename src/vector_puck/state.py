"""Playback-gated follow-ups and generation tokens invalidate stale work."""
from __future__ import annotations

import time


class Cancelled(RuntimeError):
    pass


class Gate:
    def __init__(self):
        self.epoch = 0
        self.muted = True
        self.connected = True

    def cancel(self):
        self.epoch += 1

    def allowed(self, epoch):
        return self.connected and not self.muted and epoch == self.epoch

    def guard(self, epoch):
        if not self.allowed(epoch):
            raise Cancelled("Request cancelled")


class FollowUp:
    def __init__(self, seconds, turns, session_seconds, clock=time.monotonic):
        self.seconds, self.turns, self.session_seconds, self.clock = seconds, turns, session_seconds, clock
        self.clear()

    def clear(self):
        self.remaining = 0
        self.deadline = self.until = 0.0
        self.pending = None
        self.ready = False

    def begin(self):
        self.clear()
        self.remaining, self.deadline = self.turns, self.clock() + self.session_seconds

    def expect(self, epoch):
        if self.remaining and self.seconds and self.clock() < self.deadline:
            self.pending = epoch

    def played(self, epoch, allowed):
        if self.pending == epoch and allowed and self.clock() < self.deadline:
            self.ready = True
            self.pending = None

    def open(self):
        if not self.ready or not self.remaining or self.clock() >= self.deadline:
            return False
        self.ready = False
        self.until = min(self.clock() + self.seconds, self.deadline)
        return self.active()

    def active(self):
        return 0 < self.until and self.clock() < self.until

    def consume(self):
        if not self.active():
            raise ValueError("Follow-up closed")
        self.until = 0.0
        self.remaining -= 1
