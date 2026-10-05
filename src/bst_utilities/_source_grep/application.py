"""Own the BuildStream session and dispatch the selected search backend."""

from __future__ import annotations

import os
import shutil
import sys
import time
import traceback
from contextlib import closing, nullcontext
from datetime import datetime

from . import (
    adapter,
    cas_layout,
    catalogue,
    cli,
    discovery,
    mounts,
    output,
    search_cas,
    search_fuse,
    source_cache,
)
from . import stats as metrics


def _main() -> int:
    args = cli.parse_args()

    for stream_name in (sys.stdout, sys.stderr):
        if hasattr(stream_name, "reconfigure"):
            try:
                stream_name.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    start_time = time.monotonic()

    use_fuse = True
    if args.find is not None and args.backend in ("auto", "cas"):
        use_fuse = False

    buildbox_fuse = None

    if use_fuse:
        if shutil.which("rg") is None:
            print(
                "error: ripgrep (`rg`) is required for FUSE/content search but was not found",
                file=sys.stderr,
            )
            return 2

        buildbox_fuse = discovery.find_buildbox_fuse(args)

        if buildbox_fuse is None or not os.path.isfile(buildbox_fuse):
            print(
                "error: could not find buildbox-fuse.\n"
                "hint: pass --buildbox-fuse /path/to/buildbox-fuse\n"
                "hint: or set BUILDBOX_FUSE=/path/to/buildbox-fuse",
                file=sys.stderr,
            )
            return 2

        if not os.access(buildbox_fuse, os.X_OK):
            print(
                f"error: buildbox-fuse found but is not executable: {buildbox_fuse}",
                file=sys.stderr,
            )
            print(
                "hint: chmod +x that file, or provide an executable wrapper via --buildbox-fuse",
                file=sys.stderr,
            )
            return 2

    stats = metrics.new_stats()

    try:
        Context, Project, Stream = adapter.load_api()
    except Exception as exc:
        print(
            f"error: unable to import BuildStream internals: {exc}",
            file=sys.stderr,
        )
        return 2

    selection = adapter.make_selection(args.deps)

    context = Context()
    adapter.attach_dummy_message_handler(context)

    if hasattr(context, "__enter__") and hasattr(context, "__exit__"):
        ctx_manager = context
    elif hasattr(context, "close"):
        ctx_manager = closing(context)
    else:
        ctx_manager = nullcontext(context)

    exit_code = 2
    stream = None
    mount_manager = None
    out = output.LineBuffer()

    try:
        with ctx_manager as ctx:
            if ctx is None:
                ctx = context

            try:
                load_start = time.monotonic()

                ctx.load(args.config)
                adapter.attach_dummy_message_handler(ctx)

                # The scheduler only runs for --fetch-subprojects; it calls both
                # callbacks unconditionally, so provide non-interactive ones.
                stream = Stream(
                    ctx,
                    datetime.now(),
                    interrupt_callback=lambda: stream.terminate(),
                    ticker_callback=lambda: None,
                )
                stream.init()
                project = adapter.create_project(Project, ctx, args, stream.fetch_subprojects)
                stream.set_project(project)

                elements = adapter.call_load_selection(stream, args.target, selection)
                stats["project_load_seconds"] = time.monotonic() - load_start
                phase_start = time.monotonic()
                stats["cache_checks"] = source_cache.prefetch_source_cache_state(
                    elements, ctx, min(source_cache.CACHE_CHECK_WORKERS, 2 * (os.cpu_count() or 1))
                )
                stats["cache_check_seconds"] = time.monotonic() - phase_start

                cas_dir = None

                if not use_fuse:
                    # Optional fast path for CAS-direct traversal; fall back to
                    # BuildStream directory objects when the layout is unknown.
                    try:
                        cas_dir = cas_layout.find_cas_dir(ctx, args)
                    except Exception:
                        cas_dir = None
                    if cas_dir and not os.path.isdir(os.path.join(cas_dir, "objects")):
                        cas_dir = None

                if use_fuse:
                    cas_dir = cas_layout.find_cas_dir(ctx, args)

                    if not os.path.isdir(cas_dir):
                        print(
                            f"error: CAS directory does not exist: {cas_dir}\n"
                            "hint: use --cas-dir to specify it explicitly",
                            file=sys.stderr,
                        )
                        exit_code = 2
                        return exit_code

                trees = catalogue.discover_trees(elements, args, stats, use_fuse)

                stats["load_seconds"] = time.monotonic() - load_start

                exit_code = 1
                if (
                    stats["uncached_elements"] > 0
                    or stats["unresolved_elements"] > 0
                    or (use_fuse and stats["no_digest_elements"] > 0)
                ):
                    exit_code = 2

                if not trees:
                    if exit_code != 2:
                        exit_code = 1
                    return exit_code

                # ------------------------------------------------------------
                # Emission helpers
                # ------------------------------------------------------------

                emitter = output.ResultEmitter(args, out, stats)

                # ------------------------------------------------------------
                # CAS-direct filename search
                # ------------------------------------------------------------

                if not use_fuse:
                    return search_cas.search_cas(trees, args, stats, cas_dir, emitter)

                # ------------------------------------------------------------
                # FUSE mount phase
                # ------------------------------------------------------------

                mount_manager = mounts.FuseMountManager(
                    buildbox_fuse=buildbox_fuse,
                    cas_dir=cas_dir,
                    mount_base=args.mount_dir,
                    digest_function=args.digest_function,
                    keep_mounts=args.keep_mounts,
                    force_unmount=args.force_unmount,
                )

                exit_code = search_fuse.search_fuse(trees, args, stats, mount_manager, emitter)

            except (KeyboardInterrupt, BrokenPipeError):
                raise

            except Exception as exc:
                if args.traceback:
                    traceback.print_exc()
                else:
                    print(f"ERROR: {exc!r}", file=sys.stderr)
                    print(
                        "Hint: rerun with --traceback for details.",
                        file=sys.stderr,
                    )
                exit_code = 2

            finally:
                try:
                    if mount_manager is not None:
                        mount_manager.cleanup()
                        stats["peak_mounts"] = mount_manager.peak
                        stats["fuse_processes"] = mount_manager.started
                finally:
                    adapter.cleanup_stream(stream)

    except BrokenPipeError:
        raise

    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        exit_code = 130

    except Exception as exc:
        if args.traceback:
            traceback.print_exc()
        else:
            print(f"ERROR: {exc!r}", file=sys.stderr)
            print(
                "Hint: rerun with --traceback for details.",
                file=sys.stderr,
            )
        exit_code = 2

    finally:
        out.flush()
        sys.stdout.flush()

        stats["total_seconds"] = time.monotonic() - start_time
        stats["peak_rss_mib"] = metrics.peak_rss_mib()

        if args.stats:
            metrics.print_stats(stats)

    return exit_code


def main() -> int:
    try:
        return _main()
    except BrokenPipeError:
        # Prevent interpreter shutdown from flushing the broken stdout again.
        try:
            with open(os.devnull, "wb") as sink:
                os.dup2(sink.fileno(), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 141
