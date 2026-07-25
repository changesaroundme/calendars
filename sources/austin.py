"""City of Austin adapter — Council (Legistar) + Boards & Commissions pages.

One combined "Austin" calendar from two very different sources:

1. **Council + committees** via Legistar (client "austintexas") — the shared
   two-layer machinery in sources/legistar.py. Timed events with agenda
   links, appearing ~1-4 weeks ahead. (The city also publishes a full-year
   council calendar as a PDF in EDIMS — parsing it is on the backlog; the
   document id changes yearly.)

2. **Boards & commissions** from austintexas.gov board pages, which list the
   full year of meeting dates as a plain <li> list (sometimes annotated,
   e.g. "March 31, 2026 - Special Called"). No times on the page → all-day
   events; agendas/times post to the board's meeting-documents page ~a week
   out. City bodies do NOT file with the Texas SOS (state/regional only),
   so these pages are the authoritative early source.

The tracked board list is deliberately explicit — add a tuple to BOARDS to
follow another one (find its numeric meetings-page id on the city clerk
site).
"""
from __future__ import annotations

import io
import pathlib
import re
from datetime import date, datetime

import pdfplumber
from bs4 import BeautifulSoup

from caltools.model import Event, slugify
from sources.legistar import CANCEL_RE, Legistar

SOURCE = "austin"
PREFIX = "Austin"

COUNCIL = Legistar(
    source=SOURCE,
    client="austintexas",
    host="austintexas.legistar.com",
    prefix=PREFIX,
)

# (Board name, schedule page, meeting-documents page)
BOARDS = [
    ("Planning Commission",
     "https://www.austintexas.gov/boards-commissions/board/planning-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/40_1"),
    ("Urban Transportation Commission",
     "https://www.austintexas.gov/boards-commissions/board/urban-transportation-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/50_1"),
    ("Design Commission",
     "https://www.austintexas.gov/boards-commissions/board/design-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/22_1"),
    ("Airport Advisory Commission",
     "https://www.austintexas.gov/boards-commissions/board/airport-advisory-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/7_1"),
    ("Austin Integrated Water Resource Planning Community Task Force",
     "https://www.austintexas.gov/boards-commissions/board/austin-integrated-water-resource-planning-community-task-force",
     "https://www.austintexas.gov/boards-commissions/meetings/132_1"),
]

# "January 13, 2026" with an optional annotation ("- Special Called",
# "(Cancelled)", or bare trailing text).
DATE_LI_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+(\d{1,2}),?\s+(20\d{2})"
    r"(?:(?:\s*[-–—(]\s*|\s+)(.*?))?[)\s]*$"
)


def _schedule_lists(soup: BeautifulSoup):
    """Yield only <ul>/<ol> elements that ARE meeting-date lists.

    A qualifying list has >=3 items and a majority of them matching the
    date pattern — this keeps dated news items or archive links elsewhere
    on the page from being mistaken for meetings (and from hijacking a
    real meeting's entry via the per-date dedup).
    """
    for lst in soup.find_all(["ul", "ol"]):
        items = lst.find_all("li", recursive=False)
        if len(items) < 3:
            continue
        hits = sum(bool(DATE_LI_RE.match(li.get_text(" ", strip=True))) for li in items)
        if hits >= 3 and hits / len(items) >= 0.6:
            yield lst


