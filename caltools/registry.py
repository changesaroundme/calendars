"""sources.yaml — the registry of every page this project monitors.

Grouped by organisation, one mapping per page, child pages nested under
their parent; per-organisation and file-wide `defaults` mean a page states
only what is unusual about it. load() flattens that into one dict per page
(the "rows" the build, the archive script and the sources page consume),
with `org` and `parent` filled in from the structure and every value a
string. The file holds current state only: git is the change history, and
runtime state (last checked, last changed, captures) lives in generated
files.

    defaults:                                # file-wide; an organisation may override
      status: active
      calendar-pages: {expect: always, check: twice daily, archive: no}   # pages with `calendar:`
      other-pages: {expect: sporadic, check: every 3 days, archive: yes}  # everything else
    organizations:
      CapMetro:
        name: Capital Metropolitan Transportation Authority (CapMetro)
        page: Organizations/CapMetro           # vault page (Links/Archive embeds)
        defaults: {parse: html}                # for this organisation's pages
        pages:
          - slug: capmetro-service-changes
            name: Service changes
            url: https://www.capmetro.org/servicechange
            added: 2026-09-21
            note: free text, never read by code
            children:
              - {slug: ..., name: ..., url: ..., added: ...}

validate() returns problems as strings naming the page by slug so a bad
edit is findable from the CI log without opening the file.

Tracker is the CI half of docs/status.json: it listens to every response
the build's requests.Session receives, maps the URL back to a registry
slug, and records when that page was last fetched and when its body last
differed from the previous build. No adapter knows it exists. The Mac
archive script keeps its own capture times in its own file; the generated
sources page merges the two, so the two writers never touch one file.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from datetime import date, datetime, timezone
from urllib.parse import urlparse

COLUMNS = ["slug", "org", "name", "url", "calendar", "expect", "check",
           "archive", "parse", "status", "public", "parent", "added", "note"]
PAGE_KEYS = set(COLUMNS) - {"org", "parent"} | {"children"}
DEFAULTABLE = ["expect", "check", "archive", "parse", "status", "public"]
# A page with `calendar:` is fetched by the build; one without exists to be
# archived. `defaults` may set a value for all pages, or per kind:
KINDS = {"calendar-pages": True, "other-pages": False}
ORG_KEYS = {"name", "page", "defaults", "pages"}
PARSE = {"ics", "rss", "api", "html", "html-table", "pdf", "claude",
         "manual", "none"}
STATUS = {"active", "paused", "retired"}
YESNO = {"yes", "no"}
SLUG_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
# expect grammar (one cell, hand-typed):
#   always | sporadic
#   every <N> year[s] [from <YYYY>] <Mon>[-<Mon>]        every 1 year Jun-Aug · every 2 years from 2025 Jan-May
#   every <N> month[s] from <Mon> [<D> month[s] long]    every 3 months from Jan · every 2 months from Feb 2 months long
#   every <N> quarter[s] [from Q<q> <YYYY>]              every 2 quarters from Q1 2026 (window: the whole quarter; from required for N > 1)
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
EXPECT_YEAR_RE = re.compile(
    rf"every (\d+) years?(?: from (20\d\d))? {MON}(?:-{MON})?", re.IGNORECASE)
EXPECT_MONTH_RE = re.compile(
    rf"every (\d+) months? from {MON}(?: (\d+) months? long)?", re.IGNORECASE)
EXPECT_QUARTER_RE = re.compile(r"every (\d+) quarters?(?: from Q([1-4]) (20\d\d))?", re.IGNORECASE)


def parse_expect(expect: str):
    """-> None (invalid) | ("always",) | ("sporadic",) | ("year", n, from_year|None, a, b)
    | ("month", n, anchor, length) | ("quarter", n, anchor_index|None). Months are 1-12;
    a quarter index is year*4 + quarter-1."""
    e = (expect or "").strip()
    if e in ("always", "sporadic"):
        return (e,)
    if m := EXPECT_YEAR_RE.fullmatch(e):
        n, yr, a, b = int(m.group(1)), m.group(2), m.group(3), m.group(4) or m.group(3)
        if n < 1:
            return None
        return ("year", n, int(yr) if yr else None, MONTHS.index(a.lower()) + 1, MONTHS.index(b.lower()) + 1)
    if m := EXPECT_MONTH_RE.fullmatch(e):
        n, anchor, length = int(m.group(1)), MONTHS.index(m.group(2).lower()) + 1, int(m.group(3) or 1)
        return ("month", n, anchor, length) if 1 <= length <= n else None
    if m := EXPECT_QUARTER_RE.fullmatch(e):
        n = int(m.group(1))
        if n < 1:
            return None
        anchor = int(m.group(3)) * 4 + int(m.group(2)) - 1 if m.group(2) else None
        return ("quarter", n, anchor)
    return None


ROOT = pathlib.Path(__file__).resolve().parent.parent
_ROWS: list[dict[str, str]] | None = None


REGISTRY = ROOT / "sources.yaml"
_ORGS: dict[str, dict[str, str]] = {}          # code -> {name, page}, in file order


def _text(v) -> str:
    """Every cell is a string downstream; YAML turns yes/no into booleans and
    2026-09-21 into a date, so undo that."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return str(v)


