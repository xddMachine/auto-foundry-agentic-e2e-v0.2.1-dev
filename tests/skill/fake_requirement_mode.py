"""Deterministic protocol harness for offline Requirement Mode coverage.

The harness models the ownership boundaries that are easy to regress without
calling a model, reading a source, or pretending that prose is lifecycle
state. One Lead Analyst owns the item, a controlled runner feeds code errors
back to that same attempt, one Independent Business Reviewer may request one
scoped business repair and targeted recheck, and exactly one Result Integration
Agent plus one item-only Integration Fidelity Reviewer consume accepted bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Iterable, Mapping, Sequence


class CodeFeedbackError(ValueError):
    """A compile/dependency/script validation defect for the current attempt."""


class IntegrationValidationError(ValueError):
    """A deterministic integration boundary rejected a structured value."""


@dataclass(frozen=True)
class FakeRequirement:
    requirement_id: str
    original_text: str
    priority: int | None
    scope_classification: str
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class InvocationReceipt:
    """A completed invocation receipt, including literal unavailable facts."""

    attempt_id: str
    status: str
    lane: str = "lead"
    provider: str = "unavailable"
    model: str = "unavailable"
    host: str = "unavailable"
    process: str = "unavailable"
    completed: bool = True

    @property
    def proves_execution_loss(self) -> bool:
        """Only a completed receipt explicitly marked lost authorizes recovery."""

        return self.completed and self.status == "lost"


@dataclass(frozen=True)
class ScriptExecutionReceipt:
    """One emitted preflight-failure or runtime execution receipt."""

    attempt_id: str
    phase: str
    status: str


@dataclass(frozen=True)
class ScriptRunReport:
    """Pipeline result: checks are separate from emitted runtime receipts."""

    attempt_id: str
    status: str
    receipts: tuple[ScriptExecutionReceipt, ...]
    deterministic_match: bool | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "passed"


class ControlledScriptRunner:
    """Model the real pipeline's preflight checks and runtime receipt phases."""

    runtime_phases = ("smoke", "full")
    preflight_failure_phases = {
        "syntax_error": "compile",
        "missing_import": "dependency_check",
    }

    def run(
        self,
        source: str,
        *,
        attempt_id: str,
        deterministic: bool = False,
    ) -> ScriptRunReport:
        if source in self.preflight_failure_phases:
            # Compile/dependency preflight emits a receipt only when it fails;
            # successful preflight has no separate receipt.
            return ScriptRunReport(
                attempt_id,
                "failed",
                (
                    ScriptExecutionReceipt(
                        attempt_id,
                        self.preflight_failure_phases[source],
                        "failed",
                    ),
                ),
            )
        if source in {"name_error", "type_error", "invalid"}:
            # Runtime coding/script-validation errors are reported by the
            # smoke pass and routed back to the same attempt.
            return ScriptRunReport(
                attempt_id,
                "failed",
                (ScriptExecutionReceipt(attempt_id, "smoke", "failed"),),
            )
        phases = [
            ScriptExecutionReceipt(attempt_id, phase, "passed")
            for phase in self.runtime_phases
        ]
        if deterministic:
            # The deterministic comparison is a second full runtime pass, not
            # a distinct receipt phase.
            phases.append(ScriptExecutionReceipt(attempt_id, "full", "passed"))
        return ScriptRunReport(
            attempt_id,
            "passed",
            tuple(phases),
            deterministic_match=True if deterministic else None,
        )


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    reason_class: str


def decide_recovery(*, invocation_receipt: InvocationReceipt | None, materialized: bool) -> RecoveryDecision:
    """Classify a stalled lane without turning filesystem silence into recovery."""

    if invocation_receipt is not None and invocation_receipt.proves_execution_loss:
        return RecoveryDecision("execution_recovery", "execution_recovery")
    if materialized:
        return RecoveryDecision("await_runtime", "await_runtime")
    return RecoveryDecision("materialization_guidance", "await_runtime")


@dataclass(frozen=True)
class ReviewResult:
    review_status: str
    review_strength: str
    verdict: str
    finding_ids: tuple[str, ...] = ()
    affected_paths: tuple[str, ...] = ()
    dependent_outputs: tuple[str, ...] = ()
    reviewed_draft_hash: str | None = None
    targeted_recheck_scope: tuple[str, ...] = ()


