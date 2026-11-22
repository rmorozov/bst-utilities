# Where to spend the next second in bst-source-grep

Start by fixing which workloads the benchmark actually runs. Then measure the
whole command on a fetched project before choosing a runtime change. A faster
blob scan helps a broad content query; it cannot shorten the project load that
precedes every option set.

## What the PR #17 measurements tell us

The [performance review](https://github.com/rmorozov/bst-utilities/pull/17#pullrequestreview-6013606582)
measured head `099292b` with Python 3.12.14, BuildStream 2.8.1 Directory
records and ripgrep 15.2.0. The numbers below are medians of five warm runs
after one warm-up on tmpfs. Search timings include the submodule check, search
and emission to /dev/null, but exclude startup, graph loading and cache
completeness checks. The last row times only the lookup function. Prototypes
were local experiments; none of their speedups is an end-to-end prediction.

| Scenario | Merged implementation | Observation or prototype |
| --- | ---: | --- |
| 8,000 distinct small files, fixed-string miss / 160 matching lines | 55.8 / 61.3 ms | Four rg threads; one thread took 80.6 ms for the sparse match. |
| Same files in 100 directories; `--glob 'd007/**'` | 16.6 ms | Traversing only the selected subtree took 6.4 ms. Both searched 80 blobs and emitted identical output. |
| 64 three-file trees / 64 elements sharing one three-file tree | 9.3 / 3.2 ms | Both emitted 192 records; the shared case searched 3 blobs instead of 192. |
| Eight paths share one blob with 16,000 matching lines | 2.8 ms filenames; 266 ms text; 404 ms JSON | A length-prefixed match spool reduced text output time to 161 ms. Text and JSON each matched their original output byte for byte. |
| Warm filename index, 8,000 paths: `*.c` / absent basename | 10.5 / 8.5 ms | A chunked NUL-delimited reader took 6.4 / 3.5 ms. An on-disk comparison is needed before changing the persistent format. |
| 20,000 origin lookups, 128 staged submodules | 118 ms | Looking up successive parent directories in a map took 5 ms. Trees without submodules already have a faster shortcut. |

## Two decisions before changing the runtime

1. [BSG-037: make the benchmark distinguish workloads](tasks/items/BSG-037.json).
   A host with rg but no FUSE currently skips CAS content scenarios. The four
   nominal 16 MiB files in the large-file fixture also contain identical bytes,
   so CAS scans only one blob. Exercise both shared and distinct contents;
   show skips and the number of files and blobs actually searched.
2. [BSG-038: measure the real query](tasks/items/BSG-038.json). Use a pinned,
   already fetched project, the same target, options file and pattern, and two
   output modes. For example, in Bash, with TARGET and PATTERN set:

   ```bash
   args=("$TARGET" "$PATTERN" --all-options --options-file opts.yml --origin --json --stats)
   lines_rc=0; bst-source-grep "${args[@]}" >lines.jsonl 2>lines.stats || lines_rc=$?
   files_rc=0; bst-source-grep "${args[@]}" -l >files.jsonl 2>files.stats || files_rc=$?
   printf 'lines=%s files=%s\n' "$lines_rc" "$files_rc"
   ```

   Check both exit statuses and confirm complete searches before comparing
   results. For a nonempty fixed test query, compare the distinct element and
   normalized Gerrit project pairs (use the same locale for both files):

   ```bash
   set -o pipefail
   for mode in lines files; do
     jq -c 'select(.origin.gerrit_project_effective | strings | length > 0)
            | [.element, .origin.gerrit_project_effective]' "$mode.jsonl" |
       LC_ALL=C sort -u >"$mode.pairs"
   done
   diff -u lines.pairs files.pairs
   ```

   Use this capture for parity; time separate runs with the same sink so the
   cost of writing different output volumes to disk is identifiable. Report
   wall time, time to first record (or no record), project load, cache checks,
   search, option sets, result count, files/blobs and peak tool RSS. Record
   versions, filesystem, jobs, cache state and run-to-run spread. Pair equality
   justifies recommending `-l` for this mapping; it does not replace a query
   that needs matching lines or their positions.

## Which change to try next

| If the representative run spends time here… | Try this change | Criterion for proceeding |
| --- | --- | --- |
| Walking excluded directories for a narrow include | [BSG-039: prune literal directory prefixes](tasks/items/BSG-039.json) | Fewer Directory reads, identical results and order, explicit treatment of traversal errors; broad globs retain the full walk. |
| Replaying dense matches at many paths | [BSG-040: avoid the second JSON round trip](tasks/items/BSG-040.json) | Faster dense output with bounded memory, equivalent binary/text handling and interruption cleanup. |
| Reading a warm filename index repeatedly | [BSG-041: compare path-index encodings](tasks/items/BSG-041.json) | On-disk gains justify a versioned format; no incomplete index can become visible. |
| Attributing many paths in trees with many submodules | [BSG-042: index staged module paths](tasks/items/BSG-042.json) | Faster origin output, unchanged innermost-module attribution and no regression for trees without modules. |

If project loading or completeness checks dominate wall time, record that split
before pursuing a faster inner loop. Keep completeness verification and offline
behavior intact. Track each selected implementation and its acceptance evidence
in its own task file and implementation PR.
