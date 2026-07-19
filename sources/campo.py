"""CAMPO adapter — ingest their published Tribe Events iCal feed.

CAMPO (Capital Area Metropolitan Planning Organization) already publishes a
subscribable calendar; this adapter re-ingests it so CAMPO events flow into
the combined calendar with everything else, normalized to our event model.

Source: https://www.campotexas.org/?post_type=tribe_events&ical=1&eventDisplay=list
"""
from __future__ import annotations

from datetime import date, datetime

from icalendar import Calendar

from caltools.model import Event

FEED_URL = "https://www.campotexas.org/?post_type=tribe_events&ical=1&eventDisplay=list"
SOURCE = "campo"


def classify(summary: str) -> tuple[str, str]:
    """Return (status, kind) from a Tribe Events summary line.

    CAMPO marks cancellations in the title ("... - Cancelled") rather than
    with STATUS, so we translate that into a real STATUS while keeping the
    title text (visible in every client) intact.
    """
    status = "CANCELLED" if "cancelled" in summary.lower() else "CONFIRMED"
    kind = "hearing" if "hearing" in summary.lower() else "meeting"
    return status, kind


def parse_feed(ics_text: str) -> list[Event]:
    cal = Calendar.from_ical(ics_text)
    events: list[Event] = []
    for component in cal.walk("VEVENT"):
        summary = str(component.get("SUMMARY", "")).strip()
        dtstart = component.get("DTSTART").dt if component.get("DTSTART") else None
        dtend = component.get("DTEND").dt if component.get("DTEND") else None
        if dtstart is None or not summary:
            continue
        status, kind = classify(summary)
        events.append(
            Event(
                source=SOURCE,
                summary=summary,
                start=dtstart,
                end=dtend,
                location=str(component.get("LOCATION", "")).strip(),
                url=str(component.get("URL", "")).strip(),
                kind=kind,
                status=status,
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
    return parse_feed(resp.text)
