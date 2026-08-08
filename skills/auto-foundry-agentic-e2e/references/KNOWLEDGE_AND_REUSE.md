# Knowledge and Reuse

## Purpose

The Living Ontology stores reusable business understanding so later questions do not repeat completed semantic work.

## What belongs

- business objects;
- source-local grains;
- field meanings;
- measured relationships;
- policy or rule applicability;
- process definitions;
- metric definitions;
- reusable cleaning mappings;
- known limitations or conflicts.

## What does not belong

- active question state;
- repair IDs;
- reviewer IDs;
- parser status;
- candidate versions;
- freeze hashes;
- workflow control metadata;
- temporary debugging notes.

These belong in the audit trail.

## Knowledge item

Each item should contain:

```json
{
  "item_id": "KNOW-...",
  "item_type": "object|field|relationship|rule|process|metric|quality|cleaning|limitation",
  "business_meaning": "...",
  "source_scope": ["..."],
  "evidence_refs": ["..."],
  "evidence_level": "authoritative|confirmed_source_local|working_proxy",
  "limitations": ["..."],
  "origin_question": "Q-...",
  "status": "active|superseded|conflicting"
}
```

## Promotion

Promotion occurs after the question answer passes review.

No separate ontology-finalization agent or verifier is required.

Promotion outcomes:

- `promoted`;
- `promoted_with_limits`;
- `none`.

`none` never blocks the next question.

## Reuse

Before each new question:

1. identify relevant items;
2. confirm source, period, scope, and assumptions still apply;
3. reuse them directly when current;
4. validate only material uncertainty;
5. create a new item when the previous meaning does not apply;
6. preserve conflicts rather than overwriting.

## Fresh runs

In clean-room mode, start with an empty ontology and do not read a previous run.
