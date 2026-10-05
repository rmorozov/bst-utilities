"""Attribute files to source metadata and enrich origins from cached .gitreview files."""

from __future__ import annotations

import argparse
import os

from . import adapter, paths, source_cache


def describe_source_object(src, index):
    kind = adapter.bst_value(
        src,
        (
            "kind",
            "source_kind",
            "_kind",
            "plugin_kind",
            "_plugin_kind",
        ),
    )

    url = adapter.bst_value(
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

    ref = adapter.bst_value(
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

    track = adapter.bst_value(
        src,
        (
            "track",
            "_track",
            "track_ref",
        ),
    )

    directory = adapter.bst_value(
        src,
        (
            "directory",
            "_directory",
            "subdir",
            "target_directory",
        ),
    )

    gerrit_project = adapter.bst_value(
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
        kind = adapter.bst_get(raw, "kind") or kind
        url = adapter.bst_get(raw, "url") or adapter.bst_get(raw, "path") or url
        ref = adapter.bst_get(raw, "ref") or ref
        track = adapter.bst_get(raw, "track") or track
        directory = adapter.bst_get(raw, "directory") or directory
        gerrit_project = adapter.bst_get(raw, "gerrit_project") or gerrit_project

    if ref is None and track is not None:
        ref = track

    if kind is None:
        kind = type(src).__name__

    directory = paths.normalize_relative_dir(directory)

    label = str(kind) if kind else "source"

    if url:
        label += f" {url}"

    if ref:
        label += f" #{ref}"

    if directory:
        label += f" directory={directory}"

    if gerrit_project:
        label += f" gerrit_project={gerrit_project}"

    gerrit_project_effective = paths._normalize_gerrit_project(gerrit_project)

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
    accessor = source_cache.source_accessor(element)
    infos = []

    for i, src in enumerate(adapter.get_source_objects(element, accessor), 1):
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
        "project_normalized": paths._normalize_gerrit_project(project),
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
            origin_obj["gerrit_project_match"] = paths._normalize_gerrit_project(
                source_gerrit_project
            ) == paths._normalize_gerrit_project(gitreview_project)

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
