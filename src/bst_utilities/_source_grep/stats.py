"""Initialize search counters and report phase timing and resource diagnostics."""

from __future__ import annotations

import sys


def format_seconds(value: float) -> str:
    return f"{value:.3f}s"


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
    print(f"  search jobs:          {stats['search_jobs']}", file=sys.stderr)
    print(f"  fuse processes:       {stats['fuse_processes']}", file=sys.stderr)
    print(f"  rg processes:         {stats['rg_processes']}", file=sys.stderr)
    print(f"  mount errors:         {stats['mount_errors']}", file=sys.stderr)
    print(f"  traversal errors:     {stats['traversal_errors']}", file=sys.stderr)
    print(f"  search errors:        {stats['search_errors']}", file=sys.stderr)

    print(f"  path cache hits:      {stats['path_cache_hits']}", file=sys.stderr)
    print(f"  path cache misses:    {stats['path_cache_misses']}", file=sys.stderr)

    if stats["option_sets_planned"]:
        print(f"  option sets planned:  {stats['option_sets_planned']}", file=sys.stderr)
        print(f"  option sets loaded:   {stats['option_sets']}", file=sys.stderr)
        print(f"  option sets skipped:  {stats['option_sets_skipped']}", file=sys.stderr)
        print(f"  option set errors:    {stats['option_set_errors']}", file=sys.stderr)

    print(f"  results:              {stats['results']}", file=sys.stderr)
    print(f"  peak rss:             {stats['peak_rss_mib']:.1f} MiB", file=sys.stderr)

    print(f"  load time:            {format_seconds(stats['load_seconds'])}", file=sys.stderr)
    # Parts of load time: project/element loading, then concurrent tree checks.
    project_load = format_seconds(stats["project_load_seconds"])
    print(f"  project load time:    {project_load}", file=sys.stderr)
    print(f"  cache checks:         {stats['cache_checks']}", file=sys.stderr)
    cache_check = format_seconds(stats["cache_check_seconds"])
    print(f"  cache check time:     {cache_check}", file=sys.stderr)
    print(f"  mount time:           {format_seconds(stats['mount_seconds'])}", file=sys.stderr)
    print(f"  search time:          {format_seconds(stats['search_seconds'])}", file=sys.stderr)
    print(f"  cleanup time:         {format_seconds(stats['cleanup_seconds'])}", file=sys.stderr)
    print(f"  total time:           {format_seconds(stats['total_seconds'])}", file=sys.stderr)


def new_stats():
    return {
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
        "search_jobs": 1,
        "mount_errors": 0,
        "traversal_errors": 0,
        "search_errors": 0,
        "path_cache_hits": 0,
        "path_cache_misses": 0,
        "results": 0,
        "option_sets_planned": 0,
        "option_sets": 0,
        "option_sets_skipped": 0,
        "option_set_errors": 0,
        "load_seconds": 0.0,
        "project_load_seconds": 0.0,
        "cache_check_seconds": 0.0,
        "cache_checks": 0,
        "mount_seconds": 0.0,
        "search_seconds": 0.0,
        "cleanup_seconds": 0.0,
        "total_seconds": 0.0,
        "peak_rss_mib": 0.0,
    }
