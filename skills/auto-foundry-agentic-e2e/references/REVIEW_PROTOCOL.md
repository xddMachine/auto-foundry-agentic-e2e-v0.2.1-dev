# Review protocol

## Objective

Catch material analytical errors and overclaiming at the business-result
boundary without turning natural analysis into a chain of gates.

## Reviewer routing

Use one reviewer per active item. Prefer an independent reviewer in fresh
context. If that route is unavailable, try an alternate independent route;
then try a fresh context from the same family. Do not hardcode model/provider
names. Release sessions when the host supports it.

If no route is available, continue and disclose exactly:

```json
{"review_status":"unavailable","review_strength":"none"}
```

Do not add a second review of the review.

## Reviewer inputs

The reviewer receives the user-owned question/requirement record, Navigator
exact IDs and validation evidence, concise plan, selected catalog capabilities,
material scripts and outputs, draft answer, self-check, assumptions/proxies,
relationship measurements, population/denominator, limitations, telemetry
references, and intended Knowledge Delta.

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
10. Are methods, scripts, and catalog/core operations reproducible?
11. Does every intended Knowledge Delta item have evidence and limits?

## Verdicts

- `accept`: no material defect;
- `accept_with_limits`: useful and accurate within explicit limits;
- `repair_once`: one concrete, bounded material correction is needed;
- `block_specific_claims`: remove or downgrade named unsupported claims while
  preserving supported findings.

After `repair_once`, perform one short fresh recheck of the repaired points.
Do not restart the item or create another repair. If the issue remains, record
the supported outcome and disclose the unresolved component.

## What a reviewer must not do

Do not reject a valid source-local answer merely because there is no unique
enterprise definition, a labelled proxy was used, a partial answer is
possible, or the Knowledge Delta is `no_change`. Do not demand extra artifacts,
formatting, helper use, or hidden lifecycle proof.

Do not parse free-form prose, headings, bullets, or wording to determine
state. Mechanical checks may verify structured fields, file existence, script
results, exact IDs, raw-source immutability, and internal-link integrity;
business judgment remains with the Lead Analyst and reviewer.

## Technical defects

Record workflow/tool/parser defects as `technical_failure`, preserve valid
supported analysis, disclose review availability, and continue the queue when
possible. A technical defect is not a conclusion about the data.
