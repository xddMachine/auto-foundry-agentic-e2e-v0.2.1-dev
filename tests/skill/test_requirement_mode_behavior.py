"""Behavioral, offline coverage for the settled Requirement Mode protocol."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_requirement_mode import (  # noqa: E402
    CodeFeedbackError,
    FakeRequirement,
    FakeRequirementRun,
    InvocationReceipt,
    PreparedAsset,
    ResultIntegrationAgent,
    decide_recovery,
)


def _requirement() -> FakeRequirement:
    return FakeRequirement("R-001", "Review a bounded fixture", 1, "analytics_in_scope")


def test_same_attempt_code_feedback_uses_controlled_receipts_without_recovery() -> None:
    run = FakeRequirementRun()
    result = run.run(_requirement(), script_sources=("syntax_error", "valid"))

    assert result.code_feedback_count == 1
    assert result.script_receipt.attempt_id == "R-001:attempt-1"
    assert result.script_receipt.succeeded
    assert [receipt.phase for receipt in result.script_receipt.receipts] == [
        "smoke",
        "full",
    ]
    assert all(receipt.status == "passed" for receipt in result.script_receipt.receipts)
    assert run.lead_attempts == ["R-001:attempt-1"]
    assert result.recovery.action == "await_runtime"
    assert result.business_repair_count == 0
    assert result.acceptance_envelope.terminal_reason_class == "same_attempt_feedback"
    assert run.integration_agent.calls == 1
    assert result.acceptance_envelope.snapshot_hash == result.accepted_snapshot.content_hash


def test_controlled_runner_emits_only_failure_preflight_or_runtime_phases() -> None:
    run = FakeRequirementRun()
    failed_preflight = run.runner.run("syntax_error", attempt_id="R-001:attempt-1")
    assert failed_preflight.status == "failed"
    assert [receipt.phase for receipt in failed_preflight.receipts] == ["compile"]

    missing_import = run.runner.run("missing_import", attempt_id="R-001:attempt-1")
    assert missing_import.status == "failed"
    assert [receipt.phase for receipt in missing_import.receipts] == ["dependency_check"]

    deterministic = run.runner.run(
        "valid",
        attempt_id="R-001:attempt-1",
        deterministic=True,
    )
    assert [receipt.phase for receipt in deterministic.receipts] == [
        "smoke",
        "full",
        "full",
    ]
    assert deterministic.deterministic_match is True


def test_filesystem_no_progress_guides_materialization_but_completed_loss_allows_recovery() -> None:
    no_receipt = decide_recovery(invocation_receipt=None, materialized=False)
    assert no_receipt.action == "materialization_guidance"
    assert no_receipt.reason_class == "await_runtime"

    loss = InvocationReceipt("R-001:attempt-1", "lost")
    recovered = decide_recovery(invocation_receipt=loss, materialized=False)
    assert recovered.action == "execution_recovery"
    assert recovered.reason_class == "execution_recovery"
    assert loss.provider == "unavailable"
    assert loss.model == "unavailable"


def test_one_reviewer_one_business_repair_then_re_review() -> None:
    run = FakeRequirementRun(reviewer_verdicts=("repair_once", "accept_with_limits"))
    result = run.run(_requirement())

    assert run.reviewer_calls == 2
    assert result.business_repair_count == 1
    assert result.review.verdict == "accept_with_limits"
    assert result.acceptance_envelope.terminal_reason_class == "business_repair"
    assert result.review.targeted_recheck_scope == ("/answer/findings/0/value",)
    assert result.review.finding_ids == ()
    assert run.business_repair_count == 1


def test_business_repair_finding_is_pointer_scoped_and_fidelity_review_is_item_only() -> None:
    run = FakeRequirementRun(reviewer_verdicts=("repair_once", "accept"))
    result = run.run(_requirement())

    assert run.integration_fidelity_reviewer_calls == 1
    assert result.integration_fidelity.mechanical_validation_complete is True
    assert result.integration_fidelity.reviewer_calls == 1
    assert result.integration_fidelity.targeted_repair_count == 0
    assert result.integration_fidelity.excluded_context == (
        "siblings",
        "cumulative",
        "prior_memory",
        "broad_workspace",
    )


def test_result_integration_agent_is_single_owner_and_catalog_scope_is_visibility_only() -> None:
    agent = ResultIntegrationAgent()
    asset = PreparedAsset("asset-1", "b" * 64, "0.3.3", "fixture-v1", "source")
    receipt = agent.integrate(
        claims=("claim",),
        metrics=("metric",),
        limitations=("limit",),
        evidence_refs=("evidence",),
        prepared_assets=(asset,),
        ontology_refs=("ontology",),
        relationship_refs=("relationship",),
        dashboard_facts=("dashboard",),
    )
    assert agent.calls == 1
    assert receipt.prepared_asset_ids == ("asset-1",)
    assert agent.catalog.visible(scope="source") == (asset,)
    assert agent.catalog.visible(scope="other") == ()

    # The canonical identity is immutable by source hash/core/schema; a
    # different scope is a visibility decision, not a second canonical row.
    with pytest.raises(ValueError, match="immutable"):
        agent.catalog.register(PreparedAsset("asset-2", asset.source_hash, asset.core_version, asset.schema, "requirement"))


def test_failed_same_attempt_feedback_does_not_silently_recover() -> None:
    run = FakeRequirementRun()
    with pytest.raises(CodeFeedbackError):
        run.run(_requirement(), script_sources=("type_error", "invalid"))
    assert run.lead_attempts == ["R-001:attempt-1"]
