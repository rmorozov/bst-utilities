"""Group selected elements by cached source-tree digest without losing attribution."""

from __future__ import annotations

import sys

from . import adapter, cas_layout, origins, paths, source_cache


def discover_trees(elements, args, stats, use_fuse):
    trees = {}

    # ------------------------------------------------------------
    # Pass 1: discover unique source trees
    # ------------------------------------------------------------

    for element in elements:
        stats["elements"] += 1

        label = adapter.element_label(element)
        recipe = paths.strip_junction_name(label) if args.strip_junctions else label

        directory, status, detail = source_cache.load_source_directory(element)

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

        digest_obj = cas_layout.get_cas_directory_digest(directory)
        digest_value = cas_layout.digest_to_fuse_value(digest_obj)

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
                "digest_obj": digest_obj,
                "directory": directory,
                "elements": [],
                "recipes_seen": set(),
                "mountpoint": None,
                "gitreview": origins.CasGitreviewCache(
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

    return trees
