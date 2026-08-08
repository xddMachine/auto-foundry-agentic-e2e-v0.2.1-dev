# Question Analysis Playbook

## Purpose

This playbook helps the Lead Analyst choose a minimal, natural route for one business question. It is not a mandatory stage pipeline.

## 1. Interpret the question

Identify:

- decision or business use;
- requested measures;
- entities and dimensions;
- period or as-of date;
- expected comparison or ranking;
- causal language versus descriptive language;
- requested cross-source attribution;
- policy or contract dependence.

Resolve only what is material to the answer.

## 2. Choose an answer strategy

Use one or more:

- direct source-local measurement;
- source-local proxy;
- alternative-definition scenario analysis;
- descriptive association;
- policy scenario;
- partial answer;
- evidence blocker for unsupported components.

Prefer a useful answer with explicit limitations over a complete refusal.

## 3. Evidence selection

Start with the most likely relevant sources. Deeply inspect only those sources and expand when evidence indicates a need.

Do not open hundreds of files merely to prove completeness.

## 4. Semantics

For every material field, record:

- observed name;
- working business meaning;
- grain;
- evidence;
- evidence level;
- limitation.

When two meanings remain plausible, calculate separate scenarios when practical.

## 5. Relationships

For every material join or cross-source link, measure:

- overlap count and rate;
- left/right uniqueness;
- one-to-one, one-to-many, or many-to-many shape;
- fanout;
- unmatched records;
- duplicate keys;
- date or period alignment;
- transformations applied to keys.

Use a `strong_source_local` relationship only when empirical support is high enough for the specific question. State coverage and exclusions.

## 6. Documents and rules

For a document-dependent result, separate:

- what the document says;
- whether it is applicable;
- whether it is authoritative;
- whether required operational evidence exists;
- what can be calculated as a scenario;
- what cannot be claimed as compliance.

## 7. Processes

Define:

- case;
- event;
- initial and terminal event;
- timestamps;
- timezone;
- ordering;
- repeated events;
- incomplete cases;
- exclusions.

When process authority is incomplete, label the result source-local.

## 8. Quality

Investigate only material quality risks:

- missing measure or denominator fields;
- duplicate business keys;
- invalid dates;
- impossible sequence;
- unit or currency inconsistency;
- unstable joins;
- coverage gaps;
- stale periods;
- biased or incomplete populations.

## 9. Cleaning

Use the least invasive transformation:

1. normalization;
2. explicit mapping;
3. correction supported by evidence;
4. exclusion with count;
5. quarantine.

Never overwrite raw evidence.

## 10. Population

Record:

- base count;
- eligible count;
- exclusion counts by reason;
- unresolved count;
- denominator;
- grain;
- period;
- coverage.

## 11. Analysis

Suitable outputs include:

- counts and shares;
- trends;
- distributions;
- rankings;
- cohorts;
- concentrations;
- cycle times;
- process transitions;
- scenario ranges;
- correlations or descriptive associations;
- null findings.

Use causal language only with adequate design and evidence.

## 12. Answer structure

A strong final answer contains:

1. direct business answer;
2. headline findings;
3. supported breakdowns;
4. working definitions and proxies;
5. method and population;
6. limitations;
7. unsupported components;
8. next evidence needed.

## 13. Partial-answer examples

### Missing cross-system join

Return source-local findings from both sources and block only the combined attribution.

### Missing official metric definition

Choose a reasonable working definition, label it, and optionally show alternative scenarios.

### Incomplete policy authority

Run a policy scenario and block only the compliance conclusion.

### Missing causal evidence

Report associations and avoid causal claims.
