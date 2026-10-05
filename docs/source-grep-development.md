# Developing bst-source-grep

`src/bst_utilities/source_grep.py` is the command entry point. Both the installed
`bst-source-grep` command and `python -m bst_utilities.source_grep` call the same
application. Tool-local implementation modules live in
`src/bst_utilities/_source_grep/`; they are not separate user commands or a shared
framework for other tools.

| Module | Responsibility |
| --- | --- |
| `application.py` | Select the backend, own the BuildStream session, dispatch searches, finalize resources and exit statuses |
| `cli.py` | Declare options and validate combinations; importing/help requires no BuildStream installation |
| `adapter.py` | Load/version-check BuildStream internals, configure project/selection and message callbacks |
| `source_cache.py` | Resolve element source state and issue cancellable concurrent cache-completeness checks |
| `cas_layout.py` | Obtain directory digests and discover the configured local CAS location |
| `catalogue.py` | Group selected elements into unique source trees and preserve their labels/mappings |
| `cas.py` | Traverse CAS directories and read/write complete atomic path indexes |
| `matching.py`, `paths.py` | Filename glob/filter rules and path/name normalization |
| `discovery.py` | Locate bundled or explicitly configured buildbox-fuse executables |
| `mounts.py` | Own mount readiness, release and cleanup; never reuse another run's mounts |
| `processes.py` | Child-process streaming/spooling, termination escalation, cancellation and ordered worker scheduling |
| `ripgrep.py` | Construct rg commands and decode file/JSON match records |
| `search_cas.py` | Run indexed or direct filename searches and preserve partial-result statuses |
| `search_fuse.py` | Run serial or ordered pooled mounted searches and retain mounts for origin lookups when needed |
| `origins.py` | Source metadata attribution and optional cached .gitreview enrichment |
| `output.py` | Format text/JSON records, buffer output and deduplicate stripped junction records |
| `stats.py` | Initialize counters and format per-phase diagnostics |

The application owns session and final mount cleanup. Search backends own their
iteration and per-tree lifetime, and emit through `ResultEmitter`. Formatting
knows nothing about BuildStream sessions or mounts. Cross-module calls use the
owning module explicitly, so tests can patch the same boundary that production
calls use. Keep lower-level modules independent of the application/entry point;
do not add an import back to the old facade to access helpers.

This extraction preserves existing CLI, output, cache format, ordering and
cleanup behavior, including the fixes already on PR #6. It does not change
BuildStream compatibility policy or replace existing introspection fallbacks.
BSG-011 still tracks stricter unsupported-shape diagnostics and typed records.

Run the standard commands in AGENTS.md. `tests/test_source_grep_modules.py`
checks dependency-free module help, output formats and backend order/partial
errors across the new boundaries. Existing CAS, mount, process and integration
regressions remain in place and import/patch their owning modules. GitHub CI
runs actual BuildStream 2.8.0/latest 2.x with FUSE; restricted local environments
can skip integration tests when Unix sockets or FUSE are unavailable.

The benchmark harness imports `discovery.find_buildbox_fuse` directly, while all
benchmark subprocesses continue to use `python -m bst_utilities.source_grep`.
Generated reports and temporary benchmark caches are not repository artifacts.
