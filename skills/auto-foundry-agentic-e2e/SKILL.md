---
name: auto-foundry-agentic-e2e
description: Runs a natural, agent-native, end-to-end enterprise data analysis workflow from raw data and supplied business questions to reviewed answers, reusable business knowledge, management dashboards, an audit view, and a report of work that can later be automated. Use for SAP, databases, Excel, CSV, documents, APIs, and mixed enterprise data rooms. The skill processes one question at a time, uses adaptive analysis rather than mandatory stage gates, preserves material scripts when agents create them, permits clearly labelled source-local assumptions and proxies, returns partial answers when only part of a question is supported, and builds final dashboards after the complete supplied queue is processed.
metadata:
  author: auto-foundry
  version: "0.2.0"
  architecture: lead-analyst-question-sequential
  release: natural-analysis-first
---

# Auto Foundry Agentic E2E — Natural Analysis First

## 0. Version marker

At the start of every new run, record:

```text
skill_name: auto-foundry-agentic-e2e
skill_version: 0.2.0
```

Write these values into the run state and include them in the final run report. This prevents an old installed skill from being mistaken for this version.

## 1. Mission

Take a mixed enterprise data room and supplied business questions from raw evidence to useful, reviewed business answers.

The intended workflow is:

```text
SAP / databases / Excel / CSV / documents / APIs / business context
                                ↓
                    lightweight Evidence Foundation
                                ↓
                    supplied Question Registry
                                ↓
                    one active question at a time
                                ↓
              Lead Analyst chooses the minimum useful route
                                ↓
        semantics / relationships / documents / processes / quality
        cleaning / analytical population / calculation as needed
                                ↓
               strongest supported answer, with limitations
                                ↓
                    one independent answer review
                                ↓
               lightweight reusable knowledge update
                                ↓
                         next supplied question
                                ↓
                    all supplied questions processed
                                ↓
          cross-question synthesis + management dashboards
                                ↓
             audit view + automation-candidate report
```

The primary objective is to complete useful business analysis. Governance, review, state, and artifact creation support that objective; they must not become the main work.

## 2. Core operating model

### 2.1 Fixed conceptual architecture, adaptive execution

The following are permanent analytical capabilities:

- Evidence Foundation
- question interpretation and planning
- semantic understanding
- relationship investigation
- business rules and documents
- process and event understanding
- data quality and fitness
- cleaning and harmonization
- analytical population
- analysis and calculation
- answer and claim review
- reusable knowledge
- final dashboards and audit

They are a checklist of possible work, not a mandatory sequence of independent acceptance gates.

For each question, the Lead Analyst selects only the work that is useful. A simple one-table question may need semantics, a quality check, a population, and a calculation. A complex question may need relationships, documents, process analysis, and cleaning. Do not dispatch agents or create artifacts merely to declare a capability `not_required`.

### 2.2 One Lead Analyst owns each question

The Lead Analyst:

- understands the question;
- reads the current reusable knowledge;
- chooses relevant evidence;
- decides which analytical capabilities are needed;
- performs or delegates the work;
- selects tools and output formats;
- creates scripts only when useful;
- produces the draft answer;
- records assumptions, proxies, limitations, and unsupported parts.

The Lead Analyst may call specialist agents for bounded tasks. Specialists advise the active question; they do not create parallel question lifecycles.

### 2.3 Review at the business-result boundary

Every question receives one independent review after the draft answer and its material evidence are ready.

Do not require a separate independent reviewer for every intermediate semantic, relationship, quality, cleaning, or population artifact.

A targeted early specialist review is allowed only when a high-impact irreversible interpretation would invalidate all downstream work, such as a materially ambiguous join or a policy whose applicability determines the entire calculation. Even then, keep it narrow and do not create a second control pipeline.

### 2.4 Maximum one repair cycle per question

The normal cycle is:

```text
Lead Analyst draft
→ Independent Reviewer
→ accept / accept_with_limits / one targeted repair / block unsupported part
→ one repair when requested
→ short fresh recheck
→ final answer
```

Do not create candidate-v1/v2/v3/v4 chains, repeated rethink waves, or reviewer-of-reviewer loops.

If one repair does not resolve a genuine issue:

- preserve the supported parts;
- state the unresolved part;
- return `partial_answer`, `answered_with_limits`, `blocked_by_evidence`, or `technical_failure`;
- continue to the next supplied question.

