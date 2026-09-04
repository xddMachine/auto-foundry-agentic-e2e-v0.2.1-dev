# Analytics toolkit workflow

This reference is the routed contract for the supported offline tabular
analytics path. The Analytical Owner chooses the method; deterministic program
code executes it and preserves the result. The toolkit does not fetch remote
data, infer causal effects, render a dashboard, or replace business review.
PyArrow is a base dependency for native Parquet support; `openpyxl` remains an
optional `io` extra for XLSX.

## Choose the exact supported method

Record the method, feature/metric definitions, population, grain, period,
parameters, and limitations in the owner analysis before running it. Prefer the
smallest route that answers the decision:

| Need | Toolkit call | Exact behavior and boundary |
| --- | --- | --- |
| Understand source shape, missingness, frequencies, and numeric summaries | `profile_data` | Produces a typed `data_profile` artifact. It is descriptive; frequency output is bounded and does not establish causality. |
| Compute explicit rates, counts, and numeric summaries | `compute_kpi_table` | Produces a typed `kpi_table` artifact using the bounded aggregation/filter vocabulary. Groupings must be explicit and arbitrary expressions are not evaluated. |
| Build customer segments | `segment_customers` | Fits deterministic k-means after numeric median imputation/standardization and categorical most-frequent imputation/one-hot encoding. Use an explicit `requested_k`, or candidate K values and the highest silhouette score with the lowest-K tie break. Set `random_seed` and `n_init` deliberately. Assignments are embedded for small populations; above 100,000 rows pass `assignment_output_ref` (or run under `AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT`) so the complete JSONL assignment table is emitted with a SHA-256, byte size, and row count. Without an explicit output authority, the complete assignments remain embedded. |
| Compare hierarchical structure | `segment_customers(..., compare_agglomerative=True)` | Adds a Ward-linkage agglomerative comparison at the selected K as validation evidence only. It is not assignable to new rows. |
| Assign new rows to an existing model | `score_segments` | Uses only the serialized assignable k-means centers and preprocessing state from `segment_customers`. It never treats agglomerative comparison output as an assigner. |

Use custom owner-authored Python/SQL or another local method only when the
toolkit does not support the required operation. SQLite tables are admitted
through a `TableRef`; table membership is checked against the read-only
database catalog and the selected identifier is quoted as one identifier (SQL
expressions are never accepted). Exact rows are materialized only when this
explicit analytical operation reads the named table; launch/catalog paths
remain bounded and metadata-only. Unknown, extensionless, notebook, and other
opaque members are safe to materialize explicitly; they are not analytically
parsed by the core. Run custom code through the same `ControlledScriptRunner`
boundary and document the method and validation; do not silently substitute a
toolkit method or claim native support for an unsupported method.

## Bind inputs and run deterministically

Use the exact source IDs selected through `AnalystWorkspace`, not reconstructed
filenames or paths. The source must be a local admitted CSV/TSV, Parquet, or
XLSX path, a `TableRef` naming one SQLite table, or a bound DataFrame; remote
URLs and cloud URIs are rejected.
Keep source lineage, selected columns, population, denominator, period, grain,
and units in the owner evidence.

Write a run-local script under the item work area. `ControlledScriptRunner`
performs syntax/dependency preflight without creating bytecode, then bounded
smoke/full execution. The child receives:

```text
AUTO_FOUNDRY_ANALYSIS_CONTEXT       # the immutable bound context manifest
AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT   # the current run's output directory
AUTO_FOUNDRY_ANALYSIS_PHASE         # smoke or full
```

Always resolve declared outputs from
`Path(os.environ["AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT"])`. Do not hard-code a
run path. A normal run uses the item work directory as both cwd and output
root; deterministic reruns use disposable runner-owned cwd/output-root
directories. Declare every output explicitly; pass only a stable content
projection in `deterministic_outputs` to `AnalystWorkspace.run_analysis()` when
rerun equality is required:

For a segmentation population above 100,000 rows, pass an
`assignment_output_ref` relative to that output root. The toolkit writes the
complete `population_id`/`segment` JSONL atomically and records an output
descriptor containing its relative `path`, `sha256`, `size_bytes`, exact
`row_count`, and `complete: true` in both the model payload and
`artifact.output_refs`. Parent traversal and symlink escapes are rejected. If
no explicit output root or output reference is supplied, the complete
assignment list stays embedded in the artifact; the toolkit never silently
drops rows or writes into the source directory/current directory. Segmentation
floating-point values are rounded to 12 decimal places only at artifact wire
serialization so deterministic reruns do not depend on last-bit BLAS drift.
For populations above 2,048 rows, silhouette validation uses a deterministic
2,048-row sample because the full pairwise calculation is quadratic; segment
sizes and assignments still cover every source row.

