"""Texas Legislature adapter — Senate committee hearings (watchlisted).

Scrapes https://senate.texas.gov/events.php, the Senate's single hearings
page: a server-rendered table.cmte_events with one row per hearing (date,
time linking to the official notice, committee, room, broadcast status,
notes). Only the committees in WATCH — Ian's list, Calendar Maintenance
"Legislature" section — are published; everything else on the page is
ignored. The parallel table.cmte_events_mobile duplicates every row and
must NOT be parsed, or each hearing doubles.

Two quirks drive the design:

* **The page is ephemeral.** Notices are "removed at the end of the day"
  once a hearing has happened (upcoming ones list ~2 months out). So this
  module keeps its own past: fetch() returns the fresh scrape PLUS any
  already-archived event whose day has passed and whose UID the fresh
  scrape no longer carries. Append-only, keyed by UID, no identity
  matching — per the Architecture decisions callout in the KB. A delisted
  FUTURE event is simply dropped (that's a revision or cancellation, not
  history); the day's snapshot in data/legislature.json is the archive.

* **Hearings are sporadic.** Zero upcoming hearings (or only past ones in
  the archive) is a normal, healthy state between interim hearing bursts —
  hence SPORADIC below, which tells build.py to skip the 0-events and
  no-future-events alarms for this source.

Cancellations: the row gets class="cancelled" and a CANCELLED note; the
event is kept with STATUS:CANCELLED (never deleted). REVISED notes ride
along in the description. A revision that changes the TIME changes the
UID: the old time vanishes from the feed (delisted-future rule) and the
new one appears — no phantom pair.

The notice URL (capitol.texas.gov/tlodocs/...) is stable after the page
forgets the hearing; it stays as each event's URL.

House committees (added 2026-08-14, HOUSE_WATCH list): scraped from
capitol.texas.gov's upcoming-meetings page — one usa-table where bold
sectionTitle rows declare the date, Gainsboro separator rows the time, and
committee rows inherit both. House notices are the same tlodocs family as
the Senate's, so enrich_notices and the archive/retention machinery apply
to both chambers unchanged (UIDs can't collide: House raw names get a
"House " prefix before freezing).
"""
from __future__ import annotations

import json
import pathlib
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from caltools.model import Event

SOURCE = "legislature"
EVENTS_URL = "https://senate.texas.gov/events.php"
ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
ARCHIVE = ROOT / "data" / "legislature.json"
CENTRAL = ZoneInfo("America/Chicago")
HEARING_LENGTH = timedelta(hours=3)  # interim hearings routinely run long

# Zero events, or past-only events, is the normal state between hearing
# bursts — build.py skips the 0-events and no-future alarms when set.
SPORADIC = True

# Ian's committee watchlist (KB: Calendar Maintenance > Legislature).
# Matched case-insensitively against the committee cell text, so "Select
# Committee on ..." variants and session-to-session renames that keep the
# core phrase still match.
WATCH = [
    "business and commerce",
    "natural resources",
    "transportation",
    "local government",
    "water, agriculture",
]

HOUSE_URL = ("https://capitol.texas.gov/Committees/MeetingsUpcoming.aspx"
             "?Chamber=H")
# Ian's House watchlist (2026-08-14). Substring match like WATCH, so
# subcommittees of a watched committee ride along.
HOUSE_WATCH = [
    "transportation",
    "natural resources",
    "agriculture",       # "Agriculture & Livestock"
    "energy resources",
]

DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+(\d{1,2}),\s+(20\d{2})"
)
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AP])M", re.IGNORECASE)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"])}
# "Senate Committee on X" / "Senate Select Committee on X" -> "X"
CMTE_PREFIX_RE = re.compile(r"^Senate\s+(Select\s+)?Committee\s+on\s+", re.IGNORECASE)
CAPITOL_EXT_RE = re.compile(r"^Capitol Extension,\s*(\S+)$")

_problems: list[str] = []


def health_problems() -> list[str]:
    return list(_problems)


def _location(raw: str) -> str:
    m = CAPITOL_EXT_RE.match(raw.strip())
    if m:
        return (f"Room {m.group(1)}, Texas Capitol Extension, "
                "1100 Congress Ave., Austin, TX 78701")
    return raw.strip()


