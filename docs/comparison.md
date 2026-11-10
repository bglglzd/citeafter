# Related tools

Reviewed 10 November 2026. Temporal history, lineage and dependency checks are established capabilities; no global novelty claim is made.

| Tool | Existing capability | Citeafter's scope |
| --- | --- | --- |
| [XTDB](https://docs.xtdb.com/about/time-in-xtdb.html) | Database queries over valid and system time. | A small application for one typed policy: saved field reads, rendered citations, claim/action revisions and a local release gate. It does not replace a temporal database. |
| [Dagster asset checks](https://dagster.io/docs/guides/test/asset-checks) | Checks on data assets, including schema, values and freshness, within an orchestration system. | Explicit field-to-claim-to-pending-action paths, selected for each action's business date and current knowledge, with a transaction-bound preview. It is not an orchestration platform. |

Choose the larger systems when you need their database or orchestration capabilities. Citeafter's supported format and two executable recipes keep this particular decision workflow inspectable and runnable without additional services.
