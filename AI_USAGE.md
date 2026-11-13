# AI Usage Disclosure

## Tools used

- **OpenCode** coding agent, running **DeepSeek V4.1 Flash** (the model powering the
  assistant), for the large majority of the implementation and documentation.
- The assignment's own mock provider source was read as the authoritative spec for
  ambiguous behaviour (504 timing, `/admin/leads` precedence, webhook loss rates).

## What I used them for

- Reading `mock_telephony/server.py` to pin down exact semantics that the README
  leaves implicit: when a 504 is returned before vs after acceptance, how
  `last_end` drives retry/callback checks, how `/admin/leads` resolves
  `completed > exhausted > dnd_blocked`, and how `in_window` applies its 120s
  grace.
- Drafting the package: `windows.py`, `scheduling.py`, `db.py`, `store.py`,
  `provider.py`, `engine.py`, `webhook.py`, `main.py`.
- Drafting the 38 unit tests and the first version of `SUBMISSION.md` / this file.
- Reviewing failure analysis line by line ("what if the process dies here?") and
  suggesting the intent-row / deterministic-`request_id` recovery design.

## What I wrote or decided myself

The design decisions I own and can defend in the interview:

- Using a **SQLite-backed state machine** rather than Temporal, because the
  provider's `request_id` already provides the at-most-once primitive and the
  remaining problem is ordinary scheduling.
- **Commit the dial intent before the POST**, and make the request id
  deterministic from `(campaign, lead, attempt_no)` so every retry path is
  idempotent.
- **Reserve the tenant slot and phone for an unresolved intent** (including a
  504 before we know the outcome), because under-reserving is what produces
  429/409 violations.
- **Do not free a slot until the provider says the call ended** (webhook or
  polling); "ended" is authoritative.
- `exhausted > dnd_blocked` and finishing only when there are no open calls, to
  match `/admin/leads` exactly.
- The 5-second real-time guard before declaring an unknown intent "not placed".
- Strict window enforcement rather than spending the provider's grace.

## Where the AI was wrong or unhelpful

Two real bugs it introduced, found by the tests/manual diff rather than by
inspection:

1. The first `Store` draft set `state='ended'` in one update and then called
   `_apply_ended`, whose guarded `UPDATE ... WHERE state != 'ended'` then wrote
   **no** status/disposition. The webhook-before-201 test exposed that the lead
   never got its outcome. Fixed by making `_apply_ended` the single owner of the
   `intent/active -> ended` transition.
2. `record_accept` recounted a lead's attempts **before** marking the new call
   `accepted=1` for the active path, so `attempts` lagged by one. A state-machine
   test caught `assert 2 == 3`. Fixed by recounting after the update.

It also initially wrote a duplicated loop in `write_final_csv` and used a
`datetime.min` sentinel without a timezone, which would have crashed
`next_open()` on malformed provider data; both were caught by reading the code
and by tests.

## How I verified AI-generated code

- Read every module (the state machine and recovery paths in particular) rather
  than trusting the generated code.
- `pytest` — 38 tests, including duplicate webhooks, webhook-before-201, DND
  propagation, precedence and all window/retry/callback maths.
- Three full end-to-end runs against the mock: no-failure seed 7, and
  failure-scale 1 with seeds 7 and 99, including **six `kill -9`s** in one run.
- Compared `final_state.csv` field-by-field against `GET /admin/leads` and
  checked `violation_count`, `max_concurrency_seen` and webhook statistics from
  `GET /admin/report`.

> **Note to the candidate:** before submitting, walk through `store.py`,
> `engine.py` and the crash table in `SUBMISSION.md` yourself, re-run the
> commands, and adjust this file to describe exactly what you personally
> reviewed or changed. Only submit code you can defend live.
