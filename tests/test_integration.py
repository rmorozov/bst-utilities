"""Offline end-to-end test using an isolated BuildStream configuration/cache."""

import hashlib
import json
import os
import re
import shutil
import select
import socket
import subprocess
import sys
import time
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
def test_fuse_mounts_bounded_by_jobs_with_ordered_output(review_project):
    p = review_project
    if not os.path.exists("/dev/fuse"):
        if os.environ.get("BST_UTILITIES_REQUIRE_FUSE") == "1":
            pytest.fail("FUSE integration is required but /dev/fuse is missing")
        pytest.skip("/dev/fuse is unavailable")
    elements = p.project / "elements"
    names = [f"t{i}" for i in range(6)]
    for name in names:
        (p.project / name).mkdir()
        (p.project / name / "file.txt").write_text(f"{name} dir\nother\n{name} dir again\n")
        (elements / f"{name}.bst").write_text(
            f"kind: import\nsources:\n- kind: local\n  path: {name}\n"
        )
        p.fetch(f"{name}.bst")
    deps = "".join(f"- {name}.bst\n" for name in names)
    (elements / "trees.bst").write_text(f"kind: stack\ndepends:\n{deps}")
    mounts = p.tmp_path / "mounts"
    outputs = {}
    for jobs in (1, 3):
        for extra in ([], ["--origin"]):
            result = p.search(
                "trees.bst",
                "-n",
                "dir",
                "--json",
                "--stats",
                "--mount-dir",
                str(mounts),
                "--jobs",
                str(jobs),
                *extra,
            )
            assert result.returncode == 0, result.stderr
            outputs[jobs, bool(extra)] = result.stdout
            limit = jobs + 1 if extra and jobs > 1 else jobs
            assert re.search(rf"peak mounts:\s+[1-{limit}]\b", result.stderr), result.stderr
            assert re.search(r"fuse processes:\s+6\b", result.stderr), result.stderr
            assert re.search(r"rg processes:\s+6\b", result.stderr), result.stderr
            assert not list(mounts.iterdir())
    records = [json.loads(line) for line in outputs[1, False].splitlines()]
    assert [(r["element"], r["line"]) for r in records] == [
        (f"{name}.bst", line) for name in sorted(names) for line in (1, 3)
    ]
    # Pooled output is byte-identical to serial output, in the same order.
    assert outputs[3, False] == outputs[1, False]
    assert outputs[3, True] == outputs[1, True]


