"""Stream child output, cancel processes and schedule bounded ordered work."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

ABORT_GRACE_SECONDS = 2.0


class SearchError(RuntimeError):
    """A failed ripgrep invocation or invalid output."""


@contextmanager
def rg_process(cmd, *, cwd=None):
    """Stream stdout, spool stderr and always reap the child before unmounting."""
    with tempfile.TemporaryFile(mode="w+b") as errors:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=errors)
        try:
            yield proc
            proc.wait()
            if proc.returncode not in (0, 1):
                errors.seek(0)
                raise SearchError("rg failed: " + errors.read(65536).decode("utf-8", "replace"))
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def iter_records(stream, null):
    """Decode rg output: text lines, or NUL-terminated file names."""
    if not null:
        for line in stream:
            yield line.decode("utf-8", "replace")
    else:
        pending = b""
        while chunk := stream.read(65536):
            records = (pending + chunk).split(b"\0")
            pending = records.pop()
            for record in records:
                yield os.fsdecode(record)
        if pending:
            raise SearchError("rg returned an unterminated filename")


def iter_rg_output(cmd, *, null=False, cwd=None):
    with rg_process(cmd, cwd=cwd) as proc:
        yield from iter_records(proc.stdout, null)


class RgRunner:
    """Run rg into a spool file from worker threads; abort() stops them all."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = set()
        self.aborted = False

    def run(self, cmd, cwd, out):
        with tempfile.TemporaryFile(mode="w+b") as errors:
            with self.lock:
                if self.aborted:
                    raise SearchError("search aborted")
                proc = subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=errors)
                self.active.add(proc)
            try:
                proc.wait()
            finally:
                if proc.poll() is None:
                    stop_process(proc)
                with self.lock:
                    self.active.discard(proc)
            if proc.returncode not in (0, 1):
                errors.seek(0)
                raise SearchError("rg failed: " + errors.read(65536).decode("utf-8", "replace"))

    def abort(self, grace=ABORT_GRACE_SECONDS):
        """Signal every running rg, then kill survivors after a bounded grace.

        Workers reap their own child (they are blocked in wait()); this only
        needs each child to exit so that their waits, and the pool, return.
        """
        with self.lock:
            self.aborted = True
            procs = list(self.active)
        for proc in procs:
            signal_process(proc, "terminate")
        deadline = time.monotonic() + grace
        for proc in procs:
            # returncode is set by the reaping worker; poll() may not get the
            # wait lock while that worker is blocked in waitpid.
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
        for proc in procs:
            if proc.poll() is None:
                signal_process(proc, "kill")


def signal_process(proc, method):
    try:
        getattr(proc, method)()
    except (ProcessLookupError, OSError):
        pass


def iter_ordered(items, jobs, work, on_abort=None):
    """
    Run work(item) on up to `jobs` threads and yield (item, result) in input order.

    At most `jobs` items run while the caller consumes the oldest finished one,
    so spooled results stay bounded. Closing the generator early (interrupt,
    closed pipe) calls on_abort, cancels queued items and waits for running ones.
    """
    pool = ThreadPoolExecutor(max_workers=jobs)
    pending = deque()
    source = iter(items)
    finished = False
    try:
        for item in source:
            pending.append((item, pool.submit(work, item)))
            if len(pending) >= jobs:
                break
        while pending:
            item, future = pending.popleft()
            # Keep `jobs` items running while the caller consumes this one.
            for nxt in source:
                pending.append((nxt, pool.submit(work, nxt)))
                break
            yield item, future.result()
        finished = True
    finally:
        if not finished:
            if on_abort is not None:
                on_abort()
            for _, future in pending:
                future.cancel()
        pool.shutdown(wait=True)


def stop_process(proc):
    if proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
