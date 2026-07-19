"""TxDOT adapter — Texas Transportation Commission meeting schedule.

Source: the commission's meeting-dates page, a plain server-rendered HTML
table (Date / Agenda / Video / Minutes) that lists the full year ahead —
dates and times publish a year out, agendas appear ~8 days before each
meeting, video/minutes links after it.

Notices also get filed with the Texas SOS ~a week ahead, but this page has
far longer lead time, which is the point of the calendar.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from caltools.model import Event

SOURCE = "txdot"
PAGE_URL = (
    "https://www.txdot.gov/about/leadership/"
    "texas-transportation-commission/meeting-dates-agendas.html"
)
BODY_NAME = "Texas Transportation Commission"
# Stated on the page ("meetings are held at..."); exceptions are called out
# per-meeting on the page, which would surface via the agenda link.
LOCATION = (
    "Ric Williamson Hearing Room, Dewitt C. Greer Building, "
    "125 E. 11th St., Austin, TX"
)
MEETING_LENGTH = timedelta(hours=4)  # commission meetings often run half a day

# Date cell looks like "01/29/26 (10:00 a.m.)"; time is optional.
DATE_CELL_RE = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4})\s*(?:\(\s*([\d: ]+[ap])\.?m\.?\s*\))?$",
    re.IGNORECASE,
)


def _parse_date_cell(text: str) -> datetime | None:
    m = DATE_CELL_RE.match(text.strip())
    if not m:
        return None
    d, t = m.group(1), m.group(2)
    fmt = "%m/%d/%y" if len(d.split("/")[-1]) == 2 else "%m/%d/%Y"
    day = datetime.strptime(d, fmt)
    if t:
        try:
            tv = datetime.strptime(t.replace(" ", "").upper() + "M", "%I:%M%p")
            return day.replace(hour=tv.hour, minute=tv.minute)
        except ValueError:
            pass
    return day


def parse_page(html: str) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        start = _parse_date_cell(cells[0].get_text(" ", strip=True))
        if start is None:
            continue
        links = []
        for a in row.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                href = "https://www.txdot.gov" + href
            label = a.get_text(" ", strip=True)
            for word in ("agenda", "video", "minutes"):
                if word in label.lower():
                    links.append(f"{word.capitalize()}: {href}")
                    break
        timed = start.hour != 0 or start.minute != 0
        events.append(
            Event(
                source=SOURCE,
                summary=BODY_NAME,
                start=start if timed else start.date(),
                end=(start + MEETING_LENGTH) if timed else None,
                location=LOCATION,
                url=PAGE_URL,
                description="\n".join(links),
            )
        )
    return events


def finalize(events: list[Event]) -> list[Event]:
    """Freeze UIDs from the raw body name, then apply the display prefix."""
    for ev in events:
        ev.uid = ev.stable_uid()
        if not ev.summary.startswith("TxDOT"):
            ev.summary = f"TxDOT - {ev.summary}"
    return events


def fetch(session) -> list[Event]:
    resp = session.get(PAGE_URL, timeout=30)
    resp.raise_for_status()
    return finalize(parse_page(resp.text))
