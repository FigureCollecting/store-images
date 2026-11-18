#!/usr/bin/env python3
"""Fail loudly when upstream has published a stable release newer than an image's base tag.

For every images/*.env: list the upstream repository's GitHub releases, ignore drafts, pre-releases
and tags that are not plain vX.Y.Z, and compare the newest with UPSTREAM_TAG. Every other
<KEY>_REPO / <KEY>_TAG pair of the definition (a component baked into the image) is checked the same way.
Exit 1 if any image has a newer upstream release, 2 if a check could not be made, 0 otherwise.
A newer release is the signal to re-pin consumers to upstream's own image and archive this repository,
or, for a definition with WATCH_ACTION=rebuild, to re-pin the recipe and rebuild. Opens nothing.
"""
import argparse
import glob
import json
import os
import re
import sys
import urllib.request

SEMVER = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
GITHUB = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?$")
# What a newer release asks for, chosen by WATCH_ACTION in the definition (default: archive).
ACTIONS = {
    "archive": "Check its image's Go version, re-pin to upstream's image, and archive this repository.",
    "rebuild": "Re-pin images/{image}.env and its Dockerfile to it, rebuild and bump FC_REVISION.",
}


def github_releases(owner_repo, opener=urllib.request.urlopen):
    req = urllib.request.Request(f"https://api.github.com/repos/{owner_repo}/releases?per_page=50",
                                 headers={"Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with opener(req, timeout=30) as resp:
        return json.load(resp)


def read_env(path):
    values = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw and not raw.startswith("#") and "=" in raw:
                key, value = raw.split("=", 1)
                values[key] = value
    return values


def version(tag):
    m = SEMVER.match(tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def newest_stable(releases):
    tags = [r.get("tag_name") for r in releases if not r.get("draft") and not r.get("prerelease")]
    return max((t for t in tags if version(t)), key=version, default=None)


def watched(env):
    """The key prefixes to check: UPSTREAM, then every other <KEY>_REPO of the definition, sorted."""
    return ["UPSTREAM"] + sorted(k[: -len("_REPO")] for k in env if k.endswith("_REPO") and k != "UPSTREAM_REPO")


def check(name, env, fetch, key="UPSTREAM"):
    """Return (status, message) for <key>_REPO at <key>_TAG: 0 current, 1 newer upstream release, 2 cannot tell."""
    label = name if key == "UPSTREAM" else f"{name} {key}"
    repo = GITHUB.match(env.get(f"{key}_REPO", ""))
    if not repo:
        return 2, f"{label}: {key}_REPO is not a github.com repository"
    base = env.get(f"{key}_TAG", "")
    if not version(base):
        return 2, f"{label}: {key}_TAG '{base}' is not vX.Y.Z"
    try:
        newest = newest_stable(fetch(repo.group(1)))
    except (OSError, ValueError) as exc:
        return 2, f"{label}: {exc}"
    if newest is None:
        return 2, f"{label}: no stable release found in {repo.group(1)}"
    if version(newest) > version(base):
        step = ACTIONS[env.get("WATCH_ACTION", "archive")].format(image=name)
        return 1, (f"::error title=Upstream released {label} {newest}::{label}: base {base}, upstream released "
                   f"{newest}. {step}")
    return 0, f"{label}: base {base}, newest upstream release {newest}: no newer release"


def check_image(name, env, fetch):
    """Return the (status, message) of every watched repository of one image definition."""
    action = env.get("WATCH_ACTION", "archive")
    if action not in ACTIONS:
        return [(2, f"{name}: WATCH_ACTION '{action}' is not {' or '.join(sorted(ACTIONS))}")]
    return [check(name, env, fetch, key) for key in watched(env)]


def main(argv=None, fetch=github_releases):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--images-dir", default=os.path.join(os.path.dirname(__file__), "..", "images"))
    args = ap.parse_args(argv)
    paths = sorted(glob.glob(os.path.join(args.images_dir, "*.env")))
    if not paths:
        print(f"::error title=Upstream watch failed::no image definitions in {args.images_dir}")
        return 2
    worst = 0
    for path in paths:
        name = os.path.basename(path)[: -len(".env")]
        for status, message in check_image(name, read_env(path), fetch):
            print(message if status != 2 else f"::error title=Upstream watch failed::{message}")
            worst = max(worst, status)
    return worst


if __name__ == "__main__":
    sys.exit(main())
