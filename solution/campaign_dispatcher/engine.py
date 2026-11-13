"""The dispatch loop: reconcile, then decide who to call next.

Every iteration has the same shape:

1. **Reconcile** active calls and unresolved dial intents against the provider.
   This is how lost webhooks, ambiguous 504s and crashes are healed.
2. **Stop** if every lead is final.
3. **Dispatch** eligible leads in priority order, respecting windows,
   tenant concurrency, one-call-per-phone and attempt caps.
4. Sleep briefly (real time), then repeat.

The provider's simulated clock is the only clock used for business decisions.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from .clock import ProviderClock
from .config import Config
from .provider import PlacementResult, ProviderClient
from .scheduling import Candidate, Constraints, pick_next
from .store import Store
from .windows import campaign_next_open as win_campaign_next_open

log = logging.getLogger("dispatcher.engine")

# A dial intent whose POST produced no response (process died / socket reset)
# is only declared "not placed" after this much *real* time, so a request that
# is still in flight at the provider can never be double-dialled.
UNCERTAIN_INTENT_GRACE_SECONDS = 5.0

ACTIVE_POLL_SECONDS = 0.08
IDLE_POLL_SECONDS = 0.20


class Engine:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        client: ProviderClient,
        clock: ProviderClock,
        output_path: str | Path = "final_state.csv",
    ):
        self.cfg = cfg
        self.store = store
        self.client = client
        self.clock = clock
        self.output_path = Path(output_path)
        self._next_progress_log = 0.0
        self.calls_placed = 0

    # ------------------------------------------------------------------ loop
    def run(self) -> Path:
        self.store.seed(self.cfg.leads)
        log.info(
            "seeded %d leads across %d campaigns",
            len(self.cfg.leads),
            len(self.cfg.campaigns),
        )
        while True:
            now = self.clock.now()
            try:
                self.reconcile(now)
            except Exception:  # defensive: never let a transient bug lose leads
                log.exception("reconcile failed; retrying")

            if self.store.all_final():
                path = self.store.write_final_csv(self.output_path)
                log.info("all leads final; wrote %s", path)
                return path

            try:
                self.dispatch()
            except Exception:
                log.exception("dispatch failed; retrying")

            self._log_progress(now)
            time.sleep(self._sleep_seconds())

    def _sleep_seconds(self) -> float:
        """Wake up just before the next business event, with a small floor/cap."""
        if self.store.has_open_calls():
            return ACTIVE_POLL_SECONDS

        now = self.clock.now()
        deltas: list[float] = []
        pending = self.store.next_pending_at()
        if pending is not None:
            deltas.append((pending - now).total_seconds())
        for cid, camp in self.cfg.campaigns.items():
            if not self.store.campaign_open(now, cid):
                opening = win_campaign_next_open(now, camp, self._regulatory)
                deltas.append((opening - now).total_seconds())

        if not deltas:
            return IDLE_POLL_SECONDS
        delta = max(0.0, min(deltas))
        speed = max(1.0, self.clock.speed)
        return max(0.02, min(IDLE_POLL_SECONDS, delta / speed))

    @property
    def _regulatory(self) -> tuple[str, str]:
        return (self.cfg.regulatory_start, self.cfg.regulatory_end)

    # ------------------------------------------------------------ reconcile
    def reconcile(self, now: datetime) -> None:
        # Calls we believe are running: poll for an end that no webhook told us
        # about.  "ended" is authoritative; we only free resources from it.
        for row in self.store.active_calls():
            status, public = self.client.get_call(row["call_sid"])
            if status == "found" and public and public.get("state") == "ended":
                log.info(
                    "recovered ended call %s (%s/%s) by polling",
                    row["call_sid"],
                    row["campaign_id"],
                    row["lead_id"],
                )
                self.store.record_accept(row["request_id"], public)

        # Dial intents whose outcome we never learned (crash / 504 / timeout).
        for row in self.store.unresolved_intents():
            age = time.monotonic() - (row.get("created_mono") or 0.0)
            if not row.get("response_seen") and age < UNCERTAIN_INTENT_GRACE_SECONDS:
                continue  # POST may still be in flight
            status, public = self.client.find_call(row["request_id"])
            if status == "found" and public:
                log.info(
                    "recovered intent %s as %s", row["request_id"], public.get("state")
                )
                self.store.record_accept(row["request_id"], public)
            elif status == "not_found":
                # The provider never placed this request: safe to release the
                # reservation.  The deterministic request_id means a later retry
                # of the same attempt is still idempotent.
                log.info("intent %s was never placed; releasing", row["request_id"])
                self.store.cancel_intent(row["request_id"], "not found at provider")
            # status == "error": provider unreachable, keep the reservation.

    # -------------------------------------------------------------- dispatch
    def dispatch(self) -> None:
        while True:
            now = self.clock.now()
            open_campaigns = {
                cid
                for cid in self.cfg.campaigns
                if self.store.campaign_open(now, cid)
            }
            if not open_campaigns:
                return

            snapshot = self.store.constraint_snapshot()
            constraints = Constraints(
                tenant_active=snapshot["tenant_active"],
                tenant_max=self._tenant_max(),
                locked_phones=frozenset(snapshot["locked_phones"]),
                open_leads=frozenset(snapshot["open_leads"]),
                dnd_phones=frozenset(snapshot["dnd_phones"]),
                max_attempts=self._max_attempts(),
            )
            candidates = self.store.candidate_rows(open_campaigns)
            choice = pick_next(candidates, now, constraints)
            if choice is None:
                return
            self._dial(choice, now)

    def _dial(self, choice: Candidate, now: datetime) -> None:
        attempt_no = self.store.attempt_number(choice.campaign_id, choice.lead_id)
        request_id = (
            f"au:{choice.campaign_id}:{choice.lead_id}:a{attempt_no}"
        )
        # Last-moment DND check (a webhook may have landed since the snapshot).
        if self.store.is_dnd(choice.phone):
            return
        self.store.create_intent(
            campaign_id=choice.campaign_id,
            lead_id=choice.lead_id,
            tenant_id=choice.tenant_id,
            phone=choice.phone,
            attempt_no=attempt_no,
            request_id=request_id,
            now=now,
            created_mono=time.monotonic(),
        )
        result: PlacementResult = self.client.place_call(
            request_id, choice.campaign_id, choice.lead_id, choice.phone
        )
        self._handle_placement(choice, request_id, result, now)

    def _handle_placement(
        self,
        choice: Candidate,
        request_id: str,
        result: PlacementResult,
        now: datetime,
    ) -> None:
        if result.outcome == "accepted":
            self.calls_placed += 1
            body = result.body or {}
            log.info(
                "dial %s attempt=%s request=%s -> %s",
                choice.lead_id,
                request_id.rsplit(":a", 1)[-1],
                request_id,
                body.get("state", "?"),
            )
            self.store.record_accept(request_id, body)
        elif result.outcome == "not_placed":
            log.warning(
                "dial %s request=%s 503 (not placed); will retry",
                choice.lead_id,
                request_id,
            )
            self.store.cancel_intent(request_id)
        elif result.outcome == "rejected":
            log.error(
                "dial %s request=%s rejected %s: %s",
                choice.lead_id,
                request_id,
                result.status_code,
                result.error,
            )
            self.store.mark_rejected(request_id, now, result.error or "")
        else:
            log.warning(
                "dial %s request=%s ambiguous (%s); reconciling",
                choice.lead_id,
                request_id,
                result.error,
            )
            self.store.note_ambiguous(request_id, result.error)

    # ---------------------------------------------------------------- utils
    def _tenant_max(self) -> dict[str, int]:
        return {tid: t.max_concurrent_calls for tid, t in self.cfg.tenants.items()}

    def _max_attempts(self) -> dict[str, int]:
        return {cid: c.max_attempts for cid, c in self.cfg.campaigns.items()}

    def _log_progress(self, now: datetime) -> None:
        mono = time.monotonic()
        if mono < self._next_progress_log:
            return
        self._next_progress_log = mono + 15.0
        try:
            progress = self.store.progress()
        except Exception:
            return
        log.info("progress sim=%s calls_placed=%d %s", now.isoformat(),
                 self.calls_placed, progress)
