"""--all-options enumeration, attribution and option-set merging."""

import json
import re
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
    monkeypatch.setattr(adapter, "declared_options", lambda directory: ("p", [many], []))
    monkeypatch.setattr(
        option_space, "option_values", lambda d: pytest.fail("enumerated before cap")
    )
    args = cli.parse_args(["t.bst", "--find", "*", "--all-options"])
    assert application._prepare_options(args) is None
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
    merged = option_space.cli_options([("arch", "x86_64")], option_set, {"feats"})
    assert merged == [("arch", "x86_64"), ("debug", "true")]
    assert option_space.empty_flags(option_set, {"feats"}) == ["feats"]


def test_empty_enum_value_goes_on_the_command_line():
    # BuildStream accepts an enum value of "" on the command line, but rejects
    # the [] override that only an empty flags value needs.
    option_set = {"flavour": "", "feats": ""}
    assert option_space.cli_options([], option_set, {"feats"}) == [("flavour", "")]
    assert option_space.empty_flags(option_set, {"feats"}) == ["feats"]
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
    name, declarations, _ = adapter.declared_options(str(tmp_path / "elements" / "sub"))
    assert name == "opts"
    by_name = {d.name: d for d in declarations}
    assert [d.name for d in declarations] == ["debug", "machine", "feats", "mask"]
    assert by_name["debug"].default == "true"
    assert by_name["machine"].type == "arch" and by_name["machine"].values == ("riscv64",)
    assert by_name["feats"].default == "a,b"
    assert by_name["mask"].values == ("a.bst",)


def test_declared_options_follow_local_includes(tmp_path):
    pytest.importorskip("buildstream")
    (tmp_path / "elements").mkdir()
    (tmp_path / "include" / "more").mkdir(parents=True)
    (tmp_path / "project.conf").write_text("""name: incl
min-version: 2.8
element-path: elements
(@):
- include/options.yml
- base.bst:include/shared.yml
options:
  (@): include/more/inner.yml
  local:
    type: bool
    description: declared here
    default: false
""")
    (tmp_path / "include" / "options.yml").write_text("""(@): include/more/nested.yml
options:
  flavour:
    type: enum
    description: from an include
    values: [x, y]
    default: x
""")
    (tmp_path / "include" / "more" / "nested.yml").write_text("""options:
  flavour:
    type: enum
    description: overridden by the including file
    values: [unused]
    default: unused
  nested:
    type: flags
    description: from a nested include
    values: [p, q]
""")
    (tmp_path / "include" / "more" / "inner.yml").write_text("""inner:
  type: enum
  description: included inside the options mapping
  values: [m, n]
  default: n
""")
    name, declarations, junction_includes = adapter.declared_options(str(tmp_path))
    assert name == "incl"
    by_name = {d.name: d for d in declarations}
    assert set(by_name) == {"local", "flavour", "nested", "inner"}
    assert by_name["flavour"].values == ("x", "y") and by_name["flavour"].default == "x"
    assert by_name["nested"].type == "flags" and by_name["nested"].values == ("p", "q")
    assert by_name["inner"].default == "n"
    assert junction_includes == ["base.bst:include/shared.yml"]


def test_declared_options_follow_symlinked_includes(tmp_path):
    pytest.importorskip("buildstream")
    project, outside = tmp_path / "p", tmp_path / "outside"
    (project / "elements").mkdir(parents=True)
    (project / "real").mkdir()
    outside.mkdir()
    (project / "real" / "a.yml").write_text(
        "options:\n  a:\n    type: bool\n    description: a\n    default: false\n"
    )
    (outside / "b.yml").write_text(
        "options:\n  b:\n    type: enum\n    description: b\n    values: [m, n]\n    default: m\n"
    )
    (project / "a-link.yml").symlink_to("real/a.yml")
    (project / "out").symlink_to("../outside")
    (project / "project.conf").write_text(
        "name: sl\nmin-version: 2.8\nelement-path: elements\n(@):\n- a-link.yml\n- out/b.yml\n"
    )
    _, declarations, _ = adapter.declared_options(str(project))
    assert [(d.name, d.values) for d in declarations] == [("a", ()), ("b", ("m", "n"))]


