import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tasks", ROOT / "scripts/tasks.py")
tasks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tasks)


def registry():
    return tasks.load()


def test_registry_and_report_are_valid():
    data = tasks.validate(registry())
    assert "BSG-010" in tasks.render(data)
    assert len(data["tasks"]) == len(list(tasks.TASK_DIR.glob("*.json")))


@pytest.mark.parametrize(
    "failure",
    [
        "duplicate",
        "missing_dependency",
        "cycle",
        "invalid_status",
        "missing_evidence",
        "missing_acceptance",
    ],
)
def test_invalid_registry_is_rejected(failure):
    data = copy.deepcopy(registry())
    first, second = data["tasks"][:2]
    if failure == "duplicate":
        second["id"] = first["id"]
    elif failure == "missing_dependency":
        first["depends_on"] = ["nonexistent"]
    elif failure == "cycle":
        first["depends_on"] = [second["id"]]
        second["depends_on"] = [first["id"]]
    elif failure == "invalid_status":
        first["status"] = "probably_done"
    elif failure == "missing_evidence":
        first["status"] = "done"
        first["evidence"] = []
    else:
        first["acceptance"] = []
    with pytest.raises(ValueError):
        tasks.validate(data)


def test_registry_check_and_render_under_ascii_locale(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docs" / "tasks").mkdir(parents=True)
    script = tmp_path / "scripts" / "tasks.py"
    shutil.copyfile(ROOT / "scripts" / "tasks.py", script)
    shutil.copytree(tasks.TASK_DIR, tmp_path / "docs" / "tasks" / "items")
    env = dict(os.environ, LC_ALL="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
    for command in ["check", "render", "check"]:
        result = subprocess.run(
            [sys.executable, str(script), command],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("failure", ["filename", "schema", "malformed", "empty"])
def test_invalid_task_files_are_rejected(tmp_path, failure):
    task = registry()["tasks"][0]
    data = {"schema_version": 1, "task": task}
    name = task["id"] if failure != "filename" else "WRONG-001"
    if failure == "schema":
        data["schema_version"] = 99
    if failure != "empty":
        (tmp_path / f"{name}.json").write_text(
            "{" if failure == "malformed" else json.dumps(data), encoding="utf-8"
        )
    with pytest.raises(ValueError):
        tasks.load(tmp_path)


def test_independent_tasks_can_be_added_without_shared_index(tmp_path):
    for task_id in ["NEW-001", "NEW-002"]:
        task = copy.deepcopy(registry()["tasks"][0])
        task.update(id=task_id, depends_on=[])
        (tmp_path / f"{task_id}.json").write_text(
            json.dumps({"schema_version": 1, "task": task}), encoding="utf-8"
        )
    assert [t["id"] for t in tasks.load(tmp_path)["tasks"]] == ["NEW-001", "NEW-002"]
