#!/usr/bin/env python3
"""Select full CI unless every changed path is explicitly documentation-only."""

import json
import os
import subprocess
from pathlib import Path, PurePosixPath


def docs_only(paths):
    def is_doc(path):
        p = PurePosixPath(path)
        return (
            (len(p.parts) == 1 and p.suffix == ".md")
            or path == "LICENSE"
            or (path.startswith("docs/") and p.suffix in {".md", ".json"})
            or path == ".github/pull_request_template.md"
            or path.startswith(".github/ISSUE_TEMPLATE/")
        )

    return bool(paths) and all(is_doc(path) for path in paths)


def changed_paths(event_name, event):
    if event_name == "pull_request":
        base = event["pull_request"]["base"]["sha"]
        head = event["pull_request"]["head"]["sha"]
        # Compare the PR with its merge base, excluding unrelated changes on main.
        revision = f"{base}...{head}"
    elif event_name == "push" and event.get("before", "").strip("0"):
        revision = event["before"]
        head = event["after"]
    else:
        return []  # Manual runs and unknown baselines use full validation.
    args = ["git", "diff", "--name-only", "--no-renames", "-z", revision]
    if event_name == "push":
        args.append(head)
    output = subprocess.check_output(args)
    return [p.decode("utf-8", errors="surrogateescape") for p in output.split(b"\0") if p]


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    try:
        paths = changed_paths(os.environ["GITHUB_EVENT_NAME"], event)
        full = not docs_only(paths)
    except (KeyError, subprocess.CalledProcessError):
        full = True
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"full={'true' if full else 'false'}\n")
    print("Full validation" if full else "Documentation validation only")


if __name__ == "__main__":
    main()
