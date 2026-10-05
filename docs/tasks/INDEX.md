# Task index

Generated from `registry.json` by `python scripts/tasks.py render`.

See [registry conventions](README.md) before changing status or priority.

| ID | Tool | Task | Area | Priority | Status | Effort | Depends on |
| --- | --- | --- | --- | --- | --- | --- | --- |
| BSG-001 | bst-source-grep | Evaluate content globs relative to source roots | correctness | P1 | in_review | S | — |
| BSG-002 | bst-source-grep | Exit quietly when an output consumer closes its pipe | usability | P1 | in_review | S | — |
| BSG-003 | bst-source-grep | Select the same project options and directory as the build | correctness | P1 | in_review | S | — |
| BSG-004 | bst-source-grep | Treat CAS origin enrichment as optional metadata | robustness | P2 | in_review | S | — |
| BSG-005 | bst-source-grep | Distinguish unresolved refs from missing caches | usability | P2 | in_review | S | — |
| BSG-006 | bst-source-grep | Complete every owned mount teardown despite individual failures | robustness | P2 | in_review | S | — |
| BSG-007 | bst-source-grep | Stream normal output without retaining every match | performance | P2 | in_review | S | — |
| BSG-008 | bst-source-grep | Document per-mode runtime prerequisites | usability | P2 | in_review | S | — |
| BSG-009 | bst-source-grep | Require real FUSE integration in the compatibility matrix | maintainability | P1 | in_review | M | — |
| REP-001 | repository | Establish a machine-readable task registry | workflow | P1 | in_review | S | — |
| BSG-010 | bst-source-grep | Measure startup, traversal, search and memory costs | performance | P1 | ready | M | — |
| BSG-011 | bst-source-grep | Isolate the supported BuildStream compatibility boundary | maintainability | P1 | ready | M | — |
| BSG-012 | bst-source-grep | Mount and search one tree at a time | performance | P1 | proposed | M | BSG-009, BSG-010 |
| BSG-013 | bst-source-grep | Reduce cold filename traversal and warm-index filtering work | performance | P2 | proposed | M | BSG-010, BSG-016 |
| BSG-014 | bst-source-grep | Bound output buffering by bytes | performance | P2 | proposed | M | BSG-010 |
| BSG-015 | bst-source-grep | Provide actionable environment diagnostics | usability | P2 | ready | M | — |
| BSG-016 | bst-source-grep | Make filename glob limitations explicit and tested | usability | P1 | ready | S | — |
| BSG-017 | bst-source-grep | Add result limits and predictable pipeline modes | usability | P2 | proposed | M | BSG-012 |
| BSG-018 | bst-source-grep | Inspect and prune path-index cache safely | usability | P2 | proposed | M | — |
| BSG-019 | bst-source-grep | Publish concise recipes and output contracts | usability | P2 | ready | S | — |

## BSG-001: Evaluate content globs relative to source roots

Slash-containing rg globs see absolute mount paths and give wrong matches.

Proposed work: Run one rg from each mounted tree root; preserve rg pruning and relative output.

Acceptance:

