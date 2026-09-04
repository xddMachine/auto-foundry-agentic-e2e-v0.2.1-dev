"""Offline contract checks for the v0.8.0 universal-ingestion skill tree.

These tests intentionally inspect owned text and templates only. Core loading
of the item-state template is covered by the offline integration vertical;
these checks do not call a model, read a dataset, or perform a run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import auto_foundry_core as core
from auto_foundry_core.lifecycle import classify_terminal_reason


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "auto-foundry-agentic-e2e"

OWNED_MARKDOWN = (
    SKILL / "SKILL.md",
    SKILL / "README.md",
    SKILL / "CHANGELOG.md",
    SKILL / "TEST_PROMPTS.md",
    SKILL / "references" / "QUESTION_ANALYSIS_PLAYBOOK.md",
    SKILL / "references" / "ANALYTICAL_COLLABORATION.md",
    SKILL / "references" / "KNOWLEDGE_AND_REUSE.md",
    SKILL / "references" / "REVIEW_PROTOCOL.md",
    SKILL / "references" / "ARTIFACT_AND_EFFICIENCY_POLICY.md",
    SKILL / "references" / "FINAL_PRODUCT_AND_AUTOMATION.md",
    SKILL / "references" / "ANALYTICS_TOOLKIT.md",
    SKILL / "assets" / "QUESTION_RESULT_TEMPLATE.md",
    SKILL / "assets" / "DASHBOARD_PROTOTYPE_TEMPLATE.md",
)
OWNED_JSON = (
    SKILL / "assets" / "RUN_STATE_TEMPLATE.json",
    SKILL / "assets" / "TELEMETRY_EVENT_TEMPLATE.json",
    SKILL / "assets" / "ITEM_STATE_TEMPLATE.json",
    SKILL / "assets" / "REQUIREMENT_RECORD_TEMPLATE.json",
)


def _read(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _owned_text() -> str:
    return "\n".join(_read(path) for path in OWNED_MARKDOWN + OWNED_JSON)


def test_product_docs_enforce_single_product_agent_assembly_flow() -> None:
    for path in (SKILL / "references/PRODUCT_AGENT_ASSEMBLER_CONTRACT.md", SKILL / "references/FINAL_PRODUCT_AND_AUTOMATION.md"):
        text = _read(path)
        for marker in ("ProductWorkspace", "workspace.inventory()", "workspace.build(choices", "Terminal technical-failure", "limited/empty-state", "Stale item bindings"):
            assert marker in text, (path, marker)
        assert "assemble_dashboard(" not in text
        assert "assemble_dashboard_delta(" not in text
        assert "cannot grant itself" in text


def test_product_docs_keep_accepted_visuals_independent_of_integration() -> None:
    """The product contract keeps accepted business evidence on the manager path."""

    text = _read(SKILL / "SKILL.md")
    assert "Every business-accepted Analytical Owner answer is a presentation input" in text
    assert "integration remains an ontology and" in text
    assert "never a prerequisite for showing an accepted business" in text
    assert "Ensure every accepted requirement receives a meaningful decision surface" in text
    assert "technical source/join/count evidence in the" in text
    assert "renders accepted answer visuals together with any committed" in text


def test_frontmatter_and_run_markers_are_v072() -> None:
    skill = _read(SKILL / "SKILL.md")
    normalized_skill = " ".join(skill.split())
    assert skill.startswith("---\n")
    frontmatter = skill.split("---\n", 2)[1]
    assert "name: auto-foundry-agentic-e2e" in frontmatter
    assert 'version: "0.8.0"' in frontmatter
    assert "core_name: auto_foundry_core" in frontmatter
    assert 'core_version: "0.9.0"' in frontmatter
    assert "release: reliable-analytics-dashboard" in frontmatter
    assert "release: cognitive-requirement-supervisor-and-simple-item-flow" not in frontmatter
    assert "release: monotonic-accepted-preservation-and-context-root-rebind" not in frontmatter
    assert "release: visual-repair-evidence-reference-scope" not in frontmatter
    assert "release: standalone-rebindable-context" not in frontmatter
    assert "release: targeted-review-scope-recompute" not in frontmatter
    assert "release: accepted-intermediate-context-and-authoritative-portfolio-plan" not in frontmatter
    assert "release: rebindable-inherited-context-and-transitioned-source-preflight" not in frontmatter
    assert "release: inherited-catalog-context-audit-and-active-repair-context-rebase" not in frontmatter
    for marker in (
        "skill_name: auto-foundry-agentic-e2e",
        "skill_version: 0.8.0",
        "core_name: auto_foundry_core",
        "core_version: 0.9.0",
    ):
        assert marker in skill
    assert "One Analytical Owner" in skill
    assert "AnalystWorkspace" in skill
    assert "event-driven Planner" in skill or "event-driven planner" in normalized_skill
    assert "RequirementExecutionPlan" in skill
    assert "RequirementExecutionGroup" in skill
    assert "technical failure" in normalized_skill
    assert "same run context and shared data room" in normalized_skill
    assert "ANALYTICS_TOOLKIT.md" in skill


def test_current_release_changelog_preserves_historical_release() -> None:
    changelog = _read(SKILL / "CHANGELOG.md")
    top = changelog.split("## 0.7.2 / core 0.8.1", 1)[0]
    assert "## 0.8.0 / core 0.9.0" in top
    for marker in ("ProductWorkspace", "snapshot recovery", "offline", "No live-agent"):
        assert marker in top
    assert "## 0.7.2 / core 0.8.1 — universal Data Room ingestion" in changelog


def test_run_state_template_is_exact_lifecycle_authority() -> None:
    state = json.loads(_read(OWNED_JSON[0]))
    assert set(state) == {
        "run_id",
        "run_root",
        "item_ids",
        "mode",
        "status",
        "generation",
        "manifest_hash",
        "created_at",
        "updated_at",
    }
    assert state["run_id"] == "RUN-example"
    assert state["run_root"] == "/current/run"
    assert state["item_ids"] == ["Q-001"]
    assert state["mode"].split("|") == ["question", "requirement"]
    assert state["status"].split("|") == [
        "initialized",
        "running",
        "paused",
        "analytical_complete",
        "integration_complete",
        "products_complete",
        "complete",
        "complete_with_limits",
    ]
    assert state["generation"] >= 0
    assert state["manifest_hash"].startswith("<sha256(")
    assert state["created_at"].endswith("+00:00")
    assert state["updated_at"].endswith("+00:00")


def test_item_state_template_is_authoritative_and_durable() -> None:
    state = json.loads(_read(OWNED_JSON[2]))
    assert set(state) == {
        "item_id",
        "mode",
        "original_text",
        "lifecycle_state",
        "execution_recovery_count",
        "business_repair_count",
        "created_at",
        "updated_at",
        "attempts",
        "active_attempt_id",
        "consecutive_no_progress",
        "review",
        "terminal_outcome",
        "terminal_intent",
        "integration_state",
        "integration_manifest_hash",
        "integration_manifest_ref",
    }
    assert state["item_id"] == "Q-001"
    assert state["mode"] == "question"
    assert state["original_text"]
    assert state["lifecycle_state"] == "work"
    assert state["execution_recovery_count"] == 0
    assert state["business_repair_count"] == 0
    assert state["attempts"] == []
    assert state["active_attempt_id"] is None
    assert state["consecutive_no_progress"] == 0
    assert state["review"]["status"] == "pending"
    assert state["integration_state"] == "pending"
    assert isinstance(state["created_at"], str) and "T" in state["created_at"]
    assert isinstance(state["updated_at"], str) and "T" in state["updated_at"]


def test_telemetry_template_observes_attempts_and_artifacts_only() -> None:
    event = json.loads(_read(OWNED_JSON[1]))
    assert "foundation-" not in event["item_id"]
    assert event["item_id"] == "Q-...|R-...|product|optimizer"
    assert {"lane", "role", "route", "started_at", "ended_at"} <= set(
        event["invocation"]
    )
    assert {"provider", "model", "host", "process"} <= set(event["invocation"])
    assert {"before", "after", "count_delta", "consecutive_no_progress"} <= set(
        event["artifact_progress"]
    )
    assert {
        "execution_recovery_count",
        "business_repair_count",
        "source_reads",
        "member_reads",
    } <= set(event["counts"])
    assert {"core_operation", "cache_checked", "cache_hit", "receipt_ref"} <= set(
        event["core_cache"]
    )
    assert event["physical_inventory"]["binding"] == "run_level_canonical"
    assert {
        "initial_full_bind",
        "child_context_reinventory",
        "selected_member_verified",
        "final_explicit_verification",
    } <= set(event["physical_inventory"]["counters"])
    assert {"receipt_ref", "receipt_hash", "attempt_id", "lane_id"} <= set(
        event["recovery"]
    )
    assert event["passive"] is True
    assert event["records_raw_rows"] is False
    assert event["controls_route"] is False
    assert event["invocation"]["provider"] == "unavailable"
    assert event["invocation"]["model"] == "unavailable"
    assert event["terminal_reason_class"] == "same_attempt_feedback|business_repair|execution_recovery|abort_and_new_clean_run|null"
    assert event["recovery_decision"] == "continue|materialize_now|retry_same_attempt|execution_recovery|null"
    assert set(event["phase_timing"]["phase"].split("|")) >= {
        "analyst_model",
        "controlled_execution",
        "business_review",
        "business_repair",
        "fidelity_integration_review",
        "integration_commit",
        "products",
        "optimizer",
        "reporting_finalization",
        "genuine_recovery",
    }
    assert set(event["incident"]) >= {"incident_id", "category", "admissible", "disposition"}


def test_requirement_and_dashboard_templates_use_program_owned_boundaries() -> None:
    requirement = json.loads(_read(SKILL / "assets" / "REQUIREMENT_RECORD_TEMPLATE.json"))
    assert "execution_contract" not in requirement
    assert requirement["original_text"] == (
        "Dashboard should show the ratio of milk fat content to the procurement price "
        "of the raw material for that milk."
    )
    plan = requirement["requirement_plan"]
    assert plan["tasks"]
    assert 1 <= len(plan["tasks"])
    assert plan["tasks"][0]["task_id"] == "T-1"
    assert "milk lot" in plan["tasks"][0]["question"]
    assert "ratio" in plan["tasks"][1]["question"]
    assert requirement["program_owned"]["parent_item_only"] is True
    assert requirement["program_owned"]["semantic_plan_before_analysis"] is True
    assert "shared_foundation_dependencies" not in requirement
    assert "foundation_dependencies" not in requirement
    assert "internal_tasks" not in requirement
    assert "planner" not in requirement
    assert requirement["program_owned"]["analysis_owner_authority"] == {
        "record": "work/analysis_owner.json",
        "bound_by": "program host/router",
        "owner_ref": "<host-bound stable owner_ref>",
        "analytical_agents_emit_owner_ref": False,
        "same_owner_scope": "requirement_plan, draft, both business repairs, and DataInsufficiencyConclusion",
    }
    assert "material business repairs may repeat" in requirement["program_owned"]["repair_budget"]
    assert "DataInsufficiencyConclusion" in requirement["program_owned"]["blocked_by_evidence_authority"]
    assert requirement["review"]["verdict"].startswith("accept|")
    assert "confirm_data_insufficiency" in requirement["review"]["verdict"]
    dashboard = _read(SKILL / "assets" / "DASHBOARD_PROTOTYPE_TEMPLATE.md")
    assert '"freeze_markers"' in dashboard
    assert '"prepared_data_registry_frozen": true' in dashboard
    assert "decision_flow" in dashboard
    assert "decision_flows" not in dashboard


def test_terminal_reason_classifier_matches_current_template_vocabulary() -> None:
    expected = {
        "syntax_error": "same_attempt_feedback",
        "business_review_error": "business_repair",
        "process_lost": "execution_recovery",
        "core_defect": "abort_and_new_clean_run",
        None: None,
    }
    for raw_reason, classification in expected.items():
        assert classify_terminal_reason(raw_reason) == classification

    telemetry = json.loads(_read(SKILL / "assets" / "TELEMETRY_EVENT_TEMPLATE.json"))
    assert telemetry["terminal_reason_class"] == "same_attempt_feedback|business_repair|execution_recovery|abort_and_new_clean_run|null"


def test_requirement_supervisor_exports_are_public_and_foundation_is_removed() -> None:
    assert not hasattr(core, "FoundationTask")
    assert core.RequirementExecutionPlan is not None
    assert core.RequirementExecutionGroup is not None
    assert core.RequirementSupervisorWorkspace is not None
    assert callable(core.compact_catalog_payload)


def test_requirement_scheduler_and_bounded_resolution_scan_are_explicit() -> None:
    text = "\n".join(
        _read(path)
        for path in (
            SKILL / "SKILL.md",
            SKILL / "README.md",
            SKILL / "TEST_PROMPTS.md",
            SKILL / "references" / "ANALYTICAL_COLLABORATION.md",
            SKILL / "references" / "QUESTION_ANALYSIS_PLAYBOOK.md",
            SKILL / "references" / "KNOWLEDGE_AND_REUSE.md",
        )
    )
    normalized = " ".join(text.split()).lower()
    assert "requirementsupervisorworkspace.scheduling_tick()" in normalized
    assert "domain-relevant tables" in normalized
    assert "scans the full shared data room" not in normalized
    assert "scan the full shared data room" not in normalized


def test_semantic_reuse_methods_and_trace_are_public_contract() -> None:
    text = _read(SKILL / "SKILL.md") + _read(SKILL / "README.md") + _read(
        SKILL / "TEST_PROMPTS.md"
    ) + _read(SKILL / "references" / "KNOWLEDGE_AND_REUSE.md")
    normalized = " ".join(text.split()).lower()
    for method in (
        "brief()",
        "search_ontology()",
        "select_ontology()",
        "search_prepared_assets()",
        "select_prepared_assets()",
        "load_prepared_asset()",
    ):
        assert method in normalized
    assert "work/semantic_selections.jsonl" in normalized
    workspace = core.AnalystWorkspace
    for method_name in (
        "brief",
        "search_ontology",
        "select_ontology",
        "search_prepared_assets",
        "select_prepared_assets",
        "load_prepared_asset",
    ):
        assert callable(getattr(workspace, method_name, None))


def test_question_mode_preserves_queue_and_one_analytical_owner() -> None:
    text = _read(SKILL / "SKILL.md") + _read(SKILL / "TEST_PROMPTS.md")
    normalized = " ".join(text.split()).lower()
    for phrase in (
        "one question at a time",
        "one analytical owner",
        "full reasoning loop",
        "smallest useful set",
        "genuinely independent uncertainty",
        "material uncertainty",
        "actual host capacity",
        "capacity is adaptive",
        "zero is valid",
        "never create one specialist per method or checklist item",
        "same owner and attempt",
        "one independent business reviewer",
        "material repairs may repeat",
        "targeted recheck",
        "one result integration agent",
        "build the reviewed-output dashboard",
    ):
        assert phrase in normalized
    for obsolete in ("zero to three", "up to three", "0-3", "eight active workers"):
        assert obsolete not in normalized
    assert "relay roles" in normalized
    assert "independent groups may run when host capacity permits" in normalized


def test_requirement_mode_is_analytics_only_and_priority_owned() -> None:
    text = (
        _read(SKILL / "SKILL.md")
        + _read(SKILL / "TEST_PROMPTS.md")
        + _read(SKILL / "assets" / "QUESTION_RESULT_TEMPLATE.md")
    )
    normalized = " ".join(text.split()).lower()
    for field in (
        "original_text",
        "priority",
        "objective",
        "expected analytical outputs",
        "expected visual outputs",
        "requirement plan tasks",
        "dependencies",
        "data needs",
        "ontology needs",
        "prepared-data needs",
        "working definitions",
        "limits",
        "status",
    ):
        assert field.lower() in normalized
    for classification in (
        "analytics_in_scope",
        "analytics_requires_missing_data",
        "out_of_analytics_scope",
    ):
        assert classification in text
    assert "honor explicit priority" in normalized
    assert "requirementanalysisplan" in normalized
    assert "requirementanalysistask" in normalized
    assert "milk fat content to the procurement price" in normalized
    assert "child lifecycle workspaces" in normalized
    assert "owner_ref" in normalized
    assert "work/analysis_owner.json" in normalized
    assert "analytical agents never emit" in normalized
    assert "same bound owner" in normalized
    assert "event-driven planner" in normalized
    assert "requirementexecutionplan" in normalized
    assert "requirementexecutiongroup" in normalized
    assert "sole semantic block" in normalized
    assert "same runcontext" in normalized
    assert "shared data room" in normalized
    assert "keyword router" in normalized
    assert "business-term dictionary" in normalized
    assert "boundanalysiscontext" in normalized
    assert "analystworkspace" in normalized
    assert "result integration agent" in normalized
    assert "independent business reviewer" in normalized
    assert "integration fidelity reviewer" in normalized


def test_question_result_no_progress_decisions_exclude_recovery() -> None:
    lines = _read(SKILL / "assets" / "QUESTION_RESULT_TEMPLATE.md").splitlines()
    no_progress = next(line for line in lines if line.startswith("- No-progress decisions:"))
    assert no_progress == "- No-progress decisions: `materialize_now` | `retry_same_attempt`"
    assert "recover" not in no_progress
    recovery = " ".join(
        line.strip()
        for line in lines[lines.index(no_progress) + 1 : lines.index(no_progress) + 3]
    )
    assert "separate from no-progress decisions" in recovery
    assert "canonical persisted execution-loss receipt" in recovery


def test_role_boundaries_keep_reasoning_with_owner_and_formats_with_program() -> None:
    text = _owned_text()
    normalized = " ".join(text.split()).lower()
    for phrase in (
        "interpret the decision",
        "choose the answer strategy",
        "calculate and explore alternatives",
        "write the complete business answer",
        "program-owned infrastructure",
        "does not author json files",
        "reviewer never authors json pointers",
        "only agent-facing role that works with typed internal integration records",
    ):
        assert phrase in normalized
    assert "handoff-only lead analyst" in normalized
    assert "second author" in normalized


def test_data_room_identity_review_and_prepared_asset_contract() -> None:
    text = _owned_text().lower()
    for phrase in (
        "raw inputs read-only",
        "bounded source catalog",
        "hashes",
        "identity",
        "source-completeness",
        "specialist memo",
        "population",
        "denominator",
        "grain",
        "accepted",
        "integration fidelity reviewer",
        "prepared data registry",
    ):
        assert phrase in text, phrase


def test_catalog_is_optional_internal_and_custom_code_allowed() -> None:
    text = _read(SKILL / "SKILL.md") + _read(
        SKILL / "references" / "QUESTION_ANALYSIS_PLAYBOOK.md"
    )
    normalized = " ".join(text.split()).lower()
    assert "search_sources()" in normalized
    assert "sample_source()" in normalized
    assert "source_categories()" in normalized
    assert "reproducible script" in normalized
    assert "run_analysis()" in normalized


def test_reviewer_uses_semantic_findings_not_internal_schema() -> None:
    text = _read(SKILL / "SKILL.md") + _read(SKILL / "references" / "REVIEW_PROTOCOL.md")
    normalized = " ".join(text.split()).lower()
    assert "one fresh independent business reviewer" in normalized
    assert "target answer section" in normalized
    assert "businessreviewadapter" in normalized
    assert "does not provide json pointers" in normalized
    assert "unknown sections and categories fail" in normalized
    assert "one targeted recheck" in normalized
    assert "accept_with_limits" in text
    assert "confirm_data_insufficiency" in text
    assert "blocked_by_evidence" in text
    assert "datainsufficiencyconclusion" in normalized
    assert "only the analytical owner" in normalized
    assert "block_" + "specific_claims" not in normalized
    assert "result integration agent" in normalized
    assert not re.search(r"\b(?:gpt|claude|gemini|llama|sonnet|opus)[-_\d]", text, re.I)


def test_dashboard_telemetry_and_optimizer_boundary_remains_intact() -> None:
    text = " ".join(_owned_text().lower().split())
    for phrase in (
        "whole-run freeze",
        "accepted snapshots",
        "reviewed outputs",
        "offline local",
        "internal links",
        "traceability",
        "passive",
        "optimizer_evidence_bundle.md",
        "optimizer_evidence_appendix.md",
        "evidence bundle",
        "read-only",
        "observed evidence",
        "hypothesis",
        "expected benefit",
        "client business",
        "auto-promote",
    ):
        assert phrase in text, phrase


def test_regression_prohibitions_do_not_reintroduce_legacy_workflow() -> None:
    text = _owned_text().lower()
    active_text = "\n".join(
        _read(path) for path in OWNED_MARKDOWN if path.name != "CHANGELOG.md"
    ).lower()
    fake_text = _read(ROOT / "tests" / "skill" / "fake_requirement_mode.py")
    # Exact old markers and positive legacy workflow instructions are absent.
    assert "skill_version: 0.2.1" not in text
    assert "core_version: 0.1.0" not in text
    assert "skill_version: 0.3.0" not in active_text
    assert "skill_version: 0.4.0" not in active_text
    assert "skill_version: 0.4.1" not in active_text
    assert "core_version: 0.5.1" not in active_text
    assert "skill_version: 0.2" + ".9" not in active_text
    assert "core_version: 0.3" + ".6" not in active_text
    assert "block_" + "specific_claims" not in active_text
    assert "one permitted business" + " repair" not in active_text
    assert "single permitted repair" not in active_text
    assert "one repair" + " maximum" not in active_text
    assert "navigator selects bounded" not in text
    assert "inspect the local capability catalog" not in text
    assert "one concise lead analyst self-check" not in text
    assert "ontology finalization gate" not in text
    assert "marketing funnel recipe" not in text
    assert "sap implementation" not in text
    assert "cross-run cache" in text  # explicit prohibition remains
    assert "handoff-only lead analyst" in text
    assert "reviewer never authors json pointers" in text
    assert "program schemas and paths" in text
    assert "compatibility wrapper" not in active_text
    assert not re.search(r"\bsave_prepared\s*\(", active_text)
    assert "materialize_accepted" not in active_text
    assert "host capacity permits" in text
    assert "independent groups" in text
    for obsolete in ("Fake" + "PortfolioPlanner", "Fake" + "Navigator"):
        assert f"class {obsolete}" not in fake_text
    assert "class ControlledScriptRunner" in fake_text
    assert "class ResultIntegrationAgent" in fake_text


def test_owned_markdown_links_and_json_templates_resolve() -> None:
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for path in OWNED_MARKDOWN:
        for raw_target in link_pattern.findall(_read(path)):
            target = raw_target.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (path.parent / target).resolve()
            assert resolved.is_file(), f"broken link in {path}: {raw_target}"

    for path in OWNED_JSON:
        json.loads(_read(path))