def parse_page(html: str) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    saw_table = False
    # class_ matches whole class tokens, so this selects table.cmte_events
    # but NOT table.cmte_events_mobile (whose rows duplicate every hearing).
    for table in soup.find_all("table", class_="cmte_events"):
        saw_table = True
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue  # header rows / "no events" colspan rows
            texts = [c.get_text(" ", strip=True) for c in cells]
            dm = DATE_RE.search(texts[0])
            tm = TIME_RE.search(texts[1])
            committee = texts[2]
            if not (dm and committee):
                continue
            low = committee.lower()
            if not any(w in low for w in WATCH):
                continue
            try:
                d = date(int(dm.group(3)), MONTHS[dm.group(1)], int(dm.group(2)))
            except ValueError:
                continue  # impossible date on one row shouldn't kill the source
            if tm:
                h = int(tm.group(1)) % 12 + (12 if tm.group(3).upper() == "P" else 0)
                start = datetime(d.year, d.month, d.day, h, int(tm.group(2)))
            else:
                start = d
            notice = row.find("a", href=re.compile(r"tlodocs", re.IGNORECASE))
            notice_url = notice["href"] if notice else EVENTS_URL
            note = texts[5].strip() if len(texts) > 5 else ""
            cancelled = ("cancelled" in (row.get("class") or [])
                         or "CANCELLED" in note.upper())
            # The notice URL is the event's url field — repeating it in the
            # body reads twice in calendar apps (Ian, 8/12).
            desc_bits = []
            if "REVISED" in note.upper():
                desc_bits.append("Notice was marked REVISED — recheck the link "
                                 "for changes.")
            desc_bits.append("Live/archived broadcasts: "
                             "https://senate.texas.gov/av-live.php")
            events.append(
                Event(
                    source=SOURCE,
                    summary=committee,  # raw name; UID freezes from this
                    start=start,
                    end=(start + HEARING_LENGTH)
                        if isinstance(start, datetime) else None,
                    location=_location(texts[3]),
                    url=notice_url,
                    status="CANCELLED" if cancelled else "CONFIRMED",
                    kind="hearing",
                    description="\n".join(desc_bits),
                )
            )
    if not saw_table:
        _problems.append(
            "legislature: no .cmte_events table on the Senate events page "
            "(page redesign?)"
        )
    return events


def finalize(events: list[Event]) -> list[Event]:
    """Freeze UIDs from the raw committee name, then apply display naming."""
    for ev in events:
        ev.uid = ev.stable_uid()
        short = CMTE_PREFIX_RE.sub("", ev.summary)
        ev.summary = f"Texas Senate: {short}"
    return events


def _house_location(raw: str) -> str:
    """Full address for Capitol Extension rooms; anything else stays raw
    (off-site hearings say e.g. "Weslaco, TX (See details below)" — the
    notice has the venue)."""
    raw = raw.strip()
    if re.match(r"^E\d\.\d{3}$", raw):
        return (f"Room {raw}, Texas Capitol Extension, "
                "1100 Congress Ave., Austin, TX 78701")
    return raw


def parse_house(html: str) -> list[Event]:
    """Watchlisted hearings from the House upcoming-meetings table."""
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    table = soup.find("table", class_="usa-table")
    if table is None:
        _problems.append(
            "legislature: no usa-table on the House upcoming-meetings page "
            "(page redesign?)")
        return events
    cur_day = cur_time = None
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        first = cells[0]
        text = first.get_text(" ", strip=True)
        if "sectionTitle" in (first.get("class") or []):
            dm = DATE_RE.search(text)
            try:
                cur_day = (date(int(dm.group(3)), MONTHS[dm.group(1)],
                                int(dm.group(2))) if dm else None)
            except ValueError:
                cur_day = None
            cur_time = None
            continue
        if first.get("data-label") == "Committee Meeting Time":
            tm = TIME_RE.search(text)
            cur_time = ((int(tm.group(1)) % 12
                         + (12 if tm.group(3).upper() == "P" else 0),
                         int(tm.group(2))) if tm else None)
            continue
        if cur_day is None or len(cells) < 3:
            continue
        # Committee row: "Name … Type: Public Hearing … Location: E2.012".
        name = text.split(" Type:")[0].strip()
        if not name:
            continue
        blob = (text + " " + cells[1].get_text(" ", strip=True)).lower()
        if not any(w in blob for w in HOUSE_WATCH):
            continue
        lm = re.search(r"Location:\s*(.+)$", text)
        start = (datetime(cur_day.year, cur_day.month, cur_day.day,
                          *cur_time) if cur_time else cur_day)
        notice = row.find("a", href=re.compile(r"tlodocs.*html", re.IGNORECASE))
        events.append(Event(
            source=SOURCE,
            summary=f"House {name}",  # chamber marker; UID freezes from this
            start=start,
            end=(start + HEARING_LENGTH)
                if isinstance(start, datetime) else None,
            location=_house_location(lm.group(1)) if lm else "",
            url=notice["href"] if notice else HOUSE_URL,
            kind="hearing",
        ))
    return events


