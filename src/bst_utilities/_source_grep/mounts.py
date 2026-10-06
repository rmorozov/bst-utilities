"""Own per-run FUSE readiness, mount release and final cleanup."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import processes


def unmount_mountpoint(mountpoint: str) -> bool:
    for cmd_name in ("fusermount3", "fusermount", "umount"):
        if shutil.which(cmd_name) is None:
            continue

        cmd = [cmd_name, mountpoint] if cmd_name == "umount" else [cmd_name, "-u", mountpoint]

        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            pass

    return False


class FuseMountManager:
    def __init__(
        self,
        buildbox_fuse: str,
        cas_dir: str,
        mount_base: str,
        digest_function: str,
        keep_mounts: bool,
        force_unmount: bool,
    ):
        self.buildbox_fuse = buildbox_fuse
        self.cas_dir = cas_dir
        self.mount_base = os.path.abspath(os.path.expanduser(mount_base))
        self.digest_function = digest_function
        self.keep_mounts = keep_mounts
        self.force_unmount = force_unmount
        self.mounts = {}
        self.started = 0
        self.peak = 0
        # Content-search workers mount and release trees concurrently.
        self.lock = threading.Lock()

    def ensure_mount(self, digest_value: str) -> str:
        with self.lock:
            if digest_value in self.mounts:
                return self.mounts[digest_value][0]

        safe_digest = digest_value.replace("/", "_")
        # Never reuse another search's mount: its cleanup can race our readers.
        os.makedirs(self.mount_base, exist_ok=True)
        mountpoint = tempfile.mkdtemp(prefix=safe_digest + "-", dir=self.mount_base)

        # IMPORTANT:
        # This buildbox-fuse parser requires --option=value syntax.
        cmd = [
            self.buildbox_fuse,
            f"--local={self.cas_dir}",
            f"--input-digest-value={digest_value}",
            f"--digest-function={self.digest_function}",
            mountpoint,
        ]

        log_path = f"{mountpoint}.fuse.log"

        try:
            with open(log_path, "ab") as log_file:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=log_file,
                )
        except Exception as exc:
            raise RuntimeError(f"failed to start buildbox-fuse: {exc}") from exc

        with self.lock:
            self.started += 1
        deadline = time.monotonic() + 10.0
        # buildbox-fuse usually mounts within a few milliseconds; a fixed 50 ms
        # poll made readiness waiting dominate per-tree mount cost.
        delay = MOUNT_POLL_INITIAL

        while time.monotonic() < deadline:
            if os.path.ismount(mountpoint):
                with self.lock:
                    self.mounts[digest_value] = (mountpoint, proc, True)
                    self.peak = max(self.peak, len(self.mounts))
                return mountpoint

            if proc.poll() is not None:
                stderr_text = ""
                try:
                    stderr_text = Path(log_path).read_text(
                        encoding="utf-8",
                        errors="replace",
                    )
                except Exception:
                    pass

                raise RuntimeError(f"buildbox-fuse exited early for {digest_value}: {stderr_text}")

            time.sleep(delay)
            delay = min(delay * 2, MOUNT_POLL_MAX)

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        raise RuntimeError(f"timed out waiting for buildbox-fuse mount: {mountpoint}")

    def release(self, digest_value: str) -> None:
        """Tear down one owned mount unless the user asked to keep it."""
        with self.lock:
            mountpoint, proc, mounted_by_us = self.mounts[digest_value]
            if not self.force_unmount and (not mounted_by_us or self.keep_mounts):
                return
            del self.mounts[digest_value]
        try:
            try:
                # buildbox-fuse unmounts on SIGTERM, which avoids spawning a
                # fusermount process per tree; fall back if the mount survives.
                if proc is not None:
                    processes.stop_process(proc)
            finally:
                if os.path.ismount(mountpoint):
                    unmount_mountpoint(mountpoint)
            if os.path.ismount(mountpoint):
                print(f"WARNING: mount remains live after cleanup: {mountpoint}", file=sys.stderr)
                return
            Path(mountpoint).rmdir()
            Path(f"{mountpoint}.fuse.log").unlink(missing_ok=True)
        except Exception as exc:
            print(f"WARNING: cleanup failed for {mountpoint}: {exc}", file=sys.stderr)

    def cleanup(self):
        with self.lock:
            digests = list(self.mounts)
        for digest_value in digests:
            self.release(digest_value)


MOUNT_POLL_INITIAL = 0.001


MOUNT_POLL_MAX = 0.05
