"""Durable campaign dispatcher for Assignment B.

The package is intentionally split into small pieces:

* :mod:`campaign_dispatcher.windows`  - pure IST calling-window maths
* :mod:`campaign_dispatcher.scheduling` - pure priority / eligibility logic
* :mod:`campaign_dispatcher.db`      - SQLite durability primitives
* :mod:`campaign_dispatcher.store`   - the lead state machine (transactions)
* :mod:`campaign_dispatcher.engine`  - reconciliation + dialling loop
* :mod:`campaign_dispatcher.webhook` - signed, idempotent result receiver
"""

__version__ = "1.0.0"
