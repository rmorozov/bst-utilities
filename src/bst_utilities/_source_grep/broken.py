"""Collect the elements, subprojects and option sets that failed, for --report-broken."""

from __future__ import annotations

import json
import os
import tempfile


def reason_name(error):
    """BuildStream's error reason (e.g. "user-assertion", "missing-file"), if any."""
    reason = getattr(error, "reason", None)
    name = getattr(reason, "name", None)
    if isinstance(name, str):
        return name.lower().replace("_", "-")
    if isinstance(reason, str) and reason:
        return reason
    return None


def error_text(error):
    if isinstance(error, str):
        return error
    return str(error) or type(error).__name__


class BrokenReport:
    """
    Failures grouped by (stage, name, error, reason), each with the effective
    option values of every option set it happened in, in first-seen order.
    """

    def __init__(self):
        self._records = {}

    def add(self, stage, name, error, options):
        message = error_text(error)
        reason = reason_name(error)
        record = self._records.setdefault(
            (stage, name, message, reason),
            {
                "stage": stage,
                "element": name,
                "error": message,
                "reason": reason,
                "option_sets": [],
            },
        )
        options = dict(options or {})
        if options not in record["option_sets"]:
            record["option_sets"].append(options)

    def __len__(self):
        return len(self._records)

    def write(self, path):
        """Write one JSON object per line, atomically."""
        directory = os.path.dirname(os.path.abspath(path))
        fd, temporary = tempfile.mkstemp(prefix=".broken-", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                for record in self._records.values():
                    stream.write(json.dumps(record, sort_keys=False) + "\n")
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(temporary, 0o666 & ~umask)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
