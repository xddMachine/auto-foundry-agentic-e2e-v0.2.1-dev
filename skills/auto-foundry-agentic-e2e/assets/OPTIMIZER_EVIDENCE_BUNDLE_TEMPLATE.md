# Optimizer evidence bundle

This development-only template describes the deterministic bundle produced by
`scripts/optimizer_evidence_collector.py`. The collector records run-local
facts only; it does not call a model, write a recommendation, alter products,
or promote code.

The fixed outputs are:

- `optimizer/optimizer_evidence_bundle.md`
- `optimizer/optimizer_evidence_appendix.md`

The appendix contains run-relative analytical-input SHA-256 values before and
after collection, exact duplicate groups, and the read-only inventory. A
separate fresh Optimization Agent may consume this bundle after the run is
frozen and produce a grounded free-form report. That reasoning step is not
implemented or invoked by this helper.
