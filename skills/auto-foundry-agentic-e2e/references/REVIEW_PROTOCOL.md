# Review Protocol

## Objective

Catch material analytical errors and overclaiming without recreating the analysis workflow as a control bureaucracy.

## Reviewer inputs

The Reviewer receives:

- original question;
- concise plan;
- relevant evidence references;
- material scripts and outputs;
- draft answer;
- assumptions and proxies;
- relationship measurements;
- population and denominator;
- limitations and unsupported parts.

## Required checks

1. Does the answer address the question?
2. Are headline numbers derived from actual data?
3. Are population and denominator clear?
4. Are material joins measured and disclosed?
5. Are working definitions and proxies explicit?
6. Are document-based claims appropriately scoped?
7. Are associations distinguished from causality?
8. Are supported and unsupported parts separated?
9. Could a useful partial answer be retained?
10. Are scripts or methods sufficient for the material calculations?

## Verdicts

### accept

No material defect.

### accept_with_limits

The answer is useful and accurate within explicit limitations.

### repair_once

One targeted material correction is needed. Findings must be concrete and bounded.

### block_specific_claims

Remove or downgrade specified unsupported claims. Preserve supported findings.

## One repair

After one repair, perform a short recheck of the repaired points. Do not restart the entire question unless the repair reveals that the original population or calculation was fundamentally wrong.

## Prohibited review behavior

Do not:

- demand perfect enterprise authority for a labelled source-local analysis;
- block a whole answer because one component is unsupported;
- review formatting instead of substance;
- parse free-form Markdown to establish lifecycle state;
- create a second independent verifier of the Reviewer;
- require repeated candidate generations;
- treat ontology `none` as failure;
- convert a tool or workflow defect into a data blocker.

## Technical defects

When a technical defect occurs:

- record `technical_failure`;
- preserve any valid supported analysis;
- continue the queue when possible;
- do not claim the business question is unanswerable merely because the workflow failed.