- Include/exclude bdir/** works for content and -l in a real FUSE fixture.

Evidence:

- https://github.com/rmorozov/bst-utilities/pull/1#discussion_r4184599764
- Implementation and regression coverage in PR #1; mark done after merge.

## BSG-002: Exit quietly when an output consumer closes its pipe

Piping output to head can report bogus traversal errors and leak a traceback.

Proposed work: Keep emission outside traversal exception handling and return 141 after normal resource cleanup.

Acceptance:

- Closed content pipe produces no diagnostics and leaves no mounts.
- A child-process test verifies quiet stdout shutdown; traversal counters exclude emission errors.

Evidence:

- https://github.com/rmorozov/bst-utilities/pull/1#discussion_r4184600850
- Implementation and regression coverage in PR #1; mark done after merge.

## BSG-003: Select the same project options and directory as the build

Default project options can select a different cached source tree from the one used by the build.

Proposed work: Forward repeatable -o KEY VALUE and -C DIR to Project.

Acceptance:

- Both conditional source variants are selected correctly by the real CLI fixture.
- Repeated option keys use the last value.

Evidence:

- https://github.com/rmorozov/bst-utilities/pull/1#discussion_r4184601634
- Implementation and regression coverage in PR #1; mark done after merge.

## BSG-004: Treat CAS origin enrichment as optional metadata

Invalid UTF-8 or unavailable .gitreview metadata can abort otherwise valid searches.

Proposed work: Read bytes, decode with replacement and cache enrichment failure as unavailable metadata.

Acceptance:

- Invalid UTF-8 metadata does not affect exit status or file matches.
- Unavailable metadata is tested without suppressing actual file traversal failures.

Evidence:

- https://github.com/rmorozov/bst-utilities/pull/1#discussion_r4184602435
- Implementation and regression coverage in PR #1; mark done after merge.

## BSG-005: Distinguish unresolved refs from missing caches

Unresolved cache keys cause a leaked NoneType error and misleading fetch advice.

Proposed work: Check source resolution before querying the cache and print track/configure-ref guidance.

Acceptance:

- Real unresolved remote source returns 2 with actionable advice and no TypeError.

Evidence:

- https://github.com/rmorozov/bst-utilities/pull/1#discussion_r4184603116
- Implementation and regression coverage in PR #1; mark done after merge.

## BSG-006: Complete every owned mount teardown despite individual failures

Unmount/removal failures can skip later mounts, child reaping or stream cleanup.

Proposed work: Isolate each teardown, reap each owned child and guarantee stream cleanup.

Acceptance:

- Failure on the first mount does not skip later mounts.
- Failed unmount still reaps the child; live retained mounts are reported.

Evidence:

- https://github.com/rmorozov/bst-utilities/pull/1#discussion_r4184603785
- Implementation and regression coverage in PR #1; mark done after merge.

## BSG-007: Stream normal output without retaining every match

The dedup set grows with full match output and search_errors stays zero on rg failures.

Proposed work: Enable compact dedup keys only for junction stripping; count failed searches per tree.

Acceptance:

- Normal searches retain no dedup set.
- Stripped-name duplicate file/line records still collapse.
- An invalid regex returns 2 and increments search_errors in real FUSE CI.

Evidence:

- https://github.com/rmorozov/bst-utilities/pull/1#discussion_r4184604342
- Implementation and regression coverage in PR #1; mark done after merge.

## BSG-008: Document per-mode runtime prerequisites

Bubblewrap and the CAS daemon are needed even for filename searches.

Proposed work: List bwrap/buildbox-casd and the additional FUSE/rg/device/unmount requirements.

Acceptance:

- README and tool guide identify required binaries and device access.

Evidence:

- https://github.com/rmorozov/bst-utilities/pull/1#discussion_r4184604961
- Implementation and regression coverage in PR #1; mark done after merge.

## BSG-009: Require real FUSE integration in the compatibility matrix

CAS-only CI cannot detect content-root, process and mount lifecycle regressions.

Proposed work: Install FUSE prerequisites and require real FUSE checks on minimum and latest supported BuildStream.

Acceptance:

- CI fails when FUSE prerequisites are unavailable instead of silently skipping.
- Minimum and latest 2.x validate slash filters, error counters and closed-pipe cleanup.

Evidence:

- https://github.com/rmorozov/bst-utilities/pull/1

## REP-001: Establish a machine-readable task registry

Future tools and agent iterations need durable tasks with acceptance criteria and evidence.

Proposed work: Keep a canonical JSON registry, generated Markdown view and stdlib validator in CI.

Acceptance:

- Duplicate IDs, invalid fields, missing dependencies and dependency cycles are rejected.
- CI rejects a stale Markdown index.
- Agent instructions define status transitions and evidence requirements.

Evidence:

- https://github.com/rmorozov/bst-utilities/pull/1

## BSG-010: Measure startup, traversal, search and memory costs

Existing aggregate timings do not identify dominant costs; proposed speedups are hypotheses.

Proposed work: Create offline repeatable fixtures and a benchmark command separating fresh index, warm index, narrow glob, broad content and many identical source trees. Record versions, file/tree counts, peak RSS, mount count and process launches. Distinguish a fresh tool index from cold OS/BuildStream caches.

Acceptance:

- Fixtures include many small files, few large files, many unique trees and duplicate trees.
- Reports separate startup/load, traversal/index, mounts, rg search and output fan-out.
- Benchmark writes only temporary caches and records repeat distributions rather than a single best time.
- Establish baselines before adopting concurrency, SQLite or direct content blob search.

## BSG-011: Isolate the supported BuildStream compatibility boundary

A large CLI module mixes private API access, binary discovery, matching, origins and process cleanup; broad introspection hides unsupported shapes.

Proposed work: First extract a narrow BuildStream adapter and typed element/tree records, then backend/output boundaries only where tests justify them. Keep the CLI stable and helpers tool-local until another tool needs them.

Acceptance:

- Private API calls are concentrated behind the adapter.
- Minimum/latest compatibility tests and CLI/output contracts remain unchanged.
- Unexpected API failures produce a clear unsupported-version/shape diagnostic rather than a silent fallback.

## BSG-012: Mount and search one tree at a time

All source trees are mounted before searching, although rg now runs sequentially per tree. Peak mounts and resident daemon/process costs grow with unique trees.

Proposed work: Scope each mount/search/cleanup lifecycle to one tree by default; evaluate bounded concurrency only after baseline measurements.

Acceptance:

- Peak active owned mounts is one in serial mode.
- All element mappings, origins, partial-error statuses and interruption cleanup are preserved.
- Measure many-tree latency/RSS and identify any regressions against BSG-010.

## BSG-013: Reduce cold filename traversal and warm-index filtering work

Filename traversal stats every file and traverses all subdirectories; warm JSONL indexes still require a full scan.

Proposed work: Profile stat/metadata work and compile reusable filter predicates. Evaluate safe directory pruning for narrow include prefixes; consider an alternate index only when scan cost is material. Never publish a query-filtered index as a complete tree index.

Acceptance:

- Broad and narrow patterns preserve the same match set as the current implementation.
- Complete cached indexes remain valid across different later queries.
- Report measured wall-time/RSS tradeoffs on fresh and warm indexes before changing format.

## BSG-014: Bound output buffering by bytes

LineBuffer limits record count, so a small number of very long lines can still consume substantial memory.

Proposed work: Use a byte-aware buffer threshold and preserve streaming/backpressure behavior; document what rg itself buffers for long lines.

Acceptance:

- Short/long-line workloads preserve JSON and text output.
- Buffered memory has a documented bound except a single already-materialized match.
- Closed-pipe and interruption tests still pass.

## BSG-015: Provide actionable environment diagnostics

Users currently discover missing binaries, FUSE permissions or cache layout problems one failure at a time.

Proposed work: Design --doctor or equivalent diagnostics for installed versions, project options, selected backend, cache directory, binaries and device access. Stay offline and avoid mutating project refs.

Acceptance:

- Checks identify failure and next action without exposing credentials or private source origins by default.
- Diagnostics distinguish CLI installation, cache absence, unresolved refs and FUSE access.
- A machine-readable diagnostic format supports scripting.

## BSG-016: Make filename glob limitations explicit and tested

Filename matching implements a smaller glob dialect than rg; brace and negative include syntax can silently mean something different.

Proposed work: Define a shared documented subset and reject unsupported filename patterns clearly; decide whether full parity is worth an added library after differential tests.

Acceptance:

- A table covers basename, slash, **, classes, dotfiles, case rules, include unions and exclusion precedence.
- Unsupported brace/negative patterns never silently produce plausible wrong results.
- CAS and FUSE filename modes agree for the supported subset.

## BSG-017: Add result limits and predictable pipeline modes

Users pipe to head for the first useful result, but loading/mounting and scanning may continue until pipe backpressure is observed.

Proposed work: Design global --max-results and quiet mode, with an explicit definition of emitted versus unique results and partial-cache behavior; evaluate line-buffered output for interactive use.

Acceptance:

- Limits stop further scanning/mounting and reap active processes.
- No partial path index is published after early termination.
- Exit status, per-element fan-out and broken-pipe behavior are documented.

## BSG-018: Inspect and prune path-index cache safely

Versioned indexes accumulate and corrupt entries currently require manual rebuild flags; cache scope is opaque.

Proposed work: Add offline cache size/entry inspection and explicit prune/rebuild actions. Consider corruption fallback only with a visible diagnostic and no silently lost matches.

Acceptance:

- Inspection shows version, digest, entry count and disk footprint.
- Pruning does not remove in-use temporary indexes or unrelated caches.
- Concurrent writers and corrupt/truncated indexes have regression coverage.

## BSG-019: Publish concise recipes and output contracts

Many flags make common grep/find tasks hard to discover, and text output is ambiguous for unusual filenames.

Proposed work: Document copy-paste recipes for project variants, C/C++ searches, filename search, rg-like statuses, JSON piping and origin inspection; publish a versioned JSON Lines contract.

Acceptance:

- Recipes are exercised against a local fixture.
- Examples distinguish BuildStream builders/max-jobs from search behavior without adding unrelated knobs.
- JSON field/nullability and filename/encoding guarantees are explicit.
