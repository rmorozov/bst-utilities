# bst-source-grep

## Contract

Search the combined, staged sources cached by BuildStream for a target and its
selected dependencies. The cache tree must already exist: fetching individual
plugin mirrors alone is insufficient. Run `bst source fetch --deps all TARGET`
first. No checkout per element is needed.

Requires Python 3.10+ and BuildStream >=2.8,<3 in the same environment. The tool
uses private BuildStream APIs. CI exercises 2.8.0 and the latest available 2.x;
future releases are not guaranteed compatible merely by satisfying the version
range. On Linux, both modes require `bubblewrap` (`bwrap`) and `buildbox-casd`
(the daemon is normally bundled with the BuildStream wheel). FUSE additionally
requires `buildbox-fuse`, `rg`, a usable `/dev/fuse`, and `fusermount3`/`fusermount`.
BuildStream's CAS daemon still runs in filename mode; “CAS-direct” means
no FUSE mount, not no daemon.

```sh
bst-source-grep TARGET PATTERN [options]
bst-source-grep TARGET --find GLOB [options]
```

| Option | Behavior |
| --- | --- |
| `--deps none/build/run/all` | BuildStream selection semantics; default all. Build selection excludes the target. |
| `--backend auto/cas/fuse` | Auto uses CAS for filename searches, FUSE + rg for content. CAS accepts only `--find`. |
| `-C / --directory DIR`, `-o / --option KEY VALUE` | Select the project directory and project options used when fetching/building; repeat options, last value wins. |
| `--config FILE` | BuildStream user configuration, including cache location and project overrides. |
| `--glob GLOB`, `--exclude GLOB` | Repeatable file filters. Filename includes are ORed; excludes always win and also match ancestors. |
| `-i`, `-F`, `-n`, `-l` | Ignore case, literal content pattern, show line numbers, filenames with content matches. `-i` also applies to the find pattern. |
| `--binary-files skip/text` | Default skip; text mode searches binary contents through rg. |
| `--json` | JSON Lines: file records or match records containing element, recipe, path and optionally origin. |
| `--origin` | Best-effort source metadata; overlapping sources can be ambiguous. |
| `--gitreview auto/always/never` | Supplement Gerrit origins from cached `.gitreview`; auto only when metadata has no usable Gerrit project. |
| `--gitreview-nearest` | Use nearest ancestor `.gitreview`; otherwise use tree root. Works with both backends. |
| `--strip-junctions` / `--unique-recipes` | Remove junction prefixes and deduplicate identical recipe/path records. Can collapse distinct junction instances. |
| `--fetch-subprojects` | Explicitly allow fetching missing junction sources required to load the project. Default is refuse with a diagnostic. |
| `-j / --jobs N` | Content-search trees mounted and searched concurrently; default min(4, CPUs). `1` streams each tree's output as rg produces it. |
| `--stats`, `--traceback` | Diagnostics to stderr. Stats include per-phase load, mount, search and cleanup time, peak mounts, search jobs, and buildbox-fuse/rg process counts. With more than one job, phase times are summed across trees and can exceed the total. |

Filename globs support `*`, `?`, character classes and `**`. A pattern with no
slash matches the basename at any depth; slash patterns match the entire
relative path. Quote globs in the shell. Filename search only emits regular
files, including hidden files, excluding `.git` entries and symlinks. Content
filters use ripgrep's glob syntax, which additionally supports brace alternation
and negative include patterns; these extensions are not implemented by the
filename matcher. `-F`, `-n`, `-l` and `--binary-files` affect content search.

Text output is `element:path:text` (with `-n`, `element:path:line:text`) or
`element:path` for file results. Origins add the source id before the path.
Use JSON output when names contain newlines or colons. Non-UTF-8 names/content
are escaped in JSON when supplied by rg; plain text can replace invalid bytes.
Content globs are evaluated relative to each source-tree root. Each distinct
mounted tree gets one rg process so the requested root remains correct and
ripgrep can prune excluded paths. Normal searches retain no output deduplication
set; `--strip-junctions` retains compact path/line/text keys because display names
can collide; identical lines collapse, different lines at the same position do not. Source attribution is a heuristic, not proof of which overlapping source wrote
a file.

| Exit code | Meaning |
| --- | --- |
| 0 | At least one match and no search errors |
| 1 | No matches, including an entirely sourceless selection |
| 2 | Error or incomplete search, even if some matches were emitted |
| 130 | Interrupted |
| 141 | Output pipe closed by a reader (e.g. `head`); quiet exit after cleanup |

## Cache and mounts

The path index defaults to `~/.cache/bst-source-grep/path-index`. It stores one
JSON string per line, keyed by tree digest and format version. Concurrent writers
use distinct temporary files and atomic replacement. Missing CAS objects during
index construction never publish a partial index. An existing index records the
paths from its successful construction; it does not validate every content blob
on a cache hit. Corrupt indexes produce an error; rebuild with
`--rebuild-path-cache` or bypass with `--no-path-cache`. Old `.paths` files from
the attached prototype are ignored and can be removed manually.