```python
from pathlib import Path
import os
import json

from auto_foundry_core.analytics_toolkit import compute_kpi_table
from auto_foundry_core.analytical_artifacts import AnalyticalArtifact

output_root = Path(os.environ["AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT"])
artifact = compute_kpi_table(
    selected_source,
    [{"name": "revenue_sum", "aggregation": "sum", "value_column": "revenue", "group_by": ["segment"]}],
    requirement_id="REQ-001",
)
artifact_path = output_root / "kpi_artifact.json"
artifact_path.write_text(artifact.to_json(), encoding="utf-8")
# ``created_at`` is observational envelope metadata.  Keep the complete
# artifact as a normal output and expose only a stable content projection for
# deterministic rerun comparison.
projection = {
    "artifact_id": artifact.artifact_id,
    "artifact_type": artifact.artifact_type,
    "schema_version": artifact.schema_version,
    "requirement_id": artifact.requirement_id,
    "content_hash": artifact.content_hash,
}
(output_root / "kpi_artifact_projection.json").write_text(
    json.dumps(projection, sort_keys=True, separators=(",", ":"), allow_nan=False),
    encoding="utf-8",
)
```

The host call is conceptually:

```python
report = analyst.run_analysis(
    script_path,
    outputs=("kpi_artifact.json", "kpi_artifact_projection.json"),
    deterministic_outputs=("kpi_artifact_projection.json",),
)
```

The runner compares the stable projection hash across isolated full executions
and materializes both declared outputs only after the comparison succeeds. It
records receipts, script/context/source hashes, phase, stdout/stderr bounds,
and output hashes; timeouts, undeclared writes, bytecode, output limits, and
context changes fail the same owner attempt and roll back the output set. Keep
deterministic output free of wall-clock values and unordered data. The full
artifact's observational `created_at` remains part of its strict envelope and
is validated on read-back; it is deliberately not treated as a byte-identical
deterministic output.

## Strict artifact JSON and evidence

Write the toolkit object with `AnalyticalArtifact.to_json()` (or a strict
equivalent produced from its complete wire mapping), never an ad-hoc result
dictionary. The JSON must retain the complete schema-versioned envelope:

- `artifact_type`/`type`, `artifact_id`, `schema_version`, and matching
  `requirement_id`;
- `dataset_fingerprint`, `source_fingerprints`, and exact `source_refs`;
- population, grain, period, feature/metric definitions, method, parameters,
  random seed, validation evidence, tables, findings, visualization intents,
  and limitations;
- typed `payload`, finite JSON values, `content_hash`, `envelope_hash`, and
  `created_at`.

`AnalyticalArtifact.from_json()` is the strict read-back check. It rejects
unknown/missing fields, non-finite numbers, invalid hashes, duplicate or unsafe
paths, and an artifact whose content or envelope hash no longer matches. Keep
the artifact JSON in the declared output set and preserve its hash in the
owner's evidence. Link every material claim/KPI/segment finding to the exact
selected source IDs, script receipt/output reference, method, population,
period, and validation evidence through `AnalystWorkspace.record_evidence()`;
the owner supplies business meaning while the program supplies durable paths
and hashes.

## Review, repair, and downstream integration

Submit one complete answer with the artifact-backed findings, definitions,
limitations, and safe actions. A fresh Independent Business Reviewer checks
the decision, claims, calculations, source completeness, population,
denominator, period, joins, and causal language. A material repair returns to
the same Analytical Owner and receives a targeted recheck; the reviewer never
rewrites the artifact or owns lifecycle state.

Only after business acceptance, use exactly one Result Integration Agent. It
loads the accepted item and lets `IntegrationSession.create/load` automatically
stage every exact sealed typed artifact listed in the accepted bundle. Do not
manually re-submit or re-declare an accepted artifact. The integration payload
must preserve the accepted artifact's `artifact_id`, `artifact_type`,
`schema_version`, `content_hash`, envelope, and evidence linkage;
`requirement_id` must match the item. The agent
does not write integration JSON directly, invent a new method, redo the
analysis, or turn a validation-only agglomerative comparison into an assigner.
The existing mechanical validation and independent Integration Fidelity
Reviewer run before `session.commit()`. Integration remains downstream: a
technical integration failure does not erase an accepted business answer.
