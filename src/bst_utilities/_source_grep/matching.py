"""Filename glob matching and inclusion/exclusion rules."""

from __future__ import annotations

import os
import re


def _normalize_glob_pattern(pattern) -> str:
    p = str(pattern).replace(os.sep, "/")

    while p.startswith("./"):
        p = p[2:]

    p = p.lstrip("/")

    while p.endswith("/"):
        p = p[:-1]

    return p


def _compile_glob(pattern: str):
    normalized = _normalize_glob_pattern(pattern)

    cached = _GLOB_REGEX_CACHE.get(normalized)
    if cached is not None:
        return cached, normalized

    parts = []
    i = 0
    n = len(normalized)

    while i < n:
        c = normalized[i]

        if c == "*":
            if i + 1 < n and normalized[i + 1] == "*":
                if i + 2 < n and normalized[i + 2] == "/":
                    parts.append("(?:.*/)?")
                    i += 3
                else:
                    parts.append(".*")
                    i += 2
                continue

            parts.append("[^/]*")
            i += 1

        elif c == "?":
            parts.append("[^/]")
            i += 1

        elif c == "[":
            j = i + 1

            if j < n and normalized[j] == "!":
                j += 1

            if j < n and normalized[j] == "]":
                j += 1

            while j < n and normalized[j] != "]":
                j += 1

            if j >= n:
                parts.append(re.escape("["))
                i += 1
            else:
                inner = normalized[i + 1 : j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]

                parts.append(f"[{inner}]")
                i = j + 1

        else:
            parts.append(re.escape(c))
            i += 1

    try:
        regex = re.compile("^" + "".join(parts) + r"\Z", re.DOTALL)
    except re.error:
        regex = re.compile("^" + re.escape(normalized) + r"\Z", re.DOTALL)

    _GLOB_REGEX_CACHE[normalized] = regex
    return regex, normalized


def make_find_matcher(pattern: str):
    """
    Build a fast matcher for --find.

    Special-cases common patterns like:
        *.psl
        foo.psl
        foo*
    """
    normalized = _normalize_glob_pattern(pattern)

    if not normalized:
        return lambda _path: False

    has_slash = "/" in normalized

    # Fast exact basename match: foo.psl
    if not has_slash and not any(ch in normalized for ch in "*?["):
        return lambda path: path.rsplit("/", 1)[-1] == normalized

    # Fast suffix match: *.psl
    if (
        not has_slash
        and normalized.startswith("*")
        and normalized.count("*") == 1
        and "?" not in normalized
        and "[" not in normalized
    ):
        suffix = normalized[1:]
        return lambda path: path.endswith(suffix)

    # Fast prefix match: foo*
    if (
        not has_slash
        and normalized.endswith("*")
        and normalized.count("*") == 1
        and "?" not in normalized
        and "[" not in normalized
    ):
        prefix = normalized[:-1]
        return lambda path: path.rsplit("/", 1)[-1].startswith(prefix)

    regex, normalized = _compile_glob(pattern)

    if "/" in normalized:
        return lambda path: bool(regex.match(path))

    return lambda path: bool(regex.match(path.rsplit("/", 1)[-1]))


def make_path_filter(args):
    """Shared filename filtering for CAS and FUSE; excludes win."""
    find = make_find_matcher(args.find.lower() if args.ignore_case else args.find)
    includes = [make_find_matcher(g) for g in args.glob]
    excludes = [make_find_matcher(g.lstrip("!")) for g in args.exclude]

    def accepted(path):
        parts = path.split("/")
        ancestors = ["/".join(parts[:i]) for i in range(1, len(parts))]
        return (
            ".git" not in parts
            and find(path.lower() if args.ignore_case else path)
            and (not includes or any(m(path) for m in includes))
            and not any(m(p) for m in excludes for p in [path, *ancestors])
        )

    return accepted


_GLOB_REGEX_CACHE = {}
