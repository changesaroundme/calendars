"""docs/sources.html + docs/sources.md — the public "what we watch" table.

Generated on every build from sources.csv (what) and docs/status.json
(when it was last checked / last changed). One row per public page,
grouped by organisation; child pages (a board's meeting-content page, a
survey sub-page) fold into their parent and contribute their stamps.
The HTML is script-free like the rest of the site; the markdown is the
same table as an Obsidian page (wikilinked orgs) that Ian copies into the
vault and publishes when he wants it refreshed. The Archive column comes from
docs/captures.json, which the Mac archive job (archive_page.py --due) writes
and Ian commits; it stays "—" for a page with no capture on record.
"""
from __future__ import annotations

import html
import json
import pathlib
from datetime import datetime, timezone

from caltools.ics import CENTRAL

KB = "https://changesaroundme.com"
ORG_PAGE = "Organizations"          # vault page the markdown wikilinks point at
# Organizations page anchors (Obsidian Publish joins heading words with +).
ORG_ANCHORS = {
    "ATP": "Austin Transit Partnership (ATP)",
    "CoA": "City of Austin (CoA)",
    "CapMetro": "Capital Metropolitan Transportation Authority (CapMetro)",
    "CAMPO": "Capital Area Metropolitan Planning Organization (CAMPO)",
    "CTRMA": "Central Texas Regional Mobility Authority (CTRMA)",
    "LCRA": "Lower Colorado River Authority (LCRA)",
    "PUC": "Public Utility Commission of Texas (PUC or PUCT)",
    "TxDOT": "Texas Department of Transportation (TxDOT)",
    "TPSC": "Texas Pedestrian Safety Coalition (TPSC)",
    "TTC": "Texas Transportation Commission (TTC)",
    "Legislature": "Texas Legislature",
    "SOS": "Texas Secretary of State (SOS)",
}
# Display order: local first, then regional, then state.
ORG_ORDER = ["CoA", "ATP", "CapMetro", "CAMPO", "CTRMA", "LCRA",
             "TxDOT", "TTC", "PUC", "Legislature", "TPSC", "SOS"]


def org_link(org: str) -> str:
    heading = ORG_ANCHORS.get(org, org)
    return f'<a href="{KB}/Organizations#{heading.replace(" ", "+")}">{html.escape(org)}</a>'


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt(dt: datetime | None) -> str:
    """House style: `8 Sep 2026, 6:17am` in Central time."""
    if dt is None:
        return "—"
    local = dt.astimezone(CENTRAL)
    h12 = local.hour % 12 or 12
    return f"{local.day} {local:%b %Y}, {h12}:{local.minute:02d}{'pm' if local.hour >= 12 else 'am'}"


def _latest(stamps: list[datetime | None]) -> datetime | None:
    real = [s for s in stamps if s]
    return max(real) if real else None


def load_captures(path: pathlib.Path) -> dict:
    """docs/captures.json -> {slug: {"checked", "captured", "file"}}; {} when absent."""
    try:
        return json.loads(path.read_text()).get("sources", {})
    except (OSError, ValueError, AttributeError):
        return {}


def _stamps(group: list[dict[str, str]], stamps: dict, captures: dict):
    """Newest checked / changed / captured across a page and its children, plus
    the public URL of that newest capture when the archive is in object storage."""
    checked = _latest([_parse(stamps.get(g["slug"], {}).get("checked")) for g in group])
    changed = _latest([_parse(stamps.get(g["slug"], {}).get("changed")) for g in group])
    caps = [(_parse(captures.get(g["slug"], {}).get("captured")), captures.get(g["slug"], {}).get("url", ""))
            for g in group]
    caps = [c for c in caps if c[0]]
    captured, url = max(caps) if caps else (None, "")
    return checked, changed, captured, url


