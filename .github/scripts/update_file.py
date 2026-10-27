#!/usr/bin/env python3
"""Test version of the Portfolio's update_devlog.py: same commit reading, text output.

Usage: update_file.py <clone of the source repo> <text file>

A commit is marked when the last paragraph of its message holds the trailer
"Devlog: <title>", optionally followed by "Devlog-Tags: <tag>, <tag>".
Git itself parses the trailers (git help interpret-trailers), so a trailer
counts only in a last paragraph made of trailers alone, never in the subject.

Each marked commit becomes one line, newest first:
    <author date> | <title> | <tags> | <full SHA>
A line is matched by the SHA at its end and never changed once written, so
hand edits survive and running twice adds nothing twice. Lines without a SHA
at the end (written by hand) are kept as they are.
"""

import re
import subprocess
import sys
from pathlib import Path

# One record per commit: SHA, author date, Devlog values, Devlog-Tags values.
# Fields end with 0x1F, records with 0x1E; repeated trailers are joined by 0x1D.
LOG_FORMAT = (
    "%H%x1f"
    "%aI%x1f"
    "%(trailers:key=Devlog,valueonly,unfold,separator=%x1d)%x1f"
    "%(trailers:key=Devlog-Tags,valueonly,unfold,separator=%x1d)%x1e"
)
SHA_AT_END = re.compile(r"\|\s*([0-9a-f]{40})\s*$")


def read_marked_commits(repo: str) -> list[dict]:
    """Return one entry per commit whose message has a non-empty Devlog trailer, newest first."""
    has_commits = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", "HEAD"], capture_output=True,
    ).returncode == 0
    if not has_commits:
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
            "date": date,  # author date, ISO 8601 with its UTC offset
            "title": title,
            "tags": [t.strip() for t in tags.replace("\x1d", ",").split(",") if t.strip()],
        })
    return entries


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

    new_lines = [f"{e['date']} | {e['title']} | {', '.join(e['tags'])} | {e['sha']}" for e in new]
    text_path.write_text("\n".join(new_lines + lines) + "\n", encoding="utf-8")
    for entry in new:
        print(f"Added: {entry['title']} ({entry['sha'][:7]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
