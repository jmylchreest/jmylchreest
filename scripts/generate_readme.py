#!/usr/bin/env python3
"""Generate a GitHub profile README from live repo data."""

import os
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

USERNAME = "jmylchreest"
STARS_THRESHOLD = 4
RECENT_DAYS = 7
FEATURED_FILE = Path(__file__).resolve().parent.parent / ".featured"

API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

token = os.environ.get("GH_TOKEN")
if token:
    HEADERS["Authorization"] = f"Bearer {token}"


def fetch_repos():
    """Fetch all public, non-fork repos for the user (handles pagination)."""
    repos = []
    page = 1
    while True:
        resp = requests.get(
            f"{API}/users/{USERNAME}/repos",
            headers=HEADERS,
            params={
                "type": "owner",
                "sort": "updated",
                "direction": "desc",
                "per_page": 100,
                "page": page,
            },
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return repos


def load_featured():
    """Load the list of featured repo names from .featured (one per line)."""
    if not FEATURED_FILE.exists():
        return []
    names = []
    for line in FEATURED_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


def is_notable(repo):
    """Return True if the repo has enough stars or very recent commits."""
    if repo.get("fork"):
        return False
    if repo.get("private"):
        return False
    if repo.get("archived"):
        return False

    # Stars check
    if (repo.get("stargazers_count") or 0) >= STARS_THRESHOLD:
        return True

    # Recent push check
    pushed = repo.get("pushed_at")
    if pushed:
        pushed_dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
        if pushed_dt >= cutoff:
            return True

    return False


def language_label(lang):
    """Return the language name as plain text, or empty string."""
    if not lang:
        return ""
    return lang


def format_stars(count):
    """Format star count as unicode star + number."""
    if not count:
        return ""
    return f"\u2605{count}"


def fetch_repo_meta(owner, name):
    """Fetch latest release tag, open issue count, and open PR count for a repo."""
    meta = {"release": None, "issues": 0, "prs": 0}

    # Latest release
    resp = requests.get(
        f"{API}/repos/{owner}/{name}/releases/latest",
        headers=HEADERS,
    )
    if resp.status_code == 200:
        meta["release"] = resp.json().get("tag_name")

    # Open issues (GitHub counts PRs as issues, so we need to subtract PRs)
    resp = requests.get(
        f"{API}/search/issues",
        headers=HEADERS,
        params={"q": f"repo:{owner}/{name} is:issue is:open", "per_page": 1},
    )
    if resp.status_code == 200:
        meta["issues"] = resp.json().get("total_count", 0)

    # Open PRs
    resp = requests.get(
        f"{API}/search/issues",
        headers=HEADERS,
        params={"q": f"repo:{owner}/{name} is:pr is:open", "per_page": 1},
    )
    if resp.status_code == 200:
        meta["prs"] = resp.json().get("total_count", 0)

    return meta


def format_meta_line(meta, url):
    """Build a single-line summary with links to the relevant project pages."""
    parts = []
    if meta["release"]:
        parts.append(f"[release: `{meta['release']}`]({url}/releases/latest)")
    parts.append(f"[issues: {meta['issues']}]({url}/issues)")
    parts.append(f"[PRs: {meta['prs']}]({url}/pulls)")
    return " \u00b7 ".join(parts)


def render_repo(repo, meta):
    """Render a single repo entry as markdown lines."""
    name = repo["name"]
    url = repo["html_url"]
    desc = repo.get("description") or ""
    lang = language_label(repo.get("language"))
    stars = format_stars(repo.get("stargazers_count"))

    # Title line: repo name (★X, Language)
    title_parts = []
    if stars:
        title_parts.append(stars)
    if lang:
        title_parts.append(lang)
    suffix = f" ({', '.join(title_parts)})" if title_parts else ""

    lines = []
    lines.append(f"- **[{name}]({url})**{suffix}<br>")
    lines.append(f"  <sub>{format_meta_line(meta, url)}</sub>")
    if desc:
        lines.append("")
        lines.append(f"  {desc}")
    lines.append("")
    return lines


def build_readme(repos):
    """Render the full README markdown."""
    featured_names = load_featured()
    repo_by_name = {r["name"]: r for r in repos if not r.get("private")}

    # Featured repos (ordered as listed in .featured)
    featured = [repo_by_name[n] for n in featured_names if n in repo_by_name]
    featured_set = {r["name"] for r in featured}

    # Notable / recently active (excluding featured)
    non_featured = [r for r in repos if is_notable(r) and r["name"] not in featured_set]
    starred = sorted(
        [
            r
            for r in non_featured
            if (r.get("stargazers_count") or 0) >= STARS_THRESHOLD
        ],
        key=lambda r: r.get("stargazers_count") or 0,
        reverse=True,
    )
    recent_only = sorted(
        [r for r in non_featured if r not in starred],
        key=lambda r: r.get("pushed_at") or "",
        reverse=True,
    )

    # Fetch metadata for all repos we'll render
    all_rendered = featured + starred + recent_only
    meta_cache = {}
    for r in all_rendered:
        owner = r["owner"]["login"]
        name = r["name"]
        print(f"  fetching metadata for {owner}/{name} ...")
        meta_cache[name] = fetch_repo_meta(owner, name)

    lines = []
    lines.append("## Hey, I'm John")
    lines.append("")

    if featured:
        lines.append("### Featured")
        lines.append("")
        for r in featured:
            lines.extend(render_repo(r, meta_cache[r["name"]]))
        lines.append("")

    if starred:
        lines.append("### Notable Projects")
        lines.append("")
        for r in starred:
            lines.extend(render_repo(r, meta_cache[r["name"]]))
        lines.append("")

    if recent_only:
        lines.append("### Recently Active")
        lines.append("")
        for r in recent_only:
            lines.extend(render_repo(r, meta_cache[r["name"]]))
        lines.append("")

    if not featured and not starred and not recent_only:
        lines.append("*Nothing notable right now — check back soon.*")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def main():
    repos = fetch_repos()
    readme = build_readme(repos)
    Path("README.md").write_text(readme)
    print(
        f"README.md generated — {len([r for r in repos if is_notable(r)])} repos listed"
    )


if __name__ == "__main__":
    main()