def parse(doc: dict) -> tuple[list[dict[str, str]], dict[str, dict[str, str]], list[str]]:
    """The YAML document -> (rows, orgs, structural problems). Rows are one
    flat dict per page in file order; a problem here means the file's shape
    is wrong (validate() then judges the values)."""
    rows, orgs, problems = [], {}, []
    if not isinstance(doc, dict):
        return rows, orgs, ["sources.yaml: top level must be a mapping"]
    for key in doc:
        if key not in ("defaults", "organizations"):
            problems.append(f"sources.yaml: unknown top-level key {key!r}")
    file_defaults = doc.get("defaults") or {}
    for code, org in (doc.get("organizations") or {}).items():
        org = org or {}
        for key in org:
            if key not in ORG_KEYS:
                problems.append(f"{code}: unknown key {key!r} (expected {sorted(ORG_KEYS)})")
        orgs[code] = {"name": _text(org.get("name")) or code, "page": _text(org.get("page"))}
        layers = [file_defaults, org.get("defaults") or {}]
        for layer in layers:
            for bad in [k for k in layer if k not in DEFAULTABLE and k not in KINDS]:
                problems.append(f"{code}: defaults cannot set {bad!r}")
            for kind in KINDS:
                for bad in [k for k in (layer.get(kind) or {}) if k not in DEFAULTABLE]:
                    problems.append(f"{code}: defaults.{kind} cannot set {bad!r}")

        def page_row(p, parent: str, n: int) -> dict[str, str]:
            if not isinstance(p, dict):
                problems.append(f"{code} page #{n}: not a mapping")
                return {}
            tag = _text(p.get("slug")) or f"{code} page #{n}"
            for key in p:
                if key not in PAGE_KEYS:
                    problems.append(f"{tag}: unknown key {key!r}")
            kind = "calendar-pages" if p.get("calendar") else "other-pages"
            defaults: dict = {}
            for layer in layers:                 # file then organisation; all-pages then this kind
                defaults.update({k: v for k, v in layer.items() if k in DEFAULTABLE})
                defaults.update(layer.get(kind) or {})
            row = {c: _text(p.get(c, defaults.get(c))) for c in COLUMNS}
            row.update(org=code, parent=parent)
            return row

        for n, p in enumerate(org.get("pages") or [], 1):
            row = page_row(p, "", n)
            if not row:
                continue
            rows.append(row)
            for m, c in enumerate(p.get("children") or [], 1):
                child = page_row(c, row["slug"], m)
                if child:
                    if c.get("children"):
                        problems.append(f"{child['slug']}: children cannot have children")
                    rows.append(child)
    return rows, orgs, problems


def load(path: pathlib.Path = REGISTRY) -> list[dict[str, str]]:
    """The registry as flat rows. Structural problems raise, since nothing
    downstream can work with a misshapen file; value problems are validate()'s."""
    import yaml
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    rows, orgs, problems = parse(doc)
    if problems:
        raise ValueError("; ".join(problems))
    _ORGS.clear()
    _ORGS.update(orgs)
    return rows


def rows() -> list[dict[str, str]]:
    """The registry, loaded once per process (adapters ask about their own slugs)."""
    global _ROWS
    if _ROWS is None:
        try:
            _ROWS = load(REGISTRY)
        except (OSError, ValueError):
            _ROWS = []
    return _ROWS


