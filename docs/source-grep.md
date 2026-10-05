# bst-source-grep

## Contract

Search the combined, staged sources cached by BuildStream for a target and its
selected dependencies. The cache tree must already exist: fetching individual
plugin mirrors alone is insufficient. Run `bst source fetch --deps all TARGET`
first. No checkout per element is needed.

Requires Python 3.10+ and BuildStream >=2.8,<3 in the same environment. The tool
uses private BuildStream APIs. CI exercises 2.8.0 and the latest available 2.x;
future releases are not guaranteed compatible merely by satisfying the version
range. BuildStream's CAS daemon still runs in filename mode; “CAS-direct” means
no FUSE mount, not no daemon.

```sh
bst-source-grep TARGET PATTERN [options]
bst-source-grep TARGET --find GLOB [options]
```

| Option | Behavior |
| --- | --- |
| `--deps none/build/run/all` | BuildStream selection semantics; default all. Build selection excludes the target. |
| `--backend auto/cas/fuse` | Auto uses CAS for filename searches, FUSE + rg for content. CAS accepts only `--find`. |
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
| `--stats`, `--traceback` | Diagnostics to stderr. |

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
Source attribution is a heuristic, not proof of which overlapping source wrote
a file.

| Exit code | Meaning |
| --- | --- |
| 0 | At least one match and no search errors |
| 1 | No matches, including an entirely sourceless selection |
| 2 | Error or incomplete search, even if some matches were emitted |
| 130 | Interrupted |

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
Successful cleanup unmounts, reaps the process and removes mount/log paths.
`--keep-mounts` retains this run's mounts; `--force-unmount` overrides that flag.
Retained mounts are not reused by later searches. Failed mount attempts may leave
empty directories/logs for diagnosis. Unmount failures report the retained path.

`--cas-dir`, `--mount-dir`, `--buildbox-fuse` and `--digest-function` allow
explicit backend configuration. The tool discovers bundled FUSE executables
and supports `BUILDBOX_FUSE` as an environment override. Search processes spool
stderr to a temporary file while stdout is streamed; diagnostics are bounded
when reported. Interruptions terminate/reap rg before mount cleanup.

## Validation and next steps

Regression tests cover filters, source-key initialization, callback wiring,
file-only CAS traversal, digest extraction, origins, cache concurrency/newlines,
subprocess failures/interruption and mount isolation. An offline end-to-end test
fetches a local source with the real BuildStream CLI and checks cached find,
filters and exit statuses in CI. FUSE content mode still needs validation on a
host with `/dev/fuse`; it is not covered by the hosted CAS integration test.

Future work: a dedicated FUSE integration fixture, stricter validation of glob
extensions in filename mode, and extraction of a compatibility module when a
second tool needs the same BuildStream loading code.
