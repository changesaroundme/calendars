"""Normalized event model shared by all source adapters."""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Optional


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass
class Event:
    """One calendar entry, normalized from any source.

    start/end are either both datetime (timed event, America/Chicago wall
    time unless tz-aware) or both date (all-day event).
    """

    source: str                     # adapter key, e.g. "capmetro"
    summary: str                    # human title, e.g. "Board of Directors"
    start: datetime | date
    end: Optional[datetime | date] = None
    location: str = ""
    url: str = ""
    status: str = "CONFIRMED"       # CONFIRMED | CANCELLED
    uid: str = ""                   # stable id; generated if empty
    description: str = ""

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    def stable_uid(self) -> str:
        """Deterministic UID so re-scrapes update rather than duplicate.

        Identity = source + body + date + start time (timed events only).
        Time is included so two meetings of the same body on the same day
        (e.g. a morning committee and an afternoon special session) remain
        distinct events instead of silently collapsing into one.
        """
        if self.uid:
            return self.uid
        if isinstance(self.start, datetime):
            stamp = self.start.strftime("%Y%m%dT%H%M")
        else:
            stamp = self.start.strftime("%Y%m%d")
        slug = slugify(self.summary)
        if not slug.startswith(f"{self.source}-"):
            slug = f"{self.source}-{slug}"
        return f"{slug}-{stamp}@calendars.changesaroundme.com"

    def to_json(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat() if self.end else None
        d["uid"] = self.stable_uid()
        d["all_day"] = self.all_day
        return d
