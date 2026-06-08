#!/usr/bin/env python3
"""Maintain per-repo JSON records of GitHub release download counts plus a top-level manifest.

Layout:
  data/release_downloads/manifest.json        — owner-wide totals + per-repo summary
  data/release_downloads/repos/<repo>.json    — releases nested by id, assets nested by id

For each repo we refresh every active asset from the API, carry forward
anything that disappeared upstream as `status: "removed"` (preserving the
last-known download_count), and only rewrite a per-repo file when its
contents actually change. Hourly commits stay scoped to whichever repos
moved.
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from statsfactory import StatsFactory as _StatsFactory
except ImportError:
    _StatsFactory = None

USERNAME = "jmylchreest"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "release_downloads"
REPOS_DIR = DATA_DIR / "repos"
MANIFEST_FILE = DATA_DIR / "manifest.json"
SCHEMA_VERSION = 2

API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
REQUEST_TIMEOUT = (10, 30)  # (connect, read) seconds

token = os.environ.get("GH_TOKEN")
if token:
    HEADERS["Authorization"] = f"Bearer {token}"

_SF_SERVER_URL = os.environ.get("SF_SERVER_URL", "").rstrip("/")


def _sf_app_key(repo_name: str) -> str:
    env_var = f"SF_APP_API_KEY_{repo_name.upper().replace('-', '_')}"
    return os.environ.get(env_var, "")


def _sf_server_url() -> str:
    url = _SF_SERVER_URL
    if url and "://" not in url:
        url = "https://" + url
    return url


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


def fetch_repos():
    repos = []
    page = 1
    while True:
        resp = SESSION.get(
            f"{API}/users/{USERNAME}/repos",
            params={"type": "owner", "per_page": 100, "page": page},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for r in batch:
            if r.get("private") or r.get("fork"):
                continue
            if r["name"] == USERNAME:
                continue
            repos.append(r)
        if len(batch) < 100:
            break
        page += 1
    return repos


def fetch_releases(owner, name):
    releases = []
    page = 1
    while True:
        resp = SESSION.get(
            f"{API}/repos/{owner}/{name}/releases",
            params={"per_page": 100, "page": page},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return releases


def asset_record(asset, first_seen):
    return {
        "id": asset["id"],
        "name": asset.get("name"),
        "size": asset.get("size"),
        "content_type": asset.get("content_type"),
        "created_at": asset.get("created_at"),
        "updated_at": asset.get("updated_at"),
        "download_count": asset.get("download_count", 0),
        "first_seen": first_seen,
        "status": "active",
    }


def release_shell(rel):
    return {
        "id": rel["id"],
        "tag_name": rel.get("tag_name"),
        "name": rel.get("name"),
        "published_at": rel.get("published_at"),
        "created_at": rel.get("created_at"),
        "prerelease": rel.get("prerelease", False),
        "draft": rel.get("draft", False),
        "assets": [],
    }


def load_repo_file(repo_name):
    """Return prior file contents (or empty shell) and a (release_id, asset_id) -> asset map."""
    path = REPOS_DIR / f"{repo_name}.json"
    if not path.exists():
        return None, {}
    with path.open() as f:
        data = json.load(f)
    prior_assets = {}
    for rel in data.get("releases", []):
        for a in rel.get("assets", []):
            prior_assets[(rel["id"], a["id"])] = a
    return data, prior_assets


def build_repo_payload(owner, repo_name, releases_api, prior_assets, ts):
    """Reconcile API releases with prior state; carry-forward removed assets."""
    seen_keys = set()
    new_releases = {}

    for rel in releases_api:
        rid = rel["id"]
        bucket = new_releases.setdefault(rid, release_shell(rel))
        # If a release reappears with edits, refresh metadata too.
        bucket.update({k: v for k, v in release_shell(rel).items() if k != "assets"})
        for asset in (rel.get("assets") or []):
            aid = asset["id"]
            seen_keys.add((rid, aid))
            prior = prior_assets.get((rid, aid))
            first_seen = (prior or {}).get("first_seen", ts)
            bucket["assets"].append(asset_record(asset, first_seen))

    # Carry forward anything that disappeared upstream.
    for (rid, aid), prior in prior_assets.items():
        if (rid, aid) in seen_keys:
            continue
        bucket = new_releases.get(rid)
        if bucket is None:
            # The whole release disappeared — reconstruct shell from prior asset's release fields
            # by reading the prior release entry (we lost it, so fall back to a minimal record).
            bucket = new_releases.setdefault(rid, {
                "id": rid,
                "tag_name": None,
                "name": None,
                "published_at": None,
                "created_at": None,
                "prerelease": False,
                "draft": False,
                "assets": [],
            })
        carried = dict(prior)
        if carried.get("status") != "removed":
            carried["status"] = "removed"
            carried["removed_at"] = ts
        bucket["assets"].append(carried)

    # Sort releases (asc by id) and assets within each release (asc by id).
    sorted_releases = []
    for rid in sorted(new_releases):
        rel = new_releases[rid]
        rel["assets"] = sorted(rel["assets"], key=lambda a: a["id"])
        sorted_releases.append(rel)

    active = sum(1 for r in sorted_releases for a in r["assets"] if a["status"] == "active")
    removed = sum(1 for r in sorted_releases for a in r["assets"] if a["status"] == "removed")
    downloads = sum(a["download_count"] for r in sorted_releases for a in r["assets"])

    return {
        "_meta": {
            "owner": owner,
            "repo": repo_name,
            "schema_version": SCHEMA_VERSION,
            "last_updated": ts,
            "releases": len(sorted_releases),
            "active_assets": active,
            "removed_assets": removed,
            "total_downloads": downloads,
        },
        "releases": sorted_releases,
    }


def latest_release(payload):
    candidates = [r for r in payload["releases"] if not r.get("draft")]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.get("published_at") or "")


def write_if_changed(path, payload):
    """Write payload only if contents (excluding _meta.last_updated) differ from on-disk."""
    new_text = json.dumps(payload, indent=2) + "\n"
    if path.exists():
        old_payload = json.loads(path.read_text())
        # Compare ignoring last_updated to avoid spurious rewrites.
        old_cmp = dict(old_payload)
        new_cmp = dict(payload)
        old_cmp["_meta"] = {k: v for k, v in old_cmp.get("_meta", {}).items() if k != "last_updated"}
        new_cmp["_meta"] = {k: v for k, v in new_cmp.get("_meta", {}).items() if k != "last_updated"}
        if old_cmp == new_cmp:
            return False
    path.write_text(new_text)
    return True


def compute_version_deltas(releases_api, prior_assets):
    """Return {tag_name: new_download_count} for versions that gained downloads since last run."""
    deltas = {}
    for rel in releases_api:
        rid = rel["id"]
        tag = rel.get("tag_name") or f"release-{rid}"
        version_delta = 0
        for asset in (rel.get("assets") or []):
            aid = asset["id"]
            new_count = asset.get("download_count", 0)
            prior_count = (prior_assets.get((rid, aid)) or {}).get("download_count", 0)
            version_delta += max(0, new_count - prior_count)
        if version_delta > 0:
            deltas[tag] = version_delta
    return deltas


def push_download_events(server_url, app_key, repo_name, version_deltas, ts):
    """Emit one release_downloads event per version (value=delta) via the statsfactory SDK.

    Query with aggregation=sum to get cumulative download totals in the UI.
    """
    if not server_url or not app_key or not version_deltas or not _StatsFactory:
        return
    try:
        sf = _StatsFactory(
            server_url=server_url,
            app_key=app_key,
            client_name="collect_download_stats",
            flush_interval=0,  # manual flush only — script exits immediately after
        )
        for version, count in version_deltas.items():
            sf.track("release_downloads", {"repo": repo_name, "version": version}, value=float(count))
        sf.flush()
    except Exception as exc:
        print(f"  statsfactory: {repo_name}: {exc}", file=sys.stderr)


def main():
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    repos = fetch_repos()
    print(f"Scanning {len(repos)} repos.")

    seen_repo_files = set()
    repo_summaries = []
    changed = 0

    for repo in repos:
        owner = repo["owner"]["login"]
        name = repo["name"]
        seen_repo_files.add(f"{name}.json")

        prior_payload, prior_assets = load_repo_file(name)
        # Skip writing files for repos that have no releases now and never did.
        releases_api = fetch_releases(owner, name)
        if not releases_api and not prior_assets:
            continue

        payload = build_repo_payload(owner, name, releases_api, prior_assets, ts)
        path = REPOS_DIR / f"{name}.json"
        if write_if_changed(path, payload):
            changed += 1

        app_key = _sf_app_key(name)
        if app_key:
            version_deltas = compute_version_deltas(releases_api, prior_assets)
            push_download_events(_sf_server_url(), app_key, name, version_deltas, ts)
            if version_deltas:
                total = sum(version_deltas.values())
                print(f"  statsfactory: {name}: pushed {total} new downloads across {len(version_deltas)} version(s)")

        latest = latest_release(payload)
        repo_summaries.append({
            "name": name,
            "releases": payload["_meta"]["releases"],
            "active_assets": payload["_meta"]["active_assets"],
            "removed_assets": payload["_meta"]["removed_assets"],
            "total_downloads": payload["_meta"]["total_downloads"],
            "latest_release_tag": (latest or {}).get("tag_name"),
            "latest_release_published_at": (latest or {}).get("published_at"),
        })

    # Drop per-repo files for repos that no longer exist on the user's account.
    for f in REPOS_DIR.glob("*.json"):
        if f.name not in seen_repo_files:
            print(f"Removing stale {f.name}")
            f.unlink()

    repo_summaries.sort(key=lambda r: r["name"])
    manifest = {
        "_meta": {
            "owner": USERNAME,
            "schema_version": SCHEMA_VERSION,
            "last_updated": ts,
            "total_repos": len(repo_summaries),
            "total_releases": sum(r["releases"] for r in repo_summaries),
            "total_assets": sum(r["active_assets"] + r["removed_assets"] for r in repo_summaries),
            "active_assets": sum(r["active_assets"] for r in repo_summaries),
            "removed_assets": sum(r["removed_assets"] for r in repo_summaries),
            "total_downloads": sum(r["total_downloads"] for r in repo_summaries),
        },
        "repos": repo_summaries,
    }
    write_if_changed(MANIFEST_FILE, manifest)

    print(f"Updated {changed} repo file(s); manifest covers {len(repo_summaries)} repos.")


if __name__ == "__main__":
    main()