def finalize_house(events: list[Event]) -> list[Event]:
    """Freeze UIDs from the chamber-marked raw name, then display-name."""
    for ev in events:
        ev.uid = ev.stable_uid()
        ev.summary = f"Texas House: {ev.summary.removeprefix('House ')}"
    return events


def _archived_past(fresh_uids: set[str], today: date) -> list[Event]:
    """Past events from our own snapshot that the page has since dropped.

    The snapshot build.py writes IS the archive: fetch() folds these back
    in, so each day's output accumulates history the source forgets.
    Strictly additive — past events only, fresh always wins on UID.
    """
    try:
        entries = json.loads(ARCHIVE.read_text())
    except Exception:
        return []  # first run: nothing to keep yet
    kept = []
    for d in entries:
        try:
            ev = Event.from_json(d)
        except Exception:
            continue  # one malformed archived entry shouldn't kill the rest
        day = ev.start.date() if isinstance(ev.start, datetime) else ev.start
        if day < today and ev.stable_uid() not in fresh_uids:
            kept.append(ev)
    return kept


# --- notice enrichment ------------------------------------------------------
# The tlodocs notice each hearing links (stable URL, ephemeral only in the
# sense that the EVENTS page forgets it) is Word-exported HTML with a
# regular shape: COMMITTEE/TIME & DATE/PLACE/CHAIR header, lead paragraphs,
# bold-led interim charges (with Symbol-bulleted bill items), then standing
# boilerplate from "Public testimony is limited". summarize_notice turns
# that into Ian's display format (2026-08-10): a numbered "Summarized
# Agenda" with bills abbreviated (SB/HB) and sessions as years, a
# "Testimony to include:" line of agency acronyms, and the bill
# descriptions at the bottom. Conservative: no charges parsed -> None, and
# the event keeps its unenriched description.
NOTICE_HEADER_RE = re.compile(
    r"^(COMMITTEE|TIME & DATE|PLACE|CHAIR|SENATE$|HOUSE OF REPRESENTATIVES$"
    r"|NOTICE OF PUBLIC HEARING|NOTICE OF FORMAL MEETING)",
    re.IGNORECASE)
# House notices close with an "Electronic public comment will be available
# for:" block — a bold lead plus a topic list that restates the charges.
# It reduces to its submission link (Ian, 2026-08-17: "we don't need to
# help people with anything beyond providing them the link").
ECOMMENT_RE = re.compile(r"^Electronic public comment", re.IGNORECASE)
# Off-site hearings add logistics paragraphs with bold leads that are NOT
# agenda items — the 24 Aug 2026 Weslaco NR notice put "Public Access to
# Meeting Location at:" and "Registration" into the digest (Ian, 2026-08-23).
BOILERPLATE_LEAD_RE = re.compile(
    r"^(Public Access|Registration\b|Parking\b)", re.IGNORECASE)
NOTICE_STOP_RE = re.compile(
    r"public testimony is limited|notice of assistance", re.IGNORECASE)
LEG_SESSION_RE = re.compile(r"\(?(\d{2,3})(?:st|nd|rd|th)\s+Legislature\)?,?\s*")
BILL_ABBR = [("Senate Joint Resolution", "SJR"), ("House Joint Resolution", "HJR"),
             ("Senate Bill", "SB"), ("House Bill", "HB")]
ACRONYM_DEF_RE = re.compile(r"([A-Z][A-Za-z&'’ ]{6,70}?)\s+\(([A-Z]{2,6})\)")
CHARGE_VERBS = ("Study", "Monitor", "Evaluate", "Assess", "Examine",
                "Review", "Identify", "Consider", "Explore")
NOTICE_FETCH_CAP = 8


def _session_year(n: int) -> int:
    """89th Legislature -> 2025 (regular sessions are odd years)."""
    return 2 * n + 1847


def _abbrev(text: str, acronyms: dict[str, str]) -> str:
    for full, short in BILL_ABBR:
        text = text.replace(full, short)
    for full, acro in acronyms.items():
        text = text.replace(full, acro)
    return LEG_SESSION_RE.sub(
        lambda m: f"{_session_year(int(m.group(1)))} session, ", text)


