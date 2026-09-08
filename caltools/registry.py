"""sources.csv — the registry of every page this project monitors.

One row per page, fourteen single-line columns, hand-edited in a grid
editor. The CSV holds current state only: git is the change history, and
runtime state (last checked, last changed, captures) lives in generated
files. Nothing reads the registry yet beyond this validator; adapters and
the archive script will move onto it one at a time.

validate() returns problems as strings naming the row (1-based line
number in the file, plus the slug where one exists) so a bad save is
findable from the CI log without opening the file.
"""
from __future__ import annotations

import csv
import pathlib
import re
from datetime import date
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