FUSE mounts default to `~/.cache/bst-source-grep/mounts`. Each run creates its own
mount directory, so another search cannot unmount it while it is being read.
Each unique source tree is mounted, searched with one rg, then unmounted: the
buildbox-fuse process is reaped and mount/log paths removed. Up to `--jobs` trees
do this concurrently; each rg writes to a temporary spool file and the main thread
emits trees in their original order, so output is identical to `--jobs 1`. A tree
is unmounted as soon as its rg finishes (with `--origin`, after its output is
emitted, because `.gitreview` lookups read the mount). A run therefore holds at
most `--jobs` owned mounts (one more with `--origin`), and spool space is bounded by the output of about
`--jobs` + 1 trees. `--jobs 1`, or a search with a single tree, uses the serial
streaming path, so output starts as soon as rg produces it and needs no spool. Mount readiness is polled
from 1 ms with exponential backoff up to 50 ms, so a quick buildbox-fuse start is
not rounded up to a fixed poll interval.
`--keep-mounts` retains this run's mounts (so every searched tree stays mounted
until exit); `--force-unmount` overrides that flag.
Retained mounts are not reused by later searches. Failed mount attempts may leave
empty directories/logs for diagnosis. Unmount failures report the retained path.

`--cas-dir`, `--mount-dir`, `--buildbox-fuse` and `--digest-function` allow
explicit backend configuration. The tool discovers bundled FUSE executables
and supports `BUILDBOX_FUSE` as an environment override. Cleanup attempts every owned mount and reaps its process even when an unmount
or directory removal fails. Search processes spool
stderr to a temporary file while stdout is streamed; diagnostics are bounded
when reported. Interruptions and closed pipes stop queued trees, terminate/reap
every running rg (SIGKILL after a 2 s grace if SIGTERM is ignored) and wait for
workers before mount cleanup.

Filename searches without a warm path index read the CAS Directory records
directly (sorted files, then sorted subdirectories, as BuildStream lists them),
so memory does not grow with the number of trees. While loading, each unique
source tree's cache completeness check (casd `FetchTree`) runs once, concurrently,
instead of serially per element. The checks are gRPC futures awaited on the
main thread; an interrupt cancels every one still in flight.

## Validation and next steps

Regression tests cover filters, source-key initialization, callback/options
wiring, regular-file CAS traversal, best-effort origins, cache concurrency,
closed pipes, subprocess failures, deduplication state and cleanup failures.
CI requires CAS and real FUSE integration against BuildStream 2.8.0 and latest
2.x: slash globs, `-l`, conditional sources, unresolved refs, non-UTF-8 metadata,
error counters and closed content-output pipes. Restricted local environments
may skip daemon tests; CI checks FUSE prerequisites and does not accept that skip.

## Benchmarks

`python scripts/bench_source_grep.py [--scale tiny|default] [--repeats N]` builds
offline fixtures (many small files, a few large files, many unique trees, many
elements sharing one tree) in a temporary directory, fetches them with the real
`bst`, and reports median (min-max) per scenario: interpreter startup, project
load, mount, search, cleanup, remaining BuildStream/casd time, tool and process-tree
peak RSS, results, peak mounts and rg processes. `--json-out FILE` keeps raw runs.
Content scenarios are skipped without `/dev/fuse` and `rg`. Each "fresh index" run
rebuilds the tool's path index; it does not drop OS or BuildStream caches.

`--project DIR --target ELEMENT [--config FILE]` benchmarks an existing project
whose sources are already fetched instead (find with fresh, warm and no index, a
rare file name, a no-match full content scan, `-l`, and `-n` fan-out). It never
fetches. `--allow-partial` accepts exit status 2 when the only cause is uncached or
unresolved elements, so a partly fetched project still yields numbers; the table
shows the uncached count. The manual `Benchmark freedesktop-sdk` workflow
(`.github/workflows/bench-fdsdk.yml`) fetches a pinned freedesktop-sdk on a GitHub
runner and runs this mode; it also runs on pull requests that change it or the
benchmark script.

On 300 unique three-file trees (BuildStream 2.8.0, 4 CPUs), scoped mounts with
backoff polling took the content search from 20.0 s (15.4 s of it mounting) to
5.1 s, with one live mount instead of 300.

On freedesktop-sdk `sdk.bst` (615 elements, 468 trees, 4-CPU GitHub runner) the
medians were: find with a warm index 32 s (24 s of it project load), without one
63-68 s at 1.64 GiB peak RSS, and a no-match content scan 326 s (282 s search,
13 s releases). See BSG-013, BSG-021 and BSG-024 for the follow-ups.

See [the task registry](tasks/README.md) for prioritized next steps, including
benchmarking, module boundaries, bounded mounts and improved diagnostics.
