"""--all-options enumeration, attribution and option-set merging."""

import json
from types import SimpleNamespace

import pytest

from bst_utilities._source_grep import adapter, catalogue, cli, option_space, output, stats
from bst_utilities._source_grep.option_space import Declaration


def test_option_values_per_type():
    assert option_space.option_values(Declaration("b", "bool", (), "false")) == ("false", "true")
    assert option_space.option_values(Declaration("e", "enum", ("x", "y"), "x")) == ("x", "y")
    assert option_space.option_values(Declaration("a", "arch", ("x86_64", "aarch64"), "i686")) == (
        "x86_64",
        "aarch64",
    )
    # Every subset, including the empty one, whatever the default.
    flags = Declaration("f", "flags", ("b", "a"), "a")
    assert option_space.option_values(flags) == ("", "a", "b", "a,b")
    assert option_space.value_count(flags) == 4


def test_cap_is_checked_without_enumerating_flags(monkeypatch):
    def enumerate_values(declaration):
        raise AssertionError("values enumerated before the cap was checked")

    monkeypatch.setattr(option_space, "option_values", enumerate_values)
    many = Declaration("f", "flags", tuple(f"flag{i}" for i in range(64)), "")
    axes, _ = option_space.plan([many, Declaration("d", "bool", (), "false")], {})
    assert option_space.space_size(axes) == 2**65


def test_application_rejects_flags_explosion_before_enumerating(monkeypatch, capsys):
    from bst_utilities._source_grep import application

    many = Declaration("f", "flags", tuple(f"flag{i}" for i in range(40)), "")
    monkeypatch.setattr(adapter, "declared_options", lambda directory: ("p", [many]))
    monkeypatch.setattr(
        option_space, "option_values", lambda d: pytest.fail("enumerated before cap")
    )
    args = cli.parse_args(["t.bst", "--find", "*", "--all-options"])
    assert application._plan_option_sets(args, stats.new_stats()) is None
    assert "would load 1099511627776 option sets (f=1099511627776)" in capsys.readouterr().err


def test_plan_holds_pinned_and_element_mask_options():
    declarations = [
        Declaration("debug", "bool", (), "false"),
        Declaration("mask", "element-mask", ("a.bst", "b.bst"), ""),
        Declaration("flavour", "enum", ("x", "y", "z"), "y"),
    ]
    axes, held = option_space.plan(declarations, {"flavour": "x"})
    assert [axis.name for axis in axes] == ["debug"]
    assert [name for name, _ in held] == ["mask", "flavour"]
    assert option_space.space_size(axes) == 2


def test_option_sets_start_with_defaults_and_cover_the_product_once():
    declarations = [
        Declaration("debug", "bool", (), "true"),
        Declaration("flavour", "enum", ("x", "y", "z"), "y"),
    ]
    axes, _ = option_space.plan(declarations, {})
    sets = list(option_space.iter_option_sets(axes, declarations))
    assert sets[0] == {"debug": "true", "flavour": "y"}
    assert len(sets) == option_space.space_size(axes) == 6
    assert len({tuple(s.items()) for s in sets}) == 6


def test_option_sets_without_valid_default_keep_product_order():
    # An arch option defaults to the host, which the project may not list.
    declarations = [Declaration("arch", "arch", ("aarch64", "riscv64"), "x86_64")]
    axes, _ = option_space.plan(declarations, {})
    assert list(option_space.iter_option_sets(axes, declarations)) == [
        {"arch": "aarch64"},
        {"arch": "riscv64"},
    ]


def test_no_declared_options_load_defaults_once():
    axes, held = option_space.plan([], {})
    assert list(option_space.iter_option_sets(axes, [])) == [{}]
    assert option_space.label({}) == "project defaults"


def test_cli_options_keep_pins_and_leave_empty_flags_to_overrides():
    option_set = {"debug": "true", "feats": ""}
    merged = option_space.cli_options([("arch", "x86_64")], option_set)
    assert merged == [("arch", "x86_64"), ("debug", "true")]
    assert option_space.empty_flags(option_set) == ["feats"]
    assert option_space.label({"debug": "true", "feats": ""}) == "debug=true feats="


