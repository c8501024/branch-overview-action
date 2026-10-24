"""Branch overview: what is on the development branch but not yet released on the release branch, and
which pull requests are open.

Reads the GitHub API and writes one HTML page with three lanes of blocks:

    open pull requests  ->  on dev, not yet released  ->  released on main

Python standard library only. A token is optional for public repositories (without one the API allows 60
requests per hour). It is taken from GITHUB_TOKEN / GH_TOKEN, or from `gh auth token`.

    python branch_overview.py --repo owner/name              # writes the page and opens it in the browser
    python branch_overview.py --repo owner/name --serve      # local server, each reload fetches fresh data
    python branch_overview.py --repo owner/name --base main --head develop --out site/index.html --no-open
"""
from __future__ import annotations

import argparse
import base64
import html
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone

API = "https://api.github.com"
VERSION_FILES = (
    ("pyproject.toml", r'^version\s*=\s*"([^"]+)"'),
    ("package.json", r'"version"\s*:\s*"([^"]+)"'),
    ("Cargo.toml", r'^version\s*=\s*"([^"]+)"'),
)


# --- GitHub API -----------------------------------------------------------------------------------

def find_token() -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    token = out.stdout.strip()
    return token if out.returncode == 0 and token else None


class GitHub:
    def __init__(self, repo: str, token: str | None):
        self.repo = repo
        self.token = token

    def get(self, path: str, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{API}/repos/{self.repo}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "branch-overview"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            if err.code == 403 and err.headers.get("X-RateLimit-Remaining") == "0":
                raise SystemExit("GitHub API rate limit reached. Set GITHUB_TOKEN or log in with `gh auth login`.")
            raise


def file_version(gh: GitHub, ref: str, files=VERSION_FILES) -> str | None:
    """The version on a branch, from the first of pyproject.toml, package.json or Cargo.toml it has."""
    for path, pattern in files:
        data = gh.get(f"/contents/{path}", ref=ref)
        if not data or "content" not in data:
            continue
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
        m = re.search(pattern, text, re.M)
        if m:
            return m.group(1)
    return None


def check_summary(gh: GitHub, sha: str) -> str:
    """'success', 'failure', 'pending' or 'none' from the check runs of a commit."""
    data = gh.get(f"/commits/{sha}/check-runs", per_page=100) or {}
    runs = data.get("check_runs", [])
    if not runs:
        return "none"
    if any(r["status"] != "completed" for r in runs):
        return "pending"
    if any(r["conclusion"] in ("failure", "timed_out", "cancelled", "action_required") for r in runs):
        return "failure"
    return "success"


def fetch(gh: GitHub, base: str, head: str, version_file: str | None = None) -> dict:
    """Everything the page shows, as plain data."""
    compare = gh.get(f"/compare/{base}...{head}", per_page=250) or {}
    # Commits on main that dev lacks. The merge commit of each release is always there and is harmless.
    reverse = gh.get(f"/compare/{head}...{base}", per_page=250) or {}
    merged_into_head = [p for p in gh.get("/pulls", state="closed", base=head, sort="updated",
                                          direction="desc", per_page=100) or [] if p.get("merged_at")]
    open_prs = gh.get("/pulls", state="open", sort="created", direction="desc", per_page=100) or []
    details = []
    for pr in open_prs:
        full = gh.get(f"/pulls/{pr['number']}") or pr
        full["checks"] = check_summary(gh, pr["head"]["sha"])
        details.append(full)
    releases = gh.get("/releases", per_page=1) or []
    return {
        "repo": gh.repo, "base": base, "head": head,
        "compare": compare, "only_on_base": reverse.get("commits", []), "merged": merged_into_head, "open": details,
        "release": releases[0] if releases else None,
        "version_base": _version(gh, base, version_file), "version_head": _version(gh, head, version_file),
        "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _version(gh: GitHub, ref: str, version_file: str | None) -> str | None:
    if version_file == "":
        return None
    if version_file:
        pattern = dict(VERSION_FILES).get(os.path.basename(version_file), r'^version\s*=\s*"([^"]+)"')
        return file_version(gh, ref, ((version_file, pattern),))
    return file_version(gh, ref)


# --- Model ----------------------------------------------------------------------------------------

def build_model(data: dict, released_count: int = 8) -> dict:
    """Sort the merged pull requests into 'only on dev' and 'released', and keep the commits on dev
    that no pull request brought in."""
    compare = data["compare"]
    ahead = compare.get("commits", [])
    ahead_shas = {c["sha"] for c in ahead}
    pending, released = [], []
    for pr in sorted(data["merged"], key=lambda p: p["merged_at"], reverse=True):
        (pending if pr.get("merge_commit_sha") in ahead_shas else released).append(pr)

    # Commits on dev that came in through none of the pending pull requests (pushed directly). Merge
    # commits are left out: they are either a pull request's merge or a merge of main into dev.
    covered = set()
    for pr in pending:
        covered.add(pr.get("merge_commit_sha"))
        covered.update(pr.get("_commit_shas", ()))
    direct = [c for c in ahead if c["sha"] not in covered and len(c.get("parents", [])) < 2]

    return {
        "repo": data["repo"], "base": data["base"], "head": data["head"],
        "ahead_by": compare.get("ahead_by", 0),
        "only_on_base": [c for c in data.get("only_on_base", []) if len(c.get("parents", [])) < 2],
        "pending": pending, "released": released[:released_count], "direct": direct,
        "open": data["open"], "release": data["release"],
        "version_base": data["version_base"], "version_head": data["version_head"],
        "fetched": data["fetched"],
    }


def attach_pr_commits(gh: GitHub, data: dict) -> None:
    """Add the commit list of each pending pull request, so its commits are not shown as direct ones."""
    ahead_shas = {c["sha"] for c in data["compare"].get("commits", [])}
    for pr in data["merged"]:
        if pr.get("merge_commit_sha") in ahead_shas:
            commits = gh.get(f"/pulls/{pr['number']}/commits", per_page=250) or []
            pr["_commit_shas"] = {c["sha"] for c in commits}
            pr["_commit_count"] = len(commits)


CSS = """
:root { --bg:#f6f7f9; --panel:#fff; --text:#1d2330; --muted:#667085; --line:#e3e6eb;
  --open:#3b6fd8; --pending:#d98a1c; --released:#2f9e6a; --direct:#8a5cd6; }
@media (prefers-color-scheme: dark) { :root { --bg:#14171c; --panel:#1d2128; --text:#e6e9ee; --muted:#98a2b3;
  --line:#2c323c; --open:#6f9bff; --pending:#f0a840; --released:#4cc38a; --direct:#b18cff; } }
* { box-sizing:border-box }
body { margin:0; font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--text) }
header { padding:20px 24px 8px }
h1 { font-size:20px; margin:0 0 4px }
header .sub { color:var(--muted) }
header a, .note a { color:inherit }
.flow { display:grid; grid-template-columns:1fr auto 1fr auto 1fr; gap:10px; padding:12px 24px 24px; align-items:start }
.arrow { align-self:center; font-size:28px; color:var(--muted); padding-top:30px }
.lane { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; min-width:0 }
.lane h2 { font-size:15px; margin:0 0 2px; display:flex; gap:8px; align-items:center }
.lane h2 .count { background:var(--c); color:#fff; border-radius:10px; padding:0 8px; font-size:12px }
.lane .desc { color:var(--muted); font-size:12px; margin-bottom:12px }
.lane.open { --c:var(--open) } .lane.pending { --c:var(--pending) } .lane.released { --c:var(--released) }
.lane { border-top:4px solid var(--c) }
.block { display:block; text-decoration:none; color:inherit; border:1px solid var(--line); border-left:6px solid var(--c);
  border-radius:8px; padding:9px 11px; margin-bottom:9px; background:var(--panel) }
.block:hover { box-shadow:0 2px 8px rgba(0,0,0,.12) }
.block.open { --c:var(--open) } .block.pending { --c:var(--pending) } .block.released { --c:var(--released) }
.block.direct { --c:var(--direct) }
.title { font-weight:600 }
.meta { color:var(--muted); font-size:12px; margin-top:3px }
.badges { margin-top:6px; display:flex; flex-wrap:wrap; gap:5px; align-items:center }
.badge { font-size:11px; border-radius:6px; padding:1px 7px; border:1px solid var(--line) }
.badge.ok { background:#2f9e6a22; color:var(--released) } .badge.bad { background:#d92d2022; color:#d92d20 }
.badge.wait { background:#d98a1c22; color:var(--pending) } .badge.warn { background:#d92d2015; color:#d92d20 }
.badge.draft, .badge.none { color:var(--muted) }
.diff { font-size:11px; color:var(--muted) } .add { color:var(--released) } .del { color:#d92d20 }
.label { display:inline-block; font-size:11px; border-radius:10px; padding:0 7px; margin:4px 4px 0 0;
  border:1px solid var(--c); color:var(--text) }
.empty { color:var(--muted); font-style:italic; padding:8px 0 }
.note { margin:0 0 10px; padding:8px 10px; border-radius:8px; background:#d98a1c1a; font-size:12px }
.note.warn { background:#d92d2015 }
.legend { display:flex; gap:14px; flex-wrap:wrap; color:var(--muted); font-size:12px; margin-top:8px }
.legend span::before { content:""; display:inline-block; width:10px; height:10px; border-radius:2px; background:var(--c); margin-right:5px }
@media (max-width:900px) { .flow { grid-template-columns:1fr } .arrow { transform:rotate(90deg); padding:0; justify-self:center } }
"""


# --- HTML -----------------------------------------------------------------------------------------

TEXT = {
    "en": {
        "date": "%Y-%m-%d %H:%M", "opened": "opened {d}", "merged": "merged {d}", "commits": "{n} commits",
        "draft": "Draft", "ok": "Checks passed", "bad": "Checks failed", "wait": "Checks running",
        "none": "No checks", "conflict": "Merge conflict", "behind": "behind {b}", "target": "targets {t}, not {h}",
        "files": "{n} files", "direct": "pushed to {h} without a pull request",
        "no_open": "No open pull requests.", "same": "{h} and {b} are even. Nothing waits for a release.",
        "no_released": "No merged pull requests yet.",
        "release": 'Last release <a href="{u}" target="_blank">{t}</a> on {d}', "no_release": "No release yet",
        "version_hint": "The version on {h} is still {v}. Raise it for the next release, then merge {h} into {b}.",
        "hotfix": "{b} has {n} commit(s) that {h} lacks (a hotfix on {b}, for example).", "view": "view",
        "title": "Branch overview", "version": "Version {r}: {v}", "ahead": "{h} is {n} commit(s) ahead of {b}",
        "compare": "Compare on GitHub", "as_of": "As of {d}",
        "leg_open": "open pull request", "leg_pending": "on {h}, not released", "leg_direct": "pushed directly to {h}",
        "leg_released": "released on {b}",
        "lane_open": "Open pull requests", "lane_open_desc": "Waiting for review or merge.",
        "lane_pending": "On {h}, not yet on {b}", "lane_pending_desc": "Ships with the next release ({h} into {b}).",
        "lane_released": "Released on {b}", "lane_released_desc": "The latest merged pull requests already on {b}.",
    },
    "de": {
        "date": "%d.%m.%Y %H:%M", "opened": "geöffnet {d}", "merged": "gemergt {d}", "commits": "{n} Commits",
        "draft": "Entwurf", "ok": "Checks grün", "bad": "Checks rot", "wait": "Checks laufen",
        "none": "keine Checks", "conflict": "Merge-Konflikt", "behind": "hinter {b}", "target": "Ziel ist {t}, nicht {h}",
        "files": "{n} Dateien", "direct": "direkt auf {h}, ohne PR",
        "no_open": "Keine offenen Pull Requests.", "same": "{h} und {b} sind gleich. Nichts wartet auf ein Release.",
        "no_released": "Noch keine gemergten Pull Requests.",
        "release": 'Letztes Release <a href="{u}" target="_blank">{t}</a> vom {d}', "no_release": "Noch kein Release",
        "version_hint": "Version auf {h} ist noch {v}. Für das nächste Release die Versionsnummer erhöhen, "
                        "dann {h} → {b} mergen.",
        "hotfix": "{b} hat {n} Commit(s), die nicht auf {h} sind (z. B. ein Hotfix direkt auf {b}).", "view": "ansehen",
        "title": "Branch-Überblick", "version": "Version {r}: {v}", "ahead": "{h} ist {n} Commit(s) vor {b}",
        "compare": "Vergleich auf GitHub", "as_of": "Stand {d}",
        "leg_open": "offener PR", "leg_pending": "auf {h}, nicht released", "leg_direct": "Commit direkt auf {h}",
        "leg_released": "released auf {b}",
        "lane_open": "Offene Pull Requests", "lane_open_desc": "Warten auf Review oder Merge.",
        "lane_pending": "Auf {h}, noch nicht in {b}", "lane_pending_desc": "Kommt mit dem nächsten Release ({h} → {b}).",
        "lane_released": "Released auf {b}", "lane_released_desc": "Die letzten gemergten PRs, die schon in {b} sind.",
    },
}


def _e(text) -> str:
    return html.escape(str(text if text is not None else ""))


class _Page:
    def __init__(self, m: dict, lang: str):
        self.m = m
        self.t = TEXT.get(lang, TEXT["en"])
        self.lang = lang if lang in TEXT else "en"
        # Branch names are escaped once here, every text below gets them already escaped.
        self.names = {"b": _e(m["base"]), "h": _e(m["head"])}

    def tr(self, key: str, **kw) -> str:
        return self.t[key].format(**{**self.names, **kw})

    def date(self, iso: str | None) -> str:
        if not iso:
            return ""
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime(self.t["date"])

    @staticmethod
    def labels(pr: dict) -> str:
        return "".join(f'<span class="label" style="--c:#{_e(l["color"])}">{_e(l["name"])}</span>'
                       for l in pr.get("labels", []))

    def pr_block(self, pr: dict, kind: str) -> str:
        meta = [f'#{pr["number"]}', _e(pr["user"]["login"])]
        extra = ""
        if kind == "open":
            meta.append(f'{_e(pr["head"]["ref"])} → <b>{_e(pr["base"]["ref"])}</b>')
            meta.append(self.tr("opened", d=self.date(pr["created_at"])))
            badges = []
            if pr.get("draft"):
                badges.append(f'<span class="badge draft">{self.tr("draft")}</span>')
            check = {"success": "ok", "failure": "bad", "pending": "wait"}.get(pr.get("checks"), "none")
            badges.append(f'<span class="badge {check}">{self.tr(check)}</span>')
            state = pr.get("mergeable_state")
            if state == "dirty":
                badges.append(f'<span class="badge bad">{self.tr("conflict")}</span>')
            elif state == "behind":
                badges.append(f'<span class="badge wait">{self.tr("behind", b=_e(pr["base"]["ref"]))}</span>')
            if pr["base"]["ref"] != self.m["head"]:
                badges.append(f'<span class="badge warn">{self.tr("target", t=_e(pr["base"]["ref"]))}</span>')
            if "additions" in pr:
                badges.append(f'<span class="diff"><span class="add">+{pr["additions"]}</span> '
                              f'<span class="del">−{pr["deletions"]}</span> · '
                              f'{self.tr("files", n=pr["changed_files"])}</span>')
            extra = f'<div class="badges">{"".join(badges)}</div>'
        else:
            meta.append(self.tr("merged", d=self.date(pr["merged_at"])))
            if pr.get("_commit_count"):
                meta.append(self.tr("commits", n=pr["_commit_count"]))
        return (f'<a class="block {kind}" href="{_e(pr["html_url"])}" target="_blank" rel="noopener">'
                f'<div class="title">{_e(pr["title"])}</div>{self.labels(pr)}'
                f'<div class="meta">{" · ".join(meta)}</div>{extra}</a>')

    def commit_block(self, c: dict) -> str:
        first = c["commit"]["message"].splitlines()[0]
        who = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
        return (f'<a class="block direct" href="{_e(c["html_url"])}" target="_blank" rel="noopener">'
                f'<div class="title">{_e(first)}</div>'
                f'<div class="meta">{_e(c["sha"][:7])} · {_e(who)} · {self.date(c["commit"]["author"]["date"])}'
                f' · {self.tr("direct")}</div></a>')

    def render(self, title: str | None, noindex: bool) -> str:
        m, tr = self.m, self.tr
        repo = m["repo"]
        gh_url = f"https://github.com/{repo}"
        b, h = m["base"], m["head"]
        open_prs = sorted(m["open"], key=lambda p: p["base"]["ref"] != h)  # PRs into head first
        open_html = "".join(self.pr_block(p, "open") for p in open_prs) \
            or f'<div class="empty">{tr("no_open")}</div>'
        pending_html = "".join(self.pr_block(p, "pending") for p in m["pending"])
        pending_html += "".join(self.commit_block(c) for c in m["direct"])
        if not pending_html:
            pending_html = f'<div class="empty">{tr("same")}</div>'
        released_html = "".join(self.pr_block(p, "released") for p in m["released"]) \
            or f'<div class="empty">{tr("no_released")}</div>'

        rel = m["release"]
        rel_line = tr("release", u=_e(rel["html_url"]), t=_e(rel["tag_name"]), d=self.date(rel["published_at"])) \
            if rel else tr("no_release")
        vb, vh = m["version_base"], m["version_head"]
        versions = ""
        if vb or vh:
            versions = (f' · {tr("version", r=_e(b), v=_e(vb or "?"))}'
                        f' · {tr("version", r=_e(h), v=_e(vh or "?"))}')
        version_note = ""
        if m["ahead_by"] and vb and vh and vb == vh:
            version_note = f'<div class="note">{tr("version_hint", v=_e(vh))}</div>'
        behind_note = ""
        if m["only_on_base"]:
            behind_note = (f'<div class="note warn">{tr("hotfix", n=len(m["only_on_base"]))} '
                           f'<a href="{gh_url}/compare/{_e(h)}...{_e(b)}" target="_blank">{tr("view")}</a></div>')
        heading = _e(title) if title else f'{tr("title")} <a href="{gh_url}" target="_blank">{_e(repo)}</a>'
        n_pending = len(m["pending"]) + len(m["direct"])
        robots = '\n<meta name="robots" content="noindex">' if noindex else ""
        return f"""<!doctype html>
<html lang="{self.lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{robots}
<title>{_e(title) if title else tr("title") + " " + _e(repo)}</title>
<style>{CSS}</style></head><body>
<header>
  <h1>{heading}</h1>
  <div class="sub">{rel_line}{versions}
    · {tr("ahead", n=m["ahead_by"])}
    · <a href="{gh_url}/compare/{_e(b)}...{_e(h)}" target="_blank">{tr("compare")}</a>
    · {tr("as_of", d=self.date(m["fetched"]))}</div>
  <div class="legend">
    <span style="--c:var(--open)">{tr("leg_open")}</span><span style="--c:var(--pending)">{tr("leg_pending")}</span>
    <span style="--c:var(--direct)">{tr("leg_direct")}</span><span style="--c:var(--released)">{tr("leg_released")}</span>
  </div>
</header>
<div class="flow">
  <section class="lane open"><h2>{tr("lane_open")} <span class="count">{len(m["open"])}</span></h2>
    <div class="desc">{tr("lane_open_desc")}</div>{open_html}</section>
  <div class="arrow">→</div>
  <section class="lane pending"><h2>{tr("lane_pending")} <span class="count">{n_pending}</span></h2>
    <div class="desc">{tr("lane_pending_desc")}</div>{behind_note}{version_note}{pending_html}</section>
  <div class="arrow">→</div>
  <section class="lane released"><h2>{tr("lane_released")} <span class="count">{len(m["released"])}</span></h2>
    <div class="desc">{tr("lane_released_desc")}</div>{released_html}</section>
</div>
</body></html>"""


def render(m: dict, lang: str = "en", title: str | None = None, noindex: bool = False) -> str:
    return _Page(m, lang).render(title, noindex)


# --- CLI ------------------------------------------------------------------------------------------

def generate(args, token: str | None) -> str:
    gh = GitHub(args.repo, token)
    data = fetch(gh, args.base, args.head, args.version_file)
    attach_pr_commits(gh, data)
    return render(build_model(data, args.released), args.lang, args.title, args.noindex)


def default_repo() -> str | None:
    """GITHUB_REPOSITORY in Actions, else the origin remote of the current git checkout."""
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def serve(args, token: str | None) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            try:
                body = generate(args, token).encode("utf-8")
                code = 200
            except Exception as exc:  # shown in the browser, the server keeps running
                body = f"<pre>{_e(exc)}</pre>".encode("utf-8")
                code = 500
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Branch overview at {url} (reload the page for fresh data, Ctrl+C to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Blocks of open pull requests, unreleased changes on the development "
                                            "branch and the latest released pull requests.")
    p.add_argument("--repo", default=default_repo(),
                   help="owner/name (default: GITHUB_REPOSITORY or the origin remote)")
    p.add_argument("--base", default="main", help="release branch (default %(default)s)")
    p.add_argument("--head", default="dev", help="development branch (default %(default)s)")
    p.add_argument("--lang", default="en", choices=sorted(TEXT), help="page language (default %(default)s)")
    p.add_argument("--title", help="page heading (default: 'Branch overview owner/name')")
    p.add_argument("--released", type=int, default=8, help="released pull requests shown (default %(default)s)")
    p.add_argument("--version-file", help="file with the version, e.g. setup.cfg; empty to hide versions "
                                          "(default: pyproject.toml, package.json or Cargo.toml)")
    p.add_argument("--noindex", action="store_true", help="ask search engines not to index the page")
    p.add_argument("--out", help="HTML file to write (default: a file in the temp folder)")
    p.add_argument("--no-open", action="store_true", help="do not open the browser")
    p.add_argument("--serve", action="store_true", help="local server, each reload fetches fresh data")
    p.add_argument("--port", type=int, default=8765, help="port for --serve (default %(default)s)")
    args = p.parse_args(argv)
    if not args.repo:
        p.error("--repo owner/name is needed outside a GitHub checkout")
    token = find_token()
    if not token:
        print("No GitHub token found, using the API without login (60 requests per hour).", file=sys.stderr)
    if args.serve:
        serve(args, token)
        return
    page = generate(args, token)
    out = args.out or os.path.join(tempfile.gettempdir(), "branch-overview.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(out)
    if not args.no_open:
        webbrowser.open("file://" + os.path.abspath(out))


if __name__ == "__main__":
    main()
