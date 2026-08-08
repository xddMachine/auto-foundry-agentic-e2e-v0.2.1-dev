# Progressive Living Enterprise Model and reuse

## Two linked layers

Keep these run-local layers separate but link them with evidence references:

### Enterprise Ontology

An extensible, non-transaction representation of business objects, fields,
grains, relationships, metric meanings, applicable rules, process definitions,
conflicts, effective periods, and known limits. It captures reusable
understanding; it does not copy rows from a source.

### Prepared Data Registry

A registry of reusable derived assets: profiles, normalized values, mappings,
relationship measurements, prepared tables, and other bounded views. Each
entry records source references, exact asset IDs, scope, effective period,
transformations, evidence, limits, and whether it is reusable preparation or a
requirement-scoped view.

Both layers are empty at the start of a clean-room run. Neither is a central or
cross-run cache.

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
record `asset_kind`, `source_asset_ids`, `transformation_refs`,
`reusable_preparation` (boolean), and `requirement_scope` (nullable).

## Bounded selection and validation

The Navigator reads compact indexes, selects a bounded list of exact IDs, and
records the semantic rationale. The caller then validates every ID
deterministically: existence, current-run ownership, expected layer/type,
allowed scope, effective period, and evidence references. Missing, duplicated,
or out-of-scope IDs are a bundle failure; do not guess or broaden the selection.

## Progressive promotion

After the item review, apply one reviewed Knowledge Delta atomically by code.
The result is one of:

- `promoted`;
- `promoted_with_limits`;
- `no_change`.

`no_change` is valid and does not block the next item. There is no separate
ontology closing pass or extra review layer. Preserve conflicts and
supersession links rather than replacing a prior meaning silently.

## Reuse decision

Before reusing an ontology item or prepared asset:

1. match source scope and exact asset IDs;
2. check effective period and freshness;
3. confirm evidence and transformations;
4. carry forward limits and conflicts;
5. decide whether the asset is broadly reusable preparation or must be a new
   requirement-scoped view;
6. record the decision in the active item's trace.

The model has no fixed business-term dictionary. Meanings come from supplied
context, observed evidence, and explicit reasoning for the active item.

## What does not belong in the LEM

Keep active item state, queue/portfolio status, reviewer identity,
repair/recheck control, parser status, telemetry events, clean-room incidents,
candidate reports, freeze markers, and optimizer recommendations in their
structured run/audit records, not as business knowledge.
