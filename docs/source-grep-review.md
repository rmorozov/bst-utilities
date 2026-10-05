# Initial source-grep review

Reviewed the attached standalone script before importing it as the first tool.

| Finding | Fix |
| --- | --- |
| `need_state=False` leaves source cache keys unresolved before cache query | Initialize `ElementSources` resolved state before querying, without artifact-state initialization. |
| Project expects a fetch callback, but receives a boolean | Supply `Stream.fetch_subprojects` only when authorized; otherwise provide a callback that refuses fetching with an actionable diagnostic. |
| TypeError fallback may hide an internal error and drop requested dependency selection | Make one explicit supported selection call; propagate failures. |
| Sourceless selection inherits initial error status | Start a successfully loaded search at no-matches status; actual failures override it. |
| CAS filename search ignores include/exclude filters and `.git` policy | Apply one filename filter to both backends. |
| CAS listing includes directories/symlinks while rg lists regular files | Filter using BuildStream's Directory stat API. |
| Path index has a shared `.tmp` name and newline-delimited raw filenames | Use unique temporary files, atomic replacement and versioned JSON Lines. |
| rg may block on a full stderr pipe; interrupted searches can leave children | Spool stderr separately and always terminate/reap children. |
| Filename output splits newline-containing paths; byte-valued rg JSON is discarded | Read NUL-delimited filenames and decode text/bytes JSON variants. |
| `.gitreview` enrichment only works through FUSE | Read `.gitreview` through CAS Directory APIs too. |
| Concurrent searches reuse mounts that another run can remove | Create private mount paths per run; preserve within-run tree deduplication. |
| `umount -u` is invalid; timeout cleanup does not reap children | Use the appropriate command arguments and bounded terminate/wait/kill. |

Added a package entry point, documentation, developer extras, agent/contributor
instructions, issue/PR templates, CI and Dependabot configuration.

Local validation: regression tests run with BuildStream 2.8.0, including its actual
CasBasedDirectory over an isolated file-backed blob store. Daemon integration
is skipped where Unix sockets are prohibited. CI must validate the real CLI
fetch/search path. FUSE execution has not been validated on this host.

## PR review follow-up

All eight review threads are addressed in the implementation and regression tests:
root-relative content globs; quiet closed-pipe cleanup; project directory/options;
best-effort binary `.gitreview` reads; actionable unresolved-ref diagnostics;
per-mount cleanup isolation and process reaping; compact optional deduplication
plus search-error counters; and documented bubblewrap/CAS prerequisites.

A required FUSE fixture now checks content slash globs and exclusions, filenames
with matches, invalid-pattern statistics and closed-pipe cleanup in CI.
