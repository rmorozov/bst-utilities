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
    return json.loads(tasks.REGISTRY.read_text(encoding="utf-8"))


def test_registry_and_generated_index_are_valid():
    data = tasks.validate(registry())
    assert tasks.INDEX.read_text(encoding="utf-8") == tasks.render(data)


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
    for source in [tasks.REGISTRY, tasks.INDEX]:
        shutil.copyfile(source, tmp_path / "docs" / "tasks" / source.name)
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
    assert (tmp_path / "docs" / "tasks" / "INDEX.md").read_text(
        encoding="utf-8"
    ) == tasks.INDEX.read_text(encoding="utf-8")
