#!/usr/bin/env python3
"""Add this repository to the catalog that the Marketplace and HACS read.

The catalog is the ``integration`` file of hacs/default, a sorted JSON list of
``owner/name``. A repository gets in with a pull request to that file, and the
HACS team reviews it against the checks below. This script reads every one of
them from GitHub, so a pull request is opened only when it can pass.

Usage::

    scripts/catalog_pr.py check              # run the checks, change nothing
    scripts/catalog_pr.py open --dry-run     # also show the change and the pull request text
    scripts/catalog_pr.py open               # fork hacs/default, add the entry, open the pull request

The repository defaults to the one ``manifest.json`` names under ``documentation``.
Everything goes through the GitHub CLI (``gh``), signed in as the person who opens
the pull request: the catalog accepts it only from a personal account, from the
owner of the repository or one of its main contributors.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "prusa_connect_ce" / "manifest.json"
CATALOG = "hacs/default"
CATALOG_BRANCH = "master"
CATALOG_FILE = "integration"
VALIDATE_WORKFLOW = "validate.yml"
# Job ids in .github/workflows/validate.yml.
HACS_JOB = "hacs"
HASSFEST_JOB = "hassfest"
MANIFEST_KEYS = ("domain", "documentation", "issue_tracker", "codeowners", "name", "version")


class GhError(Exception):
    """A gh command failed."""


def gh(*args: str, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GhError(result.stderr.strip() or f"gh {' '.join(args)} failed")
    return result.stdout


def api(path: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    args = ["api", "-X", method, path]
    if body is not None:
        args += ["--input", "-"]
    out = gh(*args, stdin=json.dumps(body) if body is not None else None)
    return json.loads(out) if out.strip() else None


def api_or_none(path: str) -> Any:
    try:
        return api(path)
    except GhError as err:
        if "Not Found" in str(err) or "HTTP 404" in str(err):
            return None
        raise


def file_at(repo: str, path: str, ref: str) -> str | None:
    found = api_or_none(f"repos/{repo}/contents/{path}?ref={ref}")
    if not isinstance(found, dict) or found.get("type") != "file":
        return None
    return base64.b64decode(found["content"]).decode()


def default_repo() -> str:
    url = str(json.loads(MANIFEST.read_text())["documentation"])
    match = re.match(r"https://github\.com/([^/]+/[^/#?]+)", url)
    if not match:
        sys.exit(f"manifest.json documentation is not a GitHub repository: {url}; pass --repo")
    return match.group(1).removesuffix(".git")


@dataclass
class Report:
    failures: int = 0
    links: dict[str, str] = field(default_factory=dict)

    def ok(self, text: str) -> None:
        print(f"ok    {text}")

    def warn(self, text: str) -> None:
        print(f"warn  {text}")

    def fail(self, text: str) -> None:
        print(f"FAIL  {text}")
        self.failures += 1


def check_repository(repo: str, report: Report) -> dict[str, Any] | None:
    info: dict[str, Any] | None = api_or_none(f"repos/{repo}")
    if info is None:
        report.fail(f"{repo} does not exist, or this account cannot see it")
        return None
    if info["visibility"] == "public":
        report.ok("public")
    else:
        report.fail(f"the repository is {info['visibility']}; the catalog takes public repositories only")
    if info["archived"]:
        report.fail("the repository is archived")
    if info.get("description"):
        report.ok(f"description: {info['description']}")
    else:
        report.fail("no description")
    if info.get("topics"):
        report.ok(f"topics: {', '.join(info['topics'])}")
    else:
        report.fail("no topics")
    if info["has_issues"]:
        report.ok("issues on")
    else:
        report.fail("issues are off")
    spdx = (info.get("license") or {}).get("spdx_id")
    if spdx and spdx not in ("NOASSERTION", "NONE"):
        report.ok(f"licence {spdx}")
    else:
        report.fail("GitHub does not recognise a licence")
    return info


def check_release(repo: str, report: Report) -> dict[str, Any] | None:
    # releases/latest skips drafts and pre-releases, as the review does.
    release: dict[str, Any] | None = api_or_none(f"repos/{repo}/releases/latest")
    if release is None:
        report.fail("no full release; publish one that is not marked as a pre-release")
        return None
    tag = str(release["tag_name"])
    report.ok(f"latest release {tag}, published {release['published_at']}")
    report.links["release"] = str(release["html_url"])

    hacs_text = file_at(repo, "hacs.json", tag)
    hacs: dict[str, Any] = json.loads(hacs_text) if hacs_text else {}
    if not hacs.get("name"):
        report.fail(f"hacs.json at {tag} is missing or has no name")
    elif hacs.get("zip_release"):
        assets = {asset["name"] for asset in release["assets"]}
        if hacs.get("filename") in assets:
            report.ok(f"release asset {hacs['filename']}")
        else:
            report.fail(
                f"hacs.json asks for the asset {hacs.get('filename')}, the release has {sorted(assets)}"
            )

    domains = api_or_none(f"repos/{repo}/contents/custom_components?ref={tag}") or []
    folders = [entry["name"] for entry in domains if entry["type"] == "dir"]
    if len(folders) != 1:
        report.fail(f"custom_components at {tag} must hold exactly one integration, not {folders}")
        return release
    domain = folders[0]
    manifest_text = file_at(repo, f"custom_components/{domain}/manifest.json", tag)
    manifest: dict[str, Any] = json.loads(manifest_text) if manifest_text else {}
    missing = [key for key in MANIFEST_KEYS if key not in manifest]
    if missing:
        report.fail(f"manifest.json at {tag} lacks {', '.join(missing)}")
    if manifest.get("version") == tag.removeprefix("v"):
        report.ok(f"manifest.json version {manifest['version']} matches the tag")
    else:
        report.fail(f"manifest.json version {manifest.get('version')} does not match the tag {tag}")
    if not manifest.get("codeowners"):
        report.warn("manifest.json codeowners is empty; name who answers the issues")
    if file_at(repo, f"custom_components/{domain}/brand/icon.png", tag) is None:
        report.fail(f"no custom_components/{domain}/brand/icon.png at {tag}")
    else:
        report.ok("brand/icon.png")
    return release


def check_validation(repo: str, branch: str, release: dict[str, Any] | None, report: Report) -> None:
    workflow = file_at(repo, f".github/workflows/{VALIDATE_WORKFLOW}", branch)
    if workflow is None:
        report.fail(f"no .github/workflows/{VALIDATE_WORKFLOW} on {branch}")
        return
    if re.search(r"^\s*ignore:", workflow, re.MULTILINE):
        report.fail(f"{VALIDATE_WORKFLOW} skips HACS checks with ignore; the catalog refuses that")
    runs = api(
        f"repos/{repo}/actions/workflows/{VALIDATE_WORKFLOW}/runs?branch={branch}&status=completed&per_page=30"
    )["workflow_runs"]
    if not runs:
        report.fail(f"{VALIDATE_WORKFLOW} has never run on {branch}")
        return
    published = str(release["published_at"]) if release else None
    for index, run in enumerate(runs):
        jobs = {job["name"]: job for job in api(f"repos/{repo}/actions/runs/{run['id']}/jobs")["jobs"]}
        conclusions = {
            name: jobs.get(name, {}).get("conclusion") or "missing" for name in (HACS_JOB, HASSFEST_JOB)
        }
        passed = all(conclusion == "success" for conclusion in conclusions.values())
        if index == 0 and not passed:
            # A private repository skips the HACS job, so it never passes there.
            states = ", ".join(f"{name} {conclusion}" for name, conclusion in conclusions.items())
            report.warn(f"the newest Validate run on {branch}: {states}, {run['html_url']}")
        # The template asks for a release made after the actions passed.
        if passed and (published is None or str(run["created_at"]) <= published):
            report.ok(f"HACS action and hassfest passed in run {run['html_url']}")
            report.links["hacs"] = str(jobs[HACS_JOB]["html_url"])
            report.links["hassfest"] = str(jobs[HASSFEST_JOB]["html_url"])
            break
    else:
        report.fail(
            "no Validate run where both the HACS action and hassfest passed before the latest release; "
            "make it pass, then publish a release"
        )


def check_catalog(repo: str, report: Report) -> bool:
    entries = json.loads(gh("api", "-H", "Accept: application/vnd.github.raw", catalog_path()))
    if repo.casefold() in (entry.casefold() for entry in entries):
        report.fail(f"{repo} is in the catalog already")
        return False
    report.ok("not in the catalog yet")
    found = api(f"search/issues?q=repo:{CATALOG}+is:pr+is:open+in:title+{repo}")
    for pr in found["items"]:
        report.fail(f"an open pull request adds it already: {pr['html_url']}")
        return False
    return True


def check_submitter(repo: str, report: Report) -> str:
    login = str(api("user")["login"])
    contributors = api(f"repos/{repo}/contributors?per_page=100") or []
    if login in (person["login"] for person in contributors):
        report.ok(f"{login} is a contributor of {repo}")
    else:
        report.warn(f"{login} is not among the contributors of {repo}; the review closes such pull requests")
    return login


def catalog_path() -> str:
    return f"repos/{CATALOG}/contents/{CATALOG_FILE}?ref={CATALOG_BRANCH}"


def check(repo: str) -> Report:
    report = Report()
    print(f"== {repo}")
    info = check_repository(repo, report)
    if info is None:
        return report
    print("== release")
    release = check_release(repo, report)
    print("== validation")
    check_validation(repo, str(info["default_branch"]), release, report)
    print("== catalog")
    check_catalog(repo, report)
    check_submitter(repo, report)
    return report


def pull_request_body(links: dict[str, str]) -> str:
    """The catalog's own template, ticked and with the links filled in."""
    template = gh(
        "api",
        "-H",
        "Accept: application/vnd.github.raw",
        f"repos/{CATALOG}/contents/.github/PULL_REQUEST_TEMPLATE.md",
    )
    body = template.replace("- [ ]", "- [x]")
    for label, key in (
        ("Link to current release:", "release"),
        ("Link to successful HACS action", "hacs"),
        ("Link to successful hassfest action", "hassfest"),
    ):
        pattern = re.compile(rf"^({re.escape(label)}.*?)<>[ \t]*$", re.MULTILINE)
        body, count = pattern.subn(r"\g<1><" + links[key].replace("\\", r"\\") + ">", body)
        if count != 1:
            sys.exit(
                f"the pull request template of {CATALOG} changed ({label!r}); open the pull request by hand"
            )
    return body


