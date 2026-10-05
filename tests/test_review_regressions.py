import io
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from bst_utilities import source_grep as sg


def test_project_options_and_directory_forwarded(tmp_path):
    captured = {}

    def project(directory, context, **kwargs):
        captured.update(directory=directory, **kwargs)
        return object()

    args = sg.build_parser().parse_args(
        [
            "-C",
            str(tmp_path),
            "-o",
            "arch",
            "aarch64",
            "-o",
            "arch",
            "x86_64",
            "x.bst",
            "--find",
            "*",
        ]
    )
    sg.create_project(project, object(), args)
    assert captured["directory"] == str(tmp_path)
    assert captured["cli_options"] == [("arch", "x86_64")]


def test_unresolved_sources_do_not_query_cache():
    class Sources:
        _sources = [object()]

        def get_files(self):
            raise AssertionError("must not get unresolved source files")

        def update_resolved_state(self):
            pass

        def is_resolved(self):
            return False

    element = SimpleNamespace(
        _sources=Sources(), _query_source_cache=lambda: pytest.fail("cache query")
    )
    directory, status, detail = sg.load_source_directory(element)
    assert directory is None
    assert status == "unresolved"
    assert "source track" in detail


@pytest.mark.parametrize("unavailable", [False, True])
def test_cas_gitreview_enrichment_never_aborts_search(unavailable):
    class Directory:
        def isfile(self, *args, **kwargs):
            return True

        @contextmanager
        def open_file(self, path, mode):
            assert mode == "rb"
            if unavailable:
                raise OSError("missing metadata blob")
            yield io.BytesIO(b"[gerrit]\nproject=caf\xe9/proj\n")

    cache = sg.CasGitreviewCache(Directory(), "always", False)
    result = cache.for_path("hello.txt")
    if unavailable:
        assert result is None
    else:
        assert result["project"] == "caf\ufffd/proj"


@pytest.mark.parametrize("mode", ["find", "content", "files"])
def test_slash_globs_resolve_against_tree_root(tmp_path, mode):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is required")
    for dirname in ["adir", "bdir"]:
        (tmp_path / dirname).mkdir()
        (tmp_path / dirname / "file.txt").write_text("match\n")
    argv = ["x.bst", "--find", "*.txt"] if mode == "find" else ["x.bst", "^match$"]
    if mode == "files":
        argv.append("-l")
    args = sg.build_parser().parse_args([*argv, "--glob", "bdir/**"])
    assert [r[1] for r in sg.iter_mounted_matches(args, str(tmp_path))] == ["bdir/file.txt"]
    args = sg.build_parser().parse_args([*argv, "--exclude", "bdir/**"])
    assert [r[1] for r in sg.iter_mounted_matches(args, str(tmp_path))] == ["adir/file.txt"]


def test_cleanup_reaps_all_children_even_if_unmount_and_rmdir_fail(tmp_path, monkeypatch, capsys):
    manager = sg.FuseMountManager("fuse", "cas", str(tmp_path), "SHA256", False, False)
    children = []
    for i in range(2):
        path = tmp_path / str(i)
        path.mkdir()
        if i == 0:
            (path / "leftover").touch()  # rmdir fails on first entry
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        children.append(proc)
        manager.mounts[str(i)] = (str(path), proc, True)
    monkeypatch.setattr(sg, "unmount_mountpoint", lambda path: False)
    monkeypatch.setattr(sg.os.path, "ismount", lambda path: False)
    try:
        manager.cleanup()
        assert all(p.poll() is not None for p in children)
        assert not (tmp_path / "1").exists()
        assert "cleanup failed" in capsys.readouterr().err
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_dedup_does_not_retain_normal_output():
    dedup = sg.RecordDeduplicator(False)
    for i in range(10000):
        assert not dedup.duplicate("match", "x.bst", "file.txt", i)
    assert dedup.seen is None
    dedup = sg.RecordDeduplicator(True)
    assert not dedup.duplicate("match", "x.bst", "file.txt", 1)
    assert dedup.duplicate("match", "x.bst", "file.txt", 1)


def test_emission_errors_are_not_traversal_errors():
    stats = {"traversal_errors": 0}
    paths = sg.iter_checked_paths(iter(["a.txt"]), {"digest": "123"}, stats)
    try:
        with pytest.raises(BrokenPipeError):
            for path in paths:
                raise BrokenPipeError
    finally:
        paths.close()
    assert stats["traversal_errors"] == 0


def test_closed_pipe_is_quiet_and_stops_work(tmp_path):
    # Exercise actual main/flush/pipe shutdown in a child, without BuildStream sockets.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    code = """
import sys
from bst_utilities import source_grep as sg
def run():
    out = sg.LineBuffer(limit=1)
    try:
        for i in range(100000):
            out.emit('match' * 1000)
    finally:
        print('cleanup ran', file=sys.stderr)
sg._main = run
raise SystemExit(sg.main())
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    proc.stdout.readline()
    proc.stdout.close()
    assert proc.wait(timeout=10) == 141
    assert proc.stderr.read().decode() == "cleanup ran\n"
