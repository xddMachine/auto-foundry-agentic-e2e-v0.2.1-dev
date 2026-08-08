# Benchmark A — v0.2.1/core v0.1 preparation package

Benchmark A is prepared for a later, controlled comparison of the v0.2.1
skill and `auto_foundry_core` v0.1.0 against the recorded v0.2.0 baseline. It
is not an analytical result and it was not executed in this deliverable.

This directory intentionally contains only the preparation contract:

- [questions](questions.md) — the ten supplied questions, in baseline order;
- [run configuration](run_config.example.json) — a copyable configuration
  shape with no source data or prompts;
- [baseline evidence](baseline_v0.2.0.json) — observed baseline facts and
  explicit unknowns;
- [comparison schema](comparison_schema.json) — result fields and statuses;
- [evaluation checklist](evaluation_checklist.md) — reviewer checks;
- [commands](commands.md) — PREPARE and LAUNCH LATER instructions only.

The later launch must use a new empty run root, the immutable source ZIP and
hash recorded in the baseline, the exact question order, and zero prior-run
reuse. Obtain an explicit confirmation immediately before analysis begins.
There is no time deadline in this package.
