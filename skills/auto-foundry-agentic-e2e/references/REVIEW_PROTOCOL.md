# Review protocol

## Objective

Catch material analytical errors and overclaiming at the business-result
boundary without turning natural analysis into a chain of gates. The reviewer
receives a materialized draft from the durable item workspace; scratch remains
mutable until the program writes the accepted snapshot.

## Reviewer routing

Use one Independent Business Reviewer per active item. Prefer an independent
business reviewer in a fresh context. If that route is unavailable, try an
alternate independent route;
then try a fresh context from the same family. Do not hardcode model/provider
names. Release sessions when the host supports it.

If no route is available, continue and disclose exactly:

```json
{"review_status":"unavailable","review_strength":"none","verdict":"not_reviewed"}
```

The item may still finish as `answered_with_limits` from the Lead Analyst
result, with the unavailable-review limitation disclosed. A route that was not
invoked cannot return `accept_with_limits` or any other reviewer verdict. Do
not add a second review of the review.

## Reviewer inputs

The reviewer receives the user-owned question/requirement record, data-room
source catalog references and bounded completeness-search target, Lead Analyst
exact source/LEM/prepared IDs and validation evidence, plan/source map,
material scripts and outputs, draft answer, self-check, assumptions/proxies,
relationship measurements, population/denominator, limitations, telemetry
references, intended Knowledge Delta, and any identity-escalation candidates.

## Required checks

1. Does the answer address the original request and expected outputs?
2. Are headline values derived from supplied or prepared evidence?
3. Are scope, period, units, population, and denominator clear?
4. Are material joins measured and their coverage/fanout disclosed?
5. Are working definitions, proxies, conflicts, and effective periods explicit?
6. Are document, rule, and process claims scoped to applicable evidence?
7. Are associations distinguished from causality?
8. Are supported and unsupported components separated?
9. Could a useful partial answer be retained?
10. Are methods, scripts, and core operations reproducible?
11. Does every intended Knowledge Delta item have evidence and limits?
12. For each material absence claim, did a targeted search of the physical
    source catalog check relevant archive/member metadata and bounded fields?
13. If exact overlap is absent but same-object representations are plausible,
    were candidates, evidence/coverage, semantic identity decision, and this
    review check recorded—or was route inapplicability justified?

The completeness and identity checks are targeted. Do not repeat the full
analysis (without repeating the full analysis in another form), introduce a mandatory catalog-compliance artifact, or add another
review layer.

## Verdicts

- `accept`: no material defect;
- `accept_with_limits`: useful and accurate within explicit limits;
- `repair_once`: one concrete, bounded material correction is needed;
- `block_specific_claims`: remove or downgrade named unsupported claims while
  preserving supported findings.

After `repair_once`, perform one short fresh recheck of the repaired points.
Do not restart the item or create another business repair. Execution recovery
before review is a separate program decision and does not consume this repair;
it requires a canonical persisted invocation `receipt_ref`/hash proving
lane/provider/host/process loss and matching the active attempt/lane.
Unpersisted or mismatched references fail closed. Filesystem no-progress alone yields `await_runtime` or
`materialization_guidance`. Provider/model identity may be literal
`unavailable`.
If the issue remains, record the supported outcome and disclose the unresolved
component. The program then validates and atomically applies the reviewed
Knowledge Delta; custom question code does not apply it.

## What a reviewer must not do

Do not reject a valid source-local answer merely because there is no unique
enterprise definition, a labelled proxy was used, a partial answer is
possible, or the Knowledge Delta is `no_change`. Do not demand extra artifacts,
formatting, helper use, wall-time targets, or hidden lifecycle proof.

Do not parse free-form prose, headings, bullets, or wording to determine state.
Mechanical checks may verify structured fields, file existence, script
results, exact IDs, raw-source immutability, artifact progress, and internal-
link integrity; business judgment remains with the Lead Analyst and reviewer.
There is no reviewer-of-reviewer, business-repair finalizer, manual terminalizer,
or second integration reviewer. Result Integration mechanical validation cannot
prove semantic completeness. Exactly one fresh item-only Integration Fidelity
Reviewer checks the current item after mechanical validation and before commit;
the same Result Integration Agent may make one targeted repair and receives one
targeted recheck. The packet excludes siblings, cumulative state, prior memory,
and broad workspace context; there is no prose parser or semantic compiler.

## Technical defects

Record workflow/tool/parser defects as `technical_failure` only after the
program's allowed execution-recovery routes are exhausted. Preserve valid
supported analysis, disclose review availability, preserve scratch, and
continue the queue when possible. A technical defect is not a conclusion
about the data, and no terminalizer agent is involved. Classifier output is
restricted to `same_attempt_feedback`, `business_repair`,
`execution_recovery`, `abort_and_new_clean_run`, or `null`; raw
`terminal_reason` values remain specific facts such as `syntax_error` or
`core_defect`.
