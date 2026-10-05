# bst-utilities

Helpers for automating BuildStream 2.8+ interactions. Each tool has its own
command, documentation and tests; shared code is introduced when tools need it.

| Command | Purpose | Documentation |
| --- | --- | --- |
| `bst-source-grep` | Find filenames or search contents across cached element sources without checking out each element | [Guide](docs/source-grep.md) |

## Install

Install into the environment containing your project's BuildStream installation:

```sh
/path/to/project/.venv/bin/python -m pip install /path/to/bst-utilities
```

Or create a new environment with BuildStream:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[buildstream]'
```

On Linux, install `bubblewrap` (`bwrap`) even for filename searches; BuildStream
also needs `buildbox-casd`, normally bundled with its wheel.
Filename search traverses CAS directory metadata directly. Content search also
requires Linux FUSE access (`/dev/fuse`), `buildbox-fuse`, ripgrep (`rg`) and `fusermount3` or
`fusermount`. A BuildStream wheel may bundle buildbox-fuse; use an explicit path
if automatic discovery fails.

## Search a project

Run from inside your BuildStream project after fetching the desired sources:

```sh
bst source fetch --deps all default_elements.bst
bst-source-grep default_elements.bst --find '*.h'
bst-source-grep default_elements.bst 'pthread_create' -n --glob '*.c'
bst-source-grep default_elements.bst --find '*.h' --exclude 'vendor/**' --json --stats
```

Source tracking is optional when you want to update refs; this helper never
tracks or fetches element sources. Missing caches produce exit status 2.

## Development and agent workflow

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md). Changes follow
issue/request → focused branch → regression tests → PR → CI/review → authorized
merge. CI checks Python versions, package builds and BuildStream 2.8.0 plus the
newest available 2.x. Agents receive explicit contracts for offline behavior,
cache safety, process cleanup and review handling.

[Initial review](docs/source-grep-review.md) records the fixes and validation
limits. The repository uses the Apache-2.0 license.
