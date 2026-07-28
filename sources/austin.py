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

Note that BOARDS also carries the two council advisory councils (BAC, PAC).
Their schedule pages live under /council/ rather than /boards-commissions/,
but they publish the same <li> date list and their agendas still land on a
/boards-commissions/meetings/ page — same parser, just a different URL.
"""
from __future__ import annotations

import io
import pathlib
import re
from datetime import date, datetime, timedelta

import pdfplumber
from bs4 import BeautifulSoup

from caltools.model import Event, slugify
from sources.legistar import CANCEL_RE, Legistar

SOURCE = "austin"

CITY_HALL = "Austin City Hall, 301 W. 2nd St., Austin, TX 78701"
PDC_1401 = ("Permitting and Development Center, Room 1401, "
            "6310 Wilhelmina Delco Dr., Austin, TX 78752")

COUNCIL = Legistar(
    source=SOURCE,
    client="austintexas",
    host="austintexas.legistar.com",
    prefix="",  # "Austin" is assumed for this calendar; no org prefix
    display_names={
        "Budget Meeting of the Austin City Council": "City Council - Budget Meeting",
        "City Council Work Session": "City Council - Work Session",
        "City Council Budget Work Session": "City Council - Budget Work Session",
        "City Council Special Called Meeting": "City Council - Special Called Meeting",
    },
    # Every Legistar body ending in "Committee" is a committee OF the council
    # (verified against the full bodies list 2026-07-25); corporations, TIF
    # zones, etc. don't match and keep their own names. Rule, not list, so
    # future committees are prefixed automatically.
    display_transform=lambda s: (
        f"City Council - {s}"
        if s.endswith("Committee") and not s.startswith("City Council")
        else s
    ),
    # InSite's location cell sometimes carries junk suffixes; canonicalize.
    location_fixes={"Austin City Hall": CITY_HALL},
)

# (Board name, schedule page, meeting-documents page,
#  typical start time "HH:MM" or None, typical location or None)
# Typical times/locations come from each board's regular schedule; events
# built from them say "confirm on the agenda". None = all-day placeholder.
BOARDS = [
    ("Planning Commission",
     "https://www.austintexas.gov/boards-commissions/board/planning-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/40_1",
     "18:00", f"Council Chambers Room 1001, {CITY_HALL}"),
    ("Urban Transportation Commission",
     "https://www.austintexas.gov/boards-commissions/board/urban-transportation-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/50_1",
     None, None),
    ("Design Commission",
     "https://www.austintexas.gov/boards-commissions/board/design-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/22_1",
     None, None),
    ("Airport Advisory Commission",
     "https://www.austintexas.gov/boards-commissions/board/airport-advisory-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/7_1",
     None, None),
    ("Austin Integrated Water Resource Planning Community Task Force",
     "https://www.austintexas.gov/boards-commissions/board/austin-integrated-water-resource-planning-community-task-force",
     "https://www.austintexas.gov/boards-commissions/meetings/132_1",
     None, None),
    # Council advisory councils: schedule page under /council/, agendas on a
    # /boards-commissions/meetings/ page. Both pages DO state their time and
    # venue, but as an <h3> + <ul> rather than the <dt>/<dd> accordion that
    # parse_meeting_details reads — so the values below are what ship.
    # Verified against the live markup 2026-07-28; recheck at year rollover.
    ("Bicycle Advisory Council",
     "https://www.austintexas.gov/council/bicycle-advisory-council",
     "https://www.austintexas.gov/boards-commissions/meetings/110_1",
     "18:00", PDC_1401),
    ("Pedestrian Advisory Council",
     "https://www.austintexas.gov/council/pedestrian-advisory-council",
     "https://www.austintexas.gov/boards-commissions/meetings/121_1",
     "18:00", PDC_1401),
]
BOARD_MEETING_HOURS = 3  # boards run long; assumed length for timed entries

# "January 13, 2026" with an optional annotation ("- Special Called",
# "(Cancelled)", or bare trailing text). A bare trailing "*" is a footnote
# marker, not an annotation (BAC/PAC use it for their combined September
# meeting) — swallow it so the date still parses.
DATE_LI_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+(\d{1,2}),?\s+(20\d{2})\*?"
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


TIME_HINT_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b", re.IGNORECASE)
SKIP_LOC_RE = re.compile(
    r"of the month|agenda|check|confirm|monday|tuesday|wednesday|thursday|friday",
    re.IGNORECASE,
)


def parse_meeting_details(soup) -> tuple[str | None, str | None]:
    """Auto-extract (typical time "HH:MM", typical location) from the board
    page's collapsed "Meeting Details" accordion.

    The accordion is display-hidden in a browser but present in the raw HTML,
    e.g.: <dt>Meeting Details</dt><dd><ul>
      <li>Second and fourth Tuesday of the month</li>
      <li>Usually 6 p.m. - unless designated otherwise on agenda</li>
      <li>City Hall, Council Chamber (unless listed otherwise)</li> ...
    Because this is re-read on every daily fetch, a board changing its
    regular time or venue flows into the calendar automatically.
    """
    for el in soup.find_all(string=re.compile(r"Meeting Details")):
        dt = el.find_parent("dt")
        dd = dt.find_next_sibling("dd") if dt else None
        if dd is None:
            continue
        t = loc = None
        for txt in (li.get_text(" ", strip=True) for li in dd.find_all("li")):
            m = TIME_HINT_RE.search(txt)
            if m and t is None:
                h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
                t = f"{h:02d}:{int(m.group(2) or 0):02d}"
                continue
            if loc is None and not SKIP_LOC_RE.search(txt):
                loc = re.sub(r"\s*\([^)]*\)\s*", " ", txt).strip(" ,")
        if loc and "city hall" in loc.lower() and "301" not in loc:
            loc = f"{loc}, 301 W. 2nd St., Austin, TX 78701"
        if t or loc:
            return t, loc
    return None, None


def parse_board_page(html: str, board: str, docs_url: str,
                     typical_time: str | None = None,
                     typical_location: str | None = None) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    # Auto-detected details win; configured values are the fallback.
    auto_time, auto_loc = parse_meeting_details(soup)
    typical_time = auto_time or typical_time
    typical_location = auto_loc or typical_location
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
        # Identity from board + date only; annotations and the configured
        # typical time must never change the UID.
        uid = f"{SOURCE}-{slugify(board)}-{d.strftime('%Y%m%d')}@calendars.changesaroundme.com"
        if uid in seen:
            continue
        seen.add(uid)
        note = (note or "").strip()
        summary = board + (f" ({note})" if note else "")
        kind = "special" if re.search(r"special", note, re.IGNORECASE) else "regular"
        if typical_time:
            h, mnt = map(int, typical_time.split(":"))
            start = datetime(d.year, d.month, d.day, h, mnt)
            end = start + timedelta(hours=BOARD_MEETING_HOURS)
            desc = ("Typical start time from the board's regular schedule — "
                    f"confirm on the posted agenda: {docs_url}")
        else:
            start, end = d, None
            desc = ("All-day entry: this board's typical start time isn't "
                    f"configured yet; agendas post ~a week ahead: {docs_url}")
        events.append(
            Event(
                source=SOURCE,
                summary=summary,
                start=start,
                end=end,
                location=typical_location or "",
                url=docs_url,
                status="CANCELLED" if CANCEL_RE.search(note) else "CONFIRMED",
                kind=kind,
                description=desc,
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
        # (?=T|@) covers both timed and all-day Legistar rows — a row whose
        # time didn't parse (e.g. "Cancelled" in the time column) is all-day
        # but still covers that date, so no contradictory placeholder ships.
        r"^austin-(city-council|budget-meeting-of-the-austin-city-council)-(\d{8})(?=T|@)"
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
                summary="City Council - Budget Meeting" if budget else "City Council",
                start=day,
                location=CITY_HALL,
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


def apply_budget_flags(council: list[Event], records) -> list[Event]:
    """Retitle timed Legistar council meetings that fall on budget** dates.

    InSite sometimes names a budget-focused meeting plainly "City Council";
    the annual calendar knows better. Display-only — UIDs already frozen.
    """
    budget_days = {d.strftime("%Y%m%d") for d, col, b in records
                   if col == "council" and b}
    day_re = re.compile(r"^austin-city-council-(\d{8})(?=T|@)")
    for ev in council:
        m = day_re.match(ev.uid)
        if m and m.group(1) in budget_days:
            ev.kind = "budget"
            if ev.summary == "City Council":
                ev.summary = "City Council - Budget Meeting"
    return council


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
    for board, page_url, docs_url, t_time, t_loc in BOARDS:
        try:
            resp = session.get(page_url, timeout=30)
            resp.raise_for_status()
            found = parse_board_page(resp.text, board, docs_url, t_time, t_loc)
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
        council = apply_budget_flags(council, records)
        annual = annual_council_events(records, council)
        if len(annual) < 5:
            _problems.append(
                f"austin: annual council schedule looks thin ({len(annual)} events)"
            )
    except Exception as exc:
        _problems.append(f"austin: annual council schedule failed: {exc}")
        annual = []
    return council + annual + fetch_boards(session)


def fetch_offline() -> list[Event]:
    """Build from fixtures/ (no network) — the --offline contract.

    Mirrors fetch(): Legistar page+API merge, budget flags + annual
    placeholders from the checked-in PDF, then one board page (with no
    typical-time fallbacks, so the Meeting Details auto-extraction is
    exercised).
    """
    import json

    fixtures = ANNUAL_PDF_FIXTURE.parent
    council = COUNCIL.finalize(
        COUNCIL.merge_api(
            COUNCIL.parse_calendar_html(
                (fixtures / "austin_legistar.html").read_text()
            ),
            json.loads((fixtures / "austin_api.json").read_text()),
        )
    )
    records = parse_council_pdf(ANNUAL_PDF_FIXTURE.read_bytes())
    council = apply_budget_flags(council, records)
    annual = annual_council_events(records, council)
    boards = parse_board_page(
        (fixtures / "austin_board.html").read_text(),
        "Planning Commission",
        "https://www.austintexas.gov/boards-commissions/meetings/40_1",
    )
    return council + annual + boards
