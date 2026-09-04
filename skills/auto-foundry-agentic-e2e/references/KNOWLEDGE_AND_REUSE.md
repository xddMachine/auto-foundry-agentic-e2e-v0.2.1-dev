# Progressive Living Enterprise Model and reuse

The program owns one logical run-local data room, immutable per-generation data
revisions/catalogs, and compact accepted semantic/prepared indexes. Every
Requirement Mode item binds directly to the same `RunContext`, while its
physical inputs are program-bound to the active generation's immutable D
revision. A context is immutable within an attempt; later uploads publish D
successors at a safe generation boundary and never rebind active calculations.
Old exact D/G bindings remain replayable. Items do not inherit a previous
item's context or implementation identity. Only committed integration
semantics and prepared assets selected through `AnalystWorkspace` are reused.
One run-level event-driven **Planner** is a control plane and cognitive
scheduler. Its initial order/grouping is advisory and preserves explicit user
priority/order; it never declares runtime semantic dependencies, which the
Analytical Owner discovers after understanding the requirement. The runtime
`waiting_on_resolution`/`ready_to_resume` ledger is the sole semantic block.
Its revisionable `RequirementExecutionPlan` and
`RequirementExecutionGroup` values are scheduling recommendations, not
catalog-hash or lifecycle authority. The Planner does not replace the owner,
create child lifecycles, or become a separate navigation role or gate.

## Data room and source catalog

Each immutable data revision builds one physical, searchable source catalog from
its ZIP/archive and member metadata before item analysis. Catalog entries are bounded and may
include source/member IDs, archive/member locations, formats, byte counts,
hashes, bounded columns/types, bounded samples or values, workbook sheet or
SQLite table metadata. Parquet metadata and batches are read natively; SQLite
databases are opened read-only and contribute one entry per user table. Unknown,
extensionless, notebook, and auxiliary files remain safe opaque members with no
semantic parser; they can be copied only through explicit materialization. The
raw archive remains read-only; the catalog is not a transaction copy. Source/member
reads are observed in passive telemetry.

Physical binding is generation-level: the active revision's archive/member
inventory and catalog are reused by contexts in that generation, while
selected-member verification is counted separately. A pending successor is
not active until its generation boundary is admitted. An explicit final
`verify_source_full()` detects a late mutation. Opaque members have no semantic
parser; copy them only through the safe explicit materialization operation.

## Entity-resolution domains

When the Analytical Owner proposes a new arbitrary real-world identity domain
during scouting, reserve that exact owner-bound proposal as `resolving` and
launch one Entity Resolution Owner. The Planner does not invent or pre-reserve
domains. A current item may wait for the commit; an accepted/integrated item may
also leave a proposal for later reuse. Domain scope is not a hardcoded Supplier/Factory/Order list;
strongly coupled classes may share a domain. Capacity is adaptive to the actual
host: the scheduler leases the smallest useful set for genuinely independent
work without oversubscription. Every requirement has exactly one Analytical
Owner, reviewers remain fresh and independent, and the Planner is not counted
or leased. Hosts may configure available capacity, but the scheduler must never
oversubscribe it.

The owner scans every row of domain-relevant tables and relevant documents from
reservation hints, expanding only for concrete matching/conflict evidence and
reusing the run-level catalog. It owns methodology and may inspect manually,
write Python/SQL/scripts, use existing helpers, infer and bulk-apply
patterns when justified, test samples/coverage/exceptions/population
differences, and revise its method. Manual row-by-row review, an authoritative
crosswalk, or a fixed matching script is not required. Pattern rules remain run
knowledge; future helper-library audit is deferred.

The review decision is binary per proposed mapping (accepted or not accepted).
Each accepted `CanonicalMapping` may contain one or many source identities or
representations, including bulk pattern-derived populations. Unresolved or
ambiguous records stay source-local and outside canonical mappings with
coverage and exceptions preserved; accepted mappings are never downgraded. A
ready publication contains the canonical class, source-account representation
classes, reviewed `IdentityDecision`/`CanonicalMapping` records, identity
`represents` relationships, and a versioned mapping asset with coverage where
available. Owners see only `resolving` or `ready` snapshots, never partial
mappings; the ready snapshot is exposed only after the reviewed result commits
atomically.
An explicit `no_mapping_found` result is also terminal when it contains
population, coverage, unresolved records, and evidence; it publishes no
ontology, decisions, mappings, or relationships. An unexplained empty result
is rejected before review.

