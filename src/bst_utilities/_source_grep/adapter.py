"""Version-specific BuildStream session, project and source metadata access."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
from contextlib import contextmanager


def bst_get(node, key):
    if node is None:
        return None

    if isinstance(node, dict):
        return node.get(key)

    getter = getattr(node, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except Exception:
            return None

    return None


def bst_value(obj, names):
    """
    Fetch a scalar-ish value from an object or mapping.

    IMPORTANT:
    This intentionally does NOT call callables. BuildStream source objects
    can have methods such as track() or fetch(), and calling those may access
    the network.
    """
    for name in names:
        value = None

        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            try:
                value = getattr(obj, name, None)
            except Exception:
                value = None

            if value is None:
                value = bst_get(obj, name)

        if callable(value):
            continue

        if value is not None and str(value) != "":
            return value

    return None


def get_source_objects(element, accessor):
    candidate_attrs = (
        "_sources",
        "sources",
        "_source_list",
        "_source_objects",
        "_ElementSources__sources",
    )

    for container in (accessor, element):
        if container is None:
            continue

        for attr in candidate_attrs:
            try:
                value = getattr(container, attr, None)
            except Exception:
                continue

            if isinstance(value, (list, tuple)):
                return list(value)

            if (
                value is not None
                and hasattr(value, "__iter__")
                and not isinstance(value, (str, bytes, dict))
            ):
                try:
                    return list(value)
                except Exception:
                    pass

    if accessor is not None:
        try:
            return list(accessor)
        except Exception:
            pass

    return []


class DummyMessageHandler:
    def __call__(self, *args, **kwargs):
        pass

    def message(self, *args, **kwargs):
        pass

    def handle_message(self, *args, **kwargs):
        pass


def attach_dummy_message_handler(context) -> bool:
    dummy = DummyMessageHandler()

    candidates = [context]

    try:
        messenger = getattr(context, "messenger", None)
        if messenger is not None:
            candidates.append(messenger)
    except Exception:
        pass

    for obj in candidates:
        if obj is None:
            continue

        for method_name in ("set_message_handler", "set_handler"):
            method = getattr(obj, method_name, None)
            if callable(method):
                try:
                    method(dummy)
                    return True
                except Exception:
                    pass

        for attr_name in (
            "_message_handler",
            "message_handler",
            "_handler",
            "handler",
        ):
            if hasattr(obj, attr_name):
                try:
                    setattr(obj, attr_name, dummy)
                    return True
                except Exception:
                    pass

    return False


def element_label(element) -> str:
    for name in ("_get_full_name", "get_full_name"):
        try:
            value = getattr(element, name, None)
            if value is not None:
                return value() if callable(value) else str(value)
        except Exception:
            pass

    try:
        name = getattr(element, "name", None)
        if name is not None:
            return str(name)
    except Exception:
        pass

    return str(element)


def get_pipeline_selection_class():
    for module_name in (
        "buildstream.types",
        "buildstream._types",
        "buildstream._stream",
        "buildstream",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue

        for class_name in ("PipelineSelection", "_PipelineSelection"):
            obj = getattr(module, class_name, None)
            if obj is not None:
                return obj

    return None


def make_selection(choice: str):
    cls = get_pipeline_selection_class()

    if cls is None:
        return choice

    members = getattr(cls, "__members__", None)
    if members is not None:
        upper = choice.upper()

        if upper in members:
            return members[upper]

        for member in members.values():
            if getattr(member, "value", None) == choice:
                return member

    upper_attr = getattr(cls, choice.upper(), None)
    if upper_attr is not None:
        return upper_attr

    try:
        return cls(choice)
    except Exception:
        pass

    try:
        return cls[choice.upper()]
    except Exception:
        pass

    return choice


def create_project(Project, context, args, fetch_subprojects=None, cli_options=None):
    # BuildStream expects a callback here, not the CLI's boolean.
    def refuse_fetch(junctions):
        names = ", ".join(sorted(element_label(j) for j in junctions)) or "unknown junction"
        raise RuntimeError(
            f"Subproject sources are missing for {names}; fetch them with bst "
            "(e.g. bst source fetch on a junction or any element inside it) "
            "or pass --fetch-subprojects to allow fetching"
        )

    callback = fetch_subprojects if args.fetch_subprojects else refuse_fetch
    if callback is None:
        raise RuntimeError("No BuildStream subproject fetch callback is available")
    return Project(
        args.directory,
        context,
        cli_options=list(dict(args.option).items()) if cli_options is None else cli_options,
        fetch_subprojects=callback,
    )


def call_load_selection(stream, target: str, selection):
    return list(
        stream.load_selection(
            (target,),
            selection=selection,
            connect_artifact_cache=False,
            connect_source_cache=False,
            need_state=False,
        )
    )


def cleanup_stream(stream) -> None:
    if stream is None:
        return

    cleanup = getattr(stream, "cleanup", None)
    if callable(cleanup):
        try:
            cleanup()
        except Exception:
            pass


def load_api():
    version = importlib.metadata.version("BuildStream")
    from packaging.version import Version

    if not Version("2.8") <= Version(version) < Version("3"):
        raise RuntimeError(f"BuildStream {version} is unsupported; use >=2.8,<3")
    from buildstream._context import Context
    from buildstream._project import Project
    from buildstream._stream import Stream

    return Context, Project, Stream


def declared_options(directory):
    """
    Read the option declarations of the project containing `directory`.

    Parses project.conf with BuildStream's own YAML and option classes, without
    resolving values: an arch/os option whose host default is not listed would
    otherwise fail before any combination could be chosen. `(@)` includes of
    the project's own files are followed as BuildStream's first loading pass
    does. Files included from a junction need that subproject loaded, so
    options declared there are not visible here; see project_option_names().
    Returns (project name, declarations, junction includes left unread).
    """
    from buildstream import _yaml, utils
    from buildstream._options import OptionPool

    project_dir, _ = utils._search_upward_for_files(directory, ["project.conf"])
    if project_dir is None:
        raise RuntimeError(f"no project.conf found in {directory!r} or its parents")

    node = _yaml.load(os.path.join(project_dir, "project.conf"), shortname="project.conf")
    junction_includes = _process_local_includes(node, project_dir)
    pool = OptionPool(os.path.join(project_dir, node.get_str("element-path", default=".")))
    pool.load(node.get_mapping("options", default={}))

    from .option_space import Declaration

    declarations = []
    for name, option in pool._options.items():
        values = tuple(getattr(option, "values", None) or ())
        declarations.append(
            Declaration(name, option.OPTION_TYPE, values, _option_cli_value(option))
        )
    return node.get_str("name"), declarations, junction_includes


def _process_local_includes(node, project_dir):
    """
    Compose `(@)` includes of project-local files into `node`, in place.

    Uses BuildStream's own Includes, which resolves paths, nesting, recursion
    errors and composition order. Junction includes are recorded and replaced
    with nothing instead of loading the subproject. Returns their names.
    """
    from buildstream._includes import Includes
    from buildstream.node import Node

    junction_includes = []

    class _Project:
        directory = project_dir
        junction = None

    class _Loader:
        project = _Project()

    class _LocalIncludes(Includes):
        def _include_file(self, include, loader):
            name = include.as_str()
            if ":" in name:
                junction_includes.append(name)
                return Node.from_dict({}), f"junction include {name}", loader
            return super()._include_file(include, loader)

    _LocalIncludes(_Loader()).process(node, process_project_options=False)
    return junction_includes


def _option_cli_value(option):
    """An option's current value in the command-line form option_space uses."""
    if option.OPTION_TYPE == "bool":
        return "true" if option.value else "false"
    if option.OPTION_TYPE in ("flags", "element-mask"):
        return ",".join(sorted(option.value or ()))
    return option.value