@pytest.mark.integration
def test_single_tree_streams_before_rg_finishes(review_project):
    p = review_project
    if not os.path.exists("/dev/fuse"):
        if os.environ.get("BST_UTILITIES_REQUIRE_FUSE") == "1":
            pytest.fail("FUSE integration is required but /dev/fuse is missing")
        pytest.skip("/dev/fuse is unavailable")
    # A stand-in rg that emits enough matches to flush, then keeps running.
    fake_bin = p.tmp_path / "fake-bin"
    fake_bin.mkdir()
    rg = fake_bin / "rg"
    rg.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "for n in range(20000):\n"
        "    event = {'type': 'match', 'data': {'path': {'text': './file.txt'},\n"
        "             'lines': {'text': 'line\\n'}, 'line_number': n + 1}}\n"
        "    sys.stdout.write(json.dumps(event) + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(120)\n"
    )
    rg.chmod(0o755)
    p.fetch("multi.bst")
    mounts = p.tmp_path / "mounts"
    env = dict(p.env, PATH=f"{fake_bin}{os.pathsep}{p.env['PATH']}")
    start = time.monotonic()
    proc = subprocess.Popen(
        p.base + ["multi.bst", "line", "--jobs", "4", "--mount-dir", str(mounts)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 60)
        assert ready, "no output while rg was still running"
        assert proc.stdout.readline().startswith(b"multi.bst:")
        proc.stdout.close()
        assert proc.wait(timeout=60) == 141
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert time.monotonic() - start < 100, "search waited for rg to finish"
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
        assert 1 <= trees["peak mounts"] <= trees["search jobs"] and trees["rg processes"] == 5
        serial = by_name["content-unique-trees-serial"]["runs"][0]
        assert serial["peak mounts"] == 1 and serial["search jobs"] == 1
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
            (root / "elements" / element).parent.mkdir(parents=True, exist_ok=True)
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
        {"src/lib.txt": "sub needle\n", "spare/spare.txt": "spare needle\n"},
        {
            "lib.bst": local,
            # Nothing depends on this one; only --include-subprojects reaches it.
            "spare.bst": "kind: import\nsources:\n- kind: local\n  path: spare\n",
            "inner.bst": f"kind: junction\nsources:\n- kind: tar\n  url: file://{archive}\n"
            f"  ref: {digest}\n",
        },
    )
    project(
        main,
        "main",
        {"src/lib.txt": "main needle\n", "orphan/orphan.txt": "orphan needle\n"},
        {
            "lib.bst": local,
            # Unused, with no ref: --all-elements must not try to fetch it.
            "unused.bst": "kind: junction\nsources:\n- kind: tar\n  url: file:///nonexistent.tar\n",
            # No target depends on this one; only --all-elements reaches it.
            "extra/orphan.bst": "kind: import\nsources:\n- kind: local\n  path: orphan\n",
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
    every = search("--all-elements", "--find", "*.txt", "--fetch-sources")
    assert every.returncode == 0, every.stderr
    assert sorted(every.stdout.splitlines()) == [
        "extra/orphan.bst:orphan.txt",
        "lib.bst:lib.txt",
        "sub.bst:inner.bst:leaf.bst:leaf.txt",
        "sub.bst:lib.bst:lib.txt",
    ], every.stderr
    own = search("--all-elements", "--find", "*.txt", "--deps", "none", "--fetch-sources")
    assert own.returncode == 0, own.stderr
    # alias.bst is a link, so as a target it stands for sub.bst:lib.bst.
    assert sorted(own.stdout.splitlines()) == [
        "extra/orphan.bst:orphan.txt",
        "lib.bst:lib.txt",
        "sub.bst:lib.bst:lib.txt",
    ]
    deep = search("--all-elements", "--include-subprojects", "--find", "*.txt", "--fetch-sources")
    # unused.bst has no ref, so its subproject cannot load: reported, the rest searched.
    assert deep.returncode == 2, deep.stderr
    assert "ERROR: could not load subproject unused.bst" in deep.stderr, deep.stderr
    assert sorted(deep.stdout.splitlines()) == [
        "extra/orphan.bst:orphan.txt",
        "lib.bst:lib.txt",
        "sub.bst:inner.bst:leaf.bst:leaf.txt",
        "sub.bst:lib.bst:lib.txt",
        "sub.bst:spare.bst:spare.txt",
    ], deep.stderr
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


@pytest.mark.integration
def test_all_options_searches_union_of_option_sets(tmp_path):
    pytest.importorskip("buildstream")
    if shutil.which("bst") is None:
        pytest.skip("BuildStream CLI is not installed")
    try:
        with socket.socket(socket.AF_UNIX):
            pass
    except PermissionError:
        pytest.skip("environment disallows Unix sockets required by BuildStream casd")

    main = tmp_path / "main"
    files = {
        "base/base.txt": "needle base\n",
        "x/onlyx.txt": "needle x\n",
        "y/onlyy.txt": "needle y\n",
        "extra/extra.txt": "needle extra\n",
        "sub/elements/lib.bst": """kind: import
(?):
- flavour == "x":
    sources:
    - kind: local
      path: subx
- flavour == "y":
    sources:
    - kind: local
      path: suby
""",
        "sub/project.conf": """name: sub
min-version: 2.8
element-path: elements
options:
  flavour:
    type: enum
    description: forwarded by the parent junction
    values: [x, y]
    default: x
""",
        "sub/subx/subx.txt": "needle subx\n",
        "sub/suby/suby.txt": "needle suby\n",
        "sub/include/shared.yml": """options:
  shared:
    type: bool
    description: declared in a junction include
    default: false
""",
        # extra comes from a local include, shared from a junction include.
        "project.conf": """name: main
min-version: 2.8
element-path: elements
(@):
- include/options.yml
- sub.bst:include/shared.yml
options:
  flavour:
    type: enum
    description: selects sources through conditionals and the junction
    values: [x, y]
    default: x
    variable: flavour
""",
        "include/options.yml": """options:
  extra:
    type: bool
    description: adds a dependency
    default: false
""",
        # flavour selects a source, a variable source path and a subproject option;
        # extra appends a dependency; one combination is declared unsupported.
        "elements/app.bst": """kind: stack
depends:
- base.bst
- variant.bst
- sub.bst:lib.bst
(?):
- extra:
    depends:
      (>):
      - extra.bst
- flavour == "y" and extra:
    (!): flavour y does not support extra
""",
        "elements/base.bst": "kind: import\nsources:\n- kind: local\n  path: base\n",
        "elements/extra.bst": "kind: import\nsources:\n- kind: local\n  path: extra\n",
        "elements/variant.bst": "kind: import\nsources:\n- kind: local\n  path: '%{flavour}'\n",
        "elements/sub.bst": """kind: junction
sources:
- kind: local
  path: sub
config:
  options:
    flavour: '%{flavour}'
""",
    }
    for rel, text in files.items():
        (main / rel).parent.mkdir(parents=True, exist_ok=True)
        (main / rel).write_text(text)
    config = tmp_path / "buildstream.conf"
    config.write_text(f"cachedir: {tmp_path / 'cache'}\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    def search(*args):
        cmd = [sys.executable, "-m", "bst_utilities.source_grep", "--config", str(config)]
        return subprocess.run(
            cmd + ["-C", str(main), "app.bst", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def fetch(*options):
        subprocess.run(
            ["bst", "-C", str(main), "--config", str(config), "--no-interactive", *options]
            + ["source", "fetch", "--deps", "all", "app.bst"],
            env=env,
            check=True,
            capture_output=True,
        )

    fetch()
    default = search("--find", "*.txt")
    assert default.returncode == 0, default.stderr
    assert sorted(default.stdout.splitlines()) == [
        "base.bst:base.txt",
        "sub.bst:lib.bst:subx.txt",
        "variant.bst:onlyx.txt",
    ]

    partial = search("--find", "*.txt", "--all-options")
    assert partial.returncode == 2, partial.stderr
    assert "NOTE: skipped option set [extra=true flavour=y]" in partial.stderr
    assert "source tree is not cached: extra.bst [options: extra=true flavour=x]" in partial.stderr
    assert "variant.bst:onlyx.txt" in partial.stdout.splitlines()

    # --fetch-sources fetches every option set's sources before searching it.
    fetched = search("--find", "*.txt", "--all-options", "--fetch-sources")
    assert fetched.returncode == 0, fetched.stderr
    assert "variant.bst:onlyy.txt" in fetched.stdout.splitlines()
    assert "NOTE: loaded 1/4 option sets in " in fetched.stderr
    union = search("needle", "--all-options", "--json", "--stats")
    assert union.returncode == 0, union.stderr
    assert re.search(r"option sets loaded:\s+3\b", union.stderr), union.stderr
    assert re.search(r"option sets skipped:\s+1\b", union.stderr), union.stderr
    # The locally included option is enumerated; the junction-included one is reported.
    assert "option extra" not in union.stderr
    assert "includes sub.bst:include/shared.yml from a junction" in union.stderr
    assert "option shared is declared in a file included from a junction" in union.stderr
    records = [json.loads(line) for line in union.stdout.splitlines()]
    reached = {r["path"]: r["option_sets"] for r in records}
    x, xe, y = (
        {"flavour": "x", "extra": "false"},
        {"flavour": "x", "extra": "true"},
        {"flavour": "y", "extra": "false"},
    )
    assert reached == {
        "base.txt": [x, y, xe],
        "onlyx.txt": [x, xe],
        "onlyy.txt": [y],
        "extra.txt": [xe],
        "subx.txt": [x, xe],
        "suby.txt": [y],
    }
    assert len(records) == 6  # each tree once, with every option set that reached it

    pinned = search("--find", "*.txt", "--all-options", "-o", "flavour", "y")
    assert pinned.returncode == 0, pinned.stderr
    assert "onlyx.txt" not in pinned.stdout and "variant.bst:onlyy.txt" in pinned.stdout

    capped = search("--find", "*.txt", "--all-options", "--max-option-sets", "3")
    assert capped.returncode == 2
    assert "would load 4 option sets (extra=2, flavour=2)" in capped.stderr
    assert capped.stdout == ""

    # Template -> edited options file -> search: vary flavour; extra stays commented,
    # so it keeps its configured value instead of being enumerated.
    template = search("--options-template")
    assert template.returncode == 0, template.stderr
    # "y" is a YAML 1.1 boolean, so the template quotes it.
    assert (
        '  # flavour: [x, "y"]' in template.stdout and "  # extra: [false, true]" in template.stdout
    )
    edited = template.stdout.replace('  # flavour: [x, "y"]', '  flavour: [x, "y"]')
    options_file = tmp_path / "options.yml"
    options_file.write_text(edited)
    listing = search("--list-options", "--options-file", str(options_file))
    assert listing.returncode == 0, listing.stderr
    assert "[--all-options tries 2: 'x', 'y']" in listing.stdout
    assert "[kept: not in the options file]" in listing.stdout
    narrowed = search("needle", "--all-options", "--json", "--options-file", str(options_file))
    assert narrowed.returncode == 0, narrowed.stderr
    assert "not in " + str(options_file) + " keep their configured value: extra" in narrowed.stderr
    narrowed_records = [json.loads(line) for line in narrowed.stdout.splitlines()]
    assert {r["path"]: r["option_sets"] for r in narrowed_records} == {
        "base.txt": [{"flavour": "x"}, {"flavour": "y"}],
        "onlyx.txt": [{"flavour": "x"}],
        "onlyy.txt": [{"flavour": "y"}],
        "subx.txt": [{"flavour": "x"}],
        "suby.txt": [{"flavour": "y"}],
    }
    # Without --all-options, -o replaces the file's list with a pin.
    single = search("--find", "*.txt", "--options-file", str(options_file), "-o", "flavour", "x")
    assert single.returncode == 0, single.stderr
    assert "variant.bst:onlyx.txt" in single.stdout.splitlines()


@pytest.mark.integration
def test_all_options_applies_empty_flags_over_user_configuration(tmp_path):
    pytest.importorskip("buildstream")
    if shutil.which("bst") is None:
        pytest.skip("BuildStream CLI is not installed")
    try:
        with socket.socket(socket.AF_UNIX):
            pass
    except PermissionError:
        pytest.skip("environment disallows Unix sockets required by BuildStream casd")

    project = tmp_path / "project"
    files = {
        "none/none.txt": "needle none\n",
        "a/a.txt": "needle a\n",
        "project.conf": """name: flagsproj
min-version: 2.8
element-path: elements
options:
  feats:
    type: flags
    description: empty by default
    values: [a]
    default: []
""",
        "elements/app.bst": """kind: import
(?):
- '"a" in feats':
    sources:
    - kind: local
      path: a
- '"a" not in feats':
    sources:
    - kind: local
      path: none
""",
    }
    for rel, text in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(text)
    # The user configuration selects feats=a; only the project default is empty,
    # and BuildStream's command line cannot express an empty flags value.
    plain = tmp_path / "plain.conf"
    plain.write_text(f"cachedir: {tmp_path / 'cache'}\n")
    override = tmp_path / "override.conf"
    override.write_text(
        f"cachedir: {tmp_path / 'cache'}\nprojects:\n  flagsproj:\n    options:\n      feats: [a]\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    for config in (plain, override):
        subprocess.run(
            ["bst", "-C", str(project), "--config", str(config), "--no-interactive"]
            + ["source", "fetch", "app.bst"],
            env=env,
            check=True,
            capture_output=True,
        )

    result = subprocess.run(
        [sys.executable, "-m", "bst_utilities.source_grep", "--config", str(override)]
        + ["-C", str(project), "app.bst", "--find", "*.txt", "--all-options", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    reached = {
        json.loads(line)["path"]: json.loads(line)["option_sets"]
        for line in result.stdout.splitlines()
    }
    assert reached == {"none.txt": [{"feats": ""}], "a.txt": [{"feats": "a"}]}


@pytest.mark.integration
def test_all_options_passes_empty_enum_values_on_the_command_line(tmp_path):
    pytest.importorskip("buildstream")
    if shutil.which("bst") is None:
        pytest.skip("BuildStream CLI is not installed")
    try:
        with socket.socket(socket.AF_UNIX):
            pass
    except PermissionError:
        pytest.skip("environment disallows Unix sockets required by BuildStream casd")

    project = tmp_path / "project"
    files = {
        "plain/plain.txt": "needle plain\n",
        "x/x.txt": "needle x\n",
        "project.conf": """name: enumproj
min-version: 2.8
element-path: elements
options:
  mode:
    type: enum
    description: an empty enum value is valid
    values: ['', x]
    default: ''
""",
        "elements/app.bst": """kind: import
(?):
- mode == "x":
    sources:
    - kind: local
      path: x
- mode == "":
    sources:
    - kind: local
      path: plain
""",
    }
    for rel, text in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(text)
    config = tmp_path / "buildstream.conf"
    config.write_text(f"cachedir: {tmp_path / 'cache'}\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    for options in ([], ["-o", "mode", "x"]):
        subprocess.run(
            ["bst", "-C", str(project), "--config", str(config), "--no-interactive", *options]
            + ["source", "fetch", "app.bst"],
            env=env,
            check=True,
            capture_output=True,
        )

    result = subprocess.run(
        [sys.executable, "-m", "bst_utilities.source_grep", "--config", str(config)]
        + ["-C", str(project), "app.bst", "--find", "*.txt", "--all-options", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    reached = {
        json.loads(line)["path"]: json.loads(line)["option_sets"]
        for line in result.stdout.splitlines()
    }
    assert reached == {"plain.txt": [{"mode": ""}], "x.txt": [{"mode": "x"}]}
