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


class FakeRpc:
    """A FetchTree future that blocks until it is resolved or cancelled."""

    def __init__(self, outcome):
        import threading

        self.outcome = outcome
        self.cancelled = False
        self.done = threading.Event()
        if outcome != "block":
            self.done.set()

    def result(self):
        # grpc futures wait in short slices, so a signal can interrupt them.
        while not self.done.wait(0.05):
            pass
        if self.cancelled:
            raise RuntimeError("cancelled")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def cancel(self):
        self.cancelled = True
        self.done.set()


def cache_check_fixture(outcomes):
    from types import SimpleNamespace

    pytest.importorskip("buildstream")
    import grpc

    class NotFound(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.NOT_FOUND

    class Unavailable(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.UNAVAILABLE

    errors = {"missing": NotFound(), "boom": Unavailable()}
    rpcs, calls = {}, []

    def future(request):
        h = request.root_digest.hash
        rpcs[h] = FakeRpc(errors.get(outcomes[h], outcomes[h]))
        return rpcs[h]

    class Cas:
        _remote_cache = False
        _casd = SimpleNamespace(
            get_local_cas=lambda: SimpleNamespace(FetchTree=SimpleNamespace(future=future))
        )

        def contains_directory(self, digest):
            calls.append(digest.hash)
            return "original"

    from buildstream._protos.build.bazel.remote.execution.v2 import remote_execution_pb2

    def element(h):
        files = remote_execution_pb2.Digest(hash=h, size_bytes=1)
        cache = SimpleNamespace(load_proto=lambda accessor: SimpleNamespace(files=files))
        accessor = SimpleNamespace(
            get_files=lambda: None,
            update_resolved_state=lambda: None,
            is_resolved=lambda: True,
            _elementsourcescache=cache,
            sources=[object()],
        )
        return SimpleNamespace(_sources=accessor), files

    cas = Cas()
    return cas, SimpleNamespace(get_cascache=lambda: cas), element, rpcs, calls


def test_cache_state_checked_once_per_tree():
    outcomes = {"a": None, "missing": "missing", "boom": "boom", "b": None}
    cas, ctx, element, rpcs, calls = cache_check_fixture(outcomes)
    elements, digests = zip(*(element(h) for h in ["a", "a", "missing", "boom", "b"]))
    assert sg.prefetch_source_cache_state(elements, ctx, 2) == 4
    assert sorted(rpcs) == ["a", "b", "boom", "missing"]
    assert cas.contains_directory(digests[0]) is True
    assert cas.contains_directory(digests[2]) is False
    assert calls == []
    # Indefinite answers fall back to BuildStream's own call and error handling.
    assert cas.contains_directory(digests[3]) == "original"
    assert calls == ["boom"]


def test_cache_checks_cancel_in_flight_rpcs_on_interrupt():
    import os
    import signal
    import threading

    outcomes = {h: "block" for h in "abcd"}
    cas, ctx, element, rpcs, _ = cache_check_fixture(outcomes)
    elements = [element(h)[0] for h in "abcd"]
    timer = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            sg.prefetch_source_cache_state(elements, ctx, 3)
    finally:
        timer.cancel()
    # Only `workers` RPCs were started and every one was cancelled.
    assert len(rpcs) == 3
    assert all(rpc.cancelled for rpc in rpcs.values())


def test_rg_abort_kills_children_that_ignore_sigterm(tmp_path):
    import threading
    import time

    child = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    runner = sg.RgRunner()
    out = open(tmp_path / "out", "w+b")
    errors = []

    def work():
        try:
            runner.run([sys.executable, "-c", child], str(tmp_path), out)
        except sg.SearchError as exc:
            errors.append(exc)

    worker = threading.Thread(target=work)
    worker.start()
    deadline = time.monotonic() + 10
    while (not runner.active or b"ready" not in (tmp_path / "out").read_bytes()) and (
        time.monotonic() < deadline
    ):
        time.sleep(0.01)
    (proc,) = list(runner.active)
    start = time.monotonic()
    runner.abort(grace=0.2)
    worker.join(timeout=5)
    out.close()
    assert not worker.is_alive()
    assert time.monotonic() - start < 5
    assert proc.returncode is not None and errors
