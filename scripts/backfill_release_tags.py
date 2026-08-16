#!/usr/bin/env python3
"""One-shot migration: recover tag names for archived releases that lost them.

Before the carry-forward fix, a release deleted upstream was rebuilt in the
archive with `tag_name: None` — its downloads were still counted, but they were
no longer attributable to a version.

We recover the tag by reading the version out of the release's asset filenames
and then confirming it against the repo's real git tags, which survive deletion
of the GitHub *release*. A tag is only written if it matches a tag that actually
exists, so this never invents a version. Recovered entries are marked
`tag_source: "inferred"`.

Dry run by default; pass --apply to write.
"""

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USERNAME = "jmylchreest"
REPOS_DIR = Path(__file__).resolve().parent.parent / "data" / "release_downloads" / "repos"

API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
REQUEST_TIMEOUT = (10, 30)

token = os.environ.get("GH_TOKEN")
if token:
    HEADERS["Authorization"] = f"Bearer {token}"

# A semver-ish token: 1.2.3, 1.2.3-preview.4, 1.2.3-rc1+abc123
VERSION_RE = re.compile(r"^v?(\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.+-]+)?)$")
STRIP_EXTS = (
    ".tar.gz", ".tar.xz", ".tar.bz2", ".tgz", ".zip", ".deb", ".rpm", ".apk",
    ".exe", ".msi", ".dmg", ".pkg", ".AppImage", ".gz", ".sig", ".asc",
    ".sha256", ".txt", ".json", ".yaml", ".yml",
)


def _build_session():
    s = requests.Session()
    retry = Retry(
        total=6,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s


SESSION = _build_session()


def fetch_tags(owner, name):
    """Every git tag in the repo. These outlive the releases that referenced them."""
    tags = set()
    page = 1
    while True:
        resp = SESSION.get(
            f"{API}/repos/{owner}/{name}/tags",
            params={"per_page": 100, "page": page},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            break
        batch = resp.json()
        if not batch:
            break
        tags.update(t["name"] for t in batch)
        if len(batch) < 100:
            break
        page += 1
    return tags


def candidates_from_name(name):
    """Version-looking tokens in an asset filename."""
    if not name:
        return []
    stem = name
    for ext in STRIP_EXTS:
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    return [m.group(1) for m in (VERSION_RE.match(tok) for tok in stem.split("_")) if m]


def infer_version(assets, min_share=0.6):
    """Consensus version across a release's assets, or None if not confident."""
    votes = Counter()
    contributing = 0
    for a in assets or []:
        cands = set(candidates_from_name(a.get("name")))
        if cands:
            contributing += 1
            votes.update(cands)
    if not votes or not contributing:
        return None
    ranked = votes.most_common(2)
    top, n = ranked[0]
    if len(ranked) > 1 and ranked[1][1] == n:
        return None  # tie — ambiguous
    if n / contributing < min_share:
        return None
    return top


def resolve_tag(version, real_tags):
    """Map an inferred version onto a tag that actually exists, else None.

    Covers the observed conventions: bare version, `v` prefix, and asset names
    that carry `+build` metadata the tag omits.
    """
    if not version:
        return None
    base = version.split("+", 1)[0]
    for cand in (version, f"v{version}", base, f"v{base}"):
        if cand in real_tags:
            return cand
    return None


def main():
    apply = "--apply" in sys.argv
    if not REPOS_DIR.exists():
        print(f"No archive at {REPOS_DIR}", file=sys.stderr)
        return 1

    total_fixed = total_unresolved = total_downloads = 0

    for path in sorted(REPOS_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        untagged = [r for r in data.get("releases", []) if not r.get("tag_name")]
        if not untagged:
            continue

        owner = data.get("_meta", {}).get("owner", USERNAME)
        repo = data.get("_meta", {}).get("repo", path.stem)
        real_tags = fetch_tags(owner, repo)
        if not real_tags:
            print(f"{repo}: no tags readable upstream — skipping {len(untagged)} release(s)")
            total_unresolved += len(untagged)
            continue

        fixed, unresolved, recovered_downloads = 0, [], 0
        for rel in untagged:
            tag = resolve_tag(infer_version(rel.get("assets")), real_tags)
            if not tag:
                unresolved.append(rel["id"])
                continue
            rel["tag_name"] = tag
            rel["tag_source"] = "inferred"
            fixed += 1
            recovered_downloads += sum(a.get("download_count", 0) for a in rel["assets"])

        if fixed:
            print(f"{repo}: recovered {fixed} tag(s), {recovered_downloads} downloads now attributable")
            for rel in untagged:
                if rel.get("tag_source") == "inferred":
                    dl = sum(a.get("download_count", 0) for a in rel["assets"])
                    print(f"    id={rel['id']:<12} -> {rel['tag_name']:<40} {dl:>8} downloads")
        if unresolved:
            print(f"{repo}: {len(unresolved)} release(s) left untagged (no confident match)")

        total_fixed += fixed
        total_unresolved += len(unresolved)
        total_downloads += recovered_downloads

        if apply and fixed:
            path.write_text(json.dumps(data, indent=2) + "\n")

    verb = "Recovered" if apply else "Would recover"
    print(f"\n{verb} {total_fixed} tag(s) covering {total_downloads} downloads; "
          f"{total_unresolved} left untagged.")
    if not apply:
        print("Dry run — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
