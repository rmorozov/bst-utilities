"""Discover local CAS locations and convert BuildStream directory digests."""

from __future__ import annotations

import argparse
import os


def get_cas_directory_digest(directory):
    """
    Best-effort extraction of the CAS directory digest object from a
    BuildStream CAS directory.
    """
    getter = getattr(directory, "_get_digest", None)
    if callable(getter):
        return getter()

    for attr_name in (
        "digest",
        "_digest",
        "directory_digest",
        "root_digest",
        "_root_digest",
        "_directory_digest",
    ):
        try:
            value = getattr(directory, attr_name, None)
        except Exception:
            continue

        if callable(value):
            try:
                value = value()
            except Exception:
                continue

        if value is not None:
            return value

    for method_name in (
        "_get_digest",
        "get_digest",
        "digest",
        "get_directory_digest",
        "get_root_digest",
    ):
        method = getattr(directory, method_name, None)
        if callable(method):
            try:
                value = method()
                if value is not None:
                    return value
            except Exception:
                pass

    file_digest = getattr(directory, "file_digest", None)
    if callable(file_digest):
        for candidate in ("", ".", "/"):
            try:
                value = file_digest(candidate)
                if value is not None:
                    return value
            except Exception:
                pass

    try:
        for value in vars(directory).values():
            if hasattr(value, "hash") and (hasattr(value, "size_bytes") or hasattr(value, "size")):
                return value
    except Exception:
        pass

    return None


def digest_to_fuse_value(digest):
    """
    Convert a BuildStream/CAS digest object into the <hash>/<size> format
    expected by buildbox-fuse --input-digest-value.
    """
    if digest is None:
        return None

    if isinstance(digest, str):
        if "/" in digest:
            hash_part, size_part = digest.split("/", 1)
            if hash_part and size_part.isdigit():
                return digest
        return None

    if isinstance(digest, (bytes, bytearray)):
        return None

    if isinstance(digest, tuple) and len(digest) == 2:
        hash_value, size_value = digest

        if isinstance(hash_value, (bytes, bytearray)):
            hash_value = bytes(hash_value).hex()

        try:
            size_value = int(size_value)
        except Exception:
            return None

        return f"{hash_value}/{size_value}"

    hash_value = getattr(digest, "hash", None)
    if hash_value is None:
        hash_value = getattr(digest, "hash_str", None)
    if hash_value is None:
        hash_value = getattr(digest, "digest", None)

    size_value = getattr(digest, "size_bytes", None)
    if size_value is None:
        size_value = getattr(digest, "size", None)

    if hash_value is None or size_value is None:
        return None

    if isinstance(hash_value, (bytes, bytearray)):
        hash_value = bytes(hash_value).hex()
    elif not isinstance(hash_value, str):
        hash_value = str(hash_value)

    try:
        size_value = int(size_value)
    except Exception:
        return None

    return f"{hash_value}/{size_value}"


def find_cas_dir(context, args: argparse.Namespace) -> str:
    if args.cas_dir:
        return os.path.abspath(os.path.expanduser(args.cas_dir))

    candidates = []

    def add_candidate(value):
        if value and isinstance(value, str):
            candidates.append(os.path.expanduser(value))

    for attr_name in (
        "casdir",
        "cas_cache_dir",
        "_casdir",
        "local_cas_dir",
        "_local_cas_dir",
    ):
        try:
            add_candidate(getattr(context, attr_name, None))
        except Exception:
            pass

    for obj_attr in (
        "cas",
        "_cas",
        "artifact_cache",
        "_artifact_cache",
        "source_cache",
        "_source_cache",
    ):
        try:
            obj = getattr(context, obj_attr, None)
        except Exception:
            continue

        if obj is None:
            continue

        for sub_attr in (
            "basedir",
            "_basedir",
            "cache_dir",
            "_cache_dir",
            "directory",
            "_directory",
            "path",
            "_path",
        ):
            try:
                add_candidate(getattr(obj, sub_attr, None))
            except Exception:
                pass

    for attr_name in ("sourcedir", "artifactdir", "builddir"):
        try:
            base = getattr(context, attr_name, None)
        except Exception:
            continue

        if base:
            add_candidate(base)
            add_candidate(os.path.join(base, "cas"))
            add_candidate(os.path.join(base, "cas", "cache"))

    home_cache = os.path.expanduser("~/.cache/buildstream")
    add_candidate(home_cache)
    add_candidate(os.path.join(home_cache, "cas"))
    add_candidate(os.path.join(home_cache, "cas", "cache"))

    for candidate in candidates:
        if os.path.isdir(candidate):
            objects_dir = os.path.join(candidate, "objects")
            if os.path.isdir(objects_dir):
                return candidate

            cas_subdir = os.path.join(candidate, "cas")
            if os.path.isdir(cas_subdir):
                cas_objects = os.path.join(cas_subdir, "objects")
                if os.path.isdir(cas_objects):
                    return cas_subdir
                return cas_subdir

            return candidate

    return home_cache
