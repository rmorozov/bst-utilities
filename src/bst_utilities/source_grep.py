#!/usr/bin/env python3
"""
Search BuildStream source caches.

Backends:
    --find with --backend auto/cas:
        CAS-direct traversal, optional path-index cache, no FUSE.

    content search, or --backend fuse:
        buildbox-fuse mounts + ripgrep.

Requires for FUSE/content mode:
    - buildbox-fuse
    - rg
    - fusermount3 or fusermount

Exit codes:
    0   matches found
    1   no matches found
    2   error
    130 interrupted
    141 output pipe closed by reader
"""

from __future__ import annotations

import argparse
import base64
import importlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import tempfile
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


BUILDBOX_FUSE_NAMES = (
    "buildbox-fuse",
    "buildbox-fuse.exe",
)

# Readiness polling backs off from 1 ms to 50 ms per buildbox-fuse mount.
MOUNT_POLL_INITIAL = 0.001
MOUNT_POLL_MAX = 0.05


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Search BuildStream source caches using CAS-direct traversal for "
            "--find and buildbox-fuse + ripgrep for content searches."
        )
    )

    parser.add_argument("--config", help="BuildStream user configuration file")
    parser.add_argument(
        "-C", "--directory", default=os.getcwd(), help="BuildStream project directory"
    )
    parser.add_argument(
        "-o",
        "--option",
        nargs=2,
        action="append",
        default=[],
        metavar=("KEY", "VALUE"),
        help="project option; repeatable, last value wins",
    )

    parser.add_argument(
        "target",
        help="BuildStream element, e.g. default_elements.bst",
    )
    parser.add_argument(
        "pattern",
        nargs="?",
        help="regular expression to search for",
    )

    parser.add_argument(
        "--find",
        metavar="GLOB",
        help="find files matching GLOB instead of searching contents",
    )

    parser.add_argument(
        "-i",
        "--ignore-case",
        action="store_true",
        help="case-insensitive matching",
    )
    parser.add_argument(
        "-F",
        "--fixed-string",
        action="store_true",
        help="treat PATTERN as a literal string",
    )
    parser.add_argument(
        "-n",
        "--line-number",
        action="store_true",
        help="show line numbers in text output",
    )
    parser.add_argument(
        "-l",
        "--files-with-matches",
        action="store_true",
        help="print only files containing a match",
    )

    parser.add_argument(
        "--glob",
        action="append",
        default=[],
        metavar="GLOB",
        help="only search files matching GLOB; repeatable",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="exclude files matching GLOB; repeatable",
    )

    parser.add_argument(
        "--binary-files",
        choices=("skip", "text"),
        default="skip",
        help="how to handle binary files (default: skip)",
    )
    parser.add_argument(
        "--deps",
        choices=("none", "build", "run", "all"),
        default="all",
        help="dependency selection (default: all)",
    )

    parser.add_argument(
        "--fetch-subprojects",
        dest="fetch_subprojects",
        action="store_true",
        default=False,
        help="allow BuildStream to fetch/load subprojects if required (may access network)",
    )
    parser.add_argument(
        "--no-fetch-subprojects",
        dest="fetch_subprojects",
        action="store_false",
        help="do not allow fetching subprojects (default)",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="output results as JSON Lines",
    )

    parser.add_argument(
        "--strip-junctions",
        dest="strip_junctions",
        action="store_true",
        help="strip junction/project prefixes from element names",
    )
    parser.add_argument(
        "--unique-recipes",
        dest="strip_junctions",
        action="store_true",
        help="alias for --strip-junctions",
    )

    parser.add_argument(
        "--origin",
        action="store_true",
        help="include source origin information",
    )

    parser.add_argument(
        "--gitreview",
        dest="gitreview_mode",
        choices=("auto", "always", "never"),
        default="auto",
        help=(
            "control .gitreview parsing: "
            "auto = only when source metadata lacks usable Gerrit project, "
            "always = always parse, never = disable"
        ),
    )
    parser.add_argument(
        "--no-gitreview",
        dest="gitreview_mode",
        action="store_const",
        const="never",
        help="disable .gitreview parsing",
    )
    parser.add_argument(
        "--gitreview-nearest",
        action="store_true",
        help="use nearest ancestor .gitreview instead of repository root only",
    )

    parser.add_argument(
        "--backend",
        choices=("auto", "fuse", "cas"),
        default="auto",
        help=(
            "search backend: "
            "auto = CAS-direct for --find, FUSE+rg for content; "
            "fuse = always use buildbox-fuse + ripgrep; "
            "cas = CAS-direct for --find only"
        ),
    )

    parser.add_argument(
        "--cas-dir",
        default=None,
        metavar="DIR",
        help="local BuildStream/BuildBox CAS cache directory",
    )
    parser.add_argument(
        "--mount-dir",
        default=os.path.expanduser("~/.cache/bst-source-grep/mounts"),
        metavar="DIR",
        help="directory where buildbox-fuse mounts are created",
    )
    parser.add_argument(
        "--digest-function",
        default="SHA256",
        choices=("SHA256", "SHA384", "SHA512", "SHA1", "MD5"),
        help="digest function used by buildbox-fuse (default: SHA256)",
    )
    parser.add_argument(
        "--keep-mounts",
        action="store_true",
        help="do not unmount buildbox-fuse mounts created by this run on exit",
    )
    parser.add_argument(
        "--force-unmount",
        action="store_true",
        help=("unmount this run's mount points on exit; overrides --keep-mounts"),
    )
    parser.add_argument(
        "--buildbox-fuse",
        default=None,
        metavar="PATH",
        help="path to buildbox-fuse executable",
    )

    parser.add_argument(
        "--path-cache-dir",
        default=os.path.expanduser("~/.cache/bst-source-grep/path-index"),
        metavar="DIR",
        help="directory for CAS-direct --find path-index cache",
    )
    parser.add_argument(
        "--no-path-cache",
        dest="path_cache_enabled",
        action="store_false",
        default=True,
        help="disable path-index cache for CAS-direct --find",
    )
    parser.add_argument(
        "--rebuild-path-cache",
        action="store_true",
        help="ignore existing path-index cache and rebuild it",
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help="print statistics to stderr",
    )
    parser.add_argument(
        "--traceback",
        action="store_true",
        help="show Python traceback on errors",
    )

    return parser


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def strip_junction_name(name: str) -> str:
    if not name:
        return name

    p = str(name).replace(os.sep, "/")

    if ":" in p:
        stripped = p.rsplit(":", 1)[-1]
        if stripped:
            p = stripped

    while p.startswith("./"):
        p = p[2:]

    p = p.lstrip("/")

    return p


def normalize_relative_dir(value) -> str:
    if value is None:
        return ""

    p = str(value).replace(os.sep, "/")

    while p.startswith("./"):
        p = p[2:]

    p = p.strip("/")

    if p == ".":
        return ""

    return p


def normalize_path(path) -> str:
    if isinstance(path, bytes):
        p = path.decode("utf-8", "replace")
    else:
        p = str(path)

    p = p.replace(os.sep, "/")

    while p.startswith("./"):
        p = p[2:]

    p = p.lstrip("/")

    while p.endswith("/"):
        p = p[:-1]

    if p == ".":
        return ""

    return p


def _normalize_gerrit_project(value):
    if value is None:
        return None

    p = str(value).strip().strip("/")

    if p.endswith(".git"):
        p = p[:-4]

    p = p.strip("/")

    return p if p else None


def format_seconds(value: float) -> str:
    return f"{value:.3f}s"


# ---------------------------------------------------------------------------
# Source object introspection helpers
# ---------------------------------------------------------------------------


