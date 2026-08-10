"""Offline mechanical tests for accepted-result integration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from auto_foundry_core.contracts import OntologyItem, PreparedAssetDescriptor
from auto_foundry_core.durable import ItemWorkspace, _json_bytes, _manifest_hash
from auto_foundry_core.enterprise_model import LivingEnterpriseModel
from auto_foundry_core.integration import (
    AcceptedAnalysisBundle,
    IntegrationRecord,
    IntegrationSession,
)
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.workspace import RunContext


def _accepted(context: RunContext, item_id: str, value: object | None = None) -> ItemWorkspace:
    workspace = ItemWorkspace.create(context, item_id, original_text=f"question {item_id}")
    workspace.write_plan({"item_id": item_id, "offline": True})
    workspace.write_draft(value if value is not None else {"answer": item_id, "opaque": [1, 2]})
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    workspace.accept(accepted_refs=("work/plan.json",))
    return workspace


def _asset(context: RunContext, asset_id: str, *, scope: str = "reusable") -> PreparedAssetDescriptor:
    path = context.run_root / "prepared" / f"{asset_id}.jsonl"
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


def _session(context: RunContext, item_id: str = "Q-001", value: object | None = None):
    workspace = _accepted(context, item_id, value)
    lem = LivingEnterpriseModel(run_id=context.run_id)
    registry = PreparedAssetRegistry(context)
    session = IntegrationSession.create(context, workspace, lem, registry, "integration-owner")
    return workspace, lem, registry, session


def test_accepted_bundle_is_opaque_and_evidence_short_ref_is_accepted(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context, value={"exact": ["bytes", 7]})
    before = (workspace.accepted_root / "answer_content.json").read_bytes()
    assert AcceptedAnalysisBundle.load(workspace).answer_content == before
    record_id = session.add_claim("opaque claim", scope="question", evidence_refs=("answer_content.json",))
    assert session.records[0].evidence_hashes["answer_content.json"] == hashlib.sha256(before).hexdigest()
    session.commit()
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
    workspace, lem, registry, session = _session(context)
    source_id = session.add_ontology_item(
        OntologyItem(item_id="customer", item_type="entity", label="Customer", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    target_id = session.add_ontology_item(
        OntologyItem(item_id="order", item_type="entity", label="Order", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    metric_id = session.add_metric({"item_id": "orders_metric", "label": "Orders", "value": 3}, scope="question", evidence_refs=("work/plan.json",))
    limitation_id = session.add_limitation("synthetic limit", scope="question", evidence_refs=("work/plan.json",))
    session.add_relationship(
        {"relationship_id": "customer_order", "source_id": "customer", "target_id": "order", "label": "places"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    session.add_dashboard_fact({"fact": "orders_metric", "value": 3}, scope="question", evidence_refs=("work/plan.json",))
    session.link_evidence(metric_id, ("work/plan.json",), scope="question")
    before = session.records[0].record_hash
    session.correct_record(source_id, {"item_id": "customer", "item_type": "entity", "label": "Customer corrected", "scope": "question"})
    assert session.records[0].record_hash != before
    assert session.validate().valid
    manifest = session.commit()
    assert manifest["records_count"] == 7
    assert set(("customer", "order", "orders_metric")).issubset(lem.ontology)
    assert limitation_id in lem.knowledge
    assert workspace.integration_manifest_hash == manifest["manifest_hash"]
    assert "order" in {record.payload.get("item_id") for record in session.records if record.kind == "ontology_item"}


def test_relationship_refs_include_prior_staged_metrics_and_relationship_ontology(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, _registry, session = _session(context)
    session.add_ontology_item(
        OntologyItem(item_id="customer", item_type="entity", label="Customer", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    session.add_metric({"item_id": "orders_metric", "label": "Orders"}, scope="question", evidence_refs=("work/plan.json",))
    session.add_relationship(
        {"relationship_id": "customer_orders", "source_id": "customer", "target_id": "orders_metric"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    session.add_relationship(
        {"relationship_id": "customer_orders_fact", "source_id": "customer_orders", "target_id": "customer"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    assert session.validate().valid
    session.commit()
    assert {"orders_metric", "customer_orders", "customer_orders_fact"}.issubset(lem.ontology)


def test_relationship_forward_or_unknown_refs_fail_closed(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    _workspace, _lem, _registry, session = _session(context)
    session.add_ontology_item(
        OntologyItem(item_id="customer", item_type="entity", label="Customer", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    session.add_relationship(
        {"relationship_id": "future_ref", "source_id": "customer", "target_id": "future_metric"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    session.add_metric({"item_id": "future_metric", "label": "Future"}, scope="question", evidence_refs=("work/plan.json",))
    assert not session.validate().valid
    with pytest.raises(ValueError, match="validation"):
        session.commit()


def test_preflight_collision_has_no_intent_or_partial_lem_mutation(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    lem.add_ontology_item(OntologyItem(item_id="m", item_type="metric", label="existing"))
    session.add_metric({"item_id": "m", "label": "different"}, scope="question", evidence_refs=("work/plan.json",))
    with pytest.raises(ValueError, match="collision"):
        session.commit()
    assert not (session.staging_root / "commit_intent.json").exists()
    assert len(lem.ontology) == 1
    assert workspace.integration_state == "pending"


def test_staging_snapshot_reconciles_projection_crashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace = _accepted(context, "Q-001")
    lem = LivingEnterpriseModel(run_id=context.run_id)
    registry = PreparedAssetRegistry(context)
    import auto_foundry_core.integration as integration_module

    original_json = integration_module._atomic_write_json
    def fail_session(path: Path, value: object) -> None:
        if path.name == "session.json":
            raise RuntimeError("crash after authoritative snapshot")
        original_json(path, value)
    monkeypatch.setattr(integration_module, "_atomic_write_json", fail_session)
    with pytest.raises(RuntimeError):
        IntegrationSession.create(context, workspace, lem, registry, "integration-owner")
    monkeypatch.setattr(integration_module, "_atomic_write_json", original_json)
    reloaded = IntegrationSession.create(context, workspace, lem, registry, "integration-owner")
    assert reloaded.status == "open"
    assert (reloaded.staging_root / "session.json").is_file()

    rid = reloaded.add_claim("claim", scope="question", evidence_refs=("work/plan.json",))
    original_bytes = integration_module._atomic_write_bytes
    def fail_records(path: Path, payload: bytes) -> None:
        if path.name == "records.jsonl":
            raise RuntimeError("crash after snapshot")
        original_bytes(path, payload)
    monkeypatch.setattr(integration_module, "_atomic_write_bytes", fail_records)
    with pytest.raises(RuntimeError):
        reloaded.add_limitation("later", scope="question", evidence_refs=("work/plan.json",))
    monkeypatch.setattr(integration_module, "_atomic_write_bytes", original_bytes)
    recovered = IntegrationSession.load(context, workspace, lem, registry, "integration-owner")
    assert {record.record_id for record in recovered.records} == {rid, next(record.record_id for record in reloaded.records if record.kind == "limitation")}


def test_intent_before_apply_and_retry_after_publish_or_item_state_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    session.add_claim("claim", scope="question", evidence_refs=("work/plan.json",))
    original_apply = session._apply_records
    monkeypatch.setattr(session, "_apply_records", lambda: (_ for _ in ()).throw(RuntimeError("apply crash")))
    with pytest.raises(RuntimeError):
        session.commit()
    assert (session.staging_root / "commit_intent.json").is_file()
    assert workspace.integration_state == "pending"
    monkeypatch.setattr(session, "_apply_records", original_apply)
    manifest = session.commit()
    assert workspace.integration_state == "integrated"
    assert session.commit()["manifest_hash"] == manifest["manifest_hash"]

    workspace2, lem2, registry2, session2 = _session(context, "Q-002")
    session2.add_claim("claim", scope="question", evidence_refs=("work/plan.json",))
    original_publish = session2._write_committed
    monkeypatch.setattr(session2, "_write_committed", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("publish crash")))
    with pytest.raises(RuntimeError):
        session2.commit()
    monkeypatch.setattr(session2, "_write_committed", original_publish)
    assert session2.commit()["status"] == "committed"
    assert workspace2.integration_state == "integrated"


def test_partial_external_apply_retries_without_duplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    first = _asset(context, "asset-first")
    session.register_prepared_asset(first)
    session.add_claim("later", scope="question", evidence_refs=("work/plan.json",))
    original_apply = session._apply_lem_record
    raised = {"value": False}

    def flaky(record):
        if record.kind == "claim" and not raised["value"]:
            raised["value"] = True
            raise RuntimeError("later apply crash")
        return original_apply(record)

    monkeypatch.setattr(session, "_apply_lem_record", flaky)
    with pytest.raises(RuntimeError):
        session.commit()
    assert registry.search(prepared_asset_id="asset-first")
    assert workspace.integration_state == "pending"
    monkeypatch.setattr(session, "_apply_lem_record", original_apply)
    manifest = session.commit()
    assert manifest["status"] == "committed"
    assert len(registry.search(prepared_asset_id="asset-first")) == 1


def test_published_then_item_mark_crash_rebinds_on_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    session.add_claim("claim", scope="question", evidence_refs=("work/plan.json",))
    original_mark = workspace.mark_integration_committed
    raised = {"value": False}

    def flaky_mark(*args, **kwargs):
        if not raised["value"]:
            raised["value"] = True
            raise RuntimeError("item mark crash")
        return original_mark(*args, **kwargs)

    monkeypatch.setattr(workspace, "mark_integration_committed", flaky_mark)
    with pytest.raises(RuntimeError):
        session.commit()
    assert session.committed_manifest_path.is_file()
    assert workspace.integration_state == "pending"
    monkeypatch.setattr(workspace, "mark_integration_committed", original_mark)
    manifest = session.commit()
    assert workspace.integration_state == "integrated"
    assert workspace.integration_manifest_hash == manifest["manifest_hash"]


def test_integrated_then_staging_persistence_crash_converges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    session.add_claim("claim", scope="question", evidence_refs=("work/plan.json",))
    original_persist = session._persist_state
    raised = {"value": False}

    def flaky_persist(state):
        if state.get("status") == "committed" and not raised["value"]:
            raised["value"] = True
            raise RuntimeError("staging persistence crash")
        return original_persist(state)

    monkeypatch.setattr(session, "_persist_state", flaky_persist)
    with pytest.raises(RuntimeError):
        session.commit()
    assert workspace.integration_state == "integrated"
    monkeypatch.setattr(session, "_persist_state", original_persist)
    manifest = session.commit()
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
    reusable = _asset(context, "asset-reusable", scope="reusable")
    scoped = _asset(context, "asset-scoped", scope="requirement_scoped")
    session1.register_prepared_asset(reusable)
    session1.register_prepared_asset(scoped)
    session1.commit()
    assert registry.load("asset-reusable").rows == ({"asset": "asset-reusable"},)
    assert registry.search(reusable_only=True, prepared_asset_id="asset-reusable")
    assert not registry.search(reusable_only=True, prepared_asset_id="asset-scoped")
    assert registry.search(prepared_asset_id="asset-scoped")

    workspace2 = _accepted(context, "Q-002")
    session2 = IntegrationSession.create(context, workspace2, lem, registry, "integration-owner")
    session2.add_ontology_item(OntologyItem(item_id="item-2", item_type="entity", label="Second", scope="question"), evidence_refs=("work/plan.json",))
    session2.commit()
    assert "item-2" in lem.ontology
    assert workspace1.accepted_root.joinpath("answer_content.json").read_bytes()


def test_record_shape_and_snapshot_tamper_rejected(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace, lem, registry, session = _session(context)
    rid = session.add_claim("claim", scope="question", evidence_refs=("work/plan.json",))
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
        IntegrationSession.load(context, workspace, lem, registry, "integration-owner")


def test_owner_is_single_winner(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace = _accepted(context, "Q-001")
    IntegrationSession.create(context, workspace, LivingEnterpriseModel(run_id="RUN"), PreparedAssetRegistry(context), "owner-a")
    with pytest.raises(ValueError, match="owned by another"):
        IntegrationSession.create(context, workspace, LivingEnterpriseModel(run_id="RUN"), PreparedAssetRegistry(context), "owner-b")
