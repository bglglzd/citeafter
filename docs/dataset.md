# Synthetic cases

The initial dataset contains three commercial-policy snapshots and twenty annotated decisions, D01–D20. A coding agent specified the expected source choices, results, dependency changes and gates before any Citeafter core execution; a separate agent read the labels independently. This is synthetic agent-authored data, not customer data or a human study.

The policy bodies contain no trusted observation timestamps. [Fixture observations](../citeafter/examples/observations.json) supply an explicitly injected clock: v1 on 3 January 2030, v2 on Friday 4 January, and v3 on Tuesday 8 January. V2 becomes effective on Monday 7 January. V3 corrects only a schema-declared presentation title. Operational ingestion assigns its own observation time.

Cases cover Friday history, Tuesday pending drafts, exact threshold and rounding, below-threshold equal results despite changed dependencies, effective/knowledge boundaries, unrelated shipping, normative ambiguity, unsupported currency/destination and unproven manual dependencies. Original citations must survive semantic equivalence and historical release replay.

D10 is created just before v2 observation; D11 exactly at that boundary and therefore starts on v2. D19 deliberately declares an incomplete manual dependency list; an unchanged declared edge does not prove unaffected support. D01 and D20 request explicit fresh checks at their release instants. An old preview can be rejected because its generation changed while the decision itself remains semantically current.

The original post-policy-v2 labels are eight affected, six unaffected and six needs-review cases. The binary structured subset has fourteen cases; the other six remain in the full three-class workload. These are predeclared expectations, not measured precision, recall or performance. History-query rows and alternative handling branches do not add decisions to the denominator.

Ten additional cases H01–H10 were authored after the first freeze. Their failed first result and original labels remain unchanged. Following a dependency-diagnostic fix and second freeze, another independent curator authored J01–J10, including manual evidence with unrelated, empty and ancestor declarations. These ten cases have twelve historical queries each. All forty cases are now public regression material; the separate [evaluation reports](evaluation.md) preserve each experiment's actual outcomes and denominators.
