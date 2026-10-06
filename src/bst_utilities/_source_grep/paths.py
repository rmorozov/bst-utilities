"""Normalize cached-source paths, junction labels and Gerrit project names."""

from __future__ import annotations

import os


def strip_junction_name(name: str) -> str:
    if not name:
        return name

    p = str(name).replace(os.sep, "/")

    if ":" in p:
        stripped = p.rsplit(":", 1)[-1]
        if stripped:
            p = stripped

    while p.startswith("./"):
        p = p[2:]

    p = p.lstrip("/")

    return p


def normalize_relative_dir(value) -> str:
    if value is None:
        return ""

    p = str(value).replace(os.sep, "/")

    while p.startswith("./"):
        p = p[2:]

    p = p.strip("/")

    if p == ".":
        return ""

    return p


def normalize_path(path) -> str:
    if isinstance(path, bytes):
        p = path.decode("utf-8", "replace")
    else:
        p = str(path)

    p = p.replace(os.sep, "/")

    while p.startswith("./"):
        p = p[2:]

    p = p.lstrip("/")

    while p.endswith("/"):
        p = p[:-1]

    if p == ".":
        return ""

    return p


def _normalize_gerrit_project(value):
    if value is None:
        return None

    p = str(value).strip().strip("/")

    if p.endswith(".git"):
        p = p[:-4]

    p = p.strip("/")

    return p if p else None
