"""projects.yaml — what Changes Around Me follows: projects, plans and policies.

One entry per item on the list. An entry states only what is known about it;
`defaults` fills in the rest. The file holds what the site says about a
project as data, so pages, charts and (later) the map are generated from one
place, and the author's own writing stays in the vault page an entry points to.

    defaults: {tier: watching, added: 2026-11-16}
    organizations:                 # only those sources.yaml does not have
      SGI: Something Good Initiative
    projects:
      - slug: red-line-e-5th       # permanent, lowercase-with-hyphens
        name: Red Line double tracking and E 5th Street
        kind: project              # project | plan | policy
        tier: focus                # focus (Ian has written a page) | watching
        orgs: [CapMetro, CoA]      # codes from sources.yaml or `organizations`
        page: Focuses/Red Line Double Tracking and E 5th Street   # vault page
        sources: [capmetro-rail-and-trail-improvements]           # sources.yaml slugs
        links: [{name: ..., url: ...}]
        also_called: [Rail and Trail Improvements]
        match: [Plaza Saltillo Rail Station expansion]   # wording agendas use for it
        schedule: [...]            # see below
        note: free text, never read by code
    wrong_matches:                 # matches that were wrong
      - {project: red-line-e-5th, meeting: capmetro-board-of-directors-20260928T1200@...}
      - {project: tip-amendments-2027-2030, wording: Statewide Transportation Improvement Program}

A schedule is a list of statements, oldest first. A statement records what
one source said about timing at one moment and is never edited afterwards:
when the source says something new, a new statement goes below it. The latest
statement is the current schedule; the earlier ones show how far it has moved
(slippage()).

        schedule:
          - source: CapMetro presentation          # who or what said it
            url: https://...                       # where, if anywhere
            dated: 2023-07                         # the date the source carries
            retrieved: 2026-10-27                  # or: the day it was read
            capture: https://archive...pdf         # our dated copy, if any
            about: Southern segment                # when not the whole project
            from_notes: yes                        # see below; left out otherwise
            note: why it is undated, and so on
            phases:
              - {name: Construction, said: "completion anticipated in 2026", end: 2026}

`said` is the wording as published, and the page prints it in quotation
marks. A statement that is the author's own record of what the author was
told — a conversation, a timeline handed over — has `from_notes: yes`; its
`said` is then that note, and the page prints it without quotation marks and
says so.

`start` and `end` are how the wording was read, as YYYY, YYYY-MM or
YYYY-MM-DD, each meaning "some time in that period"; a phase with only one of
them is a milestone: `start` for something that happens or begins then, `end`
for a deadline or a completion. Vague wording is read the same way every
time: spring Mar–May, summer Jun–Aug, fall Sep–Nov; early Jan–Apr, mid
May–Aug, late Sep–Dec; early or late in a season is its first or last month.
"Winter 2027" is the exception: sources use it for either end of the year, so
it is read as January–February or as December from the order the source lists
things in or from what else the page says, and the statement's note says
which way. A statement may turn out wrong — agencies slip — and that is fine
as long as it says who said it and when.

    python -m caltools.projects                    # check the file
    python -m caltools.projects --preview FILE.md  # and write a page for the vault

A meeting is tied to a project when its title, agenda or description uses
one of the project's names: `name`, `also_called` and `match`.
caltools/meetings.py does the comparing and says how; this module checks the
three lists and `wrong_matches`, which takes one meeting away from a project
(by its id) or stops one wording counting as a mention of it. A `note` on a
`wrong_matches` line says why, for whoever reads it next. A meeting that is
moved gets a new id, so its line stops applying; unknown_meetings() reports
that separately, because it is nobody's mistake and the list is still good.

The build runs the same checks (fatal offline, an alarm live) and writes the
list as docs/projects.md, with each project's meetings, a table of which
bodies' agendas are read, and every match with the line that undoes it.

load() and parse() report problems as plain strings that name the line of
the file or the project by slug, so a bad edit is findable without reading a
traceback.
"""
from __future__ import annotations

import calendar
import json
import os
import pathlib
import re
import sys
import unicodedata
from datetime import date, datetime

from caltools import meetings

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects.yaml"
DATA = ROOT / "data"

KIND = {"project", "plan", "policy"}
TIER = {"focus", "watching"}
TOP_KEYS = {"defaults", "organizations", "projects", "wrong_matches"}
DEFAULT_KEYS = {"tier", "added"}
PROJECT_KEYS = {"slug", "name", "kind", "tier", "orgs", "page", "sources", "links",
                "also_called", "match", "schedule", "added", "note"}
