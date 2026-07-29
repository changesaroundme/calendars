"""Texas Legislature adapter — curated-only for now.

There is no scraper here yet. The module exists so hand-entered committee
hearings in events/curated.yaml have somewhere to land: build.py merges
curated entries into the feed named by their `org:` key, and that key has
to correspond to a module in CALENDARS.

When this does get a scraper, the two pages worth reading are:

  * Senate committee hearings and events —
    https://senate.texas.gov/events.php
    (the per-committee pages, e.g. /cmte.php?c=640 for Transportation, do
    NOT list upcoming meetings; this one does)
  * House committees — https://house.texas.gov/committees/committee/470

Both are ephemeral: notices come down at end of day, so a once-daily build
will miss same-day postings. That's the open problem, and it's why this is
still curated — see the Calendar Maintenance page backlog.

Individual notices live at stable URLs of the shape
capitol.texas.gov/tlodocs/<session>/schedules/html/C<committee><date><time><n>.HTM
which is a usable identity if a scraper is ever built.
"""
from __future__ import annotations

from caltools.model import Event

# Tells build.py not to raise "0 events parsed": an empty result is the
# normal, healthy state for a feed that only ever carries curated entries.
CURATED_ONLY = True


def fetch(session) -> list[Event]:
    return []


def fetch_offline() -> list[Event]:
    return []
