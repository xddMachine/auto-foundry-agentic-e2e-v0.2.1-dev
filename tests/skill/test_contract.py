"""Offline contract checks for the v0.2.2 Agent Workbench skill tree.

These tests intentionally inspect owned text and templates only. Core loading
of the item-state template is covered by the offline integration vertical;
these checks do not call a model, read a dataset, or perform a run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


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
    SKILL / "assets" / "QUESTION_RESULT_TEMPLATE.md",
)
OWNED_JSON = (
    SKILL / "assets" / "RUN_STATE_TEMPLATE.json",
    SKILL / "assets" / "TELEMETRY_EVENT_TEMPLATE.json",
    SKILL / "assets" / "ITEM_STATE_TEMPLATE.json",
)


def _read(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _owned_text() -> str:
    return "\n".join(_read(path) for path in OWNED_MARKDOWN + OWNED_JSON)


def test_frontmatter_and_run_markers_are_v022() -> None:
    skill = _read(SKILL / "SKILL.md")
    assert skill.startswith("---\n")
    frontmatter = skill.split("---\n", 2)[1]
    assert "name: auto-foundry-agentic-e2e" in frontmatter
    assert 'version: "0.2.2"' in frontmatter
    assert "core_name: auto_foundry_core" in frontmatter
    assert 'core_version: "0.2.0"' in frontmatter
    for marker in (
        "skill_name: auto-foundry-agentic-e2e",
        "skill_version: 0.2.2",
        "core_name: auto_foundry_core",
        "core_version: 0.2.0",
    ):
        assert marker in skill
    assert "Agent Workbench" in skill
    assert "Durable Execution" in skill


def test_run_state_template_is_workbench_authority() -> None:
    state = json.loads(_read(OWNED_JSON[0]))
    assert state["skill_name"] == "auto-foundry-agentic-e2e"
    assert state["skill_version"] == "0.2.2"
    assert state["core_name"] == "auto_foundry_core"
    assert state["core_version"] == "0.2.0"
    assert state["mode"] == "question|requirement"
    assert state["allowed_roots"]
    assert state["workbench"]["source_catalog"] == "data_room/source_catalog.json"
    assert state["workbench"]["catalog_kind"] == "zip_member_metadata"
    assert state["workbench"]["raw_archive_read_only"] is True
    assert state["workbench"]["item_workspace_pattern"] == "questions|requirements/<id>/work"
    assert state["queue"]["priority_rule"] == "explicit_user_priority_first"
    assert state["queue"]["parallel_question_wave"] is False
    assert state["execution"]["terminalizer_agent"] is False
    assert state["execution"]["wall_time_deadline"] is False
    assert state["telemetry"]["passive"] is True
    assert state["telemetry"]["records_raw_rows"] is False
    assert state["telemetry"]["controls_route"] is False
    assert state["optimizer"]["read_only"] is True
    assert state["optimizer"]["evidence_bundle"] == "optimizer/optimizer_evidence_bundle.md"
    assert state["optimizer"]["evidence_appendix"] == "optimizer/optimizer_evidence_appendix.md"
    assert state["optimizer"]["analytical_complete"] is True


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
    }
    assert state["item_id"] == "Q-001"
    assert state["mode"] == "question"
    assert state["original_text"]
    assert state["lifecycle_state"] == "work"
    assert state["execution_recovery_count"] == 0
    assert state["business_repair_count"] == 0
    assert isinstance(state["created_at"], str) and "T" in state["created_at"]
    assert isinstance(state["updated_at"], str) and "T" in state["updated_at"]


def test_telemetry_template_observes_attempts_and_artifacts_only() -> None:
    event = json.loads(_read(OWNED_JSON[1]))
    assert {"lane", "role", "route", "started_at", "ended_at"} <= set(
        event["invocation"]
    )
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
    assert event["passive"] is True
    assert event["records_raw_rows"] is False
    assert event["controls_route"] is False


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
        "second consecutive no-progress",
        "execution recovery",
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


def test_workbench_sequence_and_recovery_are_progressive_and_separate() -> None:
    skill = _read(SKILL / "SKILL.md")
    workflow = skill.split("For each item, use this progressive sequence:", 1)[1]
    sequence = (
        "program builds data room/source catalog",
        "program creates item_state.json and mutable work/",
        "Lead Analyst writes plan and source map first",
        "Lead Analyst appends findings, evidence, and loadable prepared assets",
        "Run Director checks artifact_progress after each response",
        "optional execution recovery from the durable handoff",
        "materialized draft",
        "one Independent Reviewer",
        "at most one targeted business repair",
        "atomic accepted snapshot and final outcome",
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
    assert "independent reviewer in a fresh context" in normalized
    assert "alternate independent route" in normalized
    assert "fresh same-family context" in normalized
    assert '"review_status":"unavailable"' in text
    assert '"review_strength":"none"' in text
    assert '"verdict":"not_reviewed"' in text
    assert "targeted source-completeness search" in normalized
    assert "identity-escalation route" in normalized
    assert "without repeating the full analysis" in normalized
    assert "accept_with_limits" in text
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
    assert "parallel question wave" in text
    assert "production application" in text  # explicit prohibition/boundary


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