The resolution job is separate from the owner loop and does not answer a
requirement. If an owner needs a domain that is `resolving`, it reports
`waiting_on_resolution` and releases its lane. The Planner skips to the next
original-order runnable item and marks the earliest paused item
`ready_to_resume` when the domain is ready. If all runnable items wait, the owner lane sleeps while active
resolvers progress; block only when nothing is runnable and no resolver can
progress.

## Two linked layers

Keep these run-local layers separate but link them with evidence references:

### Enterprise Ontology

An extensible, non-transaction representation of business objects, fields,
grains, relationships, aliases, sources, documents, metric meanings, reusable
metric definitions, applicable rules, process definitions, conflicts, effective
periods, and known limits. It captures reusable understanding; it does not copy
rows from a source and is never a central ontology. Current counts, shares,
amounts, values, ranks, top-N rows, and dimensional observations remain
accepted results, claims, dashboard facts, evidence, or prepared assets;
`add_metric` records an observation and never promotes it into ontology.

### Prepared Data Registry

Analysis first writes a candidate asset and descriptor atomically below the
current item's `work/prepared/` directory. A candidate records source
references, an exact asset ID, loadable location, content hash, byte/row counts,
schema, grain, lineage/source IDs, scope, effective period, transformations,
evidence, and limits, but it is not registry state. After item acceptance, one
Result Integration Agent stages that descriptor and the accepted commit
performs exact path/hash/row/byte/scope/provenance checks before calling the
accepted-only registry API. Every accepted asset is registered once in a
canonical catalog whose identity is immutable by source hash, core version,
and schema. Samples and categories are derived views; scope and reuse
eligibility control visibility only. Exact retries are idempotent; a conflicting
same-ID descriptor fails before registry/LEM mutation.

Entries may be source-scoped when their evidence, scope, and period support
reuse. Keep reusable preparation distinct from a requirement-scoped view and
never reuse an asset outside its recorded boundaries. Neither layer is a
cross-run cache.

When present, `effective_period` is part of the candidate descriptor and
sidecar, operation manifest/hash inputs, accepted integration record, and
registry entry, and is preserved by later search/select/reuse. Omission remains
valid and means no period constraint; the program never infers one from the
current date.

Both layers start empty at the start of a clean-room run. Compact source,
ontology, and prepared indexes may be searched, but the Analytical Owner uses
`AnalystWorkspace` to select relevant IDs. The later-item snapshot is
manifest-bound, hash-checked, and read-only: it contains accepted ontology and
relationship descriptors plus accepted prepared descriptors, not current rows
or metrics. There is no Navigator, descriptor/typed-validation role, or
per-item Capability Catalog compliance artifact.

Committed typed integration records are the sole durable authority for the
cumulative LEM. Before a later item stages integration, the program validates
and replays prior commits in lifecycle order into a fresh read-only projection.
It rejects gaps, reordering, collisions, and tampered records. Agents never
provide a cumulative LEM, and there is no second checkpoint or transaction log
that can drift from accepted item commits.

## Before analysis: inspect and select exact reuse

The Analytical Owner starts each item with a readiness/scouting pass: call
`AnalystWorkspace.brief()` and search the compact accepted indexes with
`search_ontology()` and `search_prepared_assets()`. Explicitly search/select
identity mappings and relationships as well as ontology and prepared assets, or
record why none is relevant. `brief()` exposes availability counts for ontology
items, relationships, and prepared assets without loading rows. Search returns
descriptors only.

