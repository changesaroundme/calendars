"""projects.yaml — what Changes Around Me follows: projects, plans and policies.

One entry per item on the list. An entry states only what is known about it;
`defaults` fills in the rest. The file holds what the site says about a
project as data, so pages, charts and (later) the map are generated from one
place, and Ian's own writing stays in the vault page an entry points to.

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
        schedule: [...]            # see below
        note: free text, never read by code

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
            note: why it is undated, and so on
            phases:
              - {name: Construction, said: "completion anticipated in 2026", end: 2026}

`said` is the wording as published. `start` and `end` are how it was read, as
YYYY, YYYY-MM or YYYY-MM-DD, each meaning "some time in that period"; a phase
with only one of them is a milestone. Vague wording is read the same way
every time: spring Mar–May, summer Jun–Aug, fall Sep–Nov, winter Dec–Feb;
early Jan–Apr, mid May–Aug, late Sep–Dec. A statement may turn out wrong —
agencies slip — and that is fine as long as it says who said it and when.

    python -m caltools.projects                    # check the file
    python -m caltools.projects --preview=FILE.md  # and write a readable page

parse() returns problems as strings naming the project by slug, so a bad
edit is findable without opening the file.
"""
from __future__ import annotations

import calendar
import os
import pathlib
import re
import sys
from datetime import date, datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects.yaml"

KIND = {"project", "plan", "policy"}
TIER = {"focus", "watching"}
TOP_KEYS = {"defaults", "organizations", "projects"}
DEFAULT_KEYS = {"tier", "added"}
PROJECT_KEYS = {"slug", "name", "kind", "tier", "orgs", "page", "sources", "links",
                "also_called", "schedule", "added", "note"}