def parse_board_page(html: str, board: str, docs_url: str) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    seen: set[str] = set()
    for li in (li for lst in _schedule_lists(soup)
               for li in lst.find_all("li", recursive=False)):
        m = DATE_LI_RE.match(li.get_text(" ", strip=True))
        if not m:
            continue
        month, day, year, note = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        try:
            d = datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date()
        except ValueError:
            continue
        # Identity from board + date only; annotations must not change UID.
        uid = f"{SOURCE}-{slugify(board)}-{d.strftime('%Y%m%d')}@calendars.changesaroundme.com"
        if uid in seen:
            continue
        seen.add(uid)
        note = (note or "").strip()
        # Same prefix rule as legistar.finalize: don't double an org name
        # that's already part of the body name.
        base = board if board.startswith(PREFIX) else f"{PREFIX} - {board}"
        summary = base + (f" ({note})" if note else "")
        kind = "special" if re.search(r"special", note, re.IGNORECASE) else "regular"
        events.append(
            Event(
                source=SOURCE,
                summary=summary,
                start=d,
                location="",
                url=docs_url,
                status="CANCELLED" if CANCEL_RE.search(note) else "CONFIRMED",
                kind=kind,
                description=(
                    "All-day placeholder: time and agenda post to the board's "
                    f"meeting page ~a week ahead: {docs_url}"
                ),
                uid=uid,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Annual council schedule PDF (the year-ahead when/where for City Council).
#
# The city publishes a "202X City Council Meeting Calendar" PDF in EDIMS with
# three columns: Work Session | Council Meetings | Cancelled Dates, where **
# marks budget-focused meetings. Plain text extraction interleaves the
# columns, so we parse POSITIONALLY: bucket each date by its x-coordinate
# under the nearest column header. Verified against the real 2026 document.
#
# NOTE: the EDIMS document id changes each year — update ANNUAL_PDF_URL when
# the city posts the next calendar (linked from austintexas.gov/council/
# meetings). If the live fetch fails, the bundled fixture copy keeps the
# schedule flowing and a health problem flags the staleness.
# ---------------------------------------------------------------------------
ANNUAL_PDF_URL = "https://services.austintexas.gov/edims/document.cfm?id=462100"
ANNUAL_PDF_FIXTURE = (
    pathlib.Path(__file__).parent.parent / "fixtures" / "austin-council-2026.pdf"
)
COUNCIL_MEETINGS_URL = "https://www.austintexas.gov/council/meetings"

MONTHS_FULL = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"])}
PDF_DATE_RE = re.compile(r"^(\w+) (\d{1,2})(?:-(\d{1,2}))?,? (\d{4})(\*\*)?$")


def parse_council_pdf(data: bytes) -> list[tuple[date, str, bool]]:
    """Return (day, column, budget_flag) for every date in the schedule pages.

    column is one of "work-session" | "council" | "cancelled".
    """
    records: list[tuple[date, str, bool]] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            hdr = next((w for w in words if w["text"] == "Work"), None)
            if hdr is None:
                continue  # calendar-grid page, not a schedule page
            anchors = {}
            for w in words:
                if abs(w["top"] - hdr["top"]) > 3:
                    continue
                if w["text"] == "Work":
                    anchors["work-session"] = w["x0"]
                elif w["text"] == "Council":
                    anchors["council"] = w["x0"]
                elif w["text"].startswith("Cancelled"):
                    anchors["cancelled"] = w["x0"]
            if len(anchors) < 3:
                continue
            rows: dict[int, list] = {}
            for w in words:
                if w["top"] <= hdr["top"] + 3:
                    continue
                rows.setdefault(round(w["top"] / 4), []).append(w)
            for _, ws in sorted(rows.items()):
                ws.sort(key=lambda w: w["x0"])
                i = 0
                while i < len(ws):
                    if ws[i]["text"] in MONTHS_FULL:
                        m = PDF_DATE_RE.match(" ".join(t["text"] for t in ws[i:i + 3]))
                        if m:
                            month, d1 = m.group(1), int(m.group(2))
                            d2 = int(m.group(3)) if m.group(3) else d1
                            year, budget = int(m.group(4)), bool(m.group(5))
                            col = min(anchors, key=lambda k: abs(anchors[k] - ws[i]["x0"]))
                            for day in range(d1, d2 + 1):
                                records.append((date(year, MONTHS_FULL[month], day), col, budget))
                            i += 3
                            continue
                    i += 1
    return records


def annual_council_events(records, legistar_events: list[Event]) -> list[Event]:
    """All-day placeholders for Council-column dates, yielding to Legistar.

    Once a timed City Council (or budget-meeting) event exists in Legistar
    for a date, the placeholder is skipped — the meeting has "graduated" to
    a timed entry with an agenda. Only the Council Meetings column is
    emitted for now (work sessions / cancelled dates are parsed and could
    be enabled later).
    """
    council_day_re = re.compile(
        r"^austin-(city-council|budget-meeting-of-the-austin-city-council)-(\d{8})T"
    )
    covered = {m.group(2) for e in legistar_events
               if (m := council_day_re.match(e.stable_uid()))}
    events: list[Event] = []
    for day, col, budget in records:
        if col != "council":
            continue
        d8 = day.strftime("%Y%m%d")
        if d8 in covered:
            continue
        events.append(
            Event(
                source=SOURCE,
                summary="Austin - City Council",
                start=day,
                url=COUNCIL_MEETINGS_URL,
                kind="budget" if budget else "regular",
                description=(
                    "From the published annual council calendar (dates subject "
                    "to change). Time and agenda appear ~a week ahead: "
                    f"{COUNCIL_MEETINGS_URL}"
                ),
                uid=f"{SOURCE}-city-council-{d8}@calendars.changesaroundme.com",
            )
        )
    return events


def fetch_annual(session) -> bytes:
    try:
        resp = session.get(ANNUAL_PDF_URL, timeout=30)
        resp.raise_for_status()
        if resp.content[:5] == b"%PDF-":
            return resp.content
        raise ValueError("EDIMS response is not a PDF")
    except Exception as exc:
        _problems.append(f"austin: annual PDF fetch failed, using bundled copy ({exc})")
        return ANNUAL_PDF_FIXTURE.read_bytes()


# Reset by fetch(); read by build.py's health check so a single dead board
# turns CI red instead of silently shrinking coverage behind a green run.
_problems: list[str] = []


def health_problems() -> list[str]:
    return list(_problems)


def fetch_boards(session) -> list[Event]:
    events: list[Event] = []
    for board, page_url, docs_url in BOARDS:
        try:
            resp = session.get(page_url, timeout=30)
            resp.raise_for_status()
            found = parse_board_page(resp.text, board, docs_url)
            if not found:
                _problems.append(f"austin: 0 dates parsed for board '{board}'")
            events.extend(found)
        except Exception as exc:  # one board failing shouldn't sink the rest
            _problems.append(f"austin: board fetch failed ({board}): {exc}")
    return events


def fetch(session) -> list[Event]:
    _problems.clear()
    council = COUNCIL.fetch(session)
    try:
        records = parse_council_pdf(fetch_annual(session))
        annual = annual_council_events(records, council)
        if len(annual) < 5:
            _problems.append(
                f"austin: annual council schedule looks thin ({len(annual)} events)"
            )
    except Exception as exc:
        _problems.append(f"austin: annual council schedule failed: {exc}")
        annual = []
    return council + annual + fetch_boards(session)
