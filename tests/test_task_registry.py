import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tasks", ROOT / "scripts/tasks.py")
tasks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tasks)


def registry():
    return json.loads(tasks.REGISTRY.read_text())


def test_registry_and_generated_index_are_valid():
    data = tasks.validate(registry())
    assert tasks.INDEX.read_text() == tasks.render(data)


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
