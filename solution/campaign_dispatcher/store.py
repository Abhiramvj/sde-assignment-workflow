"""The lead / call state machine, expressed as short SQLite transactions.

Why a database state machine instead of Temporal
------------------------------------------------
The whole workflow needs exactly three durable facts: the *intent* to dial, the
*outcome* of a dial, and the *next action time*.  Those are naturally rows.  A
SQLite transaction gives us at-most-once dialling for free:

  1. commit the intent row (with a deterministic ``request_id``),
  2. POST it to the provider,
  3. record the response.

A crash between any two steps is recoverable by looking the ``request_id`` up at
the provider.  No extra infrastructure, and the safety argument fits on a page.

State precedence (R9): completed > exhausted > dnd_blocked.
"""
from __future__ import annotations

import csv
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from . import windows as win
from .config import Config, Lead
from .db import Database
from .scheduling import (
    Candidate,
    KIND_CALLBACK,
    KIND_FRESH,
    KIND_RETRY,
)

TERMINAL_DISPOSITIONS = {"interested", "not_interested", "wrong_number", "dnd"}

CSV_HEADER = [
    "campaign_id",
    "lead_id",
    "final_state",
    "attempts",
    "last_status",
    "last_disposition",
]


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, db: Database, cfg: Config):
        self.db = db
        self.cfg = cfg
        self._reg = (cfg.regulatory_start, cfg.regulatory_end)

    # ------------------------------------------------------------------ seed
    def seed(self, leads: Iterable[Lead]) -> None:
        leads = list(leads)
        with self.db.tx() as conn:
            for lead in leads:
                self.db.x(
                    conn,
                    """
                    INSERT OR IGNORE INTO leads
                        (campaign_id, lead_id, tenant_id, phone, attempts, kind,
                         pending_at, final_state)
                    VALUES (?, ?, ?, ?, 0, ?, NULL, NULL)
                    """,
                    (lead.campaign_id, lead.lead_id, lead.tenant_id, lead.phone, KIND_FRESH),
                )
            dnd = {
                r["phone"] for r in self.db.q(conn, "SELECT phone FROM dnd_phones")
            }
            if dnd:
                for lead in leads:
                    if lead.phone not in dnd:
                        continue
                    row = self.db.q1(
                        conn,
                        "SELECT * FROM leads WHERE campaign_id=? AND lead_id=?",
                        (lead.campaign_id, lead.lead_id),
                    )
                    if row is not None:
                        self._finalize(conn, row)

    # --------------------------------------------------------- window helpers
    def _next_open(self, when: datetime, campaign_id: str) -> datetime:
        camp = self.cfg.campaigns[campaign_id]
        return win.campaign_next_open(when, camp, self._reg)

    def campaign_open(self, now: datetime, campaign_id: str) -> bool:
        camp = self.cfg.campaigns[campaign_id]
        return win.campaign_is_open(now, camp, self._reg)

    # ------------------------------------------------------------- dial intent
    def create_intent(
        self,
        *,
        campaign_id: str,
        lead_id: str,
        tenant_id: str,
        phone: str,
        attempt_no: int,
        request_id: str,
        now: datetime,
        created_mono: float,
    ) -> None:
        with self.db.tx() as conn:
            self.db.x(
                conn,
                """
                INSERT INTO calls
                    (request_id, call_sid, campaign_id, lead_id, tenant_id, phone,
                     attempt_no, accepted, state, created_sim, created_mono,
                     response_seen)
                VALUES (?, NULL, ?, ?, ?, ?, ?, 0, 'intent', ?, ?, 0)
                """,
                (
                    request_id,
                    campaign_id,
                    lead_id,
                    tenant_id,
                    phone,
                    attempt_no,
                    _iso(now),
                    created_mono,
                ),
            )

    def cancel_intent(self, request_id: str, reason: str = "") -> None:
        """The provider definitively did *not* accept this request (503 / 4xx)."""
        with self.db.tx() as conn:
            row = self.db.q1(
                conn, "SELECT * FROM calls WHERE request_id=?", (request_id,)
            )
            self.db.x(
                conn,
                "DELETE FROM calls WHERE request_id=? AND state='intent'",
                (request_id,),
            )
            # Releasing a reservation can unblock finalisation (for example the
            # phone was DND'd by another campaign while this intent was pending).
            if row is not None:
                lead = self.db.q1(
                    conn,
                    "SELECT * FROM leads WHERE campaign_id=? AND lead_id=?",
                    (row["campaign_id"], row["lead_id"]),
                )
                if lead is not None:
                    self._finalize(conn, lead)

    def note_ambiguous(self, request_id: str, error: str | None = None) -> None:
        """Outcome unknown (504 / timeout / connection reset): keep the reserve."""
        with self.db.tx() as conn:
            self.db.x(
                conn,
                "UPDATE calls SET response_seen=1, place_error=? WHERE request_id=?",
                (error, request_id),
            )

    def mark_rejected(self, request_id: str, now: datetime, detail: str) -> None:
        """A rule was broken.  The provider did not place the call.

        The violation is already recorded provider-side; we drop the intent and
        push the lead back a little so we do not hammer the same rule again.
        """
        with self.db.tx() as conn:
            row = self.db.q1(
                conn, "SELECT * FROM calls WHERE request_id=?", (request_id,)
            )
            self.db.x(conn, "DELETE FROM calls WHERE request_id=?", (request_id,))
            if row is None:
                return
            lead = self.db.q1(
                conn,
                "SELECT * FROM leads WHERE campaign_id=? AND lead_id=?",
                (row["campaign_id"], row["lead_id"]),
            )
            if lead is None or lead["final_state"] is not None:
                return
            backoff = now + timedelta(minutes=5)
            self.db.x(
                conn,
                "UPDATE leads SET pending_at=? WHERE campaign_id=? AND lead_id=?",
                (
                    _iso(self._next_open(backoff, row["campaign_id"])),
                    row["campaign_id"],
                    row["lead_id"],
                ),
            )
            fresh = self.db.q1(
                conn,
                "SELECT * FROM leads WHERE campaign_id=? AND lead_id=?",
                (row["campaign_id"], row["lead_id"]),
            )
            if fresh is not None:
                self._finalize(conn, fresh)

    # ---------------------------------------------------------- accept / end
    def record_accept(self, request_id: str, public: dict) -> None:
        """Persist a 201/200 response (or a replayed existing call)."""
        with self.db.tx() as conn:
            row = self.db.q1(
                conn, "SELECT * FROM calls WHERE request_id=?", (request_id,)
            )
            if row is None:
                # Defensive: derive everything we can from the payload / lead row.
                cid = public.get("campaign_id", "")
                lid = public.get("lead_id", "")
                lead = self.db.q1(
                    conn,
                    "SELECT * FROM leads WHERE campaign_id=? AND lead_id=?",
                    (cid, lid),
                )
                if lead is None:
                    return
                self.db.x(
                    conn,
                    """
                    INSERT INTO calls
                        (request_id, call_sid, campaign_id, lead_id, tenant_id,
                         phone, attempt_no, accepted, state, response_seen)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 1, 'intent', 1)
                    """,
                    (
                        request_id,
                        public.get("call_sid"),
                        cid,
                        lid,
                        lead["tenant_id"],
                        lead["phone"],
                        self._recount_attempts(conn, cid, lid),
                    ),
                )
                row = self.db.q1(
                    conn, "SELECT * FROM calls WHERE request_id=?", (request_id,)
                )

            call_sid = public.get("call_sid") or row["call_sid"]
            state = public.get("state")

            if row["state"] == "ended":
                # The webhook beat the 201 response.  Nothing to do but keep the
                # sid; the outcome has already been applied exactly once.
                self.db.x(
                    conn,
                    "UPDATE calls SET accepted=1, call_sid=COALESCE(call_sid, ?), "
                    "response_seen=1 WHERE request_id=?",
                    (call_sid, request_id),
                )
                return

            if state == "ended":
                # Do not flip state here: _apply_ended owns that transition and
                # also writes status / disposition / retry schedule exactly once.
                self.db.x(
                    conn,
                    "UPDATE calls SET accepted=1, call_sid=COALESCE(?, call_sid), "
                    "response_seen=1 WHERE request_id=?",
                    (call_sid, request_id),
                )
                result = public.get("result") or {}
                fresh = self.db.q1(
                    conn, "SELECT * FROM calls WHERE request_id=?", (request_id,)
                )
                self._apply_ended(conn, fresh, result)
            else:
                self.db.x(
                    conn,
                    """
                    UPDATE calls
                    SET accepted=1, call_sid=?, response_seen=1, state='active'
                    WHERE request_id=? AND state != 'ended'
                    """,
                    (call_sid, request_id),
                )
                self._recount_lead(conn, row["campaign_id"], row["lead_id"])
                # attempts can reach max while the last call is still running;
                # finalisation is deliberately deferred until it ends.
                lead = self.db.q1(
                    conn,
                    "SELECT * FROM leads WHERE campaign_id=? AND lead_id=?",
                    (row["campaign_id"], row["lead_id"]),
                )
                if lead is not None:
                    self._finalize(conn, lead)

    def record_webhook(self, body: dict) -> bool:
        """Idempotently apply a result webhook.  Returns True if newly applied."""
        request_id = body.get("request_id")
        if not request_id:
            raise ValueError("webhook has no request_id")
        with self.db.tx() as conn:
            row = self.db.q1(
                conn, "SELECT * FROM calls WHERE request_id=?", (request_id,)
            )
            if row is None:
                cid = body.get("campaign_id", "")
                lid = body.get("lead_id", "")
                lead = self.db.q1(
                    conn,
                    "SELECT * FROM leads WHERE campaign_id=? AND lead_id=?",
                    (cid, lid),
                )
                if lead is None:
                    return False
                self.db.x(
                    conn,
                    """
                    INSERT INTO calls
                        (request_id, call_sid, campaign_id, lead_id, tenant_id,
                         phone, attempt_no, accepted, state, response_seen)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 1, 'intent', 1)
                    """,
                    (
                        request_id,
                        body.get("call_sid"),
                        cid,
                        lid,
                        body.get("tenant_id") or lead["tenant_id"],
                        body.get("phone") or lead["phone"],
                        self._recount_attempts(conn, cid, lid) + 1,
                    ),
                )
                row = self.db.q1(
                    conn, "SELECT * FROM calls WHERE request_id=?", (request_id,)
                )
            if row["state"] == "ended":
                return False  # duplicate delivery - harmless
            self._apply_ended(conn, row, body)
            return True

    def _apply_ended(self, conn: sqlite3.Connection, row: sqlite3.Row, result: dict) -> None:
        """Record an ended call and advance the lead exactly once."""
        if row is None:
            return
        cid, lid = row["campaign_id"], row["lead_id"]
        status = result.get("status")
        disposition = result.get("disposition")
        callback_at = result.get("callback_at")
        ended_at = result.get("ended_at") or row["ended_at"]
        self.db.x(
            conn,
            """
            UPDATE calls SET
                state='ended', accepted=1,
                call_sid=COALESCE(?, call_sid),
                status=?, disposition=?, callback_at=?,
                started_at=?, ended_at=?,
                response_seen=1
            WHERE request_id=?
            """,
            (
                result.get("call_sid"),
                status,
                disposition,
                callback_at,
                result.get("started_at"),
                ended_at,
                row["request_id"],
            ),
        )

        lead = self.db.q1(
            conn,
            "SELECT * FROM leads WHERE campaign_id=? AND lead_id=?",
            (cid, lid),
        )
        if lead is None:
            return
        attempts = self._recount_lead(conn, cid, lid)

        latest = self._latest_ended(conn, cid, lid)
        if latest is None:
            return
        last_status = latest["status"]
        last_disp = latest["disposition"]
        last_ended = latest["ended_at"]

        updates: dict[str, object] = {
            "attempts": attempts,
            "last_status": last_status,
            "last_disposition": last_disp,
            "last_ended_at": last_ended,
        }

        if last_disp in TERMINAL_DISPOSITIONS:
            updates["terminal_disposition"] = last_disp
        if last_disp == "dnd":
            self.db.x(
                conn,
                "INSERT OR IGNORE INTO dnd_phones(phone, sim_at) VALUES (?, ?)",
                (lead["phone"], last_ended),
            )

        # Next action, only if the lead is not finished.
        camp = self.cfg.campaigns[cid]
        if last_disp not in TERMINAL_DISPOSITIONS and attempts < camp.max_attempts:
            ended_dt = _parse(last_ended)
            if last_disp == "callback_requested":
                cb = _parse(latest["callback_at"])
                if cb is not None:
                    updates["kind"] = KIND_CALLBACK
                    updates["pending_at"] = _iso(self._next_open(cb, cid))
                else:
                    updates["kind"] = KIND_RETRY
                    updates["pending_at"] = _iso(
                        self._next_open(
                            ended_dt or self._now_fallback(), cid
                        )
                    )
            else:
                minutes = self.cfg.retry_policy.minutes_for(last_status)
                base = ended_dt or self._now_fallback()
                earliest = base + timedelta(minutes=minutes)
                updates["kind"] = KIND_RETRY
                updates["pending_at"] = _iso(self._next_open(earliest, cid))

        self._update_lead(conn, cid, lid, updates)

        # DND propagates to every other lead sharing the phone (R8).
        if last_disp == "dnd":
            self._propagate_dnd(conn, lead["phone"])

        lead = self.db.q1(
            conn,
            "SELECT * FROM leads WHERE campaign_id=? AND lead_id=?",
            (cid, lid),
        )
        if lead is not None:
            self._finalize(conn, lead)

    # ------------------------------------------------------------- finalisation
    def _finalize(self, conn: sqlite3.Connection, lead: sqlite3.Row) -> None:
        """Apply R9 precedence: completed > exhausted > dnd_blocked."""
        cid, lid = lead["campaign_id"], lead["lead_id"]
        current = lead["final_state"]

        if lead["terminal_disposition"]:
            if current != "completed":
                self.db.x(
                    conn,
                    "UPDATE leads SET final_state='completed' WHERE campaign_id=? AND lead_id=?",
                    (cid, lid),
                )
            return

        if current is not None:
            return

        # Never declare a lead final while one of its own calls is in flight.
        busy = self.db.q1(
            conn,
            "SELECT 1 FROM calls WHERE campaign_id=? AND lead_id=? "
            "AND state IN ('intent','active') LIMIT 1",
            (cid, lid),
        )
        if busy is not None:
            return

        camp = self.cfg.campaigns[cid]
        if lead["attempts"] >= camp.max_attempts:
            state = "exhausted"
        else:
            dnd = self.db.q1(
                conn, "SELECT 1 FROM dnd_phones WHERE phone=?", (lead["phone"],)
            )
            state = "dnd_blocked" if dnd is not None else None

        if state is not None:
            self.db.x(
                conn,
                "UPDATE leads SET final_state=? WHERE campaign_id=? AND lead_id=?",
                (state, cid, lid),
            )

    def _propagate_dnd(self, conn: sqlite3.Connection, phone: str) -> None:
        rows = self.db.q(
            conn, "SELECT * FROM leads WHERE phone=? AND final_state IS NULL", (phone,)
        )
        for lead in rows:
            self._finalize(conn, lead)

    # --------------------------------------------------------------- helpers
    def _recount_attempts(self, conn: sqlite3.Connection, cid: str, lid: str) -> int:
        row = self.db.q1(
            conn,
            "SELECT COUNT(*) AS n FROM calls WHERE campaign_id=? AND lead_id=? "
            "AND accepted=1",
            (cid, lid),
        )
        return int(row["n"]) if row else 0

    def _recount_lead(self, conn: sqlite3.Connection, cid: str, lid: str) -> int:
        attempts = self._recount_attempts(conn, cid, lid)
        self.db.x(
            conn,
            "UPDATE leads SET attempts=? WHERE campaign_id=? AND lead_id=?",
            (attempts, cid, lid),
        )
        return attempts

    def _latest_ended(
        self, conn: sqlite3.Connection, cid: str, lid: str
    ) -> sqlite3.Row | None:
        rows = self.db.q(
            conn,
            "SELECT * FROM calls WHERE campaign_id=? AND lead_id=? AND state='ended'",
            (cid, lid),
        )
        if not rows:
            return None
        return max(
            rows,
            key=lambda r: _parse(r["ended_at"]) or datetime.min.replace(tzinfo=timezone.utc),
        )

    @staticmethod
    def _now_fallback() -> datetime:
        # Only reached for malformed provider data (no ended_at).  Use a far
        # past time so the lead becomes immediately eligible rather than lost.
        return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _update_lead(
        conn: sqlite3.Connection, cid: str, lid: str, updates: dict[str, object]
    ) -> None:
        if not updates:
            return
        cols = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE leads SET {cols} WHERE campaign_id=? AND lead_id=?",
            (*updates.values(), cid, lid),
        )

    # ----------------------------------------------------- dispatch snapshots
    def constraint_snapshot(self) -> dict:
        with self.db.tx() as conn:
            calls = self.db.q(
                conn,
                "SELECT campaign_id, lead_id, phone, tenant_id, state FROM calls "
                "WHERE state IN ('intent','active')",
            )
            dnd = {r["phone"] for r in self.db.q(conn, "SELECT phone FROM dnd_phones")}
            open_leads = {(r["campaign_id"], r["lead_id"]) for r in calls}
            locked = {r["phone"] for r in calls}
            tenant_active: dict[str, int] = {}
            for r in calls:
                tenant_active[r["tenant_id"]] = tenant_active.get(r["tenant_id"], 0) + 1
            return {
                "tenant_active": tenant_active,
                "locked_phones": locked,
                "open_leads": open_leads,
                "dnd_phones": dnd,
            }

    def candidate_rows(self, open_campaign_ids: set[str]) -> list[Candidate]:
        with self.db.tx() as conn:
            rows = self.db.q(
                conn,
                "SELECT campaign_id, lead_id, phone, tenant_id, attempts, kind, "
                "pending_at FROM leads WHERE final_state IS NULL",
            )
        out: list[Candidate] = []
        for r in rows:
            if r["campaign_id"] not in open_campaign_ids:
                continue
            out.append(
                Candidate(
                    campaign_id=r["campaign_id"],
                    lead_id=r["lead_id"],
                    phone=r["phone"],
                    tenant_id=r["tenant_id"],
                    attempts=int(r["attempts"]),
                    kind=r["kind"] or KIND_FRESH,
                    pending_at=_parse(r["pending_at"]),
                )
            )
        return out

    def attempt_number(self, campaign_id: str, lead_id: str) -> int:
        with self.db.tx() as conn:
            return self._recount_attempts(conn, campaign_id, lead_id) + 1

    def lead_phone(self, campaign_id: str, lead_id: str) -> str | None:
        with self.db.tx() as conn:
            row = self.db.q1(
                conn,
                "SELECT phone FROM leads WHERE campaign_id=? AND lead_id=?",
                (campaign_id, lead_id),
            )
            return row["phone"] if row else None

    def is_dnd(self, phone: str) -> bool:
        with self.db.tx() as conn:
            return (
                self.db.q1(conn, "SELECT 1 FROM dnd_phones WHERE phone=?", (phone,))
                is not None
            )

    def has_open_calls(self) -> bool:
        with self.db.tx() as conn:
            return (
                self.db.q1(
                    conn,
                    "SELECT 1 FROM calls WHERE state IN ('intent','active') LIMIT 1",
                )
                is not None
            )

    def next_pending_at(self) -> datetime | None:
        """Earliest future action among incomplete leads (for adaptive sleeps)."""
        with self.db.tx() as conn:
            rows = self.db.q(
                conn,
                "SELECT pending_at FROM leads WHERE final_state IS NULL "
                "AND pending_at IS NOT NULL",
            )
        parsed = [_parse(r["pending_at"]) for r in rows]
        parsed = [p for p in parsed if p is not None]
        return min(parsed) if parsed else None

    # --------------------------------------------------------- reconciliation
    def active_calls(self) -> list[dict]:
        with self.db.tx() as conn:
            rows = self.db.q(
                conn, "SELECT * FROM calls WHERE state='active' AND call_sid IS NOT NULL"
            )
            return [dict(r) for r in rows]

    def unresolved_intents(self) -> list[dict]:
        with self.db.tx() as conn:
            rows = self.db.q(conn, "SELECT * FROM calls WHERE state='intent'")
            return [dict(r) for r in rows]

    # -------------------------------------------------------------- reporting
    def all_final(self) -> bool:
        with self.db.tx() as conn:
            remaining = self.db.q1(
                conn, "SELECT COUNT(*) AS n FROM leads WHERE final_state IS NULL"
            )["n"]
            open_calls = self.db.q1(
                conn,
                "SELECT COUNT(*) AS n FROM calls WHERE state IN ('intent','active')",
            )["n"]
            return int(remaining) == 0 and int(open_calls) == 0

    def progress(self) -> dict:
        with self.db.tx() as conn:
            rows = self.db.q(
                conn,
                "SELECT campaign_id, final_state, COUNT(*) AS n FROM leads "
                "GROUP BY campaign_id, final_state",
            )
            calls = self.db.q1(
                conn,
                "SELECT COUNT(*) AS n FROM calls WHERE state IN ('intent','active')",
            )["n"]
            total = self.db.q1(
                conn, "SELECT COUNT(*) AS n FROM leads WHERE final_state IS NULL"
            )["n"]
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["campaign_id"], {})[r["final_state"] or "incomplete"] = int(r["n"])
        return {"by_campaign": out, "open_calls": int(calls), "incomplete_leads": int(total)}

    def write_final_csv(self, path: str | Path) -> Path:
        path = Path(path)
        with self.db.tx() as conn:
            rows = self.db.q(
                conn,
                "SELECT campaign_id, lead_id, final_state, attempts, last_status, "
                "last_disposition FROM leads ORDER BY campaign_id, lead_id",
            )
        missing = [dict(r) for r in rows if not r["final_state"]]
        if missing:
            raise RuntimeError(f"refusing to write incomplete final_state.csv: {missing[:3]}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_HEADER)
            for r in rows:
                writer.writerow(
                    [
                        r["campaign_id"],
                        r["lead_id"],
                        r["final_state"],
                        int(r["attempts"]),
                        r["last_status"] or "",
                        r["last_disposition"] or "",
                    ]
                )
        os.replace(tmp, path)
        return path