def catalog_change(repo: str) -> tuple[str, str, str]:
    """The catalog file's blob sha, its text, and the text with the repository added."""
    meta = api(catalog_path())
    text = gh("api", "-H", "Accept: application/vnd.github.raw", catalog_path())
    entries: list[str] = json.loads(text)
    if entries != sorted(entries, key=str.casefold):
        sys.exit(f"{CATALOG}/{CATALOG_FILE} is not sorted on {CATALOG_BRANCH}; open the pull request by hand")
    if json.dumps(entries, indent=2) + "\n" != text:
        sys.exit(f"{CATALOG}/{CATALOG_FILE} changed its layout; open the pull request by hand")
    added = json.dumps(sorted([*entries, repo], key=str.casefold), indent=2) + "\n"
    return str(meta["sha"]), text, added


def fork_for(login: str) -> str:
    fork = str(api(f"repos/{CATALOG}/forks", method="POST")["full_name"])
    if fork.split("/", maxsplit=1)[0] != login:
        sys.exit(f"the fork landed in {fork}, not under {login}")
    # GitHub creates the fork in the background.
    for _ in range(30):
        if api_or_none(f"repos/{fork}") is not None:
            break
        time.sleep(2)
    else:
        sys.exit(f"{fork} did not appear")
    try:
        api(f"repos/{fork}/merge-upstream", method="POST", body={"branch": CATALOG_BRANCH})
    except GhError as err:
        print(f"warn  could not sync {fork} with {CATALOG}: {err}")
    return fork


