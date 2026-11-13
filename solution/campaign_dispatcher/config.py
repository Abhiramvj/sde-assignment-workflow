"""Load and validate the assignment fixtures.

The fixture files are *input*, never modified.  Everything the dispatcher needs
(windows, retry policy, concurrency limits, webhook secret, leads) is parsed once
at start-up into immutable dataclasses.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Window:
    start: str  # "HH:MM" IST
    end: str    # "HH:MM" IST, exclusive
    days: tuple[str, ...]

    def contains_day(self, weekday: int) -> bool:
        return DAY_KEYS[weekday] in self.days


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    max_concurrent_calls: int


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    tenant_id: str
    leads_file: str
    window: Window
    max_attempts: int


@dataclass(frozen=True)
class Lead:
    campaign_id: str
    lead_id: str
    phone: str
    tenant_id: str


@dataclass(frozen=True)
class RetryPolicy:
    no_answer_minutes: int = 60
    busy_minutes: int = 60
    failed_minutes: int = 15

    def minutes_for(self, status: str | None) -> int:
        if status == "failed":
            return self.failed_minutes
        if status == "busy":
            return self.busy_minutes
        # no_answer and any unexpected non-terminal status fall back to the
        # conservative 60 minute wait.
        return self.no_answer_minutes


@dataclass(frozen=True)
class Config:
    sim_start: str
    timezone: str
    regulatory_start: str
    regulatory_end: str
    window_grace_seconds: int
    retry_policy: RetryPolicy
    webhook_secret: str
    tenants: dict[str, Tenant]
    campaigns: dict[str, Campaign]
    leads: tuple[Lead, ...]

    def tenant_for(self, campaign_id: str) -> Tenant:
        return self.tenants[self.campaigns[campaign_id].tenant_id]


def load_config(fixtures_dir: str | Path) -> Config:
    fixtures_dir = Path(fixtures_dir)
    raw = json.loads((fixtures_dir / "campaigns.json").read_text())

    tenants: dict[str, Tenant] = {}
    for t in raw["tenants"]:
        tenants[t["tenant_id"]] = Tenant(
            tenant_id=t["tenant_id"],
            max_concurrent_calls=int(t["max_concurrent_calls"]),
        )

    campaigns: dict[str, Campaign] = {}
    leads: list[Lead] = []
    for c in raw["campaigns"]:
        w = c["window"]
        campaign = Campaign(
            campaign_id=c["campaign_id"],
            tenant_id=c["tenant_id"],
            leads_file=c["leads_file"],
            window=Window(
                start=w["start"],
                end=w["end"],
                days=tuple(d.lower() for d in w["days"]),
            ),
            max_attempts=int(c["max_attempts"]),
        )
        campaigns[campaign.campaign_id] = campaign
        if campaign.tenant_id not in tenants:
            raise ValueError(f"campaign {campaign.campaign_id} references unknown tenant")
        path = fixtures_dir / campaign.leads_file
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                phone = (row.get("phone") or "").strip()
                lead_id = (row.get("lead_id") or "").strip()
                if not lead_id or not phone:
                    continue
                leads.append(
                    Lead(
                        campaign_id=campaign.campaign_id,
                        lead_id=lead_id,
                        phone=phone,
                        tenant_id=campaign.tenant_id,
                    )
                )

    rp = raw["retry_policy"]
    reg = raw["regulatory_window"]
    return Config(
        sim_start=raw["sim_start"],
        timezone=raw.get("timezone", "Asia/Kolkata"),
        regulatory_start=reg["start"],
        regulatory_end=reg["end"],
        window_grace_seconds=int(raw.get("window_grace_seconds", 120)),
        retry_policy=RetryPolicy(
            no_answer_minutes=int(rp["no_answer_minutes"]),
            busy_minutes=int(rp["busy_minutes"]),
            failed_minutes=int(rp["failed_minutes"]),
        ),
        webhook_secret=raw["webhook_secret"],
        tenants=tenants,
        campaigns=campaigns,
        leads=tuple(leads),
    )


def group_leads_by_campaign(leads: Iterable[Lead]) -> dict[str, list[Lead]]:
    out: dict[str, list[Lead]] = {}
    for lead in leads:
        out.setdefault(lead.campaign_id, []).append(lead)
    return out