def orgs() -> dict[str, dict[str, str]]:
    """Organisation code -> {name, page} in file order (the display order)."""
    if not _ORGS:
        rows()
    return dict(_ORGS)


def expected_now(expect: str, today: date) -> bool:
    """Is content expected on this page right now, so that an empty page is a
    finding rather than the off-season? See parse_expect for the grammar.
    Year windows are inclusive and may wrap the year end (Nov-Jan belongs to
    the year it starts in); `every N years` counts from `from` (or any year
    when no anchor is given, though the validator requires one for N > 1)."""
    spec = parse_expect(expect)
    if spec is None:
        return True                              # unknown: keep the old behaviour
    kind = spec[0]
    if kind in ("always", "sporadic"):
        return kind == "always"
    if kind == "month":
        _, n, anchor, length = spec
        return (today.month - anchor) % n < length
    if kind == "quarter":
        _, n, anchor = spec
        this_q = today.year * 4 + (today.month - 1) // 3
        return anchor is None or (this_q - anchor) % n == 0
    _, n, start_year, a, b = spec
    if a <= b:
        in_window, window_year = a <= today.month <= b, today.year
    else:                                        # wraps: Nov-Jan
        in_window = today.month >= a or today.month <= b
        window_year = today.year if today.month >= a else today.year - 1
    if not in_window:
        return False
    return start_year is None or (window_year - start_year) % n == 0


def expected(slug: str, today: date | None = None) -> bool:
    """expected_now() for a registry slug; an unregistered slug is `always`."""
    row = next((r for r in rows() if r["slug"] == slug), None)
    return expected_now(row["expect"] if row else "always", today or date.today())


def calendar_expected(calendar: str, today: date | None = None) -> bool:
    """Should this calendar have events right now? True when any active
    registry page feeding it is in its `expect` window — build.py's
    zero-events and no-future-events checks stay quiet otherwise (the
    Legislature between sessions, TPSC between posts). A calendar with no
    registry rows is treated as always expected."""
    today = today or date.today()
    pages = [r for r in rows() if r["calendar"] == calendar and r["status"] == "active"]
    return not pages or any(expected_now(r["expect"], today) for r in pages)


def parse_mode(slug: str) -> str:
    """The registry's `parse` value for a slug ('' when unregistered). Adapters
    use it to stand down from a page whose events are curated by hand
    (`claude`, `manual`) while still fetching it for the Checked stamp."""
    row = next((r for r in rows() if r["slug"] == slug), None)
    return row["parse"] if row else ""


