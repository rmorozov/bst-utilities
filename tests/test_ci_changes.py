import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_changes", ROOT / "scripts/ci_changes.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


@pytest.mark.parametrize(
    "paths,expected",
    [
        (["README.md", "docs/tasks/items/BSG-010.json"], True),
        (["AGENTS.md", ".github/pull_request_template.md"], True),
        (["docs/source-grep.md", "src/bst_utilities/source_grep.py"], False),
        ([".github/workflows/ci.yml"], False),
        (["scripts/tasks.py"], False),
        (["tests/test_task_registry.py"], False),
        (["pyproject.toml"], False),
        (["docs/example.py"], False),
        (["unknown.txt"], False),
        ([], False),
    ],
)
def test_documentation_filter(paths, expected):
    assert ci.docs_only(paths) is expected


def test_event_baselines_and_deleted_paths(monkeypatch):
    commands = []

    def diff(args):
        commands.append(args)
        return b"docs/removed.md\0src/removed.py\0"

    monkeypatch.setattr(subprocess, "check_output", diff)
    event = {"pull_request": {"base": {"sha": "base"}, "head": {"sha": "head"}}}
    assert ci.changed_paths("pull_request", event) == ["docs/removed.md", "src/removed.py"]
    assert commands[-1][-1] == "base...head"
    ci.changed_paths("push", {"before": "base", "after": "head"})
    assert commands[-1][-2:] == ["base", "head"]
    assert "--no-renames" in commands[-1]
    assert ci.changed_paths("workflow_dispatch", {}) == []
    assert ci.changed_paths("push", {"before": "0" * 40}) == []


@pytest.mark.parametrize("event_name", ["pull_request", "push"])
def test_code_to_docs_rename_requires_full_validation(tmp_path, monkeypatch, event_name):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "runtime.py").write_text("print('example')\n")
    git("add", ".")
    git("commit", "-qm", "baseline")
    base = git("rev-parse", "HEAD")
    (tmp_path / "docs").mkdir()
    git("mv", "runtime.py", "docs/example.md")
    git("commit", "-qm", "rename")
    head = git("rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    event = {
        "before": base,
        "after": head,
        "pull_request": {"base": {"sha": base}, "head": {"sha": head}},
    }
    paths = ci.changed_paths(event_name, event)
    assert set(paths) == {"runtime.py", "docs/example.md"}
    assert not ci.docs_only(paths)
