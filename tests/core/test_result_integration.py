"""Offline mechanical tests for accepted-result integration."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from auto_foundry_core import (
    AnalystWorkspace,
    BoundAnalysisContext,
    CurrentObservationFact,
    DataAssetRef,
    DataInsufficiencyConclusion,
    DataRoomWorkbench,
    observation_as_of,
)
from auto_foundry_core.analyst_workspace import _canonical_hash as _semantic_selection_journal_hash
from auto_foundry_core.contracts import CanonicalMapping, IdentityDecision, OntologyItem, PreparedAssetDescriptor
from auto_foundry_core.durable import ItemWorkspace, _json_bytes, _manifest_hash
from auto_foundry_core.enterprise_model import LivingEnterpriseModel
from auto_foundry_core.integration import (
    AcceptedAnalysisBundle,
    IntegrationRecord,
    IntegrationSession,
    _sha256_value,
)
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.semantic_store import SemanticSnapshotStore
from auto_foundry_core.workspace import RunContext


def _accepted(
    context: RunContext,
    item_id: str,
    value: object | None = None,
    analytical_relationships: tuple[dict[str, object], ...] = (),
) -> ItemWorkspace:
    try:
        RunLifecycle.load(context)
    except FileNotFoundError:
        RunLifecycle.create(context, ("Q-001", "Q-002", "Q-REJECTED", "Q-FAILED"))
    workspace = ItemWorkspace.create(context, item_id, original_text=f"question {item_id}")
    workspace.write_plan({"item_id": item_id, "offline": True})
    if analytical_relationships:
        (workspace.work_root / "analytical_relationships.jsonl").write_text(
            "".join(json.dumps(value, sort_keys=True) + "\n" for value in analytical_relationships),
            encoding="utf-8",
        )
    workspace.write_draft(value if value is not None else {"answer": item_id, "opaque": [1, 2]})
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    refs = ("work/plan.json", "work/analytical_relationships.jsonl") if analytical_relationships else ("work/plan.json",)
    workspace.accept(accepted_refs=refs)
    return workspace


def _semantic_snapshot_and_selection(
    context: RunContext,
    relationship_ids: tuple[str, ...] = ("REL-PRIOR",),
):
    """Publish one tiny immutable semantic context for integration fixtures."""

    snapshot_ref = SemanticSnapshotStore.publish(
        context,
        {
            "ontology": (),
            "relationships": [{"relationship_id": value} for value in relationship_ids],
            "identity_decisions": (),
            "canonical_mappings": (),
            "prepared_assets": (),
        },
    )
    selection_ref = SemanticSnapshotStore.publish_selection(
        context,
        snapshot_ref,
        {
            "ontology_ids": (),
            "relationship_ids": relationship_ids,
            "identity_decision_ids": (),
            "mapping_ids": (),
            "prepared_asset_ids": (),
        },
    )
    return snapshot_ref, selection_ref


def _accepted_with_semantic_selection(
    context: RunContext,
    item_id: str,
    analytical_relationships: tuple[dict[str, object], ...],
    snapshot_ref: object,
    selection_ref: object,
    *,
    journal_item_id: str | None = None,
    journal_snapshot_hash: str | None = None,
    journal_selection_ref: str | None = None,
    historical_records: tuple[dict[str, object], ...] = (),
    duplicate_current: bool = False,
    malformed_journal: bool = False,
    journal_selection_id_override: str | None = None,
    tamper_journal_purpose: bool = False,
) -> ItemWorkspace:
    """Create an accepted item whose semantic context is hash-bound locally."""

    try:
        RunLifecycle.load(context)
    except FileNotFoundError:
        RunLifecycle.create(context, ("Q-001", "Q-002", "Q-REJECTED", "Q-FAILED"))
    workspace = ItemWorkspace.create(context, item_id, original_text=f"question {item_id}")
    workspace.write_plan({"item_id": item_id, "offline": True})
    (workspace.work_root / "analytical_relationships.jsonl").write_text(
        "".join(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n" for value in analytical_relationships),
        encoding="utf-8",
    )
    context_path = workspace.work_root / "analysis_context.json"
    context_unsigned = {
        "schema_version": "3",
        "kind": "bound_analysis_context",
        "run_id": context.run_id,
        "run_root": str(context.run_root),
        "item_id": item_id,
        "item_mode": workspace.mode,
        "manifest_path": str(context_path),
        "semantic_snapshot": snapshot_ref.to_dict(),
    }
    context_manifest_hash = hashlib.sha256(
        json.dumps(context_unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    context_path.write_text(
        json.dumps(
            {**context_unsigned, "manifest_hash": context_manifest_hash},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    selection_record = {
        "record_kind": "semantic_selection",
        "item_id": journal_item_id or item_id,
        "owner_ref": "analytical-owner-" + item_id,
        "selection_kind": "semantic_scope",
        "selection_ref": journal_selection_ref or selection_ref.selection_ref,
        "selection_hash": selection_ref.selection_hash,
        "selection_counts": dict(selection_ref.counts),
        "purpose": "REQ-12-shaped semantic relationship reuse",
        "snapshot_hash": journal_snapshot_hash or snapshot_ref.snapshot_hash,
        "context_manifest_hash": context_manifest_hash,
        "registry_hash": None,
    }
    selection_record["selection_id"] = _semantic_selection_journal_hash(selection_record)
    if journal_selection_id_override is not None:
        selection_record["selection_id"] = journal_selection_id_override
    if tamper_journal_purpose:
        selection_record["purpose"] = "tampered after selection ID"
    journal_records = [*historical_records, selection_record]
    if duplicate_current:
        journal_records.append(dict(selection_record))
    journal_bytes = b"".join(_json_bytes(record) for record in journal_records)
    if malformed_journal:
        journal_bytes += b"{malformed semantic selection\n"
    (workspace.work_root / "semantic_selections.jsonl").write_bytes(journal_bytes)
    workspace.write_draft({"answer": item_id, "opaque": [1, 2]})
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    workspace.accept(
        accepted_refs=(
            "work/plan.json",
            "work/analytical_relationships.jsonl",
            "work/analysis_context.json",
            "work/semantic_selections.jsonl",
        )
    )
    return workspace


def _asset(
    context: RunContext,
    asset_id: str,
    *,
    scope: str = "reusable",
    workspace: ItemWorkspace | None = None,
) -> PreparedAssetDescriptor:
    if workspace is None:
        # Existing tests create exactly one accepted question before creating a
        # candidate.  Keeping this fallback makes the helper convenient while
        # the public workbench candidate API is exercised by integration tests.
        question_roots = sorted((context.run_root / "questions").glob("*/work"))
        if not question_roots:
            raise AssertionError("candidate helper requires an accepted item workspace")
        path = question_roots[0] / "prepared" / f"{asset_id}.jsonl"
    else:
        path = workspace.work_root / "prepared" / f"{asset_id}.jsonl"
    payload = b'{"asset": "' + asset_id.encode("utf-8") + b'"}\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return PreparedAssetDescriptor(
        prepared_asset_id=asset_id,
        source_refs=(f"synthetic:{asset_id}",),
        source_hashes=("1" * 64,),
        location=str(path),
        schema={"asset": "string"},
        scope=scope,
        prepared_content_hash=hashlib.sha256(payload).hexdigest(),
        operation_manifest_hash="2" * 64,
        core_version="test",
        row_count=1,
        byte_count=len(payload),
        metadata={"format": "jsonl"},
    )


def _session(
    context: RunContext,
    item_id: str = "Q-001",
    value: object | None = None,
    analytical_relationships: tuple[dict[str, object], ...] = (),
):
    workspace = _accepted(context, item_id, value, analytical_relationships)
    registry = PreparedAssetRegistry(context)
    session = IntegrationSession.create(context, workspace, registry, "integration-owner", invocation_id=f"inv-{item_id}")
    return workspace, session.lem, registry, session


def _commit(session: IntegrationSession):
    """Persist the required item-local fidelity acceptance before commit."""

    if session.fidelity_result is None:
        session.record_fidelity_review(
            "accept",
            checked_record_ids=tuple(record.record_id for record in session.records),
        )
    return session.commit()


def _analytical_relationship(
    relationship_id: str,
    source_id: str,
    target_id: str,
    *,
    cardinality: str = "one_to_one",
) -> dict[str, object]:
    return {
        "record_kind": "analytical_relationship",
        "relationship_id": relationship_id,
        "source_id": source_id,
        "target_id": target_id,
        "cardinality": cardinality,
        "join_keys": [{"source_field": "id", "target_field": "id"}],
        "matched_pairs": 1,
        "source_population": 1,
        "target_population": 1,
        "matched_source_count": 1,
        "matched_target_count": 1,
        "source_coverage": 1.0,
        "target_coverage": 1.0,
        "date_authority": "fixture-controlled snapshot",
        "as_of": None,
        "limitations": ["synthetic fixture"],
        "evidence_refs": ["work/plan.json"],
        "publishable": True,
        "no_relationship_reason": None,
        "audit_id": None,
    }


def _relationship_audit(relationship_id: str, source_id: str, target_id: str) -> dict[str, object]:
    return {
        "record_kind": "relationship_audit",
        "relationship_id": relationship_id,
        "source_id": source_id,
        "target_id": target_id,
        "cardinality": "none",
        "join_keys": [],
        "matched_pairs": None,
        "source_population": None,
        "target_population": None,
        "matched_source_count": None,
        "matched_target_count": None,
        "source_coverage": None,
        "target_coverage": None,
        "date_authority": None,
        "as_of": None,
        "limitations": [],
        "evidence_refs": ["work/plan.json"],
        "publishable": False,
        "no_relationship_reason": "No reviewed key was found.",
        "audit_id": f"{relationship_id}-audit",
    }


def _staged_relationship(artifact: dict[str, object], *, relationship_id: str | None = None) -> dict[str, object]:
    payload = {
        field: artifact[field]
        for field in (
            "source_id",
            "target_id",
            "cardinality",
            "join_keys",
            "matched_pairs",
            "source_population",
            "target_population",
            "matched_source_count",
            "matched_target_count",
            "source_coverage",
            "target_coverage",
            "date_authority",
            "as_of",
            "limitations",
            "evidence_refs",
        )
    }
    payload["relationship_id"] = relationship_id or str(artifact["relationship_id"])
    payload["analysis_relationship_id"] = artifact["relationship_id"]
    for field in ("owner_ref", "audit_id"):
        if field in artifact:
            payload[field] = artifact[field]
    return payload


def test_actual_cardinality_relationships_validate_fidelity_and_commit(tmp_path: Path) -> None:
    """Actual one-to-many and partial-linkage populations survive Integration."""

    context = RunContext("RUN-ACTUAL-CARDINALITIES", tmp_path / "run")
    orders_items = _analytical_relationship("ecom-orders-to-items", "orders", "items", cardinality="one_to_many")
    orders_items.update(
        {
            "matched_pairs": 7668,
            "source_population": 3584,
            "target_population": 7668,
            "matched_source_count": 3584,
            "matched_target_count": 7668,
            "source_coverage": 1.0,
            "target_coverage": 1.0,
        }
    )
    items_orders = _analytical_relationship("ecom-items-to-orders", "items", "orders", cardinality="many_to_one")
    items_orders.update(
        {
            "matched_pairs": 7668,
            "source_population": 7668,
            "target_population": 3584,
            "matched_source_count": 7668,
            "matched_target_count": 3584,
            "source_coverage": 1.0,
            "target_coverage": 1.0,
        }
    )
    billing = _analytical_relationship("billing-partial", "billing-lines", "accounts", cardinality="many_to_one")
    billing.update(
        {
            "matched_pairs": 4111,
            "source_population": 4119,
            "target_population": 5000,
            "matched_source_count": 4111,
            "matched_target_count": 3000,
            "source_coverage": 4111 / 4119,
            "target_coverage": 0.6,
        }
    )
    workspace, _lem, _registry, session = _session(
        context,
        analytical_relationships=(orders_items, items_orders, billing),
    )
    for item_id in ("orders", "items", "billing-lines", "accounts"):
        session.add_ontology_item(
            {"item_id": item_id, "item_type": "entity", "label": item_id.title()},
            scope="question",
            evidence_refs=("work/plan.json",),
        )
    for artifact in (orders_items, items_orders, billing):
        session.add_relationship(
            _staged_relationship(artifact),
            scope="question",
            evidence_refs=("work/analytical_relationships.jsonl", "work/plan.json"),
        )
    before_invalid = session.records
    invalid = _staged_relationship(orders_items, relationship_id="invalid-cardinality")
    invalid["matched_source_count"] = 3585
    with pytest.raises(ValueError):
        session.add_relationship(
            invalid,
            scope="question",
            evidence_refs=("work/analytical_relationships.jsonl", "work/plan.json"),
        )
    assert session.records == before_invalid
    validation = session.validate()
    assert validation.valid, validation.errors
    packet = session.build_fidelity_packet()
    session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in session.records))
    manifest = session.commit()
    assert manifest["item_id"] == workspace.item_id
    assert packet.accepted_content_hash


def test_public_analyst_relationship_output_binds_integration(tmp_path: Path) -> None:
    """The public AO evidence writer is the source for a staged join payload."""

    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", "order_id,customer_id\nO-1,C-1\n")
        output.writestr("customers.csv", "customer_id\nC-1\n")

    context = RunContext("RUN-AO-INTEGRATION", tmp_path / "run", (input_root,))
    RunLifecycle.create(context, ("Q-001",))
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    workspace = ItemWorkspace.create(context, "Q-001", original_text="Join the supplied fixtures.")
    bound = BoundAnalysisContext.create(
        context,
        DataAssetRef.from_path(archive),
        workspace,
        workbench=workbench,
    )
    analyst = AnalystWorkspace(bound, owner_ref="owner-Q-001")
    analyst.begin_analysis(
        objective="Test the reviewed customer join.",
        strategy="Use the explicit customer_id equality observed in both fixtures.",
        expected_outputs=("relationship evidence",),
    )
    analyst.record_analytical_relationship(
        relationship_id="orders-customers",
        source_id="orders",
        target_id="customers",
        cardinality="many_to_one",
        join_keys=({"source_field": "customer_id", "target_field": "customer_id"},),
        matched_pairs=1,
        source_population=1,
        target_population=1,
        matched_source_count=1,
        matched_target_count=1,
        source_coverage=1.0,
        target_coverage=1.0,
        date_authority="fixture-controlled snapshot",
        as_of=None,
        limitations=("Synthetic fixture only",),
        evidence_refs=("work/plan.json",),
        publishable=True,
    )
    analyst.submit_answer("The tested join is publishable.")
    workspace.record_review("accept", reviewer_ref="reviewer-Q-001")
    analyst.accept(accepted_refs=("work/plan.json", "work/analytical_relationships.jsonl"))

    # Read the canonical post-cleanup AO artifact rather than reconstructing
    # relationship fields from the answer prose or from a compatibility alias.
    artifact_path = workspace.work_root / "analytical_relationships.jsonl"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8").splitlines()[0])
    registry = PreparedAssetRegistry(context)
    session = IntegrationSession.create(
        context,
        workspace,
        registry,
        "integration-owner",
        invocation_id="inv-Q-001",
    )
    session.add_ontology_item(
        OntologyItem(item_id=artifact["source_id"], item_type="entity", label="Orders", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    session.add_ontology_item(
        OntologyItem(item_id=artifact["target_id"], item_type="entity", label="Customers", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    session.add_relationship(
        _staged_relationship(artifact),
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )

    validation = session.validate()
    assert validation.valid
    packet = session.build_fidelity_packet()
    assert sum(record["kind"] == "relationship" for record in packet.records) == 1
    session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    session.commit()
    assert workspace.integration_state == "integrated"


def test_relationship_item_id_mismatch_is_rejected_before_staging(tmp_path: Path) -> None:
    context = RunContext("RUN-RELATIONSHIP-ITEM-ID", tmp_path / "run")
    artifact = _analytical_relationship("orders-customers-analysis", "orders", "customers")
    _workspace, _lem, _registry, session = _session(context, analytical_relationships=(artifact,))
    payload = _staged_relationship(artifact)
    payload["item_id"] = "REQ-03"
    before_records = session.records
    before_state = session.state

    with pytest.raises(ValueError, match="item_id must match relationship_id"):
        session.add_relationship(
            payload,
            scope="question",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    assert session.records == before_records
    assert session.state == before_state


def test_invalid_pre_fidelity_relationships_can_be_corrected_before_packet(tmp_path: Path) -> None:
    context = RunContext("RUN-PRE-FIDELITY-CORRECTION", tmp_path / "run")
    first = _analytical_relationship("orders-customers-analysis", "orders", "customers")
    second = _analytical_relationship("orders-shipments-analysis", "orders", "shipments")
    _workspace, _lem, _registry, session = _session(
        context,
        analytical_relationships=(first, second),
    )
    for item_id in ("orders", "customers", "shipments"):
        session.add_ontology_item(
            OntologyItem(item_id=item_id, item_type="entity", label=item_id.title(), scope="question"),
            evidence_refs=("work/plan.json",),
        )

    # Reproduce a legacy staged payload that predates the relationship/item
    # identity guard.  The private staging primitive is used only to seed the
    # invalid state; correction itself remains on the public API.
    for artifact in (first, second):
        invalid = _staged_relationship(artifact)
        invalid["item_id"] = "REQ-03"
        session._stage(
            "relationship",
            invalid,
            scope="question",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    assert not session.validate().valid
    invalid_records = tuple(record for record in session.records if record.kind == "relationship")
    assert len(invalid_records) == 2
    assert session.fidelity_result is None

    for record in invalid_records:
        canonical = dict(record.payload)
        canonical.pop("item_id")
        session.correct_record(record.record_id, canonical)

    validation = session.validate()
    assert validation.valid, validation.errors
    packet = session.build_fidelity_packet()
    assert packet.records_hash == session.state["records_hash"]
    assert all("item_id" not in record["payload"] for record in packet.records if record["kind"] == "relationship")


def test_pre_fidelity_relationship_envelopes_are_attributed_and_corrected_sequentially(tmp_path: Path) -> None:
    context = RunContext("RUN-PRE-FIDELITY-RELATIONSHIP-ATTRIBUTION", tmp_path / "run")
    artifacts = tuple(
        _analytical_relationship(f"REQ16.relationship-{index}", "orders", "customers")
        for index in range(4)
    )
    _workspace, _lem, _registry, session = _session(
        context,
        analytical_relationships=artifacts,
    )
    for item_id in ("orders", "customers"):
        session.add_ontology_item(
            OntologyItem(item_id=item_id, item_type="entity", label=item_id.title(), scope="question"),
            evidence_refs=("work/plan.json",),
        )
    valid_ids = [
        session.add_claim(
            {"claim": f"valid-{index}"},
            scope="question",
            evidence_refs=("work/plan.json",),
            claim_id=f"valid-claim-{index}",
        )
        for index in range(15)
    ]

    relationship_record_ids: list[str] = []
    for artifact in artifacts:
        relationship_record_ids.append(
            session._stage(
                "relationship",
                _staged_relationship(artifact),
                scope="question",
                # Deliberately omit the concrete analytical artifact from the
                # outer record envelope.  The public correction adds only
                # this missing ref; the payload remains unchanged.
                evidence_refs=("work/plan.json",),
            )
        )

    validation, invalid_ids = session._validate_with_invalid_record_ids()
    assert invalid_ids == frozenset(relationship_record_ids)
    assert len(valid_ids) == 15
    assert not validation.valid
    assert all(record_id in "\n".join(validation.errors) for record_id in relationship_record_ids)

    for remaining, record_id in zip(range(4, 0, -1), relationship_record_ids):
        payload = next(record.payload for record in session.records if record.record_id == record_id)
        session.correct_record(
            record_id,
            payload,
            evidence_refs=("work/analytical_relationships.jsonl",),
        )
        _validation, invalid_ids = session._validate_with_invalid_record_ids()
        assert len(invalid_ids) == remaining - 1
        assert record_id not in invalid_ids

    validation = session.validate()
    assert validation.valid, validation.errors
    packet = session.build_fidelity_packet()
    assert packet.records_hash == session.state["records_hash"]
    assert len(packet.records) == 21


def test_pre_fidelity_relationship_correction_rejects_wrong_target_and_preserves_bytes(tmp_path: Path) -> None:
    context = RunContext("RUN-PRE-FIDELITY-RELATIONSHIP-ATTRIBUTION-NEGATIVE", tmp_path / "run")
    artifact = _analytical_relationship("REQ16.relationship-negative", "orders", "customers")
    _workspace, _lem, _registry, session = _session(context, analytical_relationships=(artifact,))
    for item_id in ("orders", "customers"):
        session.add_ontology_item(
            OntologyItem(item_id=item_id, item_type="entity", label=item_id.title(), scope="question"),
            evidence_refs=("work/plan.json",),
        )
    record_id = session._stage(
        "relationship",
        _staged_relationship(artifact),
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    valid_claim_id = session.add_claim(
        {"claim": "valid"},
        scope="question",
        evidence_refs=("work/plan.json",),
        claim_id="valid-claim",
    )
    paths = (
        session.staging_root / "snapshot.json",
        session.staging_root / "session.json",
        session.staging_root / "records.jsonl",
    )

    before = {path: path.read_bytes() for path in paths}
    before_mtimes = {path: path.stat().st_mtime_ns for path in paths}
    with pytest.raises(KeyError):
        session.correct_record("missing-record", {"claim": "nope"})
    with pytest.raises(ValueError, match="repair_once result"):
        session.correct_record(valid_claim_id, {"claim": "edited"})

    mismatched = dict(next(record.payload for record in session.records if record.record_id == record_id))
    mismatched["target_id"] = "orders"
    with pytest.raises(ValueError, match="remains invalid|mismatch"):
        session.correct_record(
            record_id,
            mismatched,
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    assert {path: path.read_bytes() for path in paths} == before
    assert {path: path.stat().st_mtime_ns for path in paths} == before_mtimes
    _validation, invalid_ids = session._validate_with_invalid_record_ids()
    assert invalid_ids == frozenset({record_id})


def test_pre_fidelity_mixed_stale_and_missing_refs_remain_attributable(tmp_path: Path) -> None:
    context = RunContext("RUN-PRE-FIDELITY-MIXED-RELATIONSHIP-ATTRIBUTION", tmp_path / "run")
    first = _analytical_relationship("REQ16.relationship-stale", "orders", "customers")
    second = _analytical_relationship("REQ16.relationship-missing-ref", "orders", "customers")
    _workspace, _lem, _registry, session = _session(
        context,
        analytical_relationships=(first, second),
    )
    for item_id in ("orders", "customers"):
        session.add_ontology_item(
            OntologyItem(item_id=item_id, item_type="entity", label=item_id.title(), scope="question"),
            evidence_refs=("work/plan.json",),
        )
    first_id = session._stage(
        "relationship",
        _staged_relationship(first),
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    second_id = session._stage(
        "relationship",
        _staged_relationship(second),
        scope="question",
        evidence_refs=("work/plan.json",),
    )

    first_record = next(record for record in session.records if record.record_id == first_id)
    stale_value = first_record.to_dict()
    stale_hashes = dict(stale_value["evidence_hashes"])
    stale_hashes["work/analytical_relationships.jsonl"] = "0" * 64
    stale_value["evidence_hashes"] = stale_hashes
    stale_unsigned = dict(stale_value)
    stale_unsigned.pop("record_hash")
    stale_value["record_hash"] = _sha256_value(stale_unsigned)
    stale_record = IntegrationRecord.from_dict(stale_value)
    first_index = next(index for index, record in enumerate(session._records) if record.record_id == first_id)
    session._records[first_index] = stale_record
    session._by_id[first_id] = stale_record
    session._persist_state(session.state)
    validation, invalid_ids = session._validate_with_invalid_record_ids()
    assert invalid_ids == frozenset({first_id, second_id})
    assert "artifact evidence hash is stale" in "\n".join(validation.errors)
    assert second_id in "\n".join(validation.errors)

    paths = (
        session.staging_root / "snapshot.json",
        session.staging_root / "session.json",
        session.staging_root / "records.jsonl",
    )
    before = {path: path.read_bytes() for path in paths}
    before_mtimes = {path: path.stat().st_mtime_ns for path in paths}
    mismatched = dict(next(record.payload for record in session.records if record.record_id == first_id))
    mismatched["target_id"] = "orders"
    with pytest.raises(ValueError, match="remains invalid|mismatch"):
        session.correct_record(
            first_id,
            mismatched,
            evidence_refs=("work/analytical_relationships.jsonl",),
        )
    assert {path: path.read_bytes() for path in paths} == before
    assert {path: path.stat().st_mtime_ns for path in paths} == before_mtimes

    first_payload = next(record.payload for record in session.records if record.record_id == first_id)
    session.correct_record(
        first_id,
        first_payload,
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    _validation, invalid_ids = session._validate_with_invalid_record_ids()
    assert invalid_ids == frozenset({second_id})

    second_payload = next(record.payload for record in session.records if record.record_id == second_id)
    session.correct_record(
        second_id,
        second_payload,
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    assert session.validate().valid
    packet = session.build_fidelity_packet()
    assert packet.records_hash == session.state["records_hash"]


def test_invalid_pre_fidelity_correction_rejects_unrelated_valid_target(tmp_path: Path) -> None:
    context = RunContext("RUN-PRE-FIDELITY-UNRELATED", tmp_path / "run")
    artifact = _analytical_relationship("orders-customers-analysis", "orders", "customers")
    _workspace, _lem, _registry, session = _session(context, analytical_relationships=(artifact,))
    valid_target_id = session.add_ontology_item(
        OntologyItem(item_id="orders", item_type="entity", label="Orders", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    session.add_ontology_item(
        OntologyItem(item_id="customers", item_type="entity", label="Customers", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    invalid = _staged_relationship(artifact)
    invalid["item_id"] = "REQ-03"
    session._stage(
        "relationship",
        invalid,
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    assert not session.validate().valid
    before_records = session.records
    valid_payload = {
        "item_id": "orders",
        "item_type": "entity",
        "label": "Orders edited",
        "scope": "question",
    }

    with pytest.raises(ValueError, match="record correction requires an item fidelity repair_once result"):
        session.correct_record(valid_target_id, valid_payload)

    assert session.records == before_records


def test_pre_fidelity_correction_uses_exact_record_ownership_for_colons(tmp_path: Path) -> None:
    context = RunContext("RUN-PRE-FIDELITY-COLLIDING-IDS", tmp_path / "run")
    artifact = _analytical_relationship("orders-customers-analysis", "orders", "customers")
    _workspace, _lem, _registry, session = _session(context, analytical_relationships=(artifact,))
    for item_id in ("orders", "customers"):
        session.add_ontology_item(
            OntologyItem(item_id=item_id, item_type="entity", label=item_id.title(), scope="question"),
            evidence_refs=("work/plan.json",),
        )
    session._stage(
        "claim",
        {"claim": "valid record"},
        scope="question",
        evidence_refs=("work/plan.json",),
        record_id="foo",
    )
    invalid = _staged_relationship(artifact)
    invalid["item_id"] = "REQ-03"
    session._stage(
        "relationship",
        invalid,
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
        record_id="foo:bar",
    )
    assert not session.validate().valid

    before_records = session.records
    with pytest.raises(ValueError, match="record correction requires an item fidelity repair_once result"):
        session.correct_record("foo", {"claim": "edited valid record"})
    assert session.records == before_records

    canonical = dict(next(record.payload for record in before_records if record.record_id == "foo:bar"))
    canonical.pop("item_id")
    session.correct_record("foo:bar", canonical)
    assert session.validate().valid


def test_pre_fidelity_recorrection_rejects_now_valid_target_while_other_invalid(tmp_path: Path) -> None:
    context = RunContext("RUN-PRE-FIDELITY-RECORRECTION", tmp_path / "run")
    first = _analytical_relationship("orders-customers-analysis", "orders", "customers")
    second = _analytical_relationship("orders-shipments-analysis", "orders", "shipments")
    _workspace, _lem, _registry, session = _session(
        context,
        analytical_relationships=(first, second),
    )
    for item_id in ("orders", "customers", "shipments"):
        session.add_ontology_item(
            OntologyItem(item_id=item_id, item_type="entity", label=item_id.title(), scope="question"),
            evidence_refs=("work/plan.json",),
        )
    invalid_records = []
    for artifact in (first, second):
        invalid = _staged_relationship(artifact)
        invalid["item_id"] = "REQ-03"
        record_id = session._stage(
            "relationship",
            invalid,
            scope="question",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )
        invalid_records.append(record_id)

    first_payload = dict(next(record.payload for record in session.records if record.record_id == invalid_records[0]))
    first_payload.pop("item_id")
    session.correct_record(invalid_records[0], first_payload)
    assert not session.validate().valid
    before_records = session.records
    repeated_payload = dict(next(record.payload for record in before_records if record.record_id == invalid_records[0]))
    repeated_payload["limitations"] = ["repeated pre-fidelity edit"]

    with pytest.raises(ValueError, match="record correction requires an item fidelity repair_once result"):
        session.correct_record(invalid_records[0], repeated_payload)

    assert session.records == before_records


def test_valid_pre_fidelity_session_rejects_arbitrary_correction(tmp_path: Path) -> None:
    context = RunContext("RUN-VALID-PRE-FIDELITY", tmp_path / "run")
    artifact = _analytical_relationship("orders-customers-analysis", "orders", "customers")
    _workspace, _lem, _registry, session = _session(context, analytical_relationships=(artifact,))
    for item_id in ("orders", "customers"):
        session.add_ontology_item(
            OntologyItem(item_id=item_id, item_type="entity", label=item_id.title(), scope="question"),
            evidence_refs=("work/plan.json",),
        )
    relationship_id = session.add_relationship(
        _staged_relationship(artifact),
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    assert session.validate().valid
    before_records = session.records
    before_state = session.state
    changed = dict(next(record.payload for record in before_records if record.record_id == relationship_id))
    changed["limitations"] = ["arbitrary pre-fidelity edit"]

    with pytest.raises(ValueError, match="record correction requires an item fidelity repair_once result"):
        session.correct_record(relationship_id, changed)

    assert session.records == before_records
    assert session.state == before_state


def test_identity_decision_mapping_order_and_lem_export(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    _workspace, _lem, _registry, session = _session(context)
    decision = IdentityDecision(
        candidate_id="customer-c-1",
        decision="same_object",
        decision_id="decision-customer-c-1",
        review_status="reviewed",
        reviewer_ref="reviewer-identity",
        evidence_refs=("work/plan.json",),
        rationale="Reviewed source keys identify one customer.",
        scope="question",
    )
    mapping = CanonicalMapping(
        canonical_id="customer-canonical-1",
        object_type="entity",
        source_identities=("crm:C-1", "orders:C-1", "support:C-1"),
        decision_id=decision.decision_id,
        scope="question",
    )
    session.add_identity_decision(decision, scope="question", evidence_refs=("work/plan.json",))
    session.add_canonical_mapping(mapping, scope="question", evidence_refs=("work/plan.json",))
    validation = session.validate()
    assert validation.valid
    session.build_fidelity_packet()
    session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    _commit(session)
    exported = session.lem.export()
    assert exported["identity_decisions"] == [decision.to_dict()]
    assert exported["canonical_mappings"] == [mapping.to_dict()]


def test_identity_mapping_can_bind_to_prior_committed_decision(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    _workspace, _lem, registry, first = _session(context)
    decision = IdentityDecision(
        candidate_id="customer-c-2",
        decision="alternate_representation",
        decision_id="decision-customer-c-2",
        review_status="accepted",
        reviewer_ref="reviewer-identity",
        evidence_refs=("work/plan.json",),
        rationale="Source representation is an approved alternate.",
        scope="question",
    )
    first.add_identity_decision(decision, scope="question", evidence_refs=("work/plan.json",))
    _commit(first)
    _workspace2 = _accepted(context, "Q-002")
    second = IntegrationSession.create(context, _workspace2, registry, "integration-owner", invocation_id="inv-Q-002")
    mapping = CanonicalMapping(
        canonical_id="customer-canonical-2",
        object_type="entity",
        source_identities=("legacy:C-2", "crm:C-2"),
        decision_id=decision.decision_id,
        scope="question",
    )
    second.add_canonical_mapping(mapping, scope="question", evidence_refs=("work/plan.json",))
    assert second.validate().valid


def test_accepted_bundle_is_opaque_and_evidence_short_ref_is_accepted(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context, value={"exact": ["bytes", 7]})
    before = (workspace.accepted_root / "answer_content.json").read_bytes()
    assert AcceptedAnalysisBundle.load(workspace).answer_content == before
    record_id = session.add_claim({"claim": "opaque claim"}, scope="question", evidence_refs=("answer_content.json",))
    assert session.records[0].evidence_hashes["answer_content.json"] == hashlib.sha256(before).hexdigest()
    _commit(session)
    assert (workspace.accepted_root / "answer_content.json").read_bytes() == before
    assert session.records[0].record_id == record_id
    assert workspace.integration_state == "integrated"


def test_accepted_bundle_rejects_self_consistent_unbound_directory_replacement(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, _lem, _registry, integration_session = _session(context, value={"exact": "original"})
    # A valid reload is accepted before the replacement.
    assert AcceptedAnalysisBundle.load(workspace).item_id == workspace.item_id
    accepted = workspace.accepted_root
    replacement = b'{"exact":"replacement"}\n'
    (accepted / "answer_content.json").write_bytes(replacement)
    envelope = json.loads((accepted / "acceptance_envelope.json").read_text(encoding="utf-8"))
    content_hash = hashlib.sha256(replacement).hexdigest()
    envelope["content_hash"] = content_hash
    envelope["draft_hash"] = content_hash
    envelope_bytes = _json_bytes(envelope)
    (accepted / "acceptance_envelope.json").write_bytes(envelope_bytes)
    manifest = json.loads((accepted / "manifest.json").read_text(encoding="utf-8"))
    manifest["content_hash"] = content_hash
    manifest["envelope_hash"] = hashlib.sha256(envelope_bytes).hexdigest()
    manifest["manifest_hash"] = _manifest_hash(manifest)
    (accepted / "manifest.json").write_bytes(_json_bytes(manifest))
    with pytest.raises(ValueError, match="terminal intent"):
        AcceptedAnalysisBundle.load(workspace)


def test_typed_records_corrections_and_lem_growth(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    relationship_artifact = _analytical_relationship("customer-order-analysis", "customer", "order")
    workspace, lem, registry, session = _session(context, analytical_relationships=(relationship_artifact,))
    source_id = session.add_ontology_item(
        OntologyItem(item_id="customer", item_type="entity", label="Customer", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    target_id = session.add_ontology_item(
        OntologyItem(item_id="order", item_type="entity", label="Order", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    metric_id = session.add_metric({"item_id": "orders_metric", "label": "Orders", "value": 3}, scope="question", evidence_refs=("work/plan.json",))
    limitation_id = session.add_limitation({"limitation": "synthetic limit"}, scope="question", evidence_refs=("work/plan.json",))
    session.add_relationship(
        _staged_relationship(relationship_artifact, relationship_id="customer_order"),
        scope="question",
        evidence_refs=("work/plan.json", "work/analytical_relationships.jsonl"),
    )
    session.add_dashboard_fact({"fact": "orders_metric", "value": 3}, scope="question", evidence_refs=("work/plan.json",))
    session.link_evidence(metric_id, ("work/plan.json",), scope="question")
    before = session.records[0].record_hash
    session.record_fidelity_review(
        "repair_once",
        findings=[{"message": "label correction", "record_id": source_id, "parts": ["payload"]}],
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    session.correct_record(source_id, {"item_id": "customer", "item_type": "entity", "label": "Customer corrected", "scope": "question"})
    assert session.records[0].record_hash != before
    assert session.validate().valid
    session.build_fidelity_packet()
    session.record_fidelity_review(
        "accept",
        checked_record_ids=(source_id,),
    )
    manifest = _commit(session)
    assert manifest["records_count"] == 7
    assert {"customer", "order"}.issubset(session.lem.ontology)
    assert "orders_metric" not in session.lem.ontology
    assert limitation_id in session.lem.knowledge
    assert workspace.integration_manifest_hash == manifest["manifest_hash"]
    assert "order" in {record.payload.get("item_id") for record in session.records if record.kind == "ontology_item"}


def test_public_repair_once_remove_metric_targeted_accept_commit_and_exact_retry(tmp_path: Path) -> None:
    context = RunContext("RUN-REMOVE-METRIC", tmp_path / "run")
    workspace, _lem, _registry, session = _session(context)
    metric_id = session.add_metric(
        {"item_id": "unsupported_metric", "label": "Unsupported", "value": 7},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    baseline_hash = next(record.record_hash for record in session.records if record.record_id == metric_id)
    session.record_fidelity_review(
        "repair_once",
        affected_record_ids=(metric_id,),
        checked_record_ids=(metric_id,),
    )

    removed_hash = session.remove_record(metric_id)
    assert removed_hash == baseline_hash
    assert len(session.records) == 0
    assert session.validate().valid

    # An exact retry is read-only and returns the same deterministic baseline
    # hash, including the durable progress and packet bytes.
    retry_paths = (
        session.staging_root / "snapshot.json",
        session.staging_root / "session.json",
        session.staging_root / "records.jsonl",
        session.fidelity_progress_path,
        session.fidelity_packet_path,
    )
    before_retry = {path: path.read_bytes() for path in retry_paths}
    assert session.remove_record(metric_id) == baseline_hash
    assert {path: path.read_bytes() for path in retry_paths} == before_retry

    packet = session.build_fidelity_packet()
    assert all(record["record_id"] != metric_id for record in packet.records)
    targeted = session.record_fidelity_review("accept", checked_record_ids=(metric_id,))
    assert targeted.review_kind == "targeted"
    manifest = session.commit()
    assert manifest["records_count"] == 0
    assert workspace.integration_state == "integrated"
    assert not session.lem.knowledge
    assert session.commit() == manifest


def test_prepublication_remove_claims_exact_retry_and_restage(tmp_path: Path) -> None:
    context = RunContext("RUN-PREPUBLICATION-REMOVE", tmp_path / "run")
    workspace, _lem, registry, session = _session(context)
    claim_ids = tuple(
        session.add_claim(
            {"claim": f"staged-{index}", "value": index},
            scope="question",
            evidence_refs=("work/plan.json",),
            claim_id=f"claim-{index}",
        )
        for index in range(4)
    )
    baseline = {record.record_id: record.record_hash for record in session.records}

    for record_id in claim_ids:
        assert session.remove_record(record_id) == baseline[record_id]
    assert session.records == ()
    assert session.validate().valid

    # The authoritative state map makes retries read-only and stable even
    # after the record has disappeared from records.jsonl.
    retry_paths = (
        session.staging_root / "snapshot.json",
        session.staging_root / "session.json",
        session.staging_root / "records.jsonl",
    )
    before_retry = {path: path.read_bytes() for path in retry_paths}
    session.release()
    session = IntegrationSession.load(
        context,
        workspace,
        registry,
        "integration-owner",
        invocation_id="inv-Q-001",
    )
    for record_id in claim_ids:
        assert session.remove_record(record_id) == baseline[record_id]
    assert {path: path.read_bytes() for path in retry_paths} == before_retry

    restaged = session.add_claim(
        {"claim": "restaged", "value": 99},
        scope="question",
        evidence_refs=("work/plan.json",),
        claim_id=claim_ids[0],
    )
    new_hash = next(record.record_hash for record in session.records if record.record_id == restaged)
    assert new_hash != baseline[restaged]
    assert session.remove_record(restaged) == new_hash
    assert session.records == ()


def test_prepublication_remove_allows_incomplete_analytical_staging(tmp_path: Path) -> None:
    context = RunContext("RUN-PREPUBLICATION-INCOMPLETE-RELATIONSHIPS", tmp_path / "run")
    present = _analytical_relationship("REQ16.sales_to_product", "sales", "product")
    missing = _analytical_relationship(
        "REQ16.wms_stock_sku_to_product_variant",
        "wms_stock_sku",
        "product_variant",
    )
    workspace, _lem, registry, session = _session(
        context,
        analytical_relationships=(present, missing),
    )
    claim_ids = tuple(
        session.add_claim(
            {"claim": f"TEST-{index}"},
            scope="question",
            evidence_refs=("work/plan.json",),
            claim_id=f"test-claim-{index}",
        )
        for index in range(4)
    )
    baseline = {record.record_id: record.record_hash for record in session.records}
    for record_id in claim_ids:
        assert session.remove_record(record_id) == baseline[record_id]
    assert session.records == ()
    # Final validation deliberately retains completeness and reports the
    # absent accepted relationship; the pre-review cleanup only needs the
    # partial mechanical validator to succeed.
    assert not session.validate().valid
    before_retry = (session.staging_root / "snapshot.json").read_bytes()
    assert session.remove_record(claim_ids[0]) == baseline[claim_ids[0]]
    assert (session.staging_root / "snapshot.json").read_bytes() == before_retry

    session.release()
    reloaded = IntegrationSession.load(
        context,
        workspace,
        registry,
        "integration-owner",
        invocation_id="inv-Q-001",
    )
    restaged = reloaded.add_claim(
        {"claim": "valid-restage"},
        scope="question",
        evidence_refs=("work/plan.json",),
        claim_id="test-claim-0",
    )
    assert restaged == "test-claim-0"
    partial_validation = reloaded._validate_partial_staging()
    assert partial_validation.valid
    assert not reloaded.validate().valid


def test_prepublication_remove_rejects_partial_relationship_mismatch(tmp_path: Path) -> None:
    context = RunContext("RUN-PREPUBLICATION-PARTIAL-RELATIONSHIP-MISMATCH", tmp_path / "run")
    artifact = _analytical_relationship("REQ16.sales_to_product", "sales", "product")
    missing = _analytical_relationship(
        "REQ16.wms_stock_sku_to_product_variant",
        "wms_stock_sku",
        "product_variant",
    )
    _workspace, _lem, _registry, session = _session(
        context,
        analytical_relationships=(artifact, missing),
    )
    for item_id in ("sales", "product", "wrong-product"):
        session.add_ontology_item(
            {"item_id": item_id, "item_type": "entity", "label": item_id.title()},
            scope="question",
            evidence_refs=("work/plan.json",),
        )
    mismatched = _staged_relationship(artifact, relationship_id="REL-REQ16-MISMATCH")
    mismatched["target_id"] = "wrong-product"
    relationship_id = session.add_relationship(
        mismatched,
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    claim_id = session.add_claim(
        {"claim": "TEST"},
        scope="question",
        evidence_refs=("work/plan.json",),
        claim_id="test-claim",
    )
    paths = (
        session.staging_root / "snapshot.json",
        session.staging_root / "session.json",
        session.staging_root / "records.jsonl",
    )
    before = {path: path.read_bytes() for path in paths}
    before_mtimes = {path: path.stat().st_mtime_ns for path in paths}
    with pytest.raises(ValueError, match="mismatch|validation"):
        session.remove_record(claim_id)
    assert {path: path.read_bytes() for path in paths} == before
    assert {path: path.stat().st_mtime_ns for path in paths} == before_mtimes
    assert relationship_id in {record.record_id for record in session.records}


def test_prepublication_remove_rejects_review_commit_owner_and_tamper(tmp_path: Path) -> None:
    context = RunContext("RUN-PREPUBLICATION-REMOVE-BOUNDARIES", tmp_path / "run")
    workspace, _lem, registry, session = _session(context)
    claim_id = session.add_claim(
        {"claim": "review-boundary"},
        scope="question",
        evidence_refs=("work/plan.json",),
        claim_id="review-claim",
    )
    with pytest.raises(ValueError):
        IntegrationSession.load(context, workspace, registry, "other-owner", invocation_id="inv-Q-001")
    with pytest.raises(ValueError):
        IntegrationSession.load(context, workspace, registry, "integration-owner", invocation_id="other-invocation")

    snapshot_path = session.staging_root / "snapshot.json"
    original_snapshot = snapshot_path.read_bytes()
    snapshot_path.write_bytes(original_snapshot + b"tamper")
    with pytest.raises(ValueError, match="snapshot"):
        session.remove_record(claim_id)
    snapshot_path.write_bytes(original_snapshot)

    session.build_fidelity_packet()
    with pytest.raises(ValueError):
        session.remove_record(claim_id)

    context_review = RunContext("RUN-PREPUBLICATION-REMOVE-REVIEW", tmp_path / "review")
    _workspace_review, _lem_review, _registry_review, reviewed = _session(context_review)
    reviewed_id = reviewed.add_claim(
        {"claim": "accepted-boundary"},
        scope="question",
        evidence_refs=("work/plan.json",),
        claim_id="accepted-claim",
    )
    reviewed.record_fidelity_review("accept", checked_record_ids=(reviewed_id,))
    with pytest.raises(ValueError):
        reviewed.remove_record(reviewed_id)

    context_commit = RunContext("RUN-PREPUBLICATION-REMOVE-COMMIT", tmp_path / "commit")
    _workspace_commit, _lem_commit, _registry_commit, committed = _session(context_commit)
    committed_id = committed.add_claim(
        {"claim": "committed-boundary"},
        scope="question",
        evidence_refs=("work/plan.json",),
        claim_id="committed-claim",
    )
    _commit(committed)
    with pytest.raises(ValueError, match="terminal"):
        committed.remove_record(committed_id)


def test_prepublication_remove_rolls_back_when_staging_would_be_invalid(tmp_path: Path) -> None:
    context = RunContext("RUN-PREPUBLICATION-REMOVE-ROLLBACK", tmp_path / "run")
    relationship = _analytical_relationship("pre-review-required", "orders", "customers")
    _workspace, _lem, _registry, session = _session(context, analytical_relationships=(relationship,))
    for item_id in ("orders", "customers"):
        session.add_ontology_item(
            {"item_id": item_id, "item_type": "entity", "label": item_id.title()},
            scope="question",
            evidence_refs=("work/plan.json",),
        )
    relationship_id = session.add_relationship(
        _staged_relationship(relationship),
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    orders_id = next(
        record.record_id
        for record in session.records
        if record.kind == "ontology_item" and record.payload.get("item_id") == "orders"
    )
    paths = (
        session.staging_root / "snapshot.json",
        session.staging_root / "session.json",
        session.staging_root / "records.jsonl",
    )
    before = {path: path.read_bytes() for path in paths}
    with pytest.raises(ValueError, match="validation|staged|unknown ontology"):
        session.remove_record(orders_id)
    assert {path: path.read_bytes() for path in paths} == before
    assert relationship_id in {record.record_id for record in session.records}
    assert session.validate().valid


def test_remove_record_rejects_unauthorized_dependency_and_unknown_without_writes(tmp_path: Path) -> None:
    context = RunContext("RUN-REMOVE-SCOPE", tmp_path / "run")
    _workspace, _lem, _registry, session = _session(context)
    affected = session.add_metric(
        {"item_id": "affected", "label": "Affected", "value": 1},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    dependency = session.add_metric(
        {"item_id": "dependency", "label": "Dependency", "value": 2},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    session.record_fidelity_review(
        "repair_once",
        affected_record_ids=(affected,),
        dependency_ids=(dependency,),
        checked_record_ids=(affected, dependency),
    )
    paths = (session.staging_root / "snapshot.json", session.staging_root / "records.jsonl", session.fidelity_progress_path)
    before = {path: path.read_bytes() for path in paths}
    for target in (dependency, "unknown-record"):
        with pytest.raises(ValueError):
            session.remove_record(target)
    assert {path: path.read_bytes() for path in paths} == before
    assert {record.record_id for record in session.records} == {affected, dependency}


def test_required_relationship_removal_fails_and_rolls_back_byte_exact(tmp_path: Path) -> None:
    context = RunContext("RUN-REMOVE-RELATIONSHIP", tmp_path / "run")
    artifact = _analytical_relationship("required-analysis", "orders", "customers")
    _workspace, _lem, _registry, session = _session(context, analytical_relationships=(artifact,))
    for item_id in ("orders", "customers"):
        session.add_ontology_item(
            OntologyItem(item_id=item_id, item_type="entity", label=item_id.title(), scope="question"),
            evidence_refs=("work/plan.json",),
        )
    relationship_id = session.add_relationship(
        _staged_relationship(artifact),
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    session.record_fidelity_review(
        "repair_once",
        affected_record_ids=(relationship_id,),
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    paths = (
        session.staging_root / "snapshot.json",
        session.staging_root / "session.json",
        session.staging_root / "records.jsonl",
        session.fidelity_progress_path,
        session.fidelity_packet_path,
    )
    before = {path: path.read_bytes() for path in paths}
    with pytest.raises(ValueError, match="validation|staged"):
        session.remove_record(relationship_id)
    assert {path: path.read_bytes() for path in paths} == before
    assert relationship_id in {record.record_id for record in session.records}
    assert session.validate().valid


def test_remove_record_tamper_and_intent_symlink_fail_closed(tmp_path: Path) -> None:
    context = RunContext("RUN-REMOVE-TAMPER", tmp_path / "run")
    _workspace, _lem, _registry, session = _session(context)
    metric_id = session.add_metric(
        {"item_id": "tampered_metric", "label": "Tampered", "value": 1},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    session.record_fidelity_review(
        "repair_once",
        affected_record_ids=(metric_id,),
        checked_record_ids=(metric_id,),
    )
    progress_path = session.fidelity_progress_path
    original_progress = progress_path.read_bytes()
    progress = json.loads(original_progress.decode("utf-8"))
    progress["current_records_hash"] = "0" * 64
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError):
        session.remove_record(metric_id)
    progress_path.write_bytes(original_progress)

    snapshot_path = session.staging_root / "snapshot.json"
    original_snapshot = snapshot_path.read_bytes()
    snapshot_path.write_bytes(original_snapshot + b"tamper")
    with pytest.raises(ValueError):
        session.remove_record(metric_id)
    snapshot_path.write_bytes(original_snapshot)

    intent_path = session.staging_root / "commit_intent.json"
    intent_path.symlink_to(progress_path)
    with pytest.raises(ValueError, match="intent"):
        session.remove_record(metric_id)
    intent_path.unlink()

    authorization = session._read_repair_authorization()
    before_records_hash = hashlib.sha256(session._records_bytes()).hexdigest()
    after_records = [record for record in session.records if record.record_id != metric_id]
    after_records_hash = hashlib.sha256(
        b"".join(_json_bytes(record.to_dict()) for record in after_records)
    ).hexdigest()
    session._write_fidelity_removal_intent(
        authorization,
        target=metric_id,
        baseline_record_hash=authorization.baseline_record_hashes[metric_id],
        before_records_hash=before_records_hash,
        after_records_hash=after_records_hash,
        phase="prepared",
    )
    valid_intent = json.loads(session.fidelity_removal_intent_path.read_text(encoding="utf-8"))

    def tamper_intent(field: str, value: object) -> None:
        tampered = dict(valid_intent)
        tampered[field] = value
        unsigned = {key: item for key, item in tampered.items() if key != "intent_hash"}
        tampered["intent_hash"] = hashlib.sha256(_json_bytes(unsigned)).hexdigest()
        session.fidelity_removal_intent_path.write_bytes(_json_bytes(tampered))
        with pytest.raises(ValueError):
            session.remove_record(metric_id)
        session.fidelity_removal_intent_path.unlink()

    tamper_intent("session_id", "IS-WRONG")
    tamper_intent("invocation_id", "inv-wrong")
    tamper_intent("authorization_hash", "0" * 64)
    tamper_intent("phase", "unknown")
    assert metric_id in {record.record_id for record in session.records}


@pytest.mark.parametrize("crash_phase", ("after_intent", "after_records", "after_progress"))
def test_remove_record_hard_crash_journal_reconciles_at_every_boundary(tmp_path: Path, crash_phase: str) -> None:
    context = RunContext(f"RUN-REMOVE-CRASH-{crash_phase}", tmp_path / "run")
    workspace, _lem, _registry, session = _session(context)
    metric_id = session.add_metric(
        {"item_id": "crash_metric", "label": "Crash boundary", "value": 9},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    baseline_hash = next(record.record_hash for record in session.records if record.record_id == metric_id)
    session.record_fidelity_review(
        "repair_once",
        affected_record_ids=(metric_id,),
        checked_record_ids=(metric_id,),
    )
    session.release()

    # A real child process exits without running Python exception cleanup.  The
    # private hook is a test-only seam; production has no crash environment
    # switch or alternate persistence path.
    repo = Path(__file__).resolve().parents[2]
    child = f"""
