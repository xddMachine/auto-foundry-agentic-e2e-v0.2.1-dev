"""Offline contract checks for the v0.2.5 Agent Workbench skill tree.

These tests intentionally inspect owned text and templates only. Core loading
of the item-state template is covered by the offline integration vertical;
these checks do not call a model, read a dataset, or perform a run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from auto_foundry_core.lifecycle import classify_terminal_reason


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "auto-foundry-agentic-e2e"

OWNED_MARKDOWN = (
    SKILL / "SKILL.md",
    SKILL / "README.md",
    SKILL / "CHANGELOG.md",
    SKILL / "TEST_PROMPTS.md",
    SKILL / "references" / "QUESTION_ANALYSIS_PLAYBOOK.md",
    SKILL / "references" / "KNOWLEDGE_AND_REUSE.md",
    SKILL / "references" / "REVIEW_PROTOCOL.md",
    SKILL / "references" / "ARTIFACT_AND_EFFICIENCY_POLICY.md",
    SKILL / "references" / "FINAL_PRODUCT_AND_AUTOMATION.md",
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


def test_frontmatter_and_run_markers_are_v025() -> None:
    skill = _read(SKILL / "SKILL.md")
    assert skill.startswith("---\n")
    frontmatter = skill.split("---\n", 2)[1]
    assert "name: auto-foundry-agentic-e2e" in frontmatter
    assert 'version: "0.2.5"' in frontmatter
    assert "core_name: auto_foundry_core" in frontmatter
    assert 'core_version: "0.3.2"' in frontmatter
    for marker in (
        "skill_name: auto-foundry-agentic-e2e",
        "skill_version: 0.2.5",
        "core_name: auto_foundry_core",
        "core_version: 0.3.2",
    ):
        assert marker in skill
    assert "Agent Workbench" in skill
    assert "Durable Execution" in skill


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
    assert event["recovery_decision"] == "continue|await_runtime|materialization_guidance|execution_recovery|null"
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
    execution = requirement["execution_contract"]
    assert execution["bound_analysis_context_before_attempt"] is True
    assert execution["code_error_route"] == "same_attempt_feedback"
    assert execution["controlled_script_preflight_checks"] == ["compile", "dependency_check"]
    assert execution["successful_runtime_receipt_phases"] == ["smoke", "full"]
    assert execution["failed_preflight_receipt_phase"] == "compile|dependency_check"
    assert execution["script_timeout_seconds"] == 3600
    assert execution["execution_recovery_authority"] == "canonical_persisted_receipt_ref_and_hash_only"
    assert execution["prepared_asset_boundary"].startswith("candidate_under_item_work_prepared")
    assert "live_integration_agent" in execution["semantic_completeness_boundary"]
    integration = requirement["result_integration"]
    assert integration["owner"] == "one_result_integration_agent"
    assert "integration_reviewer" not in integration
    fidelity = integration["fidelity_review"]
    assert fidelity["reviewer"] == "one_item_only_integration_fidelity_reviewer"
    assert fidelity["after"] == "mechanical_validation"
    assert fidelity["before"] == "commit"
    assert fidelity["same_agent_targeted_repair"] is True
    assert set(integration["api_surfaces"]) == {
        "claims",
        "metrics",
        "limitations",
        "evidence",
        "prepared_assets",
        "ontology",
        "relationships",
        "dashboard_facts",
    }
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


def test_question_mode_preserves_queue_and_bounded_review() -> None:
    text = _read(SKILL / "SKILL.md") + _read(SKILL / "TEST_PROMPTS.md")
    normalized = " ".join(text.split()).lower()
    for phrase in (
        "preserve its original wording and order",
        "one question at a time",
        "one lead analyst",
        "one reviewer",
        "at most one targeted",
        "continue after",
        "build the final dashboard after",
        "artifact_progress",
        "materialization_guidance",
        "execution recovery",
        "same-attempt",
        "controlledscriptrunner",
        "boundanalysiscontext",
        "accepted snapshot",
    ):
        assert phrase.lower() in normalized
    assert "discover extra questions" in normalized
    assert "parallel question wave" in normalized


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
        "internal tasks",
        "dependencies",
        "foundation dependencies",
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
    assert "explicit user priority first" in normalized
    assert "one item at a time" in normalized
    assert "no separate planner framework" in normalized
    assert "keyword router" in normalized
    assert "business-term dictionary" in normalized
    assert "boundanalysiscontext" in normalized
    assert "same-attempt" in normalized
    assert "result integration agent" in normalized
    assert "independent business reviewer" in normalized
    assert "integration fidelity reviewer" in normalized


def test_question_result_no_progress_decisions_exclude_recovery() -> None:
    lines = _read(SKILL / "assets" / "QUESTION_RESULT_TEMPLATE.md").splitlines()
    no_progress = next(line for line in lines if line.startswith("- No-progress decisions:"))
    assert no_progress == "- No-progress decisions: `await_runtime` | `materialization_guidance`"
    assert "recover" not in no_progress
    recovery = " ".join(
        line.strip()
        for line in lines[lines.index(no_progress) + 1 : lines.index(no_progress) + 3]
    )
    assert "separate from no-progress decisions" in recovery
    assert "canonical persisted execution-loss receipt" in recovery


def test_workbench_sequence_and_recovery_are_progressive_and_separate() -> None:
    skill = _read(SKILL / "SKILL.md")
    workflow = skill.split("For each item, use this progressive sequence:", 1)[1]
    sequence = (
        "program builds data room/source catalog",
        "program creates item_state.json and mutable work/",
        "Lead Analyst writes plan, script, and source map first",
        "Lead Analyst appends findings, evidence, and loadable prepared assets",
        "Run Director checks artifact_progress after each response",
        "canonical persisted invocation receipt_ref/hash proves lane/provider/host/process loss",
        "materialized draft",
        "one Independent Business Reviewer",
        "at most one scoped business repair",
        "atomic immutable answer bytes + separate acceptance envelope",
        "one Result Integration Agent incremental API pass",
        "program validates and applies the reviewed Knowledge Delta",
    )
    positions = [workflow.index(part) for part in sequence]
    assert positions == sorted(positions)
    assert "There is no mandatory Navigator role" in skill
    assert "no per-item Capability Catalog lookup/compliance artifact" in skill
    normalized = skill.lower()
    assert "no wall-time deadline" in normalized
    assert "there is no terminalizer agent" in normalized
    assert "does not consume the business repair" in normalized


def test_data_room_identity_review_and_prepared_asset_contract() -> None:
    text = _owned_text().lower()
    for phrase in (
        "one physical source catalog",
        "zip/archive and member metadata",
        "raw archive remains read-only",
        "bounded columns",
        "samples or values",
        "hashes",
        "sheet information",
        "same-object representations",
        "identity-escalation",
        "source-completeness",
        "targeted source-completeness search",
        "loadable run-local assets",
        "asset hash",
        "schema",
        "grain",
        "lineage",
        "concrete reason",
        "applies the reviewed knowledge delta",
        "candidate",
        "accepted-only",
        "semantic completeness",
        "integration fidelity reviewer",
        "opaque",
        "inventory counters",
        "receipt_ref",
    ):
        assert phrase in text, phrase


def test_catalog_is_optional_internal_and_custom_code_allowed() -> None:
    text = _read(SKILL / "SKILL.md") + _read(
        SKILL / "references" / "QUESTION_ANALYSIS_PLAYBOOK.md"
    )
    normalized = " ".join(text.split()).lower()
    assert "catalog capabilities may be recommended or used internally" in normalized
    assert "custom reproducible code" in normalized
    assert "no mandatory navigator role" in normalized
    assert "no per-item capability catalog lookup/compliance artifact" in normalized
    assert "python -m auto_foundry_core catalog ..." in normalized
    assert "python -m auto_foundry_core run ..." in normalized


def test_reviewer_routing_completeness_identity_and_disclosure() -> None:
    text = _read(SKILL / "SKILL.md") + _read(SKILL / "references" / "REVIEW_PROTOCOL.md")
    normalized = " ".join(text.split()).lower()
    assert "independent business reviewer in a fresh context" in normalized
    assert "alternate independent route" in normalized
    assert "fresh same-family context" in normalized
    assert '"review_status":"unavailable"' in text
    assert '"review_strength":"none"' in text
    assert '"verdict":"not_reviewed"' in text
    assert "targeted source-completeness search" in normalized
    assert "identity-escalation route" in normalized
    assert "without repeating the full analysis" in normalized
    assert "accept_with_limits" in text
    assert "result integration agent" in normalized
    assert re.search(r"there is no [^.]*integration reviewer", normalized)
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
        "strictly read-only",
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
    assert "navigator selects bounded" not in text
    assert "inspect the local capability catalog" not in text
    assert "one concise lead analyst self-check" not in text
    assert "ontology finalization gate" not in text
    assert "marketing funnel recipe" not in text
    assert "sap implementation" not in text
    assert "cross-run cache" in text  # explicit prohibition remains
    assert "no mandatory navigator role" in text
    assert "no terminalizer agent" in text
    assert "no wall-time deadline" in text
    assert "fallback wrapper" in active_text
    assert "compatibility wrapper" not in active_text
    assert not re.search(r"\bsave_prepared\s*\(", active_text)
    assert "materialize_accepted" not in active_text
    assert "parallel question wave" in text
    assert "production application" in text  # explicit prohibition/boundary
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
