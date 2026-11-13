"""Signed, idempotent result-webhook receiver.

The provider may deliver the same webhook twice, deliver it before we have read
the 201 response, or never deliver it at all (the engine polls for that case).
Everything here is therefore safe to replay:

* the HMAC is verified against the exact raw bytes;
* ``Store.record_webhook`` transitions a call to ``ended`` at most once, in a
  single SQLite transaction, and duplicate deliveries are acknowledged with 2xx
  without re-applying the outcome.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .store import Store

log = logging.getLogger("dispatcher.webhook")


def create_app(store: Store, secret: str) -> FastAPI:
    app = FastAPI(title="AU campaign dispatcher", version="1.0")

    @app.post("/webhooks/telephony")
    async def telephony_webhook(request: Request):
        raw = await request.body()
        signature = request.headers.get("X-AU-Signature", "")
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            log.warning("rejected webhook with bad signature (%d bytes)", len(raw))
            return JSONResponse({"error": "invalid_signature"}, status_code=401)

        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "invalid_json"}, status_code=400)

        try:
            newly_applied = store.record_webhook(body)
        except Exception:
            log.exception("failed to process webhook for %s", body.get("request_id"))
            # 500 makes the provider redeliver (and the engine will poll anyway).
            return JSONResponse({"error": "processing_failed"}, status_code=500)

        if not newly_applied:
            log.info("duplicate webhook ignored for %s", body.get("call_sid"))
        else:
            log.info(
                "webhook %s status=%s disposition=%s",
                body.get("call_sid"),
                body.get("status"),
                body.get("disposition"),
            )
        return {"ok": True, "duplicate": not newly_applied}

    @app.get("/status")
    async def status():
        return store.progress()

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
