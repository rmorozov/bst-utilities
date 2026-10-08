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
    broken,
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
    args.current_project = project
    targets = _targets(args, project)
    if args.fetch_sources:

        def load(names):
            return adapter.load_for_fetch(stream, names, selection)
    else:

        def load(names):
            return adapter.call_load_selection(stream, names, selection)

    if args.all_elements:
        elements = _load_isolated(args, project, _element_targets(args, project, targets), load)
    else:
        elements = load(targets)
    if args.fetch_sources:
        # Elements whose sources failed to fetch are searched as uncached.
        for name, reason in adapter.fetch_loaded(stream, elements).items():
            _report_broken(args, "fetch", name, reason)
    return stream, project, elements


_BROKEN_MESSAGES = {
    "read": "could not load {}",
    "load": "could not load {}",
    "subproject": "could not load subproject {}",
    "fetch": "could not fetch sources of {}",
}


def _report_broken(args, stage, name, error):
    """Report an element, subproject or fetch that failed; the search goes on without it."""
    args.load_errors += 1
    message = _BROKEN_MESSAGES[stage].format(name)
    print(f"ERROR: {message}: {broken.error_text(error)}", file=sys.stderr)
    if args.broken is not None:
        args.broken.add(stage, name, error, adapter.effective_options(args.current_project))


def _load_isolated(args, project, targets, load):
    """
    Load --all-elements targets, skipping the ones that cannot be loaded.

    Listing reads recipes only; plugin and configuration errors appear when
    elements are instantiated. If the whole selection fails, halves are
    loaded until each failing target is found and reported, then the rest is
    loaded once more as one graph.
    """
    try:
        return load(targets)
    except Exception as exc:
        _drop_partial_load(project)
        loadable = _loadable_targets(args, project, targets, load, exc)
    if not loadable:
        raise RuntimeError("none of the listed elements could be loaded")
    return load(loadable)


def _loadable_targets(args, project, names, load, error):
    """The targets among `names`, which failed together with `error`, that load."""
    if len(names) == 1:
        _report_broken(args, "load", names[0], error)
        return []
    loadable = []
    middle = len(names) // 2
    for half in (names[:middle], names[middle:]):
        try:
            load(half)
        except Exception as exc:
            _drop_partial_load(project)
            loadable += _loadable_targets(args, project, half, load, exc)
        else:
            adapter.release_load_state()
            loadable += half
    return loadable


def _drop_partial_load(project):
    """Forget what a failed load left in the loaders and the element map."""
    adapter.reset_loader_caches(project)
    adapter.release_load_state()


def _element_targets(args, project, names):
    """
    --all-elements targets without junction elements, plus with
    --include-subprojects every element of each junctioned subproject.

    A junction's sources are a whole subproject, not a recipe's sources; the
    subproject elements that are used are reached as dependencies. Junction
    targets, including links to junctions, are dropped before fetching, so an
    unused one is never fetched. A recipe or subproject that cannot load is
    reported and skipped; the rest is still searched.
    """
    targets = []
    seen_loaders = set()
    batches = [names]
    while batches:
        subprojects = []
        for batch in batches:
            roots = _load_roots(args, project, batch)
            targets += [name for name, (kind, _) in roots if kind != "junction"]
            if not args.include_subprojects:
                continue
            junctions = {full_name for _, (kind, full_name) in roots if kind == "junction"}
            for junction in sorted(junctions):
                try:
                    loader = adapter.junction_loader(project, junction)
                except Exception as exc:
                    adapter.reset_loader_caches(project)
                    _report_broken(args, "subproject", junction, exc)
                    continue
                if id(loader) in seen_loaders:
                    continue
                seen_loaders.add(id(loader))
                subprojects.append(
                    [f"{junction}:{name}" for name in adapter.project_element_names(loader.project)]
                )
        batches = subprojects
    if not targets:
        raise RuntimeError(
            f"no loadable elements other than junctions under {project.element_path}"
        )
    return targets


def _load_roots(args, project, names):
    """
    (name, (kind, full name)) for each of `names` that loads, links resolved.

    Reads recipes with BuildStream's loader only: no element or source is
    instantiated, so listing a large project stays cheap. A batch that fails
    is split in halves until each failing name is found and reported; the
    partial loader state of every failure is dropped before the next load.
    """
    try:
        return list(zip(names, adapter.load_recipes(project, names)))
    except Exception as exc:
        adapter.reset_loader_caches(project)
        if len(names) == 1:
            _report_broken(args, "read", names[0], exc)
            return []
    middle = len(names) // 2
    return _load_roots(args, project, names[:middle]) + _load_roots(args, project, names[middle:])


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
    if args.broken is not None:
        project = args.current_project
        options = adapter.effective_options(project) if project is not None else None
        args.broken.add("option-set", None, exc, options or option_set)
    if adapter.is_user_assertion(exc):
        # The project declares this combination unsupported with (!).
        stats["option_sets_skipped"] += 1
        print(f"NOTE: skipped option set [{label}]: {exc}", file=sys.stderr)
        return

    stats["option_set_errors"] += 1
    if args.traceback:
        traceback.print_exc()
    print(
        f"ERROR: could not load option set [{label}]: {str(exc) or type(exc).__name__}",
        file=sys.stderr,
    )


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
    """An option set, recipe or subproject that failed to load makes any search incomplete."""
    if stats["option_set_errors"] > 0 or args.load_errors > 0:
        return 2
    return exit_code


def _report_destination_problem(path):
    """Why --report-broken cannot be published at path, or None."""
    if os.path.lexists(path) and not os.path.isfile(path):
        return f"--report-broken {path} exists and is not a regular file"
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        return f"--report-broken directory {directory} does not exist"
    if not os.access(directory, os.W_OK | os.X_OK):
        return f"cannot write --report-broken into {directory}"
    return None


def _publish_report(args) -> bool:
    """Write the --report-broken file; False when it was requested but not written."""
    if args.broken is None:
        return True
    try:
        args.broken.write(args.report_broken)
    except OSError as exc:
        print(f"error: could not write {args.report_broken}: {exc}", file=sys.stderr)
        return False
    return True


def _main() -> int:
    args = cli.parse_args()

    if args.list_options or args.options_template:
        return _describe_options(args)

    args.broken = None
    if args.report_broken:
        # Fail before searching rather than lose the report at the end.
        problem = _report_destination_problem(args.report_broken)
        if problem is not None:
            print(f"error: {problem}", file=sys.stderr)
            return 2
        args.broken = broken.BrokenReport()

    # Publish the report on every exit path, including a closed stdout and an
    # interrupt, independently of flushing search output.
    try:
        status = _search(args)
    finally:
        published = _publish_report(args)
    return status if published else 2


def _search(args) -> int:
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
    args.load_errors = 0
    args.current_project = None
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
                    args.current_project = None
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
                    or args.load_errors > 0
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
