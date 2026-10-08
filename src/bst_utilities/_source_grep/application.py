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
    option_space,
    output,
    search_cas,
    search_fuse,
    source_cache,
)
from . import stats as metrics


def _load_selection(ctx, Project, Stream, args, selection, streams, cli_options=None):
    """Create a Stream and toplevel Project for one option set and load the target."""
    # The scheduler only runs for --fetch-subprojects; it calls both
    # callbacks unconditionally, so provide non-interactive ones.
    stream = Stream(
        ctx,
        datetime.now(),
        interrupt_callback=lambda: stream.terminate(),
        ticker_callback=lambda: None,
    )
    # Owned by the caller from here, so a failed load is still cleaned up.
    streams.append(stream)
    stream.init()
    project = adapter.create_project(Project, ctx, args, stream.fetch_subprojects, cli_options)
    stream.set_project(project)
    targets = _targets(args, project)
    if args.all_elements:
        targets = _element_targets(args, stream, project, targets)
    if args.fetch_sources:
        adapter.fetch_sources(stream, targets, selection)
    return stream, project, adapter.call_load_selection(stream, targets, selection)


def _element_targets(args, stream, project, names):
    """
    --all-elements targets without junction elements, plus with
    --include-subprojects every element of each junctioned subproject.

    A junction's sources are a whole subproject, not a recipe's sources; the
    subproject elements that are used are reached as dependencies. Junction
    targets are dropped before fetching, so an unused one is never fetched.
    """
    none = adapter.make_selection("none")
    targets = []
    seen_loaders = set()
    while names:
        roots = adapter.call_load_selection(stream, names, none)
        junctions = sorted({adapter.element_label(e) for e in roots if adapter.is_junction(e)})
        targets += [name for name in names if name not in junctions]
        names = []
        if not args.include_subprojects:
            break
        for junction in junctions:
            try:
                loader = adapter.junction_loader(project, junction)
            except Exception as exc:
                args.subproject_errors += 1
                print(f"ERROR: could not load subproject {junction}: {exc}", file=sys.stderr)
                continue
            if id(loader) in seen_loaders:
                continue
            seen_loaders.add(id(loader))
            names += [
                f"{junction}:{name}" for name in adapter.project_element_names(loader.project)
            ]
    if not targets:
        raise RuntimeError(f"no elements other than junctions under {project.element_path}")
    return targets


def _targets(args, project):
    if not args.all_elements:
        return (args.target,)
    targets = adapter.project_element_names(project)
    if not targets:
        raise RuntimeError(f"no elements found under {project.element_path}")
    return targets


def _load_option_set(ctx, Project, Stream, args, selection, streams, option_set):
    """Load one --all-options set as the toplevel project and check it took effect."""
    adapter.reset_toplevel_project(ctx)
    empty = option_space.empty_flags(option_set, args.flags_options) + args.pinned_empty
    with adapter.empty_flags_overrides(ctx, args.project_name, empty):
        _, project, elements = _load_selection(
            ctx,
            Project,
            Stream,
            args,
            selection,
            streams,
            option_space.cli_options(args.option, option_set, args.flags_options),
        )
    loaded = adapter.loaded_option_values(project, option_set)
    if loaded != option_set:
        raise RuntimeError(f"project resolved options to [{option_space.label(loaded)}]")
    return project, elements