WRONG_MATCH_KEYS = {"project", "meeting", "wording", "note"}
STATEMENT_KEYS = {"source", "url", "dated", "retrieved", "capture", "about", "from_notes", "note", "phases"}
PHASE_KEYS = {"name", "said", "start", "end"}
SLUG_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
WHEN_RE = re.compile(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")
# What Mermaid reads as an instruction when a line of a Gantt chart starts
# with it, whatever the capitals: "Section 106 review" would open a section
# and "Title VI analysis" would rename the chart.
MERMAID_RE = re.compile(r"(?i)(gantt|dateFormat|inclusiveEndDates|topAxis|axisFormat|tickInterval|includes|excludes"
                        r"|todayMarker|weekday|weekend|title|accTitle|accDescr|section|click|href|call"
                        r"|\d{4}-\d\d-\d\d|%%)")


def _text(v) -> str:
    """Every value is a string downstream; YAML turns 2026-04-03 into a date
    and 2027 into a number, so undo that."""
    if v is None:
        return ""
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return str(v).strip()


def period(when: str) -> tuple[date, date]:
    """'2027', '2027-01' or '2027-01-10' -> the first and last day it can mean."""
    m = WHEN_RE.fullmatch(when)
    if not m:
        raise ValueError(f"{when!r} is not YYYY, YYYY-MM or YYYY-MM-DD")
    year, month = int(m[1]), int(m[2] or 1)
    try:
        if m[3]:
            return date(year, month, int(m[3])), date(year, month, int(m[3]))
        if m[2]:
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        return date(year, 1, 1), date(year, 12, 31)
    except ValueError:                             # month 00, 31 February, year 0000
        raise ValueError(f"{when!r} is not a date that exists") from None


def parse(doc, org_codes, source_slugs) -> tuple[list[dict], dict[str, str], list[str]]:
    """projects.yaml -> (projects, the organizations it adds, problems). Every
    project comes back with every field present — strings and lists (and one
    yes/no, `from_notes`), never None — and defaults applied, so nothing
    downstream has to ask. Each one also carries what `wrong_matches` says
    about it, as `not_meetings` and `not_wording`. Whether a meeting named
    there is still on the calendars is for unknown_meetings() to say."""
    if not isinstance(doc, dict):
        return [], {}, ["projects.yaml: top level must be a mapping"]
    problems = [f"projects.yaml: unknown top-level key {k!r}" for k in sorted(set(doc) - TOP_KEYS, key=str)]

    def shaped(key, kind, wanted):
        value = doc.get(key) or kind()
        if isinstance(value, kind):
            return value
        problems.append(f"{key}: must be {wanted}")
        return kind()

    defaults = shaped("defaults", dict, "a mapping, such as {tier: watching}")
    problems += [f"defaults: unknown key {k!r}" for k in sorted(set(defaults) - DEFAULT_KEYS, key=str)]
    extra = {str(code): _text(name)
             for code, name in shaped("organizations", dict, "a mapping of code: name").items()}
    for code, name in extra.items():
        if code in org_codes:
            problems.append(f"organizations: {code} is already in sources.yaml")
        if not name:
            problems.append(f"organizations: {code} has no name")
    known_orgs = set(org_codes) | set(extra)

    projects, slugs, names = [], set(), set()
    for i, raw in enumerate(shaped("projects", list, "a list of entries, each starting with '- slug:'")):
        if not isinstance(raw, dict):
            problems.append(f"projects[{i}]: not a mapping")
            continue
        slug = _text(raw.get("slug"))
        here = slug or f"projects[{i}]"

        def bad(msg, here=here):
            problems.append(f"{here}: {msg}")

        def items(key, source=raw):
            value = source.get(key)
            if value is None:
                return []
            if not isinstance(value, list):
                bad(f"{key} must be a list")
                return []
            return value

        for key in sorted(set(raw) - PROJECT_KEYS, key=str):
            bad(f"unknown key {key!r}")
        p = dict(slug=slug, name=_text(raw.get("name")), kind=_text(raw.get("kind")),
                 tier=_text(raw.get("tier") or defaults.get("tier")), page=_text(raw.get("page")),
                 added=_text(raw.get("added") or defaults.get("added")), note=_text(raw.get("note")),
                 orgs=[_text(o) for o in items("orgs")], sources=[_text(s) for s in items("sources")],
                 also_called=[_text(a) for a in items("also_called")],
                 match=[_text(m) for m in items("match")], not_meetings=[], not_wording=[],
                 links=[], schedule=[])
        if not SLUG_RE.fullmatch(slug):
            bad("slug must be lowercase letters, digits and hyphens")
        elif slug in slugs:
            bad("slug is used twice")
        slugs.add(slug)
        if not p["name"]:
            bad("no name")
        elif p["name"].lower() in names:
            bad(f"name {p['name']!r} is used twice")
        names.add(p["name"].lower())
        for key in ("also_called", "match"):
            if "" in p[key]:
                bad(f"{key} has an empty entry")
            for entry in items(key):
                if isinstance(entry, (dict, list)):
                    bad(f"{key}: {entry!r} is not plain text (a wording with a colon in it needs quotes around it)")
        if p["kind"] not in KIND:
            bad(f"kind must be one of {', '.join(sorted(KIND))}")
        if p["tier"] not in TIER:
            bad(f"tier must be one of {', '.join(sorted(TIER))}")
        if p["tier"] == "focus" and not p["page"]:
            bad("a focus project needs a page")
        if p["page"].endswith(".md"):
            bad("page is the vault path without .md")
        for org in p["orgs"]:
            if org not in known_orgs:
                bad(f"unknown organization {org!r}")
        for s in p["sources"]:
            if s not in source_slugs:
                bad(f"source {s!r} is not in sources.yaml")
        try:
            if len(p["added"]) != 10:
                raise ValueError
            period(p["added"])
        except ValueError:
            bad("added must be YYYY-MM-DD")
        for link in items("links"):
            link = link if isinstance(link, dict) else {}
            name, url = _text(link.get("name")), _text(link.get("url"))
            if not name or not url.startswith(("http://", "https://")):
                bad("each link needs a name and an http(s) url")
            p["links"].append(dict(name=name, url=url))

        last_dated: dict[str, date] = {}          # per `about`: the newest dated statement so far
        for n, st in enumerate(items("schedule"), 1):
            if not isinstance(st, dict):
                bad(f"schedule #{n}: not a mapping")
                continue

            def sbad(msg, n=n):
                bad(f"schedule #{n}: {msg}")

            for key in sorted(set(st) - STATEMENT_KEYS, key=str):
                sbad(f"unknown key {key!r}")
            s = {k: _text(st.get(k)) for k in STATEMENT_KEYS - {"phases", "from_notes"}}
            s["phases"] = []
            s["from_notes"] = st.get("from_notes") is True
            if not isinstance(st.get("from_notes", False), bool):
                sbad("from_notes must be yes or no")
            if not s["source"]:
                sbad("no source — who said it?")
            for key in ("url", "capture"):
                if s[key] and not s[key].startswith(("http://", "https://")):
                    sbad(f"{key} must be an http(s) address")
            given = s["dated"] or s["retrieved"] or s["note"]
            for key in ("dated", "retrieved"):
                try:
                    s[key] and period(s[key])
                except ValueError as e:
                    sbad(f"{key}: {e}")
                    s[key] = ""
            if not given:
                sbad("undated — give `dated`, `retrieved`, or a note saying why there is neither")
            if s["dated"]:
                day = period(s["dated"])[0]
                if day < last_dated.get(s["about"], day):
                    sbad("dated earlier than the statement above it — statements go oldest first")
                last_dated[s["about"]] = day
            phases = items("phases", st)
            if not phases:
                sbad("no phases")
            for ph in phases:
                ph = ph if isinstance(ph, dict) else {}
                for key in sorted(set(ph) - PHASE_KEYS, key=str):
                    sbad(f"phase has unknown key {key!r}")
                phase = {k: _text(ph.get(k)) for k in PHASE_KEYS}
                if not phase["name"]:
                    sbad("a phase has no name")
                elif phase["name"] in {done["name"] for done in s["phases"]}:
                    sbad(f"phase {phase['name']!r} appears twice — phases are matched by name")
                try:
                    start = phase["start"] and period(phase["start"])[0]
                    end = phase["end"] and period(phase["end"])[1]
                    if not (start or end):
                        sbad(f"phase {phase['name']!r} has neither start nor end")
                    elif start and end and start > end:
                        sbad(f"phase {phase['name']!r} ends before it starts")
                except ValueError as e:
                    sbad(f"phase {phase['name']!r}: {e}")
                    continue
                s["phases"].append(phase)
            p["schedule"].append(s)
        projects.append(p)
    if not projects:
        problems.append("projects.yaml: no projects")

    # A meeting is listed under every project one of whose names it uses, so a
    # name two projects share would list it under both. (Two projects with the
    # same `name` are already reported above.)
    claimed: dict[str, tuple[dict, bool]] = {}
    for p in projects:
        for n, phrase in enumerate(dict.fromkeys([p["name"], *p["also_called"], *p["match"]])):
            key = meetings.phrase_key(phrase)
            first, first_is_name = claimed.setdefault(key, (p, n == 0))
            if key and first is not p and first["slug"] != p["slug"] and not (n == 0 and first_is_name):
                problems.append(f"{p['slug'] or p['name']}: {phrase!r} is also a name for {first['slug']} — "
                                "a meeting that uses it would be listed under both")

    by_slug = {p["slug"]: p for p in projects}
    for n, raw in enumerate(shaped("wrong_matches", list, "a list of lines, each {project: ..., meeting: ...} "
                                                         "or {project: ..., wording: ...}"), 1):
        here = f"wrong_matches #{n}"
        if not isinstance(raw, dict):
            problems.append(f"{here}: not a mapping")
            continue
        problems += [f"{here}: unknown key {k!r}" for k in sorted(set(raw) - WRONG_MATCH_KEYS, key=str)]
        slug, meeting, wording = (_text(raw.get(k)) for k in ("project", "meeting", "wording"))
        p = by_slug.get(slug)
        if p is None:
            problems.append(f"{here}: no project has the slug {slug!r}")
        if bool(meeting) == bool(wording):
            problems.append(f"{here}: give either `meeting` (its id) or `wording`")
        elif meeting:
            if p:
                p["not_meetings"].append(meeting)
        elif p:
            names = [meetings.phrase_key(x) for x in (p["name"], *p["also_called"], *p["match"]) if x]
            if not any(name in meetings.phrase_key(wording) for name in names):
                problems.append(f"{here}: {wording!r} contains none of the names {slug} is matched on, "
                                "so it would change nothing")
            else:
                p["not_wording"].append(wording)
    return projects, extra, problems


def _read(path: pathlib.Path):
    """The file as data. Whatever is wrong with the file itself — missing, not
    YAML, a key given twice — raises ValueError, naming the line where it can.
    Two things differ from plain YAML: a key repeated in one entry is an error
    (YAML would silently keep the second), and dates stay as written, so that
    one that does not exist reaches parse() and is reported with its project."""
    import yaml

    class Loader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            seen = set()
            for key_node, _ in node.value:
                if isinstance(key_node, yaml.ScalarNode):
                    if key_node.value in seen:
                        raise ValueError(f"{path.name} line {key_node.start_mark.line + 1}: key {key_node.value!r} "
                                         "appears twice in one entry, and only the second would be kept")
                    seen.add(key_node.value)
            return super().construct_mapping(node, deep)

    Loader.add_constructor("tag:yaml.org,2002:timestamp", lambda loader, node: loader.construct_scalar(node))
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.load(f, Loader=Loader)
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"{path.name}: cannot be read ({getattr(e, 'strerror', None) or e})") from e
    except yaml.YAMLError as e:
        mark, began = getattr(e, "problem_mark", None), getattr(e, "context_mark", None)
        what = getattr(e, "problem", None) or str(e)
        if getattr(e, "context", None):
            what = f"{e.context}" + (f" that starts on line {began.line + 1}" if began else "") + f", {what}"
        raise ValueError(f"{path.name}{f' line {mark.line + 1}' if mark else ''}: not valid YAML ({what})") from e