import os
from pathlib import Path
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.integration import IntegrationSession
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.workspace import RunContext

context = RunContext({context.run_id!r}, Path({str(context.run_root)!r}))
workspace = ItemWorkspace.load(context, {workspace.item_id!r})
registry = PreparedAssetRegistry(context)

def crash(phase):
    if phase == {crash_phase!r}:
        os._exit(91)

IntegrationSession._fidelity_removal_crash_hook = staticmethod(crash)
session = IntegrationSession.load(
    context,
    workspace,
    registry,
    "integration-owner",
    invocation_id={session.invocation_id!r},
)
session.remove_record({metric_id!r})
raise SystemExit(2)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(repo / "src"), env.get("PYTHONPATH", ""))))
    child_result = subprocess.run(
        [sys.executable, "-c", child],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child_result.returncode == 91, child_result.stderr

    recovered_workspace = ItemWorkspace.load(context, workspace.item_id)
    recovered = IntegrationSession.load(
        context,
        recovered_workspace,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id=session.invocation_id,
    )
    assert metric_id not in {record.record_id for record in recovered.records}
    assert not recovered.fidelity_removal_intent_path.exists()

    retry_paths = (
        recovered.staging_root / "snapshot.json",
        recovered.staging_root / "session.json",
        recovered.staging_root / "records.jsonl",
        recovered.fidelity_progress_path,
        recovered.fidelity_packet_path,
    )
    before_retry = {path: path.read_bytes() for path in retry_paths}
    assert recovered.remove_record(metric_id) == baseline_hash
    assert {path: path.read_bytes() for path in retry_paths} == before_retry
    packet = recovered.build_fidelity_packet()
    assert metric_id not in {record["record_id"] for record in packet.records}
    targeted = recovered.record_fidelity_review("accept", checked_record_ids=(metric_id,))
    assert targeted.review_kind == "targeted"
    manifest = recovered.commit()
    assert manifest["records_count"] == 0
    assert recovered_workspace.integration_state == "integrated"
    assert not recovered.lem.knowledge


def test_current_observation_is_optional_fact_not_ontology_and_uses_observed_as_of(tmp_path: Path) -> None:
    context = RunContext("RUN-CURRENT-FACT", tmp_path / "run")
    _workspace, _lem, _registry, session = _session(context)
    # Future obligation dates are deliberately not inputs to this helper.
    as_of = observation_as_of(("2024-06-28", "2024-06-30", None))
    assert as_of == "2024-06-30"
    fact = CurrentObservationFact(
        observation_id="REQ-01.late-deliveries",
        metric="late deliveries",
        value=59,
        unit="deliveries",
        population="4,095 observed ERP deliveries",
        as_of=as_of,
        date_authority="latest observed delivery timestamp",
        evidence_refs=("work/plan.json",),
        limitations=("24 additional deliveries have unknown completion state",),
    )
    record_id = session.add_current_observation(fact, scope="requirement")
    record = next(value for value in session.records if value.record_id == record_id)
    assert record.kind == "dashboard_fact"
    assert record.payload["semantic_role"] == "current_observation_not_definition"
    assert record.payload["as_of"] == "2024-06-30"
    assert not session.lem.ontology
    assert session.validate().valid


def test_relationship_refs_include_prior_staged_metrics_and_relationship_ontology(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    first_artifact = _analytical_relationship("customer-orders-analysis", "customer", "orders_metric")
    second_artifact = _analytical_relationship("customer-orders-fact-analysis", "customer_orders", "customer")
    workspace, lem, _registry, session = _session(
        context,
        analytical_relationships=(first_artifact, second_artifact),
    )
    session.add_ontology_item(
        OntologyItem(item_id="customer", item_type="entity", label="Customer", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    session.add_metric_definition({"item_id": "orders_metric", "label": "Orders", "properties": {"unit": "count"}}, scope="question", evidence_refs=("work/plan.json",))
    session.add_relationship(
        _staged_relationship(first_artifact, relationship_id="customer_orders"),
        scope="question",
        evidence_refs=("work/plan.json", "work/analytical_relationships.jsonl"),
    )
    session.add_relationship(
        _staged_relationship(second_artifact, relationship_id="customer_orders_fact"),
        scope="question",
        evidence_refs=("work/plan.json", "work/analytical_relationships.jsonl"),
    )
    assert session.validate().valid
    _commit(session)
    assert {"orders_metric", "customer_orders", "customer_orders_fact"}.issubset(session.lem.ontology)


def test_relationship_forward_or_unknown_refs_fail_closed(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    artifact = _analytical_relationship("future-analysis", "customer", "future_metric")
    _workspace, _lem, _registry, session = _session(context, analytical_relationships=(artifact,))
    session.add_ontology_item(
        OntologyItem(item_id="customer", item_type="entity", label="Customer", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    session.add_relationship(
        _staged_relationship(artifact, relationship_id="future_ref"),
        scope="question",
        evidence_refs=("work/plan.json", "work/analytical_relationships.jsonl"),
    )
    session.add_metric({"item_id": "future_metric", "label": "Future"}, scope="question", evidence_refs=("work/plan.json",))
    assert not session.validate().valid
    with pytest.raises(ValueError, match="validation"):
        _commit(session)


def test_relationship_artifact_mutation_after_stage_is_rejected(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    artifact = _analytical_relationship("orders-customers-analysis", "orders", "customers")
    workspace, _lem, _registry, session = _session(context, analytical_relationships=(artifact,))
    session.add_ontology_item(
        OntologyItem(item_id="orders", item_type="entity", label="Orders", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    session.add_ontology_item(
        OntologyItem(item_id="customers", item_type="entity", label="Customers", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    session.add_relationship(
        _staged_relationship(artifact),
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    artifact_path = workspace.work_root / "analytical_relationships.jsonl"
    tampered = artifact_path.read_text(encoding="utf-8").replace("synthetic fixture", "tampered fixture")
    artifact_path.write_text(tampered, encoding="utf-8")
    validation = session.validate()
    assert not validation.valid
    assert any("artifact evidence hash is stale" in error for error in validation.errors)


def test_publishable_relationship_artifact_requires_temporal_authority(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    artifact = _analytical_relationship("orders-customers-no-time", "orders", "customers")
    artifact["date_authority"] = None
    artifact["as_of"] = None
    _workspace, _lem, _registry, session = _session(context, analytical_relationships=(artifact,))
    validation = session.validate()
    assert not validation.valid
    assert any("requires date_authority or as_of" in error for error in validation.errors)


def test_relationship_audit_can_coexist_without_publication(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    positive = _analytical_relationship("orders-customers-analysis", "orders", "customers")
    audit = _relationship_audit("orders-products-audit", "orders", "products")
    workspace, _lem, _registry, session = _session(
        context,
        analytical_relationships=(positive, audit),
    )
    session.add_ontology_item(
        OntologyItem(item_id="orders", item_type="entity", label="Orders", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    session.add_ontology_item(
        OntologyItem(item_id="customers", item_type="entity", label="Customers", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    session.add_relationship(
        _staged_relationship(positive),
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    assert session.validate().valid


def test_preflight_collision_has_no_intent_or_partial_lem_mutation(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace1, _lem, registry, session1 = _session(context)
    session1.add_metric_definition({"item_id": "m", "label": "existing"}, scope="question", evidence_refs=("work/plan.json",))
    _commit(session1)
    workspace = _accepted(context, "Q-002")
    session = IntegrationSession.create(context, workspace, registry, "integration-owner", invocation_id="inv-Q-002")
    session.add_metric_definition({"item_id": "m", "label": "different"}, scope="question", evidence_refs=("work/plan.json",))
    with pytest.raises(ValueError, match="collision"):
        _commit(session)
    assert not (session.staging_root / "commit_intent.json").exists()
    assert len(session.lem.ontology) == 1
    assert workspace.integration_state == "pending"


def test_staging_snapshot_reconciles_projection_crashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace = _accepted(context, "Q-001")
    registry = PreparedAssetRegistry(context)
    import auto_foundry_core.integration as integration_module

    original_json = integration_module._atomic_write_json
    def fail_session(path: Path, value: object) -> None:
        if path.name == "session.json":
            raise RuntimeError("crash after authoritative snapshot")
        original_json(path, value)
    monkeypatch.setattr(integration_module, "_atomic_write_json", fail_session)
    with pytest.raises(RuntimeError):
        IntegrationSession.create(context, workspace, registry, "integration-owner", invocation_id="inv-Q-001")
    monkeypatch.setattr(integration_module, "_atomic_write_json", original_json)
    reloaded = IntegrationSession.create(context, workspace, registry, "integration-owner", invocation_id="inv-Q-001")
    assert reloaded.status == "open"
    assert (reloaded.staging_root / "session.json").is_file()

    rid = reloaded.add_claim({"claim": "claim"}, scope="question", evidence_refs=("work/plan.json",))
    original_bytes = integration_module._atomic_write_bytes
    def fail_records(path: Path, payload: bytes) -> None:
        if path.name == "records.jsonl":
            raise RuntimeError("crash after snapshot")
        original_bytes(path, payload)
    monkeypatch.setattr(integration_module, "_atomic_write_bytes", fail_records)
    with pytest.raises(RuntimeError):
        reloaded.add_limitation({"limitation": "later"}, scope="question", evidence_refs=("work/plan.json",))
    monkeypatch.setattr(integration_module, "_atomic_write_bytes", original_bytes)
    recovered = IntegrationSession.load(context, workspace, registry, "integration-owner", invocation_id="inv-Q-001")
    assert {record.record_id for record in recovered.records} == {rid, next(record.record_id for record in reloaded.records if record.kind == "limitation")}


def test_intent_before_apply_and_retry_after_publish_or_item_state_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    session.add_claim({"claim": "claim"}, scope="question", evidence_refs=("work/plan.json",))
    original_apply = session._apply_records
    monkeypatch.setattr(session, "_apply_records", lambda: (_ for _ in ()).throw(RuntimeError("apply crash")))
    with pytest.raises(RuntimeError):
        _commit(session)
    assert (session.staging_root / "commit_intent.json").is_file()
    assert workspace.integration_state == "pending"
    monkeypatch.setattr(session, "_apply_records", original_apply)
    manifest = _commit(session)
    assert workspace.integration_state == "integrated"
    assert _commit(session)["manifest_hash"] == manifest["manifest_hash"]

    workspace2, lem2, registry2, session2 = _session(context, "Q-002")
    session2.add_claim({"claim": "claim"}, scope="question", evidence_refs=("work/plan.json",))
    original_publish = session2._write_committed
    monkeypatch.setattr(session2, "_write_committed", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("publish crash")))
    with pytest.raises(RuntimeError):
        _commit(session2)
    monkeypatch.setattr(session2, "_write_committed", original_publish)
    assert _commit(session2)["status"] == "committed"
    assert workspace2.integration_state == "integrated"


def test_partial_external_apply_retries_without_duplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    first = _asset(context, "asset-first", workspace=workspace)
    session.register_prepared_asset(first)
    session.add_claim({"claim": "later"}, scope="question", evidence_refs=("work/plan.json",))
    original_apply = session._apply_records
    raised = {"value": False}

    def flaky():
        original_apply()
        if not raised["value"]:
            raised["value"] = True
            raise RuntimeError("later apply crash")

    monkeypatch.setattr(session, "_apply_records", flaky)
    with pytest.raises(RuntimeError):
        _commit(session)
    assert registry.search(prepared_asset_id="asset-first")
    assert workspace.integration_state == "pending"
    monkeypatch.setattr(session, "_apply_records", original_apply)
    manifest = _commit(session)
    assert manifest["status"] == "committed"
    assert len(registry.search(prepared_asset_id="asset-first")) == 1


def test_published_then_item_mark_crash_rebinds_on_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    session.add_claim({"claim": "claim"}, scope="question", evidence_refs=("work/plan.json",))
    original_mark = workspace.mark_integration_committed
    raised = {"value": False}

    def flaky_mark(*args, **kwargs):
        if not raised["value"]:
            raised["value"] = True
            raise RuntimeError("item mark crash")
        return original_mark(*args, **kwargs)

    monkeypatch.setattr(workspace, "mark_integration_committed", flaky_mark)
    with pytest.raises(RuntimeError):
        _commit(session)
    assert session.committed_manifest_path.is_file()
    assert workspace.integration_state == "pending"
    monkeypatch.setattr(workspace, "mark_integration_committed", original_mark)
    manifest = _commit(session)
    assert workspace.integration_state == "integrated"
    assert workspace.integration_manifest_hash == manifest["manifest_hash"]


def test_integrated_then_staging_persistence_crash_converges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    session.add_claim({"claim": "claim"}, scope="question", evidence_refs=("work/plan.json",))
    original_persist = session._persist_state
    raised = {"value": False}

    def flaky_persist(state):
        if state.get("status") == "committed" and not raised["value"]:
            raised["value"] = True
            raise RuntimeError("staging persistence crash")
        return original_persist(state)

    monkeypatch.setattr(session, "_persist_state", flaky_persist)
    with pytest.raises(RuntimeError):
        _commit(session)
    assert workspace.integration_state == "integrated"
    monkeypatch.setattr(session, "_persist_state", original_persist)
    manifest = _commit(session)
    assert session.status == "committed"
    assert manifest["status"] == "committed"


def test_technical_failure_manifest_and_item_state_crashes_converge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    failure_path = session.staging_root.parent / "technical_failure" / "manifest.json"
    original_mark = workspace.mark_integration_failed
    raised = {"value": False}

    def flaky_mark(*args, **kwargs):
        if not raised["value"]:
            raised["value"] = True
            raise RuntimeError("failure item-state crash")
        return original_mark(*args, **kwargs)

    monkeypatch.setattr(workspace, "mark_integration_failed", flaky_mark)
    with pytest.raises(RuntimeError):
        session.mark_technical_failure("unrecoverable integration")
    assert failure_path.is_file()
    assert workspace.integration_state == "pending"
    monkeypatch.setattr(workspace, "mark_integration_failed", original_mark)
    first = session.mark_technical_failure("unrecoverable integration")
    assert first["status"] == "technical_failure"
    assert workspace.integration_state == "technical_failure"

    # A second crash after the item state is durable must reuse the exact
    # manifest rather than minting a competing timestamp/hash.
    workspace2, lem2, registry2, session2 = _session(context, "Q-002")
    original_persist = session2._persist_state
    raised2 = {"value": False}

    def flaky_persist(state):
        if state.get("status") == "technical_failure" and not raised2["value"]:
            raised2["value"] = True
            raise RuntimeError("failure session-state crash")
        return original_persist(state)

    monkeypatch.setattr(session2, "_persist_state", flaky_persist)
    with pytest.raises(RuntimeError):
        session2.mark_technical_failure("unrecoverable integration")
    assert workspace2.integration_state == "technical_failure"
    monkeypatch.setattr(session2, "_persist_state", original_persist)
    second = session2.mark_technical_failure("unrecoverable integration")
    assert second["manifest_hash"] == json.loads((session2.staging_root.parent / "technical_failure" / "manifest.json").read_text())["manifest_hash"]


def test_prepared_assets_all_scopes_register_and_reusable_loads_next_item(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace1, lem, registry, session1 = _session(context, "Q-001")
    reusable = _asset(context, "asset-reusable", scope="reusable", workspace=workspace1)
    scoped = _asset(context, "asset-scoped", scope="requirement_scoped", workspace=workspace1)
    session1.register_prepared_asset(reusable)
    session1.register_prepared_asset(scoped)
    _commit(session1)
    assert registry.load("asset-reusable").rows == ({"asset": "asset-reusable"},)
    assert registry.search(reusable_only=True, prepared_asset_id="asset-reusable")
    assert not registry.search(reusable_only=True, prepared_asset_id="asset-scoped")
    assert registry.search(prepared_asset_id="asset-scoped")

    workspace2 = _accepted(context, "Q-002")
    session2 = IntegrationSession.create(context, workspace2, registry, "integration-owner", invocation_id="inv-Q-002")
    session2.add_ontology_item(OntologyItem(item_id="item-2", item_type="entity", label="Second", scope="question"), evidence_refs=("work/plan.json",))
    _commit(session2)
    assert "item-2" in session2.lem.ontology
    assert workspace1.accepted_root.joinpath("answer_content.json").read_bytes()


def test_prepared_candidate_is_staged_without_registry_mutation_until_commit(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    candidate = _asset(context, "candidate-staged", workspace=workspace)
    session.register_prepared_asset(candidate)
    assert registry.search(prepared_asset_id=candidate.prepared_asset_id) == ()
    assert not registry.registry_path.exists()
    _commit(session)
    assert registry.search(prepared_asset_id=candidate.prepared_asset_id) == (candidate,)


def test_direct_registry_publication_bypass_requires_integration_commit_authority(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, _lem, registry, integration_session = _session(context)
    candidate = _asset(context, "direct-bypass", workspace=workspace)
    with pytest.raises(ValueError, match="IntegrationSession commit authority"):
        registry.register_accepted(candidate, item_workspace=workspace)
    assert registry.search(prepared_asset_id=candidate.prepared_asset_id) == ()


def test_registry_index_crash_repairs_on_exact_integration_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, _lem, registry, session = _session(context)
    candidate = _asset(context, "index-retry", workspace=workspace)
    session.register_prepared_asset(candidate)

    import auto_foundry_core.prepared as prepared_module

    original_atomic_write = prepared_module._atomic_write
    crashed = {"value": False}

    def fail_index_once(path: Path, content: str) -> None:
        if path == registry.index_path and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("index publication crash")
        original_atomic_write(path, content)

    monkeypatch.setattr(prepared_module, "_atomic_write", fail_index_once)
    with pytest.raises(RuntimeError, match="index publication crash"):
        _commit(session)
    assert registry.search(prepared_asset_id=candidate.prepared_asset_id) == (candidate,)
    assert workspace.integration_state == "pending"
    monkeypatch.setattr(prepared_module, "_atomic_write", original_atomic_write)

    manifest = _commit(session)
    assert manifest["status"] == "committed"
    assert workspace.integration_state == "integrated"
    index = json.loads(registry.index_path.read_text(encoding="utf-8"))
    assert [entry["prepared_asset_id"] for entry in index["entries"]] == [candidate.prepared_asset_id]


def test_accepted_bundle_metadata_is_deeply_immutable(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, _lem, _registry, session = _session(context, value={"nested": ["answer"]})
    bundle = AcceptedAnalysisBundle.load(workspace)

    assert isinstance(bundle.acceptance_envelope["accepted_refs"], tuple)
    with pytest.raises(TypeError):
        bundle.acceptance_envelope["accepted_refs"] = ("tampered",)  # type: ignore[index]
    with pytest.raises(TypeError):
        bundle.manifest["artifact_progress"]["hashes"]["tampered"] = "0" * 64  # type: ignore[index]
    with pytest.raises(AttributeError):
        bundle.manifest["artifact_progress"]["files"].append("tampered")  # type: ignore[union-attr]

    session.add_claim({"claim": "still bound"}, scope="question", evidence_refs=("answer_content.json",))
    _commit(session)


def test_rejected_or_item_technical_failure_cannot_register_prepared_asset(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    RunLifecycle.create(context, ("Q-REJECTED", "Q-FAILED"))
    rejected = ItemWorkspace.create(context, "Q-REJECTED", original_text="rejected")
    rejected.write_plan({"item_id": rejected.item_id})
    rejected.write_draft({"answer": "no"})
    rejected.record_data_insufficiency_conclusion(
        DataInsufficiencyConclusion(
            reason="rejected for fixture test",
            direct_answer_component="answer",
            missing_data=("authoritative source",),
            searches_tests=("checked fixture",),
            evidence_refs=("work/plan.json",),
        ).to_dict(),
        owner_ref="owner-Q-REJECTED",
    )
    rejected.record_review("confirm_data_insufficiency", reviewer_ref="synthetic-reviewer")
    rejected.finalize_blocked_by_evidence()
    with pytest.raises((ValueError, FileExistsError)):
        rejected.accept(accepted_refs=("work/plan.json",))
    registry = PreparedAssetRegistry(context)
    assert registry.search() == ()

    failed = ItemWorkspace.create(context, "Q-FAILED", original_text="failed")
    failed.write_plan({"item_id": failed.item_id})
    failed.write_draft({"answer": "failed"})
    failed.technical_failure("unrecoverable", recovery_exhausted=True)
    assert registry.search() == ()
    with pytest.raises(ValueError):
        IntegrationSession.create(context, failed, registry, "owner", invocation_id="inv-failed")
    assert registry.search() == ()


def test_prepared_candidate_scope_and_same_id_conflict_fail_before_commit_intent(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace1, lem, registry, session1 = _session(context, "Q-001")
    first = _asset(context, "same-id", scope="exploratory", workspace=workspace1)
    session1.register_prepared_asset(first)
    _commit(session1)
    assert registry.search(prepared_asset_id="same-id", scope="exploratory") == (first,)

    workspace2 = _accepted(context, "Q-002")
    session2 = IntegrationSession.create(context, workspace2, registry, "integration-owner", invocation_id="inv-Q-002")
    conflict = _asset(context, "same-id", scope="superseded", workspace=workspace2)
    with pytest.raises(ValueError, match="different descriptor"):
        session2.register_prepared_asset(conflict)
    assert not (session2.staging_root / "commit_intent.json").exists()
    assert workspace2.integration_state == "pending"
    assert registry.search(prepared_asset_id="same-id", scope="exploratory") == (first,)


def test_prepared_candidate_mutation_after_staging_fails_without_registry_entry(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    candidate = _asset(context, "candidate-mutated", workspace=workspace)
    session.register_prepared_asset(candidate)
    path = Path(candidate.location)
    path.write_bytes(b'{"asset":"tampered"}\n')
    with pytest.raises(ValueError, match="changed|match|byte"):
        _commit(session)
    assert registry.search(prepared_asset_id=candidate.prepared_asset_id) == ()
    assert workspace.integration_state == "pending"


def test_accepted_bundle_mutation_after_staging_is_rejected_before_external_apply(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    session.add_claim({"claim": "bound claim"}, scope="question", evidence_refs=("answer_content.json",))
    accepted_path = workspace.accepted_root / "answer_content.json"
    accepted_path.write_bytes(b'{"answer":"tampered"}\n')
    with pytest.raises(ValueError, match="terminal|bundle|hash"):
        _commit(session)
    assert workspace.integration_state == "pending"
    assert session.lem.ontology == {}
    assert registry.search() == ()


def test_record_shape_and_snapshot_tamper_rejected(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    rid = session.add_claim({"claim": "claim"}, scope="question", evidence_refs=("work/plan.json",))
    value = session.records[0].to_dict()
    value["kind"] = "unknown"
    with pytest.raises(ValueError):
        IntegrationRecord.from_dict(value)
    value = session.records[0].to_dict()
    value["accepted_content_hash"] = "bad"
    with pytest.raises(ValueError):
        IntegrationRecord.from_dict(value)
    snapshot = session.staging_root / "snapshot.json"
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["state"]["records_count"] = 99
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot hash"):
        IntegrationSession.load(context, workspace, registry, "integration-owner", invocation_id="inv-Q-001")


def test_owner_is_single_winner(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace = _accepted(context, "Q-001")
    IntegrationSession.create(context, workspace, PreparedAssetRegistry(context), "owner-a", invocation_id="inv-owner-a")
    with pytest.raises(ValueError, match="owned by another"):
        IntegrationSession.create(context, workspace, PreparedAssetRegistry(context), "owner-b", invocation_id="inv-owner-b")


def test_committed_relationship_can_be_reused_without_lem_growth(tmp_path: Path) -> None:
    """A requirement-local analytical row can reference one prior LEM edge."""

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(tmp_path)
    workspace = second.item_workspace
    second.add_relationship(
        payload,
        scope="requirement",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    assert len(second.lem.relationships) == 1
    validation = second.validate()
    assert validation.valid, validation.errors
    _commit(second)

    assert workspace.integration_state == "integrated"
    assert len(second.lem.relationships) == 1
    assert "REL-PRIOR" in second.lem.relationships
    assert "REL-REQ02-ALIAS" not in second.lem.relationships
    assert "REL-REQ02-ALIAS" not in second.lem.ontology


def test_relationship_reuse_unknown_or_mismatched_is_rejected(tmp_path: Path) -> None:
    context = RunContext("RUN-RELATIONSHIP-REUSE-GUARDS", tmp_path / "run")
    prior = _analytical_relationship("prior-analysis", "orders", "customers")
    _workspace, _lem, registry, first = _session(
        context,
        item_id="Q-001",
        analytical_relationships=(prior,),
    )
    for item_id in ("orders", "customers"):
        first.add_ontology_item(
            OntologyItem(item_id=item_id, item_type="entity", label=item_id.title(), scope="question"),
            evidence_refs=("work/plan.json",),
        )
    first.add_relationship(
        _staged_relationship(prior, relationship_id="REL-PRIOR"),
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    _commit(first)

    reused = dict(prior)
    reused["relationship_id"] = "reused-analysis"
    _workspace, _lem, _registry, second = _session(
        context,
        item_id="Q-002",
        analytical_relationships=(reused,),
    )
    unknown = _staged_relationship(reused, relationship_id="REL-UNKNOWN")
    unknown["reuse_existing_relationship_id"] = "REL-MISSING"
    with pytest.raises(ValueError, match="unknown committed relationship"):
        second.add_relationship(
            unknown,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    mismatch = _staged_relationship(reused, relationship_id="REL-MISMATCH")
    mismatch["reuse_existing_relationship_id"] = "REL-PRIOR"
    mismatch["limitations"] = ["changed limitation"]
    with pytest.raises(ValueError, match="semantics do not match"):
        second.add_relationship(
            mismatch,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )


def test_relationship_reuse_can_be_applied_via_fidelity_correction(tmp_path: Path) -> None:
    """The public repair_once/correct_record route can remove an alias edge."""

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(tmp_path)
    payload.pop("reuse_existing_relationship_id", None)
    payload["relationship_id"] = "REL-ALIAS"
    alias_id = second.add_relationship(
        payload,
        scope="requirement",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    records = tuple(record.record_id for record in second.records)
    second.record_fidelity_review(
        "repair_once",
        affected_record_ids=(alias_id,),
        checked_record_ids=records,
    )
    corrected = dict(next(record.payload for record in second.records if record.record_id == alias_id))
    corrected["reuse_existing_relationship_id"] = "REL-PRIOR"
    second.correct_record(alias_id, corrected)
    assert second.validate().valid
    second.build_fidelity_packet()
    second.record_fidelity_review("accept", checked_record_ids=(alias_id,))
    _commit(second)
    assert len(second.lem.relationships) == 1
    assert "REL-ALIAS" not in second.lem.relationships
    assert "REL-ALIAS" not in second.lem.ontology


def test_req12_relationship_reuse_allows_item_local_metadata_and_additive_evidence(tmp_path: Path) -> None:
    """REQ-12-style reuse preserves prior evidence while adding current proof."""

    _context, selection_ref, second, payload, _registry = _semantic_reuse_fixture(tmp_path)
    workspace = second.item_workspace
    second.add_relationship(
        payload,
        scope="requirement",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    assert second.validate().valid
    _commit(second)
    assert workspace.integration_state == "integrated"
    assert tuple(second.lem.relationships) == ("REL-PRIOR",)
    assert "REL-REQ12-ALIAS" not in second.lem.ontology
    assert selection_ref.selection_ref in payload["evidence_refs"]


def _semantic_reuse_fixture(
    tmp_path: Path,
    *,
    journal_item_id: str | None = None,
    journal_snapshot_hash: str | None = None,
    journal_selection_ref: str | None = None,
    wrong_snapshot: bool = False,
    payload_selection_ref: str | None = None,
    selection_relationship_ids: tuple[str, ...] = ("REL-PRIOR",),
    inherited_selection_relationship_ids: tuple[str, ...] | None = None,
    include_current_selection: bool = True,
    historical_records: tuple[dict[str, object], ...] = (),
    with_history: bool = False,
    reference_historical: bool = False,
    include_no_selection_history: bool = False,
    journal_selection_id_override: str | None = None,
    tamper_journal_purpose: bool = False,
    duplicate_current: bool = False,
    malformed_journal: bool = False,
):
    """Return a REQ-12-shaped reuse session with an accepted semantic scope."""

    context = RunContext("RUN-REQ12-SEMANTIC-REUSE", tmp_path / "run")
    prior = _analytical_relationship("prior-analysis", "orders", "customers")
    inherited_selection_ref: str | None = None
    if inherited_selection_relationship_ids is not None:
        _inherited_snapshot, inherited_selection = _semantic_snapshot_and_selection(
            context,
            inherited_selection_relationship_ids,
        )
        inherited_selection_ref = inherited_selection.selection_ref
        prior["evidence_refs"] = [*prior["evidence_refs"], inherited_selection_ref]
    _workspace, _lem, registry, first = _session(
        context,
        item_id="Q-001",
        analytical_relationships=(prior,),
    )
    for item_id in ("orders", "customers"):
        first.add_ontology_item(
            OntologyItem(item_id=item_id, item_type="entity", label=item_id.title(), scope="question"),
            evidence_refs=("work/plan.json",),
        )
    prior_payload = _staged_relationship(prior, relationship_id="REL-PRIOR")
    first.add_relationship(
        prior_payload,
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    _commit(first)

    historical_entries = list(historical_records)
    historical_selection_ref: str | None = None
    if with_history:
        historical_snapshot, historical_selection = _semantic_snapshot_and_selection(context, ("REL-HISTORY",))
        historical_selection_ref = historical_selection.selection_ref
        historical_record = {
            "record_kind": "semantic_selection",
            "item_id": "Q-002",
            "owner_ref": "analytical-owner-Q-002",
            "selection_kind": "semantic_scope",
            "selection_ref": historical_selection.selection_ref,
            "selection_hash": historical_selection.selection_hash,
            "selection_counts": dict(historical_selection.counts),
            "purpose": "historical semantic scope",
            "snapshot_hash": historical_snapshot.snapshot_hash,
            "context_manifest_hash": "a" * 64,
            "registry_hash": None,
        }
        historical_record["selection_id"] = _semantic_selection_journal_hash(historical_record)
        historical_entries.append(historical_record)
        if include_no_selection_history:
            empty_record = {
                "record_kind": "semantic_selection",
                "item_id": "Q-002",
                "owner_ref": "analytical-owner-Q-002",
                "selection_kind": "semantic_scope",
                "selection_ref": None,
                "selection_hash": None,
                "selection_counts": {
                    "ontology_ids": 0,
                    "relationship_ids": 0,
                    "identity_decision_ids": 0,
                    "mapping_ids": 0,
                    "prepared_asset_ids": 0,
                },
                "purpose": "historical empty semantic scope",
                "snapshot_hash": historical_snapshot.snapshot_hash,
                "context_manifest_hash": "a" * 64,
                "registry_hash": None,
            }
            empty_record["selection_id"] = _semantic_selection_journal_hash(empty_record)
            historical_entries.append(empty_record)
    snapshot_ref, selection_ref = _semantic_snapshot_and_selection(context, selection_relationship_ids)
    context_snapshot_ref = snapshot_ref
    if wrong_snapshot:
        context_snapshot_ref, _ = _semantic_snapshot_and_selection(context, ("REL-OTHER",))
    current = dict(prior)
    current_evidence_refs = ["work/plan.json", "work/analytical_relationships.jsonl"]
    if inherited_selection_ref is not None:
        current_evidence_refs.append(inherited_selection_ref)
    if include_current_selection:
        current_evidence_refs.append(
            payload_selection_ref
            or (historical_selection_ref if reference_historical else selection_ref.selection_ref)
        )
    current.update(
        {
            "relationship_id": "req12-reuse-analysis",
            "owner_ref": "analytical-owner-REQ-12",
            "audit_id": "audit-REQ-12",
            "evidence_refs": current_evidence_refs,
        }
    )
    workspace = _accepted_with_semantic_selection(
        context,
        "Q-002",
        (current,),
        context_snapshot_ref,
        selection_ref,
        journal_item_id=journal_item_id,
        journal_snapshot_hash=journal_snapshot_hash,
        journal_selection_ref=journal_selection_ref,
        historical_records=tuple(historical_entries),
        duplicate_current=duplicate_current,
        malformed_journal=malformed_journal,
        journal_selection_id_override=journal_selection_id_override,
        tamper_journal_purpose=tamper_journal_purpose,
    )
    second = IntegrationSession.create(
        context,
        workspace,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-Q-002",
    )
    payload = _staged_relationship(current, relationship_id="REL-REQ12-ALIAS")
    payload["reuse_existing_relationship_id"] = "REL-PRIOR"
    return context, selection_ref, second, payload, registry


def test_req12_relationship_reuse_accepts_hash_bound_semantic_selection(tmp_path: Path) -> None:
    """A current semantic-store selection is accepted without LEM growth."""

    _context, selection_ref, second, payload, _registry = _semantic_reuse_fixture(tmp_path)
    second.add_relationship(
        payload,
        scope="requirement",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    assert second.validate().valid
    _commit(second)
    assert tuple(second.lem.relationships) == ("REL-PRIOR",)
    assert "REL-REQ12-ALIAS" not in second.lem.relationships
    assert selection_ref.selection_ref in payload["evidence_refs"]


def test_relationship_reuse_requires_selected_target_membership(tmp_path: Path) -> None:
    """A valid current selection for another edge cannot authorize reuse."""

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "unrelated",
        selection_relationship_ids=("REL-OTHER",),
    )
    with pytest.raises(ValueError, match="does not include committed relationship"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )


def test_relationship_reuse_requires_current_semantic_selection(tmp_path: Path) -> None:
    """A reuse marker without a current semantic selection is rejected."""

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "missing-selection",
    )
    payload["evidence_refs"] = ["work/plan.json", "work/analytical_relationships.jsonl"]
    with pytest.raises(ValueError, match="requires a current semantic selection"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )


def test_relationship_reuse_validates_only_added_semantic_selection_evidence(tmp_path: Path) -> None:
    """Inherited prior selections stay authoritative while current additions authorize reuse."""

    _context, selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "inherited-and-current",
        inherited_selection_relationship_ids=("REL-PRIOR", "REL-HISTORY"),
    )
    inherited_ref = next(
        value
        for value in second.lem.relationships["REL-PRIOR"]["evidence_refs"]
        if value.startswith("semantic_store/selections/")
    )
    journal = second.item_workspace.work_root / "semantic_selections.jsonl"
    assert inherited_ref not in journal.read_text(encoding="utf-8")
    assert inherited_ref != selection_ref.selection_ref
    second.add_relationship(
        payload,
        scope="requirement",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "inherited-only",
        inherited_selection_relationship_ids=("REL-PRIOR", "REL-HISTORY"),
        include_current_selection=False,
    )
    with pytest.raises(ValueError, match="requires a current semantic selection"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "missing-inherited",
        inherited_selection_relationship_ids=("REL-PRIOR", "REL-HISTORY"),
    )
    inherited_ref = next(
        value
        for value in second.lem.relationships["REL-PRIOR"]["evidence_refs"]
        if value.startswith("semantic_store/selections/")
    )
    payload["evidence_refs"].remove(inherited_ref)
    with pytest.raises(ValueError, match="cannot remove committed evidence refs"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "substituted-inherited",
        inherited_selection_relationship_ids=("REL-PRIOR", "REL-HISTORY"),
    )
    inherited_ref = next(
        value
        for value in second.lem.relationships["REL-PRIOR"]["evidence_refs"]
        if value.startswith("semantic_store/selections/")
    )
    payload["evidence_refs"].remove(inherited_ref)
    payload["evidence_refs"].insert(0, "semantic_store/selections/" + ("b" * 64) + ".json")
    with pytest.raises(ValueError, match="cannot remove committed evidence refs"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )


def test_semantic_selection_evidence_rejects_tamper_and_symlink(tmp_path: Path) -> None:
    context, selection_ref, second, payload, _registry = _semantic_reuse_fixture(tmp_path)
    selection_path = context.resolve_run_path(selection_ref.selection_ref)
    original = selection_path.read_bytes()
    selection_path.write_bytes(original + b"tampered")
    with pytest.raises(ValueError, match="canonical|hash|unreadable"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    selection_path.write_bytes(original)
    copy_path = selection_path.with_name("selection-copy.json")
    copy_path.write_bytes(original)
    selection_path.unlink()
    selection_path.symlink_to(copy_path)
    with pytest.raises(ValueError, match="regular file|symlink"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )


def test_semantic_selection_evidence_rejects_wrong_context_and_unbound_ref(tmp_path: Path) -> None:
    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path,
        journal_item_id="Q-OTHER",
    )
    with pytest.raises(ValueError, match="not bound exactly once"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "unbound",
        journal_selection_ref="semantic_store/selections/" + ("a" * 64) + ".json",
    )
    with pytest.raises(ValueError, match="non-canonical|selection journal"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )


def test_semantic_selection_evidence_rejects_wrong_snapshot_and_path(tmp_path: Path) -> None:
    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path,
        wrong_snapshot=True,
    )
    with pytest.raises(ValueError, match="binding|snapshot"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(tmp_path / "path")
    payload["evidence_refs"] = ["work/plan.json", "work/analytical_relationships.jsonl", "semantic_store/selections/not-a-hash.json"]
    with pytest.raises(ValueError, match="canonical"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )


def test_semantic_selection_history_requires_one_current_binding(tmp_path: Path) -> None:
    """Historical selections remain readable, but reuse binds only current context."""

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "history",
        with_history=True,
        include_no_selection_history=True,
    )
    second.add_relationship(
        payload,
        scope="requirement",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "stale",
        with_history=True,
        reference_historical=True,
    )
    with pytest.raises(ValueError, match="not bound exactly once"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "duplicate",
        duplicate_current=True,
    )
    with pytest.raises(ValueError, match="not bound exactly once"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "malformed",
        malformed_journal=True,
    )
    with pytest.raises(ValueError, match="journal line .* invalid"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "arbitrary-id",
        journal_selection_id_override="f" * 64,
    )
    with pytest.raises(ValueError, match="selection_id"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    _context, _selection_ref, second, payload, _registry = _semantic_reuse_fixture(
        tmp_path / "tampered-field",
        tamper_journal_purpose=True,
    )
    with pytest.raises(ValueError, match="selection_id"):
        second.add_relationship(
            payload,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )


def test_relationship_reuse_rejects_evidence_deletion_and_unbound_addition(tmp_path: Path) -> None:
    context = RunContext("RUN-RELATIONSHIP-REUSE-EVIDENCE-GUARDS", tmp_path / "run")
    prior = _analytical_relationship("prior-analysis", "orders", "customers")
    _workspace, _lem, registry, first = _session(
        context,
        item_id="Q-001",
        analytical_relationships=(prior,),
    )
    for item_id in ("orders", "customers"):
        first.add_ontology_item(
            OntologyItem(item_id=item_id, item_type="entity", label=item_id.title(), scope="question"),
            evidence_refs=("work/plan.json",),
        )
    first.add_relationship(
        _staged_relationship(prior, relationship_id="REL-PRIOR"),
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    _commit(first)

    current = dict(prior)
    current["relationship_id"] = "req12-reuse-analysis"
    _workspace, _lem, _registry, second = _session(
        context,
        item_id="Q-002",
        analytical_relationships=(current,),
    )
    missing_prior = _staged_relationship(current, relationship_id="REL-MISSING-EVIDENCE")
    missing_prior["reuse_existing_relationship_id"] = "REL-PRIOR"
    missing_prior["evidence_refs"] = ["answer_content.json"]
    with pytest.raises(ValueError, match="remove committed evidence refs"):
        second.add_relationship(
            missing_prior,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )

    unbound = _staged_relationship(current, relationship_id="REL-UNBOUND-EVIDENCE")
    unbound["reuse_existing_relationship_id"] = "REL-PRIOR"
    unbound["evidence_refs"] = ["work/plan.json", "work/not-accepted.json"]
    with pytest.raises(ValueError, match="not bound by accepted manifest"):
        second.add_relationship(
            unbound,
            scope="requirement",
            evidence_refs=("work/analytical_relationships.jsonl",),
        )
