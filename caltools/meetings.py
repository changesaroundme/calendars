"""Which meetings name which project, and which bodies' agendas we can read.

A meeting names a project when its title, one of its agenda items, or the rest
of its description contains one of the project's names: `name`, `also_called`
and `match` in projects.yaml. Nothing is guessed and nothing is fetched. The
same events and the same list always give the same answer, and every answer
can be changed by editing one line of projects.yaml.

How wording is compared:
  - whole words only: "290 Extension" is not found in "1290 Extensions"
  - capital letters, accents, and the kind of dash, apostrophe or space make
    no difference, and "&" is the same as "and"
  - except that a name written all in capitals (UTP) has to be in capitals in
    the text, so an acronym is never found inside ordinary words
  - a mention inside a `wrong_matches` wording does not count: with "Statewide
    Transportation Improvement Program" listed for a project, that phrase is
    no longer a mention of its "Transportation Improvement Program"

Agenda items come from the digest the adapters write into a description: the
block that opens with the line "Summary Agenda" and runs to the next "—"
line. agenda_items() reads every layout the sources use, so a match can quote
the item that names the project, with its number when it has one. A
description with no digest is searched as it stands.

Nothing here changes an event. Tied.published() hands the calendars copies
that carry one more line, "Changes Around Me follows: ..."; data/*.json keeps
what the source said. The line names the project and does not link to it:
there is no public page for a project yet that is certain to be there.
"""
from __future__ import annotations

import json
import pathlib
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime

from .model import Event

DIGEST = "Summary Agenda"   # the line every adapter opens an agenda digest with
RULE = "—"                  # the line that ends it
FOLLOWS = "Changes Around Me follows: "   # opens the line a calendar entry gains
QUOTE_MAX = 160             # characters of an agenda item quoted on the page: the adapters' own cap
ITEMS_MAX = 2               # agenda items quoted for one meeting
UPCOMING_MAX = 12           # meetings listed under a project before "and N more"
PAST_MAX = 5
BODY_MIN = 3                # a body with fewer meetings is counted with the others


# ───────────────────────────── agenda items ─────────────────────────────

@dataclass(frozen=True)
class Item:
    """One agenda item from a digest. `number` is how the agenda refers to it
    ("5", "B1", "A21"), or "" where the digest does not number its items."""
    number: str
    text: str


_NUMBERED = [
    re.compile(r'(?:.*:\s+)?Item #(\d+)\s+"(.+)"'),   # Item #3 "Discussion and ..."      City boards
    re.compile(r"#(\d+):\s+(.+)"),                    # #5: An update on ...             City boards
    re.compile(r"([A-Z]?\d{1,3})\.\s+(.+)"),          # 7. TIP and RTP Amendments        CAMPO, Legislature, Council
    re.compile(r"([A-Z]\d{1,3}):\s+(.+)"),            # B1: The current status of ...    Council briefings
]
_UNNUMBERED = [
    re.compile(r"\d{1,2}:\d{2}\s+(.+)"),              # 10:40 NHTSA Assessment Update    TPSC (a time, not a number)
    re.compile(r"[-•]\s+(.+)"),                       # - CapMetro Budget Presentation   ATP, CapMetro, PUC, briefings
]
_TALLY = re.compile(r"\d+ [a-z .]*\bitems?\b[a-z .]*")   # "- 12 consent items" counts items; it is not one
# Headings inside a digest ("Action Items:", "Briefings:", "3 discussion
# items:") are not items. They stay with the rest of the description.


def _item(line: str) -> Item | None:
    for form in _NUMBERED:
        m = form.fullmatch(line)
        if m:
            return Item(m[1], m[2].strip())
    for form in _UNNUMBERED:
        m = form.fullmatch(line)
        if m and not _TALLY.fullmatch(m[1]):
            return Item("", m[1].strip())
    return None


def _read(description: str) -> tuple[list[Item], list[str]]:
    """A description as (agenda items, everything else as paragraphs). Lines
    of a digest that are not items — headings such as "Briefings:", counts
    such as "9 discussion items", notes and links — stay in the paragraphs,
    so no wording is lost to the search."""
    items: list[Item] = []
    rest: list[str] = []
    paragraph: list[str] = []
    in_digest = False

    def close():
        if paragraph:
            rest.append("\n".join(paragraph))
            paragraph.clear()

    for raw in (description or "").splitlines():
        line = raw.strip()
        if line in (DIGEST, RULE):
            in_digest = line == DIGEST
            close()
        elif not line:
            close()
        elif in_digest and (item := _item(line)):
            close()
            items.append(item)
        else:
            paragraph.append(line)
    close()
    return items, rest


def has_digest(description: str) -> bool:
    return any(line.strip() == DIGEST for line in (description or "").splitlines())


def agenda_items(description: str) -> list[Item]:
    """The items of a description's agenda digest, in agenda order; [] when it
    has no digest or the digest only counts items ("- 68 consent items")."""
    return _read(description)[0]


# ───────────────────────────── comparing wording ─────────────────────────────

_SAME = {**dict.fromkeys("‐‑‒–—―−", "-"), **dict.fromkeys("‘’", "'"), **dict.fromkeys("“”", '"')}
_SPACE = re.compile(r"\s")