def _bst_get(node, key):
    if node is None:
        return None

    if isinstance(node, dict):
        return node.get(key)

    getter = getattr(node, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except Exception:
            return None

    return None


def _bst_value(obj, names):
    """
    Fetch a scalar-ish value from an object or mapping.

    IMPORTANT:
    This intentionally does NOT call callables. BuildStream source objects
    can have methods such as track() or fetch(), and calling those may access
    the network.
    """
    for name in names:
        value = None

        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            try:
                value = getattr(obj, name, None)
            except Exception:
                value = None

            if value is None:
                value = _bst_get(obj, name)

        if callable(value):
            continue

        if value is not None and str(value) != "":
            return value

    return None


def source_accessor(element):
    for attr in ("_Element__sources", "_sources", "sources"):
        try:
            obj = getattr(element, attr, None)
        except Exception:
            continue

        if obj is not None and hasattr(obj, "get_files"):
            return obj

    if hasattr(element, "get_files"):
        return element

    return None


def source_count(element, accessor):
    try:
        return len(accessor)
    except Exception:
        pass

    for obj in (accessor, element):
        if obj is None:
            continue

        for attr in ("_sources", "sources", "_Element__sources"):
            try:
                value = getattr(obj, attr, None)
            except Exception:
                continue

            if isinstance(value, (list, tuple, set)):
                return len(value)

    return None


def _call_get_files(accessor):
    getter = getattr(accessor, "get_files", None)
    if getter is None:
        return None

    return getter() if callable(getter) else getter


def load_source_directory(element):
    accessor = source_accessor(element)
    if accessor is None:
        return None, "nosources", None

    count = source_count(element, accessor)
    if count == 0:
        return None, "nosources", None

    # need_state=False avoids artifact state, so initialize source keys explicitly.
    update = getattr(accessor, "update_resolved_state", None)
    if callable(update):
        try:
            update()
            if not accessor.is_resolved():
                return (
                    None,
                    "unresolved",
                    "sources have no ref; configure refs or run bst source track",
                )
        except Exception as exc:
            return None, "uncached", str(exc)

    try:
        query = getattr(element, "_query_source_cache", None)
    except Exception as exc:
        return None, "uncached", str(exc)

    if callable(query):
        try:
            query()
        except Exception as exc:
            return None, "uncached", str(exc)

    try:
        cached_value = getattr(element, "_cached_sources", None)
    except Exception as exc:
        return None, "uncached", str(exc)

    if cached_value is not None:
        try:
            cached = cached_value() if callable(cached_value) else cached_value
        except Exception as exc:
            return None, "uncached", str(exc)

        if not cached:
            if count is None:
                try:
                    files = _call_get_files(accessor)
                    if files is not None:
                        return files, "ok", None
                except Exception:
                    pass

            return None, "uncached", None

    try:
        files = _call_get_files(accessor)
    except Exception as exc:
        return None, "uncached", str(exc)

    if files is None:
        return None, "uncached", "get_files() returned None"

    return files, "ok", None


def get_source_objects(element, accessor):
    candidate_attrs = (
        "_sources",
        "sources",
        "_source_list",
        "_source_objects",
        "_ElementSources__sources",
    )

    for container in (accessor, element):
        if container is None:
            continue

        for attr in candidate_attrs:
            try:
                value = getattr(container, attr, None)
            except Exception:
                continue

            if isinstance(value, (list, tuple)):
                return list(value)

            if (
                value is not None
                and hasattr(value, "__iter__")
                and not isinstance(value, (str, bytes, dict))
            ):
                try:
                    return list(value)
                except Exception:
                    pass

    if accessor is not None:
        try:
            return list(accessor)
        except Exception:
            pass

    return []


def describe_source_object(src, index):
    kind = _bst_value(
        src,
        (
            "kind",
            "source_kind",
            "_kind",
            "plugin_kind",
            "_plugin_kind",
        ),
    )

    url = _bst_value(
        src,
        (
            "url",
            "_url",
            "repository",
            "location",
            "path",
            "filename",
            "tarball",
            "uri",
            "clone_url",
            "fetch_url",
            "repo_url",
            "resolved_url",
        ),
    )

    ref = _bst_value(
        src,
        (
            "ref",
            "_ref",
            "commit",
            "tag",
            "branch",
            "revision",
            "sha",
            "digest",
            "track",
            "track_ref",
        ),
    )

    track = _bst_value(
        src,
        (
            "track",
            "_track",
            "track_ref",
        ),
    )

    directory = _bst_value(
        src,
        (
            "directory",
            "_directory",
            "subdir",
            "target_directory",
        ),
    )

    gerrit_project = _bst_value(
        src,
        (
            "gerrit_project",
            "_gerrit_project",
            "project",
            "project_name",
        ),
    )

    raw = None
    for attr in (
        "source",
        "_source",
        "node",
        "_node",
        "spec",
        "_spec",
        "yaml",
        "_yaml",
    ):
        try:
            candidate = getattr(src, attr, None)
        except Exception:
            candidate = None

        if candidate is not None:
            raw = candidate
            break

    if raw is not None:
        kind = _bst_get(raw, "kind") or kind
        url = _bst_get(raw, "url") or _bst_get(raw, "path") or url
        ref = _bst_get(raw, "ref") or ref
        track = _bst_get(raw, "track") or track
        directory = _bst_get(raw, "directory") or directory
        gerrit_project = _bst_get(raw, "gerrit_project") or gerrit_project

    if ref is None and track is not None:
        ref = track

    if kind is None:
        kind = type(src).__name__

    directory = normalize_relative_dir(directory)

    label = str(kind) if kind else "source"

    if url:
        label += f" {url}"

    if ref:
        label += f" #{ref}"

    if directory:
        label += f" directory={directory}"

    if gerrit_project:
        label += f" gerrit_project={gerrit_project}"

    gerrit_project_effective = _normalize_gerrit_project(gerrit_project)

    return {
        "id": f"src{index}",
        "kind": str(kind) if kind is not None else None,
        "url": str(url) if url is not None else None,
        "ref": str(ref) if ref is not None else None,
        "track": str(track) if track is not None else None,
        "directory": directory,
        "gerrit_project": str(gerrit_project) if gerrit_project is not None else None,
        "gerrit_project_effective": gerrit_project_effective,
        "gerrit_project_source": "source-plugin" if gerrit_project is not None else None,
        "label": label,
    }


def element_source_infos(element):
    accessor = source_accessor(element)
    infos = []

    for i, src in enumerate(get_source_objects(element, accessor), 1):
        try:
            infos.append(describe_source_object(src, i))
        except Exception:
            infos.append(
                {
                    "id": f"src{i}",
                    "kind": None,
                    "url": None,
                    "ref": None,
                    "track": None,
                    "directory": "",
                    "gerrit_project": None,
                    "gerrit_project_effective": None,
                    "gerrit_project_source": None,
                    "label": "unparsable-source",
                }
            )

    return infos


def guess_source_info(source_infos, path: str):
    if not source_infos:
        return None

    best = None
    best_len = -1

    for info in source_infos:
        d = info.get("directory", "")

        if not d or d == ".":
            continue

        if path == d or path.startswith(d + "/"):
            if len(d) > best_len:
                best = info
                best_len = len(d)

    if best is not None:
        return best

    if len(source_infos) == 1:
        return source_infos[0]

    return None


# ---------------------------------------------------------------------------
# Glob helpers for CAS-direct --find
# ---------------------------------------------------------------------------


_GLOB_REGEX_CACHE = {}


def _normalize_glob_pattern(pattern) -> str:
    p = str(pattern).replace(os.sep, "/")

    while p.startswith("./"):
        p = p[2:]

    p = p.lstrip("/")

    while p.endswith("/"):
        p = p[:-1]

    return p


def _compile_glob(pattern: str):
    normalized = _normalize_glob_pattern(pattern)

    cached = _GLOB_REGEX_CACHE.get(normalized)
    if cached is not None:
        return cached, normalized

    parts = []
    i = 0
    n = len(normalized)

    while i < n:
        c = normalized[i]

        if c == "*":
            if i + 1 < n and normalized[i + 1] == "*":
                if i + 2 < n and normalized[i + 2] == "/":
                    parts.append("(?:.*/)?")
                    i += 3
                else:
                    parts.append(".*")
                    i += 2
                continue

            parts.append("[^/]*")
            i += 1

        elif c == "?":
            parts.append("[^/]")
            i += 1

        elif c == "[":
            j = i + 1

            if j < n and normalized[j] == "!":
                j += 1

            if j < n and normalized[j] == "]":
                j += 1

            while j < n and normalized[j] != "]":
                j += 1

            if j >= n:
                parts.append(re.escape("["))
                i += 1
            else:
                inner = normalized[i + 1 : j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]

                parts.append(f"[{inner}]")
                i = j + 1

        else:
            parts.append(re.escape(c))
            i += 1

    try:
        regex = re.compile("^" + "".join(parts) + r"\Z", re.DOTALL)
    except re.error:
        regex = re.compile("^" + re.escape(normalized) + r"\Z", re.DOTALL)

    _GLOB_REGEX_CACHE[normalized] = regex
    return regex, normalized


def make_find_matcher(pattern: str):
    """
    Build a fast matcher for --find.

    Special-cases common patterns like:
        *.psl
        foo.psl
        foo*
    """
    normalized = _normalize_glob_pattern(pattern)

    if not normalized:
        return lambda _path: False

    has_slash = "/" in normalized

    # Fast exact basename match: foo.psl
    if not has_slash and not any(ch in normalized for ch in "*?["):
        return lambda path: path.rsplit("/", 1)[-1] == normalized

    # Fast suffix match: *.psl
    if (
        not has_slash
        and normalized.startswith("*")
        and normalized.count("*") == 1
        and "?" not in normalized
        and "[" not in normalized
    ):
        suffix = normalized[1:]
        return lambda path: path.endswith(suffix)

    # Fast prefix match: foo*
    if (
        not has_slash
        and normalized.endswith("*")
        and normalized.count("*") == 1
        and "?" not in normalized
        and "[" not in normalized
    ):
        prefix = normalized[:-1]
        return lambda path: path.rsplit("/", 1)[-1].startswith(prefix)

    regex, normalized = _compile_glob(pattern)

    if "/" in normalized:
        return lambda path: bool(regex.match(path))

    return lambda path: bool(regex.match(path.rsplit("/", 1)[-1]))


def iter_relative_paths(directory):
    """
    Iterate relative paths from a BuildStream CAS directory object.
    """
    try:
        lister = getattr(directory, "list_relative_paths", None)
    except Exception:
        lister = None

    if lister is not None:
        it = lister() if callable(lister) else lister
        for path in it:
            p = normalize_path(path)
            if p:
                from buildstream.storage.directory import FileType

                if directory.stat(p).file_type == FileType.REGULAR_FILE:
                    yield p
        return

    try:
        walker = getattr(directory, "walk", None)
    except Exception:
        walker = None

    if callable(walker):
        for item in walker():
            if isinstance(item, str):
                p = normalize_path(item)
                if p:
                    yield p
                continue

            if isinstance(item, (list, tuple)):
                if len(item) == 3:
                    root, dirs, files = item

                    for name in dirs:
                        p = normalize_path(os.path.join(root, name))
                        if p:
                            yield p

                    for name in files:
                        p = normalize_path(os.path.join(root, name))
                        if p:
                            yield p

                    continue

                if item:
                    p = normalize_path(item[0])
                    if p:
                        yield p

        return

    raise RuntimeError("BuildStream CAS directory does not expose list_relative_paths() or walk()")


# ---------------------------------------------------------------------------
# Path-index cache helpers
# ---------------------------------------------------------------------------


def iter_path_cache_file(cache_file: Path):
    with open(cache_file, "r", encoding="utf-8") as f:
        for line in f:
            path = json.loads(line)
            if not isinstance(path, str):
                raise ValueError("invalid path-index record")
            yield path


def iter_directory_and_cache(directory, cache_file: Path):
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    # Each writer owns its temporary file; readers see only completed indexes.
    tmp_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cache_file.parent,
            prefix=cache_file.name + ".",
            suffix=".tmp",
            delete=False,
        ) as out:
            tmp_file = Path(out.name)
            for path in iter_relative_paths(directory):
                out.write(json.dumps(path) + "\n")
                yield path
        os.replace(tmp_file, cache_file)
    finally:
        if tmp_file is not None:
            tmp_file.unlink(missing_ok=True)