def load(path: pathlib.Path = PROJECTS) -> tuple[list[dict], dict[str, str]]:
    """(projects, organization code -> name). Any problem raises ValueError,
    one plain line per problem: a list that does not check out should not be
    published."""
    doc = _read(path)
    from caltools import registry
    known = {code: org.get("name", code) for code, org in registry.orgs().items()}
    projects, extra, problems = parse(doc, set(known), {row["slug"] for row in registry.rows()})
    if problems:
        raise ValueError("\n".join(problems))
    return projects, {**known, **extra}


def unknown_meetings(projects: list[dict], data: pathlib.Path = DATA) -> list[str]:
    """Problems with `wrong_matches` lines whose meeting is not on the
    calendars as the last build saved them in `data`. Apart from parse()
    because nobody has to edit the file for this to happen: a meeting that is
    moved gets a new id. The line then does nothing and needs copying again,
    but the rest of the list is as good as it was."""
    ids = {ev.stable_uid() for events in meetings.saved(data).values() for ev in events}
    return [f"wrong_matches: no meeting on the calendars has the id {uid!r} (given for {p['slug']}). A meeting "
            "that is moved gets a new id: copy the line again from “Every match” in docs/projects.md, or remove it"
            for p in projects for uid in p["not_meetings"] if uid not in ids]


def calendar_names() -> dict[str, str]:
    """Calendar (austin, capmetro, ...) -> the organization it belongs to, in
    the order sources.yaml lists the organizations."""
    from caltools import registry
    orgs = registry.orgs()
    names: dict[str, str] = {}
    for row in registry.rows():
        if row.get("calendar"):
            names.setdefault(row["calendar"], orgs.get(row["org"], {}).get("name") or row["org"])
    return names


def missing_pages(projects: list[dict], vault: pathlib.Path) -> list[str]:
    """Vault pages the list points at that are not there. Only checkable where
    the vault is (Ian's Mac), so it is separate from parse()."""
    return [f"{p['slug']}: page {p['page']!r} is not in the vault"
            for p in projects if p["page"] and not (vault / (p["page"] + ".md")).is_file()]


def groups(project: dict) -> dict[str, list[dict]]:
    """A project's statements by what they are about ("" = the whole project),
    each list oldest first."""
    out: dict[str, list[dict]] = {}
    for statement in project["schedule"]:
        out.setdefault(statement["about"], []).append(statement)
    return out


def _when(statement: dict) -> str:
    if statement["dated"]:
        return f"dated {statement['dated']}"
    return f"read {statement['retrieved']}" if statement["retrieved"] else "undated"