def _fold_character(ch: str) -> str:
    if ch.isspace():
        return " "
    if ch in _SAME:
        return _SAME[ch]
    plain = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    return plain if len(plain) == 1 else ch


def fold(text: str) -> str:
    """The text with the differences that do not matter removed: accents, the
    kind of dash or quote mark, the kind of white space. One character for
    one, so a position in the result is the same position in the text."""
    if text.isascii():
        return _SPACE.sub(" ", text)
    return "".join(ch if ch.isascii() and not ch.isspace() else _fold_character(ch) for ch in text)


def phrase_key(phrase: str) -> str:
    """What two phrases have in common when they would find the same text."""
    return " ".join("and" if word == "&" else word for word in fold(phrase).lower().split())


def _alternative(phrase: str) -> str:
    words = ["(?:(?i:and)|&)" if word.lower() in ("and", "&") else re.escape(word)
             for word in fold(phrase).split()]
    body = " +".join(words)
    return body if phrase.isupper() else f"(?i:{body})"


def _pattern(phrases: list[str]) -> re.Pattern | None:
    """One expression finding any of the phrases as whole words; the group
    that matched says which phrase it was."""
    if not phrases:
        return None
    return re.compile(r"(?<![^\W_])(?:" + "|".join(f"({_alternative(p)})" for p in phrases) + r")(?![^\W_])")