def test_declared_options_report_recursive_include(tmp_path):
    pytest.importorskip("buildstream")
    (tmp_path / "elements").mkdir()
    (tmp_path / "project.conf").write_text("name: loop\nmin-version: 2.8\n(@): a.yml\n")
    (tmp_path / "a.yml").write_text("(@): a.yml\n")
    with pytest.raises(Exception, match="recursively include"):
        adapter.declared_options(str(tmp_path))


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


def _declarations():
    return [
        Declaration("debug", "bool", (), "false"),
        Declaration("arch", "arch", ("x86_64", "aarch64", "riscv64"), "x86_64"),
        Declaration("mode", "enum", ("", "x"), ""),
        Declaration("feats", "flags", ("a", "b"), ""),
        Declaration("mask", "element-mask", ("a.bst", "b.bst"), ""),
    ]


def test_options_file_pins_and_restrictions():
    pins, restrictions = option_space.parse_options_file(
        {
            "debug": "True",
            "arch": ["aarch64", "x86_64", "aarch64"],
            "mode": "",
            "feats": [["b", "a"], []],
            "mask": ["b.bst"],
        },
        _declarations(),
    )
    assert pins == {"debug": "true", "mode": "", "mask": "b.bst"}
    assert restrictions == {"arch": ("aarch64", "x86_64"), "feats": ("a,b", "")}
    # A flat list (or a comma string) is one flags value: a pin.
    assert option_space.parse_options_file({"feats": "b, a"}, _declarations())[0] == {
        "feats": "a,b"
    }
    assert option_space.parse_options_file(None, _declarations()) == ({}, {})


@pytest.mark.parametrize(
    "options, message",
    [
        ({"nope": "x"}, "not an option declared"),
        ({"arch": "sparc"}, "is not one of"),
        ({"debug": "maybe"}, "expected true or false"),
        ({"feats": ["c"]}, "unknown value(s) c"),
        ({"arch": []}, "selects no value"),
        ({"arch": [["x86_64"]]}, "expected one value"),
        (["arch"], "must be a mapping"),
    ],
)
def test_options_file_errors(options, message):
    with pytest.raises(option_space.OptionsFileError, match=re.escape(message)):
        option_space.parse_options_file(options, _declarations())


def test_restrictions_become_axes_and_attribution():
    declarations = _declarations()
    axes, held = option_space.plan(
        declarations, {"debug": "true"}, {"arch": ("aarch64",), "mask": ("", "a.bst")}
    )
    assert [(a.name, a.count) for a in axes] == [
        ("arch", 1),
        ("mode", 2),
        ("feats", 4),
        ("mask", 2),
    ]
    assert held == [("debug", "pinned")]
    sets = list(option_space.iter_option_sets(axes, declarations))
    assert len(sets) == 16 and {s["arch"] for s in sets} == {"aarch64"}


def test_template_round_trips_through_buildstream_yaml(tmp_path):
    pytest.importorskip("buildstream")
    declarations = _declarations()
    text = option_space.render_template("proj", declarations, {"debug": "true"})
    assert "  debug: true" in text
    assert "  # arch: [x86_64, aarch64, riscv64]" in text
    assert '  # mode: ["", x]' in text
    assert "  # feats: []" in text
    assert "not enumerated by --all-options" in text

    untouched = tmp_path / "untouched.yml"
    untouched.write_text(text)
    assert option_space.parse_options_file(
        adapter.load_options_file(str(untouched)), declarations
    ) == ({"debug": "true"}, {})

    edited = tmp_path / "edited.yml"
    edited.write_text(
        text.replace("  # arch: [x86_64, aarch64, riscv64]", "  arch: [x86_64, riscv64]")
        .replace('  # mode: ["", x]', "  mode: ['', x]")
        .replace("  # feats: []", "  feats: [[a]]")
    )
    pins, restrictions = option_space.parse_options_file(
        adapter.load_options_file(str(edited)), declarations
    )
    assert pins == {"debug": "true"}
    assert restrictions == {"arch": ("x86_64", "riscv64"), "mode": ("", "x"), "feats": ("a",)}

    bad = tmp_path / "bad.yml"
    bad.write_text("option:\n  debug: true\n")
    with pytest.raises(option_space.OptionsFileError, match="unknown top-level"):
        adapter.load_options_file(str(bad))