def _months(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + b.month - a.month


def slippage(project: dict) -> list[dict]:
    """How far each phase has moved: for a phase's start and for its end, the
    first statement that dates it against the latest one. Each wording is a
    period (period()), so it is a move only when the two periods do not
    overlap: "2027" and then "2027-06" is the same date said more exactly. A
    move is `least` to `most` whole months, positive for later; the two differ
    when a wording is wider than a month, so "2025" then "2027-01" is 13 to
    24 months later."""
    moved = []
    for about, statements in groups(project).items():
        names = list(dict.fromkeys(ph["name"] for st in statements for ph in st["phases"]))
        for name in names:
            for edge in ("start", "end"):
                said = [(st, ph[edge]) for st in statements for ph in st["phases"]
                        if ph["name"] == name and ph[edge]]
                if len(said) < 2:
                    continue
                (first_st, first), (latest_st, latest) = said[0], said[-1]
                (a0, a1), (b0, b1) = period(first), period(latest)
                if a0 <= b1 and b0 <= a1:
                    continue
                least, most = sorted((_months(a1, b0), _months(a0, b1)), key=abs)
                moved.append(dict(about=about, phase=name, edge=edge, first=first, latest=latest,
                                  later=b0 > a1, least=least, most=most,
                                  first_source=first_st["source"], first_when=_when(first_st),
                                  latest_source=latest_st["source"], latest_when=_when(latest_st)))
    return moved


def _how_far(move: dict) -> str:
    """A move in words: "1 month later", "13 to 24 months later"."""
    least, most, way = abs(move["least"]), abs(move["most"]), "later" if move["later"] else "earlier"
    if not most:
        return f"{way} in the same month"
    if least == most:
        return f"{most} month{'' if most == 1 else 's'} {way}"
    return f"{least} to {most} months {way}" if least else f"up to {most} months {way}"


def _label(text: str) -> str:
    """Text as a Mermaid task name or title. A colon, semicolon or hash would
    end it, so each becomes a comma. A first word Mermaid would read as an
    instruction gets a zero-width space in front, which changes nothing a
    reader sees."""
    text = re.sub(r"[:;#]", ",", text)
    return "​" + text if MERMAID_RE.match(text) else text


def _axis(first: date, last: date) -> tuple[str, str]:
    """(axisFormat, tickInterval) for a chart from `first` to `last`, chosen
    so that no label repeats along the axis: days for a span of weeks, month
    and year up to about three years, years beyond that."""
    days = (last - first).days
    if days <= 45:
        return "%-d %b %Y", "1day" if days <= 10 else "1week"
    if days <= 3 * 366:
        return "%b %Y", "1month" if days <= 280 else "2month" if days <= 560 else "3month"
    return "%Y", "12month" if days <= 12 * 366 else "60month"


def gantt(project: dict) -> str:
    """A Mermaid Gantt chart of the current schedule — the latest statement of
    each group — or "" when there is nothing a time axis can show: no schedule,
    or a single date, however many phases carry it. Mermaid's own sections are
    not used, because it prints their names over the rows: one group is named
    in the title, and each row of several groups starts with its group's name,
    cut at the first comma where that still tells the groups apart."""
    by_group = groups(project)
    short = [about.split(",")[0] for about in by_group]
    rows, days, dates = [], [], set()
    for about, statements in by_group.items():
        lead = ""
        if about and len(by_group) > 1:
            lead = (about if short.count(about.split(",")[0]) > 1 else about.split(",")[0]) + " – "
        for ph in statements[-1]["phases"]:
            name, n = _label(lead + ph["name"]), len(rows) + 1
            dates.update(when for when in (ph["start"], ph["end"]) if when)
            if ph["start"] and ph["end"]:
                first, last = period(ph["start"])[0], period(ph["end"])[1]
                rows.append(f"    {name} :p{n}, {first}, {last}")
                days += [first, last]
            else:
                day = period(ph["start"])[0] if ph["start"] else period(ph["end"])[1]
                rows.append(f"    {name} :milestone, p{n}, {day}, 0d")
                days.append(day)
    if len(dates) < 2:
        return ""
    only = list(by_group)[0] if len(by_group) == 1 else ""
    title = project["name"] + (f" – {only}" if only else "")
    axis, tick = _axis(min(days), max(days))
    return "\n".join(["```mermaid", "gantt", f"    title {_label(title)}", "    dateFormat YYYY-MM-DD",
                      f"    axisFormat {axis}", f"    tickInterval {tick}", "    todayMarker off", *rows, "```"])


def gaps(projects: list[dict]) -> list[str]:
    """What the list does not know yet, in words for Ian."""
    out = []
    for p in projects:
        if not p["orgs"]:
            out.append(f"**{p['name']}**: no organization recorded.")
        if p["tier"] == "focus" and not p["schedule"]:
            out.append(f"**{p['name']}**: a Focus project with no schedule recorded.")
        if not (p["sources"] or p["links"] or p["page"]):
            out.append(f"**{p['name']}**: nothing to link to yet.")
    return out


def _reading(phase: dict) -> str:
    """How a phase's dates were read, in words. An exact day needs no
    reading, so it is "on" a day or, for a deadline, "by" it; anything
    vaguer says "read as"."""
    start, end = phase["start"], phase["end"]
    if all(len(when) == 10 for when in (start, end) if when):
        if start and end and start != end:
            return f"{start} to {end}"
        return f"on {start}" if start else f"by {end}"
    if start and end:
        return f"read as during {start}" if start == end else f"read as {start} to {end}"
    return f"read as starting {start}" if start else f"read as by the end of {end}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")                # a bare pipe would end a table cell


