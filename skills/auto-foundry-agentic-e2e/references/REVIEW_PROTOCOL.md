# Review protocol

## Objective

Use review to improve and protect the business analysis. Do not turn the
reviewer into a second analyst, a schema author, or a lifecycle operator.

## Inputs

Give one fresh Independent Business Reviewer only the active item's:

- exact question or requirement;
- complete submitted answer;
- selected sources and source-completeness evidence;
- calculation method, scripts, outputs, and reconciliations;
- population, denominator, period, units, grain, and exclusions;
- relationship and identity tests;
- specialist memos;
- assumptions, proxies, unsupported components, and limitations.

Do not expose siblings, cumulative LEM state, prior-run memory, broad reports,
internal state files, hashes, or review packet machinery.

## Required checks

Judge whether:

1. the answer addresses the user's decision;
2. every material number and claim is supported;
3. definitions, population, denominator, period, units, and grain are clear;
4. joins, identities, exclusions, conflicts, and alternate controls were tested;
5. policies and documents are authoritative and applicable;
6. proxy, scenario, association, and causal language are honest;
7. material source-completeness gaps were searched;
8. useful supported components remain visible when others are unsupported;
9. limitations and next actions match the evidence;
10. suggested visuals do not overstate the result.

For an identity domain, review the Entity Resolution Owner's method, evidence,
sample tests, coverage, exceptions, unresolved/ambiguous populations, and
source-account representation classes. The review decision is binary per
proposed mapping (accepted or not accepted); one accepted `CanonicalMapping`
may contain one or many source identities or representations, including bulk
pattern-derived populations. Coverage and exceptions describe the job and do
not turn unresolved records into canonical mappings or downgrade accepted ones.
Review full-population results for every source used by the domain, plus the
documented expansion decision; do not accept a sample-only mapping as complete.

## Reviewer output

Return one verdict:

- `accept`;
- `accept_with_limits`;
- `repair_once`;
- `confirm_data_insufficiency`.

Return all material findings together. For each finding provide only:

```text
finding ID
target answer section
semantic categories (one or more, canonical order): answer | calculation | evidence | method | source_completeness | presentation
problem
evidence
required change
```

The reviewer does not provide JSON pointers, filesystem paths, dependent
artifact paths, draft hashes, packet hashes, or state changes.
`BusinessReviewAdapter` validates the section/category set and creates the exact
program scope. Unknown sections and categories fail before review state is
written.
The host/router records the current `owner_ref` in program-owned audit state;
the reviewer never emits it. A replacement owner may continue any nonterminal
item, while the reviewer remains independent.

Calibrate the verdict: `accept` means the core requested decision is answered
and only normal disclosed limits remain. Use `accept_with_limits` only when a
material requested component is missing or unreliable. Ordinary source-local,
currency, or no-causality caveats alone do not force with-limits. Semantic
fidelity is an independent review dimension; resolution review decisions remain
binary per mapping while each accepted mapping may contain one or many source
identities and coverage/exceptions remain job-level evidence.

Before independent resolution review, `submit_result()` runs the full typed
projection validator used by commit. A malformed candidate stays with the same
Entity Resolution Owner and lease for technical correction; this does not
consume business `repair_once`. Mapping completeness is an advisory signal and
its absence or failure never blocks review or commit.

## Repair and targeted recheck

For `repair_once`, return all material findings in the initial review. The
program opens one item-local repair. The current or a replacement Analytical
Owner may update any item-local work and coherent answer sections needed to
resolve it. Finding categories remain reviewer provenance, not filesystem
capabilities. There is no fixed repair budget; code feedback and execution
recovery are ordinary work.

Run one targeted recheck after each repair. Verify each requested change and
confirm unaffected content remains preserved. The recheck may accept or return
another `repair_once` when a material finding remains. Stop when the answer is
supported or terminalize honestly; do not use an arbitrary cycle count as the
decision rule.

Only the Analytical Owner may originate a `DataInsufficiencyConclusion` with
an unanswerable component, missing information, searches/tests performed,
evidence references, and supported components. The reviewer merely confirms
that conclusion with `confirm_data_insufficiency`; the program then may publish
`blocked_by_evidence`. Presentation, calculation, evidence, method, reviewer,
program, and script defects require repair or technical failure and cannot be
used to block a business component.

If the reviewer is unavailable, preserve a useful bounded answer with explicit
`review_status: unavailable` and reduced confidence rather than inventing a
review.

## Scope incidents

If a reviewer read sibling/prior/broad context or its packet is otherwise
inadmissible, discard only that review through the program-owned
`ItemWorkspace.discard_business_review(...)` route with an item-bound
`reviewer_scope` incident. Preserve the analytical work, reset the unused
business repair, and route one new full review. Never reinterpret or partially
reuse the contaminated verdict.

Discard journals, audit heads, locks, and packet hashes are program-owned
details. Do not include them in a reviewer prompt. Implementation versions do
not determine whether analytical work may resume.

## Integration fidelity review

Business review ends before acceptance. After acceptance, one Result
Integration Agent creates typed records. Mechanical validation checks their
shape, refs, hashes, and commit rules. One fresh item-only Integration Fidelity
Reviewer then checks whether the records faithfully represent the accepted
answer.

The fidelity reviewer may identify affected record IDs and dependency IDs
because those are the objects under review. The same Integration Agent may
correct the authorized records once (or remove an authorized record) and
receive one targeted recheck. The accepted business answer bytes and
`accepted_content_hash` remain immutable; typed integration records are only a
derived projection. Normalized typed fields may differ literally from the
accepted prose/artifact without constituting a semantic conflict. The agent
preserves the accepted business meaning, rebuilds the packet, and rechecks
before commit; it may not reopen or rewrite the accepted business answer.

The Analytical Owner must establish actual joins/relationships with
`source_id`/`target_id`, `join_keys`, grain, cardinality, `matched_pairs` (the
unique tested edge-pair count), `source_population`/`target_population`,
`matched_source_count`/`matched_target_count` (distinct matched endpoints),
and `source_coverage`/`target_coverage` (endpoint count divided by its
population, with zero for a zero population), plus `as_of`/date authority,
limitations, and evidence. Integration Fidelity review accepts only
those tested relationships plus reviewed canonical identity mappings; it does
not complete a theoretical graph or infer relationships from prose.

## Prohibitions

- Do not repeat the entire analysis without a concrete issue.
- Do not rewrite the answer during review.
- Do not require the Business Reviewer to learn program schemas.
- Do not treat formatting preferences as material findings.
- Do not use review to hide supported partial results.
- Do not allow review or integration defects to become data conclusions.
- Do not require row-by-row identity review, an authoritative crosswalk, or a
  fixed matching script; judge the owner's methodology and coverage evidence.
- Do not treat an Entity Resolution Owner as an extra requirement specialist;
  it is a parallel mapping job and never owns the requirement answer.
- Do not add a reviewer-of-reviewer or a second integration reviewer.