def validate(rows: list[dict[str, str]], calendars: set[str],
             today: date | None = None) -> list[str]:
    """Return every problem found, or [] when the registry is clean."""
    today = today or date.today()
    problems: list[str] = []
    if not rows:
        return ["sources.yaml: no pages"]
    missing = [c for c in COLUMNS if c not in rows[0]]
    if missing:
        return [f"sources.yaml: rows lack {missing}"]

    slugs = [r["slug"] for r in rows]
    seen_slug: dict[str, str] = {}
    seen_url: dict[str, str] = {}
    known_orgs = set(_ORGS) | {r["org"] for r in rows}
    for i, r in enumerate(rows, start=1):
        tag = f"page {i} ({r['slug'] or '?'})"

        def bad(msg: str) -> None:
            problems.append(f"{tag}: {msg}")

        for k, v in r.items():
            if k != "note" and ("\n" in v or "\r" in v):
                bad(f"{k} spans lines")
            if v != v.strip():
                bad(f"{k} has leading/trailing whitespace")
        if not SLUG_RE.fullmatch(r["slug"]):
            bad("slug must be kebab-case a-z0-9")
        if r["slug"] in seen_slug:
            bad(f"duplicate slug (also {seen_slug[r['slug']]})")
        seen_slug.setdefault(r["slug"], tag)
        if r["org"] not in known_orgs:
            bad(f"unknown org {r['org']!r}")
        if not r["name"]:
            bad("name is empty")
        u = urlparse(r["url"])
        if u.scheme != "https" or not u.netloc or " " in r["url"]:
            bad(f"bad url {r['url']!r}")
        if r["url"] in seen_url:
            bad(f"duplicate url (also {seen_url[r['url']]})")
        seen_url.setdefault(r["url"], tag)
        if r["calendar"] and r["calendar"] not in calendars:
            bad(f"unknown calendar {r['calendar']!r}")
        spec = parse_expect(r["expect"])
        if spec is None:
            bad(f"expect must be always / sporadic / every N year(s) [from YYYY] Mon[-Mon] / "
                f"every N month(s) from Mon [D months long] / every N quarter(s) [from Qn YYYY], got {r['expect']!r}")
        elif spec[0] == "year" and spec[1] > 1 and spec[2] is None:
            bad(f"expect 'every {spec[1]} years' needs 'from <year>' to say which years")
        elif spec[0] == "quarter" and spec[1] > 1 and spec[2] is None:
            bad(f"expect 'every {spec[1]} quarters' needs 'from Q<n> <year>' to say which quarters")
        if not r["check"]:
            bad("check is empty")
        if r["archive"] not in YESNO:
            bad("archive must be yes/no")
        if r["parse"] not in PARSE:
            bad(f"unknown parse {r['parse']!r}")
        if r["status"] not in STATUS:
            bad(f"unknown status {r['status']!r}")
        if r["public"] not in YESNO:
            bad("public must be yes/no")
        if r["parent"]:
            if r["parent"] not in slugs:
                bad(f"unknown parent {r['parent']!r}")
            elif r["parent"] == r["slug"]:
                bad("parent is itself")
        try:
            added = date.fromisoformat(r["added"])
            if added > today:
                bad(f"added {r['added']} is in the future")
        except ValueError:
            bad(f"added must be YYYY-MM-DD, got {r['added']!r}")
        # Coherence: a row must be fetched, archived, or explicitly manual.
        if r["calendar"] and r["parse"] in {"none", "manual"}:
            bad(f"calendar {r['calendar']!r} set but parse is {r['parse']}")
        if not r["calendar"] and r["archive"] == "no" and r["parse"] != "manual":
            bad("row is neither fetched nor archived (parse would need to be manual)")
    return problems


