"""The lead state machine: terminal states, retries, callbacks, DND, idempotency."""
from __future__ import annotations

import csv

from campaign_dispatcher.config import Campaign, Lead, Window
from conftest import (
    ALL_DAYS,
    call_result,
    dt,
    lead_row,
    make_config,
    make_store,
    place_and_accept,
)

PHONE = "+919000000001"
BASE = "2026-11-12T11:00:00+05:30"


def _ended(
    *,
    campaign="c1",
    lead="l1",
    phone=PHONE,
    status="no_answer",
    disposition=None,
    ended_at=BASE,
    callback_at=None,
):
    return call_result(
        campaign_id=campaign,
        lead_id=lead,
        phone=phone,
        status=status,
        disposition=disposition,
        ended_at=ended_at,
        callback_at=callback_at,
    )


def test_terminal_disposition_completes_and_duplicate_webhook_is_harmless(store):
    result = _ended(status="completed", disposition="interested")
    rid = place_and_accept(store, state="ended", result=result)

    row = lead_row(store)
    assert row["final_state"] == "completed"
    assert row["attempts"] == 1
    assert row["last_status"] == "completed"
    assert row["last_disposition"] == "interested"

    body = dict(result)
    body["request_id"] = rid
    assert store.record_webhook(body) is False  # duplicate
    assert lead_row(store)["attempts"] == 1
    assert lead_row(store)["final_state"] == "completed"


def test_no_answer_schedules_retry_sixty_minutes_later(store):
    place_and_accept(store, state="ended", result=_ended(status="no_answer"))
    row = lead_row(store)
    assert row["final_state"] is None
    assert row["kind"] == "retry"
    assert row["pending_at"] == "2026-11-12T12:00:00+05:30"


def test_failed_schedules_retry_fifteen_minutes_later(store):
    place_and_accept(store, state="ended", result=_ended(status="failed"))
    row = lead_row(store)
    assert row["kind"] == "retry"
    assert row["pending_at"] == "2026-11-12T11:15:00+05:30"


def test_busy_schedules_sixty_minutes(store):
    place_and_accept(store, state="ended", result=_ended(status="busy"))
    assert lead_row(store)["pending_at"] == "2026-11-12T12:00:00+05:30"


def test_callback_inside_window_is_due_at_callback_time(store):
    place_and_accept(
        store,
        state="ended",
        result=_ended(
            status="completed",
            disposition="callback_requested",
            ended_at="2026-11-12T11:10:00+05:30",
            callback_at="2026-11-12T14:00:00+05:30",
        ),
    )
    row = lead_row(store)
    assert row["kind"] == "callback"
    assert row["pending_at"] == "2026-11-12T14:00:00+05:30"


def test_callback_outside_window_moves_to_next_opening(store):
    place_and_accept(
        store,
        state="ended",
        result=_ended(
            status="completed",
            disposition="callback_requested",
            ended_at="2026-11-12T18:00:00+05:30",
            callback_at="2026-11-12T20:15:00+05:30",
        ),
    )
    row = lead_row(store)
    assert row["kind"] == "callback"
    assert row["pending_at"] == "2026-11-13T10:00:00+05:30"


def test_third_non_terminal_attempt_exhausts_the_lead(store):
    place_and_accept(store, attempt_no=1, state="ended",
                     result=_ended(status="no_answer", ended_at="2026-11-12T11:00:00+05:30"))
    place_and_accept(store, attempt_no=2, state="ended",
                     result=_ended(status="no_answer", ended_at="2026-11-12T12:00:00+05:30"))
    place_and_accept(store, attempt_no=3, state="ended",
                     result=_ended(status="no_answer", ended_at="2026-11-12T13:00:00+05:30"))
    row = lead_row(store)
    assert row["attempts"] == 3
    assert row["final_state"] == "exhausted"
    assert row["last_status"] == "no_answer"


def test_callback_on_last_attempt_is_exhausted_not_rescheduled(store):
    place_and_accept(store, attempt_no=1, state="ended",
                     result=_ended(status="no_answer"))
    place_and_accept(store, attempt_no=2, state="ended",
                     result=_ended(status="no_answer", ended_at="2026-11-12T12:00:00+05:30"))
    place_and_accept(
        store,
        attempt_no=3,
        state="ended",
        result=_ended(
            status="completed",
            disposition="callback_requested",
            ended_at="2026-11-12T13:00:00+05:30",
            callback_at="2026-11-12T15:00:00+05:30",
        ),
    )
    row = lead_row(store)
    assert row["attempts"] == 3
    assert row["final_state"] == "exhausted"


