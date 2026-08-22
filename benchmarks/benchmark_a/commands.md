# Benchmark A commands (PREPARE / LAUNCH LATER only)

These commands document a later run. They were **not executed** while
preparing v0.7.0/core v0.8.0, and no Benchmark A run root was created here.

## PREPARE (later)

1. Copy `run_config.example.json` to a work area and replace only the run-root
   placeholder with a newly created, empty directory.
2. Verify the source ZIP SHA-256 is
   `82e9c913bf437ac9e361d6890467a9aed9b1c6db9d887cfcf0cd659035a71ec2`.
3. Verify the frozen question-order SHA-256 is
   `3a40d2f7083f0d2f0e1b216d405a0ce6c38cd4913e157b9e48a99dfa96958236` using
   the canonical payload definition in `comparison_schema.json`.
4. Verify the question file and exact markers before analysis. Do not extract
   or inspect source data as part of this preparation package.

Illustrative preparation checks (do not run as a benchmark launch):

```text
PREPARE ONLY: validate config, source path/hash, empty run root, and question order.
PREPARE ONLY: do not call a model, execute a question, or create products.
```

## LAUNCH LATER (not executed)

At the later launch boundary, use the exact prompt below. The user's request
to run Benchmark A is sufficient; after printing the required markers, begin
the run immediately.

```text
LAUNCH LATER — Benchmark A only.

Use a NEW EMPTY RUN ROOT: <absolute path supplied after preparation>. Do not
read, reuse, copy, or import any prior run root, cache, script, prompt, answer,
LEM, prepared registry, dashboard, or telemetry. Use exactly these markers in
structured state and the final report:
skill_name: auto-foundry-agentic-e2e
skill_version: 0.7.1
core_name: auto_foundry_core
core_version: 0.8.0

Use the same immutable source ZIP and SHA-256 as the baseline:
82e9c913bf437ac9e361d6890467a9aed9b1c6db9d887cfcf0cd659035a71ec2
Use the ten questions from benchmarks/benchmark_a/questions.md in exactly the
same order, without discovery or rewriting. The expected question-order
SHA-256 is:
3a40d2f7083f0d2f0e1b216d405a0ce6c38cd4913e157b9e48a99dfa96958236
Keep the source read-only and do
not make external/model calls beyond the approved product runtime boundary.

If prepared data is reused, preserve any `effective_period` through its
descriptor/sidecar, operation hash, accepted integration, registry, and later
selection/load. Omission remains valid with no period constraint. Diagnostic
run1/run2 artifacts were invalidated before counted runs.

Before analysis begins, print the resolved empty run root, source path/hash,
question-order hash, zero-prior-run-reuse marker, and the four markers. Then
begin analysis immediately; no additional user step is required.
```

There is no time deadline. Benchmark A remains unexecuted until that later
launch instruction and acceptance decision.
