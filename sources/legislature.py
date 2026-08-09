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
forgets the hearing; it stays as each event's URL. House committees are
out of scope for now (Ian's watchlist is Senate-only).
"""
from __future__ import annotations

import json
import pathlib
import re
import time
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
            desc_bits = [f"Hearing notice (topics, testimony rules): {notice_url}"]
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
        ev.summary = f"Texas Senate - {short}"
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
    r"^(COMMITTEE|TIME & DATE|PLACE|CHAIR|SENATE$|NOTICE OF PUBLIC HEARING)",
    re.IGNORECASE)
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


def summarize_notice(html: str) -> str | None:
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
        b = p.find("b")
        lead = re.sub(r"\s+", " ", b.get_text(" ", strip=True)).strip() if b else ""
        if text.startswith("·"):  # Symbol bullet: a bill item
            if not (charges and lead):
                continue
            body = text.lstrip("· ").strip()
            desc = body[len(lead):].strip() if body.startswith(lead) else body
            year = None
            ym = LEG_SESSION_RE.search(desc)
            if ym:
                year = _session_year(int(ym.group(1)))
                desc = LEG_SESSION_RE.sub("", desc, count=1).strip()
            label = _abbrev(lead.strip(), {})
            charges[-1]["bills"].append((label, _abbrev(desc, acronyms), year))
        elif lead and text.startswith(lead):
            charges.append({"title": lead.rstrip(":").strip(),
                            "body": text[len(lead):].lstrip(": ").strip(),
                            "bills": []})
        elif not charges and not text.endswith(":"):
            leads.append(text)   # e.g. the invited-testimony paragraph
    if not charges and not leads:
        return None
    lines = ["Summarized Agenda", ""]
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
    bills = [(l, d) for c in charges for l, d, _ in c["bills"]]
    if bills:
        lines.append("")
        lines.append("—")
        for label, desc in bills:
            lines += ["", f"{label}: {desc}"]
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
        if block and "Summarized Agenda" not in ev.description:
            ev.description = f"{block}\n\n—\n\n{ev.description}"


def fetch(session) -> list[Event]:
    _problems.clear()
    # senate.texas.gov refuses connections intermittently (reds on 9 and
    # 10 Aug 2026); one retry after a pause clears the transient kind.
    # A persistent refusal still fails loud, and the snapshot backfill
    # keeps serving either way.
    try:
        resp = session.get(EVENTS_URL, timeout=30)
    except Exception:
        time.sleep(25)
        resp = session.get(EVENTS_URL, timeout=30)
    resp.raise_for_status()
    fresh = finalize(parse_page(resp.text))
    today = datetime.now(CENTRAL).date()

    def _html(url):
        r = session.get(url, timeout=30)
        r.raise_for_status()
        return r.text

    try:
        enrich_notices(fresh, _html, today)
    except Exception as exc:  # upgrade-only: never sink the feed
        print(f"[{SOURCE}] WARNING: notice enrichment failed: {exc}")
    return fresh + _archived_past({e.uid for e in fresh}, today)


def fetch_offline() -> list[Event]:
    """Fixture-only (no archive read), so the parser check is deterministic."""
    _problems.clear()
    events = finalize(parse_page((FIXTURES / "legislature_events.html").read_text()))
    # Notice-enrichment chain on the real 11 Aug 2026 Natural Resources
    # capture; other notices have no fixture and skip (None). today pinned
    # so that hearing is upcoming.
    notices = {
        "https://capitol.texas.gov/tlodocs/89R/schedules/html/"
        "C5802026081109001.HTM":
            FIXTURES / "legislature_notice_c580_20260811.html",
    }
    enrich_notices(
        events,
        lambda u: notices[u].read_text() if u in notices else None,
        today=date(2026, 8, 1),
    )
    return events