@dataclass(frozen=True)
class BusinessFinding:
    """One stable, pointer-scoped business-review finding."""

    finding_id: str
    pointer: str
    dependent_outputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegrationFidelityResult:
    """Exactly one fresh item-only fidelity review before commit."""

    item_id: str
    mechanical_validation_complete: bool
    reviewer_calls: int = 1
    targeted_repair_count: int = 0
    targeted_recheck: bool = False
    excluded_context: tuple[str, ...] = (
        "siblings",
        "cumulative",
        "prior_memory",
        "broad_workspace",
    )


@dataclass(frozen=True)
class AcceptedSnapshot:
    """Immutable accepted answer bytes; lifecycle state is not embedded."""

    answer_bytes: bytes
    content_hash: str

    @classmethod
    def from_text(cls, text: str) -> "AcceptedSnapshot":
        payload = text.encode("utf-8")
        return cls(payload, hashlib.sha256(payload).hexdigest())


@dataclass(frozen=True)
class AcceptanceEnvelope:
    """Program-owned lifecycle envelope bound to an accepted snapshot hash."""

    snapshot_hash: str
    lifecycle_state: str = "accepted"
    terminal_reason_class: str = "business_repair"


@dataclass(frozen=True)
class PreparedAsset:
    asset_id: str
    source_hash: str
    core_version: str
    schema: str
    scope: str
    reusable: bool = True


