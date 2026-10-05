"""Offline end-to-end test using an isolated BuildStream configuration/cache."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.integration
def test_local_project_fetch_find_and_cache(tmp_path):
    pytest.importorskip("buildstream")
    if shutil.which("bst") is None:
        pytest.skip("BuildStream CLI is not installed")
    try:
        with socket.socket(socket.AF_UNIX):
            pass
    except PermissionError:
        pytest.skip("environment disallows Unix sockets required by BuildStream casd")
    project = tmp_path / "project"
    (project / "elements").mkdir(parents=True)
    (project / "sources" / "private").mkdir(parents=True)
    (project / "sources" / "hello.txt").write_text("hello world\n")
    (project / "sources" / "private" / "secret.txt").write_text("private\n")
    (project / "project.conf").write_text("name: smoke\nmin-version: 2.8\nelement-path: elements\n")
    (project / "elements" / "test.bst").write_text(
        "kind: import\nsources:\n- kind: local\n  path: sources\n"
    )
    (project / "elements" / "empty.bst").write_text("kind: stack\n")
    config = tmp_path / "buildstream.conf"
    config.write_text(f"cachedir: {tmp_path / 'bst-cache'}\nbuild:\n  max-jobs: 2\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    base = [sys.executable, "-m", "bst_utilities.source_grep", "--config", str(config)]

    def search(*args):
        return subprocess.run(
            base + list(args), cwd=project, env=env, capture_output=True, text=True, timeout=60
        )

    uncached = search("test.bst", "--find", "*.txt")
    assert uncached.returncode == 2, uncached.stderr
    assert "source tree is not cached" in uncached.stderr, uncached.stderr
    fetch = subprocess.run(
        ["bst", "--config", str(config), "--no-interactive", "source", "fetch", "test.bst"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert fetch.returncode == 0, fetch.stderr
    options = [
        "test.bst",
        "--find",
        "*.txt",
        "--exclude",
        "private",
        "--json",
        "--stats",
        "--path-cache-dir",
        str(tmp_path / "index"),
    ]
    for attempt in range(2):
        result = search(*options)
        assert result.returncode == 0, result.stderr
        assert [json.loads(line)["path"] for line in result.stdout.splitlines()] == ["hello.txt"]
        if attempt == 1:
            assert re.search(r"path cache hits:\s+1\b", result.stderr), result.stderr
    missing = search("test.bst", "--find", "*.missing", "--no-path-cache")
    assert missing.returncode == 1, missing.stderr
    empty = search("empty.bst", "--find", "*")
    assert empty.returncode == 1, empty.stderr


@pytest.fixture
def review_project(tmp_path):
    pytest.importorskip("buildstream")
    try:
        with socket.socket(socket.AF_UNIX):
            pass
    except PermissionError:
        pytest.skip("environment disallows Unix sockets required by BuildStream casd")
    if shutil.which("bst") is None:
        pytest.skip("BuildStream CLI is not installed")
    project = tmp_path / "review-project"
    (project / "elements").mkdir(parents=True)
    for directory in ["adir", "bdir"]:
        (project / directory).mkdir()
        (project / directory / "file.txt").write_text(f"{directory}\n")
    (project / "adir" / "onlyx.variant").write_text("x")
    (project / "bdir" / "onlyy.variant").write_text("y")
    (project / "adir" / ".gitreview").write_bytes(b"[gerrit]\nproject=caf\xe9/project\n")
    (project / "project.conf").write_text("""name: review
min-version: 2.8
element-path: elements
options:
  flavour:
    type: enum
    description: source variant
    values: [x, y]
    default: x
""")
    (project / "elements" / "multi.bst").write_text("""kind: import
sources:
- kind: local
  path: adir
  directory: adir
- kind: local
  path: bdir
  directory: bdir
""")
    (project / "elements" / "opt.bst").write_text("""kind: import
(?):
- flavour == "x":
    sources:
    - kind: local
      path: adir
- flavour == "y":
    sources:
    - kind: local
      path: bdir
""")
    (project / "elements" / "noref.bst").write_text("""kind: import
sources:
- kind: remote
  url: https://example.invalid/source.txt
""")
    config = tmp_path / "buildstream.conf"
    config.write_text(f"cachedir: {tmp_path / 'bst-cache'}\nbuild:\n  max-jobs: 2\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    base = [
        sys.executable,
        "-m",
        "bst_utilities.source_grep",
        "--config",
        str(config),
        "-C",
        str(project),
    ]

    def search(*args):
        return subprocess.run(
            base + list(args), cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
        )

    def fetch(target, *opts):
        result = subprocess.run(
            [
                "bst",
                "-C",
                str(project),
                "--config",
                str(config),
                "--no-interactive",
                *opts,
                "source",
                "fetch",
                target,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

    return SimpleNamespace(
        project=project, tmp_path=tmp_path, env=env, base=base, search=search, fetch=fetch
    )


@pytest.mark.integration
def test_options_unresolved_and_non_utf8_metadata(review_project):
    p = review_project
    p.fetch("opt.bst")
    p.fetch("opt.bst", "-o", "flavour", "y")
    for option in ["x", "y"]:
        result = p.search(
            "-o", "flavour", option, "opt.bst", "--find", "*.variant", "--json", "--no-path-cache"
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["path"] == f"only{option}.variant"
    p.fetch("multi.bst")
    result = p.search(
        "multi.bst",
        "--find",
        "*.txt",
        "--origin",
        "--gitreview-nearest",
        "--json",
        "--no-path-cache",
    )
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 2
    unresolved = p.search("noref.bst", "--find", "*")
    assert unresolved.returncode == 2
    assert "source refs are unresolved" in unresolved.stderr
    assert "source track" in unresolved.stderr
    assert "NoneType" not in unresolved.stderr


@pytest.mark.integration
def test_fuse_slash_globs_statistics_and_closed_pipe(review_project):
    p = review_project
    if not os.path.exists("/dev/fuse"):
        if os.environ.get("BST_UTILITIES_REQUIRE_FUSE") == "1":
            pytest.fail("FUSE integration is required but /dev/fuse is missing")
        pytest.skip("/dev/fuse is unavailable")
    p.fetch("multi.bst")
    mounts = p.tmp_path / "mounts"
    for suffix in [[], ["-l"]]:
        result = p.search(
            "multi.bst",
            "^bdir$",
            "--glob",
            "bdir/**",
            "--json",
            "--mount-dir",
            str(mounts),
            *suffix,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["path"] == "bdir/file.txt"
        excluded = p.search(
            "multi.bst", "^bdir$", "--exclude", "bdir/**", "--mount-dir", str(mounts), *suffix
        )
        assert excluded.returncode == 1, excluded.stderr
    invalid = p.search("multi.bst", "[", "--stats", "--mount-dir", str(mounts))
    assert invalid.returncode == 2
    assert re.search(r"search errors:\s+1\b", invalid.stderr), invalid.stderr
    assert not list(mounts.iterdir())
    # Large output ensures the producer observes a closed consumer pipe.
    (p.project / "adir" / "many.txt").write_text("match\n" * 30000)
    p.fetch("multi.bst")
    proc = subprocess.Popen(
        p.base + ["multi.bst", "^match$", "--mount-dir", str(mounts)],
        env=p.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.stdout.readline()
    proc.stdout.close()
    assert proc.wait(timeout=60) == 141
    assert not proc.stderr.read(), "closed pipe should produce no bogus search diagnostics"
    assert not list(mounts.iterdir())
