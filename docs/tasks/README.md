# Task registry

[INDEX.md](INDEX.md) is the human-readable view. `registry.json` is the canonical
source for every tool in this repository. Keep stable IDs; never reuse a dropped
or completed ID. `BSG-*` covers bst-source-grep; `REP-*` covers repository workflow.
Assign a new prefix when another tool is introduced.

```sh
python scripts/tasks.py list --tool bst-source-grep --status ready
python scripts/tasks.py render
python scripts/tasks.py check
```

Every task records its tool, problem, proposed work, area, priority, status,
rough effort, acceptance criteria, dependencies and evidence. Evidence should
link the PR, issue, benchmark or test result that supports a status change.
The validator checks field types, enums, duplicate IDs, unknown dependencies,
cycles and required evidence; `check` also rejects a stale Markdown view.

| Status | Meaning |
| --- | --- |
| proposed | Idea or optimization hypothesis; scope/measurement still needs refinement |
| ready | Scoped enough to start; check dependencies before claiming it |
| in_progress | Active implementation; record the working branch/PR as evidence |
| in_review | Implementation and checks are available in an open PR |
| blocked | Record the reason in proposal/evidence and dependencies |
| done | Merged or otherwise delivered; acceptance criteria met with evidence |
| dropped | Retained for history, with the decision recorded in evidence |

A ready task is actionable only after its dependencies are done. Status changes
are explicit; the validator does not infer completion from GitHub automatically.
Bootstrap and review fixes are delivered in merged PR #1. Future implementation
tasks remain open; mark them done only after their delivery is confirmed.

P1 means address before expansion or optimize a likely significant bottleneck;
P2 means a useful follow-up; P3 means optional. S/M/L are relative effort estimates,
not delivery dates. Registry priority expresses an intended order, not a measured
performance claim. Reprioritize after benchmarks or user feedback.

## Suggested next sequence

1. **BSG-010: measure first.** Capture startup, source loading, fresh/warm index,
   many-tree content, output fan-out and peak RSS. Record exact versions and
   fixture dimensions; do not call a fresh tool index a cold filesystem cache.
2. **BSG-012: prioritize bounded mount lifetimes.** The reviewer measured 300
   distinct three-file source trees (two runs per commit): mount time 15.5 s,
   per-tree rg search time 1.55 s and total wall time 20.0 s. The measurements
   were reported in [PR review](https://github.com/rmorozov/bst-utilities/pull/1#discussion_r4184911451),
   not reproduced here. Reproduce that fixture, then compare serial scoped
   mount/search/cleanup with a small bounded pool. Concurrency gains remain a
   hypothesis; active mounts and cancellation cleanup must remain bounded.
3. **BSG-011 and BSG-016: establish boundaries and glob contracts.** The private
   BuildStream adapter and differential matcher tests reduce regression risk.
   These can proceed alongside the benchmark/lifecycle work; preserve CLI and
   result contracts while optimizing.
4. Use the results to choose **BSG-013** (traversal/filter/index work) or **BSG-014**
   (byte-bounded output). Avoid adopting parallel workers, SQLite or direct blob
   content search merely because they sound faster.
5. Deliver **BSG-015 / BSG-019** for diagnosis and recipes, then **BSG-017 / BSG-018**
   for early result limits and explicit cache management.

The registry keeps ten follow-up tasks separate from the review fixes. The next
work can be selected by acceptance criteria rather than another broad rewrite.
