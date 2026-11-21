"""Submodule discovery, attribution and the content-glob subset searched from blobs."""

import argparse
import shutil
import subprocess

import pytest

from bst_utilities._source_grep import broken, matching, origins, submodules


class FakeReader:
    """A tree as {path: bytes} for files and {path/: None} for empty directories."""

    def __init__(self, files, empty_dirs=()):
        self.files = files
        self.empty_dirs = set(empty_dirs)

    def read_file(self, rel):
        return self.files.get(rel)

    def dir_state(self, rel):
        if rel in self.empty_dirs:
            return "empty"
        if any(path.startswith(rel + "/") for path in self.files):
            return "present"
        return "missing"


GITMODULES = b"""# comment
[submodule "lib"]
\tpath = third_party/lib
\turl = https://example.org/lib.git
[core]
\tpath = not-a-module
[submodule "off"]
\tpath = third_party/off
\turl = "https://example.org/off.git"
[submodule "gone"]
\tpath = gone/
[submodule "escape"]
\tpath = ../outside
"""


def test_parse_gitmodules_keeps_only_submodule_sections_with_safe_paths():
    modules = submodules.parse_gitmodules(GITMODULES.decode())
    assert [(m["name"], m["path"], m["url"]) for m in modules] == [
        ("lib", "third_party/lib", "https://example.org/lib.git"),
        ("off", "third_party/off", "https://example.org/off.git"),
        ("gone", "gone", None),
    ]


def test_index_finds_nested_modules_and_unstaged_ones():
    reader = FakeReader(
        {
            ".gitmodules": GITMODULES,
            "third_party/lib/a.c": b"x",
            "third_party/lib/.gitmodules": b'[submodule "deep"]\npath = deep\nurl = ../deep\n',
            "third_party/lib/deep/b.c": b"y",
        },
        empty_dirs={"third_party/off"},
    )
    index = submodules.SubmoduleIndex(reader)
    assert [(m["path"], m["state"]) for m in index.modules] == [
        ("third_party/lib", "present"),
        ("third_party/lib/deep", "present"),
        ("third_party/off", "empty"),
        ("gone", "missing"),
    ]
    assert index.for_path("third_party/lib/deep/b.c")["path"] == "third_party/lib/deep"
    assert index.for_path("third_party/lib/a.c")["path"] == "third_party/lib"
    assert index.for_path("third_party/libx/a.c") is None
    assert index.for_path("main.c") is None
    assert [m["path"] for m in index.unstaged()] == ["third_party/off", "gone"]


def test_tree_without_gitmodules_reads_one_file():
    reads = []

    class Counting(FakeReader):
        def read_file(self, rel):
            reads.append(rel)
            return super().read_file(rel)

    index = submodules.SubmoduleIndex(Counting({"a.c": b""}))
    assert index.unstaged() == [] and index.for_path("a.c") is None
    assert reads == [".gitmodules"]


def test_check_trees_warns_and_reports_without_failing(capsys):
    class Index(submodules.SubmoduleIndex):
        pass

    tree = {"digest": "d/1", "elements": [{"label": "a.bst"}, {"label": "b.bst"}]}
    reader = FakeReader(
        {".gitmodules": GITMODULES, "third_party/lib/a.c": b""}, {"third_party/off"}
    )
    args = argparse.Namespace(broken=broken.BrokenReport(), current_project=None)
    stats = {"unstaged_submodules": 0}
    original = submodules.make_index
    submodules.make_index = lambda tree, cas_dir: Index(reader)
    try:
        submodules.check_trees({"d/1": tree}, args, stats, None)
    finally:
        submodules.make_index = original
    assert stats["unstaged_submodules"] == 2
    err = capsys.readouterr().err
    assert "WARNING: a.bst, b.bst: submodule third_party/off (https://example.org/off.git)" in err
    assert "directory is empty" in err and "directory is missing" in err
    assert len(args.broken) == 4
    assert tree["submodules"].for_path("third_party/lib/a.c")["name"] == "lib"


def test_gitreview_of_a_submodule_file_comes_from_its_own_root():
    class Cache(origins.MountedGitreviewCache):
        def __init__(self, files, nearest):
            super().__init__("", "auto", nearest)
            self.files = files

        def _read_dir(self, rel_dir):
            text = self.files.get(rel_dir)
            return origins.parse_gitreview_text(text) if text else None

    files = {"": "[gerrit]\nproject=top\n", "sub": "[gerrit]\nproject=sub\n"}
    for nearest in (False, True):
        cache = Cache(files, nearest)
        assert cache.for_path("main.c")["project"] == "top"
        assert cache.for_path("sub/x/y.c", "sub")["project"] == "sub"
        # A submodule without its own .gitreview is not the superproject's.
        assert cache.for_path("nested/z.c", "nested") is None


def test_git_source_url_and_ref_come_from_its_mirror():
    class Mirror:
        url = "https://example.org/top.git"
        ref = "0123abcd"

    class GitSource:
        mirror = Mirror()
        tracking = "main"

    info = origins.describe_source_object(GitSource(), 1)
    assert (info["url"], info["ref"], info["track"]) == (Mirror.url, Mirror.ref, "main")


def content_args(globs=(), excludes=()):
    return argparse.Namespace(glob=list(globs), exclude=list(excludes))


@pytest.mark.parametrize(
    "globs,excludes,supported",
    [
        ([], [], True),
        (["*.c", "src/**", "[!a]?.h"], ["!build/**", "*.o"], True),
        (["*.{c,h}"], [], False),
        (["!*.c"], [], False),
        (["\\*.c"], [], False),
        (["/top.c"], [], False),
        (["./top.c"], [], False),
        ([], ["build/"], False),
    ],
)
def test_blob_search_supported(globs, excludes, supported):
    assert matching.blob_search_supported(content_args(globs, excludes)) is supported


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg is not installed")
def test_content_filter_agrees_with_ripgrep(tmp_path):
    tree = [
        "a/src.c/inner.h",
        "a/x.c",
        "a/.hidden.c",
        "a/.git/config",
        "b/d007/x/y.h",
        "b/z.c",
        "src/m.c",
        "top.h",
        ".git",
    ]
    for rel in tree:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x\n")
    includes = [[], ["*.c"], ["src"], ["d007/**"], ["b/**"], ["**/x/*"], ["a/*"], ["*.[ch]"]]
    includes += [["src/*"], ["[!a]*.h"], ["?.c"], ["*.c", "*.h"], ["a"], ["**/*.h"]]
    excludes = [[], ["a"], ["*.c"], ["d007"], ["src/**"], ["x"], ["**/x"], ["!*.h"]]
    for inc in includes:
        for exc in excludes:
            args = content_args(inc, exc)
            assert matching.blob_search_supported(args)
            accepted = matching.make_content_filter(args)
            cmd = ["rg", "--files", "--hidden", "--no-ignore"]
            for g in inc:
                cmd += ["--glob", g]
            for g in exc:
                cmd += ["--glob", g if g.startswith("!") else f"!{g}"]
            cmd += ["--glob", "!**/.git/**", "."]
            listed = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True).stdout
            expected = sorted(p.removeprefix("./") for p in listed.split())
            assert sorted(p for p in tree if accepted(p)) == expected, (inc, exc)