BILL_REF_RE = re.compile(
    r"(Senate Bill|House Bill|Senate Joint Resolution|House Joint Resolution"
    r"|SB|HB|SJR|HJR)\s+(\d+)")


def _denum(text: str) -> str:
    """Strip a leading '1. ' item number (charge numbering is layout)."""
    return re.sub(r"^\d{1,3}\.\s+", "", text)


def summarize_notice(html: bytes | str) -> str | None:
    # bytes, not a pre-decoded str: tlodocs serves UTF-8 Word HTML without a
    # charset header, so requests' .text guesses ISO-8859-1 and the Symbol
    # bullet "·" mojibakes to "Â·" (first live run, 10 Aug 2026 — every
    # ASCII part parsed, only the bullets vanished). BeautifulSoup reads the
    # meta charset itself when given bytes.
    soup = BeautifulSoup(html, "html.parser")
    full_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    # "Texas Commission on Environmental Quality (TCEQ)" -> {name: TCEQ},
    # harvested from the notice itself so substitutions never guess.
    acronyms = {m.group(1).strip(): m.group(2)
                for m in ACRONYM_DEF_RE.finditer(full_text)}
    charges: list[dict] = []   # {title, body, bills: [(label, desc)]}
    leads: list[str] = []
    for p in soup.find_all("p"):
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
        if not text or text == " " or NOTICE_HEADER_RE.match(text):
            continue
        if NOTICE_STOP_RE.search(text):
            break
        if ECOMMENT_RE.match(text):
            continue  # bold e-comment lead: link-only treatment (below)
        if BOILERPLATE_LEAD_RE.match(text):
            continue  # venue/registration logistics, not an agenda item
        b = p.find("b")
        lead = re.sub(r"\s+", " ", b.get_text(" ", strip=True)).strip() if b else ""
        # A bullet by markup (Word's Symbol/Wingdings marker span) or by
        # leading glyph — including the mojibake forms, belt-and-braces.
        sym = p.find("span", style=re.compile(r"Symbol|Wingdings", re.IGNORECASE))
        is_bullet = bool(sym) or text[:1] in "·•§o" or text[:2] == "Â·"
        if is_bullet:  # a bill item under the current charge
            if not charges:
                continue
            body = re.sub(r"^[·•§oÂ\s]+", "", text)
            bm = BILL_REF_RE.match(body)
            if not bm:
                continue  # decorative bullet, not a bill reference
            label = f"{_abbrev(bm.group(1), {})} {bm.group(2)}"
            # lstrip separators too: "SB 1169 - Relating to..." otherwise
            # glosses as "SB 1169: - Relating to...".
            desc = body[bm.end():].strip(" ,").lstrip(" ,:-–—")
            year = None
            ym = LEG_SESSION_RE.search(desc)
            if ym:
                year = _session_year(int(ym.group(1)))
                desc = LEG_SESSION_RE.sub("", desc, count=1).strip()
            charges[-1]["bills"].append((label, _abbrev(desc, acronyms), year))
        elif lead and _denum(text).startswith(lead):
            # Trailing separator glyphs are layout, not title ("Regional
            # Water Planning –" rendered as a dangling dash). _denum: some
            # notices NUMBER their charges ("1. **Removing Barriers...**:"
            # — Local Government, 2 Sep 2026), putting digits before the
            # bold lead; the number is layout too.
            body = _denum(text)
            charges.append({"title": lead.strip().rstrip(" :-–—"),
                            "body": body[len(lead):].lstrip(": ").strip(),
                            "bills": []})
        elif not charges and not text.endswith(":"):
            leads.append(text)   # e.g. the invited-testimony paragraph
    if not charges and not leads:
        return None
    # "Summary Agenda" is the standardized header across every source's
    # agenda digest (Ian, 2026-08-09).
    lines = ["Summary Agenda", ""]
    header_only = len(lines)  # a digest that never grows past this is noise
    for i, c in enumerate(charges, 1):
        if c["bills"]:
            labels = " and ".join(l for l, _, _ in c["bills"])
            years = {y for _, _, y in c["bills"] if y}
            suffix = (f" from the {sorted(years)[0]} session"
                      if len(years) == 1 else "")
            lines.append(f"{i}. {c['title']}: {labels}{suffix}")
        else:
            verb = c["body"].split(" ", 1)[0].rstrip(",.") if c["body"] else ""
            if verb in CHARGE_VERBS:
                lines.append(f'{i}. {verb}: "{c["title"]}"')
            else:
                lines.append(f"{i}. {c['title']}")
    testimony = next((t for t in leads if "testimony from" in t.lower()), None)
    if testimony:
        acros = [a for n, a in acronyms.items() if n in testimony]
        if acros:
            joined = (", ".join(acros[:-1]) + f", and {acros[-1]}"
                      if len(acros) > 1 else acros[0])
            lines += ["", f"Testimony to include: {joined}"]
        else:
            lines += ["", _abbrev(testimony, acronyms)]
    # Below the — rule: bill glosses, then links (Ian, 2026-08-18 — the
    # rule marks where the digest ends and reference material begins, so
    # the e-comment link lives at the bottom with any other links).
    if len(lines) == header_only:
        # No agenda items and no testimony line parsed: an empty "Summary
        # Agenda" header is worse than no digest (the 2 Sep 2026 Local
        # Government event shipped one) — leave the event's base body alone.
        return None
    ecomment = soup.find(
        "a", href=re.compile(r"comments\.house\.texas\.gov", re.IGNORECASE))
    bills = [(l, d) for c in charges for l, d, _ in c["bills"]]
    if bills or ecomment:
        lines += ["", "—"]
    for label, desc in bills:
        lines += ["", f"{label}: {desc}"]
    if ecomment:
        lines += ["", f"Electronic public comments: {ecomment['href']}"]
    return "\n".join(lines)


