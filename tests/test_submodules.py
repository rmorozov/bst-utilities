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
        (["a**/*.c"], [], False),
        ([], ["a**/*.c"], False),
        (["x/**y"], [], False),
        ([], ["!**a"], False),
        (["**", "**/b.c", "a/**/c.c", "**/a/**"], ["a/**"], True),
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
        "a.c",
        "ab.c",
        "a/b.c",
        "a/b/c.c",
        "x/y.c",
    ]
    for rel in tree:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x\n")
    includes = [[], ["*.c"], ["src"], ["d007/**"], ["b/**"], ["**/x/*"], ["a/*"], ["*.[ch]"]]
    includes += [["src/*"], ["[!a]*.h"], ["?.c"], ["*.c", "*.h"], ["a"], ["**/*.h"]]
    includes += [["**"], ["**/b.c"], ["a/**/c.c"], ["a/**"], ["**/a/**"]]
    excludes = [[], ["a"], ["*.c"], ["d007"], ["src/**"], ["x"], ["**/x"], ["!*.h"]]
    excludes += [["**/b.c"], ["a/**/c.c"], ["a/**"], ["**/a/**"]]
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


def test_blob_matches_stream_through_the_spool_one_record_at_a_time(monkeypatch):
    import json

    from bst_utilities._source_grep import search_blobs

    h = "ab" + "0" * 62
    lines = [json.dumps({"type": "begin", "data": {"path": {"text": f"./ab/{h}"}}})]
    count = 50000
    for n in range(count):
        data = {"path": {"text": f"./ab/{h}"}, "lines": {"text": f"x{n}\n"}, "line_number": n + 1}
        lines.append(json.dumps({"type": "match", "data": data}))
    lines.append(json.dumps({"type": "end", "data": {}}))
    hits = search_blobs.BlobHits(files_mode=False)
    sizes = []
    original = json.dumps

    def watched(obj, *args, **kwargs):
        text = original(obj, *args, **kwargs)
        sizes.append(len(text))
        return text

    monkeypatch.setattr(search_blobs.json, "dumps", watched)
    try:
        hits.record_matches(iter(lines))
        # Each record is serialized on its own, never the whole blob's list.
        assert max(sizes) < 100
        # Replay at two paths interleaved: each read is one record, from its own offset.
        first, second = hits.iter_matches(h), hits.iter_matches(h)
        for n in range(count):
            assert next(first) == (n + 1, f"x{n}")
            assert next(second) == (n + 1, f"x{n}")
        assert next(first, None) is None
    finally:
        hits.close()


def test_every_started_group_is_stopped_when_emission_fails(tmp_path, monkeypatch):
    """A closed output while one group is emitted must also stop the next group's rg."""
    import types

    from bst_utilities._source_grep import search_blobs

    procs = []

    class Proc:
        def __init__(self, *args, **kwargs):
            self.returncode = None
            procs.append(self)

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    stopped = []
    monkeypatch.setattr(search_blobs.subprocess, "Popen", Proc)

    def stop(proc):
        stopped.append(proc)
        proc.returncode = -15

    monkeypatch.setattr(search_blobs.processes, "stop_process", stop)
    monkeypatch.setattr(search_blobs, "GROUP_BLOBS", 1)
    monkeypatch.setattr(search_blobs, "farm_base", lambda: str(tmp_path))
    blobs = {f"t{i}": [(f"f{i}.c", f"{i:02d}" + "0" * 62)] for i in range(3)}
    monkeypatch.setattr(
        search_blobs,
        "tree_entries",
        lambda tree, cas_dir, matcher, stats: blobs[tree["digest"]],
    )

    def finish(self, hits):
        self.proc.wait()
        hits.hits[blobs["t0"][0][1]] = True
        self.close()

    monkeypatch.setattr(search_blobs.GroupSearch, "finish", finish)

    class Emitter:
        out = types.SimpleNamespace(flush=lambda: None)

        def emit_file(self, *args):
            raise BrokenPipeError

    trees = {
        name: {"digest": name, "elements": [{"label": f"{name}.bst", "element": None}]}
        for name in blobs
    }
    args = types.SimpleNamespace(
        glob=[],
        exclude=[],
        files_with_matches=True,
        ignore_case=False,
        fixed_string=False,
        binary_files="skip",
        pattern="x",
        jobs=1,
        origin=False,
    )
    stats = {k: 0 for k in ("rg_processes", "searched_blobs", "searched_files", "results")}
    with pytest.raises(BrokenPipeError):
        search_blobs.search_blobs(trees, args, stats, str(tmp_path), Emitter())
    # The first group finished; the second, started while it was emitted, was stopped.
    assert len(procs) == 2
    assert stopped == [procs[1]]
    assert all(proc.returncode is not None for proc in procs)
    assert list(tmp_path.iterdir()) == []