def make_path_filter(args):
    """Shared filename filtering for CAS and FUSE; excludes win."""
    find = make_find_matcher(args.find.lower() if args.ignore_case else args.find)
    includes = [make_find_matcher(g) for g in args.glob]
    excludes = [make_find_matcher(g.lstrip("!")) for g in args.exclude]

    def accepted(path):
        parts = path.split("/")
        ancestors = ["/".join(parts[:i]) for i in range(1, len(parts))]
        return (
            ".git" not in parts
            and find(path.lower() if args.ignore_case else path)
            and (not includes or any(m(path) for m in includes))
            and not any(m(p) for m in excludes for p in [path, *ancestors])
        )

    return accepted


class SearchError(RuntimeError):
    """A failed ripgrep invocation or invalid output."""


@contextmanager
def rg_process(cmd, *, cwd=None):
    """Stream stdout, spool stderr and always reap the child before unmounting."""
    with tempfile.TemporaryFile(mode="w+b") as errors:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=errors)
        try:
            yield proc
            proc.wait()
            if proc.returncode not in (0, 1):
                errors.seek(0)
                raise SearchError("rg failed: " + errors.read(65536).decode("utf-8", "replace"))
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def iter_rg_output(cmd, *, null=False, cwd=None):
    with rg_process(cmd, cwd=cwd) as proc:
        if not null:
            for line in proc.stdout:
                yield line.decode("utf-8", "replace")
        else:
            pending = b""
            while chunk := proc.stdout.read(65536):
                records = (pending + chunk).split(b"\0")
                pending = records.pop()
                for record in records:
                    yield os.fsdecode(record)
            if pending:
                raise SearchError("rg returned an unterminated filename")


def rg_json_text(value):
    if "text" in value:
        return value["text"]
    if "bytes" in value:
        return base64.b64decode(value["bytes"]).decode("utf-8", "surrogateescape")
    raise ValueError("rg JSON is missing text/bytes")


def iter_mounted_matches(args, mountpoint):
    """Run rg from the tree root so slash globs apply to source-relative paths."""
    files = args.find is not None or args.files_with_matches
    if args.find is not None:
        cmd = ["rg", "--files", "--null", "--hidden", "--no-ignore", "--glob", "!**/.git/**", "."]
        matcher = make_path_filter(args)
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
    with closing(iter_rg_output(cmd, null=files, cwd=mountpoint)) as output:
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
                    raise SearchError(f"invalid rg JSON: {exc}") from exc


def iter_checked_paths(iterator, tree, stats):
    """Count only iterator failures as traversal errors; emission happens outside."""
    try:
        while True:
            try:
                path = next(iterator)
            except StopIteration:
                return
            except Exception as exc:
                stats["traversal_errors"] += 1
                print(
                    f"ERROR: CAS traversal failed for tree {tree.get('digest') or 'unknown'}: {exc}",
                    file=sys.stderr,
                )
                return
            yield path
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


class RecordDeduplicator:
    """Avoid storing output-sized state unless junction stripping requires it."""

    def __init__(self, enabled):
        self.seen = set() if enabled else None

    def duplicate(self, kind, display, path, line=None):
        if self.seen is None:
            return False
        key = (kind, display, path, line)
        if key in self.seen:
            return True
        self.seen.add(key)
        return False


# ---------------------------------------------------------------------------
# .gitreview helpers
# ---------------------------------------------------------------------------


def parse_gitreview_text(text: str):
    host = None
    port = None
    project = None

    section = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or line.startswith(";"):
            continue

        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue

        if section == "gerrit" and "=" in line:
            key, value = line.split("=", 1)
            key = key.strip().lower()
            value = value.strip()

            if key == "project":
                project = value
            elif key == "host":
                host = value
            elif key == "port":
                port = value

    if host is None and port is None and project is None:
        return None

    return {
        "host": host,
        "port": port,
        "project": project,
        "project_normalized": _normalize_gerrit_project(project),
    }


