import sys

import pytest

from bst_utilities import source_grep as sg


class Process:
    def __init__(self):
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        self.stopped = True
        return 0


def test_mount_readiness_polls_with_backoff(tmp_path, monkeypatch):
    checks = iter([False] * 8 + [True])
    sleeps = []
    monkeypatch.setattr(sg.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(sg.os.path, "ismount", lambda path: next(checks))
    monkeypatch.setattr(sg.time, "sleep", sleeps.append)
    manager = sg.FuseMountManager("fuse", "cas", str(tmp_path), "SHA256", False, False)
    manager.ensure_mount("abcdef/42")
    assert sleeps[0] == sg.MOUNT_POLL_INITIAL
    assert sleeps == sorted(sleeps)
    assert max(sleeps) == sg.MOUNT_POLL_MAX
    # Readiness costs ~1 ms when buildbox-fuse mounts quickly, not a full 50 ms poll.
    assert sum(sleeps[:2]) < 0.005


@pytest.mark.parametrize(
    "keep,force,released", [(False, False, True), (True, False, False), (True, True, True)]
)
def test_release_tears_down_one_mount(tmp_path, monkeypatch, keep, force, released):
    unmounted = []
    monkeypatch.setattr(sg.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(sg.os.path, "ismount", lambda path: path not in unmounted)
    monkeypatch.setattr(sg, "unmount_mountpoint", lambda path: unmounted.append(path) or True)
    manager = sg.FuseMountManager("fuse", "cas", str(tmp_path), "SHA256", keep, force)
    paths = []
    for digest in ("a/1", "b/2"):
        paths.append(manager.ensure_mount(digest))
        manager.release(digest)
    assert manager.started == 2
    assert manager.peak == (1 if released else 2)
    assert unmounted == (paths if released else [])
    assert all(not sg.os.path.exists(p) for p in paths) == released
    manager.cleanup()
    assert unmounted == (paths if released else [])


def test_options_may_follow_target_and_pattern(monkeypatch, capsys):
    # Intermixed parsing reaches semantic validation instead of rejecting PATTERN.
    monkeypatch.setattr(sys, "argv", ["bst-source-grep", "x.bst", "-n", "pat", "--backend", "cas"])
    with pytest.raises(SystemExit) as exc:
        sg.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "only supports --find" in err
    assert "unrecognized arguments" not in err


def test_release_relies_on_sigterm_unmount(tmp_path, monkeypatch):
    stopped = []

    class Fuse(Process):
        def terminate(self):
            stopped.append(self)  # buildbox-fuse unmounts itself on SIGTERM
            super().terminate()

    calls = []
    monkeypatch.setattr(sg.subprocess, "Popen", lambda *a, **kw: Fuse())
    monkeypatch.setattr(sg.os.path, "ismount", lambda path: not stopped)
    monkeypatch.setattr(sg, "unmount_mountpoint", calls.append)
    manager = sg.FuseMountManager("fuse", "cas", str(tmp_path), "SHA256", False, False)
    path = manager.ensure_mount("a/1")
    manager.release("a/1")
    assert len(stopped) == 1
    assert calls == []
    assert not sg.os.path.exists(path)


def test_ordered_pool_bounds_work_and_preserves_order():
    import threading
    import time

    lock = threading.Lock()
    running = [0, 0]  # current, peak

    def work(item):
        with lock:
            running[0] += 1
            running[1] = max(running[1], running[0])
        time.sleep(0.01 * (5 - item % 5))  # later items often finish first
        with lock:
            running[0] -= 1
        return item * 10

    assert list(sg.iter_ordered(range(12), 3, work)) == [(i, i * 10) for i in range(12)]
    assert 1 < running[1] <= 3


def test_ordered_pool_aborts_and_cancels_on_early_close():
    started, aborted = [], []

    def work(item):
        started.append(item)
        return item

    gen = sg.iter_ordered(range(100), 2, work, on_abort=lambda: aborted.append(True))
    assert next(gen) == (0, 0)
    gen.close()
    assert aborted == [True]
    assert len(started) <= 4


def test_ordered_pool_does_not_abort_after_completion():
    aborted = []
    assert list(sg.iter_ordered([], 2, str, on_abort=lambda: aborted.append(1))) == []
    assert list(sg.iter_ordered([1], 2, str, on_abort=lambda: aborted.append(1))) == [(1, "1")]
    assert aborted == []


def test_rg_runner_refuses_new_processes_after_abort(tmp_path):
    runner = sg.RgRunner()
    runner.abort()
    with pytest.raises(sg.SearchError, match="aborted"):
        with open(tmp_path / "out", "wb") as out:
            runner.run([sys.executable, "-c", "pass"], str(tmp_path), out)


def test_cache_state_checked_once_per_tree_concurrently():
    from types import SimpleNamespace

    calls = []

    class Cas:
        def contains_directory(self, digest):
            calls.append(digest.hash)
            if digest.hash == "boom":
                raise RuntimeError("casd unavailable")
            return digest.hash != "missing"

    def element(hash_value):
        files = SimpleNamespace(hash=hash_value, size_bytes=1)
        cache = SimpleNamespace(load_proto=lambda accessor: SimpleNamespace(files=files))
        accessor = SimpleNamespace(
            get_files=lambda: None,
            update_resolved_state=lambda: None,
            is_resolved=lambda: True,
            _elementsourcescache=cache,
        )
        return SimpleNamespace(_sources=accessor), files

    elements, digests = zip(*(element(h) for h in ["a", "a", "missing", "boom", "b"]))
    for e in elements:
        e._sources.sources = [object()]
    cas = Cas()
    ctx = SimpleNamespace(get_cascache=lambda: cas)
    assert sg.prefetch_source_cache_state(elements, ctx, 4) == 4
    assert sorted(calls) == ["a", "b", "boom", "missing"]
    calls.clear()
    assert cas.contains_directory(digests[0]) is True
    assert cas.contains_directory(digests[2]) is False
    assert calls == []
    with pytest.raises(RuntimeError):
        cas.contains_directory(digests[3])  # failures are re-raised by the original call
    assert calls == ["boom"]
