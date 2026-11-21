"""Search file contents straight from CAS blobs, each distinct blob once.

A source tree in CAS is a catalogue: Directory records name every file and
point at a content-addressed blob. Instead of mounting each tree with
buildbox-fuse and letting rg walk it, read the catalogue directly, apply the
--glob/--exclude rules to the logical paths here, and run rg over the blobs
those paths name. A blob shared by several files or trees (another version,
another --all-options set, another junction instance) is searched once.

rg treats files named on its command line differently from files it finds
while walking a directory (it searches binary files it was given instead of
skipping them), so blobs are exposed to it as a directory of symlinks that it
walks with --follow, keeping the FUSE backend's binary-file behavior.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import cas, matching, origins, processes, ripgrep

GROUP_BLOBS = 20000
"""New blobs gathered from consecutive trees before one rg run searches them."""

GROUP_FILES = 200000
"""Bound on the logical paths held for a group of trees awaiting emission."""


def farm_base():
    """Prefer a memory-backed directory: creating many symlinks on disk is slow."""
    shm = "/dev/shm"
    if os.path.isdir(shm) and os.access(shm, os.W_OK | os.X_OK):
        return shm
    return None


def rg_blob_command(args):
    cmd = ["rg", "--follow", "--hidden", "--no-ignore", "--threads", str(args.jobs)]
    if args.files_with_matches:
        cmd.extend(["--files-with-matches", "--null"])
    else:
        cmd.append("--json")
    if args.ignore_case:
        cmd.append("--ignore-case")
    if args.fixed_string:
        cmd.append("--fixed-strings")
    if args.binary_files == "text":
        cmd.append("--text")
    cmd.extend(["--", args.pattern, "."])
    return cmd


def make_farm(root, objects_dir, hashes):
    farm = tempfile.mkdtemp(prefix="group-", dir=root)
    shards = set()
    for h in hashes:
        shard = h[:2]
        if shard not in shards:
            os.mkdir(os.path.join(farm, shard))
            shards.add(shard)
        os.symlink(os.path.join(objects_dir, shard, h[2:]), os.path.join(farm, shard, h))
    return farm


def blob_hash(rg_path):
    return rg_path.rsplit("/", 1)[-1]


class BlobHits:
    """Matches per searched blob; match lines live in a spool file, not memory."""

    def __init__(self, files_mode):
        self.files_mode = files_mode
        self.searched = set()
        self.hits = {}
        self.spool = None if files_mode else tempfile.TemporaryFile(mode="w+b")

    def close(self):
        if self.spool is not None:
            self.spool.close()

    def record_files(self, lines):
        for path in lines:
            self.hits[blob_hash(path.removeprefix("./"))] = True

    def record_matches(self, lines):
        current = None
        matches = []

        def finish():
            if current is not None and matches:
                data = json.dumps(matches).encode("utf-8")
                self.spool.seek(0, os.SEEK_END)
                self.hits[current] = (self.spool.tell(), len(data))
                self.spool.write(data)

        for line in lines:
            try:
                event = json.loads(line)
                kind = event.get("type")
                if kind == "begin":
                    finish()
                    current = blob_hash(ripgrep.rg_json_text(event["data"]["path"]))
                    matches = []
                elif kind == "match":
                    data = event["data"]
                    text = ripgrep.rg_json_text(data["lines"]).removesuffix("\n")
                    matches.append([data.get("line_number"), text.removesuffix("\r")])
            except (ValueError, KeyError, TypeError) as exc:
                raise processes.SearchError(f"invalid rg JSON: {exc}") from exc
        finish()

    def matches(self, h):
        hit = self.hits.get(h)
        if hit is None or hit is True:
            return hit
        offset, length = hit
        self.spool.seek(offset)
        return json.loads(self.spool.read(length).decode("utf-8"))


def tree_entries(tree, cas_dir, matcher, stats):
    """The (path, blob) pairs of a tree that pass the filters; None if unreadable."""
    try:
        return [
            (path, h)
            for path, h, size in cas.iter_cas_file_digests(cas_dir, tree["digest_obj"])
            # An empty file has no line to match, and casd may not store its blob.
            if size and matcher(path)
        ]
    except Exception as exc:
        stats["traversal_errors"] += 1
        print(
            f"ERROR: CAS traversal failed for tree {tree.get('digest') or 'unknown'}: {exc}",
            file=sys.stderr,
        )
        return None


class GroupSearch:
    """One rg run over a group's new blobs, writing to a spool while Python
    gathers the next group; finish() waits and records its hits."""

    def __init__(self, cmd, farm, files_mode):
        self.farm = farm
        self.files_mode = files_mode
        self.out = tempfile.TemporaryFile(mode="w+b")
        self.errors = tempfile.TemporaryFile(mode="w+b")
        self.proc = None
        try:
            self.proc = subprocess.Popen(cmd, cwd=farm, stdout=self.out, stderr=self.errors)
        except BaseException:
            self.close()
            raise

    def finish(self, hits):
        try:
            self.proc.wait()
            # Record what rg produced, then report its failure.
            self.out.seek(0)
            lines = processes.iter_records(self.out, self.files_mode)
            if self.files_mode:
                hits.record_files(lines)
            else:
                hits.record_matches(lines)
            if self.proc.returncode not in (0, 1):
                self.errors.seek(0)
                message = self.errors.read(65536).decode("utf-8", "replace")
                raise processes.SearchError("rg failed: " + message)
        finally:
            self.close()

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            processes.stop_process(self.proc)
        self.out.close()
        self.errors.close()
        shutil.rmtree(self.farm, ignore_errors=True)


def search_blobs(trees, args, stats, cas_dir, emitter):
    matcher = matching.make_content_filter(args)
    objects_dir = os.path.join(cas_dir, "objects")
    files_mode = args.files_with_matches
    rg_cmd = rg_blob_command(args)
    source_info_cache = {}
    stats["search_jobs"] = args.jobs
    search_start = time.monotonic()

    def emit_tree(tree, entries, hits):
        for path, h in entries:
            found = hits.matches(h)
            if not found:
                continue
            for element_info in tree["elements"]:
                origin = origins.get_origin_for_element_path(
                    element_info, path, tree, args, source_info_cache
                )
                if files_mode:
                    emitter.emit_file(element_info, path, origin)
                else:
                    for line_number, text in found:
                        emitter.emit_match(element_info, path, line_number, text, origin)
        tree["directory"] = None
        tree["gitreview"] = None

    tree_list = list(trees.values())
    position = 0

    def gather(hits):
        """Consecutive trees until enough new blobs justify one rg run."""
        nonlocal position
        group = []
        new = {}
        files = 0
        while position < len(tree_list) and len(new) < GROUP_BLOBS and files < GROUP_FILES:
            tree = tree_list[position]
            position += 1
            entries = tree_entries(tree, cas_dir, matcher, stats)
            if entries is None:
                continue
            group.append((tree, entries))
            files += len(entries)
            for _, h in entries:
                if h not in hits.searched:
                    new[h] = None
        # Claimed now so the next group, gathered while rg runs, skips them.
        hits.searched.update(new)
        return group, list(new)

    def complete(group, running, hits):
        error = None
        if running is not None:
            try:
                running.finish(hits)
            except processes.SearchError as exc:
                error = exc
        # Like the FUSE backend, emit what rg produced before its failure.
        for tree, entries in group:
            stats["searched_files"] += len(entries)
            emit_tree(tree, entries, hits)
        if error is not None:
            stats["search_errors"] += 1
            digests = ", ".join(tree.get("digest") or "unknown" for tree, _ in group)
            print(f"ERROR: trees {digests}: {error}", file=sys.stderr)

    hits = BlobHits(files_mode)
    root = tempfile.mkdtemp(prefix="bst-source-grep-blobs-", dir=farm_base())
    previous = None
    try:
        while position < len(tree_list) or previous is not None:
            current = None
            if position < len(tree_list):
                group, new = gather(hits)
                running = None
                if new:
                    stats["rg_processes"] += 1
                    stats["searched_blobs"] += len(new)
                    running = GroupSearch(rg_cmd, make_farm(root, objects_dir, new), files_mode)
                current = (group, running)
            # The previous group's rg ran while this one was gathered.
            if previous is not None:
                pending, previous = previous, None
                complete(*pending, hits)
            previous = current
    finally:
        if previous is not None and previous[1] is not None:
            previous[1].close()
        hits.close()
        shutil.rmtree(root, ignore_errors=True)
        stats["search_seconds"] = time.monotonic() - search_start

    emitter.out.flush()

    if (
        stats["uncached_elements"] > 0
        or stats["unresolved_elements"] > 0
        or stats["traversal_errors"] > 0
        or stats["search_errors"] > 0
        or stats["no_digest_elements"] > 0
    ):
        return 2
    return 0 if stats["results"] > 0 else 1