def enrich_origin_with_gitreview(origin_obj, gitreview_info):
    if gitreview_info is None:
        return origin_obj

    if origin_obj is None:
        origin_obj = {"id": "gitreview"}
    else:
        origin_obj = dict(origin_obj)

    origin_obj["gitreview"] = gitreview_info

    gitreview_project = gitreview_info.get("project_normalized") or gitreview_info.get("project")

    if gitreview_project:
        origin_obj["gitreview_project"] = gitreview_project

        source_gerrit_project = origin_obj.get("gerrit_project")

        if source_gerrit_project:
            origin_obj["gerrit_project_match"] = _normalize_gerrit_project(
                source_gerrit_project
            ) == _normalize_gerrit_project(gitreview_project)

        origin_obj["gerrit_project_effective"] = gitreview_project
        origin_obj["gerrit_project_source"] = "gitreview"

    if origin_obj.get("id") in ("ambiguous", "no-source") and gitreview_project:
        origin_obj["id"] = "gitreview"
    elif "id" not in origin_obj:
        origin_obj["id"] = "gitreview"

    return origin_obj


class MountedGitreviewCache:
    def __init__(self, root: str, mode: str, nearest: bool):
        self.root = root
        self.mode = mode
        self.nearest = nearest
        self.dir_cache = {}
        self.nearest_cache = {}

    def _read_dir(self, rel_dir: str):
        if rel_dir in self.dir_cache:
            return self.dir_cache[rel_dir]

        if rel_dir:
            path = os.path.join(self.root, rel_dir, ".gitreview")
        else:
            path = os.path.join(self.root, ".gitreview")

        info = None

        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    info = parse_gitreview_text(f.read())
            except Exception:
                info = None

        self.dir_cache[rel_dir] = info
        return info

    def for_path(self, rel_path: str):
        if self.mode == "never":
            return None

        if not self.nearest:
            return self._read_dir("")

        d = os.path.dirname(rel_path)

        if d in self.nearest_cache:
            return self.nearest_cache[d]

        visited = []
        cur = d

        while True:
            visited.append(cur)

            if cur in self.nearest_cache:
                result = self.nearest_cache[cur]
                for v in visited:
                    self.nearest_cache[v] = result
                return result

            exact = self._read_dir(cur)

            if exact is not None:
                for v in visited:
                    self.nearest_cache[v] = exact
                return exact

            if cur == "":
                for v in visited:
                    self.nearest_cache[v] = None
                return None

            cur = os.path.dirname(cur)


class CasGitreviewCache(MountedGitreviewCache):
    def __init__(self, directory, mode, nearest):
        super().__init__("", mode, nearest)
        self.directory = directory

    def _read_dir(self, rel_dir):
        if rel_dir not in self.dir_cache:
            path = f"{rel_dir}/.gitreview" if rel_dir else ".gitreview"
            info = None
            try:
                if self.directory.isfile(path, follow_symlinks=False):
                    with self.directory.open_file(path, mode="rb") as f:
                        info = parse_gitreview_text(f.read().decode("utf-8", "replace"))
            except Exception:
                # Origin enrichment is optional; unavailable metadata is not a search error.
                info = None
            self.dir_cache[rel_dir] = info
        return self.dir_cache[rel_dir]


# ---------------------------------------------------------------------------
# CAS digest helpers
# ---------------------------------------------------------------------------


def get_cas_directory_digest(directory):
    """
    Best-effort extraction of the CAS directory digest object from a
    BuildStream CAS directory.
    """
    getter = getattr(directory, "_get_digest", None)
    if callable(getter):
        return getter()

    for attr_name in (
        "digest",
        "_digest",
        "directory_digest",
        "root_digest",
        "_root_digest",
        "_directory_digest",
    ):
        try:
            value = getattr(directory, attr_name, None)
        except Exception:
            continue

        if callable(value):
            try:
                value = value()
            except Exception:
                continue

        if value is not None:
            return value

    for method_name in (
        "_get_digest",
        "get_digest",
        "digest",
        "get_directory_digest",
        "get_root_digest",
    ):
        method = getattr(directory, method_name, None)
        if callable(method):
            try:
                value = method()
                if value is not None:
                    return value
            except Exception:
                pass

    file_digest = getattr(directory, "file_digest", None)
    if callable(file_digest):
        for candidate in ("", ".", "/"):
            try:
                value = file_digest(candidate)
                if value is not None:
                    return value
            except Exception:
                pass

    try:
        for value in vars(directory).values():
            if hasattr(value, "hash") and (hasattr(value, "size_bytes") or hasattr(value, "size")):
                return value
    except Exception:
        pass

    return None


def digest_to_fuse_value(digest):
    """
    Convert a BuildStream/CAS digest object into the <hash>/<size> format
    expected by buildbox-fuse --input-digest-value.
    """
    if digest is None:
        return None

    if isinstance(digest, str):
        if "/" in digest:
            hash_part, size_part = digest.split("/", 1)
            if hash_part and size_part.isdigit():
                return digest
        return None

    if isinstance(digest, (bytes, bytearray)):
        return None

    if isinstance(digest, tuple) and len(digest) == 2:
        hash_value, size_value = digest

        if isinstance(hash_value, (bytes, bytearray)):
            hash_value = bytes(hash_value).hex()

        try:
            size_value = int(size_value)
        except Exception:
            return None

        return f"{hash_value}/{size_value}"

    hash_value = getattr(digest, "hash", None)
    if hash_value is None:
        hash_value = getattr(digest, "hash_str", None)
    if hash_value is None:
        hash_value = getattr(digest, "digest", None)

    size_value = getattr(digest, "size_bytes", None)
    if size_value is None:
        size_value = getattr(digest, "size", None)

    if hash_value is None or size_value is None:
        return None

    if isinstance(hash_value, (bytes, bytearray)):
        hash_value = bytes(hash_value).hex()
    elif not isinstance(hash_value, str):
        hash_value = str(hash_value)

    try:
        size_value = int(size_value)
    except Exception:
        return None

    return f"{hash_value}/{size_value}"


def find_cas_dir(context, args: argparse.Namespace) -> str:
    if args.cas_dir:
        return os.path.abspath(os.path.expanduser(args.cas_dir))

    candidates = []

    def add_candidate(value):
        if value and isinstance(value, str):
            candidates.append(os.path.expanduser(value))

    for attr_name in (
        "casdir",
        "cas_cache_dir",
        "_casdir",
        "local_cas_dir",
        "_local_cas_dir",
    ):
        try:
            add_candidate(getattr(context, attr_name, None))
        except Exception:
            pass

    for obj_attr in (
        "cas",
        "_cas",
        "artifact_cache",
        "_artifact_cache",
        "source_cache",
        "_source_cache",
    ):
        try:
            obj = getattr(context, obj_attr, None)
        except Exception:
            continue

        if obj is None:
            continue

        for sub_attr in (
            "basedir",
            "_basedir",
            "cache_dir",
            "_cache_dir",
            "directory",
            "_directory",
            "path",
            "_path",
        ):
            try:
                add_candidate(getattr(obj, sub_attr, None))
            except Exception:
                pass

    for attr_name in ("sourcedir", "artifactdir", "builddir"):
        try:
            base = getattr(context, attr_name, None)
        except Exception:
            continue

        if base:
            add_candidate(base)
            add_candidate(os.path.join(base, "cas"))
            add_candidate(os.path.join(base, "cas", "cache"))

    home_cache = os.path.expanduser("~/.cache/buildstream")
    add_candidate(home_cache)
    add_candidate(os.path.join(home_cache, "cas"))
    add_candidate(os.path.join(home_cache, "cas", "cache"))

    for candidate in candidates:
        if os.path.isdir(candidate):
            objects_dir = os.path.join(candidate, "objects")
            if os.path.isdir(objects_dir):
                return candidate

            cas_subdir = os.path.join(candidate, "cas")
            if os.path.isdir(cas_subdir):
                cas_objects = os.path.join(cas_subdir, "objects")
                if os.path.isdir(cas_objects):
                    return cas_subdir
                return cas_subdir

            return candidate

    return home_cache


# ---------------------------------------------------------------------------
# buildbox-fuse discovery
# ---------------------------------------------------------------------------


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except Exception:
        return False


def _iter_tree(root: Path, max_depth: int = 6):
    root = Path(root)

    if not root.is_dir():
        return

    stack = [(root, 0)]

    while stack:
        current, depth = stack.pop()

        try:
            entries = list(current.iterdir())
        except Exception:
            continue

        for entry in entries:
            yield entry, depth

            try:
                if entry.is_dir():
                    if entry.name in (
                        ".git",
                        ".hg",
                        ".svn",
                        "__pycache__",
                        ".tox",
                        ".mypy_cache",
                        ".pytest_cache",
                        "node_modules",
                    ):
                        continue

                    if depth < max_depth:
                        stack.append((entry, depth + 1))
            except Exception:
                continue


