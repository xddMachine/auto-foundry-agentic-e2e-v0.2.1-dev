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

The frozen question-order SHA-256 is
`3a40d2f7083f0d2f0e1b216d405a0ce6c38cd4913e157b9e48a99dfa96958236`; its
canonicalization rule is recorded in `comparison_schema.json` and repeated in
the later-launch prompt, config, and checklist.

The later launch must use a new empty run root, the immutable source ZIP and
hash recorded in the baseline, the exact question order, and zero prior-run
reuse. The user's instruction to run Benchmark A is sufficient; print the
resolved run root and version/source/question markers for the record, then
begin immediately. There is no time deadline in this package.
