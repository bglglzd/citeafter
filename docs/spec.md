# Specification

Citeafter is for an engineer maintaining answers and drafts derived from changing commercial policies. It stores explicit dependencies and source support so an update can identify the decisions requiring another check.

## Main journey

1. Import a synthetic policy v1 and save cited discount and shipping decisions. Keep an already released Friday operation and two pending Tuesday drafts.
2. Import v2, observed on Friday and effective on Monday. A changed discount parameter makes the Tuesday drafts stale. Friday's record retains its original support; unrelated shipping decisions remain current. Changed normative text receives `needs_review`.
3. Inspect the changed field, claim and action path. Recheck the affected drafts, producing new immutable revisions and current source support.
4. Explicitly release an exact supported revision to the local SQLite outbox. A stale preview, changed source, unresolved dependency or unavailable source blocks release.
5. Import v3 with only a schema-declared presentation typo corrected. Equivalent support remains usable without recomputing every recipe. A previously stale, unrechecked v1 decision does not become current merely because v2 and v3 are equivalent.
6. Replay a historical query using both its business date and knowledge cutoff. Previously released records remain immutable.

## Source and temporal contract

The supported input is one closed typed JSON policy format, with stable clause IDs and explicit-offset `effective_from`. Each version is a full snapshot with an explicit parent. Ingestion assigns `observed_at`; publisher content cannot supply it. Source identity includes exact received UTF-8 bytes and SHA256.

At `(valid_at, known_at)`, only assertions observed by the knowledge cutoff and effective at the business time qualify. A unique eligible descendant supersedes its ancestors. Equal-effective corrections need an explicit parent chain. Incomparable eligible forks, missing known parents, cycles and conflicting bytes under the same version label require review. They are never resolved by choosing the newest observation.

The renderer has a version and hash. Exact citations refer to UTF-8 byte spans in that labelled rendered policy, bound to original JSON identity. Old citations never move to another version. JSON Pointer comparisons use decoded token paths, including parent replacement/removal.

## Impact, result and permission

`affected`, `unaffected` and `needs_review` describe support changes. A changed business dependency can be affected even when its recomputed numeric result is identical. Unsupported semantics do not become unaffected because a graph edge is absent.

Supported executable recipes record every policy field they read and their exact inputs/evaluator identity. Discount quotations use explicit thresholds, rates and currency with exact decimal rounding; shipping decisions use a supported destination and day count. Manual claims and unsupported currency/destination cases need review. Only the declared presentation field is automatically non-normative; arbitrary text is not classified as a harmless typo.

An action's intended business time is fixed. Rechecking creates a new draft revision and retains prior support. History controls are read-only and cannot change the action's dispatch context. There is no scheduling/wait-until-business-time rule: a future draft can be eligible using current available knowledge while the operator leaves it pending.

A preview binds the exact action revision, assessment, source/freshness generation and payload. Final release checks these facts inside one SQLite write transaction and inserts one idempotent local outbox record. If another process imports a changed source between preview and release, the old preview must fail without a dispatch. A refreshed preview can authorize an unaffected action without rerunning its recipe. Released records return their original receipt when repeated.

## Real transport and recovery

The HTTP source adapter uses an operator-selected pinned endpoint, bounded JSON/UTF-8 input, explicit timeout and conditional ETag requests. It follows no redirects or implicit retries. A304 validates a prior cached representation; it does not supply a business-effective date. Availability/freshness and semantic impact remain separate. Timeout, malformed response or removal cannot silently preserve release permission.

SQLite retains original source/claim/action revisions, assessments and outbox bindings across restart. Integrity or citation corruption fails closed and preserves original data. Tests must exercise actual separate-process updates, preview races, idempotency and failure recovery.

## Interface and scope

The local UI exposes source versions, separate business/knowledge selectors, impact paths, citations, recheck and an exact action gate with a concrete reason. CLI and UI use the same Python engine. Public static demonstrations inspect labelled recorded synthetic outputs; full mutation runs locally. Each control must change useful state or expose needed evidence. There is no presentation slogan or decorative metric.

The synthetic policies are not customer data or a user study. This format does not interpret arbitrary PDFs, laws, tax rules or free-form policy semantics. Local outbox release is the advertised side effect, not evidence of an email, payment or regulated operation.

## Observable acceptance

- Three policy versions and thirty annotated decisions total, with twenty development cases and ten independently authored held-out cases created only after the evaluation rules freeze.
- Every explicit supported dependency change found, with no unrelated stale declaration on the fixed set. Full three-class matrix and false/missed invalidations remain visible.
- Correct source selection before, at and after business/knowledge boundaries; historical release never rewritten by later knowledge.
- Ambiguous/unsupported support blocks action; cosmetic correction preserves valid support without clearing previously unresolved staleness.
- No release from an obsolete preview or unresolved/freshness failure. Restart/repetition creates no second logical dispatch.
- Actual HTTP200/304/change/timeout/malformed/removal/recovery outcomes and useful UI, installation and packaged artifacts verified independently.
- Report supported-subset precision/recall and size, unsafe releases, false blocks, temporal errors, rechecked versus full decisions, edges visited and workload-specific timing. Saved data or agreement with a full recomputation alone is not proof of correctness.