def render(rows: list[dict[str, str]], status: dict, now: datetime,
           captures: dict | None = None) -> str:
    stamps = status.get("sources", {})
    captures = captures or {}
    children: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        if r["parent"]:
            children.setdefault(r["parent"], []).append(r)

    by_org: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        if r["public"] != "yes" or r["parent"] or r["status"] == "retired":
            continue
        by_org.setdefault(r["org"], []).append(r)

    body = []
    for org in ORG_ORDER + sorted(set(by_org) - set(ORG_ORDER)):
        pages = by_org.get(org)
        if not pages:
            continue
        body.append(f"<h2 id=\"{html.escape(org)}\">{org_link(org)}</h2>")
        body.append("<table><thead><tr><th>Page</th><th>Checked</th>"
                    "<th>Last changed</th><th>Archive</th></tr></thead><tbody>")
        for r in sorted(pages, key=lambda x: x["name"].lower()):
            group = [r] + children.get(r["slug"], [])
            checked, changed, captured, cap_url = _stamps(group, stamps, captures)
            name = html.escape(r["name"])
            if r["status"] == "paused":
                name += ' <span class="tag">paused</span>'
            archive = (f'<a href="{html.escape(cap_url)}">{fmt(captured)}</a>' if cap_url else fmt(captured))
            body.append(
                f'<tr><td><a href="{html.escape(r["url"])}">{name}</a></td>'
                f'<td>{fmt(checked)}</td><td>{fmt(changed)}</td>'
                f'<td>{archive}</td></tr>')
        body.append("</tbody></table>")

    generated = _parse(status.get("generated")) or now
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pages we watch — Changes Around Me</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 60rem; margin: 2rem auto; padding: 0 1.25rem; line-height: 1.45; font-size: .95rem; }}
  h1 {{ font-size: 1.4rem; margin-bottom: .25rem; }}
  h2 {{ font-size: 1.1rem; margin: 1.6rem 0 .4rem; }}
  p.meta {{ opacity: .7; margin-top: 0; font-size: .85rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: .3rem .6rem; border-bottom: 1px solid rgba(128,128,128,.3); vertical-align: top; }}
  th {{ font-weight: 600; font-size: .8rem; text-transform: uppercase; letter-spacing: .03em; opacity: .75; }}
  td:nth-child(n+2), th:nth-child(n+2) {{ white-space: nowrap; width: 1%; }}
  .tag {{ font-size: .75rem; opacity: .7; border: 1px solid currentColor; border-radius: 3px; padding: 0 .3em; }}
  footer {{ margin-top: 2rem; font-size: .85rem; opacity: .7; }}
</style>
</head>
<body>
<h1>Pages we watch</h1>
<p class="meta">Every public page the calendars and the web archive are built from.
<b>Checked</b> is the last time the build read the page; <b>Last changed</b> is the
last time its content differed from the previous read; <b>Archive</b> is the newest saved copy
of the page in the web archive (linked once the archive is online). Updated {fmt(generated)} (Central).</p>
{chr(10).join(body)}
<footer>Generated from <a href="https://github.com/changesaroundme/calendars/blob/main/sources.csv">sources.csv</a>
and <a href="./status.json">status.json</a>, with capture times from <a href="./captures.json">captures.json</a>.
A <a href="{KB}">Changes Around Me</a> project.</footer>
</body>
</html>
"""


def fmt_md(dt: datetime | None) -> str:
    """Vault table style (Brief 2 sample): `6 Sep 2026 09:00`, Central."""
    if dt is None:
        return "—"
    local = dt.astimezone(CENTRAL)
    return f"{local.day} {local:%b %Y %H:%M}"


def render_markdown(rows: list[dict[str, str]], status: dict,
                    captures: dict | None = None) -> str:
    """One Obsidian table of every public page: Org | Page | Checked | Last changed | Archive.
    Written to docs/sources.md by the build; the vault copy is a plain file copy."""
    stamps = status.get("sources", {})
    captures = captures or {}
    children: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        if r["parent"]:
            children.setdefault(r["parent"], []).append(r)
    order = {o: i for i, o in enumerate(ORG_ORDER)}
    public = [r for r in rows if r["public"] == "yes" and not r["parent"] and r["status"] != "retired"]
    public.sort(key=lambda r: (order.get(r["org"], 99), r["name"].lower()))
    generated = _parse(status.get("generated"))
    lines = [
        f"*Every public page the calendars and the web archive are built from. "
        f"**Checked** is the last time the build read the page; **Last changed** is the last time "
        f"its content differed from the previous read; **Archive** is the newest saved copy of the page "
        f"in the web archive. Generated {fmt_md(generated)} — "
        f"regenerated on every build as `docs/sources.md` in the calendars repo; [[Links]] is a copy of that file.*",
        "",
        "| Org | Page | Checked | Last changed | Archive |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in public:
        group = [r] + children.get(r["slug"], [])
        checked, changed, captured, cap_url = _stamps(group, stamps, captures)
        org = f"[[{ORG_PAGE}#{ORG_ANCHORS.get(r['org'], r['org'])}\\|{r['org']}]]"
        name = r["name"].replace("|", "\\|")
        if r["status"] == "paused":
            name += " *(paused)*"
        archive = f"[{fmt_md(captured)}]({cap_url})" if cap_url else fmt_md(captured)
        lines.append(f"| {org} | [{name}]({r['url']}) | {fmt_md(checked)} | {fmt_md(changed)} | {archive} |")
    return "\n".join(lines) + "\n"