def enrich_notices(events: list[Event], get_html, today: date) -> None:
    """Prepend a summarized agenda to upcoming hearings, from each event's
    own notice URL — no matching, the link IS the declaration."""
    spent = 0
    for ev in events:
        day = ev.start.date() if isinstance(ev.start, datetime) else ev.start
        if (day < today or ev.status == "CANCELLED"
                or "tlodocs" not in ev.url or spent >= NOTICE_FETCH_CAP):
            continue
        spent += 1
        try:
            html = get_html(ev.url)
            block = summarize_notice(html) if html else None
        except Exception as exc:  # enrichment must never break the feed
            print(f"[{SOURCE}] WARNING: notice fetch/parse failed "
                  f"({ev.url}): {exc}")
            continue
        if block and "Summary Agenda" not in ev.description:
            # No dangling rule when there's nothing below it (House events
            # start with an empty description; Senate ones carry notes).
            ev.description = (f"{block}\n\n—\n\n{ev.description}"
                              if ev.description else block)


def fetch(session) -> list[Event]:
    _problems.clear()
    # (The old manual sleep-and-retry for senate.texas.gov refusals is
    # gone: build.py's session-level Retry armor covers it for every URL.)
    resp = session.get(EVENTS_URL, timeout=30)
    resp.raise_for_status()
    fresh = finalize(parse_page(resp.text))
    # One chamber failing shouldn't sink the other; a flagged problem
    # still turns the build red while the snapshot serves the gap.
    try:
        rh = session.get(HOUSE_URL, timeout=30)
        rh.raise_for_status()
        fresh += finalize_house(parse_house(rh.text))
    except Exception as exc:
        _problems.append(f"legislature: House meetings fetch failed: {exc}")
    today = datetime.now(CENTRAL).date()

    def _notice(url):
        r = session.get(url, timeout=30)
        r.raise_for_status()
        return r.content  # bytes: see the charset note on summarize_notice

    try:
        enrich_notices(fresh, _notice, today)
    except Exception as exc:  # upgrade-only: never sink the feed
        print(f"[{SOURCE}] WARNING: notice enrichment failed: {exc}")
    return fresh + _archived_past({e.uid for e in fresh}, today)


def fetch_offline() -> list[Event]:
    """Fixture-only (no archive read), so the parser check is deterministic."""
    _problems.clear()
    events = finalize(parse_page((FIXTURES / "legislature_events.html").read_text())) \
        + finalize_house(parse_house(
            (FIXTURES / "legislature_house.html").read_text()))
    # Notice-enrichment chain on the real 11 Aug 2026 Natural Resources
    # capture; other notices have no fixture and skip (None). today pinned
    # so that hearing is upcoming.
    notices = {
        "https://capitol.texas.gov/tlodocs/89R/schedules/html/"
        "C5802026081109001.HTM":
            FIXTURES / "legislature_notice_c580_20260811.html",
        # House shape: chamber header skip + e-comment link reduction.
        "https://capitol.texas.gov/tlodocs/89R/schedules/html/"
        "C0202026081811001.HTM":
            FIXTURES / "legislature_notice_c020_20260818.html",
    }
    enrich_notices(
        events,
        lambda u: notices[u].read_bytes() if u in notices else None,
        today=date(2026, 8, 1),
    )
    return events
