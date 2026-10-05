#!/usr/bin/env python3
"""Benchmark bst-source-grep against generated fixtures or an existing project.

By default everything (project, BuildStream cache, path indexes, mounts) lives in
a temporary work directory that is removed afterwards unless --keep-workdir is
given. With --project the benchmark searches an existing, already fetched project
(for example freedesktop-sdk) through --config; it never fetches or tracks it.
Each scenario is repeated and reported as a distribution, never a single best
time. "Fresh index" means a newly built tool path index, not a cold OS page cache.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCALES = {
    # name: small files, large files, large file MiB, unique trees, duplicate elements
    "tiny": dict(small=200, large=2, large_mib=1, unique=5, dup=3),
    "default": dict(small=20000, large=4, large_mib=16, unique=300, dup=100),
}

STAT_LINE = re.compile(r"^\s{2}([a-z][a-z -]+):\s+([0-9.]+)(?:s| MiB)?$")

# name, target, extra arguments, needs FUSE, path index mode
SCENARIOS = [
    ("find-fresh-index", "small-files.bst", ["--find", "*.c"], False, "fresh"),
    ("find-warm-index", "small-files.bst", ["--find", "*.c"], False, "warm"),
    ("find-narrow-glob", "small-files.bst", ["--find", "*.c", "--glob", "d007/**"], False, "warm"),
    ("find-no-index", "small-files.bst", ["--find", "*.c"], False, "none"),
    ("content-small-files", "small-files.bst", ["needle"], True, "none"),
    ("content-large-files", "large-files.bst", ["needle"], True, "none"),
    ("content-unique-trees", "unique-trees.bst", ["needle"], True, "none"),
    ("content-unique-trees-serial", "unique-trees.bst", ["needle", "--jobs", "1"], True, "none"),
    ("content-duplicate-trees", "dup-trees.bst", ["needle"], True, "none"),
    ("output-fanout", "small-files.bst", ["-n", "line"], True, "none"),
]

# Scenarios for --project; None stands for --target. The content pattern of
# content-no-match is absent from real sources, so it measures a full scan.
EXTERNAL_SCENARIOS = [
    ("find-fresh-index", None, ["--find", "*.c"], False, "fresh"),
    ("find-warm-index", None, ["--find", "*.c"], False, "warm"),
    ("find-no-index", None, ["--find", "*.c"], False, "none"),
    ("find-rare-name", None, ["--find", "meson.build"], False, "warm"),
    ("content-no-match", None, ["-F", "bst-source-grep-bench-absent-string"], True, "none"),
    (
        "content-no-match-serial",
        None,
        ["-F", "bst-source-grep-bench-absent-string", "--jobs", "1"],
        True,
        "none",
    ),
    ("content-files-with-matches", None, ["-l", "-F", "Copyright"], True, "none"),
    ("output-fanout", None, ["-n", "-F", "#include", "--glob", "*.c"], True, "none"),
]

# Exit status 2 with only these reasons is a partial search over a partly
# fetched project, which --allow-partial accepts.
PARTIAL_ONLY = ("uncached elements", "unresolved elements")
HARD_ERRORS = ("mount errors", "traversal errors", "search errors")

METRICS = [
    ("wall", "wall s"),
    ("startup", "startup s"),
    ("load time", "load s"),
    ("project load time", "project load s"),
    ("cache check time", "cache check s"),
    ("mount time", "mount s"),
    ("search time", "search s"),
    ("cleanup time", "cleanup s"),
    ("other", "other s"),
    ("peak rss", "tool RSS MiB"),
    ("max_rss_mib", "tree max RSS MiB"),
]


def write_tree(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


def make_fixture(project: Path, scale: dict) -> dict:
    """Create the project; return its dimensions for the report."""
    elements = project / "elements"
    elements.mkdir(parents=True)
    (project / "project.conf").write_text(
        "name: bench\nmin-version: 2.8\nelement-path: elements\n", encoding="utf-8"
    )

    def import_element(name, path):
        (elements / name).write_text(
            f"kind: import\nsources:\n- kind: local\n  path: {path}\n", encoding="utf-8"
        )

    def stack(name, deps):
        body = "".join(f"- {dep}\n" for dep in deps)
        (elements / name).write_text(f"kind: stack\ndepends:\n{body}", encoding="utf-8")

    small = {
        f"d{i % 100:03d}/f{i}.{'c' if i % 2 else 'h'}": (
            f"line {i} one\nline {i} two\n" + ("needle\n" if i % 50 == 0 else "")
        )
        for i in range(scale["small"])
    }
    write_tree(project / "src" / "small", small)
    import_element("small-files.bst", "src/small")

    block = "".join(f"large line {n} filler text for searching\n" for n in range(1000))
    repeats = max(1, scale["large_mib"] * 1024 * 1024 // len(block))
    large = {f"big{i}.txt": block * repeats + "needle\n" for i in range(scale["large"])}
    write_tree(project / "src" / "large", large)
    import_element("large-files.bst", "src/large")

    unique = []
    for i in range(scale["unique"]):
        write_tree(
            project / "src" / "unique" / f"t{i}",
            {f"f{j}.c": f"tree {i} file {j} needle\n" for j in range(3)},
        )
        import_element(f"unique-{i}.bst", f"src/unique/t{i}")
        unique.append(f"unique-{i}.bst")
    stack("unique-trees.bst", unique)

    write_tree(project / "src" / "dup", {f"f{j}.c": f"shared {j} needle\n" for j in range(3)})
    dups = []
    for i in range(scale["dup"]):
        import_element(f"dup-{i}.bst", "src/dup")
        dups.append(f"dup-{i}.bst")
    stack("dup-trees.bst", dups)

    stack("all.bst", ["small-files.bst", "large-files.bst", "unique-trees.bst", "dup-trees.bst"])
    return {
        "small_files": scale["small"],
        "large_files": scale["large"],
        "large_file_mib": scale["large_mib"],
        "unique_trees": scale["unique"],
        "unique_tree_files": 3,
        "duplicate_elements": scale["dup"],
    }


def tool_version(cmd) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except (OSError, IndexError, subprocess.SubprocessError):
        return "unavailable"


def environment(buildbox_fuse) -> dict:
    try:
        import importlib.metadata

        bst = importlib.metadata.version("BuildStream")
    except Exception:
        bst = "unavailable"
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "buildstream": bst,
        "ripgrep": tool_version(["rg", "--version"]),
        "buildbox_fuse": tool_version([buildbox_fuse, "--version"]) if buildbox_fuse else "n/a",
    }


def fuse_available() -> bool:
    return os.path.exists("/dev/fuse") and shutil.which("rg") is not None


def run_once(base_cmd, args, cwd) -> dict:
    start = time.monotonic()
    proc = subprocess.Popen(
        base_cmd + args + ["--stats"],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    stderr = proc.stderr.read().decode("utf-8", "replace")
    _, status, usage = os.wait4(proc.pid, 0)
    wall = time.monotonic() - start
    proc.returncode = os.waitstatus_to_exitcode(status)
    # ru_maxrss here is the largest single process in the tree (often casd);
    # the tool's own peak comes from its "peak rss" statistic.
    record = {"returncode": proc.returncode, "wall": wall, "max_rss_mib": usage.ru_maxrss / 1024}
    for line in stderr.splitlines():
        match = STAT_LINE.match(line)
        if match:
            record[match.group(1).strip()] = float(match.group(2))
    if "total time" in record:
        # startup: interpreter/imports before timing; other: BuildStream context,
        # casd start/stop and anything else inside the tool's total.
        record["startup"] = wall - record["total time"]
        phases = ("load time", "mount time", "search time", "cleanup time")
        # With several search jobs, phase times overlap and are summed, so the
        # remainder is not meaningful.
        if record.get("search jobs", 1) <= 1 or record.get("mounted trees", 0) == 0:
            record["other"] = record["total time"] - sum(record.get(k, 0.0) for k in phases)
    if proc.returncode not in (0, 1):
        record["stderr"] = stderr[-2000:]
    return record


def acceptable(run, allow_partial) -> bool:
    if run["returncode"] in (0, 1):
        return True
    return (
        allow_partial
        and run["returncode"] == 2
        and any(run.get(key, 0) > 0 for key in PARTIAL_ONLY)
        and not any(run.get(key, 0) > 0 for key in HARD_ERRORS)
    )


def summarize(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", choices=sorted(SCALES), default="default")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--scenario", action="append", help="run only these scenarios")
    parser.add_argument("--json-out", type=Path, help="write raw results as JSON")
    parser.add_argument("--keep-workdir", action="store_true")
    parser.add_argument("--project", type=Path, help="benchmark this fetched project instead")
    parser.add_argument("--target", help="element to search with --project")
    parser.add_argument("--config", type=Path, help="BuildStream configuration for --project")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="accept partial searches caused only by uncached or unresolved elements",
    )
    args = parser.parse_args()

    if shutil.which("bst") is None:
        parser.exit(2, "error: the BuildStream CLI (bst) is required\n")
    if (args.project is None) != (args.target is None):
        parser.error("--project and --target must be given together")
    if args.config and not args.project:
        parser.error("--config requires --project")
    catalogue = EXTERNAL_SCENARIOS if args.project else SCENARIOS
    selected = [s for s in catalogue if not args.scenario or s[0] in args.scenario]

    workdir = Path(tempfile.mkdtemp(prefix="bst-source-grep-bench-"))
    try:
        if args.project:
            project = args.project.resolve()
            dims = {"project": str(project), "target": args.target}
            config = args.config.resolve() if args.config else None
        else:
            project = workdir / "project"
            dims = make_fixture(project, SCALES[args.scale])
            config = workdir / "buildstream.conf"
            config.write_text(f"cachedir: {workdir / 'bst-cache'}\n", encoding="utf-8")
            fetch = subprocess.run(
                ["bst", "--config", str(config), "--no-interactive", "source", "fetch"]
                + ["--deps", "all", "all.bst"],
                cwd=project,
                capture_output=True,
                text=True,
            )
            if fetch.returncode != 0:
                parser.exit(2, f"error: fixture fetch failed:\n{fetch.stderr[-2000:]}")

        src = Path(__file__).resolve().parents[1] / "src"
        sys.path.insert(0, str(src))
        from bst_utilities._source_grep import discovery

        buildbox_fuse = discovery.find_buildbox_fuse(argparse.Namespace(buildbox_fuse=None))
        base_cmd = [sys.executable, "-m", "bst_utilities.source_grep"]
        if config:
            base_cmd += ["--config", str(config)]
        base_cmd += ["--mount-dir", str(workdir / "mounts")]
        os.environ["PYTHONPATH"] = os.pathsep.join(
            [str(src)] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
        )
        has_fuse = fuse_available()

        results = []
        for name, target, extra, needs_fuse, index in selected:
            target = target or args.target
            entry = {"scenario": name, "target": target, "args": extra, "runs": []}
            results.append(entry)
            if needs_fuse and not has_fuse:
                entry["skipped"] = "FUSE or rg unavailable"
                continue
            index_dir = workdir / "index" / name
            if index == "warm":
                run_once(base_cmd, [target, *extra, "--path-cache-dir", str(index_dir)], project)
            for _ in range(args.repeats):
                cmd = [target, *extra]
                if index == "none":
                    cmd.append("--no-path-cache")
                else:
                    if index == "fresh":
                        shutil.rmtree(index_dir, ignore_errors=True)
                    cmd += ["--path-cache-dir", str(index_dir)]
                run = run_once(base_cmd, cmd, project)
                entry["runs"].append(run)
                print(f"{name}: rc {run['returncode']}, {run['wall']:.2f}s", file=sys.stderr)

        report = {
            "environment": environment(buildbox_fuse),
            "scale": "external" if args.project else args.scale,
            "repeats": args.repeats,
            "fixture": dims,
            "results": results,
        }
        if args.json_out:
            args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print_report(report)
        failed = [
            r["scenario"]
            for r in results
            for run in r["runs"]
            if not acceptable(run, args.allow_partial)
        ]
        if failed:
            print(f"\nerror: failing scenarios: {', '.join(sorted(set(failed)))}", file=sys.stderr)
            for r in results:
                for run in r["runs"]:
                    if "stderr" in run and r["scenario"] in failed:
                        print(f"--- {r['scenario']}:\n{run['stderr']}", file=sys.stderr)
                        break
            return 2
        return 0
    finally:
        if args.keep_workdir:
            print(f"\nwork directory kept: {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def print_report(report) -> None:
    env = report["environment"]
    print("## bst-source-grep benchmark\n")
    for key, value in env.items():
        print(f"- {key}: {value}")
    print(f"- scale: {report['scale']}, repeats: {report['repeats']}")
    print(f"- fixture: {json.dumps(report['fixture'])}\n")
    print("Values are median (min-max) over repeats.\n")
    counts = [
        ("elements", "elements"),
        ("uncached elements", "uncached"),
        ("unique trees", "trees"),
        ("results", "results"),
        ("peak mounts", "peak mounts"),
        ("search jobs", "jobs"),
        ("rg processes", "rg procs"),
    ]
    header = ["scenario", *(label for _, label in METRICS), *(label for _, label in counts)]
    print("| " + " | ".join(header) + " |")
    print("|" + " --- |" * len(header))
    for entry in report["results"]:
        if "skipped" in entry:
            print(f"| {entry['scenario']} | skipped: {entry['skipped']} |")
            continue
        cells = [entry["scenario"]]
        for key, _ in METRICS:
            summary = summarize([run.get(key) for run in entry["runs"]])
            cells.append(
                "-"
                if summary is None
                else f"{summary['median']:.3f} ({summary['min']:.3f}-{summary['max']:.3f})"
            )
        last = entry["runs"][-1] if entry["runs"] else {}
        for key, _ in counts:
            value = last.get(key)
            cells.append("-" if value is None else str(int(value)))
        print("| " + " | ".join(cells) + " |")


if __name__ == "__main__":
    sys.exit(main())
