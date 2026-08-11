# Progressive Living Enterprise Model and reuse

The program owns one run-local data room, one source catalog, and two linked
LEM layers. This reference defines the knowledge boundary; it does not create
a Portfolio Planner, Navigator, separate navigation role, or lifecycle gate.

## Data room and source catalog

The data room builds one physical, searchable source catalog from ZIP/archive
and member metadata before item analysis. Catalog entries are bounded and may
include source/member IDs, archive/member locations, formats, byte counts,
hashes, bounded columns/types, bounded samples or values, and workbook sheet
metadata. The raw archive remains read-only; the catalog is not a transaction
copy. Source/member reads are observed in passive telemetry.

Physical binding is run-level: the initial full archive/member inventory is
counted once, bound child contexts reuse it, and selected-member verification
is counted separately. An explicit final `verify_source_full()` detects a late
mutation. Opaque members have no semantic parser; copy them only through the
safe explicit materialization operation.

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

Both layers start empty at the start of a clean-room run. Compact source,
ontology, and prepared indexes may be searched, but the Lead Analyst selects
relevant IDs directly. There is no Navigator, descriptor/typed-validation role,
or per-item Capability Catalog compliance artifact.

## Durable item workspace and bounded selection

The program creates `questions/<id>/work` or `requirements/<id>/work`,
authoritative `item_state.json`, and immutable `BoundAnalysisContext` before
invoking the Lead Analyst. The Lead Analyst writes a plan, script, and source
map first, then appends findings and prepared asset references. The program
validates every selected ID deterministically:
existence, current-run ownership, expected layer/type, allowed scope,
effective period, hash/location, schema, grain, lineage, and evidence
references. Missing, duplicated, or out-of-scope IDs are a bundle failure; do
not guess or broaden the selection.

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
  mandatory JSON, or reviewer chain.

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

`no_change` is valid, does not block the next item, and always includes a
concrete reason such as “source scope is too narrow for reusable preparation”
or “review found no new supported relationship.” Preserve conflicts and
supersession links rather than replacing a prior meaning silently.

## Reuse decision

Before reusing an ontology item or prepared asset:

1. match source scope and exact asset IDs;
2. check effective period and freshness;
3. confirm hash, location, schema, grain, lineage, evidence, and
   transformations;
4. carry forward limits and conflicts;
5. decide whether the asset is source-scoped reusable preparation or must be a
   new requirement-scoped view;
6. record the decision in the active item's trace.

When exact identity overlap is absent but same-object representations are
materially plausible, record candidate representations, independent evidence
and coverage, a semantic identity decision, and reviewer confirmation before
declaring a combined relationship unavailable. If the route is inapplicable,
record the reason explicitly.

## What does not belong in the LEM

Keep active item state, queue/portfolio status, reviewer identity,
execution-recovery and business-repair control, parser status, telemetry
events, clean-room incidents, candidate reports, freeze markers, and optimizer
recommendations in structured run/audit records, not as business knowledge.
