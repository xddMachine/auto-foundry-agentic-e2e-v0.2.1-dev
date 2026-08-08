# Artifact and Efficiency Policy

## Principle

Preserve the work that materially supports the answer. Do not manufacture paperwork.

## Always preserve

- original supplied question;
- concise plan;
- evidence references;
- assumptions and proxies;
- population and denominator;
- draft answer;
- independent review;
- final answer;
- reusable knowledge update;
- outcome.

## Preserve when created or used

- Python;
- SQL;
- shell scripts;
- notebooks;
- spreadsheet formulas;
- transformed workbooks;
- mapping files;
- generated extracts;
- chart specifications;
- dashboard code;
- build commands;
- material intermediate outputs.

## Analysis trace

Keep a concise `analysis_trace.md` for each question:

```text
Evidence inspected
Tools used
Specialists called
Scripts created
Key decisions
Assumptions/proxies
Outputs
Unresolved issues
Approximate effort
```

This trace exists to study the natural agent workflow and identify automation opportunities.

## Do not create

- empty stage folders;
- per-stage candidate/review/freeze trees;
- verifier scripts for prose;
- repeated copies of unchanged artifacts;
- scripts only to satisfy a formal requirement;
- separate artifacts for every `not_required` capability;
- broad data scans with no question relevance.

## Efficiency

- inventory globally once;
- profile deeply per question;
- reuse prior profiles and extracts;
- batch related reads;
- cache material derived outputs inside the current run;
- use scripts for repeated mechanical work;
- stop when sufficient evidence exists;
- keep one repair cycle;
- keep effort proportional to data size and business complexity.

## Reproducibility

For complex or material calculations, prefer a saved script.

For simple calculations, record:

- input;
- formula;
- output;
- exclusion logic.

## Security

Never preserve secrets, tokens, passwords, private keys, or unnecessary personal data in reports.