def test_cli_accepts_all_options_and_validates_cap():
    args = cli.parse_args(["t.bst", "--find", "*", "--all-options", "--max-option-sets", "8"])
    assert args.all_options and args.max_option_sets == 8
    default = cli.parse_args(["t.bst", "--find", "*"])
    assert not default.all_options
    assert default.max_option_sets == option_space.DEFAULT_MAX_OPTION_SETS
    with pytest.raises(SystemExit):
        cli.parse_args(["t.bst", "--find", "*", "--max-option-sets", "0"])


def _fake_tree_sources(monkeypatch):
    """Fake elements carry their name and the digest of their source tree."""
    from bst_utilities._source_grep import cas_layout, source_cache

    monkeypatch.setattr(
        source_cache, "load_source_directory", lambda element: (element, "ok", None)
    )
    monkeypatch.setattr(cas_layout, "get_cas_directory_digest", lambda directory: directory)
    monkeypatch.setattr(cas_layout, "digest_to_fuse_value", lambda element: element.digest)
    monkeypatch.setattr(adapter, "element_label", lambda element: element.name)


@pytest.mark.parametrize("strip", [False, True])
def test_option_sets_merge_per_element_and_tree(monkeypatch, strip):
    lib_x = SimpleNamespace(name="lib", digest="d1/1")
    lib_y = SimpleNamespace(name="lib", digest="d2/1")
    lib_z = SimpleNamespace(name="lib", digest="d1/1")
    app = SimpleNamespace(name="app", digest="d3/1")
    _fake_tree_sources(monkeypatch)
    args = SimpleNamespace(strip_junctions=strip, origin=False)
    counters = stats.new_stats()
    x, y, z = {"flavour": "x"}, {"flavour": "y"}, {"flavour": "z"}
    trees = {}
    catalogue.discover_trees([lib_x, app], args, counters, True, trees, x)
    catalogue.discover_trees([lib_y, app], args, counters, True, trees, y)
    catalogue.discover_trees([lib_z], args, counters, True, trees, z)

    assert list(trees) == ["d1/1", "d3/1", "d2/1"]
    attribution = {
        digest: [(e["label"], e["option_sets"]) for e in tree["elements"]]
        for digest, tree in trees.items()
    }
    assert attribution == {
        "d1/1": [("lib", [x, z])],
        "d2/1": [("lib", [y])],
        "d3/1": [("app", [x, y])],
    }
    assert counters["trees"] == 3 and counters["elements"] == 5
    assert counters["duplicate_elements"] == 0


def test_single_load_keeps_element_records_unchanged(monkeypatch):
    lib = SimpleNamespace(name="lib", digest="d1/1")
    _fake_tree_sources(monkeypatch)
    args = SimpleNamespace(strip_junctions=False, origin=False)
    trees = catalogue.discover_trees([lib], args, stats.new_stats(), True)
    assert trees["d1/1"]["elements"] == [{"element": lib, "label": "lib", "recipe": "lib"}]


@pytest.mark.parametrize("json_mode", [False, True])
def test_json_records_carry_option_sets(capsys, json_mode):
    args = SimpleNamespace(json=json_mode, origin=False, line_number=False, strip_junctions=False)
    emitter = output.ResultEmitter(args, output.LineBuffer(), stats.new_stats())
    sets = [{"flavour": "x"}, {"flavour": "y"}]
    element = {"label": "lib.bst", "recipe": "lib.bst", "option_sets": sets}
    emitter.emit_file(element, "a.c")
    emitter.emit_match(element, "a.c", 1, "needle")
    emitter.out.flush()
    lines = capsys.readouterr().out.splitlines()
    if json_mode:
        assert [json.loads(line)["option_sets"] for line in lines] == [sets, sets]
    else:
        # Text output keeps its element:path[:text] format.
        assert lines == ["lib.bst:a.c", "lib.bst:a.c:needle"]