## 3. Non-negotiable rules

### 3.1 Process supplied questions in order

When the user supplies questions:

- preserve their original wording;
- preserve their order;
- do not discover or activate additional business questions unless explicitly requested;
- process exactly one active question at a time;
- continue to the next supplied question after the current question receives a final outcome.

Internal analytical subquestions are allowed. They are not new Question Registry items.

### 3.2 Start clean when requested

When the user requests a fresh, blind, or clean-room run:

- create a new run identity and empty output root;
- do not read previous runs, dashboards, reports, scripts, ontologies, caches, reviews, or prior agent outputs;
- use only the current skill, current dataset, supplied context, and supplied questions;
- do not infer answers from memory of a previous run.

### 3.3 Keep sources read-only

Do not modify raw files, source systems, SAP records, databases, APIs, or previous accepted artifacts.

Create derived files in the new run workspace. External writes require explicit user instruction for the exact action.

### 3.4 Produce the strongest supported answer

Do not require a unique enterprise-authoritative definition before useful analysis can begin.

When official authority is unavailable, use one of these clearly labelled evidence levels:

1. `authoritative` — established by an applicable official source or unambiguous system authority;
2. `confirmed_source_local` — strongly established within the selected source and scope;
3. `working_proxy` — a reasonable analytical proxy chosen for this question;
4. `exploratory_only` — useful for diagnostics but not for final quantitative claims.

Final answers may use levels 1–3 when the level, scope, and limitation are explicit. Level 4 must remain diagnostic.

Examples:

- use a confirmed date field as a source-local completion event;
- use a stated working definition of customer commitment;
- calculate alternative scenarios when two reasonable definitions exist;
- report a policy-based scenario without claiming full compliance when document authority is incomplete.

Never present a proxy as an official enterprise definition.

### 3.5 Partial answers are required when possible

When only part of a question is supported:

- answer the supported part;
- identify the unsupported part;
- explain exactly what evidence is missing;
- do not block the whole question unless no material part can be answered safely.

Example:

```text
Supported:
719 of 1,220 support cases have no closed_at value.

Limitation:
Missing closed_at is not identical to operationally unresolved.

Unsupported:
Causal attribution to carriers and warehouses is not established.
```

### 3.6 A blocker must block only what it invalidates

Missing cross-system identity may block a joined ranking while leaving source-local counts valid.

Missing document precedence may block a compliance conclusion while leaving a policy-scenario calculation valid.

Missing proof of causality must not block descriptive associations.

### 3.7 Continue after limited, blocked, or failed questions

A question-level outcome must not stop the complete supplied queue.

Allowed outcomes:

- `answered`
- `answered_with_limits`
- `partial_answer`
- `null_finding`
- `blocked_by_evidence`
- `unsupported`
- `technical_failure`

`technical_failure` describes a problem in the workflow, tool, parser, or agent execution. It must never be presented as a conclusion about the data.

Continue to the next independent question after recording any of these outcomes.

Stop the whole run only when a global infrastructure failure makes all remaining questions impossible.

### 3.8 Scripts are optional; preservation is mandatory when used

Agents may create:

- Python;
- SQL;
- shell;
- notebooks;
- spreadsheet formulas;
- mappings;
- transformed tables;
- chart code;
- dashboard code.

Create them only when they improve accuracy, scale, repeatability, or clarity.

If a script, query, formula, notebook, transformation, or material command affects a result, preserve:

- its purpose;
- the file itself;
- material inputs;
- material outputs;
- assumptions;
- limitations.

Do not create code merely to satisfy the skill.

### 3.9 Reusable knowledge is lightweight and non-blocking

After a reviewed final answer, promote only reusable business knowledge:

- business objects;
- grains;
- field meanings;
- measured relationships;
- rule applicability;
- process definitions;
- metric definitions;
- reusable quality or cleaning knowledge;
- known limitations.

Do not store review IDs, repair generations, parser status, candidate freeze hashes, or lifecycle control data in the Living Ontology.

A knowledge update may be:

```text
promoted
promoted_with_limits
none
```

`none` is valid and must never block the next question.

Do not run a separate ontology-finalization review. The final answer review determines which knowledge is safe to promote.

### 3.10 Build dashboards after the complete queue

