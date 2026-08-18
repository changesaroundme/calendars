"""CAMPO adapter — ingest their published Tribe Events iCal feed.

CAMPO (Capital Area Metropolitan Planning Organization) already publishes a
subscribable calendar; this adapter re-ingests it so CAMPO events flow into
the combined calendar with everything else, normalized to our event model.

Source: https://www.campotexas.org/?post_type=tribe_events&ical=1&eventDisplay=list
"""
from __future__ import annotations

import pathlib
import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup
from icalendar import Calendar

from caltools.model import Event

FEED_URL = "https://www.campotexas.org/?post_type=tribe_events&ical=1&eventDisplay=list"
SOURCE = "campo"
FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"

# CAMPO marks cancellations in the title ("... - Cancelled") rather than with
# STATUS; translate to a real STATUS while keeping their title text intact.
CANCEL_RE = re.compile(r"cancell?ed|postponed", re.IGNORECASE)


def parse_feed(ics_data: bytes | str) -> list[Event]:
    cal = Calendar.from_ical(ics_data)
    events: list[Event] = []
    for component in cal.walk("VEVENT"):
        summary = str(component.get("SUMMARY", "")).strip()
        dtstart = component.get("DTSTART").dt if component.get("DTSTART") else None
        dtend = component.get("DTEND").dt if component.get("DTEND") else None
        if dtstart is None or not summary:
            continue
        events.append(
            Event(
                source=SOURCE,
                # Org prefix is display-only; identity comes from CAMPO's UID.
                summary=f"CAMPO: {summary}",
                start=dtstart,
                end=dtend,
                # Their feed's TAC entries carry a junk "TX" location; the
                # real venue (per KB Organizations/Overview) is CAMPO's
                # offices. TPB entries already carry a full address.
                location=(
                    "CAMPO Offices, 5330 Bluffstone Ln, Austin, TX 78759"
                    if str(component.get("LOCATION", "")).strip() in ("", "TX")
                    else str(component.get("LOCATION", "")).strip()
                ),
                url=str(component.get("URL", "")).strip(),
                status="CANCELLED" if CANCEL_RE.search(summary) else "CONFIRMED",
                kind="hearing" if "hearing" in summary.lower() else "regular",
                # Keep CAMPO's own UID: their feed is the system of record,
                # and reusing it means someone subscribed to both feeds sees
                # one event, not two.
                uid=str(component.get("UID", "")).strip(),
            )
        )
    return events


# --- meeting-packet enrichment ---------------------------------------------
# CAMPO posts agenda packets to a server-rendered archive at AGENDAS_URL,
# month-addressable via plain query params. Each entry's title DECLARES its
# own body and date ("Transportation Policy Board – 8.10.2026"), so the
# match rule needs no inference: attach a packet to an event only when the
# packet's own date equals the event's date AND the packet's body name is
# contained in the event summary. Upgrade-only: any failure or non-match
# leaves the event exactly as the feed built it.
AGENDAS_URL = "https://www.campotexas.org/resource-category/meeting-agendas/"
AGENDA_HORIZON = timedelta(days=45)
TITLE_RE = re.compile(r"^(.*?)\s*[–—-]\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$")


def _day(e: Event) -> date:
    return e.start.date() if isinstance(e.start, datetime) else e.start


def parse_agenda_archive(html: str) -> list[tuple[str, date, str]]:
    """(body, date, packet_url) triples from one archive month page."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, date, str]] = []
    for h3 in soup.select("#archive-list h3.post-title"):
        m = TITLE_RE.match(h3.get_text(" ", strip=True))
        if not m:
            continue
        try:
            d = date(int(m.group(4)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        section = h3.find_parent("section")
        a = section.select_one("ul.resource-links a[href]") if section else None
        if a:
            out.append((m.group(1).strip(), d, a["href"]))
    return out


# The Executive Director's memo opens every TPB packet with per-item
# bullets ("• Item 6 – Fiscal Agent Agreement: ..."); those short titles
# are the packet's own editorial cut of what's substantive, so they're the
# preferred item list (the numbering gaps are inherent — standing items
# aren't summarized). Fallback for packets without a memo: the formal
# AGENDA's numbered list, minus standing items.
MEMO_ITEM_RE = re.compile(r"Item\s+(\d{1,2})\s*[–—-]\s*(.+?)\s*[:–—](?!\d)")
AGENDA_ITEM_RE = re.compile(r"^(\d{1,2})\.\s+(.+)$", re.MULTILINE)
STANDING_RE = re.compile(
    r"quorum|public comments|meeting minutes|announcements|adjourn",
    re.IGNORECASE)
PACKET_PAGES = 6          # memo + formal agenda live up front
PACKET_ITEM_CAP = 12
PACKET_FETCH_CAP = 3      # PDFs per run — a packet is ~10MB


def packet_items(pdf_bytes: bytes) -> tuple[str, list[str]]:
    """(label, numbered item lines) from a packet's opening pages."""
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join((p.extract_text() or "")
                         for p in pdf.pages[:PACKET_PAGES])
    memo = [f"{n}. {t}" for n, t in MEMO_ITEM_RE.findall(text)]
    if len(memo) >= 2:
        # Attribution reads as a parenthetical under the "Summary Agenda"
        # header (Ian's mock, 2026-08-09); the numbering gaps speak for
        # themselves once the source is named.
        return ("From the packet's executive summary", memo[:PACKET_ITEM_CAP])
    items: list[str] = []
    for m in AGENDA_ITEM_RE.finditer(text):
        title = re.sub(r"[.…]{2,}.*$", "", m.group(2)).strip().rstrip(".")
        if int(m.group(1)) != len(items) + 1:
            continue  # strictly sequential = the agenda numbering, not tables
        items.append(title)
        if title.lower().startswith("adjourn"):
            break
    kept = [f"{i + 1}. {t}" for i, t in enumerate(items)
            if not STANDING_RE.search(t)]
    return ("From the agenda; standing items omitted",
            kept[:PACKET_ITEM_CAP])


