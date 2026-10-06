"""Regular-file CAS traversal and complete atomic path-index storage."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from . import paths


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
            p = paths.normalize_path(path)
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
                p = paths.normalize_path(item)
                if p:
                    yield p
                continue

            if isinstance(item, (list, tuple)):
                if len(item) == 3:
                    root, dirs, files = item

                    for name in dirs:
                        p = paths.normalize_path(os.path.join(root, name))
                        if p:
                            yield p

                    for name in files:
                        p = paths.normalize_path(os.path.join(root, name))
                        if p:
                            yield p

                    continue

                if item:
                    p = paths.normalize_path(item[0])
                    if p:
                        yield p

        return

    raise RuntimeError("BuildStream CAS directory does not expose list_relative_paths() or walk()")


def iter_cas_files(cas_dir: str, digest):
    """
    Yield regular-file paths of a CAS tree by reading Directory protos directly.

    Same order as CasBasedDirectory.list_relative_paths() filtered to regular
    files (sorted files, then sorted subdirectories, depth first), but without
    BuildStream caching every visited subdirectory as a Python object or
    resolving each path from the root again. Memory is bounded by tree depth.
    """
    from buildstream._protos.build.bazel.remote.execution.v2 import remote_execution_pb2

    def read(dir_digest):
        h = dir_digest.hash
        message = remote_execution_pb2.Directory()
        with open(os.path.join(cas_dir, "objects", h[:2], h[2:]), "rb") as f:
            message.ParseFromString(f.read())
        return message

    # Each frame: (prefix, iterator over sorted subdirectory nodes).
    def frame(prefix, message):
        for name in sorted(node.name for node in message.files):
            yield f"{prefix}{name}"
        for node in sorted(message.directories, key=lambda n: n.name):
            yield from frame(f"{prefix}{node.name}/", read(node.digest))

    yield from frame("", read(digest))


def iter_path_cache_file(cache_file: Path):
    with open(cache_file, "r", encoding="utf-8") as f:
        for line in f:
            path = json.loads(line)
            if not isinstance(path, str):
                raise ValueError("invalid path-index record")
            yield path


def tree_paths(tree, cas_dir):
    digest = tree.get("digest_obj")
    if cas_dir and getattr(digest, "hash", None):
        return iter_cas_files(cas_dir, digest)
    return iter_relative_paths(tree["directory"])


def iter_directory_and_cache(paths, cache_file: Path):
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
            for path in paths:
                out.write(json.dumps(path) + "\n")
                yield path
        os.replace(tmp_file, cache_file)
    finally:
        if tmp_file is not None:
            tmp_file.unlink(missing_ok=True)


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
