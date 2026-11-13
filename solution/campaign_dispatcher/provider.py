"""Thin HTTP client for the mock telephony provider.

The provider is deliberately hostile: 503s, ambiguous 504s, duplicated/lost
webhooks.  This client classifies each response into something the state machine
can reason about:

* ``accepted``    - the provider has (or had) a call for this request_id
* ``not_placed``  - definitively not placed; safe to forget the intent
* ``ambiguous``   - outcome unknown; keep the reservation and reconcile
* ``rejected``    - a rule was broken (provider recorded a violation)

We never raise on a ``504`` or a transport error: those are the whole point.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class PlacementResult:
    outcome: str
    status_code: int | None = None
    body: dict | None = None
    error: str | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome == "accepted"

    @property
    def call_sid(self) -> str | None:
        if self.body:
            return self.body.get("call_sid")
        return None


class ProviderClient:
    def __init__(self, base_url: str, callback_url: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.callback_url = callback_url
        # The mock sleeps up to 2s before returning a 504.  A client timeout
        # must be comfortably longer, otherwise we manufacture ambiguity.
        self._client = httpx.Client(timeout=httpx.Timeout(timeout))

    def close(self) -> None:
        self._client.close()

    def get_clock(self) -> dict:
        r = self._client.get(self.base_url + "/v1/clock")
        r.raise_for_status()
        return r.json()

    def place_call(self, request_id: str, campaign_id: str, lead_id: str, phone: str) -> PlacementResult:
        payload = {
            "request_id": request_id,
            "campaign_id": campaign_id,
            "lead_id": lead_id,
            "phone": phone,
            "callback_url": self.callback_url,
        }
        try:
            r = self._client.post(self.base_url + "/v1/calls", json=payload)
        except httpx.HTTPError as exc:
            # Could have reached the provider or not.  Never assume "not placed".
            return PlacementResult("ambiguous", error=f"{type(exc).__name__}: {exc}")

        if r.status_code in (200, 201):
            try:
                body = r.json()
            except ValueError:
                return PlacementResult("ambiguous", r.status_code, error="invalid json")
            return PlacementResult("accepted", r.status_code, body)

        if r.status_code == 503:
            return PlacementResult("not_placed", r.status_code, _safe_json(r))

        if r.status_code == 504:
            return PlacementResult("ambiguous", r.status_code, _safe_json(r),
                                   error="provider timeout")

        if r.status_code in (409, 422, 429):
            return PlacementResult("rejected", r.status_code, _safe_json(r),
                                   error=_error_text(r))

        if r.status_code >= 500:
            return PlacementResult("ambiguous", r.status_code, error=f"http {r.status_code}")

        return PlacementResult("rejected", r.status_code, _safe_json(r),
                               error=f"http {r.status_code}")

    def get_call(self, call_sid: str) -> tuple[str, dict | None]:
        """Return ("found", payload) | ("not_found", None) | ("error", None)."""
        try:
            r = self._client.get(f"{self.base_url}/v1/calls/{call_sid}")
        except httpx.HTTPError:
            return "error", None
        if r.status_code == 200:
            try:
                return "found", r.json()
            except ValueError:
                return "error", None
        if r.status_code == 404:
            return "not_found", None
        return "error", None

    def find_call(self, request_id: str) -> tuple[str, dict | None]:
        try:
            r = self._client.get(
                self.base_url + "/v1/calls", params={"request_id": request_id}
            )
        except httpx.HTTPError:
            return "error", None
        if r.status_code == 200:
            try:
                return "found", r.json()
            except ValueError:
                return "error", None
        if r.status_code == 404:
            return "not_found", None
        return "error", None


def _safe_json(r: httpx.Response) -> dict | None:
    try:
        value = r.json()
        return value if isinstance(value, dict) else None
    except ValueError:
        return None


def _error_text(r: httpx.Response) -> str:
    data = _safe_json(r)
    if data:
        return f"{data.get('error', '')}: {data.get('detail', '')}".strip(": ")
    return r.text[:200]
