"""CapMetro adapter — thin wrapper over the shared Legistar machinery.

See sources/legistar.py for the two-layer design (InSite calendar page for
early dates + Web API for agenda enrichment) and the observed API lag that
motivates it (first seen here, 2026-07-18: the 7/27 Board meeting was on
Calendar.aspx while the API still returned nothing after 6/22).
"""
from __future__ import annotations

import pathlib
from sources.legistar import Legistar
FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"

ADAPTER = Legistar(
    source="capmetro",
    client="capmetrotx",
    host="capmetrotx.legistar.com",
    prefix="CapMetro",
    # Their Legistar publishes only the room name; give subscribers a real
    # address (CapMetro HQ boardroom).
    location_fixes={
        "Rosa Parks Boardroom":
            "Rosa Parks Boardroom, CapMetro HQ, 2910 E. 5th St., Austin, TX 78702",
    },
)

fetch = ADAPTER.fetch  # build.py calls module.fetch; everything else via ADAPTER


def fetch_offline() -> list[Event]:
    """Build from fixtures/ (no network) — the --offline contract."""
    import json

    rows = json.loads((FIXTURES / "capmetro.json").read_text())
    events = ADAPTER.parse_calendar_html(
        (FIXTURES / "capmetro_calendar.html").read_text()
    )
    return ADAPTER.finalize(ADAPTER.merge_api(events, rows))
