"""Pure calling-window maths.

All business rules use the *provider's simulated clock*, expressed as
timezone-aware datetimes in IST.  Nothing in this module reads the machine clock.

The provider allows ``window_grace_seconds`` (120s) of skew.  The dispatcher
never relies on that grace: it only starts a call when ``now`` is strictly inside
the intersection of the campaign window and the regulatory window.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# A window never repeats for longer than a week; scan two weeks to be safe
# (also covers a campaign that is closed for an entire week in bad input).
_MAX_SCAN_DAYS = 14


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("window maths requires timezone-aware datetimes")
    return dt.astimezone(IST)


def _hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def _bounds(day: datetime, start: str, end: str) -> tuple[datetime, datetime]:
    """Return ``[start, end)`` for the IST calendar day containing ``day``."""
    sh, sm = _hm(start)
    eh, em = _hm(end)
    s = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
    e = day.replace(hour=eh, minute=em, second=0, microsecond=0)
    return s, e


def _segment(
    day: datetime,
    camp_start: str,
    camp_end: str,
    reg_start: str | None,
    reg_end: str | None,
) -> tuple[datetime, datetime] | None:
    """Intersection of the campaign window and the regulatory window for a day."""
    s, e = _bounds(day, camp_start, camp_end)
    if reg_start is not None and reg_end is not None:
        rs, re = _bounds(day, reg_start, reg_end)
        s, e = max(s, rs), min(e, re)
    if e <= s:
        return None
    return s, e


def in_window(
    t: datetime,
    camp_start: str,
    camp_end: str,
    days: tuple[str, ...] | list[str] | None,
    *,
    reg_start: str | None = None,
    reg_end: str | None = None,
) -> bool:
    """True when ``t`` is strictly inside the allowed windows.

    Mirrors the provider's own rule (``[start, end)``, IST, on the listed days,
    additionally constrained by the regulatory window when supplied).
    """
    lt = to_ist(t)
    if days is not None and DAY_KEYS[lt.weekday()] not in days:
        return False
    seg = _segment(lt, camp_start, camp_end, reg_start, reg_end)
    if seg is None:
        return False
    s, e = seg
    return s <= lt < e


def next_open(
    t: datetime,
    camp_start: str,
    camp_end: str,
    days: tuple[str, ...] | list[str] | None,
    *,
    reg_start: str | None = None,
    reg_end: str | None = None,
) -> datetime:
    """Earliest instant ``>= t`` that is inside the allowed windows.

    If the windows are empty/misconfigured we return ``t`` unchanged; the
    dispatcher's strict ``in_window`` check still refuses to dial.
    """
    lt = to_ist(t)
    start_of_day = lt.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(_MAX_SCAN_DAYS):
        day = start_of_day + timedelta(days=offset)
        if days is not None and DAY_KEYS[day.weekday()] not in days:
            continue
        seg = _segment(day, camp_start, camp_end, reg_start, reg_end)
        if seg is None:
            continue
        s, e = seg
        if offset == 0:
            if lt < s:
                return s
            if lt < e:
                return lt
            continue
        return s
    return lt


def campaign_is_open(now: datetime, campaign, regulatory) -> bool:
    """Convenience wrapper used by the dispatcher (campaign + regulatory)."""
    return in_window(
        now,
        campaign.window.start,
        campaign.window.end,
        campaign.window.days,
        reg_start=regulatory[0],
        reg_end=regulatory[1],
    )


def campaign_next_open(now: datetime, campaign, regulatory) -> datetime:
    return next_open(
        now,
        campaign.window.start,
        campaign.window.end,
        campaign.window.days,
        reg_start=regulatory[0],
        reg_end=regulatory[1],
    )