def test_lead_is_not_exhausted_while_its_last_call_is_active(store):
    place_and_accept(store, attempt_no=1, state="ended", result=_ended(status="no_answer"))
    place_and_accept(store, attempt_no=2, state="ended",
                     result=_ended(status="no_answer", ended_at="2026-11-12T12:00:00+05:30"))
    place_and_accept(store, attempt_no=3, state="active")
    assert lead_row(store)["final_state"] is None
    assert lead_row(store)["attempts"] == 3

    rid = "au:c1:l1:a3"
    body = _ended(status="no_answer", ended_at="2026-11-12T13:00:00+05:30")
    body["request_id"] = rid
    body["call_sid"] = "CAc1_l1_3"
    assert store.record_webhook(body) is True
    assert lead_row(store)["final_state"] == "exhausted"


def test_webhook_arriving_before_201_is_applied_once(store):
    rid = "au:c1:l1:a1"
    store.create_intent(
        campaign_id="c1", lead_id="l1", tenant_id="t1", phone=PHONE,
        attempt_no=1, request_id=rid, now=dt(BASE), created_mono=0.0,
    )
    body = _ended(status="completed", disposition="interested")
    body["request_id"] = rid
    assert store.record_webhook(body) is True

    # The 201 response now arrives and still says "active"; it must not reset
    # the ended state or double-count the attempt.
    store.record_accept(
        rid,
        {"call_sid": "CAc1_l1_1", "campaign_id": "c1", "lead_id": "l1", "state": "active"},
    )
    row = lead_row(store)
    assert row["attempts"] == 1
    assert row["final_state"] == "completed"
    with store.db.tx() as conn:
        call = store.db.q1(conn, "SELECT * FROM calls WHERE request_id=?", (rid,))
    assert call["state"] == "ended"
    assert call["accepted"] == 1


# ---------------------------------------------------------------- DND (R8/R9)
def _two_campaign_config():
    campaigns = [
        Campaign("c1", "t1", "a.csv", Window("10:00", "19:00", ALL_DAYS), 3),
        Campaign("c2", "t1", "b.csv", Window("10:00", "19:00", ALL_DAYS), 3),
    ]
    leads = [
        Lead("c1", "a", PHONE, "t1"),
        Lead("c2", "b", PHONE, "t1"),
    ]
    return make_config(campaigns=campaigns, leads=leads)


def test_dnd_blocks_other_campaigns_sharing_the_phone(tmp_path):
    store = make_store(_two_campaign_config(), tmp_path / "dnd.db")
    place_and_accept(
        store, campaign_id="c1", lead_id="a", state="ended",
        result=_ended(campaign="c1", lead="a", status="completed",
                      disposition="dnd", ended_at="2026-11-12T11:00:00+05:30"),
    )
    a = lead_row(store, "c1", "a")
    b = lead_row(store, "c2", "b")
    assert a["final_state"] == "completed"  # terminal disposition wins
    assert b["final_state"] == "dnd_blocked"
    assert b["attempts"] == 0
    assert store.is_dnd(PHONE)
    store.db.close()


def test_exhausted_takes_precedence_over_dnd_blocked(tmp_path):
    store = make_store(_two_campaign_config(), tmp_path / "precedence.db")
    # Campaign 2 lead burns all three attempts on no_answer -> exhausted.
    for i, hour in enumerate((10, 11, 12), start=1):
        place_and_accept(
            store, campaign_id="c2", lead_id="b", attempt_no=i, state="ended",
            result=_ended(campaign="c2", lead="b", status="no_answer",
                          ended_at=f"2026-11-12T{hour}:00:00+05:30"),
        )
    assert lead_row(store, "c2", "b")["final_state"] == "exhausted"

    # Now the same phone says DND in campaign 1: exhausted must not downgrade.
    place_and_accept(
        store, campaign_id="c1", lead_id="a", state="ended",
        result=_ended(campaign="c1", lead="a", status="completed",
                      disposition="dnd", ended_at="2026-11-12T13:00:00+05:30"),
    )
    assert lead_row(store, "c2", "b")["final_state"] == "exhausted"
    assert lead_row(store, "c1", "a")["final_state"] == "completed"
    store.db.close()