def _prepare_options(args):
    """
    Read the declarations and merge --options-file with -o.

    Sets args.option to the pins passed on the command line (file pins first,
    so -o wins), args.pinned_empty to pinned empty flags (applied as
    overrides, which the command line cannot express) and args.restrictions.
    Returns an exit status on error, else None.
    """
    args.project_name, args.declarations, junction_includes = adapter.declared_options(
        args.directory
    )
    for name in junction_includes:
        print(
            f"NOTE: project.conf includes {name} from a junction; options declared "
            "there are not listed or enumerated",
            file=sys.stderr,
        )
    args.flags_options = {d.name for d in args.declarations if d.type in option_space.SET_TYPES}
    pins, restrictions = {}, {}
    if args.options_file:
        try:
            raw = adapter.load_options_file(args.options_file)
            pins, restrictions = option_space.parse_options_file(raw, args.declarations)
        except option_space.OptionsFileError as exc:
            print(f"error: {args.options_file}: {exc}", file=sys.stderr)
            return 2

    for name, value in args.option:
        pins[name] = value
        restrictions.pop(name, None)

    if not (args.all_options or args.list_options or args.options_template):
        # Without enumeration, a one-value list is just a pin.
        for name in [name for name, values in restrictions.items() if len(values) == 1]:
            pins[name] = restrictions.pop(name)[0]

    if restrictions and not (args.all_options or args.list_options or args.options_template):
        names = ", ".join(restrictions)
        print(
            f"error: {args.options_file} lists several values for {names}; "
            "that needs --all-options",
            file=sys.stderr,
        )
        return 2

    args.pinned_empty = [
        name for name, value in pins.items() if value == "" and name in args.flags_options
    ]
    args.option = [(name, value) for name, value in pins.items() if name not in args.pinned_empty]
    args.pins = pins
    args.restrictions = restrictions
    return None


