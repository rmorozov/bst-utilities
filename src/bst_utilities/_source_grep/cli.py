"""Declare and validate the bst-source-grep command-line contract."""

from __future__ import annotations

import argparse
import os

from .option_space import DEFAULT_MAX_OPTION_SETS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Search BuildStream source caches using CAS-direct traversal for "
            "--find and buildbox-fuse + ripgrep for content searches."
        )
    )

    parser.add_argument("--config", help="BuildStream user configuration file")
    parser.add_argument(
        "-C", "--directory", default=os.getcwd(), help="BuildStream project directory"
    )
    parser.add_argument(
        "-o",
        "--option",
        nargs=2,
        action="append",
        default=[],
        metavar=("KEY", "VALUE"),
        help="project option; repeatable, last value wins",
    )
    parser.add_argument(
        "--all-options",
        action="store_true",
        help=(
            "search the union of sources reached under every combination of the "
            "toplevel project's options; -o pins an option to one value"
        ),
    )
    parser.add_argument(
        "--max-option-sets",
        type=int,
        default=DEFAULT_MAX_OPTION_SETS,
        metavar="N",
        help=(
            "refuse --all-options when it would load more than N option sets "
            f"(default: {DEFAULT_MAX_OPTION_SETS})"
        ),
    )
    parser.add_argument(
        "--options-file",
        metavar="FILE",
        help=(
            "YAML options file: 'name: value' pins an option, 'name: [a, b]' limits "
            "--all-options to those values; -o overrides it"
        ),
    )
    parser.add_argument(
        "--fetch-sources",
        action="store_true",
        help=(
            "fetch the selected sources (and junctions) into the local cache before "
            "searching, for every option set with --all-options; uses the network"
        ),
    )
    parser.add_argument(
        "--unlisted-options",
        choices=("keep", "vary"),
        default="keep",
        help=(
            "with --options-file, options the file does not list keep their configured "
            "value (keep, default) or --all-options tries every value (vary)"
        ),
    )
    parser.add_argument(
        "--list-options",
        action="store_true",
        help="list the project's options, their values and pins, then exit",
    )
    parser.add_argument(
        "--options-template",
        action="store_true",
        help="print an --options-file template with every option commented out, then exit",
    )

    parser.add_argument(
        "--all-elements",
        action="store_true",
        help=(
            "search every element under the project's element path instead of TARGET; "
            "give only PATTERN (or --find)"
        ),
    )

    parser.add_argument(
        "target",
        nargs="?",
        help="BuildStream element, e.g. default_elements.bst",
    )
    parser.add_argument(
        "pattern",
        nargs="?",
        help="regular expression to search for",
    )

    parser.add_argument(
        "--find",
        metavar="GLOB",
        help="find files matching GLOB instead of searching contents",
    )

    parser.add_argument(
        "-i",
        "--ignore-case",
        action="store_true",
        help="case-insensitive matching",
    )
    parser.add_argument(
        "-F",
        "--fixed-string",
        action="store_true",
        help="treat PATTERN as a literal string",
    )
    parser.add_argument(
        "-n",
        "--line-number",
        action="store_true",
        help="show line numbers in text output",
    )
    parser.add_argument(
        "-l",
        "--files-with-matches",
        action="store_true",
        help="print only files containing a match",
    )

    parser.add_argument(
        "--glob",
        action="append",
        default=[],
        metavar="GLOB",
        help="only search files matching GLOB; repeatable",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="exclude files matching GLOB; repeatable",
    )

    parser.add_argument(
        "--binary-files",
        choices=("skip", "text"),
        default="skip",
        help="how to handle binary files (default: skip)",
    )
    parser.add_argument(
        "--deps",
        choices=("none", "build", "run", "all"),
        default="all",
        help="dependency selection (default: all)",
    )

    parser.add_argument(
        "--fetch-subprojects",
        dest="fetch_subprojects",
        action="store_true",
        default=False,
        help="allow BuildStream to fetch/load subprojects if required (may access network)",
    )
    parser.add_argument(
        "--no-fetch-subprojects",
        dest="fetch_subprojects",
        action="store_false",
        help="do not allow fetching subprojects (default)",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="output results as JSON Lines",
    )

    parser.add_argument(
        "--strip-junctions",
        dest="strip_junctions",
        action="store_true",
        help="strip junction/project prefixes from element names",
    )
    parser.add_argument(
        "--unique-recipes",
        dest="strip_junctions",
        action="store_true",
        help="alias for --strip-junctions",
    )

    parser.add_argument(
        "--origin",
        action="store_true",
        help="include source origin information",
    )

    parser.add_argument(
        "--gitreview",
        dest="gitreview_mode",
        choices=("auto", "always", "never"),
        default="auto",
        help=(
            "control .gitreview parsing: "
            "auto = only when source metadata lacks usable Gerrit project, "
            "always = always parse, never = disable"
        ),
    )
    parser.add_argument(
        "--no-gitreview",
        dest="gitreview_mode",
        action="store_const",
        const="never",
        help="disable .gitreview parsing",
    )
    parser.add_argument(
        "--gitreview-nearest",
        action="store_true",
        help="use nearest ancestor .gitreview instead of repository root only",
    )

    parser.add_argument(
        "--backend",
        choices=("auto", "fuse", "cas"),
        default="auto",
        help=(
            "search backend: "
            "auto = CAS-direct for --find, FUSE+rg for content; "
            "fuse = always use buildbox-fuse + ripgrep; "
            "cas = CAS-direct for --find only"
        ),
    )

    parser.add_argument(
        "--cas-dir",
        default=None,
        metavar="DIR",
        help="local BuildStream/BuildBox CAS cache directory",
    )
    parser.add_argument(
        "--mount-dir",
        default=os.path.expanduser("~/.cache/bst-source-grep/mounts"),
        metavar="DIR",
        help="directory where buildbox-fuse mounts are created",
    )
    parser.add_argument(
        "--digest-function",
        default="SHA256",
        choices=("SHA256", "SHA384", "SHA512", "SHA1", "MD5"),
        help="digest function used by buildbox-fuse (default: SHA256)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help=(
            "content-search trees mounted and searched concurrently "
            "(default: min(4, CPUs); 1 streams each tree's output as it is found)"
        ),
    )
    parser.add_argument(
        "--keep-mounts",
        action="store_true",
        help="do not unmount buildbox-fuse mounts created by this run on exit",
    )
    parser.add_argument(
        "--force-unmount",
        action="store_true",
        help=("unmount this run's mount points on exit; overrides --keep-mounts"),
    )
    parser.add_argument(
        "--buildbox-fuse",
        default=None,
        metavar="PATH",
        help="path to buildbox-fuse executable",
    )

    parser.add_argument(
        "--path-cache-dir",
        default=os.path.expanduser("~/.cache/bst-source-grep/path-index"),
        metavar="DIR",
        help="directory for CAS-direct --find path-index cache",
    )
    parser.add_argument(
        "--no-path-cache",
        dest="path_cache_enabled",
        action="store_false",
        default=True,
        help="disable path-index cache for CAS-direct --find",
    )
    parser.add_argument(
        "--rebuild-path-cache",
        action="store_true",
        help="ignore existing path-index cache and rebuild it",
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help="print statistics to stderr",
    )
    parser.add_argument(
        "--traceback",
        action="store_true",
        help="show Python traceback on errors",
    )

    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_intermixed_args(argv)

    if args.list_options and args.options_template:
        parser.error("--list-options cannot be used together with --options-template")

    if args.list_options or args.options_template:
        # These describe the project; a target or pattern is not needed.
        return args

    if args.all_elements:
        if args.pattern is not None:
            parser.error("TARGET cannot be used together with --all-elements")
        # With no TARGET, the only positional argument is the pattern.
        args.pattern, args.target = args.target, None
    elif args.target is None:
        parser.error("TARGET is required unless --all-elements is used")

    if args.fetch_sources:
        # Sources inside a junction cannot be fetched without its subproject.
        args.fetch_subprojects = True

    if args.find is None and args.pattern is None:
        parser.error("PATTERN is required unless --find is used")

    if args.find is not None and args.pattern is not None:
        parser.error("PATTERN cannot be used together with --find")

    if args.backend == "cas" and args.find is None:
        parser.error("--backend=cas currently only supports --find")

    if args.max_option_sets < 1:
        parser.error("--max-option-sets must be at least 1")

    if args.jobs is None:
        args.jobs = min(DEFAULT_JOBS, os.cpu_count() or 1)
    elif args.jobs < 1:
        parser.error("--jobs must be at least 1")

    return args


DEFAULT_JOBS = 4
