"""Search mounted trees serially or through an ordered bounded worker pool."""

from __future__ import annotations

import sys
import tempfile
import time
from contextlib import closing

from . import origins, processes, ripgrep


def search_fuse(trees, args, stats, mount_manager, emitter):
    emit_file = emitter.emit_file
    emit_match = emitter.emit_match
    out = emitter.out
    source_info_cache = {}

    def emit_tree_matches(tree, matches):
        with closing(matches):
            for kind, rel_path, line_number, text in matches:
                for element_info in tree["elements"]:
                    origin = origins.get_origin_for_element_path(
                        element_info,
                        rel_path,
                        tree,
                        args,
                        source_info_cache,
                    )
                    if kind == "file":
                        emit_file(element_info, rel_path, origin)
                    else:
                        emit_match(element_info, rel_path, line_number, text, origin)

    def attach_gitreview(tree, mountpoint):
        tree["mountpoint"] = mountpoint
        tree["gitreview"] = origins.MountedGitreviewCache(
            mountpoint,
            args.gitreview_mode,
            args.gitreview_nearest,
        )

    def release_tree(tree):
        phase_start = time.monotonic()
        tree["gitreview"] = None
        mount_manager.release(tree["digest"])
        return time.monotonic() - phase_start

    tree_list = list(trees.values())
    # A pool only helps with several trees; a single tree keeps the
    # streaming path so output starts at once and needs no spool.
    jobs = max(1, min(args.jobs, len(tree_list)))
    # Phase times are summed across concurrent trees, so with more
    # than one job they can exceed the wall-clock total.
    stats["search_jobs"] = jobs

    if jobs == 1:
        # Each tree is mounted, searched and released before the next
        # one, streaming rg output as it arrives.
        for tree in tree_list:
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
            attach_gitreview(tree, mountpoint)

            phase_start = time.monotonic()
            try:
                stats["rg_processes"] += 1
                emit_tree_matches(tree, ripgrep.iter_mounted_matches(args, mountpoint))
            except processes.SearchError as exc:
                stats["search_errors"] += 1
                print(f"ERROR: tree {tree['digest']}: {exc}", file=sys.stderr)
            finally:
                stats["search_seconds"] += time.monotonic() - phase_start

            stats["cleanup_seconds"] += release_tree(tree)
    else:
        # Up to --jobs trees are mounted and searched concurrently into
        # spool files; output is emitted in tree order from this thread.
        # Without --origin, a worker releases its mount as soon as rg
        # finishes, so unmounting overlaps other trees' searches.
        runner = processes.RgRunner()
        rg_cmd = ripgrep.rg_command(args)[0]
        keep_until_emitted = bool(args.origin)

        def search_tree(tree):
            result = {"error": None, "kind": None, "spool": None}
            phase_start = time.monotonic()
            try:
                mountpoint = mount_manager.ensure_mount(tree["digest"])
            except Exception as exc:
                result.update(kind="mount", error=str(exc))
                return result
            finally:
                result["mount_seconds"] = time.monotonic() - phase_start
            result["mountpoint"] = mountpoint
            phase_start = time.monotonic()
            spool = tempfile.TemporaryFile(mode="w+b")
            result["spool"] = spool
            try:
                runner.run(rg_cmd, mountpoint, spool)
            except processes.SearchError as exc:
                result.update(kind="search", error=str(exc))
            except Exception:
                spool.close()
                raise
            finally:
                result["search_seconds"] = time.monotonic() - phase_start
                if not keep_until_emitted:
                    result["cleanup_seconds"] = release_tree(tree)
            return result

        try:
            with closing(
                processes.iter_ordered(tree_list, jobs, search_tree, runner.abort)
            ) as done:
                for tree, result in done:
                    stats["mount_seconds"] += result["mount_seconds"]
                    if result["kind"] == "mount":
                        stats["mount_errors"] += 1
                        print(
                            f"ERROR: failed to mount tree {tree['digest']}: {result['error']}",
                            file=sys.stderr,
                        )
                        continue
                    stats["mounted_trees"] += 1
                    stats["rg_processes"] += 1
                    stats["search_seconds"] += result["search_seconds"]
                    if keep_until_emitted:
                        attach_gitreview(tree, result["mountpoint"])
                    phase_start = time.monotonic()
                    try:
                        # Like the streaming path, emit what rg produced
                        # before reporting its failure.
                        emit_tree_matches(tree, ripgrep.iter_spooled_matches(args, result["spool"]))
                        if result["kind"] == "search":
                            raise processes.SearchError(result["error"])
                    except processes.SearchError as exc:
                        stats["search_errors"] += 1
                        print(f"ERROR: tree {tree['digest']}: {exc}", file=sys.stderr)
                    finally:
                        result["spool"].close()
                        stats["search_seconds"] += time.monotonic() - phase_start
                    if keep_until_emitted:
                        stats["cleanup_seconds"] += release_tree(tree)
                    else:
                        stats["cleanup_seconds"] += result["cleanup_seconds"]
        finally:
            runner.abort()

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

    return exit_code
