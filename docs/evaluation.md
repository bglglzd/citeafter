# Evaluation

These results describe one fixed synthetic workload, not customer accuracy or a language model. No model is used. The original failed comparisons remain in the published report.

The workload is three policy versions and 30 annotated decisions: 20 development cases and 10 fresh-context cases authored after parser, selection, dependencies, freshness, release, evaluator and runtime were frozen. [Provenance and component hashes](results/provenance.json) bind the chronology and original case bytes. The cases are now public regression material; they are no longer blind.

| Observation | Development | First held-out attempt |
| --- | ---: | ---: |
| Fully matched annotated decisions | 20 / 20 | **8 / 10** |
| Post-v2 support class matched | 20 / 20 | 10 / 10 |
| Historical source/result queries matched | 140 / 140 | 70 / 70 |
| Structured binary subset | 14 | 6 |
| Affected TP / FP / FN | 8 / 0 / 0 | 2 / 0 / 0 |
| Unsafe local outbox releases | 0 | 0 |
| False release blocks | 0 | 0 |

The combined class matrix has 10 affected, 10 unaffected and 10 needs-review decisions on its diagonal, with no off-diagonal classifications. This does **not** make the full annotation comparison 30/30: the original strict total is 28/30. Affected precision and recall are 1.0 on the explicitly supported binary subset of 20; manual/unsupported cases are reported separately.

## Original held-out disagreements

The [first held-out report](results/heldout-first-v1.json) remains byte-exact, with `passed:false` and six `reason_paths` mismatches across the two v3 branches and v2. The [development report](results/development-frozen-v1.json) uses the same frozen component hashes. No rule, scorer or original label was changed after held-out inspection.

- H07 is blocked for unsupported currency. Its changed rate is present in recorded reads, `changed_required_paths` and the reverse path, but the blocking `reason_paths` field names only currency. The annotation expected both paths.
- H09 is a manual claim with unproven dependency coverage and an unchanged shipping declaration. The blocking field names the primary manual clause; the annotation also expected the shipping context and a more specific manual-text pointer. No complete coverage, changed shipping edge or supported result is inferred.

Both actions remain blocked with null output and no outbox record. The contract does not define blocking `reason_paths` as an exhaustive union of changed and contextual fields; this remains an explanation-completeness / annotation-semantics limitation. The strict mismatch is retained. Three development diagnostics also disclose ancestor-versus-leaf reason specificity. These are not rewritten as matching labels.

## Execution and timing

Across the 30 decisions, v2 rechecking invokes the recipe evaluator 10 times. A full path invokes it 30 times and produces 20 supported results. Inspection, source updates and release checks perform no recipe evaluation. All decisions still receive lightweight dependency/temporal inspection. The first holdout visits four changed reverse edges; development visits twelve.

This is a reduction in recipe calls, **not measured latency savings**. The recipes are inexpensive, while immutable ledger writes and validation add work. Timing in each report includes Python profiling and independent per-case SQLite databases; it is not a shared 30-decision batch or production throughput benchmark. The first holdout ran on Windows, CPython 3.11.15, SQLite 3.53.1, Intel Core i7-12700K (20 logical CPUs). Its v2 update plus recheck totals about 240 ms, compared with about 53 ms for full read-only recomputation. Exact timings and environment are in the report.

## Reproduce

From a source checkout:

```sh
python -m citeafter.evaluation --cases citeafter/examples/development --output reports/dev
python -m citeafter.evaluation --cases citeafter/examples/heldout --output reports/heldout
```

Choose new output directories: previous attempts are never overwritten. The development command exits 0. The original held-out comparison exits **1** because the six diagnostic-path mismatches are preserved. This expected strict failure must not be represented as a passing comparison. Runtime performance can differ; input and component hashes remain inspectable.

Primary correctness comparisons use independently authored expected classes, sources, exact outputs, dependencies and temporal queries. A fresh full-recomputation path is a secondary comparison. Agreement between two implementation paths alone is not accuracy evidence. Manual claims have unproven coverage and stay `needs_review`, including when their declared edge points at unchanged shipping data.

Report all three classes, the explicitly eligible structured binary subset and its denominator, affected precision/recall, false/missed invalidations, unsafe releases, false blocks, source-selection mistakes, unsupported cases and citation/dependency mismatches. Count recipe executions separately from lightweight temporal/dependency inspection. The two v3 branches are alternative handling histories of the same decisions; they do not inflate the30-decision denominator.

The interactive six-case example is an inspection journey, not the full evaluation corpus. Mutation, parser, corruption, transport and concurrent-process tests are separate from decision accuracy. Timing must name workload, runtime, machine and whether database setup, inspection or arithmetic is included. Synthetic results do not establish performance on customer policies or arbitrary prose.
