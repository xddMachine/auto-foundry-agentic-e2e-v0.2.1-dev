# Artifact and efficiency policy

## Principle

Preserve work that materially supports a reviewed answer. Do not manufacture
paperwork, empty folders, or per-capability artifacts.

## Always preserve

- original question or Requirement Mode record;
- structured run identity, mode, scope classification, and outcome;
- Navigator bundle IDs and deterministic validation result;
- concise plan and analysis trace;
- evidence references, definitions, assumptions, limits, population, and
  denominator;
- draft answer, Lead Analyst self-check, reviewer verdict, and any one repair;
- final answer and reviewed Knowledge Delta result (`promoted`,
  `promoted_with_limits`, or `no_change`);
- passive telemetry event references;
- dashboard/product traceability and internal-link checks.

## Preserve when created or used

Keep Python, SQL, shell, notebook, spreadsheet formula, mapping, transformed
asset, chart specification, dashboard source, command, and material output
when it affects a result or improves reproducibility. Record purpose, material
inputs and outputs, assumptions, limits, and a reproduction command. Never
overwrite raw evidence.

## Natural analysis trace

Use one concise trace per active item:

```text
Evidence and exact IDs inspected
Catalog capabilities inspected and selected (or gap)
Tools and specialists used
Scripts or transformations created
Key decisions and working definitions
Population, denominator, and relationship measurements
Outputs and evidence references
Self-check and review result
Unresolved issues and limits
Approximate effort (optional)
```

The trace is observational evidence for the post-run optimizer. It does not
control lifecycle state.

## Do not create

- empty directories or files;
- a folder for every capability that was not needed;
- capability-by-capability approval trees or finalizer artifacts;
- verifier scripts that inspect prose wording to decide state;
- repeated copies of unchanged artifacts;
- scripts created only to satisfy this policy;
- broad scans unrelated to the active item;
- central ontologies or cross-run caches.

## Efficiency and reuse

Inventory sources once, then profile deeply only for the active item. Use
compact ontology/prepared indexes and exact IDs to bound reads. Reuse prepared
assets when source scope, effective period, evidence, and limits still apply;
create requirement-scoped views when they do not. Replan the Requirement Mode
portfolio briefly between items, not as a second workflow.

## Reproducibility

For a material or repeated calculation, prefer preserved code. For a simple
calculation, record input references, formula, output, exclusions, and units.
The Capability Catalog is inspected first, but custom code is valid when it is
the clearest fit and remains reproducible.

## Telemetry and privacy

Telemetry is append-only and passive. Store event metadata and artifact IDs,
not raw business rows, secrets, tokens, or unnecessary personal data. Do not
use telemetry to invent timing benchmarks or to alter an answer.

## Optimizer boundary

The optimizer may read frozen traces, telemetry, and product manifests only
after the run products are complete. Its report and evidence appendix are
new read-only artifacts; it cannot mutate code, state, LEM, prepared data,
products, source files, or configuration.
