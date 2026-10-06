"""Enumerate the toplevel project's option combinations for --all-options."""

from __future__ import annotations

import itertools
from dataclasses import dataclass

# Option types whose value space is enumerated. element-mask values are every
# .bst file under the element path, so its power set is held at the default.
ENUMERATED_TYPES = ("bool", "enum", "arch", "os", "flags")

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

    @property
    def name(self):
        return self.declaration.name

    @property
    def values(self):
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


def plan(declarations, pinned):
    """
    Split declarations into enumerated axes and held option names.

    `pinned` holds names fixed with -o; those and element-mask options are
    held. Returns (axes, held) where held is a list of (name, reason). Values
    are not enumerated here, so space_size() can enforce a cap first.
    """
    axes = []
    held = []
    for declaration in declarations:
        if declaration.name in pinned:
            held.append((declaration.name, "pinned with -o"))
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
