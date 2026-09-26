"""ERCOT adapter — added 2026-09-25.

ERCOT (Electric Reliability Council of Texas) publishes one iCal feed per
committee or working group: https://www.ercot.com/ical/meetings?host_id=<id>
(the "Subscribe to meeting updates" button on each committee page; the id is
the committee's short name — board, tac, prs, ros, wms, rms, llwg, …). A feed
lists that body's FUTURE meetings only; past ones are kept by the build's
append-only retention like every other source.

Which bodies we carry is BODIES below — one line each. The KB page
Organizations/ERCOT lists every active committee and working group with its
page; its feed id is in the page's subscribe link.

Feed quirks, normalised here:
  * DTSTART carries TZID=America/Chicago but DTEND is floating (no TZID) —
    both are read as Austin wall time.
  * LOCATION is "[ERCOT Austin\\,  Boardroom B and C]": brackets and double
    spaces stripped, and ERCOT's Austin campus address appended.
  * No URL property — the meeting page (agenda, materials, webcast) is in
    DESCRIPTION ("For more information: https://www.ercot.com/calendar/…").
  * SUMMARY uses the body's acronym ("LLWG Meeting"); the full name is used
    instead so the calendar reads without an ERCOT glossary.

Identity: ERCOT's own UID (a GUID per meeting), prefixed "ercot-" so the
embed's org filter can recognise it.
"""
from __future__ import annotations

import pathlib
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from icalendar import Calendar

from caltools.model import Event

SOURCE = "ercot"
FEED_URL = "https://www.ercot.com/ical/meetings?host_id={id}"
FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
CENTRAL = ZoneInfo("America/Chicago")
CAMPUS = "8000 Metropolis Dr (Building E), Suite 100, Austin, TX 78744"

# feed id -> (acronym as the feed writes it, full name). Order = display order.
BODIES = {
    "board": ("Board of Directors", "Board of Directors"),
    "llwg": ("LLWG", "Large Load Working Group"),
}

CANCEL_RE = re.compile(r"cancell?ed|postponed", re.IGNORECASE)
MORE_RE = re.compile(r"https://www\.ercot\.com/calendar/\S+")

_problems: list[str] = []


def health_problems() -> list[str]:
    return list(_problems)


def _wall(dt):
    """datetime -> naive Austin wall time (tz-aware ones converted first)."""
    if isinstance(dt, datetime):
        return dt.astimezone(CENTRAL).replace(tzinfo=None) if dt.tzinfo else dt
    return dt


def _location(raw: str) -> str:
    loc = re.sub(r"\s+", " ", raw.strip().strip("[]")).strip()
    if loc.lower().startswith("ercot austin"):
        room = loc[len("ercot austin"):].lstrip(" ,")
        return f"{room}, ERCOT, {CAMPUS}" if room else f"ERCOT, {CAMPUS}"
    return loc


def parse_feed(ics_data: bytes | str, host_id: str) -> list[Event]:
    acronym, full = BODIES.get(host_id, (host_id.upper(), host_id.upper()))
    cal = Calendar.from_ical(ics_data)
    events: list[Event] = []
    for c in cal.walk("VEVENT"):
        summary = str(c.get("SUMMARY", "")).strip()
        start = c.get("DTSTART").dt if c.get("DTSTART") else None
        if start is None or not summary:
            continue
        end = c.get("DTEND").dt if c.get("DTEND") else None
        if summary.startswith(acronym) and acronym != full:
            summary = full + summary[len(acronym):]
        desc = str(c.get("DESCRIPTION", ""))
        m = MORE_RE.search(desc)
        url = m.group(0) if m else f"https://www.ercot.com/committees/{host_id}"
        uid = str(c.get("UID", "")).strip()
        events.append(Event(
            source=SOURCE,
            summary=f"ERCOT: {summary}",
            start=_wall(start),
            end=_wall(end),
            location=_location(str(c.get("LOCATION", ""))),
            url=url,
            status="CANCELLED" if CANCEL_RE.search(summary) else "CONFIRMED",
            kind="regular",
            uid=f"ercot-{uid}" if uid else "",
            description=("Agenda, meeting materials and the webcast link: " + url) if m else "",
        ))
    return events


def fetch(session) -> list[Event]:
    _problems.clear()
    events: list[Event] = []
    for host_id in BODIES:
        try:
            resp = session.get(FEED_URL.format(id=host_id), timeout=30)
            resp.raise_for_status()
            events += parse_feed(resp.content, host_id)
        except Exception as exc:
            # One body's feed failing must not drop the others.
            _problems.append(f"ercot: {host_id} feed fetch failed ({exc})")
    if _problems and not events:
        raise RuntimeError("; ".join(_problems))
    return events


def fetch_offline() -> list[Event]:
    """Build from fixtures/ (no network) — the --offline contract."""
    events: list[Event] = []
    for host_id in BODIES:
        path = FIXTURES / f"ercot_{host_id}.ics"
        if path.exists():
            events += parse_feed(path.read_bytes(), host_id)
    return events