def _sort_key(name: str) -> str:
    """Names in the order a reader expects: "César Chávez" with the Cs before
    "Convention Center", and a name in quotation marks under its first letter."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]", "", plain).strip()


HEADS_UP = ("Heads up: this project leans heavily on AI, and not every line has been reviewed by a human. "
            "My buddy Claude and I do our best to get things right, but please check the linked source before "
            "relying on a date.")


def preview(projects: list[dict], org_names: dict[str, str], vault: bool = False,
            today: date | None = None, tied: meetings.Tied | None = None) -> str:
    """The list as a readable page: what it is and how to read it, then every
    project with a schedule (chart, statements, slippage) or a meeting, then a
    table of all. The same Markdown serves GitHub (docs/projects.md) and
    Obsidian. `vault` is for the author's own copy: it adds the open questions
    and the links to vault pages, which only resolve inside the vault. `tied`
    is which meetings name which project (caltools.meetings.tie); with it the
    page lists each project's meetings and ends with the table of agenda
    digests and the table of every match."""
    scheduled = [p for p in projects if p["schedule"]]
    met = {p["slug"]: meetings.project_section(tied, p) for p in projects} if tied else {}
    codes = sorted({o for p in projects for o in p["orgs"]}, key=str.lower)
    who = "; ".join(f"{c} = {re.sub(r' *[(][^()]*[)]$', '', org_names.get(c, c))}" for c in codes)
    out = ["> [!NOTE]", f"> {HEADS_UP}", "",
           "# What Changes Around Me follows", "",
           "[Changes Around Me](https://changesaroundme.com) follows a hand-picked set of public projects, plans "
           "and policies, most of them in Austin, Texas. This page lists them, with what their sources have said "
           "about timing" + (" and the public meetings that name them" if tied else "")
           + ". It is generated from `projects.yaml`" + (" and the meeting calendars" if tied else "")
           + (f" on {today.isoformat()}" if today else "")
           + "; edits made here are overwritten, so change that file instead.", "",
           f"{len(projects)} on the list: {sum(p['tier'] == 'focus' for p in projects)} Focus, "
           f"{sum(p['tier'] == 'watching' for p in projects)} watching. "
           f"{len(scheduled)} have a schedule recorded."
           + (f" {sum(map(bool, met.values()))} are named by a meeting on the calendars." if tied else ""), "",
           "## How to read this page",
           "- **Focus** and **watching.** A Focus project is one the site's author has written a page about. "
           "The rest are watched: they are on the list and the web pages about them are checked for changes.",
           "- **Statements.** A schedule is a list of statements. Each is what one source said about timing at "
           "one moment, in the source's own words, with a link to the source and to our saved copy of it where "
           "there is one. A statement marked as recorded in the site author's notes is what the author was told "
           "and wrote down; it is not a quotation, and is printed without quotation marks.",
           "- **Dated** and **Read.** \"Dated\" is the date the source itself carries. \"Read\" is the day the "
           "source was read, given when the source carries no date.",
           "- **How wording is read.** The brackets after a quotation give the dates it is taken to mean; "
           "\"read as\" marks wording that names no exact day. A year or a month means some time in it. "
           "Spring is March to May, summer June to August and fall September to November; early in a year is "
           "January to April, mid May to August and late September to December. \"Winter\" can mean either end "
           "of a year, so it is read from the rest of the page, and the statement says which way.",
           "- **Moved.** A phase has moved when the first statement that dates it and the latest one cannot "
           "both be right. Where one of them names only a year, the move is given as a range of months.",
           *(["- **Meetings.** A meeting on the [calendars](https://changesaroundme.com/calendar) is listed under "
              "a project when its title, its agenda or its description uses one of the project's names. Only the "
              "wording is compared, so a meeting that discusses a project without naming it is not listed. The two "
              "tables at the end of the page show which bodies' agendas are read and every match that was made."]
             if tied else []),
           f"- **Who.** {who}.",
           "- **Monitored pages.** How many web pages about a project are checked for changes and archived. "
           f"They are listed in {'[[Links]]' if vault else '[sources.md](sources.md)'}.", ""]
    questions = gaps(projects) if vault else []
    if questions:
        out += ["## Open questions", *[f"- {q}" for q in questions], ""]
    out += ["## Schedules and meetings" if tied else "## Schedules",
            "Each chart shows the latest statement, or the latest for each part of a project "
            "recorded in parts; a schedule with a single date has no chart. Underneath is every statement "
            "recorded here, oldest first, and how far each phase has moved between the first and the latest."
            + (" Last come the meetings that name the project: those still to come, then the most recent ones "
               "held, with the agenda item that names it where the agenda has been read." if tied else ""), ""]
    for p in projects:
        if not (p["schedule"] or met.get(p["slug"])):
            continue
        facts = [p["kind"].capitalize(), p["tier"], ", ".join(p["orgs"])]
        facts += [f"also called {', '.join(p['also_called'])}"] if p["also_called"] else []
        facts += [f"[[{p['page']}|page]]"] if p["page"] and vault else []
        out += [f"### {p['name']}", " · ".join(f for f in facts if f), ""]
        if p["links"]:
            out += ["Links: " + ", ".join(f"[{link['name']}]({link['url']})" for link in p["links"]), ""]
        chart = gantt(p)
        if chart:
            out += [chart, ""]
        for st in p["schedule"]:
            where = [f"[source]({st['url']})"] if st["url"] else []
            where += [f"[our copy]({st['capture']})"] if st["capture"] else []
            head = f"- **{_when(st).capitalize()}.** {st['source']}" + (f" ({', '.join(where)})" if where else "")
            out.append(head + (f" — about: {st['about']}" if st["about"] else "")
                       + (" — recorded in the site author's notes, not quoted from a document" if st["from_notes"] else "")
                       + (f" *{st['note']}*" if st["note"] else ""))
            for ph in st["phases"]:
                said = ph["said"] if st["from_notes"] else f"“{ph['said']}”"
                out.append(f"    - {ph['name']}: " + (f"{said} ({_reading(ph)})" if ph["said"] else _reading(ph)))
        moved = slippage(p)
        if moved:
            out += ["", "**Moved:**"]
            for m in moved:
                out.append(f"- {m['phase']}{' (' + m['about'] + ')' if m['about'] else ''}, {m['edge']}: "
                           f"{_how_far(m)}. {m['first']} ({m['first_source']}, {m['first_when']}) → "
                           f"{m['latest']} ({m['latest_source']}, {m['latest_when']}).")
        if p["schedule"]:
            out.append("")
        out += met.get(p["slug"], [])
    out += ["## Everything on the list", "",
            "| Name | Kind | Tier | Who | Monitored pages | Statements |" + (" Meetings |" if tied else "") + " Links |",
            "|---|---|---|---|---|---|---|" + ("---|" if tied else "")]
    for p in sorted(projects, key=lambda p: (p["tier"] != "focus", _sort_key(p["name"]))):
        name = p["name"] + (f" *(also called {', '.join(p['also_called'])})*" if p["also_called"] else "")
        links = ", ".join(f"[{link['name']}]({link['url']})" for link in p["links"])
        out.append(f"| {_cell(name)} | {p['kind']} | {p['tier']} | {', '.join(p['orgs']) or '—'} | "
                   f"{len(p['sources']) or ''} | {len(p['schedule']) or ''} | "
                   + (f"{sum(map(len, meetings.meeting_lines(tied, p))) or ''} | " if tied else "")
                   + f"{_cell(links)} |")
    if tied:
        out += ["", *meetings.coverage_section(tied), *meetings.matches_section(tied)]
    return "\n".join(out).rstrip("\n") + "\n"


def selftest() -> None:
    """Prove the checks catch what a hand edit most plausibly breaks, that
    slippage, the chart and the page read a schedule the way the docstring
    says, and that the page lists a project's meetings."""
    import tempfile
    assert period("2027") == (date(2027, 1, 1), date(2027, 12, 31))
    assert period("2028-02") == (date(2028, 2, 1), date(2028, 2, 29))
    assert period("2027-01-10") == (date(2027, 1, 10), date(2027, 1, 10))
    for wrong in ("27", "2027-13", "2027-1", "early 2027", "2027-00", "2026-02-31", "2026-00-10", "0000"):
        try:
            period(wrong)
            raise AssertionError(f"period({wrong!r}) should fail")
        except ValueError:
            pass

    def doc(**changes):
        project = dict(slug="rail", name="Rail", kind="project", tier="focus", orgs=["CapMetro"],
                       page="Focuses/Rail", sources=["capmetro-rail"], schedule=[
                           dict(source="An undated page", note="carries no date",
                                phases=[dict(name="Construction", said="in 2025", start=2025, end=2025)]),
                           dict(source="A presentation", dated="2023-07",
                                phases=[dict(name="Construction", end=2026)]),
                           dict(source="Staff", dated=date(2026, 3, 10),
                                phases=[dict(name="Construction", start="2027-01", end="2029-01"),
                                        dict(name="Opening", start="2029-02")])])
        project.update(changes)
        return dict(defaults=dict(added="2026-11-16"), organizations=dict(SGI="Something Good"),
                    projects=[project])

    def problems(**changes):
        return parse(doc(**changes), {"CapMetro"}, {"capmetro-rail"})[2]

    def one(*schedule, **changes):
        """A checked project with this schedule."""
        found, _, wrong = parse(doc(schedule=list(schedule), **changes), {"CapMetro"}, {"capmetro-rail"})
        assert not wrong, wrong
        return found[0]

    def said(when, **phase):
        return dict(source=f"said in {when}", dated=when, phases=[dict(name="C", **phase)])

    projects, extra, none = parse(doc(), {"CapMetro"}, {"capmetro-rail"})
    assert none == [] and extra == {"SGI": "Something Good"}, none
    rail = projects[0]
    assert rail["added"] == "2026-11-16" and rail["schedule"][2]["dated"] == "2026-03-10"
    assert rail["schedule"][0]["phases"][0]["start"] == "2025"            # numbers become strings

    # Moves: a year against a month is a range, and only periods that cannot both be right count.
    assert [(m["edge"], m["least"], m["most"]) for m in slippage(rail)] == [("start", 13, 24), ("end", 37, 48)]
    assert [_how_far(m) for m in slippage(rail)] == ["13 to 24 months later", "37 to 48 months later"]
    for first, latest, expect in [
            (dict(start=2027), dict(start="2027-06"), None),                     # said more exactly: no move
            (dict(start="2027-06"), dict(start=2027), None),
            (dict(end="2026-03"), dict(end="2026-03-31"), None),
            (dict(start="2027-01"), dict(start="2027-02"), "1 month later"),
            (dict(start="2027-03"), dict(start="2026-12"), "3 months earlier"),
            (dict(start=2027), dict(start="2026-11"), "2 to 13 months earlier"),
            (dict(end="2026-03-04"), dict(end="2026-03-18"), "later in the same month"),
            (dict(end="2026-03-18"), dict(end="2026-03-04"), "earlier in the same month")]:
        moves = slippage(one(said("2025-01", **first), said("2025-02", **latest)))
        assert [_how_far(m) for m in moves] == ([expect] if expect else []), (first, latest, moves)

    # Charts: the latest statement, one row a phase, and nothing Mermaid could misread.
    chart = gantt(rail)
    assert "    Construction :p1, 2027-01-01, 2029-01-31" in chart, chart
    assert "    Opening :milestone, p2, 2029-02-01, 0d" in chart, chart
    assert "    axisFormat %b %Y" in chart and "    todayMarker off" in chart and "section" not in chart, chart
    assert gantt(dict(rail, schedule=[])) == ""
    assert gantt(one(said("2026", start="2026-05-21"))) == ""              # one date: nothing to chart
    assert gantt(one(dict(source="s", dated=2026, phases=[
        dict(name="Goes to Council", start=2026, end=2026), dict(name="Approved", start=2026, end=2026)]))) == ""
    assert "axisFormat %Y\n    tickInterval 12month" in gantt(one(said("2026", start=2026, end=2032)))
    assert "axisFormat %-d %b %Y" in gantt(one(dict(source="s", dated=2026, phases=[
        dict(name="Hearing", start="2026-09-14"), dict(name="Comments close", end="2026-09-21")])))
    parts = gantt(one(dict(source="s", dated=2026, about="South, A St to B St", phases=[dict(name="Bids", end="2026-07")]),
                      dict(source="s", dated=2026, about="North", phases=[dict(name="Bids", start=2029)])))
    assert "    South – Bids :milestone, p1, 2026-07-31, 0d" in parts, parts     # an end alone is its last day
    assert "    North – Bids :milestone, p2, 2029-01-01, 0d" in parts and "section" not in parts, parts
    assert "title Rail – South, A St to B St" in gantt(one(dict(
        source="s", dated=2026, about="South, A St to B St", phases=[dict(name="Bids", start=2026, end=2027)])))
    for name in ("Section 106 review", "Title VI analysis", "2026-11-03 election", "click here"):
        row = gantt(one(said("2026", start=2026, end=2027), dict(
            source="t", dated=2027, phases=[dict(name=name, start=2027, end=2028)]))).splitlines()[7]
        assert row.strip().startswith("\u200b" + name), row
        assert not MERMAID_RE.match(row.strip()), row
    assert _label("Design: 30%; #2") == "Design, 30%, ,2"

    # The page: public by default, with the author's extras only in the vault copy.
    page = preview(projects, {"CapMetro": "Capital Metro (CapMetro)"})
    assert "37 to 48 months later. 2025 (An undated page, undated) → 2029-01 (Staff, dated 2026-03-10)." in page, page
    assert "[[" not in page and "2026-11-16" not in page and "Open questions" not in page and HEADS_UP in page
    assert "CapMetro = Capital Metro." in page and "“in 2025” (read as during 2025)" in page, page
    mine = preview(parse(doc(orgs=[]), {"CapMetro"}, {"capmetro-rail"})[0], {}, vault=True, today=date(2026, 11, 16))
    assert "[[Focuses/Rail|page]]" in mine and "## Open questions" in mine and "2026-11-16" in mine
    for phase, expect in [
            (dict(start="2026-05-21"), "on 2026-05-21"), (dict(end="2026-08-31"), "by 2026-08-31"),
            (dict(start="2026-05-21", end="2026-05-21"), "on 2026-05-21"),
            (dict(start="2018-11-28", end="2019-01-28"), "2018-11-28 to 2019-01-28"),
            (dict(start="2027-01"), "read as starting 2027-01"), (dict(end=2026), "read as by the end of 2026"),
            (dict(start="2027-03", end="2027-05"), "read as 2027-03 to 2027-05")]:
        assert f"C: {expect}\n" in preview([one(said("2026", **phase))], {}), (phase, expect)
    notes = preview([one(dict(source="Staff", dated=2026, from_notes=True, phases=[
        dict(name="C", said="Jan 2027, estimated start", start="2027-01")]), name="A | B", also_called=["Rail"],
        links=[dict(name="City page", url="https://example.org")])], {})
    assert "C: Jan 2027, estimated start (read as starting 2027-01)" in notes and "“" not in notes, notes
    assert "not quoted from a document" in notes and "| A \\| B *(also called Rail)* |" in notes, notes
    assert "· also called Rail" in notes and "Links: [City page](https://example.org)" in notes, notes
    assert sorted(["Convention Center", "César Chávez", '"Dog\'s head"', "East"], key=_sort_key) == [
        "César Chávez", "Convention Center", '"Dog\'s head"', "East"]

    for changes, expect in [
            (dict(slug="Rail Line"), "slug must be"), (dict(kind="study"), "kind must be"),
            (dict(tier="focused"), "tier must be"), (dict(page=""), "needs a page"),
            (dict(page="Focuses/Rail.md"), "without .md"), (dict(orgs=["Capmetro"]), "unknown organization"),
            (dict(sources=["capmetro-rial"]), "not in sources.yaml"), (dict(orgs="CapMetro"), "must be a list"),
            (dict(colour="red"), "unknown key 'colour'"), (dict(links=[dict(name="x", url="example.org")]), "http(s) url"),
            (dict(schedule=[dict(phases=[dict(name="C", start=2027)], dated=2026)]), "no source"),
            (dict(schedule=[dict(source="s", phases=[dict(name="C", start=2027)])]), "undated"),
            (dict(schedule=[dict(source="s", dated=2026, phases=[])]), "no phases"),
            (dict(schedule=[dict(source="s", dated=2026, phases=[dict(name="C")])]), "neither start nor end"),
            (dict(schedule=[dict(source="s", dated=2026, phases=[dict(name="C", start=2028, end=2027)])]), "ends before"),
            (dict(schedule=[dict(source="s", dated=2026, phases=[dict(name="C", start="early 2027")])]), "is not YYYY"),
            (dict(schedule=[dict(source="s", dated=2026, phases=[dict(name="C", start="2027-00")])]),
             "rail: schedule #1: phase 'C': '2027-00' is not a date that exists"),
            (dict(schedule=[dict(source="s", dated="2026-02-31", phases=[dict(name="C", start=2027)])]),
             "rail: schedule #1: dated: '2026-02-31' is not a date that exists"),
            (dict(schedule=[dict(source="s", dated=2026, from_notes="maybe", phases=[dict(name="C", start=2027)])]),
             "from_notes must be yes or no"),
            (dict(schedule=[dict(source="s", dated=2026, phases=[dict(name="C", start=2027),
                                                                  dict(name="C", start=2028)])]), "appears twice"),
            (dict(schedule=[dict(source="s", dated=2026, phases=[dict(name="C", start=2027)]),
                            dict(source="t", dated=2025, phases=[dict(name="C", start=2028)])]), "oldest first")]:
        found = problems(**changes)
        assert any(expect in p for p in found), (changes, found)
    two = doc()
    two["projects"].append(dict(two["projects"][0]))
    assert any("used twice" in p for p in parse(two, {"CapMetro"}, {"capmetro-rail"})[2])
    assert any("already in sources.yaml" in p for p in parse(doc(), {"CapMetro", "SGI"}, {"capmetro-rail"})[2])
    for key, wrong in (("defaults", ["tier"]), ("organizations", ["SGI"]), ("projects", dict(slug="rail"))):
        found = parse(dict(doc(), **{key: wrong}), {"CapMetro"}, {"capmetro-rail"})[2]
        assert any(p.startswith(f"{key}: must be") for p in found), (key, found)

    # The file itself: every way it can be unreadable is a ValueError that says where.
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "projects.yaml"
        entry = ("defaults: {tier: watching, added: 2026-11-16}\nprojects:\n  - slug: rail\n    name: Rail\n"
                 "    kind: project\n    orgs: [CapMetro]\n")
        for text, expect in [
                (None, "projects.yaml: cannot be read"),
                (entry.replace("name: Rail", 'name: "Rail'), "projects.yaml line 8: not valid YAML (while scanning "
                 "a quoted scalar that starts on line 4, found unexpected end of stream)"),
                (entry + "    schedule: []\n    schedule: []\n", "projects.yaml line 8: key 'schedule' appears twice")]:
            if text is not None:
                path.write_text(text + "\n", encoding="utf-8")
            try:
                load(path)
                raise AssertionError(f"load() should fail: {expect}")
            except ValueError as e:
                assert str(e).startswith(expect), (expect, str(e))
        path.write_text(entry + "    schedule:\n      - {source: s, dated: 2026-02-31, phases: [{name: C, start: 2027}]}\n",
                        encoding="utf-8")
        assert parse(_read(path), {"CapMetro"}, set())[2] == [
            "rail: schedule #1: dated: '2026-02-31' is not a date that exists"]

    # Names for matching meetings, and the lines that undo a match.
    board = "capmetro-board-of-directors-20261116T1200@calendars.changesaroundme.com"

    def matching(wrong=(), **second):
        """Two projects checked, the second with `second`, and `wrong` as wrong_matches."""
        d = doc(match=["Plaza Saltillo Rail Station expansion"], also_called=["Rail and Trail Improvements"])
        d["projects"].append(dict(slug="trail", name="Red Line Trail Study", kind="plan", tier="watching",
                                  orgs=["CapMetro"], **second))
        d["wrong_matches"] = list(wrong)
        return parse(d, {"CapMetro"}, {"capmetro-rail"})

    listed, _, none = matching([dict(project="rail", meeting=board, note="a contract for buses"),
                                dict(project="rail", wording="Rail and Trail Improvements Newsletter")])
    assert none == [] and listed[0]["match"] == ["Plaza Saltillo Rail Station expansion"], none
    assert listed[0]["not_meetings"] == [board] and listed[1]["not_meetings"] == []
    assert listed[0]["not_wording"] == ["Rail and Trail Improvements Newsletter"]
    for wrong, second, expect in [
            ([dict(project="rial", meeting=board)], {}, "wrong_matches #1: no project has the slug 'rial'"),
            ([dict(project="rail")], {}, "give either `meeting` (its id) or `wording`"),
            ([dict(project="rail", meeting=board, wording="Rail")], {}, "give either `meeting` (its id) or `wording`"),
            ([dict(project="rail", wording="Bus stop improvements")], {}, "contains none of the names rail is matched on"),
            ([dict(project="rail", meeting=board, because="x")], {}, "wrong_matches #1: unknown key 'because'"),
            (["rail"], {}, "wrong_matches #1: not a mapping"),
            ([], dict(match=[""]), "trail: match has an empty entry"),
            ([], dict(match=[{"City Council": "Budget Meeting"}]), "is not plain text (a wording with a colon"),
            ([], dict(also_called=[None]), "trail: also_called has an empty entry"),
            ([], dict(match="Trail Study"), "trail: match must be a list"),
            ([], dict(match=["plaza saltillo  rail station expansion"]), "is also a name for rail"),
            ([], dict(match=["Rail & Trail Improvements"]), "is also a name for rail"),
            ([], dict(also_called=["Rail"]), "trail: 'Rail' is also a name for rail")]:
        found = matching(wrong, **second)[2]
        assert any(expect in p for p in found), (wrong, second, found)
    found = parse(dict(doc(), wrong_matches="rail"), {"CapMetro"}, {"capmetro-rail"})[2]
    assert len(found) == 1 and found[0].startswith("wrong_matches: must be a list"), found
    # A rejected meeting the calendars no longer have (it was moved, or the id is mistyped) is reported
    # apart from the rest, so that it never costs the list.
    with tempfile.TemporaryDirectory() as tmp:
        saved = pathlib.Path(tmp)
        (saved / "capmetro.json").write_text(json.dumps([dict(source="capmetro", summary="Board of Directors",
                                                              start="2026-11-16T12:00:00", uid=board)]))
        (saved / "health.json").write_text("{}")
        assert unknown_meetings(listed, saved) == []
        moved = matching([dict(project="rail", meeting="capmetro-board-of-directors-20261117T1200@x")])
        assert moved[2] == [] and len(unknown_meetings(moved[0], saved)) == 1
        assert unknown_meetings(moved[0], saved)[0].startswith(
            "wrong_matches: no meeting on the calendars has the id 'capmetro-board-of-directors-20261117T1200@x' "
            "(given for rail).")

    # The page with meetings: a project with no schedule gets a section once a meeting names it.
    from caltools.model import Event
    listed = matching()[0]
    events = [Event(source="capmetro", summary="CapMetro: Board of Directors", start=date(2026, 11, 16), uid=board,
                    url="https://example.org/board",
                    description="Summary Agenda\n\nAction Items:\n- Construction of the Plaza Saltillo Rail Station "
                                "expansion\n\nMemos:\n- Red Line Trail Study update"),
              Event(source="capmetro", summary="CapMetro: Red Line Trail Study open house", start=date(2026, 12, 9))]
    tied = meetings.tie(listed, {"capmetro": events}, date(2026, 12, 1), {"capmetro": "CapMetro"})
    page = preview(listed, {"CapMetro": "CapMetro"}, tied=tied)
    assert "## Schedules and meetings" in page and "2 are named by a meeting on the calendars." in page
    assert ("**Past meetings:**\n- 16 Nov 2026 · [CapMetro: Board of Directors](https://example.org/board) · "
            "“Construction of the Plaza Saltillo Rail Station expansion”\n\n### Red Line Trail Study\n"
            "Plan · watching · CapMetro\n\n**Upcoming meetings:**\n- 9 Dec 2026 · CapMetro: Red Line Trail Study "
            "open house\n\n**Past meetings:**\n- 16 Nov 2026 ·") in page, page
    assert "| Name | Kind | Tier | Who | Monitored pages | Statements | Meetings | Links |\n|---|---|---|---|---|---|---|---|" in page
    assert "| Rail *(also called Rail and Trail Improvements)* | project | focus | CapMetro | 1 | 3 | 1 |  |" in page
    assert "| Red Line Trail Study | plan | watching | CapMetro |  |  | 2 |  |" in page
    assert "| **CapMetro** | **2** | **1** | **2** |" in page
    assert f"| Rail | 16 Nov 2026 | CapMetro: Board of Directors | Plaza Saltillo Rail Station expansion | " \
           f"`- {{project: rail, meeting: {board}}}` |" in page and page.endswith("|\n") and "\n\n\n" not in page
    assert "[[" not in page and "Open questions" not in page and "- **Meetings.**" in page
    assert "eeting" not in preview(listed, {"CapMetro": "CapMetro"})
    rejected = matching([dict(project="trail", meeting=board)])[0]
    assert len(meetings.tie(rejected, {"capmetro": events}, date(2026, 12, 1)).of(rejected[1])) == 1

    assert _options([]) == "" and _options(["--preview", "a b.md"]) == _options(["--preview=a b.md"]) == "a b.md"
    for wrong in (["--preview"], ["--preview="], ["--vault"]):
        try:
            _options(wrong)
            raise AssertionError(f"{wrong} should be refused")
        except ValueError:
            pass