def selftest() -> None:
    """Prove the validator catches what a grid edit most plausibly breaks."""
    good = dict(zip(COLUMNS, [
        "coa-planning-commission", "CoA", "Planning Commission",
        "https://www.austintexas.gov/boards-commissions/board/planning-commission",
        "austin", "always", "twice daily", "no", "html", "active", "yes", "",
        "2026-07-25", ""]))
    cals = {"austin"}
    assert validate([good], cals) == [], validate([good], cals)
    dup = dict(good, url="https://example.com/x")
    bad_url = dict(good, slug="coa-x", url="austintexas.gov/no-scheme")
    orphan = dict(good, slug="coa-y", url="https://example.com/y",
                  calendar="", archive="no")
    future = dict(good, slug="coa-z", url="https://example.com/z",
                  added="2999-01-01")
    probs = validate([good, dup, bad_url, orphan, future], cals,
                     today=date(2026, 9, 8))
    hits = [p for p in probs if "duplicate slug" in p]
    assert hits and hits[0].startswith("page 2 ") and "page 1 " in hits[0], probs
    assert any("bad url" in p and p.startswith("page 3 (coa-x)") for p in probs), probs
    assert any("neither fetched nor archived" in p and "(coa-y)" in p for p in probs), probs
    assert any("in the future" in p and "(coa-z)" in p for p in probs), probs
    # The YAML shape: defaults layer (file < organisation < page), children
    # inherit org and parent, yes/no and dates come back as text, and a
    # misplaced key is a structural problem.
    doc = {"defaults": {"status": "active", "public": True, "parse": "none",
                        "calendar-pages": {"expect": "always", "check": "twice daily", "archive": False},
                        "other-pages": {"expect": "sporadic", "check": "every 3 days", "archive": True}},
           "organizations": {"CoA": {"name": "City of Austin (CoA)", "page": "Organizations/City of Austin",
                                     "defaults": {"calendar-pages": {"parse": "html"}},
                                     "pages": [{"slug": "coa-a", "name": "A", "url": "https://x.gov/a",
                                                "added": date(2026, 9, 21), "calendar": "austin",
                                                "children": [{"slug": "coa-a-1", "name": "A1",
                                                              "url": "https://x.gov/a/1", "added": "2026-09-21",
                                                              "note": "kept\nas is"}]}]}}}
    rs, og, pr = parse(doc)
    assert pr == [], pr
    assert [r["slug"] for r in rs] == ["coa-a", "coa-a-1"]
    assert rs[0]["archive"] == "no" and rs[0]["parse"] == "html" and rs[0]["check"] == "twice daily"
    assert rs[1]["archive"] == "yes" and rs[1]["parse"] == "none" and rs[1]["public"] == "yes"
    assert rs[1]["parent"] == "coa-a" and rs[1]["org"] == "CoA" and rs[1]["expect"] == "sporadic"
    assert rs[0]["added"] == "2026-09-21" and rs[1]["note"] == "kept\nas is"
    assert og == {"CoA": {"name": "City of Austin (CoA)", "page": "Organizations/City of Austin"}}
    assert validate(rs, cals) == [], validate(rs, cals)
    _, _, pr = parse({"organizations": {"CoA": {"pages": [{"slug": "x", "runner": "ci"}]}}})
    assert any("unknown key 'runner'" in p for p in pr), pr
    # Seasonal expectation windows.
    assert expected_now("always", date(2026, 3, 1))
    assert not expected_now("sporadic", date(2026, 3, 1))
    assert expected_now("every 1 year Jun-Aug", date(2026, 7, 4))
    assert not expected_now("every 1 year Jun-Aug", date(2026, 9, 16))
    assert expected_now("every 1 year Jan", date(2026, 1, 20)) and not expected_now("every 1 year Jan", date(2026, 2, 1))
    assert expected_now("every 1 year Nov-Jan", date(2027, 1, 5)) and not expected_now("every 1 year Nov-Jan", date(2026, 6, 1))
    leg = "every 2 years from 2025 Jan-May"          # Texas Legislature: odd years
    assert expected_now(leg, date(2025, 3, 1)) and expected_now(leg, date(2027, 1, 20))
    assert not expected_now(leg, date(2026, 3, 1)) and not expected_now(leg, date(2025, 9, 1))
    assert expected_now("every 2 years from 2025 Nov-Jan", date(2026, 1, 10))   # window started in 2025
    assert not expected_now("every 2 years from 2025 Nov-Jan", date(2027, 1, 10))
    q = "every 2 quarters from Q1 2026"               # Q1 2026, Q3 2026, Q1 2027 ...
    assert expected_now(q, date(2026, 2, 1)) and expected_now(q, date(2026, 8, 31)) and expected_now(q, date(2027, 1, 1))
    assert not expected_now(q, date(2026, 5, 1)) and not expected_now(q, date(2026, 12, 31))
    assert expected_now("every 3 quarters from Q4 2025", date(2026, 7, 15)) and not expected_now("every 3 quarters from Q4 2025", date(2026, 4, 15))
    assert expected_now("every 1 quarter", date(2026, 6, 1))                   # every quarter = always
    assert any("needs 'from Q" in p for p in validate([dict(good, expect="every 2 quarters")], cals))
    assert parse_expect("quarterly") is None
    assert expected_now("every 2 months from Feb 2 months long", date(2026, 3, 1))
    assert not expected_now("every 6 months from Jan", date(2026, 4, 1))
    assert any("needs 'from" in p for p in validate([dict(good, expect="every 2 years Jan-May")], cals))
    assert any("expect must be" in p for p in validate([dict(good, expect="annual Jun-Aug")], cals))
    assert parse_expect("every 3 months from Jan 4 months long") is None      # window longer than period
    # URL matching: exact, query/path extensions, longest wins, no false prefix.
    rows = [dict(good, slug="a", url="https://x.gov/a"),
            dict(good, slug="a-b", url="https://x.gov/a/b"),
            dict(good, slug="api", url="https://api.x.gov/v1/events"),
            dict(good, slug="html", url="https://x.gov/c.html")]
    assert match_slug("https://x.gov/a/", rows) == "a"
    assert match_slug("https://X.gov/a/b?year=2026", rows) == "a-b"
    assert match_slug("https://api.x.gov/v1/events?$filter=1", rows) == "api"
    assert match_slug("https://x.gov/c.html/sub", rows) == "html"
    assert match_slug("https://x.gov/c.htmlx", rows) is None
    assert match_slug("https://x.gov/ab", rows) is None
    # Per-request noise must not read as a change; real edits still must.
    # One pair per _NOISE pattern, lifted from the sites' actual markup.
    noise = [
        (b'<input name="__VIEWSTATE" id="v" value="AAA" /><p>same</p>',
         b'<input name="__VIEWSTATE" id="v" value="BBB" /><p>same</p>'),
        (b'<link href="Feed.ashx?M=Calendar&amp;ID=44783118&amp;GUID=3feb264b-2018-4eb2-a7e8-ae002aa49398"',
         b'<link href="Feed.ashx?M=Calendar&amp;ID=44783122&amp;GUID=224d659d-383d-4d43-9474-d53e641e8300"'),
        (b"&quot;Alerts.aspx?M=CA&amp;ID=44783118&amp;GUID=3feb264b-2018-4eb2-a7e8-ae002aa49398&quot;",
         b"&quot;Alerts.aspx?M=CA&amp;ID=44783122&amp;GUID=224d659d-383d-4d43-9474-d53e641e8300&quot;"),
        (b"const sessionId = 'fb2evltlow0b3nnacxzmfw0n';",
         b"const sessionId = '3z3dgrk55quil20e0j3uoe30';"),
        (b'data-links="gov.tx.txdot.cmd.reImagine.core.models.FooterItem@31f60766,gov.tx.txdot.cmd.reImagine.core.models.FooterItem@3d3f1ae6"',
         b'data-links="gov.tx.txdot.cmd.reImagine.core.models.FooterItem@3dbc0fc5,gov.tx.txdot.cmd.reImagine.core.models.FooterItem@687e8fb7"'),
        (b'<script src="/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd425f7e032f3&ns=2&cb=130436041" async></script>',
         b'<script src="/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd425f7e032f3&ns=3&cb=701614767" async></script>'),
        (b"d.innerHTML=\"window.__CF$cv$params={r:'a3d55717ae51c303',t:'MTc4OTc4NjgyNw=='}\"",
         b"d.innerHTML=\"window.__CF$cv$params={r:'a3d55263ee1c303',t:'MTc4OTc4NjgyOQ=='}\""),
        (b'var wpdm_js = {"client_id":"b80b3b982f16cc12fdccac361a4b9c05","color_scheme":"system"};',
         b'var wpdm_js = {"client_id":"df3105383208244e4fa2f42c13025969","color_scheme":"system"};'),
        (b'"ajaxurl":"https:\\/\\/www.lcra.org\\/wp-admin\\/admin-ajax.php","nonce":"612be2e9df","preview":false',
         b'"ajaxurl":"https:\\/\\/www.lcra.org\\/wp-admin\\/admin-ajax.php","nonce":"a1b2c3d4e5","preview":false'),
        (b"admin-ajax.php?action=x&_wpnonce=612be2e9df\"", b"admin-ajax.php?action=x&_wpnonce=a1b2c3d4e5\""),
        (b"BEGIN:VEVENT\r\nDTSTAMP:20260918T215951\r\nCREATED:20251124T190855Z\r\n",
         b"BEGIN:VEVENT\r\nDTSTAMP:20260919T101502\r\nCREATED:20251124T190855Z\r\n"),
    ]
    for a, b in noise:
        assert body_hash(a) == body_hash(b), (a, b)
    assert body_hash(noise[0][0]) != body_hash(b'<p>other</p>')
    # Content changes beside the noise still register.
    assert body_hash(b"DTSTAMP:20260918T215951\r\nSUMMARY:Board\r\n") != \
        body_hash(b"DTSTAMP:20260918T215951\r\nSUMMARY:Board (moved)\r\n")
    assert body_hash(b'"nonce":"612be2e9df","postId":350') != body_hash(b'"nonce":"612be2e9df","postId":351')
    # Email addresses are not Java identity hashes.
    assert normalize(b"mail first.last@cafe.org now") == b"mail first.last@cafe.org now"
    assert body_hash(b"x").startswith(HASH_SCHEME + ":")
    # Markup, scripts and attributes are not content; visible text is.
    assert body_hash(b'<html><body><p id="a1">Board meets <b>May 5</b></p><script>t=1</script></body></html>') == \
        body_hash(b'<!DOCTYPE html>\n<html><head><title>x</title></head><body><p id="z9">Board   meets <b>May 5</b></p><script>t=2</script></body></html>')
    assert body_hash(b'<html><body><p>Board meets May 5</p></body></html>') != \
        body_hash(b'<html><body><p>Board meets May 6</p></body></html>')
    assert visible_text(b'{"a": "<html>"}') == b'{"a": "<html>"}'
    # A hash from an older scheme (or a new page) clears changed and starts
    # since; a same-scheme difference stamps changed; the same hash keeps it.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        prior = pathlib.Path(td) / "old.json"
        prior.write_text(json.dumps({"sources": {
            "a": {"checked": "2026-01-01T00:00:00Z", "changed": "2025-12-01T00:00:00Z", "hash": "sha256v2:old"},
            "b": {"checked": "2026-01-01T00:00:00Z", "changed": "2025-12-01T00:00:00Z", "hash": body_hash(b"same")},
            "c": {"checked": "2026-01-01T00:00:00Z", "changed": "2025-12-01T00:00:00Z", "hash": body_hash(b"was")},
        }}))
        t = Tracker([])
        t.seen = {"a": body_hash(b"x"), "b": body_hash(b"same"), "c": body_hash(b"now")}
        doc = t.write(pathlib.Path(td) / "new.json", prior, datetime(2026, 2, 1, tzinfo=timezone.utc))
        a, b, c = (doc["sources"][k] for k in "abc")
        assert a["changed"] is None and a["since"] == "2026-02-01T00:00:00Z"
        assert b["changed"] == "2025-12-01T00:00:00Z" and b["since"] == "2025-12-01T00:00:00Z"
        assert c["changed"] == "2026-02-01T00:00:00Z"