def test_completed_takes_precedence_over_dnd_blocked(tmp_path):
    store = make_store(_two_campaign_config(), tmp_path / "precedence2.db")
    place_and_accept(
        store, campaign_id="c2", lead_id="b", state="ended",
        result=_ended(campaign="c2", lead="b", status="completed",
                      disposition="not_interested", ended_at="2026-11-12T10:00:00+05:30"),
    )
    place_and_accept(
        store, campaign_id="c1", lead_id="a", state="ended",
        result=_ended(campaign="c1", lead="a", status="completed",
                      disposition="dnd", ended_at="2026-11-12T11:00:00+05:30"),
    )
    assert lead_row(store, "c2", "b")["final_state"] == "completed"
    store.db.close()


def test_releasing_an_intent_after_dnd_still_finalises_the_lead(tmp_path):
    """A DND that arrives while a dial intent is unresolved must not lose the lead."""
    store = make_store(_two_campaign_config(), tmp_path / "dnd_intent.db")
    # Campaign 2 lead has an ambiguous intent (reservation held, no 201 yet).
    store.create_intent(
        campaign_id="c2", lead_id="b", tenant_id="t1", phone=PHONE,
        attempt_no=1, request_id="au:c2:b:a1", now=dt(BASE), created_mono=0.0,
    )
    # Campaign 1 lead gets DND on the same phone; b cannot finalise yet (intent open).
    place_and_accept(
        store, campaign_id="c1", lead_id="a", state="ended",
        result=_ended(campaign="c1", lead="a", status="completed",
                      disposition="dnd", ended_at="2026-11-12T11:00:00+05:30"),
    )
    assert lead_row(store, "c2", "b")["final_state"] is None

    # Reconcile discovers the intent was never placed and releases it.
    store.cancel_intent("au:c2:b:a1", "not found at provider")
    assert lead_row(store, "c2", "b")["final_state"] == "dnd_blocked"
    store.db.close()


# ------------------------------------------------------------------ output CSV
def test_final_csv_is_sorted_and_has_the_contract_columns(tmp_path):
    cfg = make_config(
        campaigns=[
            Campaign("c2", "t1", "b.csv", Window("10:00", "19:00", ALL_DAYS), 3),
            Campaign("c1", "t1", "a.csv", Window("10:00", "19:00", ALL_DAYS), 3),
        ],
        leads=[Lead("c2", "z", "+912", "t1"), Lead("c1", "y", "+911", "t1")],
    )
    store = make_store(cfg, tmp_path / "csv.db")
    place_and_accept(store, campaign_id="c2", lead_id="z", phone="+912",
                     state="ended",
                     result=_ended(campaign="c2", lead="z", phone="+912",
                                   status="completed", disposition="interested"))
    place_and_accept(store, campaign_id="c1", lead_id="y", phone="+911",
                     attempt_no=1, state="ended",
                     result=_ended(campaign="c1", lead="y", phone="+911",
                                   status="no_answer"))
    place_and_accept(store, campaign_id="c1", lead_id="y", phone="+911",
                     attempt_no=2, state="ended",
                     result=_ended(campaign="c1", lead="y", phone="+911",
                                   status="no_answer",
                                   ended_at="2026-11-12T12:00:00+05:30"))
    place_and_accept(store, campaign_id="c1", lead_id="y", phone="+911",
                     attempt_no=3, state="ended",
                     result=_ended(campaign="c1", lead="y", phone="+911",
                                   status="busy",
                                   ended_at="2026-11-12T13:00:00+05:30"))
    out = store.write_final_csv(tmp_path / "final_state.csv")
    with out.open() as fh:
        rows = list(csv.DictReader(fh))
    assert [r["lead_id"] for r in rows] == ["y", "z"]
    assert rows[0] == {
        "campaign_id": "c1",
        "lead_id": "y",
        "final_state": "exhausted",
        "attempts": "3",
        "last_status": "busy",
        "last_disposition": "",
    }
    assert rows[1]["final_state"] == "completed"
    assert rows[1]["last_disposition"] == "interested"
    store.db.close()
