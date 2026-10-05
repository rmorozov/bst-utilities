# Contributing

Follow [AGENTS.md](AGENTS.md) for the repository layout and validation commands.
For each new helper, add a module and console entry point, a document under
`docs/`, tests and a README tool-catalog entry. Declare heavy dependencies as
extras so unrelated helpers remain usable.

Issues should describe a reproducible problem or workflow, expected behavior and
BuildStream/Python versions. Work on a branch and submit a focused PR using the
provided template. Address review findings and CI failures before merging.
The BuildStream integration matrix checks 2.8.0 and the newest available 2.x;
keep fixtures local and caches isolated.

Use [the task registry](docs/tasks/README.md) to claim work, track dependencies,
and record acceptance evidence. Edit only the relevant `docs/tasks/items/<ID>.json`
files; run `python scripts/tasks.py check`. Mark tasks done in the implementation
PR after acceptance checks pass; merge delivers that status without a follow-up
PR. Generated reports are optional and are not committed. See
[task conventions](docs/tasks/README.md) for concurrent work.

Documentation-only PRs run lightweight task validation and focused tooling tests.
Code, dependencies, scripts, tests and workflow changes run the full test matrices.
Manual CI runs always run the full suite; `CI result` reports the selected result.