def excerpt(text: str, start: int, end: int, limit: int = QUOTE_MAX) -> str:
    """The text if it is short, otherwise up to `limit` characters of it that
    keep text[start:end] in view, cut between words, with "…" where it was cut."""
    if len(text) <= limit:
        return text
    lo = 0 if end <= limit else max(0, min(start - (limit - (end - start)) // 2, len(text) - limit, start))
    hi = min(len(text), max(lo + limit, end))
    if lo > 0:
        lo = text.find(" ", lo, start) + 1 or start
    if hi < len(text):
        cut = text.rfind(" ", end, hi + 1)
        hi = cut if cut > 0 else hi
    kept = text[lo:hi].strip(" ,;:")
    return ("…" if lo else "") + (kept.rstrip("… ,;:") + "…" if hi < len(text) else kept)


_MARKER = re.compile(r"(?:[-•]|\d{1,3}\.)\s+")


def _line_excerpt(paragraph: str, start: int, end: int) -> str:
    """The line of a paragraph that holds paragraph[start:end], without its
    list marker, cut to length. (A name can run over a line break in hand-
    wrapped text; then it is the lines it touches.)"""
    lo = paragraph.rfind("\n", 0, start) + 1
    hi = paragraph.find("\n", end)
    line = paragraph[lo:hi if hi >= 0 else len(paragraph)].replace("\n", " ")
    start, end = start - lo, end - lo
    marker = _MARKER.match(line)
    if marker and marker.end() <= start:
        line, start, end = line[marker.end():], start - marker.end(), end - marker.end()
    return excerpt(line, start, end)


# ───────────────────────────── finding mentions ─────────────────────────────

@dataclass(frozen=True)
class Mention:
    """One meeting that names one project."""
    project: dict
    event: Event
    phrase: str               # the name that was found (the first, if several were)
    items: tuple[Item, ...]   # the agenda items naming the project, cut to a readable length
    excerpt: str              # where the description names it, when neither the title nor an item does


class _Names:
    """One project's names, ready to be looked for."""

    def __init__(self, project: dict):
        self.project = project
        self.phrases = list(dict.fromkeys(
            p for p in [project["name"], *project["also_called"], *project["match"]] if p))
        self.wanted = _pattern(self.phrases)
        self.barred = _pattern([w for w in project["not_wording"] if w])
        self.not_meetings = set(project["not_meetings"])

    def spans(self, folded: str) -> list[tuple[int, int, str]]:
        """(start, end, name) for each mention in an already folded text."""
        barred = [m.span() for m in self.barred.finditer(folded)] if self.barred else []
        return [(m.start(), m.end(), self.phrases[m.lastindex - 1]) for m in self.wanted.finditer(folded)
                if not any(a <= m.start() and m.end() <= b for a, b in barred)]


def _find(names: list[_Names], events: list[Event]) -> list[Mention]:
    found = []
    for ev in events:
        uid = ev.stable_uid()
        items, rest = _read(ev.description)
        title = fold(ev.summary)
        item_texts = [fold(item.text) for item in items]
        rest_texts = [fold(paragraph) for paragraph in rest]
        everything = "\n".join([title, *item_texts, *rest_texts])
        for n in names:
            if uid in n.not_meetings or not n.wanted.search(everything):
                continue
            in_title = n.spans(title)
            in_items = [(item, spans) for item, text in zip(items, item_texts) if (spans := n.spans(text))]
            in_rest = [(paragraph, spans) for paragraph, text in zip(rest, rest_texts) if (spans := n.spans(text))]
            if not (in_title or in_items or in_rest):
                continue
            first = (in_title or (in_items[0][1] if in_items else in_rest[0][1]))[0]
            quoted = tuple(Item(item.number, excerpt(item.text, spans[0][0], spans[0][1])) for item, spans in in_items)
            where = "" if in_title or in_items else _line_excerpt(in_rest[0][0], *in_rest[0][1][0][:2])
            found.append(Mention(n.project, ev, first[2], quoted, where))
    return found


def _by_date(m: Mention) -> tuple[str, str]:
    return m.event.start.isoformat(), m.event.stable_uid()


def find(projects: list[dict], events: list[Event]) -> list[Mention]:
    """Every meeting that names a project, in the order of the list and then
    of the calendar. The projects are as caltools.projects.parse() returns them."""
    names = [n for n in map(_Names, projects) if n.wanted]
    rank = {id(n.project): i for i, n in enumerate(names)}
    return sorted(_find(names, events), key=lambda m: (rank[id(m.project)], *_by_date(m)))


def with_projects(events: list[Event], mentions: list[Mention]) -> list[Event]:
    """The events as the calendars publish them: one that names a project is
    replaced by a copy whose description ends with a line saying which. The
    events passed in are left as they are."""
    named: dict[str, list[str]] = {}
    for m in mentions:
        named.setdefault(m.event.stable_uid(), []).append(m.project["name"])
    out = []
    for ev in events:
        projects = named.get(ev.stable_uid())
        if projects:
            line = FOLLOWS + "; ".join(projects)
            body = (ev.description or "").rstrip()
            ev = replace(ev, description=f"{body}\n\n{line}" if body else line)
        out.append(ev)
    return out


class Tied:
    """Which meetings name which project, gathered one calendar at a time so
    the build can use the answer for a calendar as soon as it has its events.
    The projects are as caltools.projects.parse() returns them. `labels` gives
    each calendar (austin, capmetro, ...) the name to show for it; calendars
    are shown in the order `labels` has them, then any others."""

    def __init__(self, projects: list[dict], today: date, labels: dict[str, str] | None = None):
        self.today = today
        self.labels = labels or {}
        self.events: dict[str, list[Event]] = {}     # calendar -> its events, each once
        self._names = [n for n in map(_Names, projects) if n.wanted]
        self._rank = {n.project["slug"]: i for i, n in enumerate(self._names)}
        self._found: list[Mention] = []

    def add(self, calendar: str, events: list[Event]) -> None:
        """Take in events of one calendar. An event that calendar already
        gave (the same id) is skipped, so a second batch may overlap the first."""
        have = self.events.setdefault(calendar, [])
        seen = {ev.stable_uid() for ev in have}
        new = [ev for ev in events if not (ev.stable_uid() in seen or seen.add(ev.stable_uid()))]
        self._found += _find(self._names, new)
        have += new

    @property
    def mentions(self) -> list[Mention]:
        """Every match, in the order of the list and then of the calendar."""
        return sorted(self._found, key=lambda m: (self._rank[m.project["slug"]], *_by_date(m)))

    def of(self, project: dict) -> list[Mention]:
        return sorted((m for m in self._found if m.project["slug"] == project["slug"]), key=_by_date)

    def published(self, events: list[Event]) -> list[Event]:
        """`events` for a calendar file: see with_projects()."""
        return with_projects(events, self.mentions)

    def calendars(self) -> dict[str, list[Event]]:
        """Shown name -> events. Two calendars that share a name are counted together."""
        out: dict[str, list[Event]] = {}
        for key in [k for k in self.labels if k in self.events] + [k for k in self.events if k not in self.labels]:
            out.setdefault(self.labels.get(key, key), []).extend(self.events[key])
        return out


def tie(projects: list[dict], calendars: dict[str, list[Event]], today: date,
        labels: dict[str, str] | None = None) -> Tied:
    """Match every calendar's events against the list. `calendars` is keyed
    the way data/ is (austin, capmetro, ...)."""
    tied = Tied(projects, today, labels)
    for calendar, events in calendars.items():
        tied.add(calendar, events)
    return tied


def saved(data: pathlib.Path) -> dict[str, list[Event]]:
    """The calendars as the last build saved them: data/<calendar>.json ->
    events. A file that is not a list of events (health.json,
    openmeetings.json) or cannot be read is left out."""
    out = {}
    for path in sorted(data.glob("*.json")):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                out[path.stem] = [Event.from_json(row) for row in rows]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


# ───────────────────────────── the projects page ─────────────────────────────

def _first_day(ev: Event) -> date:
    return ev.start.date() if isinstance(ev.start, datetime) else ev.start


def _last_day(ev: Event) -> date:
    last = ev.end or ev.start
    return last.date() if isinstance(last, datetime) else last


def _md(text: str) -> str:
    """Text that Markdown will show as written, on one line. ("$" and "~"
    too: GitHub and Obsidian read a pair of them as maths or a strike-out.)"""
    return re.sub(r"([\\`*_\[\]|<>$~])", r"\\\1", " ".join(text.split()))


def _link(ev: Event) -> str:
    title = _md(ev.summary)
    if not ev.url:
        return title
    return f"[{title}]({ev.url.replace(' ', '%20').replace('(', '%28').replace(')', '%29')})"


def _day_text(ev: Event) -> str:
    day = _first_day(ev)
    return f"{day.day} {day:%b %Y}"


def _meeting_line(m: Mention) -> str:
    line = f"- {_day_text(m.event)} · {_link(m.event)}"
    if m.event.status == "CANCELLED" and "cancel" not in m.event.summary.lower():
        line += " (cancelled)"
    quotes = [(f"item {item.number}: " if item.number else "") + f"“{_md(item.text)}”" for item in m.items[:ITEMS_MAX]]
    if not quotes and m.excerpt:
        quotes = [f"“{_md(m.excerpt)}”"]
    if len(m.items) > ITEMS_MAX:
        quotes.append(f"and {len(m.items) - ITEMS_MAX} more")
    return line + (" · " + "; ".join(quotes) if quotes else "")


def meeting_lines(tied: Tied, project: dict) -> tuple[list[str], list[str]]:
    """A project's meetings as Markdown list lines: (upcoming, soonest
    first; past, latest first). Two records of one meeting (the same day,
    title and link) read the same and are given once."""
    lines = list(dict.fromkeys((_last_day(m.event) >= tied.today, _meeting_line(m)) for m in tied.of(project)))
    return [line for ahead, line in lines if ahead], [line for ahead, line in reversed(lines) if not ahead]


def project_section(tied: Tied, project: dict) -> list[str]:
    """A project's meetings for the page: upcoming ones, then past ones,
    each list cut off with a count of the rest. [] when no meeting names
    the project."""
    out = []
    for heading, group, limit, rest in zip(("Upcoming meetings", "Past meetings"), meeting_lines(tied, project),
                                           (UPCOMING_MAX, PAST_MAX), ("later", "earlier")):
        if group:
            out += [f"**{heading}:**", *group[:limit]]
            if len(group) > limit:
                out.append(f"- and {len(group) - limit} {rest}")
            out.append("")
    return out


def body_of(ev: Event) -> str:
    """The body a meeting belongs to: its title without the notes sources add
    to it ("(Cancelled)", "(Special Called)", "- Cancelled")."""
    return re.sub(r"\s*(?:\((?:Cancelled|Special Called)\)|- Cancelled)$", "", ev.summary.strip())


def coverage(tied: Tied) -> list[tuple[str, str, int, int, int]]:
    """Rows of (kind, label, meetings, with an agenda digest, naming a
    project): one "calendar" row for each calendar, a "body" row for each
    title in it with BODY_MIN meetings or more, an "other" row for the rest of
    its titles, and a closing "total" row."""
    naming = {m.event.stable_uid() for m in tied.mentions}

    def count(events: list[Event]) -> tuple[int, int, int]:
        return (len(events), sum(has_digest(ev.description) for ev in events),
                sum(ev.stable_uid() in naming for ev in events))

    rows = []
    calendars = tied.calendars()
    for label, events in calendars.items():
        if not events:
            continue
        bodies: dict[str, list[Event]] = {}
        for ev in events:
            bodies.setdefault(body_of(ev), []).append(ev)
        rows.append(("calendar", label, *count(events)))
        small = {body: evs for body, evs in bodies.items() if len(evs) < BODY_MIN}
        for body, evs in sorted(bodies.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if body not in small:
                rows.append(("body", body, *count(evs)))
        if small:
            rows.append(("other", f"{len(small)} other title{'' if len(small) == 1 else 's'}",
                         *count([ev for evs in small.values() for ev in evs])))
    rows.append(("total", "All calendars", *count([ev for events in calendars.values() for ev in events])))
    return rows


def coverage_section(tied: Tied) -> list[str]:
    out = ["## Agenda digests by body", "",
           "A digest is the short agenda the build adds to a meeting when the body posts an agenda it can read. "
           "Where that column shows 0, no agenda of the body has been read so far, and its meetings can be tied "
           "to a project only by their titles and descriptions.", "",
           "Each calendar is followed by its meetings grouped by title, which is usually the body's name; a title "
           f"with fewer than {BODY_MIN} meetings is counted under “other titles”. The counts cover every meeting "
           "in the calendar data, past and upcoming. An upcoming meeting usually has no agenda until about a week "
           "before, and digests were not kept for meetings held before a body's agendas were first read.", "",
           "| Calendar and meeting title | Meetings | With an agenda digest | Naming a listed project |",
           "|---|---:|---:|---:|"]
    for kind, label, *numbers in coverage(tied):
        cells = [_md(label), *map(str, numbers)]
        if kind in ("calendar", "total"):
            cells = [f"**{cell}**" for cell in cells]
        out.append("| " + " | ".join(cells) + " |")
    return out + [""]


def matches_section(tied: Tied) -> list[str]:
    out = ["## Every match", "",
           "Each meeting tied to a project, and the wording that tied it. To tie more meetings to a project, add "
           "the wording agendas use for it to the project's `match` list in `projects.yaml`. To undo a match that "
           "is wrong, copy the line from the last column to `wrong_matches` at the end of that file.", "",
           "| Project | Date | Meeting | Matched on | Line that undoes it |", "|---|---|---|---|---|"]
    for m in tied.mentions:
        out.append(f"| {_md(m.project['name'])} | {_day_text(m.event)} | {_md(m.event.summary)} | {_md(m.phrase)} | "
                   f"`- {{project: {m.project['slug']}, meeting: {m.event.stable_uid()}}}` |")
    return out + [""]


# ───────────────────────────── self-test ─────────────────────────────

# One real digest of each layout in data/*.json, cut down to a few lines, and CapMetro's two layouts, which
# reach data/ once its agendas are read.
_EXAMPLES = {
    "ATP: lettered agenda sections, bulleted items": ("""\
Summary Agenda

Action Items:
- CAC Alternative Appointment Recommendation
- 2027 Meeting Calendar Approval

Discussion Items:
- CapMetro Service Standards & Guidelines

—

Agenda: https://www.projectconnect.com/wp-content/uploads/2026/11/20261112_CACAgenda.pdf""",
        [("", "CAC Alternative Appointment Recommendation"), ("", "2027 Meeting Calendar Approval"),
         ("", "CapMetro Service Standards & Guidelines")]),
    # The 16 Nov 2026 Board digest whole, as sources/capmetro.py writes it.
    "CapMetro: agenda sections, bulleted items": ("""\
Summary Agenda

Action Items:
- Contract with Doggett Freightliner of South Texas, LLC, for purchase of one (1) heavy-duty tow truck in a total amount not to exceed $552,205
- Construction contract with Jay-Reese Contractors, Inc. for the construction of the Plaza Saltillo Rail Station expansion, double tracking, and shared use path…
- Approving the CapMetro Public Transportation Agency Safety Plan (PTASP), authorizing the President & CEO, or her designee, to finalize and execute the Plan, an…
- Appointing Anna Frieze to the CapMetro Access Advisory Committee as Board Member Stephen Chang’s appointee
- Interlocal Agreement (“ILA”) with the University of Texas at Austin (“University”), for participation in the University’s IC2 Institute Readiness Training Prog…
- Approval of a resolution by the CapMetro Board of Directors that President & CEO Dottie Watkins hereby receives a performance rating of ____ for Fiscal Year 20…

Discussion Items:
- FY2027 Long Range Financial Plan Update
- Presentation of the CapMetro Strategic Plan 2030 Annual Progress Report

Executive Session under Chapter 551 of the Texas Government Code:
- Section 551.074 for Personnel Matters related to President & CEO annual performance evaluation and compensation

Memos:
- Fiscal Year 2026 Performance Update - November 9, 2026
- Bus Stop and Transit Amenity Standard Details and Specifications Manual - November 9, 2026
- Fare Strategy Implementation Update - October 30, 2026
- Proposed Updates to the Service Standards and Guidelines - October 30, 2026

—

Agenda: https://capmetrotx.legistar1.com/capmetrotx/meetings/2026/11/3061_A_Board_of_Directors_26-11-16_Agenda.pdf""",
        [("", "Contract with Doggett Freightliner of South Texas, LLC, for purchase of one (1) heavy-duty tow truck "
              "in a total amount not to exceed $552,205"),
         ("", "Construction contract with Jay-Reese Contractors, Inc. for the construction of the Plaza Saltillo "
              "Rail Station expansion, double tracking, and shared use path…"),
         ("", "Approving the CapMetro Public Transportation Agency Safety Plan (PTASP), authorizing the President & "
              "CEO, or her designee, to finalize and execute the Plan, an…"),
         ("", "Appointing Anna Frieze to the CapMetro Access Advisory Committee as Board Member Stephen Chang’s "
              "appointee"),
         ("", "Interlocal Agreement (“ILA”) with the University of Texas at Austin (“University”), for participation "
              "in the University’s IC2 Institute Readiness Training Prog…"),
         ("", "Approval of a resolution by the CapMetro Board of Directors that President & CEO Dottie Watkins "
              "hereby receives a performance rating of ____ for Fiscal Year 20…"),
         ("", "FY2027 Long Range Financial Plan Update"),
         ("", "Presentation of the CapMetro Strategic Plan 2030 Annual Progress Report"),
         ("", "Section 551.074 for Personnel Matters related to President & CEO annual performance evaluation and "
              "compensation"),
         ("", "Fiscal Year 2026 Performance Update - November 9, 2026"),
         ("", "Bus Stop and Transit Amenity Standard Details and Specifications Manual - November 9, 2026"),
         ("", "Fare Strategy Implementation Update - October 30, 2026"),
         ("", "Proposed Updates to the Service Standards and Guidelines - October 30, 2026")]),
    "CapMetro: consent items counted, then sections": ("""\
Summary Agenda

- 9 consent items

Action Items:
- Implement the January 2027 Service Changes, and accepting the results of the Title VI Analysis

Report:
- President & CEO Monthly Update - September 2026""",
        [("", "Implement the January 2027 Service Changes, and accepting the results of the Title VI Analysis"),
         ("", "President & CEO Monthly Update - September 2026")]),
    "CAMPO: numbered items under a note": ("""\
Summary Agenda
(From the packet's executive summary)

6. UPWP Amendment #4
7. TIP and RTP Amendments
10. TDM Program Update

—

Meeting packet: https://www.campotexas.org/wp-content/uploads/2026/09/October-05-2026-TPB-Meeting-Packet_Updated.pdf""",
        [("6", "UPWP Amendment #4"), ("7", "TIP and RTP Amendments"), ("10", "TDM Program Update")]),
    "Legislature: numbered charges, then a link and the bills after the rule": ("""\
Summary Agenda

2. Monitoring: SB 1555 and SB 1939
4. Study: "Legislative Prioritization of Texas Department of Transportation Projects"
9. Agency Oversight

Electronic public comments: https://comments.house.texas.gov/home?c=c470

—

SB 1555: relating to a grant program to fund certain railroad grade separation projects; and""",
        [("2", "Monitoring: SB 1555 and SB 1939"),
         ("4", 'Study: "Legislative Prioritization of Texas Department of Transportation Projects"'),
         ("9", "Agency Oversight")]),
    "PUC: bulleted items, links with no rule before them": ("""\
Summary Agenda

- Longshore – Drill Hole (Docket 59029); discussion and possible action.
- Dinosaur – Longshore (Docket 59315); discussion and possible action.

More: https://changesaroundme.com/Focuses/765+kV+Transmission+Lines

Watch live or archived: http://www.adminmonitor.com/tx/puct/open_meeting/20260821/""",
        [("", "Longshore – Drill Hole (Docket 59029); discussion and possible action."),
         ("", "Dinosaur – Longshore (Docket 59315); discussion and possible action.")]),
    "TPSC: the digest after a rule, items led by a time": ("""\
Virtual meeting (Microsoft Teams)

—

Summary Agenda

10:40 NHTSA Assessment Update
11:40 Stakeholder Updates

—

Minutes: https://www.texaspedsafety.org/wp-content/uploads/2025/03/3.27.25-TPSC-Meeting-Minutes.pdf""",
        [("", "NHTSA Assessment Update"), ("", "Stakeholder Updates")]),
    "TPSC: bulleted items": ("""\
Virtual meeting (Microsoft Teams)

—

Summary Agenda

- 2026 Texas Pedestrian Safety Forum
- Stakeholder Updates

—

Slides: https://www.texaspedsafety.org/wp-content/uploads/2025/12/12.11.25-TPSC.pdf""",
        [("", "2026 Texas Pedestrian Safety Forum"), ("", "Stakeholder Updates")]),
    "Council committee: numbered items with file numbers, briefings as bullets": ("""\
Summary Agenda

3. Discussion and possible action to set the Mobility Committee meeting dates for calendar year 2027. (26-2971, Discussion and Possible Action)

Briefings:
- The near-term implementation strategy for the Austin Core Transportation Plan, which is part of the Austin Strategic Mobility Plan (ASMP) that focuses on downt…

—

Agenda: https://austintexas.legistar1.com/austintexas/meetings/2026/11/4208_A_Mobility_Committee_26-11-12_Committee_Agenda.pdf""",
        [("3", "Discussion and possible action to set the Mobility Committee meeting dates for calendar year 2027. "
               "(26-2971, Discussion and Possible Action)"),
         ("", "The near-term implementation strategy for the Austin Core Transportation Plan, which is part of the "
              "Austin Strategic Mobility Plan (ASMP) that focuses on downt…")]),
    "Council work session: lettered item numbers": ("""\
Summary Agenda

A21. A contract for telecommunication services for all City departments through the Texas Department of Information Resources cooperative contract program, for an i… (26-2646, Pre-Selected Item)

Briefings:
B1: The Joint Emergency Call Center Feasibility Assessment for the City of Austin

E1. Discuss legal issues related to Levins et al. v. City of Austin et al. (26-2910, Executive Session)""",
        [("A21", "A contract for telecommunication services for all City departments through the Texas Department of "
                 "Information Resources cooperative contract program, for an i… (26-2646, Pre-Selected Item)"),
         ("B1", "The Joint Emergency Call Center Feasibility Assessment for the City of Austin"),
         ("E1", "Discuss legal issues related to Levins et al. v. City of Austin et al. (26-2910, Executive Session)")]),
    "City Council: counts only, so no items": ("""\
Summary Agenda
(same agenda as the 14 Aug and 17 Aug meeting)

- 12 consent items
- 10 item from council items
- 1 mueller local govt. item""", []),
    "City board: briefings, quoted discussion items, counts": ("""\
Summary Agenda

1 briefing:
- implementation of urban design guidelines into the Great Streets plan update. Presented

3 discussion items:

Item #3 "Discussion and possible action regarding the Downtown Density Bonus Program (DDBP), including Phase I background and context, Design Guidelines Working Group re…"

3 action items

—

Agenda: https://services.austintexas.gov/edims/document.cfm?id=479347""",
        [("", "implementation of urban design guidelines into the Great Streets plan update. Presented"),
         ("3", "Discussion and possible action regarding the Downtown Density Bonus Program (DDBP), including Phase I "
               "background and context, Design Guidelines Working Group re…")]),
    "City board: one discussion item on the count's own line": ("""\
Summary Agenda

8 cases: 2 x Plan Amendment, 4 x Rezoning, 1 x Restrictive Covenant Termination, 1 x Historic Zoning

1 discussion item: Item #10 "Discussion and action to establish a working group related to Dog’s Head Developments."

4 permanent committee and 1 working group update""",
        [("10", "Discussion and action to establish a working group related to Dog’s Head Developments.")]),
    "City board: items numbered with #, updates as bullets": ("""\
Summary Agenda

2 discussion items:

#5: An update on the Lower Colorado Watershed Restoration and Access Plan (WRAP)

2 updates:
- 12 Aug 2026 Architectural Review Committee

—

Agenda: https://services.austintexas.gov/edims/document.cfm?id=486047""",
        [("5", "An update on the Lower Colorado Watershed Restoration and Access Plan (WRAP)"),
         ("", "12 Aug 2026 Architectural Review Committee")]),
}


def selftest() -> None:
    """The agenda reader against a real digest of every layout, the matcher's
    rules one at a time, and what the calendars and the page are given."""
    for layout, (description, expected) in _EXAMPLES.items():
        got = [(item.number, item.text) for item in agenda_items(description)]
        assert got == expected, (layout, got)
        assert has_digest(description), layout
    assert agenda_items("Agenda: https://example.org/a.pdf\n\nAgenda items:\n1. Not a digest") == []
    assert not has_digest("The Summary Agenda is posted a week ahead.") and agenda_items("") == []

    assert fold("César Chávez St – “Dog’s”\n") == 'Cesar Chavez St - "Dog\'s" '
    assert len(fold("Ünïcödé … ﬁ")) == len("Ünïcödé … ﬁ")
    assert phrase_key("Service  Standards & Guidelines") == phrase_key("service standards and guidelines")

    long = "Approve an ordinance about many other things first, and then, well after the cut, the Red Line Trail Study itself."
    start = long.index("Red Line")
    assert excerpt(long, start, start + 20, 60) == "…then, well after the cut, the Red Line Trail Study itself."
    assert excerpt(long, 0, 7, 40) == "Approve an ordinance about many other…"
    assert excerpt("Short.", 0, 5) == "Short."
    assert excerpt("A bond issue for Park… (26-2115, Consent)", 2, 6, 24) == "A bond issue for Park…"

    def project(slug, name, **more):
        return dict(dict(slug=slug, name=name, also_called=[], match=[], not_meetings=[], not_wording=[]), **more)

    def event(summary, description="", day=date(2026, 9, 14), **more):
        return Event(source="t", summary=summary, start=day, description=description, **more)

    def slugs(projects, *events):
        return [(m.project["slug"], m.event.summary) for m in find(projects, list(events))]

    rail = project("rail", "290 Extension", also_called=["UTP"], match=["Service Standards and Guidelines"])
    # Whole words; capitals, dashes and "&" make no difference; an acronym stays an acronym.
    assert slugs([rail], event("CTRMA - 290 extension open house")) == [("rail", "CTRMA - 290 extension open house")]
    assert slugs([rail], event("US 1290 Extension"), event("290 Extensions"), event("290-Extension")) == []
    assert slugs([rail], event("TxDOT: UTP - Virtual public hearing")) == [("rail", "TxDOT: UTP - Virtual public hearing")]
    assert slugs([rail], event("See /tpp/utp/ for the outputs")) == []
    assert len(find([rail], [event("Board", "Summary Agenda\n\n- CapMetro Service Standards & Guidelines")])) == 1
    assert len(find([rail], [event("Board", "Hearing on the draft Service Standards\nand Guidelines.")])) == 1
    dash = project("grove", "Grove–Riverside site")
    assert slugs([dash], event("Open house", "including the Grove-Riverside Site (125 acres)")) == [("grove", "Open house")]

    # The item that names the project is the one quoted, with its number.
    tip = project("tip", "2027–2030 TIP amendments", also_called=["Transportation Improvement Program"],
                  match=["TIP and RTP Amendments"])
    board = event("CAMPO: Transportation Policy Board", _EXAMPLES["CAMPO: numbered items under a note"][0])
    (m,) = find([tip], [board])
    assert m.items == (Item("7", "TIP and RTP Amendments"),) and m.phrase == "TIP and RTP Amendments" and not m.excerpt
    # Named only in the description: the line that names it. Named in the title: nothing more to quote.
    (m,) = find([dash], [event("PUC: Open Meeting", "Watched cases:\n- Docket 1: Grove–Riverside site line\n- Docket 2: other")])
    assert m.items == () and m.excerpt == "Docket 1: Grove–Riverside site line"
    (m,) = find([dash], [event("Grove-Riverside Site tour", "A tour of the Grove-Riverside site.")])
    assert m.items == () and m.excerpt == ""

    # A wrong match is rejected by wording or by meeting, and only for that project.
    stip = event("TxDOT: Statewide Transportation Improvement Program - FY 2027-2030", day=date(2026, 6, 30))
    assert slugs([tip], stip) == [("tip", stip.summary)]
    strict = dict(tip, not_wording=["Statewide Transportation Improvement Program"])
    assert slugs([strict], stip) == []
    both = event("Hearing", "The Statewide Transportation Improvement Program and CAMPO's Transportation Improvement Program")
    assert len(find([strict], [both])) == 1
    assert slugs([dict(tip, not_meetings=[board.stable_uid()])], board, stip) == [("tip", stip.summary)]
    # A phrase two projects both claim ties the meeting to both (parse() reports it as a problem).
    other = project("other", "Regional plan", match=["TIP and RTP Amendments"])
    assert [slug for slug, _ in slugs([tip, other], board)] == ["tip", "other"]

    # The calendars get a copy with one more line; the event itself is untouched.
    out = with_projects([board, stip], find([tip, other], [board, stip]))
    assert out[0].description.endswith("\n\nChanges Around Me follows: 2027–2030 TIP amendments; Regional plan")
    assert out[1].description == "Changes Around Me follows: 2027–2030 TIP amendments" and stip.description == ""
    assert FOLLOWS not in board.description and out[0].stable_uid() == board.stable_uid()
    assert with_projects([board], []) == [board]

    # CapMetro's layout: the item under "Action Items:" is the one quoted, whole and without a number.
    saltillo = project("saltillo", "Red Line double tracking", match=["Plaza Saltillo Rail Station expansion"])
    capmetro = event("CapMetro: Board of Directors", _EXAMPLES["CapMetro: agenda sections, bulleted items"][0],
                     day=date(2026, 11, 16))
    (m,) = find([saltillo, rail], [capmetro])[:1]
    assert m.items == (Item("", "Construction contract with Jay-Reese Contractors, Inc. for the construction of the "
                                "Plaza Saltillo Rail Station expansion, double tracking, and shared use path…"),), m.items
    assert [m.project["slug"] for m in find([saltillo, rail], [capmetro])] == ["saltillo", "rail"]

    # The page: upcoming soonest first, then past latest first, long lists cut off; one line per meeting.
    budget = project("budget", "City budget", match=["Budget Meeting"])
    days = [date(2026, 8, d) for d in range(1, 9)] + [date(2026, 12, 3), date(2026, 12, 1)]
    meetings = [event("City Council: Budget Meeting", day=d, url="https://example.org/(a)") for d in days]
    meetings.append(replace(meetings[0], uid="a-second-record-of-the-same-meeting"))
    meetings.append(event("Budget Meeting [moved] | Q&A", day=date(2026, 12, 5), status="CANCELLED"))
    tied = tie([budget, tip], {"t": meetings, "campo": [board, stip]}, date(2026, 12, 1), {"campo": "CAMPO"})
    lines = project_section(tied, budget)
    assert lines[:4] == ["**Upcoming meetings:**",
                         "- 1 Dec 2026 · [City Council: Budget Meeting](https://example.org/%28a%29)",
                         "- 3 Dec 2026 · [City Council: Budget Meeting](https://example.org/%28a%29)",
                         "- 5 Dec 2026 · Budget Meeting \\[moved\\] \\| Q&A (cancelled)"], lines
    assert lines[4:7] == ["", "**Past meetings:**", "- 8 Aug 2026 · [City Council: Budget Meeting](https://example.org/%28a%29)"]
    assert lines[-2:] == ["- and 3 earlier", ""] and len(lines) == 6 + PAST_MAX + 2, lines
    assert [len(group) for group in meeting_lines(tied, budget)] == [3, 8]
    assert project_section(tied, tip)[1].endswith("· item 7: “TIP and RTP Amendments”")
    assert project_section(tied, other) == [] and meeting_lines(tied, other) == ([], [])
    assert coverage(tied) == [("calendar", "CAMPO", 2, 1, 2), ("other", "2 other titles", 2, 1, 2),
                              ("calendar", "t", 12, 0, 12), ("body", "City Council: Budget Meeting", 11, 0, 11),
                              ("other", "1 other title", 1, 0, 1), ("total", "All calendars", 14, 1, 14)]
    assert "| **CAMPO** | **2** | **1** | **2** |" in coverage_section(tied)
    assert ("| City budget | 5 Dec 2026 | Budget Meeting \\[moved\\] \\| Q&A | Budget Meeting | "
            f"`- {{project: budget, meeting: {meetings[-1].stable_uid()}}}` |") in matches_section(tied)
    assert _md("Tow truck, $552,205 | contingency ~15%\n to $51,119,762") == \
        "Tow truck, \\$552,205 \\| contingency \\~15% to \\$51,119,762"
    assert body_of(event("CoA: Planning Commission (Special Called)")) == "CoA: Planning Commission"
    assert body_of(event("CAMPO: Technical Advisory Committee - Cancelled")) == "CAMPO: Technical Advisory Committee"

    # Gathered a calendar at a time, the answer is the same; a second batch may repeat the first, and
    # two calendars shown under one name are counted together.
    again = Tied([budget, tip], date(2026, 12, 1), {"campo": "CAMPO", "campo2": "CAMPO"})
    again.add("campo2", [stip])
    again.add("t", meetings[:6])
    again.add("t", meetings[3:])
    again.add("campo", [board])
    assert [(m.project["slug"], m.event.stable_uid()) for m in again.mentions] == \
        [(m.project["slug"], m.event.stable_uid()) for m in tied.mentions]
    assert coverage(again) == coverage(tied) and project_section(again, budget) == lines
    published = again.published([stip, meetings[0]])
    assert published[0].description == "Changes Around Me follows: 2027–2030 TIP amendments"
    assert published[1].description == "Changes Around Me follows: City budget" and meetings[0].description == ""


def selftest_list(projects: list[dict]) -> None:
    """The list itself against a real agenda: the 16 Nov 2026 CapMetro Board
    digest has to tie that meeting to red-line-e-5th, and quote the
    construction contract for the Plaza Saltillo station as the reason."""
    digest = _EXAMPLES["CapMetro: agenda sections, bulleted items"][0]
    board = Event(source="capmetro", summary="CapMetro: Board of Directors", start=datetime(2026, 11, 16, 12, 0),
                  description=digest)
    quoted = [item.text for m in find(projects, [board]) if m.project["slug"] == "red-line-e-5th" for item in m.items]
    assert len(quoted) == 1 and quoted[0].startswith("Construction contract with Jay-Reese Contractors"), \
        ("projects.yaml: red-line-e-5th no longer matches the Plaza Saltillo construction contract on the "
         f"16 Nov 2026 CapMetro Board agenda (was a `match` wording removed?); it matched {quoted}")
