"""Calling-window maths (R1, R5, R6 window parts).

Fixed dates:
  2026-11-12 Thu, 2026-11-13 Fri, 2026-11-14 Sat, 2026-11-15 Sun, 2026-11-16 Mon
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from campaign_dispatcher import windows as w
from conftest import dt


def test_start_inclusive_end_exclusive():
    assert w.in_window(dt("2026-11-12T10:00:00+05:30"), "10:00", "19:00", w.DAY_KEYS)
    assert w.in_window(dt("2026-11-12T18:59:59+05:30"), "10:00", "19:00", w.DAY_KEYS)
    assert not w.in_window(dt("2026-11-12T19:00:00+05:30"), "10:00", "19:00", w.DAY_KEYS)
    assert not w.in_window(dt("2026-11-12T09:59:59+05:30"), "10:00", "19:00", w.DAY_KEYS)


def test_day_filtering_saturday_is_closed_for_weekdays_only():
    saturday = dt("2026-11-14T12:00:00+05:30")
    assert saturday.weekday() == 5
    assert not w.in_window(saturday, "09:30", "18:00", ("mon", "tue", "wed", "thu", "fri"))
    assert w.in_window(saturday, "09:30", "18:00", w.DAY_KEYS)


def test_regulatory_window_intersection():
    # Campaign says 08:00-22:00 but regulation caps it at 09:00-21:00.
    assert not w.in_window(
        dt("2026-11-12T08:30:00+05:30"), "08:00", "22:00", w.DAY_KEYS,
        reg_start="09:00", reg_end="21:00",
    )
    assert w.in_window(
        dt("2026-11-12T09:00:00+05:30"), "08:00", "22:00", w.DAY_KEYS,
        reg_start="09:00", reg_end="21:00",
    )
    assert not w.in_window(
        dt("2026-11-12T21:00:00+05:30"), "08:00", "22:00", w.DAY_KEYS,
        reg_start="09:00", reg_end="21:00",
    )


def test_next_open_before_during_and_after():
    args = ("10:00", "19:00", w.DAY_KEYS)
    assert w.next_open(dt("2026-11-12T08:00:00+05:30"), *args) == dt(
        "2026-11-12T10:00:00+05:30"
    )
    inside = dt("2026-11-12T12:34:00+05:30")
    assert w.next_open(inside, *args) == inside
    assert w.next_open(dt("2026-11-12T19:00:00+05:30"), *args) == dt(
        "2026-11-13T10:00:00+05:30"
    )


def test_next_open_skips_closed_days():
    # Zen-style: weekdays only. Friday 18:30 -> Monday 09:30.
    nxt = w.next_open(
        dt("2026-11-13T18:30:00+05:30"),
        "09:30", "18:00", ("mon", "tue", "wed", "thu", "fri"),
    )
    assert nxt == dt("2026-11-16T09:30:00+05:30")


def test_next_open_respects_regulatory_window():
    nxt = w.next_open(
        dt("2026-11-12T07:00:00+05:30"),
        "06:00", "23:00", w.DAY_KEYS,
        reg_start="09:00", reg_end="21:00",
    )
    assert nxt == dt("2026-11-12T09:00:00+05:30")


def test_provider_style_grace_is_not_needed_by_us():
    """We dial strictly inside the window; 120s of provider grace is a buffer."""
    just_before = dt("2026-11-12T09:59:00+05:30")
    assert not w.in_window(just_before, "10:00", "19:00", w.DAY_KEYS)
    # ... and one minute after close is closed for us too.
    assert not w.in_window(
        dt("2026-11-12T19:01:00+05:30"), "10:00", "19:00", w.DAY_KEYS
    )