def test_user_assertion_detection():
    reason = SimpleNamespace(name="USER_ASSERTION", value=10)
    assert adapter.is_user_assertion(SimpleNamespace(reason=reason))
    assert not adapter.is_user_assertion(SimpleNamespace(reason=SimpleNamespace(name="MISSING")))
    assert not adapter.is_user_assertion(RuntimeError("plain"))


def test_declared_options_read_project_conf_without_resolving(tmp_path):
    pytest.importorskip("buildstream")
    (tmp_path / "elements" / "sub").mkdir(parents=True)
    (tmp_path / "elements" / "a.bst").write_text("kind: stack\n")
    (tmp_path / "project.conf").write_text("""name: opts
min-version: 2.8
element-path: elements
options:
  debug:
    type: bool
    description: debug
    default: true
  machine:
    type: arch
    description: only foreign architectures, so resolving would fail here
    values: [riscv64]
  feats:
    type: flags
    description: features
    values: [b, a]
    default: [b, a]
  mask:
    type: element-mask
    description: masked elements
""")
    name, declarations = adapter.declared_options(str(tmp_path / "elements" / "sub"))
    assert name == "opts"
    by_name = {d.name: d for d in declarations}
    assert [d.name for d in declarations] == ["debug", "machine", "feats", "mask"]
    assert by_name["debug"].default == "true"
    assert by_name["machine"].type == "arch" and by_name["machine"].values == ("riscv64",)
    assert by_name["feats"].default == "a,b"
    assert by_name["mask"].values == ("a.bst",)


def test_strip_dedup_keeps_json_attribution_of_distinct_trees(capsys):
    def run(json_mode):
        args = SimpleNamespace(json=json_mode, origin=False, line_number=True, strip_junctions=True)
        emitter = output.ResultEmitter(args, output.LineBuffer(), stats.new_stats())
        off, on = {"debug": "false"}, {"debug": "true"}
        # lib.bst reaches two different trees with the same matched path and line.
        for option_set in (off, on):
            element = {"label": "lib.bst", "recipe": "lib.bst", "option_sets": [option_set]}
            emitter.emit_file(element, "common.c")
            emitter.emit_match(element, "common.c", 3, "needle")
        # A second junction instance of the same tree and sets still collapses.
        element = {"label": "sub.bst:lib.bst", "recipe": "lib.bst", "option_sets": [on]}
        emitter.emit_match(element, "common.c", 3, "needle")
        emitter.out.flush()
        return capsys.readouterr().out.splitlines()

    records = [json.loads(line) for line in run(True)]
    assert [(r["type"], r["option_sets"]) for r in records] == [
        ("file", [{"debug": "false"}]),
        ("match", [{"debug": "false"}]),
        ("file", [{"debug": "true"}]),
        ("match", [{"debug": "true"}]),
    ]
    assert run(False) == ["lib.bst:common.c", "lib.bst:common.c:3:needle"]


def test_empty_flags_overrides_replace_user_configuration_temporarily():
    pytest.importorskip("buildstream")
    from buildstream.node import Node

    original = Node.from_dict({"opts": {"options": {"feats": ["a"]}, "strict": False}})
    context = SimpleNamespace(_project_overrides=original)
    with adapter.empty_flags_overrides(context, "opts", ["feats", "more"]):
        options = context._project_overrides.get_mapping("opts").get_mapping("options")
        assert options.get_sequence("feats").as_str_list() == []
        assert options.get_sequence("more").as_str_list() == []
    assert context._project_overrides is original
    assert original.strip_node_info() == {"opts": {"options": {"feats": ["a"]}, "strict": "False"}}

    empty = SimpleNamespace(_project_overrides=Node.from_dict({}))
    with adapter.empty_flags_overrides(empty, "opts", ["feats"]):
        assert empty._project_overrides.strip_node_info() == {"opts": {"options": {"feats": []}}}
    with adapter.empty_flags_overrides(SimpleNamespace(), "opts", []):
        pass
