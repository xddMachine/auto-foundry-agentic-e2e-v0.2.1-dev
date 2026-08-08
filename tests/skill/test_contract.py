"""Offline contract checks for the install-ready skill tree.

These tests intentionally inspect text and templates only. They never import
the analytics core, call a model, read a dataset, or perform a run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "auto-foundry-agentic-e2e"


def _read(name: str) -> str:
    return (SKILL / name).read_text(encoding="utf-8")


def _all_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(SKILL.rglob("*"))
        if path.is_file() and path.suffix.lower() in {".md", ".json", ".css"}
    )


def test_frontmatter_and_run_markers_are_v021() -> None:
    skill = _read("SKILL.md")
    assert skill.startswith("---\n")
    frontmatter = skill.split("---\n", 2)[1]
    assert "name: auto-foundry-agentic-e2e" in frontmatter
    assert 'version: "0.2.1"' in frontmatter
    assert "core_name: auto_foundry_core" in frontmatter
    assert 'core_version: "0.1.0"' in frontmatter
    for marker in (
        "skill_name: auto-foundry-agentic-e2e",
        "skill_version: 0.2.1",
        "core_name: auto_foundry_core",
        "core_version: 0.1.0",
    ):
        assert marker in skill


def test_run_state_template_is_structured_authority() -> None:
    state = json.loads(_read("assets/RUN_STATE_TEMPLATE.json"))
    assert state["skill_name"] == "auto-foundry-agentic-e2e"
    assert state["skill_version"] == "0.2.1"
    assert state["core_name"] == "auto_foundry_core"
    assert state["core_version"] == "0.1.0"
    assert state["mode"] == "question|requirement"
    assert state["allowed_roots"]
    assert state["clean_room"]["empty_lem_at_start"] is True
    assert state["clean_room"]["empty_cache_at_start"] is True
    assert state["telemetry"]["passive"] is True
    assert state["optimizer"]["read_only"] is True
    assert state["optimizer"]["report"] == "optimizer/experimental_optimizer_report.md"
    assert state["optimizer"]["experimental_optimizer_evidence_appendix"] == (
        "optimizer/experimental_optimizer_evidence_appendix.md"
    )
    assert "evidence_appendix" not in state["optimizer"]


def test_requirement_record_template_keeps_user_fields_structured() -> None:
    record = json.loads(_read("assets/REQUIREMENT_RECORD_TEMPLATE.json"))
    for key in (
        "requirement_id",
        "original_text",
        "priority",
        "objective",
        "expected_outputs",
        "internal_tasks",
        "dependencies",
        "foundation_dependencies",
        "needs",
        "working_definitions",
        "limits",
        "scope_classification",
        "status",
    ):
        assert key in record
    assert set(record["needs"]) == {"data", "ontology", "prepared"}
    assert "analytics_requires_missing_data" in record["scope_classification"]
    assert "out_of_analytics_scope" in record["scope_classification"]
    assert record["review"]["repair_count"] == 0


def test_question_mode_preserves_queue_and_bounded_review() -> None:
    text = _read("SKILL.md") + _read("TEST_PROMPTS.md")
    normalized = " ".join(text.split())
    for phrase in (
        "preserve its original wording and order",
        "one question at a time",
        "one concise Lead Analyst self-check",
        "one routed Independent Reviewer",
        "at most one targeted repair",
        "Continue after `answered_with_limits`",
        "Build the final dashboard after the complete supplied queue",
    ):
        assert phrase.lower() in normalized.lower()
    assert "discover extra questions" in text


def test_requirement_mode_is_analytics_only_and_user_owned() -> None:
    text = _read("SKILL.md") + _read("TEST_PROMPTS.md") + _read(
        "assets/QUESTION_RESULT_TEMPLATE.md"
    )
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
        assert field in text
    for classification in (
        "analytics_in_scope",
        "analytics_requires_missing_data",
        "out_of_analytics_scope",
    ):
        assert classification in text
    normalized = " ".join(text.split())
    assert "Portfolio Planner is one semantic planning pass over the full portfolio" in normalized
    assert "Explicit user priority is honored first" in normalized
    assert "replan" in text
    assert "Requirement items execute one at a time" in normalized
    assert "not a user requirement" in normalized
    assert "keyword router" in text
    assert "fixed business-term dictionary" in text


def test_requirement_item_workflow_is_progressive_and_atomic() -> None:
    skill = _read("SKILL.md")
    workflow = skill.split("For each Requirement Mode item, use this exact, progressive sequence:", 1)[1]
    sequence = (
        "Navigator selects bounded IDs from compact indexes",
        "deterministic exact-ID bundle validation",
        "inspect Capability Catalog",
        "concise plan",
        "natural analysis",
        "optional specialists, core operations, or custom reproducible code",
        "one concise Lead Analyst self-check",
        "one routed Independent Reviewer",
        "at most one targeted repair",
        "final answer and outcome",
        "reviewed Knowledge Delta applied atomically by code",
    )
    positions = [workflow.index(part) for part in sequence]
    assert positions == sorted(positions)
    assert "no capability-by-capability approval tree" in skill
    assert "separate ontology closing role" in " ".join(skill.split())


def test_lem_separation_reuse_and_exact_ids() -> None:
    skill = _read("SKILL.md") + _read("references/KNOWLEDGE_AND_REUSE.md")
    for phrase in (
        "Enterprise Ontology",
        "Prepared Data Registry",
        "not a transaction copy",
        "reusable preparation",
        "requirement-scoped view",
        "effective periods",
        "conflicting definitions",
        "no_change",
        "compact indexes",
        "exact IDs",
        "deterministically",
    ):
        assert phrase in skill
    assert "both layers start empty" in skill.lower()


def test_catalog_core_policy_and_reproducible_custom_code() -> None:
    text = _read("SKILL.md") + _read("references/QUESTION_ANALYSIS_PLAYBOOK.md")
    assert "inspect the local Capability Catalog" in text
    assert "Use a catalog capability when it fits" in text
    assert "python -m auto_foundry_core catalog ..." in text
    assert "python -m auto_foundry_core run ..." in text
    assert "Custom" in text or "custom" in text
    assert "capability gap" in text
    assert "reproduction" in text


def test_reviewer_routing_fallback_and_disclosure() -> None:
    text = _read("SKILL.md") + _read("references/REVIEW_PROTOCOL.md")
    assert "independent reviewer in a fresh context" in text
    assert "alternate independent route" in text
    assert "fresh same-family context" in text
    assert '"review_status":"unavailable"' in text
    assert '"review_strength":"none"' in text
    assert "Release sessions" in text or "release reviewer sessions" in text
    assert not re.search(r"\b(?:gpt|claude|gemini|llama|sonnet|opus)[-_\d]", text, re.I)


def test_clean_room_dashboard_telemetry_and_optimizer_contract() -> None:
    text = _all_text()
    for phrase in (
        "empty run root",
        "empty LEM",
        "empty run cache",
        "allowed roots",
        "sibling runs",
        "previous-run caches",
        "discarded-lane incident",
        "not evidence of host-level sandboxing",
        "reviewed outputs",
        "new analytics",
        "offline local",
        "internal links",
        "traceability",
        "passive telemetry",
        "experimental_optimizer_report.md",
        "experimental_optimizer_evidence_appendix.md",
        "strictly read-only",
        "observed evidence",
        "hypothesis",
        "expected benefit",
        "Generality",
        "client business",
        "auto-promote",
    ):
        assert phrase.lower() in text.lower(), phrase
    assert (SKILL / "assets" / "DASHBOARD_PROTOTYPE_TEMPLATE.md").is_file()
    assert (SKILL / "assets" / "dashboard.css").is_file()
    assert (SKILL / "assets" / "TELEMETRY_EVENT_TEMPLATE.json").is_file()


def test_regression_prohibitions_have_no_legacy_or_domain_recipe() -> None:
    text = _all_text().lower()
    # Explicit negative guidance is allowed; deprecated operational recipes are not.
    assert "skill_version: 0.2." + "0" not in text
    assert "candidate-v" + "1" not in text
    assert "candidate-v" + "2" not in text
    assert "repair_v2" not in text
    assert "parallel " + "requirement" not in text
    assert "parallel " + "wave" not in text
    assert "ontology finalization gate" not in text
    assert "marketing funnel recipe" not in text
    assert "sap implementation" not in text
    assert not re.search(r"\bproduction app\b", text)
    assert "cross-run cache" in text  # the prohibition is explicit


def test_owned_markdown_links_and_json_templates_resolve() -> None:
    markdown_files = sorted(SKILL.rglob("*.md"))
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for path in markdown_files:
        for raw_target in link_pattern.findall(path.read_text(encoding="utf-8")):
            target = raw_target.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (path.parent / target).resolve()
            assert resolved.is_file(), f"broken link in {path}: {raw_target}"

    for path in sorted((SKILL / "assets").glob("*.json")):
        json.loads(path.read_text(encoding="utf-8"))
