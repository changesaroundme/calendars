"""sources.csv — the registry of every page this project monitors.

One row per page, fourteen single-line columns, hand-edited in a grid
editor. The CSV holds current state only: git is the change history, and
runtime state (last checked, last changed, captures) lives in generated
files. Nothing reads the registry yet beyond this validator; adapters and
the archive script will move onto it one at a time.

validate() returns problems as strings naming the row (1-based line
number in the file, plus the slug where one exists) so a bad save is
findable from the CI log without opening the file.

Tracker is the CI half of docs/status.json: it listens to every response
the build's requests.Session receives, maps the URL back to a registry
slug, and records when that page was last fetched and when its body last
differed from the previous build. No adapter knows it exists. The Mac
archive script keeps its own capture times in its own file; the generated
sources page merges the two, so the two writers never touch one file.
"""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import re
from datetime import date, datetime
from urllib.parse import urlparse

COLUMNS = ["slug", "org", "name", "url", "calendar", "expect", "check",
           "archive", "parse", "status", "public", "parent", "runner", "added"]
ORGS = {"ATP", "CoA", "CapMetro", "CAMPO", "CTRMA", "LCRA", "PUC", "TxDOT",
        "TPSC", "TTC", "Legislature", "SOS"}
PARSE = {"ics", "rss", "api", "html", "html-table", "pdf", "claude",
         "manual", "none"}
STATUS = {"active", "paused", "retired"}
RUNNER = {"ci", "mac", "manual"}
YESNO = {"yes", "no"}
SLUG_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
EXPECT_RE = re.compile(r"always|sporadic|annual .+")


def load(path: pathlib.Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def validate(rows: list[dict[str, str]], calendars: set[str],
             today: date | None = None) -> list[str]:
    """Return every problem found, or [] when the registry is clean."""
    today = today or date.today()
    problems: list[str] = []
    if not rows:
        return ["sources.csv: no rows"]
    header = list(rows[0].keys())
    if header != COLUMNS:
        return [f"sources.csv: columns are {header}, expected {COLUMNS}"]

    slugs = [r["slug"] for r in rows]
    seen_slug: dict[str, int] = {}
    seen_url: dict[str, int] = {}
    for i, r in enumerate(rows, start=2):        # line 1 is the header
        tag = f"line {i} ({r['slug'] or '?'})"

        def bad(msg: str) -> None:
            problems.append(f"{tag}: {msg}")

        for k, v in r.items():
            if v is None:
                bad(f"too many cells")
                break
            if "\n" in v or "\r" in v:
                bad(f"{k} spans lines")
            if v != v.strip():
                bad(f"{k} has leading/trailing whitespace")
        if not SLUG_RE.fullmatch(r["slug"]):
            bad("slug must be kebab-case a-z0-9")
        if r["slug"] in seen_slug:
            bad(f"duplicate slug (also line {seen_slug[r['slug']]})")
        seen_slug.setdefault(r["slug"], i)
        if r["org"] not in ORGS:
            bad(f"unknown org {r['org']!r}")
        if not r["name"]:
            bad("name is empty")
        u = urlparse(r["url"])
        if u.scheme != "https" or not u.netloc or " " in r["url"]:
            bad(f"bad url {r['url']!r}")
        if r["url"] in seen_url:
            bad(f"duplicate url (also line {seen_url[r['url']]})")
        seen_url.setdefault(r["url"], i)
        if r["calendar"] and r["calendar"] not in calendars:
            bad(f"unknown calendar {r['calendar']!r}")
        if not EXPECT_RE.fullmatch(r["expect"]):
            bad(f"expect must be always / sporadic / annual <when>, got {r['expect']!r}")
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
        if r["runner"] not in RUNNER:
            bad(f"unknown runner {r['runner']!r}")
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
        "ci", "2026-07-25"]))
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
    assert hits and "line 3" in hits[0], probs
    assert any("bad url" in p and "line 4" in p for p in probs), probs
    assert any("neither fetched nor archived" in p and "line 5" in p for p in probs), probs
    assert any("in the future" in p and "line 6" in p for p in probs), probs
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
    # ASP.NET viewstate churn must not read as a change.
    v1 = b'<input name="__VIEWSTATE" id="v" value="AAA" /><p>same</p>'
    v2 = b'<input name="__VIEWSTATE" id="v" value="BBB" /><p>same</p>'
    assert body_hash(v1) == body_hash(v2)
    assert body_hash(v1) != body_hash(b'<p>other</p>')


# ---------------------------------------------------------------------------
# docs/status.json — per-slug "checked" / "changed", from the live session
# ---------------------------------------------------------------------------

# ASP.NET pages (Legistar) carry a fresh __VIEWSTATE on every response, so
# hashing the raw body would report "changed" on every build. Blank those
# hidden fields before hashing; anything else that churns will show up as
# daily changes in status.json and can be added here when it does.
_ASPNET_RE = re.compile(
    rb'(name="__(?:VIEWSTATE|VIEWSTATEGENERATOR|EVENTVALIDATION)"[^>]*?value=")[^"]*(")')


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
    return "sha256:" + hashlib.sha256(_ASPNET_RE.sub(rb"\1\2", body)).hexdigest()


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
        Slugs not fetched this build keep their previous entry untouched."""
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
            sources[slug] = {
                "checked": stamp,
                "changed": old.get("changed", stamp) if old.get("hash") == h else stamp,
                "hash": h,
            }
        doc = {"generated": stamp,
               "sources": {k: sources[k] for k in sorted(sources)}}
        out.write_text(json.dumps(doc, indent=1) + "\n")
        return doc