def _find_named_file(root, max_depth: int = 6, executable_only: bool = False):
    root = Path(root)

    if not root.is_dir():
        return None

    for entry, _ in _iter_tree(root, max_depth=max_depth):
        try:
            if entry.is_file() and entry.name in BUILDBOX_FUSE_NAMES:
                if executable_only:
                    if _is_executable_file(entry):
                        return entry
                else:
                    return entry
        except Exception:
            continue

    return None


def _iter_buildstream_roots():
    roots = []
    seen = set()

    def add(path):
        if not path:
            return

        try:
            p = Path(path).resolve()
        except Exception:
            return

        if p not in seen:
            seen.add(p)
            roots.append(p)

    try:
        import buildstream

        module_file = getattr(buildstream, "__file__", None)
        if module_file:
            add(Path(module_file).parent)

        module_path = getattr(buildstream, "__path__", None)
        if module_path:
            for p in module_path:
                add(p)

    except Exception:
        pass

    try:
        spec = importlib.util.find_spec("buildstream")

        if spec is not None:
            if spec.origin and spec.origin not in ("built-in", "frozen"):
                add(Path(spec.origin).parent)

            search_locations = getattr(spec, "submodule_search_locations", None)
            if search_locations:
                for p in search_locations:
                    add(p)

    except Exception:
        pass

    return roots


def _iter_buildstream_package_candidates():
    for root in _iter_buildstream_roots():
        if not root.is_dir():
            continue

        known = [
            root / "subprojects" / "buildbox" / "buildbox-fuse",
            root / "subprojects" / "buildbox" / "buildbox-fuse" / "buildbox-fuse",
            root / "subprojects" / "buildbox" / "bin" / "buildbox-fuse",
            root / "subprojects" / "buildbox" / "build" / "buildbox-fuse",
            root / "subprojects" / "buildbox" / "build" / "bin" / "buildbox-fuse",
            root / "buildbox" / "buildbox-fuse",
            root / "bin" / "buildbox-fuse",
            root.parent / "bin" / "buildbox-fuse",
        ]

        for candidate in known:
            yield candidate

        subprojects = root / "subprojects"
        if subprojects.is_dir():
            found = _find_named_file(subprojects, max_depth=6, executable_only=True)
            if found is not None:
                yield found

        found = _find_named_file(root, max_depth=2, executable_only=True)
        if found is not None:
            yield found


def _iter_distribution_metadata_candidates():
    distribution_names = (
        "buildstream",
        "BuildStream",
        "buildbox",
        "buildbox-fuse",
    )

    for dist_name in distribution_names:
        try:
            dist = importlib.metadata.distribution(dist_name)
        except Exception:
            continue

        try:
            files = dist.files
        except Exception:
            files = None

        if not files:
            continue

        for rel_path in files:
            try:
                rel_path = Path(str(rel_path))
            except Exception:
                continue

            if rel_path.name not in BUILDBOX_FUSE_NAMES:
                continue

            if rel_path.is_absolute():
                yield rel_path
                continue

            try:
                yield Path(dist.locate_file(rel_path))
            except Exception:
                continue


def _iter_venv_bin_dirs():
    dirs = []
    seen = set()

    def add(path):
        try:
            p = Path(path).resolve()
        except Exception:
            return

        if p not in seen:
            seen.add(p)
            dirs.append(p)

    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        add(Path(virtual_env) / "bin")
        add(Path(virtual_env) / "Scripts")

    if sys.prefix:
        add(Path(sys.prefix) / "bin")
        add(Path(sys.prefix) / "Scripts")

    if sys.executable:
        add(Path(sys.executable).parent)

    return dirs


def find_buildbox_fuse(args):
    fallback_non_executable = None

    def consider_candidate(candidate):
        nonlocal fallback_non_executable

        if not candidate:
            return None

        try:
            p = Path(os.path.expanduser(str(candidate))).absolute()
        except Exception:
            return None

        if p.is_file():
            if _is_executable_file(p):
                return p

            if fallback_non_executable is None:
                fallback_non_executable = p

            return None

        if p.is_dir():
            found = _find_named_file(p, max_depth=6, executable_only=True)
            if found is not None:
                return found

            found_any = _find_named_file(p, max_depth=6, executable_only=False)
            if found_any is not None and fallback_non_executable is None:
                fallback_non_executable = found_any

        return None

    # 1. Explicit CLI override.
    explicit = getattr(args, "buildbox_fuse", None)
    if explicit:
        found = consider_candidate(explicit)
        if found is not None:
            return str(found)

        if fallback_non_executable is not None:
            return str(fallback_non_executable)

        return None

    # 2. Environment overrides.
    for env_var in (
        "BSSG_BUILDBOX_FUSE",
        "BUILDBOX_FUSE",
        "BUILDSTREAM_BUILDBOX_FUSE",
    ):
        value = os.environ.get(env_var)
        if value:
            found = consider_candidate(value)
            if found is not None:
                return str(found)

            if fallback_non_executable is not None:
                return str(fallback_non_executable)

            return None

    # 3. Package metadata candidates.
    for candidate in _iter_distribution_metadata_candidates():
        found = consider_candidate(candidate)
        if found is not None:
            return str(found)

    # 4. BuildStream package layout candidates.
    for candidate in _iter_buildstream_package_candidates():
        found = consider_candidate(candidate)
        if found is not None:
            return str(found)

    # 5. Virtualenv bin directories.
    for bin_dir in _iter_venv_bin_dirs():
        for name in BUILDBOX_FUSE_NAMES:
            candidate = bin_dir / name
            found = consider_candidate(candidate)
            if found is not None:
                return str(found)

    # 6. Normal PATH lookup.
    path_result = shutil.which("buildbox-fuse")
    if path_result:
        return path_result

    # 7. Last resort: return a non-executable file if we found one.
    if fallback_non_executable is not None:
        return str(fallback_non_executable)

    return None


# ---------------------------------------------------------------------------
# FUSE mount management
# ---------------------------------------------------------------------------


def unmount_mountpoint(mountpoint: str) -> bool:
    for cmd_name in ("fusermount3", "fusermount", "umount"):
        if shutil.which(cmd_name) is None:
            continue

        cmd = [cmd_name, mountpoint] if cmd_name == "umount" else [cmd_name, "-u", mountpoint]

        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            pass

    return False


