# Tasks

Each task has its own canonical file in [items/](items/), named `<ID>.json`.
There is no shared registry or checked-in generated index to update. Keep stable
IDs; never reuse a dropped or completed ID. `BSG-*` covers bst-source-grep;
`REP-*` covers repository workflow. Assign a prefix when another tool is added.

```sh
python scripts/tasks.py list --tool bst-source-grep --status ready
python scripts/tasks.py check
python scripts/tasks.py render > /tmp/bst-tasks.md
```

`render` prints an optional Markdown report; do not commit the generated report.
Each UTF-8 JSON file contains `schema_version: 1` and a `task` object with the
existing ID, tool, title, problem, proposal, area, priority, status, effort,
acceptance, dependencies and evidence fields. The validator loads all files in
stable ID order and checks filenames, field types, duplicate IDs, unknown
dependencies, cycles and required evidence.

| Status | Meaning |
| --- | --- |
| proposed | Idea or hypothesis; scope/measurement still needs refinement |
| ready | Scoped enough to start; check dependencies before claiming it |
| in_progress | Active implementation; record the working branch/PR as evidence |
| in_review | Work is awaiting review or still lacks acceptance evidence |
| blocked | Record the reason in proposal/evidence and dependencies |
| done | Acceptance criteria met in this branch; delivery is effective when merged |
| dropped | Retained for history, with the decision recorded in evidence |

## One PR per change

Include the task file and implementation in the same PR. Once acceptance checks
pass, set the task to `done` in that PR and record the check results and PR/branch
reference as evidence. Review and merge state live in GitHub, so `in_review` is
optional; there is no second PR to mark work done after merge. On `main`, `done`
means delivered. On an unmerged branch, it means ready for delivery; do not treat
another branch's tasks as delivered dependencies. If review changes invalidate
acceptance, reopen the task and update its evidence before merging.

For concurrent work, edit only the task files you own. Record ownership through
the working branch/PR; consult open PRs before claiming an existing task. Choose
an unused ID on main and in open PRs when adding a task. Independent task edits
merge independently; edits to the same task need coordination. Validation of
merged files catches missing dependencies and cycles, but does not provide a
locking service. Rebase onto current main and rerun validation before merging.
Do not create separate claim/status-only PRs as a routine requirement.

P1 means address before expansion or optimize a likely significant bottleneck;
P2 means a useful follow-up; P3 means optional. S/M/L are relative effort estimates,
not delivery dates. Registry priority expresses an intended order, not a measured
performance claim. Reprioritize after benchmarks or user feedback.

## Suggested next sequence

1. **BSG-010 / BSG-012 (done): measure, then bound mount lifetimes.** The
   benchmark (`scripts/bench_source_grep.py`) reproduced the 300-tree baseline:
   the fixed 50 ms readiness poll, not buildbox-fuse startup (~3 ms), dominated
   mounting. Scoped serial mounts with backoff polling and SIGTERM-driven
   unmounts cut that scenario from 20.0 s to 5.1 s with one live mount.
2. **Real workload (freedesktop-sdk `sdk.bst`, 615 elements, 468 trees, 4 CPUs).**
   The fdsdk benchmark workflow measured three costs that the fixtures hid:
   **BSG-013**, a find without a warm index peaks at 1.64 GiB RSS because BuildStream
   directory objects are retained (118 MiB warm); **BSG-024**, 24-28 s of project
   load and cache-state resolution precede every query; **BSG-021**, a full
   content scan spends 282 s in one rg at a time plus 13 s of releases.
3. **BSG-013, then BSG-021 and BSG-024.** Memory first (bounded, small change),
   then the pool for content scans and load-phase profiling.
4. **BSG-011 and BSG-016: establish boundaries and glob contracts.** The private
   BuildStream adapter and differential matcher tests reduce regression risk;
   BSG-024 depends on the adapter. Preserve CLI and result contracts while
   optimizing. Choose **BSG-014** (byte-bounded output) when output volume matters.
5. Deliver **BSG-015 / BSG-019** for diagnosis and recipes, then **BSG-017 / BSG-018**
   for early result limits and explicit cache management.

The registry keeps ten follow-up tasks separate from the review fixes. The next
work can be selected by acceptance criteria rather than another broad rewrite.
