#!/usr/bin/env python3
"""Validate the canonical task registry and render its reviewable Markdown view."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs/tasks/registry.json"
INDEX = ROOT / "docs/tasks/INDEX.md"
STATUSES = {"proposed", "ready", "in_progress", "in_review", "blocked", "done", "dropped"}
FIELDS = {
    "id",
    "tool",
    "title",
    "area",
    "priority",
    "status",
    "effort",
    "problem",
    "proposal",
    "acceptance",
    "depends_on",
    "evidence",
}


def validate(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("registry schema_version must be 1")
    if not isinstance(data.get("tasks"), list):
        raise ValueError("tasks must be a list")
    tasks = {}
    for task in data["tasks"]:
        if not isinstance(task, dict) or set(task) != FIELDS:
            raise ValueError("every task must have exactly the documented fields")
        task_id = task["id"]
        if not isinstance(task_id, str) or not task_id or task_id in tasks:
            raise ValueError(f"duplicate or invalid task id: {task_id!r}")
        tasks[task_id] = task
        for field in ["tool", "title", "area", "problem", "proposal"]:
            if not isinstance(task[field], str) or not task[field].strip():
                raise ValueError(f"{task_id}: {field} must be nonempty text")
        for field in ["acceptance", "depends_on", "evidence"]:
            if not isinstance(task[field], list) or not all(
                isinstance(v, str) and v.strip() for v in task[field]
            ):
                raise ValueError(f"{task_id}: {field} must be a list of nonempty strings")
        if not task["acceptance"]:
            raise ValueError(f"{task_id}: acceptance criteria are required")
        if task["priority"] not in {"P1", "P2", "P3"} or task["effort"] not in {"S", "M", "L"}:
            raise ValueError(f"{task_id}: invalid priority or effort")
        if task["status"] not in STATUSES:
            raise ValueError(f"{task_id}: invalid status")
        if task["status"] in {"in_review", "done"} and not task["evidence"]:
            raise ValueError(f"{task_id}: completed work needs evidence")
    visiting, visited = set(), set()

    def visit(task_id):
        if task_id in visiting:
            raise ValueError(f"dependency cycle at {task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in tasks[task_id]["depends_on"]:
            if dependency not in tasks:
                raise ValueError(f"{task_id}: unknown dependency {dependency}")
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)
    return data


def render(data):
    def cell(text):
        return text.replace("|", "\\|").replace("\n", " ")

    lines = [
        "# Task index",
        "",
        "Generated from `registry.json` by `python scripts/tasks.py render`.",
        "",
        "See [registry conventions](README.md) before changing status or priority.",
        "",
        "| ID | Tool | Task | Area | Priority | Status | Effort | Depends on |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for task in data["tasks"]:
        values = [
            task["id"],
            task["tool"],
            task["title"],
            task["area"],
            task["priority"],
            task["status"],
            task["effort"],
            ", ".join(task["depends_on"]) or "—",
        ]
        lines.append("| " + " | ".join(cell(v) for v in values) + " |")
    for task in data["tasks"]:
        lines.extend(
            [
                "",
                f"## {task['id']}: {task['title']}",
                "",
                task["problem"],
                "",
                "Proposed work: " + task["proposal"],
                "",
                "Acceptance:",
                "",
            ]
        )
        lines.extend("- " + criterion for criterion in task["acceptance"])
        if task["evidence"]:
            lines.extend(["", "Evidence:", ""])
            lines.extend("- " + evidence for evidence in task["evidence"])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "render", "list"])
    parser.add_argument("--status", choices=sorted(STATUSES))
    parser.add_argument("--tool")
    args = parser.parse_args()
    try:
        data = validate(json.loads(REGISTRY.read_text()))
        if args.command == "render":
            INDEX.write_text(render(data))
        elif args.command == "check":
            if not INDEX.exists() or INDEX.read_text() != render(data):
                raise ValueError("task index is stale; run python scripts/tasks.py render")
            print(f"{len(data['tasks'])} tasks validated; index is current")
        else:
            for task in data["tasks"]:
                if (args.status is None or task["status"] == args.status) and (
                    args.tool is None or task["tool"] == args.tool
                ):
                    print(f"{task['id']} {task['priority']} {task['status']}: {task['title']}")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