When a descriptor is useful, the owner calls `select_ontology()` or
`select_prepared_assets()` with exact accepted IDs and a human-readable purpose.
The run may publish multiple successive immutable content-addressed semantic
snapshots and loads their layers on demand as commits or refreshes change the
semantic projection. Each distinct snapshot manifest is stored once in the
run-local namespace; each layer/index blob is stored once per distinct
canonical byte hash under `semantic_store/blobs/<sha256>.json` and reused by
every snapshot that references it. Snapshot directories contain only the
manifest. Exact per-layer IDs are stored once in a content-addressed selection
asset; `work/semantic_selections.jsonl` contains only the selection
reference/hash/counts, purpose, snapshot/context bindings, and reuse/no-reuse
decision. Selection never broadens scope or silently merges similar labels.

Rows are loaded only after exact prepared-asset selection. The
`load_prepared_asset()` call rechecks registry visibility, location, schema,
lineage, row/byte counts, and content hash before returning rows. If no
accepted descriptor applies, inspect the bounded source catalog and establish
new semantics; do not claim reuse from a nominal name match.

## Durable item workspace and bounded selection

The program creates `questions/<id>/work` or `requirements/<id>/work`,
authoritative `item_state.json`, and immutable `BoundAnalysisContext` before
invoking the Analytical Owner. The owner records strategy, selected sources,
evidence, specialist memos, calculations, and the final answer through
`AnalystWorkspace`. The program
validates every selected ID deterministically:
existence, current-run ownership, expected layer/type, allowed scope,
effective period, hash/location, schema, grain, lineage, and evidence
references. Missing, duplicated, or out-of-scope IDs are a bundle failure; do
not guess or broaden the selection.

In Requirement Mode, the Planner receives exact `RequirementRecord` values,
compact physical catalog metadata, and current outcomes. Its initial order and
grouping are advisory; it does not predeclare runtime semantic dependencies.
It recommends only the smallest useful set of owner specialists for genuinely
independent uncertainty, bounded by actual host capacity; zero is valid and it
never creates one specialist per method or checklist item. It revises the
current recommendation and preserves the exact input records. It reads no rows or
internal artifacts and does not calculate or write answers. A technical failure
does not create Planner dependency blocks; independent groups remain eligible
and runtime resolution state controls waiting and resume. Each requirement creates an item-local `RequirementAnalysisPlan` with 1..N
`RequirementAnalysisTask` values and follows the ordinary loop of analysis,
review, iterative material repairs, accept or `technical_failure`, and
integration. Within a group one Analytical Owner remains per requirement;
bounded shared investigation may be reused and independent groups may run when
host capacity permits.

After acceptance, exactly one Result Integration Agent incrementally consumes
claims, metrics, limitations, evidence refs, prepared assets, ontology,
relationships, and dashboard facts through small program APIs. It performs
semantic mapping; deterministic code validates types, paths, refs, hashes,
stages, and commits. Mechanical validation cannot prove semantic completeness.
Exactly one fresh item-only Integration Fidelity Reviewer checks the staged
current item after mechanical validation and before commit; the same Result
Integration Agent may make one targeted repair and receives one targeted
recheck. The packet excludes siblings, cumulative state, prior memory, and
broad workspace context. There is no prose parser, semantic compiler, giant
mandatory JSON, minimum record count, or reviewer chain.

The integration boundary publishes material reusable semantics actually
established: business objects and table mappings; grain; key fields and
normalization; relationship/cardinality/coverage/date authority/limits; and
descriptors for truly reusable prepared assets. It does not publish every
merge, every result row, metric observation, Japan/Spain filter, or
question-specific aggregation as reusable knowledge. `no_change` remains a
valid result only when the item established no reusable semantic understanding
or asset, with a concrete reason; it is not the default for every answer. The
existing Integration Fidelity Reviewer uses its current review to check this
semantic correctness. No new role, gate, mandatory large schema, or minimum
count is introduced.

Current measured values use the explicit `CurrentObservationFact` boundary and
remain dashboard facts, not ontology definitions. Their `as_of` comes only from
observed timestamps, never a future due/target date. Repeated observation
shapes may produce advisory semantic-promotion suggestions; suggestions do not
mutate the LEM and cannot block a run. Prepared assets remain optional and are
published only when later requirements can genuinely reuse the same typed rows.

