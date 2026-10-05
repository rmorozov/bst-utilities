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
and record acceptance evidence. Update `registry.json` and regenerate its index
with `python scripts/tasks.py render`; CI validates the registry and rendered view.