# ---------------------------------------------------------------------------
# docs/status.json — per-slug "checked" / "changed", from the live session
# ---------------------------------------------------------------------------

# What counts as a page change. An HTML page is hashed on its visible text
# only — scripts, styles, attributes and markup are dropped first — because
# raw-HTML hashing registered a "change" on most builds (Sep 2026: 36 of 51
# pages, from tokens in scripts and attributes no reader sees). Feeds (iCal,
# RSS, JSON) are hashed as served. Per-request noise that must not read as a
# page change in either is blanked by _NOISE. Each pattern was
# found by fetching the page twice and diffing (Sep 2026); the (site) note
# says where it came from. Blank the noise, hash what is left. HASH_SCHEME
# bumps whenever this hashing changes; the first build after a bump resets
# every page's baseline: "changed" is cleared and "since" set to that build
# (no change seen since then), rather than stamping every page as changed.
HASH_SCHEME = "sha256v3"
_HTML_SNIFF = re.compile(rb"^\s*(?:<!--.*?-->\s*)*<(?:!doctype\s+html|html|head|body)\b", re.I | re.S)
_DROP_TAGS = ("script", "style", "noscript", "template", "svg", "head", "iframe")
_NOISE = [
    # ASP.NET (Legistar, eSCRIBE, PUC): viewstate blobs differ per response.
    (rb'(name="__(?:VIEWSTATE|VIEWSTATEGENERATOR|EVENTVALIDATION)"[^>]*?value=")[^"]*(")',
     rb"\1\2"),
    # Legistar: each render mints a saved-filter ID + GUID for the feed/alert links.
    (rb'((?:Feed\.ashx|Alerts\.aspx)\?M=\w+&(?:amp;)?ID=)\d+(&(?:amp;)?GUID=)[0-9A-Fa-f-]{36}',
     rb"\1\2"),
    # eSCRIBE (ATP): `const sessionId = '...'`.
    (rb"""(\bsessionId\s*=\s*['"])[^'"]*(['"])""", rb"\1\2"),
    # TxDOT (AEM): Java object identity hashes in data attributes, `FooterItem@31f60766`.
    (rb'(\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+@)[0-9a-f]{1,8}(?![\w.])', rb"\1"),
    # austintexas.gov (Imperva/Incapsula): `_Incapsula_Resource?...&ns=8&cb=1165696431`.
    (rb'(_Incapsula_Resource\?[^"\'\s>]*?&ns=)\d+&cb=\d+', rb"\1"),
    # Cloudflare bot management (CTRMA, Project Connect): `__CF$cv$params={r:'..',t:'..'}`.
    (rb"(__CF\$cv\$params=\{)r:'[^']*',t:'[^']*'", rb"\1"),
    # WordPress Download Manager (LCRA): per-request `"client_id":"<32 hex>"`.
    (rb'("client_id":")[0-9a-f]{32}(")', rb"\1\2"),
    # WordPress nonces rotate every 12-24 h: `"nonce":"612be2e9df"`, `_wpnonce=...`.
    (rb"""((?:_wp)?nonce["']?\s*[:=]\s*["']?)[0-9a-f]{10}(?![0-9a-f])""", rb"\1"),
    # iCalendar feeds (The Events Calendar): DTSTAMP is the generation time.
    (rb"(?m)^(DTSTAMP:)\d{8}T\d{6}Z?", rb"\1"),
]
_NOISE = [(re.compile(p), r) for p, r in _NOISE]


