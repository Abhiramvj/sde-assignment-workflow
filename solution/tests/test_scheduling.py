"""Scheduling rules: retry/callback eligibility and priority ordering (R5, R6)."""
from __future__ import annotations

from datetime import timedelta

from campaign_dispatcher import windows as w
from campaign_dispatcher.config import RetryPolicy
from campaign_dispatcher.scheduling import (
    Candidate,
    Constraints,
    KIND_CALLBACK,
    KIND_FRESH,
    KIND_RETRY,
    callback_due_at,
    pick_next,
    retry_due_at,
)
from conftest import dt

POLICY = RetryPolicy(no_answer_minutes=60, busy_minutes=60, failed_minutes=15)


def _open(t):
    return w.next_open(t, "10:00", "19:00", w.DAY_KEYS)


# --------------------------------------------------------------- retry timing
def test_no_answer_and_busy_wait_sixty_minutes():
    ended = dt("2026-11-12T11:00:00+05:30")
    assert retry_due_at(ended, "no_answer", POLICY, _open) == dt(
        "2026-11-12T12:00:00+05:30"
    )
    assert retry_due_at(ended, "busy", POLICY, _open) == dt(
        "2026-11-12T12:00:00+05:30"
    )


def test_failed_waits_fifteen_minutes():
    ended = dt("2026-11-12T11:00:00+05:30")
    assert retry_due_at(ended, "failed", POLICY, _open) == dt(
        "2026-11-12T11:15:00+05:30"
    )


def test_retry_after_close_moves_to_next_window_open():
    # 18:50 + 60min = 19:50 -> outside 10:00-19:00 -> next day 10:00.
    ended = dt("2026-11-12T18:50:00+05:30")
    assert retry_due_at(ended, "no_answer", POLICY, _open) == dt(
        "2026-11-13T10:00:00+05:30"
    )


def test_retry_just_before_close_stays_inside_window():
    # 18:30 + 15min = 18:45, still open.
    ended = dt("2026-11-12T18:30:00+05:30")
    assert retry_due_at(ended, "failed", POLICY, _open) == dt(
        "2026-11-12T18:45:00+05:30"
    )


# ------------------------------------------------------------ callback timing
def test_callback_inside_window_is_honoured_exactly_on_time():
    cb = dt("2026-11-12T15:15:00+05:30")
    assert callback_due_at(cb, _open) == cb


def test_callback_outside_window_moves_to_next_opening():
    cb = dt("2026-11-12T20:15:00+05:30")
    assert callback_due_at(cb, _open) == dt("2026-11-13T10:00:00+05:30")


def test_callback_on_closed_day_moves_to_next_allowed_day():
    # Saturday callback for a weekdays-only campaign -> Monday.
    cb = dt("2026-11-14T12:00:00+05:30")
    nxt = w.next_open(
        cb, "09:30", "18:00", ("mon", "tue", "wed", "thu", "fri")
    )
    assert nxt == dt("2026-11-16T09:30:00+05:30")


# ---------------------------------------------------------------- priority
def _cand(cid, lid, kind, pending, tenant="t1", phone=None, attempts=0):
    return Candidate(
        campaign_id=cid,
        lead_id=lid,
        phone=phone or f"+91{lid}",
        tenant_id=tenant,
        attempts=attempts,
        kind=kind,
        pending_at=pending,
    )


def _constraints(**overrides):
    base = dict(
        tenant_active={"t1": 0},
        tenant_max={"t1": 4},
        locked_phones=frozenset(),
        open_leads=frozenset(),
        dnd_phones=frozenset(),
        max_attempts={"c1": 3, "c2": 3},
    )
    base.update(overrides)
    return Constraints(**base)


NOW = dt("2026-11-12T12:00:00+05:30")


def test_callbacks_before_retries_before_fresh():
    fresh = _cand("c1", "fresh", KIND_FRESH, None)
    retry = _cand("c1", "retry", KIND_RETRY, dt("2026-11-12T11:00:00+05:30"))
    callback = _cand("c1", "cb", KIND_CALLBACK, dt("2026-11-12T11:59:00+05:30"))
    chosen = pick_next([fresh, retry, callback], NOW, _constraints())
    assert chosen is not None and chosen.lead_id == "cb"


def test_retry_beats_fresh_even_if_fresh_is_older():
    fresh = _cand("c1", "aaa", KIND_FRESH, None)
    retry = _cand("c1", "zzz", KIND_RETRY, dt("2026-11-12T11:30:00+05:30"))
    chosen = pick_next([fresh, retry], NOW, _constraints())
    assert chosen.lead_id == "zzz"


def test_within_a_class_oldest_due_time_wins():
    late = _cand("c1", "late", KIND_RETRY, dt("2026-11-12T11:50:00+05:30"))
    early = _cand("c1", "early", KIND_RETRY, dt("2026-11-12T11:10:00+05:30"))
    chosen = pick_next([late, early], NOW, _constraints())
    assert chosen.lead_id == "early"


def test_not_yet_due_is_not_eligible():
    future = _cand("c1", "future", KIND_RETRY, NOW + timedelta(minutes=1))
    assert pick_next([future], NOW, _constraints()) is None


def test_attempt_cap_is_enforced():
    exhausted = _cand("c1", "l", KIND_FRESH, None, attempts=3)
    assert pick_next([exhausted], NOW, _constraints()) is None


def test_dnd_phone_is_never_dialled():
    lead = _cand("c1", "l", KIND_FRESH, None)
    assert pick_next([lead], NOW, _constraints(dnd_phones=frozenset({lead.phone}))) is None


def test_phone_lock_prevents_concurrent_dial_same_number():
    lead = _cand("c1", "l", KIND_FRESH, None)
    assert pick_next(
        [lead], NOW, _constraints(locked_phones=frozenset({lead.phone}))
    ) is None


def test_tenant_concurrency_is_shared_across_campaigns():
    a = _cand("c1", "a", KIND_FRESH, None)
    b = _cand("c2", "b", KIND_FRESH, None)
    full = _constraints(tenant_active={"t1": 4})
    assert pick_next([a, b], NOW, full) is None
    one_free = _constraints(tenant_active={"t1": 3})
    assert pick_next([a, b], NOW, one_free).lead_id == "a"


def test_candidate_with_open_call_is_skipped():
    lead = _cand("c1", "l", KIND_FRESH, None)
    assert pick_next(
        [lead], NOW, _constraints(open_leads=frozenset({("c1", "l")}))
    ) is None
