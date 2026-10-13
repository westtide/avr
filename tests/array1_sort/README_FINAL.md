# AVR Array1 correctness regressions

The final implementation is described in [IMPLEMENTATION_20261010T050558Z.md](IMPLEMENTATION_20261010T050558Z.md).
`cases_portable_payload.json` covers 33 mathematical SAFE/UNSAFE fixtures;
`cases_explicit_unsupported_v2.json` covers three explicit backend limits.
Model paths are relative to the manifests, and all 36 fixtures are committed.

Use a new output directory and an available CPU. From the fork directory:

```sh
python3 tests/array1_sort/run_regression_v4.py --engine avr \
  --backend y2bt --avr-root . --avr-bin build/bin --witness \
  --cpu 104 --output /absolute/new-regression-output
```

`--backend y2` checks the second compiled backend. Add
`--manifest tests/array1_sort/cases_explicit_unsupported_v2.json` to check explicit
Unsupported outcomes. Each run uses 1 CPU, 15 GiB and at most 30 seconds per
case, requires a parser pass, and records witnesses separately from verdicts.
Assertions, f_err and crashes are failures even after a printed answer.

`replay_after_init_check.py` preserves the original witness and only creates a
separate equivalent copy after checking explicit initialized array values
against the model. Uninitialized state and input assignments remain intact.

`prepare_failed_subset.py`, `rerun_failed_subset_v2.py` and
`assess_failed_rerun.py` reproduce this workspace's pinned historical failure
experiment and exact-query reference comparison. These experiment scripts
require the recorded workspace and frozen resource helper; they are not a
standalone test framework. Outputs must be new paths. Historical input/output
files and binaries are never removed or overwritten.
