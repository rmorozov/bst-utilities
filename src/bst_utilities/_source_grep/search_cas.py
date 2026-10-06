"""Search cached filenames with direct traversal or complete path indexes."""

from __future__ import annotations

import os
import time
from contextlib import closing
from pathlib import Path

from . import cas, matching, origins


def search_cas(trees, args, stats, cas_dir, emitter):
    emit_file = emitter.emit_file
    out = emitter.out
    source_info_cache = {}
    search_start = time.monotonic()

    matcher = matching.make_path_filter(args)

    path_cache_dir = None
    if args.path_cache_enabled and args.path_cache_dir:
        path_cache_dir = Path(os.path.abspath(os.path.expanduser(args.path_cache_dir)))
        path_cache_dir.mkdir(parents=True, exist_ok=True)

    for tree in trees.values():
        directory = tree.get("directory")
        if directory is None:
            continue

        digest_value = tree.get("digest")
        path_iterator = None

        if path_cache_dir is not None and digest_value is not None:
            cache_file = path_cache_dir / (digest_value.replace("/", "_") + ".v2.paths.jsonl")

            if cache_file.is_file() and not args.rebuild_path_cache:
                stats["path_cache_hits"] += 1
                path_iterator = cas.iter_path_cache_file(cache_file)
            else:
                stats["path_cache_misses"] += 1
                path_iterator = cas.iter_directory_and_cache(
                    cas.tree_paths(tree, cas_dir),
                    cache_file,
                )
        else:
            path_iterator = cas.tree_paths(tree, cas_dir)

        with closing(cas.iter_checked_paths(iter(path_iterator), tree, stats)) as paths:
            for path in paths:
                if not matcher(path):
                    continue
                for element_info in tree["elements"]:
                    origin = origins.get_origin_for_element_path(
                        element_info,
                        path,
                        tree,
                        args,
                        source_info_cache,
                    )
                    emit_file(element_info, path, origin)
        # Release per-tree BuildStream objects once traversed.
        tree["directory"] = None
        tree["gitreview"] = None

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