def normalize(body: bytes) -> bytes:
    for pat, rep in _NOISE:
        body = pat.sub(rep, body)
    return body


def visible_text(body: bytes) -> bytes:
    """An HTML document's readable text, one non-empty line per block, with
    whitespace collapsed; anything that is not HTML comes back unchanged."""
    if not _HTML_SNIFF.match(body[:4096]):
        return body
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    lines = (" ".join(l.split()) for l in soup.get_text("\n").splitlines())
    return "\n".join(l for l in lines if l).encode("utf-8")


def _norm(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme.lower()}://{u.netloc.lower()}{u.path.rstrip('/') or '/'}" + (
        f"?{u.query}" if u.query else "")


def match_slug(url: str, rows: list[dict[str, str]]) -> str | None:
    """The registry row a fetched URL belongs to: exact, or the longest
    registry URL that the fetched URL extends with '?', '&' or '/'."""
    n = _norm(url)
    best = None
    for r in rows:
        ru = _norm(r["url"])
        if n == ru or n.startswith(ru + "?") or n.startswith(ru + "&") or n.startswith(ru + "/"):
            if best is None or len(ru) > len(_norm(best["url"])):
                best = r
    return best["slug"] if best else None


def body_hash(body: bytes) -> str:
    return f"{HASH_SCHEME}:" + hashlib.sha256(normalize(visible_text(body))).hexdigest()