def open_pull_request(repo: str, dry_run: bool) -> int:
    report = check(repo)
    if report.failures:
        print(f"\n{report.failures} check(s) failed; no pull request.")
        return 1
    blob, before, after = catalog_change(repo)
    body = pull_request_body(report.links)
    title = f"Add {repo}"
    if dry_run:
        print(f"\n== {CATALOG_FILE}")
        sys.stdout.writelines(
            difflib.unified_diff(
                before.splitlines(True), after.splitlines(True), CATALOG_FILE, CATALOG_FILE, n=2
            )
        )
        print(f"\n== pull request: {title}\n{body}")
        return 0

    login = str(api("user")["login"])
    fork = fork_for(login)
    branch = "add-" + repo.replace("/", "-").lower()
    base = str(api(f"repos/{CATALOG}/git/ref/heads/{CATALOG_BRANCH}")["object"]["sha"])
    if api_or_none(f"repos/{fork}/git/ref/heads/{branch}") is None:
        api(f"repos/{fork}/git/refs", method="POST", body={"ref": f"refs/heads/{branch}", "sha": base})
    else:
        api(f"repos/{fork}/git/refs/heads/{branch}", method="PATCH", body={"sha": base, "force": True})
    api(
        f"repos/{fork}/contents/{CATALOG_FILE}",
        method="PUT",
        body={
            "message": title,
            "content": base64.b64encode(after.encode()).decode(),
            "sha": blob,
            "branch": branch,
        },
    )
    # gh lets the maintainers edit the pull request by default, which the catalog requires.
    url = gh(
        "pr", "create", "--repo", CATALOG, "--base", CATALOG_BRANCH, "--head", f"{login}:{branch}",
        "--title", title, "--body", body,
    ).strip()  # fmt: skip
    print(f"\nopened {url}\nDo not request reviews: the HACS team closes pull requests that do.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo", help="owner/name, by default the repository manifest.json documents")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="run the catalog checks against GitHub")
    open_parser = commands.add_parser("open", help="open the pull request to hacs/default")
    open_parser.add_argument(
        "--dry-run", action="store_true", help="show the change and the text, open nothing"
    )
    args = parser.parse_args()
    repo = args.repo or default_repo()
    try:
        if args.command == "check":
            report = check(repo)
            print(f"\n{report.failures} check(s) failed." if report.failures else "\nReady for the catalog.")
            return 1 if report.failures else 0
        return open_pull_request(repo, args.dry_run)
    except GhError as err:
        print(f"gh: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
