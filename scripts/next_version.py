#!/usr/bin/env python3
"""Work out the next release version and write it into manifest.json.

Usage::

    scripts/next_version.py <stable|beta|alpha> <patch|minor|major>          # print it
    scripts/next_version.py <stable|beta|alpha> <patch|minor|major> --write  # also set it

The version in ``manifest.json`` is where the cycle stands. A stable version
opens a new cycle moved by ``bump``; a pre-release keeps its cycle, so the bump
is ignored until the cycle ends with its stable release:

    0.1.1         stable patch -> 0.1.2     beta minor -> 0.2.0-beta.1
    0.2.0-beta.1  beta         -> 0.2.0-beta.2
    0.2.0-alpha.3 beta         -> 0.2.0-beta.1
    0.2.0-beta.2  stable       -> 0.2.0
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "custom_components" / "prusa_connect_ce" / "manifest.json"
STAGES = ("alpha", "beta")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta)\.(\d+))?")


def next_version(current: str, kind: str, bump: str) -> str:
    match = _VERSION.fullmatch(current)
    if match is None:
        raise ValueError(f"{current!r} is not a version this script moves")
    major, minor, patch = (int(part) for part in match.groups()[:3])
    stage, number = match.group(4), match.group(5)

    if stage is None:
        if bump == "major":
            major, minor, patch = major + 1, 0, 0
        elif bump == "minor":
            minor, patch = minor + 1, 0
        elif bump == "patch":
            patch += 1
        else:
            raise ValueError(f"{bump!r} is not patch, minor or major")
        base = f"{major}.{minor}.{patch}"
        return base if kind == "stable" else f"{base}-{kind}.1"

    base = f"{major}.{minor}.{patch}"
    if kind == "stable":
        return base
    if kind not in STAGES:
        raise ValueError(f"{kind!r} is not stable, beta or alpha")
    if STAGES.index(kind) < STAGES.index(stage):
        raise ValueError(f"{current} is past {kind}; release a {stage} or the stable {base}")
    if kind == stage:
        return f"{base}-{kind}.{int(number) + 1}"
    return f"{base}-{kind}.1"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("kind", choices=["stable", *STAGES])
    parser.add_argument("bump", choices=["patch", "minor", "major"])
    parser.add_argument("--write", action="store_true", help="set the version in manifest.json")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text())
    try:
        version = next_version(manifest["version"], args.kind, args.bump)
    except ValueError as err:
        print(err, file=sys.stderr)
        return 1
    if args.write:
        manifest["version"] = version
        MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
