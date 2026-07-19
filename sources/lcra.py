"""LCRA adapter — board & committee meeting schedule page.

LCRA publishes a year of meeting *dates* (no times) as two HTML tables on
one WordPress page: one for LCRA proper (committee day + board day per
month) and one for LCRA Transmission Services Corporation. Times and
detailed locations only appear later in agenda PDFs and the SOS open-
meetings filing, so v1 emits all-day events; a future SOS adapter can
upgrade them to timed events.

Table semantics (verified against live markup 2026-07-19):
- 4-cell row:  Month | committee day | board day | city
- 3-cell row with colspan=2 on the middle cell: one combined day for board
  and committees (e.g. "September | 23 | Austin")
- "No meeting" rows and single-cell year rows ("2026") in between.

Source: https://www.lcra.org/about/leadership/board-meeting-schedule/
"""
from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from caltools.model import Event

SOURCE = "lcra"
SCHEDULE_URL = "https://www.lcra.org/about/leadership/board-meeting-schedule/"
AGENDAS_URL = "https://www.lcra.org/about/leadership/board-agendas/"
NOTE = (
    "All-day placeholder: LCRA publishes dates a year out; the exact time "
    f"and room post with the agenda (~1 week ahead): {AGENDAS_URL}"
)

MONTHS = {
    m.lower(): i + 1
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"]
    )
}
YEAR_RE = re.compile(r"^(20\d{2})$")
DAY_RE = re.compile(r"\d{1,2}")


def _days(cell_text: str) -> list[int]:
    """All day-numbers in a cell ('18', '17-18', '18, 19' all work)."""
    return [int(d) for d in DAY_RE.findall(cell_text) if 1 <= int(d) <= 31]


def _mk(summary: str, year: int, month: int, day: int, city: str) -> Event | None:
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    return Event(
        source=SOURCE,
        summary=summary,
        start=d,
        location=f"{city}, TX" if city and city != "–" else "",
        url=AGENDAS_URL,
        description=NOTE,
    )


def _parse_table(table, is_tsc: bool, default_year: int) -> list[Event]:
    events: list[Event] = []
    year = default_year
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        texts = [c.get_text(" ", strip=True) for c in cells]
        if len(texts) == 1:
            m = YEAR_RE.match(texts[0])
            if m:
                year = int(m.group(1))
            continue
        if not texts or texts[0].lower() not in MONTHS:
            continue  # header rows, titles
        month = MONTHS[texts[0].lower()]
        body_cells = cells[1:]
        city = texts[-1] if len(texts) > 1 else ""
        if any("no meeting" in t.lower() for t in texts):
            continue
        if is_tsc:
            for day in _days(texts[1] if len(texts) > 1 else ""):
                ev = _mk("Transmission Services Corp. Board", year, month, day, city)
                if ev:
                    events.append(ev)
            continue
        # LCRA table: combined day if the day cell spans both columns
        if len(body_cells) >= 1 and body_cells[0].get("colspan"):
            for day in _days(texts[1]):
                ev = _mk("Board & Committee Meetings", year, month, day, city)
                if ev:
                    events.append(ev)
        elif len(texts) >= 4:
            for day in _days(texts[1]):
                ev = _mk("Board Committees", year, month, day, city)
                if ev:
                    events.append(ev)
            for day in _days(texts[2]):
                ev = _mk("Board of Directors", year, month, day, city)
                if ev:
                    events.append(ev)
    return events


def parse_page(html: str, default_year: int | None = None) -> list[Event]:
    if default_year is None:
        default_year = datetime.now().year
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    for table in soup.find_all("table"):
        title = table.find("td")
        title_text = title.get_text(" ", strip=True).lower() if title else ""
        is_tsc = "transmission" in title_text
        events.extend(_parse_table(table, is_tsc, default_year))
    return events


def finalize(events: list[Event]) -> list[Event]:
    """Freeze UIDs from the raw body name, then apply the display prefix."""
    for ev in events:
        ev.uid = ev.stable_uid()
        if not ev.summary.startswith("LCRA"):
            ev.summary = f"LCRA - {ev.summary}"
    return events


def fetch(session) -> list[Event]:
    resp = session.get(SCHEDULE_URL, timeout=30)
    resp.raise_for_status()
    return finalize(parse_page(resp.text))
