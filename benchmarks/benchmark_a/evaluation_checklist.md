# Benchmark A evaluation checklist

The checklist is for the later launch/review only. It was not run while
preparing this package.

- [ ] Confirm a new, empty run root exists and no sibling run/cache/artifact is
      readable or reused.
- [ ] Confirm the exact skill/core markers are present in structured state and
      final report: skill `0.2.1`, core `0.1.0`.
- [ ] Confirm the source ZIP path and SHA-256 exactly match the baseline and
      the source remains read-only.
- [ ] Confirm the canonical question-order SHA-256 is
      `3a40d2f7083f0d2f0e1b216d405a0ce6c38cd4913e157b9e48a99dfa96958236`.
- [ ] Confirm all ten questions are byte-for-byte equivalent to `questions.md`
      and remain in Q-001 through Q-010 order.
- [ ] Record per-question outcome, review status/strength, and at most one
      bounded repair; keep blocked claims visible rather than converting them
      to zeros.
- [ ] Record custom script count/LOC only when empirically countable and
      record ontology item count from the run-local reviewed artifact.
- [ ] Record observed wall time only if the harness measured it; otherwise
      record the required explicit unknown shape.
- [ ] Verify dashboard/audit links, traceability, limitations, and local-only
      assets without re-running source analysis.
- [ ] Capture answer quality, product-runtime model/tool workload, core/cache
      use, prepared-data reuse, dashboard quality, and source immutability
      fields required by `comparison_schema.json`.
- [ ] Reproduce or explicitly classify the Q-004 full-horizon/34-unit issue;
      do not publish the blocked claims as accepted results.
- [ ] Confirm no model, external, benchmark, V3, xddMachine, autofoundary, or
      publication operation occurred before acceptance.