The Analytical Owner establishes actual business joins and relationships and
records `source_id`/`target_id`, `join_keys`, grain, cardinality,
`matched_pairs` (the unique tested edge-pair count),
`source_population`/`target_population`,
`matched_source_count`/`matched_target_count` (distinct matched endpoints),
and `source_coverage`/`target_coverage` (endpoint count divided by its
population, with zero for a zero population), plus `as_of`/date authority,
limitations, and evidence. Integration publishes only
reviewed tested relationships plus reviewed canonical identity mappings; it
does not complete a theoretical graph or infer relationships from prose.

For the Q1→Q2/Q9 path, Q1 may publish order-header, order-line, delivery,
customer, and material objects and relationships plus a reusable
order-fulfillment core. Q2 and Q9 search/select/load those exact accepted IDs
instead of rediscovering joins, then compute their own requirement-specific
measures and filters in the answer/dashboard layer.

## Knowledge item shape

Use structured JSON/JSONL records such as:

```json
{
  "item_id": "ONT-...",
  "layer": "enterprise_ontology",
  "item_type": "object|field|relationship|rule|process|metric|quality|cleaning|limitation",
  "business_meaning": "...",
  "source_scope": ["..."],
  "effective_period": {"from": "...", "to": "..."},
  "evidence_refs": ["..."],
  "evidence_level": "authoritative|confirmed_source_local|working_proxy|exploratory_only",
  "limitations": ["..."],
  "conflicts": ["ONT-..."],
  "origin_item_id": "Q-...|R-...",
  "status": "active|superseded|conflicting"
}
```

Prepared Registry entries use the same provenance discipline and additionally
record `asset_kind`, `asset_hash`, `asset_location`, `schema`, `grain`,
`lineage`, `source_asset_ids`, `transformation_refs`, `reusable_preparation`
(boolean), and `requirement_scope` (nullable).

## Progressive promotion

After the item review, the program—not custom question code—validates and
applies one reviewed Knowledge Delta atomically. The result is one of:

- `promoted`;
- `promoted_with_limits`;
- `no_change`.

`no_change` is valid only when there is genuinely no reusable semantic
understanding or asset from the accepted item. It does not block the next item
and always includes a concrete reason such as “source scope is too narrow for
reusable preparation” or “review found no new supported relationship.” It is
not a default outcome for every answer. Preserve conflicts and supersession
links rather than replacing a prior meaning silently.

## Reuse decision

Before reusing an ontology item or prepared asset:

1. call `brief()` and search the compact accepted indexes;
2. match source scope and exact accepted IDs;
3. check effective period and freshness;
4. confirm hash, location, schema, grain, lineage, evidence, and
   transformations;
5. carry forward limits and conflicts;
6. decide whether the asset is source-scoped reusable preparation or must be a
   new requirement-scoped view;
7. select the exact IDs with a purpose, then load prepared rows only after
   registry hash validation;
8. record the compact selection reference/hashed counts in
   `work/semantic_selections.jsonl` and the active item's analytical trace;
   load the exact ID set on demand only when needed.

When exact identity overlap is absent but same-object representations are
materially plausible, record candidate representations, independent evidence
and coverage, a semantic identity decision, and reviewer confirmation before
declaring a combined relationship unavailable. The review decision is binary per
proposed mapping (accepted or not accepted), and an accepted mapping may
contain one or many source identities or representations. Only accepted
mappings enter canonical ontology; unresolved or ambiguous records remain
source-local with exceptions and coverage without downgrading proven mappings.
Analytical scripts consume these decisions through a materialized
`IdentityMappingView`; unique reviewed source identities resolve and ambiguous
ones remain unresolved. The automatically calculated completeness report is
advisory-only and cannot change review, commit, or analytical lifecycle state.
If the route is inapplicable,
record the reason explicitly.

## What does not belong in the LEM

Keep active item state, queue/portfolio status, reviewer identity,
execution-recovery and business-repair control, parser status, telemetry
events, clean-room incidents, candidate reports, freeze markers, and optimizer
recommendations in structured run/audit records, not as business knowledge.
Implementation versions remain audit metadata and never invalidate reusable
business knowledge.