def loaded_option_values(project, names):
    """Resolved values of `names` in a loaded project, in command-line form."""
    options = project.options._options
    return {name: _option_cli_value(options[name]) for name in names}


@contextmanager
def empty_flags_overrides(context, project_name, names):
    """
    Give flags options `names` an empty value while a project loads.

    The command line cannot express an empty flags value, and leaving the
    option out would inherit a user-configuration value instead. BuildStream
    applies user-configuration options before command-line ones, so this sets
    them through a copy of the context's project overrides.
    """
    if not names:
        yield
        return

    original = getattr(context, "_project_overrides", None)
    if original is None or not hasattr(original, "clone"):
        raise RuntimeError("this BuildStream version cannot override empty flags options")

    overrides = original.clone()
    if overrides.get_mapping(project_name, default=None) is None:
        overrides[project_name] = {}
    project = overrides.get_mapping(project_name)
    if project.get_mapping("options", default=None) is None:
        project["options"] = {}
    options = project.get_mapping("options")
    for name in names:
        options[name] = []

    context._project_overrides = overrides
    try:
        yield
    finally:
        context._project_overrides = original


def project_option_names(project):
    """Names of every option the loaded project declares, including included ones."""
    try:
        return set(project.options._options)
    except Exception:
        return set()


def reset_toplevel_project(context) -> None:
    """
    Let the next Project created on `context` become its toplevel project.

    BuildStream treats the first project added to a Context as the toplevel
    (project.refs lookup, junction overrides). Loading another option set in
    the same session must not resolve refs through a previous set's project.
    """
    projects = getattr(context, "_projects", None)
    if not isinstance(projects, list):
        raise RuntimeError("this BuildStream version cannot load several option sets")
    context._projects = []


def is_user_assertion(exc) -> bool:
    """Whether a load failed on a project `(!)` assertion for these options."""
    return getattr(getattr(exc, "reason", None), "name", None) == "USER_ASSERTION"


def load_options_file(path):
    """The `options` mapping of an --options-file, as plain Python values."""
    from buildstream import _yaml

    node = _yaml.load(os.path.abspath(path), shortname=os.path.basename(path))
    data = node.strip_node_info()
    unknown = sorted(set(data) - {"options"})
    if unknown:
        from .option_space import OptionsFileError

        raise OptionsFileError(f"unknown top-level key(s): {', '.join(unknown)}")
    return data.get("options")
