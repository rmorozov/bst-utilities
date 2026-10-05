"""Offline end-to-end test using an isolated BuildStream configuration/cache."""

import hashlib
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


@pytest.mark.integration
def test_fuse_mounts_one_tree_at_a_time(review_project):
    p = review_project
    if not os.path.exists("/dev/fuse"):
        if os.environ.get("BST_UTILITIES_REQUIRE_FUSE") == "1":
            pytest.fail("FUSE integration is required but /dev/fuse is missing")
        pytest.skip("/dev/fuse is unavailable")
    elements = p.project / "elements"
    for name in ["adir", "bdir"]:
        (elements / f"{name}.bst").write_text(
            f"kind: import\nsources:\n- kind: local\n  path: {name}\n"
        )
    (elements / "trees.bst").write_text("kind: stack\ndepends:\n- adir.bst\n- bdir.bst\n")
    p.fetch("adir.bst")
    p.fetch("bdir.bst")
    mounts = p.tmp_path / "mounts"
    result = p.search("trees.bst", "-n", "dir$", "--json", "--stats", "--mount-dir", str(mounts))
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert sorted((r["element"], r["path"], r["line"]) for r in records) == [
        ("adir.bst", "file.txt", 1),
        ("bdir.bst", "file.txt", 1),
    ]
    assert re.search(r"peak mounts:\s+1\b", result.stderr), result.stderr
    assert re.search(r"fuse processes:\s+2\b", result.stderr), result.stderr
    assert re.search(r"rg processes:\s+2\b", result.stderr), result.stderr
    assert not list(mounts.iterdir())


@pytest.mark.integration
def test_benchmark_harness_smoke(tmp_path):
    pytest.importorskip("buildstream")
    if shutil.which("bst") is None:
        pytest.skip("BuildStream CLI is not installed")
    try:
        with socket.socket(socket.AF_UNIX):
            pass
    except PermissionError:
        pytest.skip("environment disallows Unix sockets required by BuildStream casd")
    report_path = tmp_path / "bench.json"
    script = Path(__file__).resolve().parents[1] / "scripts" / "bench_source_grep.py"
    result = subprocess.run(
        [sys.executable, str(script), "--scale", "tiny", "--repeats", "1"]
        + ["--json-out", str(report_path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["environment"]["buildstream"] != "unavailable"
    by_name = {entry["scenario"]: entry for entry in report["results"]}
    fresh = by_name["find-fresh-index"]["runs"][0]
    assert fresh["results"] == 100 and fresh["path cache misses"] == 1
    assert by_name["find-warm-index"]["runs"][0]["path cache hits"] == 1
    if os.environ.get("BST_UTILITIES_REQUIRE_FUSE") == "1":
        trees = by_name["content-unique-trees"]["runs"][0]
        assert trees["peak mounts"] == 1 and trees["rg processes"] == 5
        assert by_name["content-duplicate-trees"]["runs"][0]["fuse processes"] == 1
    assert "| find-warm-index |" in result.stdout


@pytest.mark.integration
def test_junctions_nested_fetch_strip_and_targets(tmp_path):
    pytest.importorskip("buildstream")
    if shutil.which("bst") is None:
        pytest.skip("BuildStream CLI is not installed")
    try:
        with socket.socket(socket.AF_UNIX):
            pass
    except PermissionError:
        pytest.skip("environment disallows Unix sockets required by BuildStream casd")

    def project(root, name, files, elements):
        (root / "elements").mkdir(parents=True)
        (root / "project.conf").write_text(
            f"name: {name}\nmin-version: 2.0\nelement-path: elements\n"
        )
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        for element, text in elements.items():
            (root / "elements" / element).write_text(text)

    local = "kind: import\nsources:\n- kind: local\n  path: src\n"
    leaf = tmp_path / "leaf"
    project(leaf, "leaf", {"src/leaf.txt": "leaf needle\n"}, {"leaf.bst": local})
    archive = tmp_path / "leaf.tar"
    shutil.make_archive(str(archive.with_suffix("")), "tar", leaf)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    main = tmp_path / "main"
    project(
        main / "sub",
        "sub",
        {"src/lib.txt": "sub needle\n"},
        {
            "lib.bst": local,
            "inner.bst": f"kind: junction\nsources:\n- kind: tar\n  url: file://{archive}\n"
            f"  ref: {digest}\n",
        },
    )
    project(
        main,
        "main",
        {"src/lib.txt": "main needle\n"},
        {
            "lib.bst": local,
            "sub.bst": "kind: junction\nsources:\n- kind: local\n  path: sub\n",
            "alias.bst": "kind: link\nconfig:\n  target: sub.bst:lib.bst\n",
            "app.bst": "kind: stack\ndepends:\n- lib.bst\n- alias.bst\n- sub.bst:inner.bst:leaf.bst\n",
        },
    )
    config = tmp_path / "buildstream.conf"
    config.write_text(f"cachedir: {tmp_path / 'cache'}\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    def search(*args):
        cmd = [sys.executable, "-m", "bst_utilities.source_grep", "--config", str(config)]
        return subprocess.run(
            cmd + ["-C", str(main), *args], env=env, capture_output=True, text=True, timeout=120
        )

    refused = search("app.bst", "--find", "*.txt")
    assert refused.returncode == 2
    assert "Subproject sources are missing for sub.bst" in refused.stderr, refused.stderr
    # Junctions are fetched on request (this used to crash in the scheduler callbacks);
    # element sources stay unfetched, so the search reports them as uncached.
    fetched = search("app.bst", "--find", "*.txt", "--fetch-subprojects")
    assert fetched.returncode == 2, fetched.stderr
    assert "source tree is not cached: sub.bst:inner.bst:leaf.bst" in fetched.stderr
    assert "NoneType" not in fetched.stderr

    subprocess.run(
        ["bst", "-C", str(main), "--config", str(config), "--no-interactive"]
        + ["source", "fetch", "--deps", "all", "app.bst"],
        env=env,
        check=True,
        capture_output=True,
    )
    found = search("app.bst", "--find", "*.txt")
    assert sorted(found.stdout.splitlines()) == [
        "lib.bst:lib.txt",
        "sub.bst:inner.bst:leaf.bst:leaf.txt",
        "sub.bst:lib.bst:lib.txt",
    ], found.stderr
    nested = search("sub.bst:inner.bst:leaf.bst", "--find", "*")
    assert nested.stdout.splitlines() == ["sub.bst:inner.bst:leaf.bst:leaf.txt"]
    if os.path.exists("/dev/fuse"):
        # Stripping collapses names, never distinct content at the same path/line.
        stripped = search(
            "app.bst", "needle", "--strip-junctions", "--mount-dir", str(tmp_path / "m")
        )
        assert sorted(stripped.stdout.splitlines()) == [
            "leaf.bst:leaf.txt:leaf needle",
            "lib.bst:lib.txt:main needle",
            "lib.bst:lib.txt:sub needle",
        ], stripped.stderr