def _describe_options(args):
    """--list-options and --options-template: print and exit."""
    try:
        adapter.load_api()
        status = _prepare_options(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if status is not None:
        return status
    if args.options_template:
        text = option_space.render_template(
            args.project_name, args.declarations, args.pins, args.restrictions
        )
    else:
        text = option_space.render_listing(
            args.declarations, args.pins, args.restrictions, _only_listed(args)
        )
    sys.stdout.write(text)
    return 0


def _only_listed(args):
    """Whether options the options file leaves out are held, not enumerated."""
    return bool(args.options_file) and args.unlisted_options == "keep"


def _plan_option_sets(args, stats):
    """Return the option sets to load for --all-options, or None on error."""
    declarations = args.declarations
    axes, held = option_space.plan(declarations, args.pins, args.restrictions, _only_listed(args))
    args.planned_options = {d.name for d in declarations} | set(args.pins)

    for name, reason in held:
        if reason not in ("pinned", option_space.NOT_IN_FILE):
            print(f"NOTE: option {name} is held at its configured value: {reason}", file=sys.stderr)
    kept = [name for name, reason in held if reason == option_space.NOT_IN_FILE]
    if kept:
        print(
            f"NOTE: {len(kept)} option(s) not in {args.options_file} keep their configured "
            f"value: {', '.join(kept)} (--unlisted-options vary tries every value)",
            file=sys.stderr,
        )

    size = option_space.space_size(axes)
    if size > args.max_option_sets:
        counts = ", ".join(f"{axis.name}={axis.count}" for axis in axes)
        print(
            f"error: --all-options would load {size} option sets ({counts}), more than "
            f"--max-option-sets {args.max_option_sets}\n"
            "hint: pin or narrow options with -o KEY VALUE or --options-file "
            "(start from --options-template), or raise --max-option-sets",
            file=sys.stderr,
        )
        return None

    stats["option_sets_planned"] = size
    # Lazily: a large --max-option-sets must not materialize every set up front.
    return option_space.iter_option_sets(axes, declarations)


def _duration(seconds):
    seconds = int(seconds + 0.5)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m"
    days = hours / 24
    if days < 730:
        return f"{days:.0f} days"
    return f"{days / 365:.0f} years"


class _Progress:
    """Estimate and report how long the remaining option sets will take."""

    INTERVAL = 30.0

    def __init__(self, total, clock=time.monotonic, stream=None):
        self.total = total
        self.done = 0
        self.clock = clock
        self.stream = stream
        self.start = clock()
        self.last_report = self.start

    def step(self):
        self.done += 1
        if self.total <= 1 or self.done >= self.total:
            return
        now = self.clock()
        if self.done > 1 and now - self.last_report < self.INTERVAL:
            return
        self.last_report = now
        elapsed = now - self.start
        left = elapsed / self.done * (self.total - self.done)
        print(
            f"NOTE: loaded {self.done}/{self.total} option sets in {_duration(elapsed)}; "
            f"about {_duration(left)} left",
            file=self.stream or sys.stderr,
        )


def _report_option_set_failure(args, stats, option_set, exc):
    label = option_space.label(option_set)
    if adapter.is_user_assertion(exc):
        # The project declares this combination unsupported with (!).
        stats["option_sets_skipped"] += 1
        print(f"NOTE: skipped option set [{label}]: {exc}", file=sys.stderr)
        return

    stats["option_set_errors"] += 1
    if args.traceback:
        traceback.print_exc()
    print(f"ERROR: could not load option set [{label}]: {exc}", file=sys.stderr)


def _warn_unplanned_options(args, project):
    """Options declared in junction includes were not enumerated; say so once each."""
    for name in sorted(adapter.project_option_names(project) - args.planned_options):
        args.planned_options.add(name)
        print(
            f"NOTE: option {name} is declared in a file included from a junction "
            "and was not enumerated; pin it with -o to search another value",
            file=sys.stderr,
        )


def _with_load_errors(exit_code, stats, args):
    """An option set or subproject that failed to load makes any search incomplete."""
    if stats["option_set_errors"] > 0 or args.subproject_errors > 0:
        return 2
    return exit_code


def _main() -> int:
    args = cli.parse_args()

    if args.list_options or args.options_template:
        return _describe_options(args)

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

    args.pinned_empty = []
    args.subproject_errors = 0
    if args.all_options or args.options_file:
        try:
            status = _prepare_options(args)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if status is not None:
            return status

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
    streams = []
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

                option_sets = [None]
                if args.all_options:
                    option_sets = _plan_option_sets(args, stats)
                    if option_sets is None:
                        exit_code = 2
                        return exit_code
                progress = _Progress(stats.get("option_sets_planned", 1))

                trees = {}
                loaded_any = False
                discover_seconds = 0.0
                for option_set in option_sets:
                    try:
                        if option_set is None:
                            with adapter.empty_flags_overrides(
                                ctx, getattr(args, "project_name", None), args.pinned_empty
                            ):
                                _, project, elements = _load_selection(
                                    ctx, Project, Stream, args, selection, streams
                                )
                        else:
                            project, elements = _load_option_set(
                                ctx, Project, Stream, args, selection, streams, option_set
                            )
                    except (KeyboardInterrupt, BrokenPipeError):
                        raise
                    except Exception as exc:
                        if option_set is None:
                            raise
                        _report_option_set_failure(args, stats, option_set, exc)
                        progress.step()
                        continue
                    if option_set is not None:
                        stats["option_sets"] += 1
                        _warn_unplanned_options(args, project)
                    elapsed = time.monotonic() - load_start
                    stats["project_load_seconds"] = (
                        elapsed - stats["cache_check_seconds"] - discover_seconds
                    )

                    phase_start = time.monotonic()
                    stats["cache_checks"] += source_cache.prefetch_source_cache_state(
                        elements,
                        ctx,
                        min(source_cache.CACHE_CHECK_WORKERS, 2 * (os.cpu_count() or 1)),
                        shared=args.all_options,
                    )
                    stats["cache_check_seconds"] += time.monotonic() - phase_start
                    phase_start = time.monotonic()
                    catalogue.discover_trees(elements, args, stats, use_fuse, trees, option_set)
                    discover_seconds += time.monotonic() - phase_start
                    loaded_any = True
                    if option_set is not None:
                        # Keep only what the trees reference, so memory does not
                        # grow with every option set.
                        del project, elements
                        adapter.release_load_state()
                        del streams[1:]
                    progress.step()

                if not loaded_any:
                    print("error: no option set could be loaded", file=sys.stderr)
                    exit_code = 2
                    return exit_code

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

                stats["load_seconds"] = time.monotonic() - load_start

                exit_code = 1
                if (
                    stats["option_set_errors"] > 0
                    or args.subproject_errors > 0
                    or stats["uncached_elements"] > 0
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
                    exit_code = search_cas.search_cas(trees, args, stats, cas_dir, emitter)
                    return _with_load_errors(exit_code, stats, args)

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
                exit_code = _with_load_errors(exit_code, stats, args)

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
                    for stream in streams:
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
