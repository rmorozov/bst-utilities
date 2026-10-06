import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from bst_utilities._source_grep import (
    adapter,
    application,
    cas_layout,
    cli,
    matching,
    mounts,
    origins,
    processes,
    ripgrep,
    source_cache,
)
from bst_utilities._source_grep import (
    cas as cas_backend,
)


def args(*extra):
    return cli.build_parser().parse_args(["test.bst", "--find", "*.txt", *extra])


@pytest.mark.parametrize(
    "path,accepted",
    [
        ("src/a.txt", True),
        ("src/private/a.txt", False),
        ("other/a.txt", False),
        ("src/.git/a.txt", False),
        ("src/a.c", False),
        ("src/x\ny.txt", True),
    ],
)
def test_filters(path, accepted):
    assert (
        matching.make_path_filter(args("--glob", "src/**", "--exclude", "private"))(path)
        == accepted
    )


def test_find_case_insensitive():
    assert matching.make_path_filter(args("-i"))("HELLO.TXT")


@pytest.mark.parametrize(
    "pattern,path", [("**/*.txt", "sub\ndir/a.txt"), ("a?.txt", "ab.txt"), ("[!x]*.txt", "a.txt")]
)
def test_find_globs(pattern, path):
    assert matching.make_find_matcher(pattern)(path)


def test_selection_does_not_retry_internal_typeerror():
    class BrokenStream:
        calls = 0

        def load_selection(self, *args, **kwargs):
            self.calls += 1
            raise TypeError("internal failure")

    stream = BrokenStream()
    with pytest.raises(TypeError, match="internal failure"):
        adapter.call_load_selection(stream, "test.bst", "all")
    assert stream.calls == 1


def test_sources_initialized_before_cache_query():
    calls = []

    class Sources:
        _sources = [object()]

        def update_resolved_state(self):
            calls.append("resolve")

        def is_resolved(self):
            return True

        def get_files(self):
            return "directory"

    class Element:
        _sources = Sources()

        def _query_source_cache(self):
            assert calls == ["resolve"]
            calls.append("query")

        def _cached_sources(self):
            return True

    assert source_cache.load_source_directory(Element()) == ("directory", "ok", None)


def test_cache_concurrent_writers_and_newlines(tmp_path, monkeypatch):
    paths = ["a.txt", "sub/x\ny.txt", "back\\slash.txt"]
    cache = tmp_path / "index.jsonl"
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(
            pool.map(
                lambda _: list(cas_backend.iter_directory_and_cache(iter(paths), cache)), range(8)
            )
        )
    assert result == [paths] * 8
    assert list(cas_backend.iter_path_cache_file(cache)) == paths
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_cache_write_preserves_previous_index(tmp_path, monkeypatch):
    cache = tmp_path / "index.jsonl"
    cache.write_text('"previous.txt"\n')

    def fail():
        yield "partial.txt"
        raise OSError("missing CAS blob")

    with pytest.raises(OSError):
        list(cas_backend.iter_directory_and_cache(fail(), cache))
    assert list(cas_backend.iter_path_cache_file(cache)) == ["previous.txt"]
    assert not list(tmp_path.glob("*.tmp"))


def test_rg_stderr_cannot_block_stdout():
    cmd = [sys.executable, "-c", "import sys; sys.stderr.write('x'*200000); print('match')"]
    assert list(processes.iter_rg_output(cmd)) == ["match\n"]


def test_rg_error_reports_failure():
    with pytest.raises(RuntimeError, match="bad pattern"):
        list(
            processes.iter_rg_output(
                [sys.executable, "-c", "import sys; sys.stderr.write('bad pattern'); sys.exit(2)"]
            )
        )


def test_rg_child_reaped_on_interrupt():
    with pytest.raises(KeyboardInterrupt):
        with processes.rg_process([sys.executable, "-c", "import time; time.sleep(30)"]) as proc:
            raise KeyboardInterrupt
    assert proc.poll() is not None


def test_rg_null_paths_and_byte_json():
    cmd = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'sub/x\\ny.txt\\0')"]
    assert list(processes.iter_rg_output(cmd, null=True)) == ["sub/x\ny.txt"]
    assert ripgrep.rg_json_text({"bytes": base64.b64encode(b"x\xff").decode()}) == "x\udcff"


class BlobStore:
    """File-backed CAS blobs used by the real BuildStream Directory implementation."""

    def __init__(self, root):
        self.root = root
        self.tmpdir = str(root)

    def objpath(self, digest):
        # Same layout as a BuildStream CAS directory, so iter_cas_files can read it.
        path = self.root / "objects" / digest.hash[:2] / digest.hash[2:]
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    def add_object(self, *, buffer=None, path=None):
        from buildstream._protos.build.bazel.remote.execution.v2 import remote_execution_pb2

        data = buffer if buffer is not None else Path(path).read_bytes()
        digest = remote_execution_pb2.Digest(
            hash=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
        )
        Path(self.objpath(digest)).write_bytes(data)
        return digest


