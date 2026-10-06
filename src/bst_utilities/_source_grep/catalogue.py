"""Group selected elements by cached source-tree digest without losing attribution."""

from __future__ import annotations

import sys

from . import adapter, cas_layout, option_space, origins, paths, source_cache


def discover_trees(elements, args, stats, use_fuse, trees=None, option_set=None):
    """
    Group `elements` into `trees` (a new dict unless one is passed in).

    With `option_set`, the elements came from one --all-options load: an
    element whose name already reached the same tree under another option set
    gains that set in its "option_sets" list instead of a second entry.
    """
    if trees is None:
        trees = {}
    context = f" [options: {option_space.label(option_set)}]" if option_set is not None else ""

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
                f"ERROR: source refs are unresolved: {label} ({detail}){context}",
                file=sys.stderr,
            )
            continue

        if status == "uncached":
            stats["uncached_elements"] += 1
            message = f"ERROR: source tree is not cached: {label}"
            if detail:
                message += f" ({detail})"
            print(message + context, file=sys.stderr)
            continue

        stats["cached_elements"] += 1

        digest_obj = cas_layout.get_cas_directory_digest(directory)
        digest_value = cas_layout.digest_to_fuse_value(digest_obj)

        if digest_value is None:
            stats["no_digest_elements"] += 1

            if use_fuse:
                print(
                    f"WARNING: could not determine CAS digest for: {label}{context}",
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
                "entries": {},
                "mountpoint": None,
                "gitreview": origins.CasGitreviewCache(
                    directory, args.gitreview_mode, args.gitreview_nearest
                )
                if not use_fuse and args.origin
                else None,
            }
            trees[tree_key] = tree
            stats["trees"] += 1

        if option_set is not None:
            key = recipe if args.strip_junctions else label
            existing = tree["entries"].get(key)
            if existing is not None:
                # Sets load one after another, so a repeat of the latest set
                # is a second junction instance within the same load.
                if existing["option_sets"][-1] is option_set:
                    stats["duplicate_elements"] += 1
                else:
                    existing["option_sets"].append(option_set)
                continue
            entry = {"element": element, "label": label, "recipe": recipe}
            entry["option_sets"] = [option_set]
            tree["entries"][key] = entry
            tree["elements"].append(entry)
            continue

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
