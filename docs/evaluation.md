# Evaluation

These are agent-authored synthetic policies and decisions, not customer accuracy or a human study. No language model is used. Original labels, failed reports and later runs are published separately.

## Current frozen implementation

Source review found that a manual claim could omit evidence for its own clause. [Dependency diagnostics revision 2](dependency-contract-v2.md) now records that clause's kind and normative text alongside declared evidence, preserves changed dependencies in blocked explanations, and keeps manual coverage unproven. Manual claims cannot release.

After the second freeze, a separate fresh-context curator authored and sealed ten new cases, J01–J10, without reading implementation or product outputs. The first score ran from a clean checkout before the author inspected their contents. [Provenance](results/provenance.json) binds the chronology, components, original case bytes and reports. The scorer was unchanged. These published cases are now regression data.

| First revision-2 held-out attempt | Result |
| --- | ---: |
| Fully matched decisions | 10 / 10 |
| Historical source/result queries | 120 / 120 |
| Affected / unaffected / needs-review | 2 / 2 / 6 |
| Citation/dependency mismatches | 0 |
| Unsafe releases / previews | 0 / 0 |
| False blocks / temporal errors | 0 / 0 |
| Supported binary subset | 4 |
| Affected TP / FP / FN | 2 / 0 / 0 |

Affected precision and recall are 1.0 on that four-case supported subset. Six unsupported/manual cases are reported separately. This small fixed set does not predict accuracy on arbitrary policies.

The [raw first report](results/heldout-first-v2.json) records 71 preview attempts, five obsolete-preview release probes and one explicit release followed by receipt replay. Two v3 handling branches are alternative histories of the same decisions, not twenty cases.

## Original experiment and retained failures

The first freeze used twenty development decisions and ten subsequently authored held-out cases. The [first development report](results/development-frozen-v1.json) matched 20/20. The [first held-out report](results/heldout-first-v1.json) remains byte-exact: `passed:false`, 8/10 fully matched cases and six `reason_paths` disagreements across H07/H09 and their branches. All thirty original support classes, outputs and temporal queries matched; the strict comparison still failed.

H07 omitted a changed rate from its blocking explanation although the change appeared elsewhere. H09 did not read or cite its own manual clause when the caller declared only unrelated shipping evidence, so its normative-text change had no reverse edge. Source review prompted revision 2 and a new blind set. Both original cases were blocked with null output and no outbox entry.

Revision 2 against the unchanged original labels produces these historical regressions:

| Original labels, current rules | Strict result | Differences |
| --- | ---: | --- |
| [Development](results/development-regression-v2.json) | **18 / 20**, `passed:false` | D17 expects currency-only reasons; D19 expects shipping-only reads and the old manual explanation. Seven comparisons differ. |
| [Held-out](results/heldout-regression-v2.json) | **9 / 10**, `passed:false` | H09 expects the old shipping-only initial reads. One comparison differs. |

Their classes, outputs, history, release safety and false-block counts remain correct. Labels and scorer were not rewritten. These failed historical comparisons are not new blind results or adjusted passing scores. Do not combine different frozen implementations into one accuracy number.

## Work and timing

The new ten-case workload performs two actual recipe rechecks and visits seven changed reverse edges. Full recomputation invokes recipes ten times and yields four supported results. Inspection and source updates perform no recipe evaluation; every decision still receives dependency/temporal inspection.

Fewer recipe calls did **not** improve latency. On Windows, CPython 3.11.15, SQLite 3.53.1 and an Intel Core i7-12700K (20 logical CPUs), summed v2 update plus recheck time was about 232 ms, versus about 48 ms for full read-only recomputation. Measurements include profiling and independent per-case SQLite databases, not shared-ledger throughput. Transactions verify stored history and source checks append assessments; cost grows with retained history. No large-ledger throughput claim is made.

## Reproduce

From a source checkout, choose new output directories:

```sh
python -m citeafter.evaluation --cases examples/heldout-v2 --output reports/current-heldout
python -m citeafter.evaluation --cases citeafter/examples/development --output reports/original-development
python -m citeafter.evaluation --cases citeafter/examples/heldout --output reports/original-heldout
```

The J comparison exits 0. Both comparisons against original labels exit **1** with the retained differences above. Previous attempts are never overwritten. The source package includes J01–J10; the wheel contains the original cases and can evaluate a supplied case directory with `--cases`.

Primary comparisons use independently authored classes, sources, outputs, dependencies, gates and historical queries. Full recomputation is a secondary comparison against that gold; agreement between implementation paths alone is not correctness evidence. Parser, corruption, HTTP, race and UI recovery checks do not inflate decision denominators.
