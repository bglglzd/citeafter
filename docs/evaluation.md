# Evaluation

Evaluation is in development; no final accuracy or performance result is claimed here.

The workload is three synthetic commercial-policy versions and30 annotated decisions total:20 development cases and10 fresh-context cases authored only after parser/selection/dependency/gate/evaluator rules and runtime are frozen. Original gold, first failures and component identities are retained. A previously revealed set is regression material, not a future blind test.

Primary correctness comparisons use independently authored expected classes, sources, exact outputs, dependencies and temporal queries. A fresh full-recomputation path is a secondary comparison. Agreement between two implementation paths alone is not accuracy evidence. Manual claims have unproven coverage and stay `needs_review`, including when their declared edge points at unchanged shipping data.

Report all three classes, the explicitly eligible structured binary subset and its denominator, affected precision/recall, false/missed invalidations, unsafe releases, false blocks, source-selection mistakes, unsupported cases and citation/dependency mismatches. Count recipe executions separately from lightweight temporal/dependency inspection. The two v3 branches are alternative handling histories of the same decisions; they do not inflate the30-decision denominator.

The interactive six-case example is an inspection journey, not the full evaluation corpus. Mutation, parser, corruption, transport and concurrent-process tests are separate from decision accuracy. Timing must name workload, runtime, machine and whether database setup, inspection or arithmetic is included. Synthetic results do not establish performance on customer policies or arbitrary prose.