def test_listing_shows_pins_restrictions_and_counts():
    text = option_space.render_listing(
        _declarations(), {"debug": "true"}, {"arch": ("aarch64", "x86_64")}
    )
    lines = text.splitlines()
    assert lines[0] == "debug (bool), default: false  [pinned to 'true']"
    assert "arch (arch), default: x86_64  [--all-options tries 2: 'aarch64', 'x86_64']" in lines
    assert "feats (flags), default: []  [--all-options tries 4]" in lines
    assert "mask (element-mask), default: []  [held]" in lines


def test_options_file_varies_only_the_options_it_lists():
    declarations = _declarations()
    axes, held = option_space.plan(
        declarations, {"debug": "true"}, {"arch": ("aarch64", "x86_64")}, only_restricted=True
    )
    assert [(a.name, a.count) for a in axes] == [("arch", 2)]
    assert held == [
        ("debug", "pinned"),
        ("mode", option_space.NOT_IN_FILE),
        ("feats", option_space.NOT_IN_FILE),
        ("mask", option_space.NOT_IN_FILE),
    ]
    text = option_space.render_listing(
        declarations, {"debug": "true"}, {"arch": ("aarch64", "x86_64")}, only_restricted=True
    )
    assert "feats (flags), default: []  [kept: not in the options file]" in text.splitlines()


def test_cap_counts_only_what_the_options_file_varies(monkeypatch, capsys):
    from bst_utilities._source_grep import application

    wide = Declaration("wide", "flags", tuple("pqrstuvw"), "")
    monkeypatch.setattr(
        adapter, "declared_options", lambda directory: ("p", [*_declarations(), wide], [])
    )
    monkeypatch.setattr(adapter, "load_options_file", lambda path: {"arch": ["aarch64", "x86_64"]})
    args = cli.parse_args(["t.bst", "x", "--all-options", "--options-file", "o.yml"])
    assert application._prepare_options(args) is None
    stats = {}
    sets = list(application._plan_option_sets(args, stats))
    assert sets == [{"arch": "x86_64"}, {"arch": "aarch64"}]
    assert stats["option_sets_planned"] == 2
    err = capsys.readouterr().err
    assert "NOTE: 5 option(s) not in o.yml keep their configured value: " in err
    assert "debug, mode, feats, mask, wide" in err

    # Without a file every option still varies, so the same project hits the cap.
    args = cli.parse_args(["t.bst", "x", "--all-options"])
    assert application._prepare_options(args) is None
    assert application._plan_option_sets(args, {}) is None
    assert "wide=256" in capsys.readouterr().err

    # --unlisted-options vary enumerates what the file leaves out, as before.
    argv = ["t.bst", "x", "--all-options", "--options-file", "o.yml", "--unlisted-options", "vary"]
    args = cli.parse_args(argv)
    assert application._prepare_options(args) is None
    assert application._plan_option_sets(args, {}) is None
    assert "(debug=2, arch=2, mode=2, feats=4, wide=256)" in capsys.readouterr().err


def test_cli_describe_modes_need_no_target():
    args = cli.parse_args(["--list-options"])
    assert args.list_options and args.target is None
    assert cli.parse_args(["--options-template", "-C", "/p"]).options_template
    for argv in (["--find", "*"], ["--list-options", "--options-template"]):
        with pytest.raises(SystemExit):
            cli.parse_args(argv)


@pytest.mark.parametrize("all_options", [False, True])
def test_prepare_merges_file_and_command_line(monkeypatch, tmp_path, capsys, all_options):
    from bst_utilities._source_grep import application

    monkeypatch.setattr(adapter, "declared_options", lambda directory: ("p", _declarations(), []))
    raw = {"debug": "true", "arch": ["aarch64"], "mode": ["", "x"], "feats": []}
    monkeypatch.setattr(adapter, "load_options_file", lambda path: raw)
    argv = ["t.bst", "--find", "*", "--options-file", "o.yml", "-o", "mode", "x"]
    args = cli.parse_args(argv + (["--all-options"] if all_options else []))
    assert application._prepare_options(args) is None
    # -o replaces the file's mode list; the empty flags pin needs an override.
    assert args.pinned_empty == ["feats"]
    if all_options:
        assert args.restrictions == {"arch": ("aarch64",)}
        assert args.option == [("debug", "true"), ("mode", "x")]
    else:
        assert args.restrictions == {}
        assert args.option == [("debug", "true"), ("mode", "x"), ("arch", "aarch64")]

    raw["arch"] = ["aarch64", "x86_64"]
    args = cli.parse_args(["t.bst", "--find", "*", "--options-file", "o.yml"])
    assert application._prepare_options(args) == 2
    assert "lists several values for arch, mode; that needs --all-options" in (
        capsys.readouterr().err
    )


