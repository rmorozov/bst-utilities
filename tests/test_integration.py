"""Offline end-to-end test using an isolated BuildStream configuration/cache."""

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

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
            assert "Path-cache hits: 1" in result.stderr
    missing = search("test.bst", "--find", "*.missing", "--no-path-cache")
    assert missing.returncode == 1, missing.stderr
    empty = search("empty.bst", "--find", "*")
    assert empty.returncode == 1, empty.stderr
