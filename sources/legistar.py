"""Shared machinery for orgs on Granicus Legistar (InSite + Web API).

Observed on both CapMetro and Austin: the public Web API
(webapi.legistar.com/v1/{client}/events) only lists meetings once their
agenda is published, while the InSite calendar page (Calendar.aspx) shows
scheduled meetings earlier. Since early when/where is the point:

  1. scrape Calendar.aspx for the schedule (earliest dates/times/locations),
  2. query the API and merge in enrichment (agenda URL, detail page)
     for whichever meetings it already knows about.

Merge key = (body slug, date, start time) — the same identity rule as the
stable UID, so an event stays itself as it graduates from "scheduled" to
"agenda posted", and two same-day meetings of one body never collapse.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from caltools.model import Event, slugify

DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[AP]M$", re.IGNORECASE)
# InSite marks cancellations in the time column and/or appends to the name.
CANCEL_RE = re.compile(r"cancell?ed|postponed|deferred|rescheduled", re.IGNORECASE)
NAME_SUFFIX_RE = re.compile(
    r"\s*[-–—(]\s*(cancell?ed|postponed|deferred|rescheduled)\)?\s*$", re.IGNORECASE
)  # hyphen, en dash, em dash, or paren before the marker

# Calendar.aspx opens on a "This Month" filter, so a bare GET structurally
# cannot see a meeting scheduled for next month -- exactly the early notice
# this project exists for (observed 2026-07-28: CapMetro's page showed one
# record, its own last meeting, while the API had nothing upcoming either).
# The period is a Telerik RadComboBox driven by an ASP.NET postback rather
# than a query parameter, so widening it means replaying the form.
#
# "All Years" rather than "This Year": Legistar caps the grid at the 100
# most recent rows, so this self-bounds -- measured 2026-07-28 on capmetrotx
# as 100 rows spanning Feb 2024 to Dec 2026. "This Year" would empty out
# every 1 January and take the year's history with it; letting the source
# carry its own past is why this project needs no archive of its own.
# Agenda-item summaries (see agenda_block): at or below the threshold the
# items are listed outright; above it, a one-line count by matter type.
AGENDA_LIST_MAX = 6
AGENDA_TITLE_CHARS = 160
# Digest polish (Ian, 2026-08-26 committee mock): procedural filler drops
# entirely — minutes approvals, and the standing "identify items for
# future meetings" row (matter type "Future Items"). Items whose Legistar
# matter type is a briefing collapse into one "Briefings:" section — the
# header carries the word, so each line drops its "Briefing on" lead-in,
# its [presenter credit] bracket, and its (file, type) tag to leave just
# the topic.
PROCEDURAL_TYPE_RE = re.compile(r"\bminutes\b|\bfuture\s+items\b",
                                re.IGNORECASE)
BRIEFING_TYPE_RE = re.compile(r"\bbriefings?\b", re.IGNORECASE)
BRIEFING_LEAD_RE = re.compile(r"^Briefings?\s+(?:on|regarding|about)\s+",
                              re.IGNORECASE)
PRESENTER_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]")

CALENDAR_PERIOD = "All Years"
PERIOD_FIELD = "ctl00$ContentPlaceHolder1$lstYears"
PERIOD_STATE_FIELD = "ctl00_ContentPlaceHolder1_lstYears_ClientState"


def parse_time(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip().upper(), "%I:%M %p")
    except ValueError:
        return None


def _time_key(ev: Event) -> str:
    return ev.start.strftime("%H%M") if isinstance(ev.start, datetime) else ""


def agenda_block(items: list[dict]) -> str:
    """Human summary of an event's agenda items for the description.

    Real agenda items carry EventItemAgendaNumber; rows without one are
    boilerplate (public-comment notice, closed-session notice, section
    headers) and are ignored. At or below AGENDA_LIST_MAX items, list them
    with file number and type — per Ian, the useful bit is what's actually
    being decided. Above it, a bulleted count by matter type keeps the
    description readable ("- 31 consent items"). Either way the block opens
    with "Summary Agenda", the standardized header across every source's
    agenda digest (Ian, 2026-08-09).
    """
    def _is_header(i: dict) -> bool:
        # Section-header rows ("A. Pre-Selected Agenda Items") carry a bare
        # LETTER as their agenda number and no file/matter type — layout,
        # not items (the 25 Aug 2026 work session counted them as "5 other
        # items"). Real items always have a digit ("2.", "B1."), so a row
        # is dropped only when BOTH signals agree: an item that merely
        # lacks a file number is still an item (Ian, 2026-09-02).
        num = str(i.get("EventItemAgendaNumber") or "")
        return (not re.search(r"\d", num)
                and not (i.get("EventItemMatterFile")
                         or i.get("EventItemMatterType")))

    numbered = [i for i in items if i.get("EventItemAgendaNumber")
                and not _is_header(i)
                and not PROCEDURAL_TYPE_RE.search(
                    i.get("EventItemMatterType") or "")]
    if not numbered:
        return ""

    def _cap(t: str) -> str:
        return (t[:AGENDA_TITLE_CHARS - 1].rstrip() + "\u2026"
                if len(t) > AGENDA_TITLE_CHARS else t)

    if len(numbered) <= AGENDA_LIST_MAX:
        # Agenda order preserved; all briefings merge into one "Briefings:"
        # block sitting where the first briefing appeared. Plain items stay
        # contiguous lines; the briefings block gets blank-line breathing
        # room on both sides.
        chunks: list[list] = []      # ["item", lines...] / ["briefings", ...]
        briefings: list[str] = None
        for i in numbered:
            title = PRESENTER_BRACKET_RE.sub(
                "", " ".join((i.get("EventItemTitle") or "").split())).strip()
            if BRIEFING_TYPE_RE.search(i.get("EventItemMatterType") or ""):
                t = BRIEFING_LEAD_RE.sub("", title).strip(" .")
                if briefings is None:
                    briefings = ["Briefings:"]
                    chunks.append(briefings)
                # Lettered agenda numbers (B1, B2 — council style) are how
                # the posted agenda refers to the item, so keep them as the
                # line label; bare ordinals add nothing under the section
                # header and become plain bullets (committee style).
                num = str(i.get("EventItemAgendaNumber") or "") \
                    .strip().rstrip(".")
                label = f"{num}: " if re.search(r"[A-Za-z]", num) else "- "
                briefings.append(label + _cap(t[:1].upper() + t[1:]))
                continue
            tags = ", ".join(x for x in
                             [i.get("EventItemMatterFile"),
                              i.get("EventItemMatterType")] if x)
            num = str(i["EventItemAgendaNumber"]).strip().rstrip(".")
            line = f"{num}. {_cap(title)}" + (f" ({tags})" if tags else "")
            if chunks and chunks[-1][0] == "item":
                chunks[-1].append(line)
            else:
                chunks.append(["item", line])
        return "Summary Agenda\n\n" + "\n\n".join(
            "\n".join(c[1:] if c[0] == "item" else c) for c in chunks)
    counts: dict[str, int] = {}
    for i in numbered:
        t = i.get("EventItemMatterType") or "other"
        counts[t] = counts.get(t, 0) + 1
    def _bullet(t: str, n: int) -> str:
        t = t.lower()
        # Matter types like "Action Item" already end in the word — don't
        # emit "action item items"; just pluralize what's there. Types
        # STARTING with it ("Item from Council") pluralize in place:
        # "10 items from council", not "10 item from council items".
        if t.startswith("item "):
            label = t.replace("item", "items", 1) if n != 1 else t
            return f"- {n} {label}"
        label = t if t.endswith("item") else f"{t} item"
        return f"- {n} {label}{'' if n == 1 else 's'}"
    return "Summary Agenda\n\n" + "\n".join(
        _bullet(t, n) for t, n in
        sorted(counts.items(), key=lambda kv: -kv[1]))


@dataclass
class Legistar:
    source: str          # adapter key, e.g. "capmetro"
    client: str          # Web API client slug, e.g. "capmetrotx"
    host: str            # InSite host, e.g. "capmetrotx.legistar.com"
    prefix: str          # display prefix, e.g. "CapMetro"; "" = no prefix
    meeting_length: timedelta = timedelta(hours=2)
    # Display renames applied AFTER the UID is frozen (raw body name is the
    # identity; these are presentation only), e.g.
    # {"Budget Meeting of the Austin City Council": "City Council: Budget Meeting"}
    display_names: dict = None
    # Location cleanups: if a location starts with a key, replace it with the
    # canonical value (sources often publish just a room name or junk-suffixed
    # venue), e.g. {"Rosa Parks Boardroom": "Rosa Parks Boardroom, ..."}
    location_fixes: dict = None
    # Optional display rename RULE (str -> str), applied after display_names —
    # for patterns rather than exact names (e.g. Austin: any body ending in
    # "Committee" is a council committee -> "City Council: <name>").
    display_transform: object = None
    # Optional predicate (raw body name -> bool): which bodies get their
    # agenda ITEMS fetched and summarized into the description (one extra
    # API call per matching future event with a published agenda).
    agenda_detail: object = None

    @property
    def calendar_url(self) -> str:
        return f"https://{self.host}/Calendar.aspx"

    @property
    def api_url(self) -> str:
        return f"https://webapi.legistar.com/v1/{self.client}/events"

    def parse_calendar_html(self, html: str) -> list[Event]:
        """Pull meeting rows out of the InSite calendar page.

        Deliberately tolerant: rather than binding to Legistar's control IDs,
        we scan every table row for a date-shaped cell, then read name / time
        / location relative to it. Survives cosmetic template changes; the
        health check in build.py catches a redesign beyond recognition.
        """
        soup = BeautifulSoup(html, "html.parser")
        events: list[Event] = []
        for row in soup.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if len(cells) < 3:
                continue
            date_idx = next((i for i, c in enumerate(cells) if DATE_RE.match(c)), None)
            if date_idx is None or date_idx == 0:
                continue
            raw_name = cells[0]
            if not raw_name:
                continue
            # Strip cancellation markers from the name so the event's identity
            # (UID) survives a rename like "Board of Directors - CANCELLED".
            name = NAME_SUFFIX_RE.sub("", raw_name).strip()
            status = "CANCELLED" if CANCEL_RE.search(raw_name) else "CONFIRMED"

            try:
                meeting_date = datetime.strptime(cells[date_idx], "%m/%d/%Y")
            except ValueError:
                continue  # impossible date on one row shouldn't kill the source
            time_idx, tval = next(
                ((i, parse_time(c)) for i, c in enumerate(cells[date_idx + 1:], date_idx + 1)
                 if TIME_RE.match(c.replace("\xa0", " ").strip())),
                (None, None),
            )
            if tval is not None:
                start = meeting_date.replace(hour=tval.hour, minute=tval.minute)
                end = start + self.meeting_length
            else:
                start, end = meeting_date.date(), None
                # No parseable time — the time column may say why.
                trailing = " ".join(cells[date_idx + 1:])
                if CANCEL_RE.search(trailing):
                    status = "CANCELLED"
            location = ""
            loc_idx = time_idx if time_idx is not None else date_idx + 1
            if loc_idx + 1 < len(cells):
                location = re.sub(r"\s{2,}", " ", cells[loc_idx + 1])[:200]
                if CANCEL_RE.fullmatch(location.strip()):
                    location = ""
            detail_url = ""
            link = row.find("a", href=re.compile(r"MeetingDetail", re.IGNORECASE))
            if link and link.get("href"):
                detail_url = f"https://{self.host}/" + link["href"].lstrip("/")
            events.append(
                Event(
                    source=self.source,
                    summary=name,
                    start=start,
                    end=end,
                    location=location,
                    url=detail_url or self.calendar_url,
                    status=status,
                )
            )
        return events

    def api_events(self, session, since: datetime) -> list[dict]:
        params = {
            "$filter": f"EventDate ge datetime'{since.strftime('%Y-%m-%d')}'",
            "$orderby": "EventDate",
        }
        resp = session.get(self.api_url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def merge_api(self, events: list[Event], api_rows: list[dict],
                  item_fetcher=None) -> list[Event]:
        """Enrich scraped events with API data; add API-only events.

        Match on (body slug, date, time); if the times disagree but the body
        has exactly one meeting that day, fall back to that one — a
        reschedule shouldn't produce a phantom duplicate.
        """
        by_key: dict[tuple[str, str, str], Event] = {}
        for e in events:
            k = (slugify(e.summary), e.start.strftime("%Y%m%d"), _time_key(e))
            prev = by_key.get(k)
            # Same body/day/time scraped twice (e.g. a cancelled posting plus
            # a re-posted one): prefer the CONFIRMED row — the meeting is
            # happening in some form. Ties keep the first, matching emit()'s
            # first-wins dedupe so both layers collapse pairs the same way.
            if prev is None or (prev.status == "CANCELLED" and e.status != "CANCELLED"):
                by_key[k] = e

        def _enrich(ev: Event, insite: str, agenda: str, row: dict) -> None:
            if insite:
                ev.url = insite
            if agenda and agenda not in ev.description:
                ev.description = (ev.description + f"\nAgenda: {agenda}").strip()
            # Agenda-item summary: the fetcher decides which rows merit a
            # per-event API call (live: future + agenda-published + capped;
            # offline: fixture lookup) and returns None to decline.
            # Not gated on the agenda FILE: Legistar serves meeting items
            # through the API before "Published agenda" flips from "Not
            # available" (the 12/14 Aug 2026 budget meetings each showed 24
            # items with no published file — Ian, 2026-08-09). The fetcher
            # still gates on body/date/cap.
            if item_fetcher and "Summary Agenda" not in ev.description:
                try:
                    items = item_fetcher(row)
                except Exception as exc:
                    print(f"[{self.source}] WARNING: agenda items fetch "
                          f"failed for event {row.get('EventId')}: {exc}")
                    items = None
                if items:
                    block = agenda_block(items)
                    if block:
                        # Standardized shape (Ian, 2026-08-09): summary on
                        # top, then a — rule, then the link lines that were
                        # already in the description.
                        links = ev.description.strip()
                        ev.description = block + (
                            f"\n\n—\n\n{links}" if links else "")

        for row in api_rows:
            d = (row.get("EventDate") or "")[:10].replace("-", "")
            slug = slugify(row.get("EventBodyName") or "")
            t = parse_time(row.get("EventTime") or "")
            tkey = t.strftime("%H%M") if t else ""
            agenda = row.get("EventAgendaFile") or ""
            insite = row.get("EventInSiteURL") or ""

            if (slug, d, tkey) in by_key:
                _enrich(by_key[(slug, d, tkey)], insite, agenda, row)
                continue
            same_day = [e for (s, dd, _), e in by_key.items() if s == slug and dd == d]
            if len(same_day) == 1:
                _enrich(same_day[0], insite, agenda, row)
                continue

            try:
                base = datetime.strptime((row.get("EventDate") or "")[:10], "%Y-%m-%d")
            except ValueError:
                continue
            start = base.replace(hour=t.hour, minute=t.minute) if t else base.date()
            ev = Event(
                source=self.source,
                summary=row.get("EventBodyName") or "Meeting",
                start=start,
                end=(start + self.meeting_length) if isinstance(start, datetime) else None,
                location=row.get("EventLocation") or "",
                url=insite or self.calendar_url,
                description=f"Agenda: {agenda}" if agenda else "",
            )
            by_key[(slug, d, tkey)] = ev
        return list(by_key.values())

    def finalize(self, events: list[Event]) -> list[Event]:
        """Freeze UIDs from the raw body name, then apply display polish."""
        for ev in events:
            ev.uid = ev.stable_uid()
            low = ev.summary.lower()
            if "work session" in low:
                ev.kind = "work-session"
            elif "budget" in low:
                ev.kind = "budget"
            elif "special called" in low:
                ev.kind = "special"
            if self.display_names:
                ev.summary = self.display_names.get(ev.summary, ev.summary)
            if self.display_transform:
                ev.summary = self.display_transform(ev.summary)
            if self.prefix and not ev.summary.startswith(self.prefix):
                ev.summary = f"{self.prefix}: {ev.summary}"
            if self.location_fixes:
                for pfx, canonical in self.location_fixes.items():
                    if ev.location.startswith(pfx):
                        ev.location = canonical
                        break
        return events

    def widen_calendar_html(self, session, html: str) -> str | None:
        """Replay Calendar.aspx as a postback with the period set to a year.

        Returns the widened page, or None when the form is not shaped the
        way we expect or the postback fails -- callers keep the default
        view, so the worst case is exactly the behaviour we had before.
        """
        if PERIOD_FIELD not in html:
            return None  # not the Legistar template we know; leave it alone
        soup = BeautifulSoup(html, "html.parser")
        form = {
            i["name"]: i.get("value", "")
            for i in soup.find_all("input", {"type": "hidden"})
            if i.get("name")
        }
        if "__VIEWSTATE" not in form:
            return None
        form["__EVENTTARGET"] = PERIOD_FIELD
        form["__EVENTARGUMENT"] = ""
        form[PERIOD_FIELD] = CALENDAR_PERIOD
        # The combo posts its own widget state alongside the plain value;
        # the server reads "text", so both have to agree.
        form[PERIOD_STATE_FIELD] = json.dumps({
            "logEntries": [], "value": "", "text": CALENDAR_PERIOD,
            "enabled": True, "checkedIndices": [],
            "checkedItemsTextOverflows": False,
        })
        try:
            resp = session.post(self.calendar_url, data=form, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[{self.source}] WARNING: calendar widen failed: {exc}")
            return None
        return resp.text

    def fetch(self, session) -> list[Event]:
        resp = session.get(self.calendar_url, timeout=30)
        resp.raise_for_status()
        events = self.parse_calendar_html(resp.text)
        # Try for the whole year, but never accept a view that sees less
        # than the default did -- a failed postback often still renders a
        # valid-looking page, and silently shrinking coverage is worse than
        # not widening at all.
        widened = self.widen_calendar_html(session, resp.text)
        if widened is not None:
            wider = self.parse_calendar_html(widened)
            if len(wider) >= len(events):
                events = wider
            else:
                print(f"[{self.source}] WARNING: {CALENDAR_PERIOD} view "
                      f"returned {len(wider)} rows vs {len(events)} for the "
                      "default view; keeping the default")
        since = datetime.now() - timedelta(days=90)
        try:
            rows = self.api_events(session, since)
        except Exception as exc:  # API enrichment is best-effort
            print(f"[{self.source}] WARNING: API enrichment failed: {exc}")
            rows = []
        fetcher = None
        if self.agenda_detail:
            today = datetime.now().strftime("%Y-%m-%d")
            spent = {"n": 0}

            def fetcher(row):
                # The fetcher gates; merge_api just calls it. Past meetings
                # and non-matching bodies cost nothing; the cap bounds API
                # load however many agendas publish at once.
                if not self.agenda_detail(row.get("EventBodyName") or ""):
                    return None
                if (row.get("EventDate") or "")[:10] < today:
                    return None
                if spent["n"] >= 15 or not row.get("EventId"):
                    return None
                spent["n"] += 1
                r = session.get(
                    f"{self.api_url}/{row['EventId']}/eventitems", timeout=30)
                r.raise_for_status()
                return r.json()
        return self.finalize(self.merge_api(events, rows, item_fetcher=fetcher))
