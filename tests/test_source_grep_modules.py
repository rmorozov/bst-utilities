"""CLI and backend contracts across the extracted module boundaries."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bst_utilities._source_grep import cli, output, search_fuse, stats


def test_module_help_without_site_packages():
    # -S excludes BuildStream and packaging: help must remain dependency-free.
    src = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [sys.executable, "-S", "-m", "bst_utilities.source_grep", "--help"],
        env=dict(os.environ, PYTHONPATH=str(src)),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--fetch-subprojects" in result.stdout and "--jobs" in result.stdout


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("origin_mode", [False, True])
@pytest.mark.parametrize("line_mode", [False, True])
@pytest.mark.parametrize("strip_mode", [False, True])
def test_emitter_preserves_output_contract(capsys, json_mode, origin_mode, line_mode, strip_mode):
    args = SimpleNamespace(
        json=json_mode, origin=origin_mode, line_number=line_mode, strip_junctions=strip_mode
    )
    counters = stats.new_stats()
    emitter = output.ResultEmitter(args, output.LineBuffer(), counters)
    element = {"label": "sub.bst:lib.bst", "recipe": "lib.bst"}
    origin = {"id": "source-1"}
    emitter.emit_file(element, "file.c", origin)
    emitter.emit_match(element, "file.c", 3, "needle", origin)
    emitter.out.flush()
    lines = capsys.readouterr().out.splitlines()
    display = "lib.bst" if strip_mode else element["label"]
    if json_mode:
        expected = [
            {"type": "file", "element": element["label"], "recipe": display, "path": "file.c"},
            {
                "type": "match",
                "element": element["label"],
                "recipe": display,
                "path": "file.c",
                "line": 3,
                "text": "needle",
            },
        ]
        if origin_mode:
            for record in expected:
                record["origin"] = origin
        assert [json.loads(line) for line in lines] == expected
    else:
        prefix = display + (":source-1" if origin_mode else "") + ":file.c"
        assert lines == [prefix, prefix + (":3" if line_mode else "") + ":needle"]
    assert counters["results"] == 2


@pytest.mark.parametrize("jobs", [1, 3])
@pytest.mark.parametrize("failed_mount", [False, True])
def test_extracted_fuse_backend_orders_output_and_preserves_partial_status(
    tmp_path, capsys, jobs, failed_mount
):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is required")
    args = cli.parse_args(["root.bst", "needle", "--jobs", str(jobs), "-n"])
    trees = {}
    for i in range(3):
        directory = tmp_path / str(i)
        directory.mkdir()
        (directory / "file.c").write_text("needle\n")
        trees[str(i)] = {
            "digest": str(i),
            "elements": [{"label": f"{i}.bst", "recipe": f"{i}.bst"}],
            "gitreview": None,
        }

    class Mounts:
        def ensure_mount(self, digest):
            if failed_mount and digest == "1":
                raise RuntimeError("fixture mount failure")
            return str(tmp_path / digest)

        def release(self, digest):
            pass

    counters = stats.new_stats()
    emitter = output.ResultEmitter(args, output.LineBuffer(), counters)
    result = search_fuse.search_fuse(trees, args, counters, Mounts(), emitter)
    captured = capsys.readouterr()
    selected = [0, 2] if failed_mount else [0, 1, 2]
    assert captured.out.splitlines() == [f"{i}.bst:file.c:1:needle" for i in selected]
    assert result == (2 if failed_mount else 0)
    assert counters["mount_errors"] == int(failed_mount)
    assert counters["results"] == len(selected)
