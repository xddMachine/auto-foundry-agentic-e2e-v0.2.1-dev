from __future__ import annotations

from pathlib import Path

from auto_foundry_core import PlannerAction
import auto_foundry_core.coordinator as coordinator_module


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills" / "auto-foundry-agentic-e2e"


def test_analytical_owner_guidance_is_toolkit_first_and_adaptive() -> None:
    guidance = coordinator_module._role_guidance(
        PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    )
    lowered = guidance.lower()
    for method in ("profile_data", "compute_kpi_table", "segment_customers", "score_segments"):
        assert method in guidance
    assert "toolkit first" in lowered
    assert "custom owner-authored code only for methods the toolkit does not support" in lowered
    assert "genuinely independent uncertainty" in lowered
    assert "smallest useful set" in lowered
    assert "actual host capacity" in lowered
    assert "one specialist per method" in lowered


def test_integration_guidance_stages_exact_typed_artifact_via_public_api() -> None:
    guidance = coordinator_module._role_guidance(
        PlannerAction("integrate_requirement", "integration_agent", "REQ-001", "integrate")
    )
    lowered = guidance.lower()
    assert "business-accepted typed analyticalartifact" in lowered
    assert "sealed item-local state" in lowered
    assert "integrationsession.create/load" in lowered
    assert "do not manually re-submit" in lowered
    assert "do not invent a new integration method" in lowered
    assert "accepted_content_hash are immutable" in lowered
    assert "derived pre-commit projection" in lowered
    assert "correct_record for every authorized affected record" in lowered
    assert "remove_record when removal is authorized" in lowered
    assert "literal difference between normalized typed fields" in lowered


def test_skill_contract_routes_toolkit_and_has_no_fixed_specialist_caps() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    readme = (SKILL_ROOT / "README.md").read_text(encoding="utf-8")
    test_prompts = (SKILL_ROOT / "TEST_PROMPTS.md").read_text(encoding="utf-8")
    playbook = (SKILL_ROOT / "references" / "QUESTION_ANALYSIS_PLAYBOOK.md").read_text(encoding="utf-8")
    collaboration = (SKILL_ROOT / "references" / "ANALYTICAL_COLLABORATION.md").read_text(encoding="utf-8")
    knowledge_reuse = (SKILL_ROOT / "references" / "KNOWLEDGE_AND_REUSE.md").read_text(encoding="utf-8")
    artifact_policy = (SKILL_ROOT / "references" / "ARTIFACT_AND_EFFICIENCY_POLICY.md").read_text(encoding="utf-8")
    toolkit = (SKILL_ROOT / "references" / "ANALYTICS_TOOLKIT.md").read_text(encoding="utf-8")

    assert "references/ANALYTICS_TOOLKIT.md" in skill
    assert "IntegrationSession.create/load" in skill
    assert "ANALYTICS_TOOLKIT.md" in playbook
    assert "ANALYTICS_TOOLKIT.md" in collaboration
    assert "IntegrationSession.create/load" in collaboration
    assert "AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT" in toolkit
    assert "AnalyticalArtifact.to_json()" in toolkit
    assert "IntegrationSession.create/load" in toolkit
    for document in (skill, readme, test_prompts, playbook, collaboration, knowledge_reuse, artifact_policy):
        lowered = " ".join(document.lower().split())
        assert "smallest useful set" in lowered
        assert "actual host capacity" in lowered
        assert "exactly one analytical owner" in lowered
        assert "zero to three" not in lowered
        assert "0-3" not in lowered
        assert "up to three" not in lowered
        assert "eight active workers" not in lowered