TRICKY = ("a,b", "a]b", "a # b", 'q"uote', "back\\slash", "new\nline", "", "null", "x: y", "plain")


def _uncomment(text, name):
    return text.replace(f"  # {name}: ", f"  {name}: ")


def _load_text(tmp_path, text):
    path = tmp_path / "options.yml"
    path.write_text(text)
    return adapter.load_options_file(str(path))


def test_template_values_survive_yaml_exactly(tmp_path):
    pytest.importorskip("buildstream")
    declarations = [
        Declaration("mode", "enum", TRICKY, "a # b"),
        Declaration("feats", "flags", ("p]q", "r # s", "t"), "t"),
    ]
    text = option_space.render_template("proj", declarations, {})
    # Untouched, every line is a comment or the empty mapping.
    assert option_space.parse_options_file(_load_text(tmp_path, text), declarations) == ({}, {})
    # Uncommented, the example lists every value exactly once and unchanged.
    pins, restrictions = option_space.parse_options_file(
        _load_text(tmp_path, _uncomment(_uncomment(text, "mode"), "feats")), declarations
    )
    assert restrictions == {"mode": TRICKY}
    assert pins == {"feats": "t"}
    # Pins of each awkward value load back as that value.
    for value in TRICKY:
        pinned = option_space.render_template("proj", declarations, {"mode": value})
        pins, _ = option_space.parse_options_file(_load_text(tmp_path, pinned), declarations)
        assert pins == {"mode": value}


def test_template_from_options_file_reproduces_its_choices(tmp_path):
    pytest.importorskip("buildstream")
    declarations = _declarations()
    pins = {"debug": "false"}
    restrictions = {
        "arch": ("riscv64",),
        "mode": ("", "x"),
        "feats": ("a,b", ""),
        "mask": ("b.bst",),
    }
    text = option_space.render_template("proj", declarations, pins, restrictions)
    loaded = option_space.parse_options_file(_load_text(tmp_path, text), declarations)
    assert loaded == (pins, restrictions)

    def planned(p, r):
        axes, held = option_space.plan(declarations, p, r)
        return held, list(option_space.iter_option_sets(axes, declarations))

    assert planned(*loaded) == planned(pins, restrictions)


def test_template_switch_renders_existing_restrictions(monkeypatch, capsys):
    from bst_utilities._source_grep import application

    monkeypatch.setattr(adapter, "load_api", lambda: None)
    monkeypatch.setattr(adapter, "declared_options", lambda directory: ("p", _declarations(), []))
    monkeypatch.setattr(adapter, "load_options_file", lambda path: {"arch": ["riscv64"]})
    args = cli.parse_args(["--options-template", "--options-file", "o.yml"])
    assert application._describe_options(args) == 0
    assert "\n  arch: [riscv64]\n" in capsys.readouterr().out


def test_progress_estimates_remaining_option_sets():
    import io

    from bst_utilities._source_grep import application

    now = [0.0]
    out = io.StringIO()
    progress = application._Progress(1000, clock=lambda: now[0], stream=out)
    now[0] = 3.0
    progress.step()  # the first set always reports an estimate
    for _ in range(9):
        now[0] += 3.0
        progress.step()  # within the interval: quiet
    now[0] += 3.0
    progress.step()
    assert out.getvalue().splitlines() == [
        "NOTE: loaded 1/1000 option sets in 3s; about 49m57s left",
        "NOTE: loaded 11/1000 option sets in 33s; about 49m27s left",
    ]
    assert application._duration(2e9 * 2.8) == "178 years"

    quiet = io.StringIO()
    single = application._Progress(1, clock=lambda: 0.0, stream=quiet)
    single.step()
    assert quiet.getvalue() == ""