STATEMENT_KEYS = {"source", "url", "dated", "retrieved", "capture", "about", "note", "phases"}
PHASE_KEYS = {"name", "said", "start", "end"}
SLUG_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
WHEN_RE = re.compile(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")


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
    year, month, day = int(m[1]), m[2] and int(m[2]), m[3] and int(m[3])
    if day:
        return date(year, month, day), date(year, month, day)
    if month:
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    return date(year, 1, 1), date(year, 12, 31)


def parse(doc, org_codes, source_slugs) -> tuple[list[dict], dict[str, str], list[str]]:
    """projects.yaml -> (projects, the organisations it adds, problems). Every
    project comes back with every field present — strings and lists, never
    None — and defaults applied, so nothing downstream has to ask."""
    if not isinstance(doc, dict):
        return [], {}, ["projects.yaml: top level must be a mapping"]
    problems = [f"projects.yaml: unknown top-level key {k!r}" for k in sorted(set(doc) - TOP_KEYS)]
    defaults = doc.get("defaults") or {}
    problems += [f"defaults: unknown key {k!r}" for k in sorted(set(defaults) - DEFAULT_KEYS)]
    extra = {str(code): _text(name) for code, name in (doc.get("organizations") or {}).items()}
    for code, name in extra.items():
        if code in org_codes:
            problems.append(f"organizations: {code} is already in sources.yaml")
        if not name:
            problems.append(f"organizations: {code} has no name")
    known_orgs = set(org_codes) | set(extra)

    projects, slugs, names = [], set(), set()
    for i, raw in enumerate(doc.get("projects") or []):
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

        for key in sorted(set(raw) - PROJECT_KEYS):
            bad(f"unknown key {key!r}")
        p = dict(slug=slug, name=_text(raw.get("name")), kind=_text(raw.get("kind")),
                 tier=_text(raw.get("tier") or defaults.get("tier")), page=_text(raw.get("page")),
                 added=_text(raw.get("added") or defaults.get("added")), note=_text(raw.get("note")),
                 orgs=[_text(o) for o in items("orgs")], sources=[_text(s) for s in items("sources")],
                 also_called=[_text(a) for a in items("also_called")], links=[], schedule=[])
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
                bad(f"unknown organisation {org!r}")
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

            for key in sorted(set(st) - STATEMENT_KEYS):
                sbad(f"unknown key {key!r}")
            s = {k: _text(st.get(k)) for k in STATEMENT_KEYS - {"phases"}}
            s["phases"] = []
            if not s["source"]:
                sbad("no source — who said it?")
            for key in ("url", "capture"):
                if s[key] and not s[key].startswith(("http://", "https://")):
                    sbad(f"{key} must be an http(s) address")
            for key in ("dated", "retrieved"):
                try:
                    s[key] and period(s[key])
                except ValueError as e:
                    sbad(f"{key}: {e}")
                    s[key] = ""
            if not (s["dated"] or s["retrieved"] or s["note"]):
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
                for key in sorted(set(ph) - PHASE_KEYS):
                    sbad(f"phase has unknown key {key!r}")
                phase = {k: _text(ph.get(k)) for k in PHASE_KEYS}
                if not phase["name"]:
                    sbad("a phase has no name")
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
    return projects, extra, problems


def load(path: pathlib.Path = PROJECTS) -> tuple[list[dict], dict[str, str]]:
    """(projects, organisation code -> name). Any problem raises: a list that
    does not check out should not be published."""
    import yaml
    from caltools import registry
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    known = {code: org.get("name", code) for code, org in registry.orgs().items()}
    projects, extra, problems = parse(doc, set(known), {row["slug"] for row in registry.rows()})
    if problems:
        raise ValueError("\n".join(problems))
    return projects, {**known, **extra}


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


def slippage(project: dict) -> list[dict]:
    """How far each phase has moved: for a phase's start and for its end, the
    first statement that dates it against the latest one, in whole months
    (positive = later). A start is compared by the first day its wording can
    mean and an end by the last, so "2025" against "2029-01" is 37 months."""
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
                a, b = period(first)[edge == "end"], period(latest)[edge == "end"]
                months = (b.year - a.year) * 12 + b.month - a.month
                if months:
                    moved.append(dict(about=about, phase=name, edge=edge, first=first, latest=latest,
                                      months=months, first_source=first_st["source"],
                                      latest_source=latest_st["source"]))
    return moved


def _label(text: str) -> str:
    return re.sub(r"[:;#]", ",", text)          # these end a Mermaid task name


def gantt(project: dict) -> str:
    """A Mermaid Gantt chart of the current schedule — the latest statement of
    each group — or "" when the project has none."""
    rows, n, by_group = [], 0, groups(project)
    for about, statements in by_group.items():
        if about or len(by_group) > 1:
            rows.append(f"    section {_label(about or 'Whole project')}")
        for ph in statements[-1]["phases"]:
            n += 1
            if ph["start"] and ph["end"]:
                rows.append(f"    {_label(ph['name'])} :p{n}, {period(ph['start'])[0]}, {period(ph['end'])[1]}")
            else:
                day = period(ph["start"])[0] if ph["start"] else period(ph["end"])[1]
                rows.append(f"    {_label(ph['name'])} :milestone, p{n}, {day}, 0d")
    if not n:
        return ""
    return "\n".join(["```mermaid", "gantt", f"    title {_label(project['name'])}",
                      "    dateFormat YYYY-MM-DD", "    axisFormat %Y", *rows, "```"])


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


def _when(statement: dict) -> str:
    if statement["dated"]:
        return f"dated {statement['dated']}"
    return f"read {statement['retrieved']}" if statement["retrieved"] else "undated"


def _read_as(phase: dict) -> str:
    if phase["start"] and phase["end"]:
        return f"during {phase['start']}" if phase["start"] == phase["end"] else f"{phase['start']} to {phase['end']}"
    return f"starting {phase['start']}" if phase["start"] else f"by the end of {phase['end']}"


def preview(projects: list[dict], org_names: dict[str, str], today: date) -> str:
    """The list as a page Ian can read in Obsidian: open questions, then every
    project with a schedule (chart, statements, slippage), then a table of all."""
    scheduled = [p for p in projects if p["schedule"]]
    out = ["> [!info] Generated page",
           f"> Made from `projects.yaml` in the `calendars` repo on {today.isoformat()} by "
           "`python -m caltools.projects --preview=…`. Edits made here are overwritten: "
           "change the file, or tell Claude what to change.", "",
           f"{len(projects)} on the list: {sum(p['tier'] == 'focus' for p in projects)} Focus, "
           f"{sum(p['tier'] == 'watching' for p in projects)} watching. "
           f"{len(scheduled)} have a schedule recorded.", ""]
    questions = gaps(projects)
    if questions:
        out += ["## Open questions", *[f"- {q}" for q in questions], ""]
    out += ["## Schedules", "Each chart shows the latest statement. Underneath is everything that has been "
            "said, oldest first, and how far each phase has moved between the first and the latest.", ""]
    for p in scheduled:
        who = ", ".join(p["orgs"]) or "organization not recorded"
        out += [f"### {p['name']}", f"{p['kind'].capitalize()} · {p['tier']} · {who}"
                + (f" · [[{p['page']}|page]]" if p["page"] else ""), "", gantt(p), ""]
        for st in p["schedule"]:
            where = [f"[source]({st['url']})"] if st["url"] else []
            where += [f"[our copy]({st['capture']})"] if st["capture"] else []
            head = f"- **{_when(st).capitalize()}.** {st['source']}" + (f" ({', '.join(where)})" if where else "")
            out.append(head + (f" — about: {st['about']}" if st["about"] else "")
                       + (f" *{st['note']}*" if st["note"] else ""))
            for ph in st["phases"]:
                out.append(f"    - {ph['name']}: " + (f"“{ph['said']}” " if ph["said"] else "")
                           + f"(read as {_read_as(ph)})")
        moved = slippage(p)
        if moved:
            out += ["", "**Moved:**"]
            for m in moved:
                later = f"{abs(m['months'])} months {'later' if m['months'] > 0 else 'earlier'}"
                out.append(f"- {m['phase']}{' (' + m['about'] + ')' if m['about'] else ''}, {m['edge']}: "
                           f"{m['first']} → {m['latest']}, {later}.")
        out.append("")
    out += ["## Everything on the list", "", "| Name | Kind | Tier | Who | Monitored pages | Statements |",
            "|---|---|---|---|---|---|"]
    for p in sorted(projects, key=lambda p: (p["tier"] != "focus", p["name"].lower())):
        out.append(f"| {p['name']} | {p['kind']} | {p['tier']} | {', '.join(p['orgs']) or '?'} | "
                   f"{len(p['sources']) or ''} | {len(p['schedule']) or ''} |")
    codes = sorted({o for p in projects for o in p["orgs"]})
    out += ["", "Who: " + "; ".join(f"{c} = {org_names.get(c, c)}" for c in codes) + "."]
    return "\n".join(out) + "\n"


def selftest() -> None:
    """Prove the checks catch what a hand edit most plausibly breaks, and that
    slippage and the chart read a schedule the way the docstring says."""
    assert period("2027") == (date(2027, 1, 1), date(2027, 12, 31))
    assert period("2028-02") == (date(2028, 2, 1), date(2028, 2, 29))
    assert period("2027-01-10") == (date(2027, 1, 10), date(2027, 1, 10))
    for wrong in ("27", "2027-13", "2027-1", "early 2027"):
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

    projects, extra, none = parse(doc(), {"CapMetro"}, {"capmetro-rail"})
    assert none == [] and extra == {"SGI": "Something Good"}, none
    rail = projects[0]
    assert rail["added"] == "2026-11-16" and rail["schedule"][2]["dated"] == "2026-03-10"
    assert rail["schedule"][0]["phases"][0]["start"] == "2025"            # numbers become strings
    assert [(m["edge"], m["months"]) for m in slippage(rail)] == [("start", 24), ("end", 37)]
    chart = gantt(rail)
    assert "    Construction :p1, 2027-01-01, 2029-01-31" in chart, chart
    assert "    Opening :milestone, p2, 2029-02-01, 0d" in chart, chart
    assert gantt(dict(rail, schedule=[])) == ""
    assert "37 months later" in preview(projects, {"CapMetro": "CapMetro"}, date(2026, 11, 16))

    for changes, expect in [
            (dict(slug="Rail Line"), "slug must be"), (dict(kind="study"), "kind must be"),
            (dict(tier="focused"), "tier must be"), (dict(page=""), "needs a page"),
            (dict(page="Focuses/Rail.md"), "without .md"), (dict(orgs=["Capmetro"]), "unknown organisation"),
            (dict(sources=["capmetro-rial"]), "not in sources.yaml"), (dict(orgs="CapMetro"), "must be a list"),
            (dict(colour="red"), "unknown key 'colour'"), (dict(links=[dict(name="x", url="example.org")]), "http(s) url"),
            (dict(schedule=[dict(phases=[dict(name="C", start=2027)], dated=2026)]), "no source"),
            (dict(schedule=[dict(source="s", phases=[dict(name="C", start=2027)])]), "undated"),
            (dict(schedule=[dict(source="s", dated=2026, phases=[])]), "no phases"),
            (dict(schedule=[dict(source="s", dated=2026, phases=[dict(name="C")])]), "neither start nor end"),
            (dict(schedule=[dict(source="s", dated=2026, phases=[dict(name="C", start=2028, end=2027)])]), "ends before"),
            (dict(schedule=[dict(source="s", dated=2026, phases=[dict(name="C", start="early 2027")])]), "is not YYYY"),
            (dict(schedule=[dict(source="s", dated=2026, phases=[dict(name="C", start=2027)]),
                            dict(source="t", dated=2025, phases=[dict(name="C", start=2028)])]), "oldest first")]:
        found = problems(**changes)
        assert any(expect in p for p in found), (changes, found)
    two = doc()
    two["projects"].append(dict(two["projects"][0]))
    assert any("used twice" in p for p in parse(two, {"CapMetro"}, {"capmetro-rail"})[2])
    assert any("already in sources.yaml" in p for p in parse(doc(), {"CapMetro", "SGI"}, {"capmetro-rail"})[2])


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    selftest()
    try:
        projects, org_names = load()
    except (OSError, ValueError) as e:
        print(e)
        return 1
    vault = pathlib.Path(os.environ.get("CAM_VAULT_ROOT") or pathlib.Path.home() / "Obsidian Sync" / "Changes Around Me")
    problems = missing_pages(projects, vault) if vault.is_dir() else []
    for problem in problems:
        print(problem)
    for arg in argv:
        if arg.startswith("--preview="):
            out = pathlib.Path(arg.split("=", 1)[1]).expanduser()
            out.write_text(preview(projects, org_names, date.today()), encoding="utf-8")
            print(f"wrote {out}")
    print(f"projects.yaml: {len(projects)} entries, {sum(bool(p['schedule']) for p in projects)} with a schedule"
          + ("" if vault.is_dir() else " (vault not here: pages not checked)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