def enrich_agendas(events: list[Event], get_html, today: date,
                   get_bytes=None) -> None:
    """Attach posted meeting packets (and their items) to upcoming events."""
    horizon = today + AGENDA_HORIZON
    upcoming = [e for e in events
                if e.status != "CANCELLED" and today <= _day(e) <= horizon]
    if not upcoming:
        return
    entries: list[tuple[str, date, str]] = []
    for yr, mo in sorted({(_day(e).year, _day(e).month) for e in upcoming}):
        url = f"{AGENDAS_URL}?resource_year={yr}&resource_month={mo:02d}"
        try:
            entries += parse_agenda_archive(get_html(url))
        except Exception as exc:  # enrichment must never break the feed
            print(f"[{SOURCE}] WARNING: agenda archive fetch failed "
                  f"({url}): {exc}")
    spent = 0
    for ev in upcoming:
        for body, d, pdf in entries:
            if d != _day(ev) or body.lower() not in ev.summary.lower():
                continue
            link_line = f"Meeting packet: {pdf}"
            summary = None
            if get_bytes is not None and spent < PACKET_FETCH_CAP:
                spent += 1
                try:
                    label, items = packet_items(get_bytes(pdf))
                    if items:
                        summary = (f"Summary Agenda\n({label})\n\n"
                                   + "\n".join(items))
                except Exception as exc:
                    print(f"[{SOURCE}] WARNING: packet parse failed "
                          f"({pdf}): {exc}")
            # Standardized shape (Ian, 2026-08-09): summary on top, then a
            # — rule, then the packet link.
            block = (f"{summary}\n\n—\n\n{link_line}" if summary
                     else link_line)
            if link_line not in ev.description:
                ev.description = (
                    f"{ev.description}\n\n{block}" if ev.description
                    else block)
            break


def fetch(session) -> list[Event]:
    resp = session.get(FEED_URL, timeout=30)
    resp.raise_for_status()
    # Bytes, not resp.text: requests guesses ISO-8859-1 when the server omits
    # a charset, which would mojibake UTF-8; icalendar handles bytes cleanly.
    events = parse_feed(resp.content)

    def _html(url):
        r = session.get(url, timeout=30)
        r.raise_for_status()
        return r.text

    def _bytes(url):
        r = session.get(url, timeout=60)  # packets run ~10MB
        r.raise_for_status()
        return r.content

    try:
        enrich_agendas(events, _html, datetime.now().date(), get_bytes=_bytes)
    except Exception as exc:  # upgrade-only: never sink the feed
        print(f"[{SOURCE}] WARNING: agenda enrichment failed: {exc}")
    return events


def fetch_offline() -> list[Event]:
    """Build from fixtures/ (no network) — the --offline contract."""
    events = parse_feed((FIXTURES / "campo.ics").read_text())
    # Enrichment chain on real captures; today pinned so the 10 Aug 2026
    # TPB meeting is upcoming. The packet fixture is the real packet's
    # first 6 pages (the full document is ~11MB; the memo and formal
    # agenda both live up front).
    enrich_agendas(
        events,
        lambda url: (FIXTURES / "campo_agendas.html").read_text(),
        today=date(2026, 8, 1),
        get_bytes=lambda url: (
            FIXTURES / "campo_packet_tpb_20260810.pdf").read_bytes(),
    )
    return events