class Tracker:
    """requests response hook that records fetches per registry slug."""

    def __init__(self, rows: list[dict[str, str]]):
        self.rows = rows
        self.seen: dict[str, str] = {}      # slug -> body hash (last response wins)

    def record(self, resp, **_kw) -> None:
        if not getattr(resp, "ok", False):
            return
        for url in [resp.url] + [h.url for h in getattr(resp, "history", [])] \
                + [getattr(getattr(resp, "request", None), "url", None)]:
            slug = match_slug(url, self.rows) if url else None
            if slug:
                self.seen[slug] = body_hash(resp.content)
                return

    def write(self, out: pathlib.Path, prior_path: pathlib.Path, now: datetime) -> dict:
        """Merge this build's fetches into the previous status.json and write it.
        Slugs not fetched this build keep their previous entry untouched.
        Per slug: checked (last fetch), changed (last build whose hash differed
        from the one before; null while none has), since (when the current
        hashing first saw the page: "no change since" reads from here), hash."""
        prior: dict = {}
        if prior_path.exists():
            try:
                prior = json.loads(prior_path.read_text()).get("sources", {})
            except (ValueError, AttributeError):
                prior = {}
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        sources = dict(prior)
        for slug, h in self.seen.items():
            old = prior.get(slug, {})
            same_scheme = str(old.get("hash", "")).split(":", 1)[0] == HASH_SCHEME
            if not same_scheme:                 # new page, or a hashing change: baseline only
                entry = {"checked": stamp, "changed": None, "since": stamp, "hash": h}
            else:
                entry = {"checked": stamp,
                         "changed": old.get("changed") if old.get("hash") == h else stamp,
                         "since": old.get("since") or old.get("changed") or stamp,
                         "hash": h}
            sources[slug] = entry
        doc = {"generated": stamp,
               "sources": {k: sources[k] for k in sorted(sources)}}
        out.write_text(json.dumps(doc, indent=1) + "\n")
        return doc
