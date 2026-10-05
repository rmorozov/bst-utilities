"""Locate explicitly configured or bundled buildbox-fuse executables."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
import shutil
import sys
from pathlib import Path


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except Exception:
        return False


def _iter_tree(root: Path, max_depth: int = 6):
    root = Path(root)

    if not root.is_dir():
        return

    stack = [(root, 0)]

    while stack:
        current, depth = stack.pop()

        try:
            entries = list(current.iterdir())
        except Exception:
            continue

        for entry in entries:
            yield entry, depth

            try:
                if entry.is_dir():
                    if entry.name in (
                        ".git",
                        ".hg",
                        ".svn",
                        "__pycache__",
                        ".tox",
                        ".mypy_cache",
                        ".pytest_cache",
                        "node_modules",
                    ):
                        continue

                    if depth < max_depth:
                        stack.append((entry, depth + 1))
            except Exception:
                continue


def _find_named_file(root, max_depth: int = 6, executable_only: bool = False):
    root = Path(root)

    if not root.is_dir():
        return None

    for entry, _ in _iter_tree(root, max_depth=max_depth):
        try:
            if entry.is_file() and entry.name in BUILDBOX_FUSE_NAMES:
                if executable_only:
                    if _is_executable_file(entry):
                        return entry
                else:
                    return entry
        except Exception:
            continue

    return None


def _iter_buildstream_roots():
    roots = []
    seen = set()

    def add(path):
        if not path:
            return

        try:
            p = Path(path).resolve()
        except Exception:
            return

        if p not in seen:
            seen.add(p)
            roots.append(p)

    try:
        import buildstream

        module_file = getattr(buildstream, "__file__", None)
        if module_file:
            add(Path(module_file).parent)

        module_path = getattr(buildstream, "__path__", None)
        if module_path:
            for p in module_path:
                add(p)

    except Exception:
        pass

    try:
        spec = importlib.util.find_spec("buildstream")

        if spec is not None:
            if spec.origin and spec.origin not in ("built-in", "frozen"):
                add(Path(spec.origin).parent)

            search_locations = getattr(spec, "submodule_search_locations", None)
            if search_locations:
                for p in search_locations:
                    add(p)

    except Exception:
        pass

    return roots


def _iter_buildstream_package_candidates():
    for root in _iter_buildstream_roots():
        if not root.is_dir():
            continue

        known = [
            root / "subprojects" / "buildbox" / "buildbox-fuse",
            root / "subprojects" / "buildbox" / "buildbox-fuse" / "buildbox-fuse",
            root / "subprojects" / "buildbox" / "bin" / "buildbox-fuse",
            root / "subprojects" / "buildbox" / "build" / "buildbox-fuse",
            root / "subprojects" / "buildbox" / "build" / "bin" / "buildbox-fuse",
            root / "buildbox" / "buildbox-fuse",
            root / "bin" / "buildbox-fuse",
            root.parent / "bin" / "buildbox-fuse",
        ]

        for candidate in known:
            yield candidate

        subprojects = root / "subprojects"
        if subprojects.is_dir():
            found = _find_named_file(subprojects, max_depth=6, executable_only=True)
            if found is not None:
                yield found

        found = _find_named_file(root, max_depth=2, executable_only=True)
        if found is not None:
            yield found


def _iter_distribution_metadata_candidates():
    distribution_names = (
        "buildstream",
        "BuildStream",
        "buildbox",
        "buildbox-fuse",
    )

    for dist_name in distribution_names:
        try:
            dist = importlib.metadata.distribution(dist_name)
        except Exception:
            continue

        try:
            files = dist.files
        except Exception:
            files = None

        if not files:
            continue

        for rel_path in files:
            try:
                rel_path = Path(str(rel_path))
            except Exception:
                continue

            if rel_path.name not in BUILDBOX_FUSE_NAMES:
                continue

            if rel_path.is_absolute():
                yield rel_path
                continue

            try:
                yield Path(dist.locate_file(rel_path))
            except Exception:
                continue


def _iter_venv_bin_dirs():
    dirs = []
    seen = set()

    def add(path):
        try:
            p = Path(path).resolve()
        except Exception:
            return

        if p not in seen:
            seen.add(p)
            dirs.append(p)

    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        add(Path(virtual_env) / "bin")
        add(Path(virtual_env) / "Scripts")

    if sys.prefix:
        add(Path(sys.prefix) / "bin")
        add(Path(sys.prefix) / "Scripts")

    if sys.executable:
        add(Path(sys.executable).parent)

    return dirs


def find_buildbox_fuse(args):
    fallback_non_executable = None

    def consider_candidate(candidate):
        nonlocal fallback_non_executable

        if not candidate:
            return None

        try:
            p = Path(os.path.expanduser(str(candidate))).absolute()
        except Exception:
            return None

        if p.is_file():
            if _is_executable_file(p):
                return p

            if fallback_non_executable is None:
                fallback_non_executable = p

            return None

        if p.is_dir():
            found = _find_named_file(p, max_depth=6, executable_only=True)
            if found is not None:
                return found

            found_any = _find_named_file(p, max_depth=6, executable_only=False)
            if found_any is not None and fallback_non_executable is None:
                fallback_non_executable = found_any

        return None

    # 1. Explicit CLI override.
    explicit = getattr(args, "buildbox_fuse", None)
    if explicit:
        found = consider_candidate(explicit)
        if found is not None:
            return str(found)

        if fallback_non_executable is not None:
            return str(fallback_non_executable)

        return None

    # 2. Environment overrides.
    for env_var in (
        "BSSG_BUILDBOX_FUSE",
        "BUILDBOX_FUSE",
        "BUILDSTREAM_BUILDBOX_FUSE",
    ):
        value = os.environ.get(env_var)
        if value:
            found = consider_candidate(value)
            if found is not None:
                return str(found)

            if fallback_non_executable is not None:
                return str(fallback_non_executable)

            return None

    # 3. Package metadata candidates.
    for candidate in _iter_distribution_metadata_candidates():
        found = consider_candidate(candidate)
        if found is not None:
            return str(found)

    # 4. BuildStream package layout candidates.
    for candidate in _iter_buildstream_package_candidates():
        found = consider_candidate(candidate)
        if found is not None:
            return str(found)

    # 5. Virtualenv bin directories.
    for bin_dir in _iter_venv_bin_dirs():
        for name in BUILDBOX_FUSE_NAMES:
            candidate = bin_dir / name
            found = consider_candidate(candidate)
            if found is not None:
                return str(found)

    # 6. Normal PATH lookup.
    path_result = shutil.which("buildbox-fuse")
    if path_result:
        return path_result

    # 7. Last resort: return a non-executable file if we found one.
    if fallback_non_executable is not None:
        return str(fallback_non_executable)

    return None


BUILDBOX_FUSE_NAMES = (
    "buildbox-fuse",
    "buildbox-fuse.exe",
)
