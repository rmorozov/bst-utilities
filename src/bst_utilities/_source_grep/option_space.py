"""Enumerate the toplevel project's option combinations for --all-options."""

from __future__ import annotations

import itertools
import json
import re
import textwrap
from dataclasses import dataclass

# Option types whose value space is enumerated. element-mask values are every
# .bst file under the element path, so its power set is held at the default.
ENUMERATED_TYPES = ("bool", "enum", "arch", "os", "flags")

# Types whose value is a set of names; "" is the empty set.
SET_TYPES = ("flags", "element-mask")

DEFAULT_MAX_OPTION_SETS = 64


@dataclass(frozen=True)
class Declaration:
    """One option declared in project.conf, with values in command-line form."""

    name: str
    type: str
    values: tuple
    default: str | None


@dataclass(frozen=True)
class Axis:
    """An enumerated option; its values are produced only when iterated."""

    declaration: Declaration
    count: int
    choices: tuple | None = None  # restricted by an options file

    @property
    def name(self):
        return self.declaration.name

    @property
    def values(self):
        if self.choices is not None:
            return self.choices
        return option_values(self.declaration)


def value_count(declaration):
    """Number of values option_values() would produce, without producing them."""
    if declaration.type == "bool":
        return 2
    if declaration.type in ("enum", "arch", "os"):
        return len(declaration.values)
    if declaration.type == "flags":
        return 2 ** len(declaration.values)
    return 0


def option_values(declaration):
    """Command-line form of every value of one option; "" is the empty flags set."""
    if declaration.type == "bool":
        return ("false", "true")

    if declaration.type in ("enum", "arch", "os"):
        return tuple(declaration.values)

    if declaration.type == "flags":
        flags = sorted(declaration.values)
        return tuple(
            ",".join(subset)
            for size in range(len(flags) + 1)
            for subset in itertools.combinations(flags, size)
        )

    return ()


NOT_IN_FILE = "not in the options file"


def plan(declarations, pinned, restrictions=None, only_restricted=False):
    """
    Split declarations into enumerated axes and held option names.

    `pinned` holds names fixed with -o or an options file; those and
    element-mask options are held. `restrictions` maps names to the only
    values to enumerate. With `only_restricted` (an options file was given),
    options it does not list are held too. Returns (axes, held) where held
    is a list of (name, reason). Values are not enumerated here, so
    space_size() can enforce a cap first.
    """
    restrictions = restrictions or {}
    axes = []
    held = []
    for declaration in declarations:
        if declaration.name in pinned:
            held.append((declaration.name, "pinned"))
        elif declaration.name in restrictions:
            choices = restrictions[declaration.name]
            axes.append(Axis(declaration, len(choices), choices))
        elif only_restricted:
            held.append((declaration.name, NOT_IN_FILE))
        elif declaration.type not in ENUMERATED_TYPES:
            held.append((declaration.name, f"{declaration.type} options are not enumerated"))
        else:
            count = value_count(declaration)
            if count:
                axes.append(Axis(declaration, count))
    return axes, held


def space_size(axes):
    size = 1
    for axis in axes:
        size *= axis.count
    return size


def iter_option_sets(axes, declarations):
    """
    Yield every combination as an ordered {name: value} dict.

    The project-default combination comes first when every default is a
    valid value (an arch/os default is the host's, which may not be listed).
    """
    defaults = {d.name: d.default for d in declarations}
    values_per_axis = [axis.values for axis in axes]
    default_set = {axis.name: defaults.get(axis.name) for axis in axes}
    has_default = all(
        default_set[axis.name] in values for axis, values in zip(axes, values_per_axis)
    )

    if has_default:
        yield default_set

    for values in itertools.product(*values_per_axis):
        option_set = {axis.name: value for axis, value in zip(axes, values)}
        if has_default and option_set == default_set:
            continue
        yield option_set


def cli_options(base, option_set, flags_options):
    """
    Combine -o values with one option set.

    BuildStream cannot parse an empty flags value from the command line, so
    those are left out here and applied with empty_flags() instead. Other
    options, such as an enum with an empty value, are passed as they are.
    """
    merged = dict(base)
    empty = set(empty_flags(option_set, flags_options))
    for name, value in option_set.items():
        if name not in empty:
            merged[name] = value
    return list(merged.items())


def empty_flags(option_set, flags_options):
    """Names of the flags options (in `flags_options`) this set leaves empty."""
    return [name for name, value in option_set.items() if value == "" and name in flags_options]


def label(option_set):
    if not option_set:
        return "project defaults"
    return " ".join(f"{name}={value}" for name, value in option_set.items())


class OptionsFileError(ValueError):
    pass


def _set_value(declaration, raw):
    """A flags/element-mask value: a list of names or a comma-separated string."""
    if isinstance(raw, str):
        names = [name for name in "".join(raw.split()).split(",") if name]
    elif isinstance(raw, list) and all(isinstance(name, str) for name in raw):
        names = raw
    else:
        raise OptionsFileError(f"{declaration.name}: expected a list of names, got {raw!r}")
    unknown = sorted(set(names) - set(declaration.values))
    if unknown:
        raise OptionsFileError(f"{declaration.name}: unknown value(s) {', '.join(unknown)}")
    return ",".join(sorted(set(names)))


def _scalar_value(declaration, raw):
    if not isinstance(raw, str):
        raise OptionsFileError(f"{declaration.name}: expected one value, got {raw!r}")
    if declaration.type == "bool":
        value = raw.lower()
        if value not in ("true", "false"):
            raise OptionsFileError(f"{declaration.name}: expected true or false, got {raw!r}")
        return value
    if raw not in declaration.values:
        valid = ", ".join(repr(v) for v in declaration.values)
        raise OptionsFileError(f"{declaration.name}: {raw!r} is not one of {valid}")
    return raw