class CanonicalCatalog:
    """Immutable canonical asset identities with derived visibility views."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], PreparedAsset] = {}

    def register(self, asset: PreparedAsset) -> PreparedAsset:
        key = (asset.source_hash, asset.core_version, asset.schema)
        existing = self._entries.get(key)
        if existing is not None and existing != asset:
            raise IntegrationValidationError("canonical catalog identity is immutable")
        self._entries.setdefault(key, asset)
        return self._entries[key]

    def visible(self, *, scope: str) -> tuple[PreparedAsset, ...]:
        return tuple(asset for asset in self._entries.values() if asset.scope == scope)


@dataclass(frozen=True)
class IntegrationReceipt:
    claims: tuple[str, ...]
    metrics: tuple[str, ...]
    limitations: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    prepared_asset_ids: tuple[str, ...]
    ontology_refs: tuple[str, ...]
    relationship_refs: tuple[str, ...]
    dashboard_facts: tuple[str, ...]


class ResultIntegrationAgent:
    """The single post-acceptance owner; validation remains deterministic."""

    def __init__(self, catalog: CanonicalCatalog | None = None) -> None:
        self.catalog = catalog or CanonicalCatalog()
        self.calls = 0

    def integrate(
        self,
        *,
        claims: Iterable[str],
        metrics: Iterable[str],
        limitations: Iterable[str],
        evidence_refs: Iterable[str],
        prepared_assets: Iterable[PreparedAsset],
        ontology_refs: Iterable[str],
        relationship_refs: Iterable[str],
        dashboard_facts: Iterable[str],
    ) -> IntegrationReceipt:
        self.calls += 1
        values = {
            "claims": tuple(claims),
            "metrics": tuple(metrics),
            "limitations": tuple(limitations),
            "evidence_refs": tuple(evidence_refs),
            "ontology_refs": tuple(ontology_refs),
            "relationship_refs": tuple(relationship_refs),
            "dashboard_facts": tuple(dashboard_facts),
        }
        if any(not isinstance(value, str) or not value.strip() for group in values.values() for value in group):
            raise IntegrationValidationError("integration values require non-empty strings")
        registered = tuple(self.catalog.register(asset).asset_id for asset in prepared_assets)
        return IntegrationReceipt(**values, prepared_asset_ids=registered)


@dataclass(frozen=True)
class RequirementResult:
    requirement_id: str
    script_receipt: ScriptRunReport
    code_feedback_count: int
    review: ReviewResult
    business_repair_count: int
    recovery: RecoveryDecision
    accepted_snapshot: AcceptedSnapshot
    acceptance_envelope: AcceptanceEnvelope
    integration: IntegrationReceipt
    integration_fidelity: IntegrationFidelityResult


class FakeRequirementRun:
    """Run one requirement through the settled ownership protocol."""

    def __init__(self, *, reviewer_verdicts: Sequence[str] = ("accept",)) -> None:
        self.runner = ControlledScriptRunner()
        self.reviewer_verdicts = tuple(reviewer_verdicts)
        self.lead_attempts: list[str] = []
        self.reviewer_calls = 0
        self.business_repair_count = 0
        self.integration_fidelity_reviewer_calls = 0
        self.integration_agent = ResultIntegrationAgent()

    def run(
        self,
        requirement: FakeRequirement,
        *,
        script_sources: Sequence[str] = ("valid",),
        invocation_receipt: InvocationReceipt | None = None,
    ) -> RequirementResult:
        attempt_id = f"{requirement.requirement_id}:attempt-1"
        self.lead_attempts.append(attempt_id)
        script_receipt: ScriptRunReport | None = None
        code_feedback_count = 0
        for source in script_sources:
            report = self.runner.run(source, attempt_id=attempt_id)
            if report.succeeded:
                script_receipt = report
                break
            code_feedback_count += 1
        if script_receipt is None:
            raise CodeFeedbackError("same-attempt code feedback exhausted")

        recovery = decide_recovery(invocation_receipt=invocation_receipt, materialized=True)
        self.reviewer_calls += 1
        verdict = self.reviewer_verdicts[0] if self.reviewer_verdicts else "accept"
        draft_hash = hashlib.sha256(
            f"draft:{requirement.requirement_id}".encode("utf-8")
        ).hexdigest()
        findings: tuple[BusinessFinding, ...] = ()
        review = ReviewResult(
            "available",
            "independent",
            verdict,
            reviewed_draft_hash=draft_hash,
        )
        if verdict == "repair_once":
            self.business_repair_count += 1
            if len(self.reviewer_verdicts) < 2:
                raise ValueError("repair_once requires one re-review verdict")
            findings = (
                BusinessFinding(
                    "finding-1",
                    "/answer/findings/0/value",
                    (f"claim:{requirement.requirement_id}",),
                ),
            )
            review = ReviewResult(
                "available",
                "independent",
                verdict,
                finding_ids=tuple(item.finding_id for item in findings),
                affected_paths=tuple(item.pointer for item in findings),
                dependent_outputs=tuple(
                    output
                    for item in findings
                    for output in item.dependent_outputs
                ),
                reviewed_draft_hash=draft_hash,
            )
            self.reviewer_calls += 1
            review = ReviewResult(
                "available",
                "independent",
                self.reviewer_verdicts[1],
                finding_ids=(),
                affected_paths=(),
                dependent_outputs=(),
                reviewed_draft_hash=draft_hash,
                targeted_recheck_scope=tuple(item.pointer for item in findings),
            )
        if self.business_repair_count > 1:
            raise ValueError("at most one business repair is allowed")

        snapshot = AcceptedSnapshot.from_text(f"accepted answer for {requirement.requirement_id}")
        envelope = AcceptanceEnvelope(snapshot.content_hash, terminal_reason_class=("business_repair" if self.business_repair_count else "same_attempt_feedback"))
        integration = self.integration_agent.integrate(
            claims=(f"claim:{requirement.requirement_id}",),
            metrics=("metric:bounded",),
            limitations=("fixture-only",),
            evidence_refs=(f"evidence:{requirement.requirement_id}",),
            prepared_assets=(PreparedAsset("asset-1", "a" * 64, "0.3.1", "fixture-v1", "source"),),
            ontology_refs=("ontology:fixture",),
            relationship_refs=("relationship:fixture",),
            dashboard_facts=("dashboard:fixture",),
        )
        self.integration_fidelity_reviewer_calls += 1
        fidelity = IntegrationFidelityResult(
            requirement.requirement_id,
            mechanical_validation_complete=True,
            reviewer_calls=self.integration_fidelity_reviewer_calls,
            targeted_repair_count=0,
            targeted_recheck=False,
        )
        return RequirementResult(
            requirement.requirement_id,
            script_receipt,
            code_feedback_count,
            review,
            self.business_repair_count,
            recovery,
            snapshot,
            envelope,
            integration,
            fidelity,
        )
