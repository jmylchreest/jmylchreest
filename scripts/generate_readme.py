#!/usr/bin/env python3
"""Generate a GitHub profile README from live repo data."""

import fnmatch
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
    """Fetch all public, non-fork, non-profile repos for the user (handles pagination)."""
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
        repos.extend(
            r for r in batch
            if not r.get("private", False) and r["name"] != USERNAME
        )
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


SKIP_ASSET_EXTS = (
    ".sig",
    ".asc",
    ".pem",
    ".sha256",
    ".sha512",
    ".md5",
    ".pub",
    ".crt",
    ".cert",
    ".minisig",
)
SKIP_ASSET_KEYWORDS = (
    "checksum",
    "sbom",
    ".spdx",
    ".cdx",
    "provenance",
    ".intoto",
    "-metadata",
)

# Per-repo asset-name globs to exclude from download counts (non-primary artifacts).
REPO_SKIP_PATTERNS = {
    "aide": ("aide-grammar-*",),
}


def is_countable_asset(name, repo_name=None):
    """Exclude signatures, checksums, SBOMs — count only real user downloads."""
    if not name:
        return False
    lower = name.lower()
    if lower.endswith(SKIP_ASSET_EXTS):
        return False
    if any(k in lower for k in SKIP_ASSET_KEYWORDS):
        return False
    for pat in REPO_SKIP_PATTERNS.get(repo_name or "", ()):
        if fnmatch.fnmatch(lower, pat.lower()):
            return False
    return True


def sum_asset_downloads(assets, repo_name=None):
    return sum(
        a.get("download_count", 0)
        for a in (assets or [])
        if is_countable_asset(a.get("name", ""), repo_name)
    )


def format_count(n):
    """Compact human count: 1234 -> 1.2k, 1234567 -> 1.2M."""
    if n is None:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}k".replace(".0k", "k")
    return str(n)


def fetch_repo_meta(owner, name):
    """Fetch latest release tag, download counts, open issues and PRs."""
    meta = {
        "release": None,
        "issues": 0,
        "prs": 0,
        "downloads_latest": 0,
        "downloads_total": 0,
        "has_releases": False,
    }

    # Latest release (tag + per-version downloads)
    resp = requests.get(
        f"{API}/repos/{owner}/{name}/releases/latest",
        headers=HEADERS,
    )
    if resp.status_code == 200:
        data = resp.json()
        meta["release"] = data.get("tag_name")
        meta["downloads_latest"] = sum_asset_downloads(data.get("assets"), name)

    # All releases (for lifetime download total). Paginated.
    page = 1
    while True:
        resp = requests.get(
            f"{API}/repos/{owner}/{name}/releases",
            headers=HEADERS,
            params={"per_page": 100, "page": page},
        )
        if resp.status_code != 200:
            break
        batch = resp.json()
        if not batch:
            break
        meta["has_releases"] = True
        for rel in batch:
            meta["downloads_total"] += sum_asset_downloads(rel.get("assets"), name)
        if len(batch) < 100:
            break
        page += 1

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
    if meta.get("has_releases") and meta.get("downloads_total"):
        latest = format_count(meta.get("downloads_latest", 0))
        total = format_count(meta.get("downloads_total", 0))
        parts.append(f"[downloads: {latest} / {total}]({url}/releases)")
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