class FuseMountManager:
    def __init__(
        self,
        buildbox_fuse: str,
        cas_dir: str,
        mount_base: str,
        digest_function: str,
        keep_mounts: bool,
        force_unmount: bool,
    ):
        self.buildbox_fuse = buildbox_fuse
        self.cas_dir = cas_dir
        self.mount_base = os.path.abspath(os.path.expanduser(mount_base))
        self.digest_function = digest_function
        self.keep_mounts = keep_mounts
        self.force_unmount = force_unmount
        self.mounts = {}
        self.started = 0
        self.peak = 0

    def ensure_mount(self, digest_value: str) -> str:
        if digest_value in self.mounts:
            return self.mounts[digest_value][0]

        safe_digest = digest_value.replace("/", "_")
        # Never reuse another search's mount: its cleanup can race our readers.
        os.makedirs(self.mount_base, exist_ok=True)
        mountpoint = tempfile.mkdtemp(prefix=safe_digest + "-", dir=self.mount_base)

        # IMPORTANT:
        # This buildbox-fuse parser requires --option=value syntax.
        cmd = [
            self.buildbox_fuse,
            f"--local={self.cas_dir}",
            f"--input-digest-value={digest_value}",
            f"--digest-function={self.digest_function}",
            mountpoint,
        ]

        log_path = f"{mountpoint}.fuse.log"

        try:
            with open(log_path, "ab") as log_file:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=log_file,
                )
        except Exception as exc:
            raise RuntimeError(f"failed to start buildbox-fuse: {exc}") from exc

        self.started += 1
        deadline = time.monotonic() + 10.0
        # buildbox-fuse usually mounts within a few milliseconds; a fixed 50 ms
        # poll made readiness waiting dominate per-tree mount cost.
        delay = MOUNT_POLL_INITIAL

        while time.monotonic() < deadline:
            if os.path.ismount(mountpoint):
                self.mounts[digest_value] = (mountpoint, proc, True)
                self.peak = max(self.peak, len(self.mounts))
                return mountpoint

            if proc.poll() is not None:
                stderr_text = ""
                try:
                    stderr_text = Path(log_path).read_text(
                        encoding="utf-8",
                        errors="replace",
                    )
                except Exception:
                    pass

                raise RuntimeError(f"buildbox-fuse exited early for {digest_value}: {stderr_text}")

            time.sleep(delay)
            delay = min(delay * 2, MOUNT_POLL_MAX)

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        raise RuntimeError(f"timed out waiting for buildbox-fuse mount: {mountpoint}")

    def release(self, digest_value: str) -> None:
        """Tear down one owned mount unless the user asked to keep it."""
        mountpoint, proc, mounted_by_us = self.mounts[digest_value]
        if not self.force_unmount and (not mounted_by_us or self.keep_mounts):
            return
        del self.mounts[digest_value]
        try:
            try:
                # buildbox-fuse unmounts on SIGTERM, which avoids spawning a
                # fusermount process per tree; fall back if the mount survives.
                if proc is not None:
                    stop_process(proc)
            finally:
                if os.path.ismount(mountpoint):
                    unmount_mountpoint(mountpoint)
            if os.path.ismount(mountpoint):
                print(f"WARNING: mount remains live after cleanup: {mountpoint}", file=sys.stderr)
                return
            Path(mountpoint).rmdir()
            Path(f"{mountpoint}.fuse.log").unlink(missing_ok=True)
        except Exception as exc:
            print(f"WARNING: cleanup failed for {mountpoint}: {exc}", file=sys.stderr)

    def cleanup(self):
        for digest_value in list(self.mounts):
            self.release(digest_value)


def stop_process(proc):
    if proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


class LineBuffer:
    def __init__(self, limit: int = 4096):
        self.limit = limit
        self.buf = []
        self.write = sys.stdout.write

    def emit(self, line: str):
        self.buf.append(line)
        self.buf.append("\n")

        if len(self.buf) >= self.limit * 2:
            self.flush()

    def emit_json(self, obj):
        self.emit(json.dumps(obj, ensure_ascii=True))

    def flush(self):
        if self.buf:
            self.write("".join(self.buf))
            self.buf.clear()


# ---------------------------------------------------------------------------
# BuildStream bootstrapping
# ---------------------------------------------------------------------------


class DummyMessageHandler:
    def __call__(self, *args, **kwargs):
        pass

    def message(self, *args, **kwargs):
        pass

    def handle_message(self, *args, **kwargs):
        pass


def attach_dummy_message_handler(context) -> bool:
    dummy = DummyMessageHandler()

    candidates = [context]

    try:
        messenger = getattr(context, "messenger", None)
        if messenger is not None:
            candidates.append(messenger)
    except Exception:
        pass

    for obj in candidates:
        if obj is None:
            continue

        for method_name in ("set_message_handler", "set_handler"):
            method = getattr(obj, method_name, None)
            if callable(method):
                try:
                    method(dummy)
                    return True
                except Exception:
                    pass

        for attr_name in (
            "_message_handler",
            "message_handler",
            "_handler",
            "handler",
        ):
            if hasattr(obj, attr_name):
                try:
                    setattr(obj, attr_name, dummy)
                    return True
                except Exception:
                    pass

    return False


def element_label(element) -> str:
    for name in ("_get_full_name", "get_full_name"):
        try:
            value = getattr(element, name, None)
            if value is not None:
                return value() if callable(value) else str(value)
        except Exception:
            pass

    try:
        name = getattr(element, "name", None)
        if name is not None:
            return str(name)
    except Exception:
        pass

    return str(element)


