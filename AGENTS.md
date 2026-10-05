# Working in bst-utilities

This repository contains independent helpers for BuildStream 2.8+ interactions.
Read README.md and the affected tool's document in docs/ before changing behavior.

## Structure

- `src/bst_utilities/`: Python modules, one command per tool. Keep shared code here
  only when at least two tools actually need it.
- `pyproject.toml`: command entry points, dependency extras, validation settings.
- `tests/`: regression tests and isolated integration fixtures.
- `docs/`: per-tool contracts, design notes, review findings and roadmap.

## Agent work loop

1. Read the issue/request and relevant code. Identify the observable problem and
   acceptance criteria; record material assumptions in the PR.
2. Use a focused branch. For behavior fixes, reproduce the failure and add a
   regression test that checks behavior. For a new tool, add its CLI contract,
   documentation, entry point and tests together.
3. Implement the smallest complete change. Preserve unrelated tools and command
   contracts. Do not grow a shared framework before it has a concrete consumer.
4. Run the checks below. Review the diff for errors, side effects and unrelated
   changes. Report any checks that could not run and why.
5. Open/update a PR with the problem, resulting behavior, validation and remaining
   limitations. Inspect CI and review comments; fix actionable findings, rerun
   affected checks, and update the description to match the final implementation.
6. Merge only when the user has authorized merging and required checks pass.
   Do not silently change branch protection, bypass failures or enable auto-merge.

## Commands

Use an isolated Python environment; for tools that inspect a user's BuildStream
installation, install into that same environment.

```sh
python -m pip install -e '.[dev,buildstream]' build
ruff check .
ruff format --check .
pytest -ra
python -m build
bst-source-grep --help
```

## BuildStream and process constraints

- Confirm private APIs against actual BuildStream source. Test the minimum 2.8.0
  and the latest available 2.x in CI. State limitations rather than claiming all
  future releases are verified.
- Keep searches offline by default. Never invoke source track/fetch or connect
  remote caches implicitly. `--fetch-subprojects` is explicit authorization for
  fetching junctions only.
- Integration tests must use temporary project/config/cache paths. Never modify
  the developer's BuildStream cache or real project refs in tests.
- Stream large search output. Reap child processes on failure and interruption;
  avoid deadlocks between stdout and stderr. Respect mount ownership.
- Preserve exit statuses: matches 0, no matches 1, errors/partial searches 2,
  interruption 130. Diagnostics go to stderr; JSON Lines stays parseable.
- Cache writes must be atomic, safe under concurrent writers, and versioned when
  the format changes. Never publish a partial index.
- Never add credentials, source-cache contents or private source origins to git.

No hosted AI credential or unattended write automation is required by this repo.
Agents use the same branch, PR, review and CI process as human contributors.
