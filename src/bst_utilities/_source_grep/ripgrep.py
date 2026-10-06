"""Build rg commands and decode file-name or JSON match records."""

from __future__ import annotations

import base64
import json
from contextlib import closing

from . import matching, processes


def rg_json_text(value):
    if "text" in value:
        return value["text"]
    if "bytes" in value:
        return base64.b64decode(value["bytes"]).decode("utf-8", "surrogateescape")
    raise ValueError("rg JSON is missing text/bytes")


def rg_command(args):
    """Return (rg argv, NUL-separated file output, path matcher or None)."""
    files = args.find is not None or args.files_with_matches
    matcher = None
    if args.find is not None:
        cmd = ["rg", "--files", "--null", "--hidden", "--no-ignore", "--glob", "!**/.git/**", "."]
        matcher = matching.make_path_filter(args)
    else:
        cmd = ["rg", "--hidden", "--no-ignore"]
        cmd.extend(["--files-with-matches", "--null"] if files else ["--json"])
        for pattern in args.glob:
            cmd.extend(["--glob", pattern])
        for pattern in args.exclude:
            cmd.extend(["--glob", pattern if pattern.startswith("!") else f"!{pattern}"])
        # rg gives later globs precedence; keep the built-in exclusion last.
        cmd.extend(["--glob", "!**/.git/**"])
        if args.ignore_case:
            cmd.append("--ignore-case")
        if args.fixed_string:
            cmd.append("--fixed-strings")
        if args.binary_files == "text":
            cmd.append("--text")
        cmd.extend(["--", args.pattern, "."])
    return cmd, files, matcher


def parse_rg_records(records, args, files, matcher):
    with closing(records) as output:
        for line in output:
            if files:
                path = line.removeprefix("./")
                if path and (args.find is None or matcher(path)):
                    yield "file", path, None, None
            else:
                try:
                    event = json.loads(line)
                    if event.get("type") != "match":
                        continue
                    data = event["data"]
                    path = rg_json_text(data["path"]).removeprefix("./")
                    text = rg_json_text(data["lines"]).removesuffix("\n").removesuffix("\r")
                    yield "match", path, data.get("line_number"), text
                except (ValueError, KeyError, TypeError) as exc:
                    raise processes.SearchError(f"invalid rg JSON: {exc}") from exc


def iter_mounted_matches(args, mountpoint):
    """Run rg from the tree root so slash globs apply to source-relative paths."""
    cmd, files, matcher = rg_command(args)
    yield from parse_rg_records(
        processes.iter_rg_output(cmd, null=files, cwd=mountpoint), args, files, matcher
    )


def iter_spooled_matches(args, spool):
    _, files, matcher = rg_command(args)
    spool.seek(0)
    yield from parse_rg_records(processes.iter_records(spool, files), args, files, matcher)
