"""Shared fixtures for the dispatcher tests.

All scheduling tests use explicit datetimes (a fake clock); nothing here sleeps
or talks to the mock provider.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Allow `pytest` to run from either the repo root or solution/.
SOLUTION = Path(__file__).resolve().parents[1]
if str(SOLUTION) not in sys.path:
    sys.path.insert(0, str(SOLUTION))

from campaign_dispatcher.config import (  # noqa: E402
    Campaign,
    Config,
    Lead,
    RetryPolicy,
    Tenant,
    Window,
)
from campaign_dispatcher.db import Database  # noqa: E402
from campaign_dispatcher.store import Store  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
ALL_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")


def dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    return parsed


def make_config(
    campaigns: list[Campaign] | None = None,
    tenants: list[Tenant] | None = None,
    leads: list[Lead] | None = None,
    regulatory: tuple[str, str] = ("09:00", "21:00"),
) -> Config:
    campaigns = campaigns or [
        Campaign("c1", "t1", "leads.csv", Window("10:00", "19:00", ALL_DAYS), 3)
    ]
    tenants = tenants or [Tenant("t1", 2)]
    leads = leads or [Lead("c1", "l1", "+910000000001", "t1")]
    return Config(
        sim_start="2026-11-12T17:30:00+05:30",
        timezone="Asia/Kolkata",
        regulatory_start=regulatory[0],
        regulatory_end=regulatory[1],
        window_grace_seconds=120,
        retry_policy=RetryPolicy(),
        webhook_secret="whsec_test",
        tenants={t.tenant_id: t for t in tenants},
        campaigns={c.campaign_id: c for c in campaigns},
        leads=tuple(leads),
    )


@pytest.fixture()
def store(tmp_path) -> Store:
    cfg = make_config()
    db = Database(tmp_path / "state.db")
    s = Store(db, cfg)
    s.seed(cfg.leads)
    yield s
    db.close()


def make_store(cfg: Config, path) -> Store:
    db = Database(path)
    s = Store(db, cfg)
    s.seed(cfg.leads)
    return s


# --------------------------------------------------------------- test helpers
def call_result(
    *,
    campaign_id: str,
    lead_id: str,
    phone: str = "+910000000001",
    status: str,
    disposition: str | None,
    ended_at: str,
    started_at: str | None = None,
    callback_at: str | None = None,
    call_sid: str = "CA1",
) -> dict:
    return {
        "event": "call.completed",
        "call_sid": call_sid,
        "request_id": "",
        "tenant_id": "t1",
        "campaign_id": campaign_id,
        "lead_id": lead_id,
        "phone": phone,
        "status": status,
        "disposition": disposition,
        "callback_at": callback_at,
        "started_at": started_at or ended_at,
        "ended_at": ended_at,
        "duration_sec": 10,
    }


def place_and_accept(
    store: Store,
    *,
    campaign_id: str = "c1",
    lead_id: str = "l1",
    attempt_no: int = 1,
    phone: str = "+910000000001",
    state: str = "active",
    result: dict | None = None,
    call_sid: str | None = None,
) -> str:
    rid = f"au:{campaign_id}:{lead_id}:a{attempt_no}"
    store.create_intent(
        campaign_id=campaign_id,
        lead_id=lead_id,
        tenant_id="t1",
        phone=phone,
        attempt_no=attempt_no,
        request_id=rid,
        now=dt("2026-11-12T11:00:00+05:30"),
        created_mono=0.0,
    )
    public = {
        "call_sid": call_sid or f"CA{campaign_id}_{lead_id}_{attempt_no}",
        "request_id": rid,
        "campaign_id": campaign_id,
        "lead_id": lead_id,
        "state": state,
    }
    if state == "ended":
        result = dict(result or {})
        result["call_sid"] = public["call_sid"]
        public["result"] = result
    store.record_accept(rid, public)
    return rid


def lead_row(store: Store, campaign_id: str = "c1", lead_id: str = "l1"):
    with store.db.tx() as conn:
        return store.db.q1(
            conn,
            "SELECT * FROM leads WHERE campaign_id=? AND lead_id=?",
            (campaign_id, lead_id),
        )


def call_row(store: Store, request_id: str):
    with store.db.tx() as conn:
        return store.db.q1(
            conn, "SELECT * FROM calls WHERE request_id=?", (request_id,)
        )
