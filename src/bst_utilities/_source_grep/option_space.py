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
    name: str
    values: tuple


def option_values(declaration):
    """Command-line values for one option; "" means "leave at the default"."""
    if declaration.type == "bool":
        return ("false", "true")

    if declaration.type in ("enum", "arch", "os"):
        return tuple(declaration.values)

    if declaration.type == "flags":
        # BuildStream cannot parse an empty flags value from the command line,
        # so the empty set is only reachable when it is the project default.
        values = []
        if declaration.default == "":
            values.append("")
        flags = sorted(declaration.values)
        for size in range(1, len(flags) + 1):
            values.extend(",".join(subset) for subset in itertools.combinations(flags, size))
        return tuple(values)

    return ()


def plan(declarations, pinned):
    """
    Split declarations into enumerated axes and held option names.

    `pinned` holds names fixed with -o; those and element-mask options are
    held. Returns (axes, held) where held is a list of (name, reason).
    """
    axes = []
    held = []
    for declaration in declarations:
        if declaration.name in pinned:
            held.append((declaration.name, "pinned with -o"))
        elif declaration.type not in ENUMERATED_TYPES:
            held.append((declaration.name, f"{declaration.type} options are not enumerated"))
        else:
            values = option_values(declaration)
            if values:
                axes.append(Axis(declaration.name, values))
    return axes, held


def space_size(axes):
    size = 1
    for axis in axes:
        size *= len(axis.values)
    return size


def iter_option_sets(axes, declarations):
    """
    Yield every combination as an ordered {name: value} dict.

    The project-default combination comes first when every default is a
    valid value (an arch/os default is the host's, which may not be listed).
    """
    defaults = {d.name: d.default for d in declarations}
    default_set = {axis.name: defaults.get(axis.name) for axis in axes}
    has_default = all(default_set[axis.name] in axis.values for axis in axes)

    if has_default:
        yield default_set

    for values in itertools.product(*(axis.values for axis in axes)):
        option_set = {axis.name: value for axis, value in zip(axes, values)}
        if has_default and option_set == default_set:
            continue
        yield option_set


def cli_options(base, option_set):
    """Combine -o values with one option set; empty flags stay at their default."""
    merged = dict(base)
    for name, value in option_set.items():
        if value != "":
            merged[name] = value
    return list(merged.items())


def label(option_set):
    if not option_set:
        return "project defaults"
    return " ".join(f"{name}={value}" for name, value in option_set.items())