Do not publish management dashboards between questions.

Exploratory charts are allowed during analysis.

After every supplied question has an outcome, build:

- a management dashboard or dashboard suite;
- an audit / technical view;
- a concise cross-question synthesis;
- an automation-candidate report.

Build these even when some questions are limited or blocked. A data-readiness and evidence-gap view is a legitimate part of the final product.

## 4. Inputs

Inputs may include:

- ZIP data rooms;
- SAP extracts or access metadata;
- databases or data warehouses;
- Excel, CSV, JSON, Parquet, or similar files;
- PDF, DOCX, TXT, Markdown, email exports, policies, SOPs, contracts, or presentations;
- APIs or application exports;
- business context;
- supplied business questions;
- an optional prior Living Ontology, unless clean-room mode is requested.

Work with what is available and state what is not.

## 5. Roles

### 5.1 Run Director

The activating agent becomes the Run Director.

The Run Director:

- reads project instructions;
- creates or resumes one run workspace;
- records the skill version;
- builds the lightweight Evidence Foundation;
- registers supplied questions;
- keeps one active question;
- assigns a Lead Analyst;
- calls specialists only when useful;
- assigns one Independent Reviewer per question;
- continues the queue after question-level blockers or technical failures;
- triggers final product generation after the queue is complete;
- creates the final automation-candidate report.

### 5.2 Lead Analyst

The Lead Analyst owns the natural analytical workflow for one question.

### 5.3 Optional Specialists

Possible specialist missions include:

- data exploration;
- semantic interpretation;
- relationship measurement;
- document interpretation;
- process analysis;
- quality assessment;
- cleaning;
- calculation;
- visualization.

Specialists return concise findings and material artifacts to the Lead Analyst. Their work does not require separate stage acceptance unless the Lead Analyst explicitly identifies a high-impact risk.

### 5.4 Independent Reviewer

The Independent Reviewer checks the complete draft answer, evidence, assumptions, relationships, calculations, and statement strength.

The Reviewer must not demand enterprise authority where a clearly labelled source-local or proxy answer is valid.

### 5.5 Product Builder and Product Reviewer

After all questions, the Product Builder creates the final dashboards and audit view. One Product Reviewer checks that published values and statements match reviewed question results.

## 6. Minimal workspace

Use the simplest useful workspace. A recommended structure is:

```text
run/
├── run_state.json
├── RUN_SUMMARY.md
├── inputs/
│   └── source_manifest.md
├── evidence/
│   ├── inventory.csv
│   └── evidence_summary.md
├── questions/
│   ├── QUESTION_REGISTRY.md
│   └── Q-001/
│       ├── question.md
│       ├── plan.md
│       ├── analysis_trace.md
│       ├── scripts/
│       ├── outputs/
│       ├── draft_answer.md
│       ├── review.md
│       └── final_answer.md
├── knowledge/
│   ├── living_ontology.jsonl
│   └── ontology_summary.md
└── final/
    ├── management_dashboard.*
    ├── audit_report.md
    ├── cross_question_synthesis.md
    ├── automation_candidates.md
    └── final_run_report.md
```

Do not create empty folders or files. Do not create separate stage directories unless the actual work benefits from them.

`run_state.json` is the authoritative lifecycle state. Markdown is a human-readable view only. Never parse free-form Markdown to determine active question, terminal status, or next action.

## 7. Run lifecycle

### Phase A — Initialize

1. Read repository and project instructions.
2. Determine whether the run is fresh, clean-room, or resume.
3. Create `run_state.json` with:
   - run ID;
   - skill name and version;
   - input references;
   - question mode;
   - current active question;
   - question statuses;
   - output root.
4. Establish read-only source behavior.
5. Preserve the original supplied questions.

### Phase B — Lightweight Evidence Foundation

Build a broad physical map:

- files and source types;
- tables, sheets, documents, and broad business areas;
- date coverage;
- obvious identifiers, currencies, units, and source systems;
- inaccessible or malformed inputs;
- major limitations.

Do not deeply profile every file before the first answer. Inventory globally, investigate deeply only for the active question.

Reuse profiles and extracts across later questions.

### Phase C — Question Registry

When questions are supplied:

- use `user_supplied` origin;
- preserve exact wording and order;
- disable discovery;
- activate the first question;
- leave the rest queued.

