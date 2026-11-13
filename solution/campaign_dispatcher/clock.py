"""The provider's simulated clock, mirrored locally.

``sim_now = sim_now_at_sync + (monotonic_now - monotonic_at_sync) * speed``

Anchoring on ``time.monotonic()`` instead of absolute machine time means we do
not care about wall-clock/UTC differences, and re-syncing every few real seconds
keeps drift far below the provider's 120 simulated-second grace.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from .provider import ProviderClient


class ProviderClock:
    def __init__(self, client: ProviderClient, resync_every: float = 10.0):
        self._client = client
        self._resync_every = resync_every
        self._lock = threading.Lock()
        self._sim_at_sync: datetime
        self._mono_at_sync: float
        self.speed: float = 1.0
        self._sync()

    def _sync(self) -> None:
        data = self._client.get_clock()
        self._sim_at_sync = datetime.fromisoformat(data["sim_now"])
        self.speed = float(data["speed"])
        self._mono_at_sync = time.monotonic()

    def now(self) -> datetime:
        with self._lock:
            if time.monotonic() - self._mono_at_sync > self._resync_every:
                try:
                    self._sync()
                except Exception:
                    # Keep the old anchor; a late sync is better than stopping
                    # the workflow.
                    self._mono_at_sync = time.monotonic()
            return self._sim_at_sync + timedelta(
                seconds=(time.monotonic() - self._mono_at_sync) * self.speed
            )

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    def sim_seconds_until(self, when: datetime) -> float:
        return max(0.0, (when - self.now()).total_seconds())