def test_real_buildstream_cas_paths_digest_and_gitreview(tmp_path):
    pytest.importorskip("buildstream")
    from buildstream.storage._casbaseddirectory import CasBasedDirectory

    cas = BlobStore(tmp_path)
    directory = CasBasedDirectory(cas)
    child = directory.open_directory("nested.txt", create=True)
    with child.open_file("hello\nworld.txt", mode="w") as f:
        f.write("hello\n")
    with directory.open_file(".gitreview", mode="w") as f:
        f.write("[gerrit]\nhost=gerrit.example.org\nproject=example/project.git\n")
    digest = cas_layout.get_cas_directory_digest(directory)
    assert cas_layout.digest_to_fuse_value(digest) == f"{digest.hash}/{digest.size_bytes}"
    # Deserialize the tree again, exercising recursive blob traversal.
    reopened = CasBasedDirectory(cas, digest=digest)
    assert set(cas_backend.iter_relative_paths(reopened)) == {
        ".gitreview",
        "nested.txt/hello\nworld.txt",
    }
    assert (
        origins.CasGitreviewCache(reopened, "always", True).for_path("nested.txt/hello\nworld.txt")[
            "project"
        ]
        == "example/project.git"
    )


@pytest.mark.parametrize(
    "arguments",
    [[], ["x.bst"], ["x.bst", "pattern", "--find", "*"], ["x.bst", "p", "--backend", "cas"]],
)
def test_invalid_cli(arguments, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bst-source-grep", *arguments])
    with pytest.raises(SystemExit) as exc:
        application.main()
    assert exc.value.code == 2


def test_project_fetch_subprojects_uses_callback(monkeypatch):
    class Project:
        def __init__(self, cwd, context, *, cli_options, fetch_subprojects):
            self.fetch = fetch_subprojects

    project = adapter.create_project(Project, object(), args())
    with pytest.raises(RuntimeError, match="Subproject sources are missing"):
        project.fetch(["junction"])
    fetched = []
    project = adapter.create_project(Project, object(), args("--fetch-subprojects"), fetched.extend)
    project.fetch(["junction"])
    assert fetched == ["junction"]


def test_mounts_are_not_shared_between_runs(tmp_path, monkeypatch):
    class Process:
        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(os.path, "ismount", lambda path: True)
    managers = [
        mounts.FuseMountManager("fuse", "cas", str(tmp_path), "SHA256", True, False)
        for _ in range(2)
    ]
    paths = [m.ensure_mount("abcdef/42") for m in managers]
    assert paths[0] != paths[1]
    assert managers[0].ensure_mount("abcdef/42") == paths[0]


def test_umount_fallback_has_valid_arguments(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: name if name == "umount" else None)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: calls.append(cmd))
    assert mounts.unmount_mountpoint("/tmp/mount")
    assert calls == [["umount", "/tmp/mount"]]


@pytest.mark.parametrize(
    "status,paths,expected",
    [("nosources", [], 1), ("uncached", [], 2), ("ok", ["hello.txt"], 0), ("ok", ["hello.c"], 1)],
)
def test_main_exit_statuses(monkeypatch, tmp_path, capsys, status, paths, expected):
    pytest.importorskip("buildstream")
    import buildstream._context
    import buildstream._project
    import buildstream._stream

    class Context:
        def load(self, config):
            pass

        def close(self):
            pass

    class Stream:
        def __init__(self, ctx, start, **callbacks):
            pass

        def init(self):
            pass

        def fetch_subprojects(self, junctions):
            raise AssertionError("must remain offline")

        def set_project(self, project):
            pass

        def load_selection(self, *args, **kwargs):
            return [SimpleNamespace(name="test.bst")]

        def cleanup(self):
            pass

    monkeypatch.setattr(buildstream._context, "Context", Context)
    monkeypatch.setattr(buildstream._project, "Project", lambda *a, **kw: object())
    monkeypatch.setattr(buildstream._stream, "Stream", Stream)
    monkeypatch.setattr(
        source_cache,
        "load_source_directory",
        lambda el: (object() if status == "ok" else None, status, None),
    )
    monkeypatch.setattr(cas_layout, "get_cas_directory_digest", lambda d: ("abcdef", 42))
    monkeypatch.setattr(cas_backend, "iter_relative_paths", lambda d: iter(paths))
    monkeypatch.setattr(
        sys, "argv", ["bst-source-grep", "test.bst", "--find", "*.txt", "--json", "--no-path-cache"]
    )
    assert application.main() == expected
    output = capsys.readouterr()
    if expected == 0:
        assert json.loads(output.out)["path"] == "hello.txt"
    else:
        assert output.out == ""


def test_direct_cas_walk_matches_buildstream_listing(tmp_path):
    pytest.importorskip("buildstream")
    from buildstream.storage._casbaseddirectory import CasBasedDirectory

    cas = BlobStore(tmp_path)
    directory = CasBasedDirectory(cas)
    for rel in ["z.c", "a.c", "b/y.c", "b/c/x.c", "b/a.h", "B/upper.c", "e/empty/.keep"]:
        parent, _, name = rel.rpartition("/")
        target = directory.open_directory(parent, create=True) if parent else directory
        with target.open_file(name, mode="w") as f:
            f.write(rel)
    directory.open_directory("only-dirs/nested", create=True)
    digest = cas_layout.get_cas_directory_digest(directory)
    expected = list(cas_backend.iter_relative_paths(CasBasedDirectory(cas, digest=digest)))
    assert list(cas_backend.iter_cas_files(str(tmp_path), digest)) == expected
    assert expected[:2] == ["a.c", "z.c"]