### Phase D — Sequential question loop

For each active question:

#### D1. Load reusable knowledge

Read the current Living Ontology. Reuse items that remain applicable. Validate only the knowledge material to this question.

#### D2. Create a minimal analysis plan

Write one concise `plan.md` containing:

- what the question is asking;
- the expected answer shape;
- relevant evidence;
- required analytical capabilities;
- working definitions or scenarios;
- material risks;
- likely scripts or tools;
- what will be considered sufficient.

Do not create a separate plan artifact for every analytical capability.

#### D3. Execute a natural analytical pass

The Lead Analyst may perform any of the following as needed:

- inspect relevant sources;
- clarify semantics;
- measure relationships;
- read policies or contracts;
- reconstruct events or processes;
- assess quality;
- create derived cleaning;
- define population and denominator;
- calculate;
- visualize;
- delegate specialist missions.

Record the natural work in `analysis_trace.md`:

- evidence used;
- tools used;
- scripts created;
- key decisions;
- assumptions and proxies;
- outputs;
- unresolved issues.

#### D4. Produce the draft answer

The draft must separate:

- direct answer;
- key numbers or findings;
- scope and period;
- working definitions;
- methodology;
- supported parts;
- unsupported parts;
- limitations;
- association versus causality;
- next evidence needed.

#### D5. Independent review

The Reviewer checks:

- source relevance;
- material semantic choices;
- join quality and fanout where applicable;
- population and denominator;
- calculations and reproducibility;
- use of documents;
- assumptions and proxy labels;
- claim strength;
- whether a partial answer is possible.

The Reviewer returns one of:

- `accept`
- `accept_with_limits`
- `repair_once`
- `block_specific_claims`

#### D6. Repair once when needed

Make one targeted repair. Preserve the original draft and review. Perform a short fresh recheck of the repaired points only.

#### D7. Finalize the question

Write `final_answer.md` and assign one question outcome.

Update `run_state.json`.

#### D8. Update reusable knowledge

Promote reviewed reusable business knowledge, or record `none`. Do not block the queue.

Activate the next supplied question.

### Phase E — Cross-question synthesis

After all questions have outcomes:

- identify common themes;
- distinguish supported findings from limitations;
- reconcile compatible metrics;
- expose contradictions;
- summarize business implications;
- list missing data and definitions.

Do not create new calculations in synthesis. Return to the source question only when a genuine material defect is found.

### Phase F — Final products

Create:

1. management dashboard or dashboard suite;
2. audit / technical report;
3. cross-question synthesis;
4. final run report;
5. automation-candidate report.

Use the simplest format supported by the environment. Prefer the existing project stack. Static HTML, Markdown, or a lightweight local app are acceptable when they are genuinely usable.

## 8. Adaptive analytical capability checklist

Use `references/QUESTION_ANALYSIS_PLAYBOOK.md` for details.

### 8.1 Semantic understanding

Needed when field, object, date, amount, status, unit, or grain meaning is material.

A source-local working meaning is sufficient when labelled and evidenced.

### 8.2 Relationships

Needed when multiple tables or sources contribute to a result.

Measure:

- key overlap;
- uniqueness;
- coverage;
- multiplicity;
- fanout;
- unmatched records;
- temporal consistency;
- contradictions.

Relationship evidence levels:

- `confirmed`;
- `strong_source_local`;
- `exploratory_only`;
- `unsupported`.

Final quantitative analysis may use `confirmed` or `strong_source_local` relationships when limitations are explicit.

### 8.3 Documents and rules

Needed when a policy, contract, SLA, SOP, or approval rule changes the answer.

When authority is incomplete:

- provide a document-based scenario;
- do not claim full compliance;
- state missing version, precedence, effective-date, or approval evidence.

### 8.4 Processes and events

Needed for delays, transitions, cycle time, handoffs, rework, or incomplete cases.

Use source-local process definitions when enterprise-wide authority is unavailable.

### 8.5 Quality and fitness

Assess only issues that can materially change the active answer.

Do not produce a generic quality score as a substitute for analysis.

### 8.6 Cleaning

Clean only when material to the answer.

Preserve raw values and create a derived result. Distinguish normalization, correction, exclusion, and quarantine.

### 8.7 Analytical population

Define:

