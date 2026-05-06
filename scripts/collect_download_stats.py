#!/usr/bin/env python3
"""Maintain a single JSON record of GitHub release download counts across all repos.

Loads data/release_downloads.json (if it exists), refreshes every active asset
from the API, marks anything that disappeared upstream as removed (preserving
the last-known download_count), and writes the file back. Sorted output keeps
git diffs stable so historical evolution can be read from `git log -p`.
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USERNAME = "jmylchreest"
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "release_downloads.json"
SCHEMA_VERSION = 1

API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
REQUEST_TIMEOUT = (10, 30)  # (connect, read) seconds

token = os.environ.get("GH_TOKEN")
if token:
    HEADERS["Authorization"] = f"Bearer {token}"


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


def asset_record(owner, repo_name, rel, asset, first_seen):
    return {
        "owner": owner,
        "repo": repo_name,
        "release_id": rel["id"],
        "release_tag": rel.get("tag_name"),
        "release_name": rel.get("name"),
        "release_published_at": rel.get("published_at"),
        "release_created_at": rel.get("created_at"),
        "release_prerelease": rel.get("prerelease", False),
        "release_draft": rel.get("draft", False),
        "asset_id": asset["id"],
        "asset_name": asset.get("name"),
        "asset_size": asset.get("size"),
        "asset_content_type": asset.get("content_type"),
        "asset_created_at": asset.get("created_at"),
        "asset_updated_at": asset.get("updated_at"),
        "download_count": asset.get("download_count", 0),
        "first_seen": first_seen,
        "status": "active",
    }


def main():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    old_state = {}
    old_by_key = {}
    if DATA_FILE.exists():
        with DATA_FILE.open() as f:
            old_state = json.load(f)
        for a in old_state.get("assets", []):
            key = (a["owner"], a["repo"], a["release_id"], a["asset_id"])
            old_by_key[key] = a

    repos = fetch_repos()
    print(f"Scanning {len(repos)} repos.")

    new_by_key = {}
    for repo in repos:
        owner = repo["owner"]["login"]
        name = repo["name"]
        for rel in fetch_releases(owner, name):
            for asset in (rel.get("assets") or []):
                key = (owner, name, rel["id"], asset["id"])
                prior = old_by_key.get(key)
                first_seen = (prior or {}).get("first_seen", ts)
                new_by_key[key] = asset_record(owner, name, rel, asset, first_seen)

    # Carry forward anything that disappeared upstream — preserve last-known data,
    # flip status to "removed" (only stamp removed_at on the transition).
    for key, prior in old_by_key.items():
        if key in new_by_key:
            continue
        carried = dict(prior)
        if carried.get("status") != "removed":
            carried["status"] = "removed"
            carried["removed_at"] = ts
        new_by_key[key] = carried

    sorted_assets = sorted(
        new_by_key.values(),
        key=lambda a: (a["owner"], a["repo"], a["release_id"], a["asset_id"]),
    )

    # No-op when nothing material changed (avoids hourly noise commits).
    if sorted_assets == old_state.get("assets"):
        print("No changes since last snapshot.")
        return

    active = sum(1 for a in sorted_assets if a["status"] == "active")
    removed = sum(1 for a in sorted_assets if a["status"] == "removed")
    prior_active = sum(1 for a in old_state.get("assets", []) if a.get("status") == "active")
    prior_removed = sum(1 for a in old_state.get("assets", []) if a.get("status") == "removed")

    output = {
        "_meta": {
            "username": USERNAME,
            "schema_version": SCHEMA_VERSION,
            "last_updated": ts,
            "total_assets": len(sorted_assets),
            "active_assets": active,
            "removed_assets": removed,
        },
        "assets": sorted_assets,
    }

    with DATA_FILE.open("w") as f:
        json.dump(output, f, indent=2, sort_keys=True)
        f.write("\n")

    print(
        f"Wrote {len(sorted_assets)} assets "
        f"(active {prior_active}->{active}, removed {prior_removed}->{removed})."
    )


if __name__ == "__main__":
    main()
