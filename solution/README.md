# Solution: Durable Campaign Dispatcher

A single-process, SQLite-backed dispatcher. It survives `kill -9` because every
state transition is a committed transaction, and every dial carries a
deterministic idempotency key that is persisted *before* the HTTP request.

```
campaign_dispatcher/
├── config.py      # fixtures -> immutable dataclasses
├── windows.py     # pure IST window maths (campaign ∩ regulatory)
├── scheduling.py  # pure eligibility + priority rules
├── db.py          # SQLite schema + serialised transactions
├── store.py       # lead/call state machine (R4-R9)
├── provider.py    # hostile-provider HTTP client (503/504 classification)
├── engine.py      # reconcile -> stop? -> dispatch -> sleep
├── webhook.py     # HMAC-verified, idempotent result receiver
└── main.py        # CLI / uvicorn thread / entrypoint
```

## Run

From the repository root (mock provider first, in another terminal):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cd assignment-b-workflow
python -m mock_telephony                 # http://127.0.0.1:8099

# terminal 2
cd solution
python -m campaign_dispatcher            # writes ./final_state.csv
```

Options:

```bash
python -m campaign_dispatcher --help
python -m campaign_dispatcher \
  --fixtures ../assignment-b-workflow/fixtures \
  --provider-url http://127.0.0.1:8099 \
  --port 8200 \
  --db .dispatcher_state.db \
  --output final_state.csv
```

The SQLite file is the durable state. Keep it between restarts. Delete it only
to start a brand-new run (with a fresh provider).

## Test

```bash
cd solution
pytest
```