def parse_options_file(options, declarations):
    """
    Turn an options file's `options` mapping into (pins, restrictions).

    A single value pins an option. A list of values (for flags and
    element-mask, a list of lists) restricts --all-options to those values.
    Values are returned in the command-line form option_values() uses.
    """
    if options is None or options == "":
        # An empty `options:` (every entry commented out) loads as "".
        return {}, {}
    if not isinstance(options, dict):
        raise OptionsFileError("'options' must be a mapping of option names to values")

    by_name = {declaration.name: declaration for declaration in declarations}
    pins, restrictions = {}, {}
    for name, raw in options.items():
        declaration = by_name.get(name)
        if declaration is None:
            raise OptionsFileError(f"{name}: not an option declared in project.conf")
        if declaration.type in SET_TYPES:
            if isinstance(raw, list) and raw and all(isinstance(item, list) for item in raw):
                values = [_set_value(declaration, item) for item in raw]
            else:
                pins[name] = _set_value(declaration, raw)
                continue
        elif isinstance(raw, list):
            values = [_scalar_value(declaration, item) for item in raw]
        else:
            pins[name] = _scalar_value(declaration, raw)
            continue
        if not values:
            raise OptionsFileError(f"{name}: an empty list selects no value")
        restrictions[name] = tuple(dict.fromkeys(values))
    return pins, restrictions


# Plain YAML scalars that need no quoting; anything else is double-quoted.
_PLAIN_SCALAR = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./+=-]*")
_YAML_WORDS = {"null", "~", "true", "false", "yes", "no", "on", "off", "y", "n"}


def _yaml_scalar(value):
    """A YAML scalar that loads back as exactly `value`, on one line."""
    if _PLAIN_SCALAR.fullmatch(value) and value.lower() not in _YAML_WORDS:
        return value
    # A JSON string is a valid YAML double-quoted scalar with all escapes.
    return json.dumps(value)


def _set_names(value):
    return value.split(",") if value else []


def _describe(declaration):
    """Type, default and possible values of one option, as comment lines."""
    default = _yaml_value(declaration, declaration.default or "")
    lines = [f"{declaration.name} ({declaration.type}), default: {default}"]
    if declaration.type == "bool":
        values = "true, false"
    elif declaration.type in SET_TYPES:
        values = "any set of: " + ", ".join(_yaml_scalar(v) for v in declaration.values)
    else:
        values = ", ".join(_yaml_scalar(v) for v in declaration.values)
    lines += textwrap.wrap(f"values: {values}", 76, subsequent_indent="  ")
    if declaration.type == "element-mask":
        lines.append("not enumerated by --all-options; pin it or list the sets to search")
    return lines


def _yaml_value(declaration, value):
    """One value in options-file form: a scalar, or a flow list for a set."""
    if declaration.type in SET_TYPES:
        return "[" + ", ".join(_yaml_scalar(name) for name in _set_names(value)) + "]"
    if declaration.type == "bool":
        return value
    return _yaml_scalar(value)


def _yaml_list(declaration, values):
    return "[" + ", ".join(_yaml_value(declaration, value) for value in values) + "]"


def render_template(project_name, declarations, pinned, restrictions=None):
    """
    An options file listing every option, commented out unless set.

    Pins and restrictions (from -o or an existing options file) are written as
    active entries, so rendering a loaded file reproduces its choices.
    Uncommenting `name: value` pins an option; `name: [a, b]` limits
    --all-options to those values.
    """
    restrictions = restrictions or {}
    lines = [
        f"# Options of BuildStream project '{project_name}' for bst-source-grep.",
        "# Pass this file with --options-file. Uncomment a line to use it:",
        "#   name: value       pin the option to one value (not enumerated)",
        "#   name: [v1, v2]    make --all-options enumerate only these values",
        "# Each example lists every value; delete the ones you do not need.",
        "# A flags value is a list of flags; a list of such lists limits",
        "# --all-options to those sets. With this file, --all-options varies only",
        "# the options listed in it; commented options keep their configured value",
        "# (--unlisted-options vary tries every value of them instead).",
        "# -o KEY VALUE on the command line overrides this file.",
        "options:",
    ]
    for declaration in declarations:
        lines.append("")
        lines += [f"  # {line}" for line in _describe(declaration)]
        name = declaration.name
        if name in pinned:
            lines.append(f"  {name}: {_yaml_value(declaration, pinned[name])}")
        elif name in restrictions:
            lines.append(f"  {name}: {_yaml_list(declaration, restrictions[name])}")
        elif declaration.type in SET_TYPES:
            lines.append(f"  # {name}: {_yaml_value(declaration, declaration.default or '')}")
        else:
            # Every value: delete the ones not to search, or keep one to pin it.
            lines.append(f"  # {name}: {_yaml_list(declaration, option_values(declaration))}")
    return "\n".join(lines) + "\n"


def render_listing(declarations, pinned, restrictions=None, only_restricted=False):
    """Human-readable list of options, their values and how a search uses them."""
    restrictions = restrictions or {}
    lines = []
    for declaration in declarations:
        name = declaration.name
        described = _describe(declaration)
        if name in pinned:
            state = f"pinned to {pinned[name]!r}"
        elif name in restrictions:
            state = f"--all-options tries {len(restrictions[name])}: " + ", ".join(
                repr(v) for v in restrictions[name]
            )
        elif only_restricted:
            state = "kept: not in the options file"
        elif declaration.type in ENUMERATED_TYPES:
            state = f"--all-options tries {value_count(declaration)}"
        else:
            state = "held"
        lines.append(f"{described[0]}  [{state}]")
        lines += [f"    {line}" for line in described[1:]]
    return "\n".join(lines) + ("\n" if lines else "")
