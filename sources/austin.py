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
        "Budget Meeting of the Austin City Council": "City Council: Budget Meeting",
        "City Council Work Session": "City Council: Work Session",
        "City Council Budget Work Session": "City Council: Budget Work Session",
        "City Council Special Called Meeting": "City Council: Special Called Meeting",
    },
    # Every Legistar body ending in "Committee" is a committee OF the council
    # (verified against the full bodies list 2026-07-25); corporations, TIF
    # zones, etc. don't match and keep their own names. Rule, not list, so
    # future committees are prefixed automatically.
    display_transform=lambda s: (
        f"City Council: {s}"
        if s.endswith("Committee") and not s.startswith("City Council")
        else s
    ),
    # InSite's location cell sometimes carries junk suffixes; canonicalize.
    location_fixes={"Austin City Hall": CITY_HALL},
    # Summarize agenda items into descriptions for council meetings (incl.
    # work sessions, "Budget Meeting of the ...") and council committees
    # (every Legistar body ending in "Committee" is one — same rule as
    # display_transform above). Corporations etc. stay excluded.
    agenda_detail=lambda name: name.startswith("City Council")
    or name.startswith("Budget Meeting")
    or name.endswith("Committee"),
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
    # Batch added 2026-08-09 from the KB Organizations "To add: Definitely"
    # list (Design Commission and UTC were already tracked). Docs ids
    # discovered from each landing page's "Agendas, Approved Minutes..."
    # link; typical times/locations below are fallbacks transcribed from
    # each page's Meeting Details accordion, which the auto-extraction
    # re-reads (and overrides these) on every fetch.
    ("Bond Oversight Commission",
     "https://www.austintexas.gov/boards-commissions/board/bond-oversight-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/2_1",
     "14:00", f"Board and Commission Room 1101, {CITY_HALL}"),
    ("Environmental Commission",
     "https://www.austintexas.gov/boards-commissions/board/environmental-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/28_1",
     "18:00", ("Permitting and Development Center, Events Center Room 1405, "
               "6310 Wilhelmina Delco Dr., Austin, TX 78752")),
    ("Small Area Planning Joint Committee",
     "https://www.austintexas.gov/boards-commissions/board/small-area-planning-joint-committee",
     "https://www.austintexas.gov/boards-commissions/meetings/139_1",
     # Their page names no room — venue only, per the venue-name rule.
     "11:30", ("Permitting and Development Center, "
               "6310 Wilhelmina Delco Dr., Austin, TX 78752")),
    ("Historic Landmark Commission",
     "https://www.austintexas.gov/boards-commissions/board/historic-landmark-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/31_1",
     "18:00", f"Council Chambers Room 1001, {CITY_HALL}"),
    ("Electric Utility Commission",
     "https://www.austintexas.gov/boards-commissions/board/electric-utility-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/27_1",
     # Exactly this form — Apple's calendar apps geocode it (Ian, 8/10).
     "18:00", ("Austin Energy Headquarters, 4815 Mueller Blvd, "
               "Austin, TX 78723, United States")),
    ("Water and Wastewater Commission",
     "https://www.austintexas.gov/boards-commissions/board/water-and-wastewater-commission",
     "https://www.austintexas.gov/boards-commissions/meetings/52_1",
     "17:30", "Waller Creek Center, 625 E. 10th St., Austin, TX 78701"),
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
    r"|November|December)\s+(\d{1,2}),?\s+(20\d{2})\s*\*?"
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
            # Trailing "unless otherwise noted/indicated ..." hedges are
            # boilerplate, and some carry the word "agenda" — strip BEFORE
            # the skip-check so a real venue isn't discarded over its hedge
            # (Historic Landmark: "City Hall, Council Chambers unless
            # otherwise noted in the agenda posting").
            txt = re.sub(r"\s*[,–—-]?\s*unless\b.*$", "", txt,
                         flags=re.IGNORECASE)
            if loc is None and txt and not SKIP_LOC_RE.search(txt):
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
    # Auto-detected details win — EXCEPT a configured location beats an
    # auto-extracted one that lacks a full mailing address (no TX zip):
    # calendar apps only geocode complete addresses, and the accordions
    # often stop at "…, 4815 Mueller Blvd." (Ian, 2026-08-10).
    auto_time, auto_loc = parse_meeting_details(soup)
    typical_time = auto_time or typical_time
    if auto_loc and (re.search(r"(?:TX|Texas)\s*\d{5}", auto_loc)
                     or not typical_location):
        typical_location = auto_loc
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
        # Display prefix ("CoA: " on
        # Austin boards and one-offs; the City Council family keeps its
        # bare names). The uid above froze from the raw board name first.
        summary = f"CoA: {board}" + (f" ({note})" if note else "")
        kind = "special" if re.search(r"special", note, re.IGNORECASE) else "regular"
        if typical_time:
            h, mnt = map(int, typical_time.split(":"))
            start = datetime(d.year, d.month, d.day, h, mnt)
            end = start + timedelta(hours=BOARD_MEETING_HOURS)
            # The event's url field IS the meeting-documents page — don't
            # repeat it here (calendar apps show both and it reads twice).
            desc = ("Typical start time from the board's regular schedule — "
                    "confirm on the agenda posted at the event link "
                    "(~a week ahead).")
        else:
            start, end = d, None
            desc = ("All-day entry: this board's typical start time isn't "
                    "configured yet; the agenda posts at the event link "
                    "~a week ahead.")
        cancelled = bool(CANCEL_RE.search(note))
        if cancelled:
            # Start-time commentary is noise on a meeting that isn't
            # happening (Ian, 2026-08-09) — cancelled entries get no body.
            desc = ""
        events.append(
            Event(
                source=SOURCE,
                summary=summary,
                start=start,
                end=end,
                location=typical_location or "",
                url=docs_url,
                status="CANCELLED" if cancelled else "CONFIRMED",
                kind=kind,
                description=desc,
                uid=uid,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Board agenda enrichment: the posted agenda is the authoritative when/where.
#
# Each board's meeting-documents page (the docs_url in BOARDS — the same
# "Agendas, Approved Minutes and Supporting Documents" link the KB
# Organizations page carries) lists per-date document rows; the Agenda link
# is an EDIMS PDF whose page-1 header states the confirmed start time
# ("TUESDAY, AUGUST 4, 2026, AT 5:00 P.M."). For upcoming meetings we fetch
# that chain and replace the typical-time guess with the agenda's time and
# a direct link to the agenda itself. Upgrade-only: any failure (agenda not
# posted yet, page redesign, PDF quirk) leaves the event exactly as the
# typical-time path built it. UIDs are date-only and never touched.
# ---------------------------------------------------------------------------
AGENDA_HORIZON = timedelta(days=45)   # agendas post ~a week out; 45d is slack
AGENDA_FETCH_CAP = 16   # PDFs per run, across all boards (13 tracked;
                        # agendas post ~a week out, so posted-at-once
                        # stays well under this)

AGENDA_HEADER_RE = re.compile(
    # Header separators vary: UTC says "AUGUST 4, 2026 AT 5:00 P.M.", EUC
    # "August 10, 2026 ▪ 6:00 PM" (decorative glyph — whatever pdfplumber
    # extracts it as, [\W_]{0,8} swallows symbols/space but never words or
    # digits). Safe to relax: a time must still follow the date directly,
    # AM/PM and all, and agenda_start still demands exactly one distinct
    # time for the day.
    r"(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER"
    r"|NOVEMBER|DECEMBER)\s+(\d{1,2}),\s*(\d{4})[\W_]{0,8}(?:AT\s+)?"
    r"(\d{1,2})(?::(\d{2}))?\s*([AP])\.?\s*M\b",
    re.IGNORECASE)


def _day_of(e: Event) -> date:
    return e.start.date() if isinstance(e.start, datetime) else e.start


def parse_meetings_page(html: str) -> dict[date, str]:
    """date -> Agenda PDF url from a board's meeting-documents page.

    The bcic markup is flat siblings: a bcic_mtgdate div (date, possibly
    "(Cancelled)"), then bcic_doc divs until the next date. Only the link
    whose text is exactly "Agenda" counts — cancellation notices, minutes,
    backup and video rows all carry other labels.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict[date, str] = {}
    for div in soup.find_all("div", class_="bcic_mtgdate"):
        m = DATE_LI_RE.match(div.get_text(" ", strip=True))
        if not m:
            continue
        try:
            d = datetime.strptime(
                f"{m.group(1)} {int(m.group(2))} {m.group(3)}", "%B %d %Y"
            ).date()
        except ValueError:
            continue
        for sib in div.find_next_siblings("div"):
            classes = sib.get("class") or []
            if "bcic_mtgdate" in classes:
                break
            if "bcic_doc" not in classes:
                continue
            a = next((a for a in sib.find_all("a")
                      if a.get_text(" ", strip=True) == "Agenda"), None)
            if a and a.get("href") and d not in out:
                out[d] = a["href"]
    return out


def _agenda_pages(pdf_bytes: bytes, n: int = 10) -> list[str]:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [(p.extract_text() or "") for p in pdf.pages[:n]]


# --- board agenda digest (Ian's format, 2026-08-10) -------------------------
# Case items on land-use agendas type themselves ("2. Plan Amendment:
# NPA-2025-0018.02 - ..."), so counting by type is declared, not inferred.
# Discussion items are the rare high-signal entries -> quoted verbatim
# (capped); standing committee/working-group updates are boilerplate on
# every agenda -> counts only (verified against the 11 Aug 2026 PC agenda).
CASE_NUM_RE = re.compile(r"\b[A-Z]{1,4}\d{0,2}[A-Z]?-\d{2,4}-[0-9A-Za-z.()]+")
CASE_TYPE_CANON = {
    "Plan": "Plan Amendment", "Rezoning": "Rezoning", "Zoning": "Rezoning",
    "Historic": "Historic Zoning",
    "Restrictive": "Restrictive Covenant Termination",  # label wraps in the
    # PDF text layer ("Restrictive / Covenant / Termination:"), so the first
    # word is the reliable key
    "Site": "Site Plan", "Preliminary": "Preliminary Plan",
    "Final": "Final Plat", "Conditional": "Conditional Use",
}
AGENDA_SECTIONS = {
    "PUBLIC HEARINGS": "case",
    "STAFF BRIEFINGS": "briefing",
    # Historic Landmark Commission (Ian's mock, 2026-09-02): hearings are
    # grouped under Title-Case subsection headings ("... Applications")
    # and every item states its council district — the digest counts
    # items per subsection per district. Committee updates reduce to
    # date + body.
    "BRIEFINGS": "briefing",
    "PUBLIC HEARINGS/DISCUSSION ITEMS": "hearing-grouped",
    "COMMITTEE UPDATES": "updates",
    "APPROVAL OF MINUTES": "skip",
    "PUBLIC COMMUNICATION: GENERAL": "skip",
    "FUTURE AGENDA ITEMS": "skip",
    # AAC's slash heading is its ACTION section (count-only); PC's "AND"
    # heading is where novel one-offs land (quoted). Distinct on purpose.
    "DISCUSSION/ACTION ITEMS": "action",
    # Same handling; the digest keeps the agenda's own section wording
    # ("N discussion and action items:" vs "N discussion items:").
    "DISCUSSION AND ACTION ITEMS": "discussion-action",
    "DISCUSSION ITEMS": "discussion",
    "PERMANENT COMMITTEE UPDATES": "committee",
    "WORKING GROUP UPDATES": "workgroup",
}
BRIEFING_PREFIX_RE = re.compile(r"^(?:Staff\s+)?[Bb]riefing\s+(?:on|regarding|about)\s+")
# "... Report by Lisa Martin, Deputy General Manager and COO" — a trailing
# by-<Capitalized Name> clause is a presenter credit, not the topic (Ian,
# 2026-08-10). Requires a capitalized word after "by" so "by the board" or
# "by-laws" survive.
BRIEFING_BY_RE = re.compile(r"\s+by\s+[A-Z][\w.'-]*.*$")
# "the Third Quarter Operations Report" -> "Q3 Operations Report" (brevity,
# same request).
QUARTER_RE = re.compile(r"(?:[Tt]he\s+)?\b(First|Second|Third|Fourth)\s+[Qq]uarter\b")
QUARTERS = {"first": "Q1", "second": "Q2", "third": "Q3", "fourth": "Q4"}
BRIEFING_PRESENTER_RE = re.compile(r"[.\s]+(?:presented|provided|given)\s+by\b.*$",
                                   re.DOTALL | re.IGNORECASE)
QUOTE_CAP = 3      # discussion items shown individually up to this many
QUOTE_CHARS = 160
# Item lines show the TOPIC, not the procedural sentence (Ian, 2026-08-23:
# "that way the space is focused on the topic"): strip the "Discussion and
# possible action regarding the..." lead-in, then trim elaboration. The
# bounded second pattern catches indirect leads ("...to approve a
# recommendation regarding the X") without ever eating a topic's own
# mid-sentence "regarding".
ITEM_LEAD_RE = re.compile(
    r"^Discussion(?:\s+and\s+possible\s+action)?\s+"
    r"(?:on|regarding|about)\s+(?:the\s+)?", re.IGNORECASE)
ITEM_LEAD_INDIRECT_RE = re.compile(
    r"^Discussion\s+and\s+possible\s+action\s+.{0,60}?"
    r"\bregarding\s+(?:the\s+)?", re.IGNORECASE)
ITEM_ELABORATION_RE = re.compile(r",\s+including\b.*$", re.DOTALL)


HLC_SUBSECTIONS = {  # heading -> digest label (Ian's wording)
    "historic landmark and local historic district applications":
        "Historic Landmark/Local Historic District",
    "national register historic district permit applications":
        "National Register Historic District",
    "demolition and relocation permit applications":
        "Demolition and Relocation",
}
SUBSECTION_RE = re.compile(r"^[A-Z][A-Za-z/&' -]+ Applications$")
DISTRICT_RE = re.compile(r"^Council District (\d{1,2})\b")
# "Update from the Architectural Review Committee regarding the August 12,
# 2026 meeting." / "Update from Commissioner X regarding the August 19,
# 2026 Downtown Commission meeting." -> "12 Aug 2026 Architectural Review
# Committee" / "19 Aug 2026 Downtown Commission".
UPDATE_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+(\d{1,2}),\s+(20\d{2})")
UPDATE_BODY_RE = re.compile(
    r"^Update from (?:the )?(?P<from>.+?) regarding the "
    r"(?P<date>[A-Z][a-z]+ \d{1,2}, 20\d{2}) (?P<after>.*?)\s*meeting\b")
# (prefix match: a wrapped "Members: ..." tail after "meeting." is ignored)


def _update_line(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    m = UPDATE_BODY_RE.match(text)
    if not m:
        return text
    dm = UPDATE_DATE_RE.search(m.group("date"))
    when = ""
    if dm:
        from datetime import date as _date
        try:
            when = _date(int(dm.group(3)),
                         ["January", "February", "March", "April", "May",
                          "June", "July", "August", "September", "October",
                          "November", "December"].index(dm.group(1)) + 1,
                         int(dm.group(2))).strftime("%-d %b %Y")
        except ValueError:
            pass
    # The body is whichever side names a committee/commission: "from the
    # Architectural Review Committee regarding the <date> meeting" or
    # "from Commissioner X regarding the <date> Downtown Commission meeting".
    body = m.group("after").strip() or m.group("from").strip()
    return f"{when} {body}".strip()


def _district_counts(items: list[tuple[str, str | None]]) -> str:
    """[(subsection, district)] -> "1 x D1, 2 x D9" ordered by district."""
    counts: dict[str, int] = {}
    for _, d in items:
        key = f"D{d}" if d else "district n/a"
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{n} x {k}" for k, n in sorted(
        counts.items(), key=lambda kv: (kv[0] == "district n/a",
                                        int(kv[0][1:]) if kv[0][1:].isdigit() else 99)))


def remote_registration(pages: list[str], day: date) -> str | None:
    """Top-of-body action line when the agenda requires advance registration
    to speak remotely ("noon the day before" + a registration link)."""
    text = "\n".join(pages)
    low = text.lower()
    link = re.search(r"https://forms\.office\.com/\S+", text)
    if not link or "the day before" not in low:
        return None
    clock = "noon" if "noon" in low or "12 pm" in low else "the deadline"
    before = (day - timedelta(days=1)).strftime("%-d %b %Y")
    return (f"To speak remotely, register by {clock} {before}:\n"
            f"{link.group(0).rstrip('.')}")


def agenda_summary(pages: list[str]) -> str | None:
    section = None
    cases: list[str] = []
    briefings: list[list] = []    # [number, text] — text grows on wraps
    discussion: list[list] = []
    disc_label = "discussion item"
    action = committee = workgroup = 0
    grouped: list[tuple[str, str | None]] = []   # (subsection, district)
    subsection = ""
    updates: list[list] = []
    cur = None
    for raw in "\n".join(pages).splitlines():
        line = raw.strip()
        up = line.upper()
        if up == "ADJOURNMENT":
            break  # everything after is how-to-participate boilerplate
        if up in AGENDA_SECTIONS:
            section, cur = AGENDA_SECTIONS[up], None
            continue
        if section == "hearing-grouped":
            if SUBSECTION_RE.match(line):
                subsection = HLC_SUBSECTIONS.get(
                    line.lower(), re.sub(r"\s+(Permit\s+)?Applications$", "", line))
                continue
            dm = DISTRICT_RE.match(line)
            if dm and grouped and grouped[-1][1] is None:
                grouped[-1] = (grouped[-1][0], dm.group(1))
                continue
        m = re.match(r"(\d{1,3})\.\s+(.+)", line)
        if m and section:
            cur = None
            body = m.group(2).strip()
            if section == "skip":
                continue
            if section == "hearing-grouped":
                grouped.append((subsection or "Other", None))
                continue
            if section == "updates":
                updates.append([int(m.group(1)), body])
                cur = updates[-1]
                continue
            if section == "case":
                cm = CASE_NUM_RE.search(body)
                if cm:
                    first = body.split(" ", 1)[0].rstrip(":")
                    cases.append(CASE_TYPE_CANON.get(
                        first, body[:cm.start()].rstrip(" :") or "Other"))
            elif section == "briefing":
                briefings.append([int(m.group(1)), body])
                cur = briefings[-1]
            elif section in ("discussion", "discussion-action"):
                if section == "discussion-action":
                    disc_label = "discussion and action item"
                discussion.append([int(m.group(1)), body])
                cur = discussion[-1]
            elif section == "action":
                action += 1
            elif section == "committee":
                committee += 1
            elif section == "workgroup":
                workgroup += 1
        elif cur and line and not line.isupper():
            if len(cur[1]) < QUOTE_CHARS + 60:  # wrapped sentence continues
                cur[1] += " " + line
    if not (cases or briefings or discussion or action or committee
            or workgroup or grouped or updates):
        return None

    def _s(n):
        return "" if n == 1 else "s"

    out = ["Summary Agenda"]  # standardized digest header (Ian, 2026-08-09)
    if cases:
        by_type: dict[str, int] = {}
        for t in cases:
            by_type[t] = by_type.get(t, 0) + 1
        out.append(f"{len(cases)} case{_s(len(cases))}: "
                   + ", ".join(f"{n} x {t}" for t, n in by_type.items()))
    if briefings:
        titles = []
        for _, text in briefings:
            t = re.sub(r"\s+", " ", text).strip()
            t = BRIEFING_PRESENTER_RE.sub("", BRIEFING_PREFIX_RE.sub("", t))
            t = BRIEFING_BY_RE.sub("", t)
            t = QUARTER_RE.sub(lambda m: QUARTERS[m.group(1).lower()], t)
            t = re.sub(r"^(?:[Tt]he|[Aa]n?)\s+", "", t)  # leading article
            t = t[:1].upper() + t[1:]
            # Parenthetical program tags read as clutter in titles (Ian).
            t = re.sub(r"\s*\([^)]*\)", "", t)
            titles.append(f"- {re.sub(r'  +', ' ', t).strip(' .,')}")
        out.append(f"{len(briefings)} briefing{_s(len(briefings))}:\n"
                   + "\n".join(titles))
    if grouped:
        by_sub: dict[str, list] = {}
        for sub, d in grouped:
            by_sub.setdefault(sub, []).append((sub, d))
        out.append(f"{len(grouped)} public hearing{_s(len(grouped))}:\n"
                   + "\n".join(f"- {sub} ({_district_counts(items)})"
                                for sub, items in by_sub.items()))
    if action:
        out.append(f"{action} action item{_s(action)}")
    if discussion:
        def _q(num, text):
            t = re.sub(r"\s+", " ", text).strip()
            # Sponsors are procedural noise in a calendar body (Ian, 8/12).
            t = re.sub(r"\s*\(Sponsored by[^)]*\)\s*\.?\s*$", "", t)
            t = ITEM_LEAD_RE.sub("", t) if ITEM_LEAD_RE.match(t) \
                else ITEM_LEAD_INDIRECT_RE.sub("", t)
            t = re.split(r"\.\s+", t)[0]        # topic = first sentence
            t = ITEM_ELABORATION_RE.sub("", t)  # drop ", including ..."
            t = t.strip(" .,")
            t = t[:1].upper() + t[1:]
            if len(t) > QUOTE_CHARS:
                t = t[:QUOTE_CHARS].rstrip() + "…"
            return f"#{num}: {t}"
        if all(re.sub(r"\s+", " ", t).strip().startswith(
                "Review City Council action") for _, t in discussion):
            # Standing post-council reviews: count them, note the pattern.
            out.append(f"{len(discussion)} {disc_label}"
                       f"{_s(len(discussion))} (all related to recent City "
                       "Council actions)")
        elif len(discussion) <= QUOTE_CAP:
            out.append(f"{len(discussion)} {disc_label}"
                       f"{_s(len(discussion))}:")
            out += [_q(n, t) for n, t in discussion]
        else:
            out.append(f"{len(discussion)} {disc_label}s")
    if committee or workgroup:
        parts = ([f"{committee} permanent committee"] if committee else []) \
            + ([f"{workgroup} working group"] if workgroup else [])
        last = workgroup if workgroup else committee
        out.append(" and ".join(parts) + f" update{_s(last)}")
    if updates:
        out.append(f"{len(updates)} update{_s(len(updates))}:\n"
                   + "\n".join(f"- {_update_line(txt)}" for _, txt in updates))
    # Blank lines between blocks — breathing room in calendar bodies.
    return "\n\n".join(out)


def agenda_start(page1: bytes | str, day: date) -> tuple[int, int] | None:
    """(hour, minute) from the agenda header naming this meeting day.

    Takes the page-1 TEXT (or raw PDF bytes, for direct callers).
    Conservative: the header must state OUR day, and exactly one distinct
    time for it — an ambiguous agenda leaves the typical time in place.
    """
    if isinstance(page1, bytes):
        pages = _agenda_pages(page1, n=1)
        text = pages[0] if pages else ""
    else:
        text = page1
    times: set[tuple[int, int]] = set()
    for m in AGENDA_HEADER_RE.finditer(text):
        try:
            d = date(int(m.group(3)), MONTHS_FULL[m.group(1).title()],
                     int(m.group(2)))
        except (ValueError, KeyError):
            continue
        if d != day:
            continue
        h = int(m.group(4)) % 12 + (12 if m.group(6).upper() == "P" else 0)
        times.add((h, int(m.group(5) or 0)))
    return times.pop() if len(times) == 1 else None


def enrich_board_agendas(events: list[Event], get_html, get_bytes,
                         today: date) -> None:
    """Upgrade upcoming board events in place from their posted agendas."""
    horizon = today + AGENDA_HORIZON
    board_urls = {docs for _, _, docs, _, _ in BOARDS}
    by_docs: dict[str, list[Event]] = {}
    for ev in events:
        # Cancelled meetings still enrich when an agenda exists (a tentative
        # agenda may have posted before the cancellation — Ian, 2026-08-09);
        # they just queue behind confirmed ones for the fetch cap.
        if ev.url in board_urls and today <= _day_of(ev) <= horizon:
            by_docs.setdefault(ev.url, []).append(ev)
    spent = 0
    for docs_url, evs in by_docs.items():
        try:
            agendas = parse_meetings_page(get_html(docs_url))
        except Exception as exc:  # enrichment must never break the feed
            print(f"[{SOURCE}] WARNING: meeting-docs fetch failed "
                  f"({docs_url}): {exc}")
            continue
        evs.sort(key=lambda e: e.status == "CANCELLED")
        for ev in evs:
            d = _day_of(ev)
            agenda_url = agendas.get(d)
            if not agenda_url or spent >= AGENDA_FETCH_CAP:
                continue
            spent += 1
            try:
                pages = _agenda_pages(get_bytes(agenda_url))
                t = agenda_start(pages[0], d) if pages else None
                summary = agenda_summary(pages)
            except Exception as exc:
                print(f"[{SOURCE}] WARNING: agenda parse failed "
                      f"({agenda_url}): {exc}")
                continue
            # A fetched agenda ALWAYS contributes its link and digest —
            # even when the header won't confirm a start time (Ian,
            # 2026-08-10; the EUC agenda cost tonight's meeting its link
            # under the old all-or-nothing rule). The time upgrade stays
            # conservative: only a confirmed header replaces the start, and
            # only then does the typical-time caveat drop — commentary
            # survives exactly where the uncertainty is real.
            caveat = ""
            if t is not None:
                ev.start = datetime(d.year, d.month, d.day, *t)
                ev.end = ev.start + timedelta(hours=BOARD_MEETING_HOURS)
            else:
                caveat = ev.description  # typical-time / all-day note
            register = remote_registration(pages, d) if pages else None
            ev.description = "\n\n".join(x for x in [
                register, "—" if register and summary else "",
                summary, "—" if summary else "", caveat,
                f"Agenda: {agenda_url}"] if x)


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
                summary="City Council: Budget Meeting" if budget else "City Council",
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
                ev.summary = "City Council: Budget Meeting"
    return council


def note_related_budget_meetings(council: list[Event]) -> None:
    """Cross-note budget meetings that share one published agenda.

    The August budget readings post as two Legistar meetings carrying an
    IDENTICAL agenda (observed 12/14 Aug 2026); each gets a parenthetical
    under its Summary Agenda header naming the other date(s). Equality of
    the published digest block is the declared trigger — display-only, no
    identity inference, and it self-corrects if an agenda later diverges.
    """
    groups: dict[str, list[Event]] = {}
    for ev in council:
        if (ev.summary == "City Council: Budget Meeting"
                and (ev.description or "").startswith("Summary Agenda")):
            groups.setdefault(
                ev.description.split("\n\n—\n\n", 1)[0], []).append(ev)
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda e: str(e.start))
        for ev in group:
            others = " and ".join(_day_of(x).strftime("%-d %b")
                                  for x in group if x is not ev)
            s = "" if len(group) == 2 else "s"
            ev.description = ev.description.replace(
                "Summary Agenda\n",
                f"Summary Agenda\n(same agenda as the {others} "
                f"meeting{s})\n", 1)


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
    # After apply_budget_flags — the annual calendar may be what names a
    # meeting "City Council: Budget Meeting" in the first place.
    note_related_budget_meetings(council)
    boards = fetch_boards(session)
    try:
        enrich_board_agendas(
            boards,
            lambda u: _got(session, u).text,
            lambda u: _got(session, u).content,
            datetime.now().date(),
        )
    except Exception as exc:  # upgrade-only: never sink the boards
        print(f"[{SOURCE}] WARNING: agenda enrichment failed: {exc}")
    return council + annual + boards


def _got(session, url):
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp


def fetch_offline() -> list[Event]:
    """Build from fixtures/ (no network) — the --offline contract.

    Mirrors fetch(): Legistar page+API merge, budget flags + annual
    placeholders from the checked-in PDF, then one board page (with no
    typical-time fallbacks, so the Meeting Details auto-extraction is
    exercised).
    """
    import json

    # Item-fetch gate: the API's "Public Health Committee  " (trailing
    # spaces, 2 Sep 2026) must pass; corporations and older meetings must
    # not.
    gate = lambda name, day: COUNCIL.wants_items(
        {"EventBodyName": name, "EventDate": day, "EventId": 1}, "2026-08-27")
    assert gate("Public Health Committee  ", "2026-09-02T00:00:00")
    assert gate("City Council", "2026-09-10T00:00:00")
    assert not gate("Austin Housing Finance Corporation", "2026-09-10T00:00:00")
    assert not gate("Public Health Committee", "2026-08-26T00:00:00")

    fixtures = ANNUAL_PDF_FIXTURE.parent
    council = COUNCIL.finalize(
        COUNCIL.merge_api(
            COUNCIL.parse_calendar_html(
                (fixtures / "austin_legistar.html").read_text()
            ),
            json.loads((fixtures / "austin_api.json").read_text()),
            # Offline: serve the checked-in eventitems capture for council
            # rows so the agenda-summary path is exercised deterministically.
            item_fetcher=lambda row: (
                json.loads((fixtures / "austin_eventitems.json").read_text())
                if (row.get("EventBodyName") or "").startswith("City Council")
                else None
            ),
        )
    )
    records = parse_council_pdf(ANNUAL_PDF_FIXTURE.read_bytes())
    council = apply_budget_flags(council, records)
    # Mirrors fetch(): the fixture eventitems land on every council row, so
    # identical-digest budget meetings exercise the related-note path here.
    note_related_budget_meetings(council)
    annual = annual_council_events(records, council)
    boards = parse_board_page(
        (fixtures / "austin_board.html").read_text(),
        "Planning Commission",
        "https://www.austintexas.gov/boards-commissions/meetings/40_1",
    )
    # Agenda-enrichment chain, exercised end-to-end from real captures:
    # UTC board page -> meeting-documents page -> the 4 Aug 2026 agenda PDF
    # ("... AT 5:00 P.M."). today pinned so the Aug 4 meeting is upcoming.
    utc = parse_board_page(
        (fixtures / "austin_board_utc.html").read_text(),
        "Urban Transportation Commission",
        "https://www.austintexas.gov/boards-commissions/meetings/50_1",
    )
    agenda_pdf = fixtures / "austin_agenda_utc_20260804.pdf"
    if agenda_pdf.exists():
        enrich_board_agendas(
            utc,
            lambda u: (fixtures / "austin_meetings_utc.html").read_text(),
            lambda u: agenda_pdf.read_bytes(),
            today=date(2026, 8, 1),
        )
    # HLC agenda grammar (hearings counted per subsection per council
    # district, committee updates, remote-registration action line):
    # parser check on the real 2 Sep 2026 agenda. Fails the offline build
    # loudly if the grammar regresses; the board-page chain is UTC's above.
    hlc_pdf = fixtures / "austin_agenda_hlc_20260902.pdf"
    if hlc_pdf.exists():
        pages = _agenda_pages(hlc_pdf.read_bytes())
        digest = agenda_summary(pages) or ""
        assert "18 public hearings" in digest and "(1 x D1, 2 x D9)" in digest, digest
        assert "12 Aug 2026 Architectural Review Committee" in digest, digest
        assert remote_registration(pages, date(2026, 9, 2)), "registration line"
    return council + annual + boards + utc