- base population;
- eligible population;
- exclusions;
- unresolved records;
- denominator;
- grain;
- period;
- dimensions.

### 8.8 Analysis

Use actual data. Prefer saved scripts for material, repeated, or complex calculations. Record enough method detail to reproduce simple calculations.

## 9. Review protocol

Use `references/REVIEW_PROTOCOL.md`.

The Reviewer is not a governance engine. The purpose is to catch material analytical errors and overclaiming.

The Reviewer must not reject a valid answer solely because:

- no unique enterprise-wide definition exists;
- a clearly labelled source-local proxy was used;
- a supported partial answer does not cover the entire original question;
- an ontology update is `none`;
- a Markdown format differs from a template.

Do not write verifier scripts that parse prose, bullets, backticks, headings, or wording to determine acceptance or lifecycle state.

Mechanical checks may verify:

- a file exists;
- a script runs;
- a calculation matches;
- a required structured field exists;
- raw sources are unchanged.

Business judgment belongs to the Lead Analyst and Independent Reviewer.

## 10. Living Ontology

Use `references/KNOWLEDGE_AND_REUSE.md`.

The Living Ontology exists to reduce repeated business interpretation.

Each item should record:

- item type;
- business meaning;
- source scope;
- evidence references;
- confidence/evidence level;
- limitations;
- originating question;
- status.

Before reuse, check scope and freshness.

Do not make ontology promotion a separate multi-wave process.

## 11. Artifact and efficiency policy

Use `references/ARTIFACT_AND_EFFICIENCY_POLICY.md`.

Before creating an artifact, ask:

```text
Does this materially help answer the question,
preserve reproducibility,
enable reuse,
or communicate the result?
```

If not, do not create it.

Efficiency rules:

- batch related inspections;
- reuse profiles and intermediate outputs;
- do not reread the full data room for every question;
- use scripts for repeated tabular work;
- avoid repeated candidate versions;
- avoid exhaustive formalism on small data;
- stop specialist work when sufficient evidence exists;
- keep review focused on material risks;
- make workflow effort proportional to dataset and question complexity.

## 12. Final dashboards and audit

Use `references/FINAL_PRODUCT_AND_AUTOMATION.md`.

The management dashboard should show:

- reviewed answers;
- key metrics;
- segments and concentrations;
- trends or distributions;
- assumptions and limitations;
- blocked components;
- data-readiness gaps.

The audit view should show:

- source inventory;
- question methods;
- evidence references;
- scripts and outputs when created;
- assumptions and proxies;
- reviewer verdicts;
- reusable knowledge;
- traceability from dashboard elements to question results.

The Product Builder must not silently perform new analysis.

## 13. Automation-candidate report

This report is central to the skill.

After the run, inspect the work agents actually performed.

For each repeated operation, record:

- operation;
- questions where it occurred;
- tools or scripts used;
- approximate repetition or effort;
- failure modes;
- whether business judgment was required;
- whether deterministic code could safely replace or assist it;
- recommended next implementation;
- expected benefit.

Classify candidates:

- `mechanical_now`;
- `deterministic_after_more_runs`;
- `keep_agentic`;
- `do_not_automate`.

Do not implement the candidates during the same analytical run.

## 14. Completion criteria

A run is complete when:

- every supplied question has an outcome;
- supported answers are delivered even when other parts are blocked;
- one independent review has been resolved per question;
- material scripts and outputs are preserved;
- reusable knowledge has been updated or explicitly recorded as `none`;
- the final dashboard and audit view exist;
- the automation-candidate report exists;
- the final report states skill version `0.2.0`.

## 15. Anti-patterns

Do not:

- turn every analytical capability into a mandatory stage gate;
- require independent review for every intermediate artifact;
- create reviewer-of-reviewer loops;
- create more than one repair cycle per question;
- parse Markdown prose to determine state;
- let a control failure masquerade as a data conclusion;
- require enterprise authority before using a labelled working proxy;
- block supported parts because another part is unsupported;
- stop the supplied queue after one limited or blocked question;
- make ontology promotion block the next question;
- fill the ontology with lifecycle metadata;
- create scripts only for formality;
- create large candidate/review/freeze trees;
- spend more effort proving workflow correctness than performing analysis;
- build dashboards before the complete queue;
- claim causality from association;
- modify raw sources.
