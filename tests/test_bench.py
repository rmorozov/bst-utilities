import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_source_grep.py"
spec = importlib.util.spec_from_file_location("bench_source_grep", SCRIPT)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


@pytest.mark.parametrize(
    "run,allow,ok",
    [
        ({"returncode": 1}, False, True),
        ({"returncode": 2, "uncached elements": 3}, False, False),
        ({"returncode": 2, "uncached elements": 3}, True, True),
        ({"returncode": 2, "uncached elements": 3, "search errors": 1}, True, False),
        ({"returncode": 2}, True, False),
        ({"returncode": 130, "uncached elements": 3}, True, False),
    ],
)
def test_partial_runs_are_accepted_only_for_missing_sources(run, allow, ok):
    assert bench.acceptable(run, allow) == ok


def test_stat_lines_include_counts_and_rss():
    lines = ["  uncached elements:    11", "  peak rss:             74.5 MiB", "  load time: 2.0s"]
    parsed = [bench.STAT_LINE.match(line).groups() for line in lines]
    assert parsed == [("uncached elements", "11"), ("peak rss", "74.5"), ("load time", "2.0")]