def _options(argv: list[str]) -> str:
    """The file the command line asks the preview to be written to, "" for
    none: --preview FILE or --preview=FILE. Anything else is refused, so a
    mistyped option cannot pass for a run that did what was asked."""
    out, argv = "", list(argv)
    while argv:
        arg = argv.pop(0)
        if arg.startswith("--preview=") and arg != "--preview=":
            out = arg.split("=", 1)[1]
        elif arg == "--preview" and argv:
            out = argv.pop(0)
        else:
            raise ValueError(f"did not understand {arg!r}; usage: python -m caltools.projects [--preview FILE.md]")
    return out


def main(argv: list[str] | None = None) -> int:
    try:
        out = _options(sys.argv[1:] if argv is None else argv)
    except ValueError as e:
        print(e)
        return 2
    selftest()
    try:
        projects, org_names = load()
    except ValueError as e:
        print(e)
        return 1
    vault = pathlib.Path(os.environ.get("CAM_VAULT_ROOT") or pathlib.Path.home() / "Obsidian Sync" / "Changes Around Me")
    problems = unknown_meetings(projects) + (missing_pages(projects, vault) if vault.is_dir() else [])
    for problem in problems:
        print(problem)
    # Meetings are matched against the calendars as the last build saved them.
    tied = meetings.tie(projects, meetings.saved(DATA), date.today(), calendar_names())
    if out:
        out = pathlib.Path(out).expanduser()
        out.write_text(preview(projects, org_names, vault=True, today=date.today(), tied=tied), encoding="utf-8")
        print(f"wrote {out}")
    print(f"projects.yaml: {len(projects)} entries, {sum(bool(p['schedule']) for p in projects)} with a schedule, "
          f"{len({m.project['slug'] for m in tied.mentions})} named by a meeting"
          + ("" if vault.is_dir() else " (vault not here: pages not checked)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
