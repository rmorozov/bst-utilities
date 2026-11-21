"""Find git submodules in cached source trees: where they are and whether they were staged.

A source plugin that checks out submodules (buildstream-plugins `git` does by
default) stages their files into the same cached tree as the superproject, so
they are searched like any other file. The superproject's `.gitmodules` is
staged too, which makes two things cheap: attributing a file to the submodule
it belongs to, and noticing a submodule whose directory the plugin left empty
or absent (`checkout-submodules: false`, a plugin without submodule support,
a submodule never added with `git submodule add`). Only trees whose root, or a
staged submodule's root, holds a `.gitmodules` read more than one Directory.
"""

from __future__ import annotations

import sys

from . import adapter, cas


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def parse_gitmodules(text):
    """[{"name", "path", "url"}] from a .gitmodules file, in file order; path required."""
    modules = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            header = line[1:-1].strip()
            section, _, name = header.partition(" ")
            current = None
            if section.lower() == "submodule":
                current = {"name": _unquote(name), "path": None, "url": None}
                modules.append(current)
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        if key in ("path", "url"):
            current[key] = _unquote(value)
    result = []
    for module in modules:
        path = (module["path"] or "").strip("/")
        if path and ".." not in path.split("/"):
            module["path"] = path
            result.append(module)
    return result


class CasTreeReader:
    """Read single files and directory states of a tree from CAS Directory records."""

    def __init__(self, cas_dir, digest):
        self.cas_dir = cas_dir
        self.digest = digest
        self.dirs = {}

    def _directory(self, rel):
        if rel in self.dirs:
            return self.dirs[rel]
        if not rel:
            message = cas.read_directory(self.cas_dir, self.digest.hash)
        else:
            parent_rel, _, name = rel.rpartition("/")
            parent = self._directory(parent_rel)
            message = None
            if parent is not None:
                for node in parent.directories:
                    if node.name == name:
                        message = cas.read_directory(self.cas_dir, node.digest.hash)
                        break
        self.dirs[rel] = message
        return message

    def read_file(self, rel):
        parent_rel, _, name = rel.rpartition("/")
        parent = self._directory(parent_rel)
        if parent is None:
            return None
        for node in parent.files:
            if node.name == name:
                h = node.digest.hash
                path = f"{self.cas_dir}/objects/{h[:2]}/{h[2:]}"
                try:
                    with open(path, "rb") as f:
                        return f.read()
                except FileNotFoundError:
                    # casd does not store the empty blob.
                    return b"" if node.digest.size_bytes == 0 else None
        return None

    def dir_state(self, rel):
        message = self._directory(rel)
        if message is None:
            return "missing"
        if not (message.files or message.directories or message.symlinks):
            return "empty"
        return "present"


class DirectoryReader:
    """The same through BuildStream's Directory API, when the CAS layout is unknown."""

    def __init__(self, directory):
        self.directory = directory

    def read_file(self, rel):
        if not self.directory.isfile(rel, follow_symlinks=False):
            return None
        with self.directory.open_file(rel, mode="rb") as f:
            return f.read()

    def dir_state(self, rel):
        if not self.directory.isdir(rel, follow_symlinks=False):
            return "missing"
        sub = self.directory.open_directory(rel)
        return "present" if next(iter(sub.list_relative_paths()), None) else "empty"


class SubmoduleIndex:
    """
    Submodules declared by a tree's .gitmodules files, nested ones included.

    Each module: {"name", "path", "url", "state"} with path relative to the
    tree root and state "present", "empty" or "missing". Nested .gitmodules are
    only read inside present submodules, so an unstaged submodule's own
    submodules are not reported separately.
    """

    def __init__(self, reader):
        self.reader = reader
        self._modules = None

    @property
    def modules(self):
        if self._modules is None:
            self._modules = []
            self._discover("")
            # Longest paths first, so for_path() finds the innermost module.
            self._by_depth = sorted(self._modules, key=lambda m: -len(m["path"]))
        return self._modules

    def _discover(self, root):
        data = self.reader.read_file(f"{root}/.gitmodules" if root else ".gitmodules")
        if data is None:
            return
        for module in parse_gitmodules(data.decode("utf-8", "replace")):
            path = f"{root}/{module['path']}" if root else module["path"]
            module = dict(module, path=path, state=self.reader.dir_state(path))
            self._modules.append(module)
            if module["state"] == "present":
                self._discover(path)

    def for_path(self, rel_path):
        """The innermost staged submodule containing rel_path, or None."""
        if not self.modules:
            return None
        for module in self._by_depth:
            if module["state"] == "present" and rel_path.startswith(module["path"] + "/"):
                return module
        return None

    def unstaged(self):
        return [m for m in self.modules if m["state"] != "present"]


class _Unstaged:
    """A --report-broken error whose reason names the kind of gap."""

    def __init__(self, text, reason):
        self.text = text
        self.reason = reason

    def __str__(self):
        return self.text


def make_index(tree, cas_dir):
    digest = tree.get("digest_obj")
    if cas_dir and getattr(digest, "hash", None):
        return SubmoduleIndex(CasTreeReader(cas_dir, digest))
    if tree.get("directory") is not None:
        return SubmoduleIndex(DirectoryReader(tree["directory"]))
    return None


def check_trees(trees, args, stats, cas_dir):
    """
    Attach a SubmoduleIndex to every tree and warn about unstaged submodules.

    An unstaged submodule is a gap in what is searched, not a search error:
    `checkout-submodules: false` is often deliberate, so the exit status stays.
    """
    for tree in trees.values():
        try:
            index = make_index(tree, cas_dir)
            unstaged = index.unstaged() if index is not None else []
        except Exception as exc:
            # Attribution and this check are optional; the search is not.
            print(
                f"WARNING: could not read .gitmodules of tree {tree.get('digest')}: {exc}",
                file=sys.stderr,
            )
            index, unstaged = None, []
        tree["submodules"] = index
        if not unstaged:
            continue
        labels = [entry["label"] for entry in tree["elements"]]
        shown = ", ".join(labels[:3]) + (f" and {len(labels) - 3} more" if len(labels) > 3 else "")
        for module in unstaged:
            stats["unstaged_submodules"] += 1
            where = "is empty" if module["state"] == "empty" else "is missing"
            url = f" ({module['url']})" if module["url"] else ""
            text = (
                f"submodule {module['path']}{url} is declared in .gitmodules but its "
                f"directory {where} in the cached sources, so its files are not searched"
            )
            print(f"WARNING: {shown}: {text}", file=sys.stderr)
            if args.broken is None:
                continue
            error = _Unstaged(text, f"submodule-{module['state']}")
            for entry in tree["elements"]:
                option_sets = entry.get("option_sets")
                if option_sets is None:
                    project = getattr(args, "current_project", None)
                    option_sets = [adapter.effective_options(project) if project else {}]
                for options in option_sets:
                    args.broken.add("submodule", entry["label"], error, options)
