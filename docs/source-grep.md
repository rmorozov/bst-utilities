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
| `--all-options`, `--max-option-sets N` | Search the union of sources reached under every combination of the toplevel project's options (see below); `-o` pins an option. Refuses more than N sets (default 64). |
| `--list-options`, `--options-template` | Print the toplevel project's options (type, default, values, how a search uses them) or an `--options-file` template, then exit. No target is needed. |
| `--options-file FILE` | `name: value` pins an option; `name: [a, b]` limits `--all-options` to those values. `-o` overrides the file. |
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

## Searching every option set

Project options change which sources a target reaches. `(?)` conditionals can
replace or extend an element's `sources`, add or replace `depends` (so whole
elements enter or leave the tree), and an option with `variable:` is expanded
into source configuration such as a path or URL. Options never propagate into
junctioned subprojects implicitly, but a junction's `config: options:` can
forward a parent option value (`'%{arch}'`) or set one conditionally.
`project.refs` can also be conditional. A search with the default or `-o`
options therefore misses sources that other configurations use.

`--all-options` reads the option declarations in the toplevel `project.conf` and
loads the target once per combination, each as the toplevel project of one
BuildStream session. Junction-forwarded options follow each combination; options
a subproject declares but its junction does not set stay at their defaults.

| Type | Enumerated values |
| --- | --- |
| `bool` | `false`, `true` |
| `enum`, `arch`, `os` | Every listed value (foreign architectures load without building) |
| `flags` | Every subset. BuildStream cannot parse an empty flags value from the command line, so the empty set is applied as a user-configuration override for that load |
| `element-mask` | Not enumerated (its values are every `.bst` file); held at the configured value |

Options pinned with `-o` are held. Enumerated values override user-configuration
option values. Options declared only through a project.conf include are not
seen before loading; a note names each one held at its configured value. The
project-default combination is loaded first when every default is a listed value.

The product of value counts (2^N for N flags) must not exceed `--max-option-sets`
(default 64); it is computed before any value is enumerated, and otherwise the run
fails before loading and names each option's count, so you can pin some with
`-o`. After each load the resolved values are compared with the planned set; a
mismatch is reported as a load failure for that set. Every combination costs a full project load; cache checks are
shared, so a tree reached by several combinations, even a single new one, is checked
and searched once.

### Narrowing long option lists

Pinning every option with `-o` does not scale, and pinning loses the sources the
other values reach. Instead, list or template the options and edit a file:

```sh
bst-source-grep --list-options
bst-source-grep --options-template > search-options.yml
$EDITOR search-options.yml
bst-source-grep TARGET PATTERN --all-options --options-file search-options.yml
```

The template has an `options:` mapping with every option commented out, each
under a comment giving its type, default and values. The example line lists every
value (`# arch: [x86_64, aarch64, riscv64]`); uncomment it and delete the values
you do not need. In the file:

| Entry | Meaning |
| --- | --- |
| `name: value` | Pin to one value; not enumerated or shown in `option_sets` |
| `name: [v1, v2]` | `--all-options` enumerates only these values (shown in `option_sets`) |
| `flags: [a, b]` or `'a,b'` | Pin a flags or element-mask option to that set (`[]` is the empty set) |
| `flags: [[a], [a, b], []]` | `--all-options` enumerates only these sets |

`-o KEY VALUE` overrides the file for that option. Without `--all-options`, a
one-value list is a pin and a longer list is an error. `--list-options` also
reads `-o` and `--options-file` and shows how many values each option
contributes, so you can see the product before running a search. `--options-template
--options-file FILE` writes that file's pins and value lists back as active entries,
so a template can be regenerated without losing choices. Values that are not plain
words are written as double-quoted YAML strings. When the cap is
exceeded, the error points to these options. Values are validated against
`project.conf`; unknown names or values fail before loading.

A combination rejected by a project `(!)` assertion is skipped with a `NOTE` and
counted in `--stats`. Any other load failure, and every uncached or unresolved
tree, is reported with its option set (`[options: arch=aarch64 debug=true]`) and
makes the exit status 2. Fetch each combination you want covered, e.g.
`bst -o arch aarch64 source fetch --deps all TARGET`.

Text output is unchanged; one record is printed per element and tree, however
many combinations reached it. JSON records add `option_sets`, the list of
enumerated `{option: value}` sets that reached that element's tree, in load
order. The same element name can therefore appear in several records when its
sources differ between combinations. With `--strip-junctions`, JSON records are
only collapsed when their option sets are equal too, so no set loses attribution;
text output collapses as before.

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
runner and runs this mode. It runs only when started by hand (Actions tab or
API, on any branch), because a run takes about 45 minutes.

On 300 unique three-file trees (BuildStream 2.8.0, 4 CPUs), scoped mounts with
backoff polling took the content search from 20.0 s (15.4 s of it mounting) to
5.1 s, with one live mount instead of 300.

On freedesktop-sdk `sdk.bst` (615 elements, 468 trees, 4-CPU GitHub runner) the
medians were: find with a warm index 32 s (24 s of it project load), without one
63-68 s at 1.64 GiB peak RSS, and a no-match content scan 326 s (282 s search,
13 s releases). After BSG-013, BSG-021 and BSG-024: find with a warm index
24 s, without one 28 s at 120 MiB, and the same scan 239 s with `--jobs 4`
(374 s with `--jobs 1`). Load is now 17.5-20 s, of which 1.4 s is project
load and the rest casd verifying the ~600k file blobs of 468 trees.

See [the task registry](tasks/README.md) for prioritized next steps, including
benchmarking, module boundaries, bounded mounts and improved diagnostics.

## Implementation layout

See [the development guide](source-grep-development.md) for module boundaries,
resource ownership and validation. The public command and module entry point
remain unchanged.
