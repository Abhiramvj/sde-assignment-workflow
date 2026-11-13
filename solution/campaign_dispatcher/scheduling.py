"""Pure scheduling decisions.

This module knows *nothing* about HTTP, SQLite or wall-clock time.  Everything it
needs is passed in, which makes the priority rules trivially testable with a fake
clock and hand-built lead rows.

Priority (R6): due callbacks > due retries > fresh leads.  Within a class the
oldest due time wins, then campaign/lead id for determinism.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping

KIND_CALLBACK = "callback"
KIND_RETRY = "retry"
KIND_FRESH = "fresh"

KIND_RANK = {KIND_CALLBACK: 0, KIND_RETRY: 1, KIND_FRESH: 2}

# Any datetime used as a "never" sentinel.  Compared, never rendered.  It must be
# timezone-aware so it can be compared with provider datetimes.
_NEVER = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Candidate:
    campaign_id: str
    lead_id: str
    phone: str
    tenant_id: str
    attempts: int
    kind: str
    pending_at: datetime | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.campaign_id, self.lead_id)


@dataclass(frozen=True)
class Constraints:
    tenant_active: Mapping[str, int]
    tenant_max: Mapping[str, int]
    locked_phones: frozenset[str] | set[str]
    open_leads: frozenset[tuple[str, str]] | set[tuple[str, str]]
    dnd_phones: frozenset[str] | set[str]
    max_attempts: Mapping[str, int]


def _sort_key(c: Candidate) -> tuple:
    pending = c.pending_at or _NEVER
    return (KIND_RANK.get(c.kind, KIND_FRESH), pending, c.campaign_id, c.lead_id)


def eligible(c: Candidate, now: datetime, constraints: Constraints) -> bool:
    """Return True if this candidate may be dialled right now.

    ``now`` must already be inside the campaign's calling window; window gating
    is the caller's responsibility (the set of open campaigns is computed once
    per tick).
    """
    if c.attempts >= constraints.max_attempts[c.campaign_id]:
        return False
    if c.pending_at is not None and c.pending_at > now:
        return False  # retry / callback not due yet
    if c.phone in constraints.dnd_phones:
        return False
    if c.phone in constraints.locked_phones:
        return False  # R3: one active call per phone
    if c.key in constraints.open_leads:
        return False  # already dialling / active
    max_calls = constraints.tenant_max.get(c.tenant_id, 0)
    if constraints.tenant_active.get(c.tenant_id, 0) >= max_calls:
        return False  # R2: tenant-wide concurrency
    return True


def pick_next(
    candidates: Iterable[Candidate],
    now: datetime,
    constraints: Constraints,
) -> Candidate | None:
    choices = [c for c in candidates if eligible(c, now, constraints)]
    if not choices:
        return None
    return min(choices, key=_sort_key)


def retry_due_at(
    ended_at: datetime,
    status: str | None,
    policy,
    next_open,
) -> datetime:
    """Earliest allowed time for the next attempt after a failed attempt (R5).

    ``next_open`` is injected so the window maths stays in :mod:`.windows`.
    """
    minutes = policy.minutes_for(status)
    return next_open(ended_at + timedelta(minutes=minutes))


def callback_due_at(callback_at: datetime, next_open) -> datetime:
    """Earliest allowed time to honour a callback (R6).

    If ``callback_at`` is outside the calling window, ``next_open`` pushes it to
    the next opening.  The provider measures lateness against exactly this
    instant, so scheduling at ``next_open(callback_at)`` keeps lateness minimal.
    """
    return next_open(callback_at)
