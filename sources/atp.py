"""ATP adapter — ingest Austin Transit Partnership's Tribe Events iCal feed.

Same platform and pattern as CAMPO: ATP publishes a subscribable calendar
covering Board meetings and Community Advisory Committee meetings (plus any
other events they post). We re-ingest it, keep their UIDs (their feed is the
system of record), and prefix the display names.

Note (observed 2026-07-25): the feed listed only CAC meetings — the board
meets "third Wednesday, but not every month" per [[Organiations]], so board
entries appear whenever ATP schedules one; nothing extra to do here.

Source: https://www.atptx.org/?post_type=tribe_events&ical=1&eventDisplay=list
"""
from __future__ import annotations

from icalendar import Calendar

from caltools.model import Event
from sources.legistar import CANCEL_RE

FEED_URL = "https://www.atptx.org/?post_type=tribe_events&ical=1&eventDisplay=list"
SOURCE = "atp"
DEFAULT_LOCATION = "ATP Office, 203 Colorado St, Austin, TX 78701"


def parse_feed(ics_data: bytes | str) -> list[Event]:
    cal = Calendar.from_ical(ics_data)
    events: list[Event] = []
    for component in cal.walk("VEVENT"):
        summary = str(component.get("SUMMARY", "")).strip()
        dtstart = component.get("DTSTART").dt if component.get("DTSTART") else None
        if dtstart is None or not summary:
            continue
        events.append(
            Event(
                source=SOURCE,
                # Org prefix is display-only; identity comes from ATP's UID.
                summary=f"ATP - {summary}",
                start=dtstart,
                end=component.get("DTEND").dt if component.get("DTEND") else None,
                location=str(component.get("LOCATION", "")).strip() or DEFAULT_LOCATION,
                url=str(component.get("URL", "")).strip(),
                status="CANCELLED" if CANCEL_RE.search(summary) else "CONFIRMED",
                kind="hearing" if "hearing" in summary.lower() else "regular",
                uid=str(component.get("UID", "")).strip(),
            )
        )
    return events


def fetch(session) -> list[Event]:
    resp = session.get(FEED_URL, timeout=30)
    resp.raise_for_status()
    # Bytes, not resp.text: avoids charset-guess mojibake.
    return parse_feed(resp.content)