def get_pipeline_selection_class():
    for module_name in (
        "buildstream.types",
        "buildstream._types",
        "buildstream._stream",
        "buildstream",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue

        for class_name in ("PipelineSelection", "_PipelineSelection"):
            obj = getattr(module, class_name, None)
            if obj is not None:
                return obj

    return None


def make_selection(choice: str):
    cls = get_pipeline_selection_class()

    if cls is None:
        return choice

    members = getattr(cls, "__members__", None)
    if members is not None:
        upper = choice.upper()

        if upper in members:
            return members[upper]

        for member in members.values():
            if getattr(member, "value", None) == choice:
                return member

    upper_attr = getattr(cls, choice.upper(), None)
    if upper_attr is not None:
        return upper_attr

    try:
        return cls(choice)
    except Exception:
        pass

    try:
        return cls[choice.upper()]
    except Exception:
        pass

    return choice


def create_project(Project, context, args, fetch_subprojects=None):
    # BuildStream expects a callback here, not the CLI's boolean.
    def refuse_fetch(junctions):
        names = ", ".join(sorted(element_label(j) for j in junctions)) or "unknown junction"
        raise RuntimeError(
            f"Subproject sources are missing for {names}; fetch them with bst "
            "(e.g. bst source fetch on a junction or any element inside it) "
            "or pass --fetch-subprojects to allow fetching"
        )

    callback = fetch_subprojects if args.fetch_subprojects else refuse_fetch
    if callback is None:
        raise RuntimeError("No BuildStream subproject fetch callback is available")
    return Project(
        args.directory,
        context,
        cli_options=list(dict(args.option).items()),
        fetch_subprojects=callback,
    )


def call_load_selection(stream, target: str, selection):
    return list(
        stream.load_selection(
            (target,),
            selection=selection,
            connect_artifact_cache=False,
            connect_source_cache=False,
            need_state=False,
        )
    )


def cleanup_stream(stream) -> None:
    if stream is None:
        return

    cleanup = getattr(stream, "cleanup", None)
    if callable(cleanup):
        try:
            cleanup()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Origin resolution
# ---------------------------------------------------------------------------


def get_origin_for_element_path(
    element_info,
    rel_path: str,
    tree,
    args: argparse.Namespace,
    source_info_cache: dict,
):
    if not args.origin:
        return None

    element = element_info["element"]
    key = id(element)

    source_infos = source_info_cache.get(key)
    if source_infos is None:
        source_infos = element_source_infos(element)
        source_info_cache[key] = source_infos

    source_info = guess_source_info(source_infos, rel_path)

    if source_info is not None:
        origin = dict(source_info)
    else:
        origin = {
            "id": "ambiguous" if source_infos else "no-source",
        }

    if args.gitreview_mode != "never":
        needs_gitreview = False

        if args.gitreview_mode == "always":
            needs_gitreview = True
        else:
            if not origin.get("gerrit_project_effective"):
                needs_gitreview = True

        if needs_gitreview:
            gitreview_cache = tree.get("gitreview")
            if gitreview_cache is not None:
                gitreview_info = gitreview_cache.for_path(rel_path)
                if gitreview_info is not None:
                    origin = enrich_origin_with_gitreview(origin, gitreview_info)

    return origin


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def peak_rss_mib() -> float:
    """Peak resident set of this process only (not casd, buildbox-fuse or rg)."""
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except (ImportError, OSError):
        return 0.0


def print_stats(stats: dict) -> None:
    print("", file=sys.stderr)
    print("bst-source-grep statistics:", file=sys.stderr)

    print(f"  elements:             {stats['elements']}", file=sys.stderr)
    print(f"  cached elements:      {stats['cached_elements']}", file=sys.stderr)
    print(f"  sourceless elements:  {stats['sourceless_elements']}", file=sys.stderr)
    print(f"  uncached elements:    {stats['uncached_elements']}", file=sys.stderr)
    print(f"  unresolved elements:  {stats['unresolved_elements']}", file=sys.stderr)
    print(f"  no-digest elements:   {stats['no_digest_elements']}", file=sys.stderr)
    print(f"  duplicate elements:   {stats['duplicate_elements']}", file=sys.stderr)

    print(f"  unique trees:         {stats['trees']}", file=sys.stderr)
    print(f"  mounted trees:        {stats['mounted_trees']}", file=sys.stderr)
    print(f"  peak mounts:          {stats['peak_mounts']}", file=sys.stderr)
    print(f"  fuse processes:       {stats['fuse_processes']}", file=sys.stderr)
    print(f"  rg processes:         {stats['rg_processes']}", file=sys.stderr)
    print(f"  mount errors:         {stats['mount_errors']}", file=sys.stderr)
    print(f"  traversal errors:     {stats['traversal_errors']}", file=sys.stderr)
    print(f"  search errors:        {stats['search_errors']}", file=sys.stderr)

    print(f"  path cache hits:      {stats['path_cache_hits']}", file=sys.stderr)
    print(f"  path cache misses:    {stats['path_cache_misses']}", file=sys.stderr)

    print(f"  results:              {stats['results']}", file=sys.stderr)
    print(f"  peak rss:             {stats['peak_rss_mib']:.1f} MiB", file=sys.stderr)

    print(f"  load time:            {format_seconds(stats['load_seconds'])}", file=sys.stderr)
    print(f"  mount time:           {format_seconds(stats['mount_seconds'])}", file=sys.stderr)
    print(f"  search time:          {format_seconds(stats['search_seconds'])}", file=sys.stderr)
    print(f"  cleanup time:         {format_seconds(stats['cleanup_seconds'])}", file=sys.stderr)
    print(f"  total time:           {format_seconds(stats['total_seconds'])}", file=sys.stderr)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _main() -> int:
    parser = build_parser()
    args = parser.parse_intermixed_args()

    if args.find is None and args.pattern is None:
        parser.error("PATTERN is required unless --find is used")

    if args.find is not None and args.pattern is not None:
        parser.error("PATTERN cannot be used together with --find")

    if args.backend == "cas" and args.find is None:
        parser.error("--backend=cas currently only supports --find")

    for stream_name in (sys.stdout, sys.stderr):
        if hasattr(stream_name, "reconfigure"):
            try:
                stream_name.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    start_time = time.monotonic()

    use_fuse = True
    if args.find is not None and args.backend in ("auto", "cas"):
        use_fuse = False

    buildbox_fuse = None

    if use_fuse:
        if shutil.which("rg") is None:
            print(
                "error: ripgrep (`rg`) is required for FUSE/content search but was not found",
                file=sys.stderr,
            )
            return 2

        buildbox_fuse = find_buildbox_fuse(args)

        if buildbox_fuse is None or not os.path.isfile(buildbox_fuse):
            print(
                "error: could not find buildbox-fuse.\n"
                "hint: pass --buildbox-fuse /path/to/buildbox-fuse\n"
                "hint: or set BUILDBOX_FUSE=/path/to/buildbox-fuse",
                file=sys.stderr,
            )
            return 2

        if not os.access(buildbox_fuse, os.X_OK):
            print(
                f"error: buildbox-fuse found but is not executable: {buildbox_fuse}",
                file=sys.stderr,
            )
            print(
                "hint: chmod +x that file, or provide an executable wrapper via --buildbox-fuse",
                file=sys.stderr,
            )
            return 2

    stats = {
        "elements": 0,
        "cached_elements": 0,
        "sourceless_elements": 0,
        "uncached_elements": 0,
        "unresolved_elements": 0,
        "no_digest_elements": 0,
        "duplicate_elements": 0,
        "trees": 0,
        "mounted_trees": 0,
        "peak_mounts": 0,
        "fuse_processes": 0,
        "rg_processes": 0,
        "mount_errors": 0,
        "traversal_errors": 0,
        "search_errors": 0,
        "path_cache_hits": 0,
        "path_cache_misses": 0,
        "results": 0,
        "load_seconds": 0.0,
        "mount_seconds": 0.0,
        "search_seconds": 0.0,
        "cleanup_seconds": 0.0,
        "total_seconds": 0.0,
        "peak_rss_mib": 0.0,
    }

    try:
        version = importlib.metadata.version("BuildStream")
        from packaging.version import Version

        if not Version("2.8") <= Version(version) < Version("3"):
            raise RuntimeError(f"BuildStream {version} is unsupported; use >=2.8,<3")
        from buildstream._context import Context
        from buildstream._project import Project
        from buildstream._stream import Stream
    except Exception as exc:
        print(
            f"error: unable to import BuildStream internals: {exc}",
            file=sys.stderr,
        )
        return 2

    selection = make_selection(args.deps)

    context = Context()
    attach_dummy_message_handler(context)

    if hasattr(context, "__enter__") and hasattr(context, "__exit__"):
        ctx_manager = context
    elif hasattr(context, "close"):
        ctx_manager = closing(context)
    else:
        ctx_manager = nullcontext(context)

    exit_code = 2
    stream = None
    mount_manager = None
    out = LineBuffer()
    dedup = RecordDeduplicator(args.strip_junctions)
    source_info_cache = {}

    try:
        with ctx_manager as ctx:
            if ctx is None:
                ctx = context

            try:
                load_start = time.monotonic()

                ctx.load(args.config)
                attach_dummy_message_handler(ctx)

                # The scheduler only runs for --fetch-subprojects; it calls both
                # callbacks unconditionally, so provide non-interactive ones.
                stream = Stream(
                    ctx,
                    datetime.now(),
                    interrupt_callback=lambda: stream.terminate(),
                    ticker_callback=lambda: None,
                )
                stream.init()
                project = create_project(Project, ctx, args, stream.fetch_subprojects)
                stream.set_project(project)

                elements = call_load_selection(stream, args.target, selection)

                cas_dir = None

                if use_fuse:
                    cas_dir = find_cas_dir(ctx, args)

                    if not os.path.isdir(cas_dir):
                        print(
                            f"error: CAS directory does not exist: {cas_dir}\n"
                            "hint: use --cas-dir to specify it explicitly",
                            file=sys.stderr,
                        )
                        exit_code = 2
                        return exit_code

                trees = {}

                # ------------------------------------------------------------
                # Pass 1: discover unique source trees
                # ------------------------------------------------------------

                for element in elements:
                    stats["elements"] += 1

                    label = element_label(element)
                    recipe = strip_junction_name(label) if args.strip_junctions else label

                    directory, status, detail = load_source_directory(element)

                    if status == "nosources":
                        stats["sourceless_elements"] += 1
                        continue

                    if status == "unresolved":
                        stats["unresolved_elements"] += 1
                        print(
                            f"ERROR: source refs are unresolved: {label} ({detail})",
                            file=sys.stderr,
                        )
                        continue

                    if status == "uncached":
                        stats["uncached_elements"] += 1
                        message = f"ERROR: source tree is not cached: {label}"
                        if detail:
                            message += f" ({detail})"
                        print(message, file=sys.stderr)
                        continue

                    stats["cached_elements"] += 1

                    digest_obj = get_cas_directory_digest(directory)
                    digest_value = digest_to_fuse_value(digest_obj)

                    if digest_value is None:
                        stats["no_digest_elements"] += 1

                        if use_fuse:
                            print(
                                f"WARNING: could not determine CAS digest for: {label}",
                                file=sys.stderr,
                            )
                            continue

                        tree_key = f"nondigest:{label}:{id(element)}"
                    else:
                        tree_key = digest_value

                    tree = trees.get(tree_key)
                    if tree is None:
                        tree = {
                            "digest": digest_value,
                            "directory": directory,
                            "elements": [],
                            "recipes_seen": set(),
                            "mountpoint": None,
                            "gitreview": CasGitreviewCache(
                                directory, args.gitreview_mode, args.gitreview_nearest
                            )
                            if not use_fuse and args.origin
                            else None,
                        }
                        trees[tree_key] = tree
                        stats["trees"] += 1

                    if args.strip_junctions:
                        if recipe in tree["recipes_seen"]:
                            stats["duplicate_elements"] += 1
                            continue
                        tree["recipes_seen"].add(recipe)

                    tree["elements"].append(
                        {
                            "element": element,
                            "label": label,
                            "recipe": recipe,
                        }
                    )

                stats["load_seconds"] = time.monotonic() - load_start

                exit_code = 1
                if (
                    stats["uncached_elements"] > 0
                    or stats["unresolved_elements"] > 0
                    or (use_fuse and stats["no_digest_elements"] > 0)
                ):
                    exit_code = 2

                if not trees:
                    if exit_code != 2:
                        exit_code = 1
                    return exit_code

                # ------------------------------------------------------------
                # Emission helpers
                # ------------------------------------------------------------

                def emit_file(element_info, rel_path, origin=None):
                    display = (
                        element_info["recipe"] if args.strip_junctions else element_info["label"]
                    )

                    if dedup.duplicate("file", display, rel_path):
                        return

                    if args.json:
                        record = {
                            "type": "file",
                            "element": element_info["label"],
                            "recipe": display,
                            "path": rel_path,
                        }

                        if args.origin and origin is not None:
                            record["origin"] = origin

                        out.emit_json(record)
                    else:
                        if args.origin and origin is not None:
                            origin_id = origin.get("id", "?")
                            out.emit(f"{display}:{origin_id}:{rel_path}")
                        else:
                            out.emit(f"{display}:{rel_path}")

                    stats["results"] += 1

                def emit_match(element_info, rel_path, line_number, text, origin=None):
                    display = (
                        element_info["recipe"] if args.strip_junctions else element_info["label"]
                    )

                    # Distinct trees can share a stripped recipe name and path but
                    # differ in content, so the line text is part of the identity.
                    if dedup.duplicate("match", display, rel_path, (line_number, hash(text))):
                        return

                    if args.json:
                        record = {
                            "type": "match",
                            "element": element_info["label"],
                            "recipe": display,
                            "path": rel_path,
                            "line": line_number,
                            "text": text,
                        }

                        if args.origin and origin is not None:
                            record["origin"] = origin

                        out.emit_json(record)
                    else:
                        if args.origin and origin is not None:
                            origin_id = origin.get("id", "?")
                            prefix = f"{display}:{origin_id}:{rel_path}"
                        else:
                            prefix = f"{display}:{rel_path}"

                        if args.line_number:
                            out.emit(f"{prefix}:{line_number}:{text}")
                        else:
                            out.emit(f"{prefix}:{text}")

                    stats["results"] += 1

                # ------------------------------------------------------------
                # CAS-direct filename search
                # ------------------------------------------------------------

                if not use_fuse:
                    search_start = time.monotonic()

                    matcher = make_path_filter(args)

                    path_cache_dir = None
                    if args.path_cache_enabled and args.path_cache_dir:
                        path_cache_dir = Path(
                            os.path.abspath(os.path.expanduser(args.path_cache_dir))
                        )
                        path_cache_dir.mkdir(parents=True, exist_ok=True)

                    for tree in trees.values():
                        directory = tree.get("directory")
                        if directory is None:
                            continue

                        digest_value = tree.get("digest")
                        path_iterator = None

                        if path_cache_dir is not None and digest_value is not None:
                            cache_file = path_cache_dir / (
                                digest_value.replace("/", "_") + ".v2.paths.jsonl"
                            )

                            if cache_file.is_file() and not args.rebuild_path_cache:
                                stats["path_cache_hits"] += 1
                                path_iterator = iter_path_cache_file(cache_file)
                            else:
                                stats["path_cache_misses"] += 1
                                path_iterator = iter_directory_and_cache(
                                    directory,
                                    cache_file,
                                )
                        else:
                            path_iterator = iter_relative_paths(directory)

                        with closing(iter_checked_paths(iter(path_iterator), tree, stats)) as paths:
                            for path in paths:
                                if not matcher(path):
                                    continue
                                for element_info in tree["elements"]:
                                    origin = get_origin_for_element_path(
                                        element_info,
                                        path,
                                        tree,
                                        args,
                                        source_info_cache,
                                    )
                                    emit_file(element_info, path, origin)

                    stats["search_seconds"] = time.monotonic() - search_start

                    out.flush()

                    if (
                        stats["uncached_elements"] > 0
                        or stats["unresolved_elements"] > 0
                        or stats["traversal_errors"] > 0
                        or stats["search_errors"] > 0
                    ):
                        exit_code = 2
                    else:
                        exit_code = 0 if stats["results"] > 0 else 1

                    return exit_code

                # ------------------------------------------------------------
                # FUSE mount phase
                # ------------------------------------------------------------

                mount_manager = FuseMountManager(
                    buildbox_fuse=buildbox_fuse,
                    cas_dir=cas_dir,
                    mount_base=args.mount_dir,
                    digest_function=args.digest_function,
                    keep_mounts=args.keep_mounts,
                    force_unmount=args.force_unmount,
                )

                # Each tree is mounted, searched and released before the next one,
                # so at most one owned mount is live unless --keep-mounts is used.
                for tree in trees.values():
                    phase_start = time.monotonic()
                    try:
                        mountpoint = mount_manager.ensure_mount(tree["digest"])
                    except Exception as exc:
                        stats["mount_errors"] += 1
                        print(
                            f"ERROR: failed to mount tree {tree['digest']}: {exc}",
                            file=sys.stderr,
                        )
                        continue
                    finally:
                        stats["mount_seconds"] += time.monotonic() - phase_start

                    stats["mounted_trees"] += 1
                    tree["mountpoint"] = mountpoint
                    tree["gitreview"] = MountedGitreviewCache(
                        mountpoint,
                        args.gitreview_mode,
                        args.gitreview_nearest,
                    )

                    phase_start = time.monotonic()
                    try:
                        stats["rg_processes"] += 1
                        with closing(iter_mounted_matches(args, mountpoint)) as matches:
                            for kind, rel_path, line_number, text in matches:
                                for element_info in tree["elements"]:
                                    origin = get_origin_for_element_path(
                                        element_info,
                                        rel_path,
                                        tree,
                                        args,
                                        source_info_cache,
                                    )
                                    if kind == "file":
                                        emit_file(element_info, rel_path, origin)
                                    else:
                                        emit_match(
                                            element_info, rel_path, line_number, text, origin
                                        )
                    except SearchError as exc:
                        stats["search_errors"] += 1
                        print(f"ERROR: tree {tree['digest']}: {exc}", file=sys.stderr)
                    finally:
                        stats["search_seconds"] += time.monotonic() - phase_start

                    phase_start = time.monotonic()
                    tree["gitreview"] = None
                    mount_manager.release(tree["digest"])
                    stats["cleanup_seconds"] += time.monotonic() - phase_start

                out.flush()

                if (
                    stats["uncached_elements"] > 0
                    or stats["unresolved_elements"] > 0
                    or stats["mount_errors"] > 0
                    or stats["search_errors"] > 0
                    or stats["no_digest_elements"] > 0
                ):
                    exit_code = 2
                else:
                    exit_code = 0 if stats["results"] > 0 else 1

            except (KeyboardInterrupt, BrokenPipeError):
                raise

            except Exception as exc:
                if args.traceback:
                    traceback.print_exc()
                else:
                    print(f"ERROR: {exc!r}", file=sys.stderr)
                    print(
                        "Hint: rerun with --traceback for details.",
                        file=sys.stderr,
                    )
                exit_code = 2

            finally:
                try:
                    if mount_manager is not None:
                        mount_manager.cleanup()
                        stats["peak_mounts"] = mount_manager.peak
                        stats["fuse_processes"] = mount_manager.started
                finally:
                    cleanup_stream(stream)

    except BrokenPipeError:
        raise

    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        exit_code = 130

    except Exception as exc:
        if args.traceback:
            traceback.print_exc()
        else:
            print(f"ERROR: {exc!r}", file=sys.stderr)
            print(
                "Hint: rerun with --traceback for details.",
                file=sys.stderr,
            )
        exit_code = 2

    finally:
        out.flush()
        sys.stdout.flush()

        stats["total_seconds"] = time.monotonic() - start_time
        stats["peak_rss_mib"] = peak_rss_mib()

        if args.stats:
            print_stats(stats)

    return exit_code


def main() -> int:
    try:
        return _main()
    except BrokenPipeError:
        # Prevent interpreter shutdown from flushing the broken stdout again.
        try:
            with open(os.devnull, "wb") as sink:
                os.dup2(sink.fileno(), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 141


if __name__ == "__main__":
    sys.exit(main())
