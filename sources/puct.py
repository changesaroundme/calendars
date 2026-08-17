"""PUCT adapter — curated-only substrate (no scraper yet).

The Public Utility Commission got its org feed on 2026-08-17 to carry
hand-curated entries (765-kV docket milestones, open meetings, rulemaking
comment windows) from events/curated.yaml. There is deliberately no
scraper: PUCT activity is docket-driven and needs the content filter the
Calendar Maintenance page describes (Ian's 765-kV interest, not every
docket) before automated ingestion makes sense. The SOS open-meetings
shadow already watches PUCT filings for future enrichment.

CURATED_ONLY tells build.py that zero scraped events is this source's
normal, healthy state.
"""
from __future__ import annotations

from caltools.model import Event

SOURCE = "puct"
CURATED_ONLY = True


def fetch(session) -> list[Event]:
    return []


def fetch_offline() -> list[Event]:
    return []


def health_problems() -> list[str]:
    return []
