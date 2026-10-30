#!/usr/bin/env python3
"""Test version of the Portfolio's update_devlog.py: same commit reading, text output.

Usage: update_file.py <clone of the source repo> <text file>

A commit is marked when the last paragraph of its message holds the trailer
"Devlog: <title>", optionally followed by "Devlog-Tags: <tag>, <tag>".
Git itself parses the trailers (git help interpret-trailers), so a trailer
counts only in a last paragraph made of trailers alone, never in the subject.

Each marked commit becomes one line, newest first:
    <push time> | <title> | <tags> | <full SHA>
The push time is when the commit reached GitHub, read from the source repo's
activity list (GET /repos/{owner}/{repo}/activity). Git itself only stores when
a commit was made, not when it was pushed. If GitHub can't be reached, the
run fails without writing anything, so the next run retries. If GitHub answers
but has no push for a commit, its line gets the commit's own time followed by
"(commit time)".

A line is matched by the SHA at its end and never changed once written, so
hand edits survive and running twice adds nothing twice. Lines without a SHA
at the end (written by hand) are kept as they are.

Environment: GH_TOKEN (optional) authenticates the activity request.
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIME_ZONE = "Europe/Rome"  # push times are written in this time zone

# One record per commit: SHA, author date, Devlog values, Devlog-Tags values.
# Fields end with 0x1F, records with 0x1E; repeated trailers are joined by 0x1D.
LOG_FORMAT = (
    "%H%x1f"
    "%aI%x1f"
    "%(trailers:key=Devlog,valueonly,unfold,separator=%x1d)%x1f"
    "%(trailers:key=Devlog-Tags,valueonly,unfold,separator=%x1d)%x1e"
)
SHA_AT_END = re.compile(r"\|\s*([0-9a-f]{40})\s*$")
NEXT_PAGE = re.compile(r'<([^>]+)>;\s*rel="next"')
NO_COMMIT = "0" * 40  # "before" of a branch's first push


def git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8")


def read_marked_commits(repo: str) -> list[dict]:
    """Return one entry per commit whose message has a non-empty Devlog trailer, newest first."""
    if git(repo, "rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
        print("The source repo has no commits yet.")
        return []

    log = subprocess.run(
        ["git", "-C", repo, "log", f"--format={LOG_FORMAT}"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout

    entries = []
    for record in log.split("\x1e"):
        record = record.strip("\n")  # git log puts a newline between records
        if not record:
            continue
        sha, date, titles, tags = record.split("\x1f")
        title = titles.split("\x1d")[0].strip()  # with several Devlog lines, the first wins
        if not title:
            continue
        entries.append({
            "sha": sha,
            "commit_date": date,  # author date, ISO 8601 with its UTC offset
            "title": title,
            "tags": [t.strip() for t in tags.replace("\x1d", ",").split(",") if t.strip()],
        })
    return entries


def fetch_activity_pages(owner: str, name: str, branch: str, token: str | None):
    """Yield pages of the branch's activity (pushes, merges...), newest first."""
    url = (f"https://api.github.com/repos/{owner}/{name}/activity"
           f"?ref=refs/heads/{branch}&direction=desc&per_page=100")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "devlog-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    while url:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            yield json.load(response)
            match = NEXT_PAGE.search(response.headers.get("Link", ""))
        url = match.group(1) if match else None


def commits_introduced(repo: str, before: str | None, after: str) -> list[str]:
    """Commits an activity added to the branch: reachable from after, not from before."""
    if git(repo, "cat-file", "-e", f"{after}^{{commit}}").returncode != 0:
        return []  # no longer on the branch (e.g. overwritten by a force push)
    args = ["rev-list", after]
    if before and before != NO_COMMIT and git(repo, "cat-file", "-e", f"{before}^{{commit}}").returncode == 0:
        args += ["--not", before]
    result = git(repo, *args)
    return result.stdout.split() if result.returncode == 0 else []


def find_push_times(repo: str, wanted: set[str], pages) -> dict[str, str]:
    """Map each wanted SHA to the timestamp of the earliest activity that brought it to the branch."""
    found: dict[str, str] = {}
    for page in pages:
        for activity in page:  # newest first, so an older activity overwrites a newer one
            if activity.get("activity_type") == "branch_deletion":
                continue
            for sha in commits_introduced(repo, activity.get("before"), activity["after"]):
                if sha in wanted:
                    found[sha] = activity["timestamp"]
        if wanted <= found.keys():
            break
    return found


def local_time(timestamp: str) -> str:
    """'2026-10-30T10:27:15Z' -> '2026-10-30T11:27:15+01:00' in TIME_ZONE (UTC if unavailable)."""
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    try:
        return moment.astimezone(ZoneInfo(TIME_ZONE)).isoformat()
    except ZoneInfoNotFoundError:
        return moment.astimezone(timezone.utc).isoformat()


def source_repo_name(repo: str) -> tuple[str, str, str]:
    """Owner, name and default branch of the cloned source repo."""
    url = git(repo, "remote", "get-url", "origin").stdout.strip()
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not match:
        sys.exit(f"Can't read owner/repo from the source remote URL: {url!r}")
    branch = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() or "main"
    return match.group(1), match.group(2), branch


def add_push_times(repo: str, entries: list[dict]) -> None:
    """Give each entry a "time": its push time, or its commit time marked as such."""
    owner, name, branch = source_repo_name(repo)
    token = os.environ.get("GH_TOKEN")
    # The workflow's token belongs to this repo; if GitHub refuses it for the
    # source repo, retry once without a token (the source repo is public).
    for attempt_token in ([token, None] if token else [None]):
        try:
            pages = fetch_activity_pages(owner, name, branch, attempt_token)
            push_times = find_push_times(repo, {e["sha"] for e in entries}, pages)
            break
        except urllib.error.HTTPError as error:
            if attempt_token and error.code in (401, 403, 404):
                print(f"Activity request refused with the token ({error}); retrying without it.")
                continue
            sys.exit(f"Couldn't read push times from GitHub ({error}); nothing was added.")
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as error:
            # Fail without writing anything: the next run retries and still finds these entries.
            sys.exit(f"Couldn't read push times from GitHub ({error}); nothing was added.")
    for entry in entries:
        if entry["sha"] in push_times:
            entry["time"] = local_time(push_times[entry["sha"]])
        else:
            print(f"Warning: no push found for {entry['sha'][:7]}; using its commit time.")
            entry["time"] = f"{entry['commit_date']} (commit time)"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    repo, text_path = sys.argv[1], Path(sys.argv[2])

    lines = text_path.read_text(encoding="utf-8").splitlines() if text_path.exists() else []
    known = {match.group(1) for line in lines if (match := SHA_AT_END.search(line))}

    new = [entry for entry in read_marked_commits(repo) if entry["sha"] not in known]
    if not new:
        print("No new entries.")
        return 0

    add_push_times(repo, new)
    new_lines = [f"{e['time']} | {e['title']} | {', '.join(e['tags'])} | {e['sha']}" for e in new]
    text_path.write_text("\n".join(new_lines + lines) + "\n", encoding="utf-8")
    for entry in new:
        print(f"Added: {entry['title']} ({entry['sha'][:7]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
