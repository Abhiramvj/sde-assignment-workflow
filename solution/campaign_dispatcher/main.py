"""Entry point.

Run from a clean clone (after ``pip install -r requirements.txt``)::

    python -m campaign_dispatcher --help
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

import uvicorn

from .clock import ProviderClock
from .config import load_config
from .db import Database
from .engine import Engine
from .provider import ProviderClient
from .store import Store
from .webhook import create_app

log = logging.getLogger("dispatcher")


def _default_fixtures() -> Path:
    # solution/campaign_dispatcher/main.py -> repo/assignment-b-workflow/fixtures
    return Path(__file__).resolve().parents[2] / "assignment-b-workflow" / "fixtures"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Durable campaign dispatcher (Assignment B)")
    p.add_argument("--fixtures", default=str(_default_fixtures()),
                   help="directory containing campaigns.json and lead CSVs")
    p.add_argument("--provider-url", default="http://127.0.0.1:8099")
    p.add_argument("--host", default="127.0.0.1", help="webhook listener host")
    p.add_argument("--port", type=int, default=8200, help="webhook listener port")
    p.add_argument("--callback-url", default=None,
                   help="provider callback URL (default http://<host>:<port>/webhooks/telephony)")
    p.add_argument("--db", default=".dispatcher_state.db",
                   help="SQLite path (persists across restarts; do not delete mid-run)")
    p.add_argument("--output", default="final_state.csv")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(args.fixtures)
    db = Database(args.db)
    store = Store(db, cfg)
    client = ProviderClient(args.provider_url, args.callback_url or
                            f"http://{args.host}:{args.port}/webhooks/telephony")

    clock = ProviderClock(client)
    log.info(
        "provider clock: sim=%s speed=%.0fx",
        clock.now().isoformat(),
        clock.speed,
    )

    app = create_app(store, cfg.webhook_secret)
    uvicorn_config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)
    thread = threading.Thread(target=server.run, name="webhook-server", daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        if not thread.is_alive():
            log.error("webhook server failed to start on %s:%s", args.host, args.port)
            return 1
        time.sleep(0.02)
    if not server.started:
        log.error("timed out starting webhook server on %s:%s", args.host, args.port)
        return 1
    log.info("webhook listener on http://%s:%s/webhooks/telephony", args.host, args.port)

    engine = Engine(cfg, store, client, clock, output_path=args.output)
    try:
        path = engine.run()
    except KeyboardInterrupt:
        log.warning("interrupted; state is durable and will resume on restart")
        return 130
    finally:
        try:
            server.should_exit = True
            thread.join(timeout=3)
        finally:
            client.close()
            db.close()

    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
