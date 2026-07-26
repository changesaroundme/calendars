"""TxDOT engagement events adapter — non-recurring public involvement.

Watches TxDOT public-involvement pages (currently the UTP page; add more
URLs to PAGES as they matter) for two caption-labeled table shapes their
CMS uses:

  1. "... public involvement events" — rows of dated, timed events
     (virtual public meetings, hearings) with recording/presentation links.
  2. "... comment period" — a start date and an end date+time. Emitted as a
     single all-day event SPANNING the window, titled with the closing
     deadline (the part people actually miss), kind=comment-window.

The tables are server-rendered; the comment-period one just sits deep
enough in the page that summarizing fetch tools truncate it — raw HTML has
it (hard-won lesson, see Calendar Maintenance page).

Also watches TxDOT advisory-committee pages (COMMITTEES below), which use a
third caption shape: "<year> meeting agendas and materials" with
Date | Time | Location | Agenda | Handout columns. Only current-and-later
year tables are parsed (past years keep the same caption on some pages).
Committee date cells sometimes omit the year ("Jan. 22") — the caption's
year fills it in.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from caltools.model import Event, slugify

SOURCE = "txdotev"
PAGES = [
    ("UTP", "https://www.txdot.gov/projects/planning/utp/utp-public-involvement.html"),
]
# (display abbrev, raw body name [frozen into UIDs], page url)
COMMITTEES = [
    ("BPAC", "Bicycle and Pedestrian Advisory Committee",
     "https://www.txdot.gov/about/advisory-committees/"
     "bicycle-pedestrian-advisory-committee.html"),
    ("PTAC", "Public Transportation Advisory Committee",
     "https://www.txdot.gov/about/advisory-committees/"
     "public-transportation-advisory-committee.html"),
]
EVENT_LENGTH = timedelta(hours=2)
STASSNEY = "TxDOT Stassney Campus Auditorium, 6230 E. Stassney Ln., Austin, TX 78744"

NUM_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
LONG_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+(\d{1,2}),?\s+(20\d{2})"
)
TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b", re.IGNORECASE)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"])}


def _time_of(text: str):
    m = TIME_RE.search(text)
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    return h, int(m.group(2) or 0)


def _long_date(text: str) -> date | None:
    m = LONG_DATE_RE.search(text)
    if not m:
        return None
    return date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))


def _kind_of(topic: str) -> str:
    low = topic.lower()
    if "hearing" in low:
        return "hearing"
    return "engagement"


def parse_page(html: str, context: str, page_url: str) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    for table in soup.find_all("table"):
        caption = table.find("caption")
        cap = caption.get_text(" ", strip=True) if caption else ""

        if re.search(r"comment period", cap, re.IGNORECASE):
            # One row: start date | end date + time.
            for row in table.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
                if len(cells) < 2:
                    continue
                start = _long_date(cells[0])
                end = _long_date(cells[1])
                if not (start and end):
                    continue
                t = _time_of(cells[1])
                deadline = (f"{end.strftime('%b %-d')}, "
                            f"{t[0] % 12 or 12}:{t[1]:02d} "
                            f"{'p.m.' if t[0] >= 12 else 'a.m.'}") if t else end.strftime("%b %-d")
                events.append(
                    Event(
                        source=SOURCE,
                        summary=f"TxDOT - {cap} (closes {deadline})",
                        start=start,
                        end=end + timedelta(days=1),  # DTEND exclusive
                        url=page_url,
                        kind="comment-window",
                        description=(
                            f"Public comment window: {cells[0]} through {cells[1]}. "
                            f"Details and how to comment: {page_url}"
                        ),
                        uid=(f"{SOURCE}-{slugify(cap)}-{start.strftime('%Y%m%d')}"
                             "@calendars.changesaroundme.com"),
                    )
                )
            continue

        if re.search(r"involvement events", cap, re.IGNORECASE):
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                when = cells[0].get_text(" ", strip=True)
                topic = cells[1].get_text(" ", strip=True)
                dm = NUM_DATE_RE.search(when)
                if not dm or not topic:
                    continue
                d = date(int(dm.group(3)), int(dm.group(1)), int(dm.group(2)))
                t = _time_of(when)
                start = datetime(d.year, d.month, d.day, t[0], t[1]) if t else d
                links = "\n".join(
                    f"{a.get_text(' ', strip=True)}: {a['href']}"
                    for a in (cells[2].find_all("a", href=True) if len(cells) > 2 else [])
                    if a.get_text(strip=True)
                )
                events.append(
                    Event(
                        source=SOURCE,
                        summary=f"TxDOT - {context}: {topic}",
                        start=start,
                        end=(start + EVENT_LENGTH) if isinstance(start, datetime) else None,
                        url=page_url,
                        kind=_kind_of(topic),
                        description=links,
                        uid=(f"{SOURCE}-{slugify(context)}-{slugify(topic)}-"
                             f"{d.strftime('%Y%m%d')}@calendars.changesaroundme.com"),
                    )
                )
    return events


CAPTION_YEAR_RE = re.compile(r"(20\d{2})\s+meeting agendas", re.IGNORECASE)
# "Jan. 30, 2026" / "April 24, 2026" / "Jan. 22" / "August 13"
COMMITTEE_DATE_RE = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:,?\s+(20\d{2}))?")
MONTHS3 = {name[:3].lower(): num for name, num in MONTHS.items()}

_problems: list[str] = []


def health_problems() -> list[str]:
    return list(_problems)


def _committee_location(raw: str) -> str:
    """Normalize the messy Location cell to something map-appable.

    Observed cell texts: "Hybrid 6230 E Stassney Lane, Austin, TX 78744",
    "6230 E. Stassney Lane, Austin, Tx Auditorium, Teams virtual meeting",
    "Teams virtual meeting has surpassed", "Virtual",
    "Teams virtual link will be provided".
    """
    low = raw.lower()
    if "stassney" in low:
        virtual_too = "teams" in low or "virtual" in low or "hybrid" in low
        return STASSNEY + (" (Teams option available)" if virtual_too else "")
    if "teams" in low or "virtual" in low:
        return "Virtual (Teams)"
    return re.sub(r"\s+", " ", raw).strip()


def parse_committee_page(
    html: str, abbrev: str, name: str, page_url: str, min_year: int | None = None
) -> list[Event]:
    if min_year is None:
        min_year = date.today().year
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    saw_table = False
    for table in soup.find_all("table"):
        caption = table.find("caption")
        cap = caption.get_text(" ", strip=True) if caption else ""
        ym = CAPTION_YEAR_RE.search(cap)
        if not ym:
            continue
        saw_table = True
        year = int(ym.group(1))
        if year < min_year:
            continue  # archive table for a past year
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            texts = [c.get_text(" ", strip=True).replace("\xa0", " ") for c in cells]
            dm = COMMITTEE_DATE_RE.search(texts[0])
            month = MONTHS3.get(dm.group(1)[:3].lower()) if dm else None
            if not month:
                continue
            d = date(int(dm.group(3) or year), month, int(dm.group(2)))
            t = _time_of(texts[1])
            start = datetime(d.year, d.month, d.day, t[0], t[1]) if t else d
            links = []
            for a in row.find_all("a", href=True):
                label = (a.get("title") or a.get_text(" ", strip=True)).strip()
                href = a["href"]
                if href.startswith("/"):
                    href = "https://www.txdot.gov" + href
                if label and href.startswith("http"):
                    links.append(f"{label}: {href}")
            desc = [f"Posted location: {re.sub(r'  +', ' ', texts[2])}"] if texts[2] else []
            events.append(
                Event(
                    source=SOURCE,
                    summary=f"TxDOT {abbrev} - Meeting",
                    start=start,
                    end=(start + EVENT_LENGTH) if isinstance(start, datetime) else None,
                    location=_committee_location(texts[2]),
                    url=page_url,
                    status=("CANCELLED" if "cancel" in " ".join(texts).lower()
                            else "CONFIRMED"),
                    kind="regular",
                    description="\n".join(desc + links),
                    uid=(f"{SOURCE}-{slugify(name)}-"
                         f"{start.strftime('%Y%m%dT%H%M') if isinstance(start, datetime) else start.strftime('%Y%m%d')}"
                         "@calendars.changesaroundme.com"),
                )
            )
    if not saw_table:
        _problems.append(
            f"txdotev: no '<year> meeting agendas' table on {abbrev} page "
            "(page redesign?)"
        )
    return events


def fetch(session) -> list[Event]:
    _problems.clear()
    events: list[Event] = []
    for context, url in PAGES:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        events.extend(parse_page(resp.text, context, url))
    for abbrev, name, url in COMMITTEES:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        events.extend(parse_committee_page(resp.text, abbrev, name, url))
    return events
