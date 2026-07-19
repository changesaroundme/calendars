"""CAMPO adapter — ingest their published Tribe Events iCal feed.

CAMPO (Capital Area Metropolitan Planning Organization) already publishes a
subscribable calendar; this adapter re-ingests it so CAMPO events flow into
the combined calendar with everything else, normalized to our event model.

Source: https://www.campotexas.org/?post_type=tribe_events&ical=1&eventDisplay=list
"""
from __future__ import annotations

import re

from icalendar import Calendar

from caltools.model import Event

FEED_URL = "https://www.campotexas.org/?post_type=tribe_events&ical=1&eventDisplay=list"
SOURCE = "campo"

# CAMPO marks cancellations in the title ("... - Cancelled") rather than with
# STATUS; translate to a real STATUS while keeping their title text intact.
CANCEL_RE = re.compile(r"cancell?ed|postponed", re.IGNORECASE)


def parse_feed(ics_data: bytes | str) -> list[Event]:
    cal = Calendar.from_ical(ics_data)
    events: list[Event] = []
    for component in cal.walk("VEVENT"):
        summary = str(component.get("SUMMARY", "")).strip()
        dtstart = component.get("DTSTART").dt if component.get("DTSTART") else None
        dtend = component.get("DTEND").dt if component.get("DTEND") else None
        if dtstart is None or not summary:
            continue
        events.append(
            Event(
                source=SOURCE,
                # Org prefix is display-only; identity comes from CAMPO's UID.
                summary=f"CAMPO - {summary}",
                start=dtstart,
                end=dtend,
                location=str(component.get("LOCATION", "")).strip(),
                url=str(component.get("URL", "")).strip(),
                status="CANCELLED" if CANCEL_RE.search(summary) else "CONFIRMED",
                # Keep CAMPO's own UID: their feed is the system of record,
                # and reusing it means someone subscribed to both feeds sees
                # one event, not two.
                uid=str(component.get("UID", "")).strip(),
            )
        )
    return events


def fetch(session) -> list[Event]:
    resp = session.get(FEED_URL, timeout=30)
    resp.raise_for_status()
    # Bytes, not resp.text: requests guesses ISO-8859-1 when the server omits
    # a charset, which would mojibake UTF-8; icalendar handles bytes cleanly.
    return parse_feed(resp.content)
