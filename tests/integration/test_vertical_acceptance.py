"""One complete, generic offline proof of the normal runtime/product path.

The fixture intentionally contains no business dataset and no model/agent call.
Semantic role outputs are represented by small typed records so this test can
prove the wiring between the real filesystem, ``RunContext``, ``CoreRuntime``,
the LEM, and the two local product helpers.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SCRIPTS = ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dashboard_renderer = _load("vertical_dashboard_renderer", SCRIPTS / "dashboard_renderer.py")
evidence_collector = _load("vertical_optimizer_evidence_collector", SCRIPTS / "optimizer_evidence_collector.py")

from auto_foundry_core import (  # noqa: E402
    CanonicalMapping,
    CoreExecutionResult,
    CoreRuntime,
    DataAssetRef,
    FoundationTask,
    IdentityCandidate,
    IdentityDecision,
    KnowledgeDelta,
    LEMRef,
    LivingEnterpriseModel,
    OperationResultRef,
    OperationSpec,
    OntologyItem,
    PreparedAssetDescriptor,
    RequirementRecord,
    RunContext,
    apply_decision,
)
from auto_foundry_core.artifacts import hash_value  # noqa: E402
from auto_foundry_core.workspace import AllowedRootError  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _asset(value: object) -> DataAssetRef:
    if isinstance(value, DataAssetRef):
        return value
    assert isinstance(value, dict)
    return DataAssetRef.from_dict(value)


def _result_ref(value: object) -> OperationResultRef:
    if isinstance(value, OperationResultRef):
        return value
    assert isinstance(value, dict)
    return OperationResultRef.from_dict(value)


def test_complete_generic_offline_vertical_path(tmp_path: Path) -> None:
    input_root = tmp_path / "input-fixture"
    run_root = tmp_path / "run-micro"
    sibling_root = tmp_path / "sibling-run"
    input_root.mkdir()
    sibling_root.mkdir()

    ledger = input_root / "ledger.csv"
    entities = input_root / "entities.jsonl"
    events = input_root / "events.jsonl"
    ledger.write_text(
        "entity_id,amount,event_date\nleft-1,10,2026-01-01\nleft-2,20,2026-01-02\n",
        encoding="utf-8",
    )
    entities.write_text(
        '{"entity_id":"left-1","label":"North","observed_date":"2026-01-01"}\n'
        '{"entity_id":"left-2","label":"South","observed_date":"2026-01-02"}\n',
        encoding="utf-8",
    )
    events.write_text(
        '{"entity_id":"left-1","kind":"sale","event_date":"2026-01-03"}\n'
        '{"entity_id":"left-2","kind":"sale","event_date":"2026-01-04"}\n',
        encoding="utf-8",
    )
    sibling_source = sibling_root / "outside.json"
    sibling_source.write_text('[{"entity_id":"outside"}]\n', encoding="utf-8")
    source_hashes_before = {path: _sha256(path) for path in (ledger, entities, events, sibling_source)}

    context = RunContext("RUN-MICRO", run_root, (input_root,))
    runtime = CoreRuntime(context)
    assert isinstance(runtime, CoreRuntime)
    assert runtime.context is context
    assert isinstance(CoreExecutionResult, type)

    # The run boundary rejects a sibling before probing it, and the integrated
    # runtime reports the same technical error through its passive receipt.
    with pytest.raises(AllowedRootError):
        context.resolve_input(sibling_source)
    with pytest.raises(AllowedRootError) as escaped:
        runtime.execute(OperationSpec("sources.preview", parameters={"path": str(sibling_source), "limit": 1}))
    assert getattr(escaped.value, "receipt").errors
    assert context.resolve_run_path("products") == run_root / "products"  # explicit run-relative resolution
    with pytest.raises(AllowedRootError):
        context.resolve_run_path(sibling_root / "output.json")

    # Three real source registrations and a profile/normalization all go
    # through CoreRuntime; the source references retain immutable hashes.
    registered: dict[str, DataAssetRef] = {}
    for source in (ledger, entities, events):
        execution = runtime.execute(OperationSpec("sources.register", parameters={"path": source.name}))
        registered[source.name] = _asset(execution.value)
        assert execution.receipt.capability_id == "sources.register"
        assert execution.receipt.input_hashes
        assert registered[source.name].content_hash == _sha256(source)
    profile = runtime.execute(
        OperationSpec(
            "profiling.profile",
            parameters={"path": registered["entities.jsonl"].to_dict(), "sample_limit": 10},
        )
    )
    assert profile.receipt.capability_id == "profiling.profile"
    assert profile.value["row_count"] == 2
    normalized = runtime.execute(
        OperationSpec(
            "normalization.normalize",
            parameters={
                "rows": [{"event_date": "03/01/2026", "amount": "10"}],
                "fields": {
                    "event_date": {"kind": "date", "formats": ["%d/%m/%Y"]},
                    "amount": "number",
                },
                "return_metadata": True,
            },
        )
    )
    assert normalized.receipt.capability_id == "normalization.normalize"
    assert normalized.value["rows"][0]["event_date_normalized"] == "2026-01-03"
    assert normalized.value["rows"][0]["amount_normalized"] == 10.0
    assert normalized.value["rows"][0]["event_date"] == "03/01/2026"

    # One deterministic operation proves the integrated miss -> hit path and
    # the cache/receipt/telemetry wiring, rather than only testing RunCache.
    preview_spec = OperationSpec("sources.preview", parameters={"path": "ledger.csv", "limit": 2})
    preview_miss = runtime.execute(preview_spec)
    preview_hit = runtime.execute(preview_spec)
    assert preview_miss.cache_status == "miss"
    assert preview_hit.cache_status == "hit"
    assert preview_miss.value == preview_hit.value
    assert preview_miss.receipt.input_hashes
    assert preview_hit.receipt.cache_status == "hit"
    assert [event.event_type for event in runtime.telemetry.events].count("cache_miss") >= 1
    assert [event.event_type for event in runtime.telemetry.events].count("cache_hit") >= 1
    operation_events = [event for event in runtime.telemetry.events if event.event_type == "operation"]
    assert any(event.capability_id == "sources.preview" and event.cache_status == "miss" for event in operation_events)
    assert any(event.capability_id == "sources.preview" and event.cache_status == "hit" for event in operation_events)
    assert runtime.telemetry.event_path and runtime.telemetry.event_path.is_file()
    assert not any(
        event.capability_id and event.capability_id.startswith(("agent.", "model."))
        for event in runtime.telemetry.events
    )

    # Two requirements share one foundation task.  These are typed fixture
    # records, not agent outputs or an analytical dataset result.
    review_unavailable = {
        "review_status": "unavailable",
        "review_strength": "none",
        "verdict": "not_reviewed",
    }
    requirements = (
        RequirementRecord(
            "REQ-ALPHA",
            "Summarize the fixture amount evidence.",
            business_objective="fixture amount orientation",
            expected_analytical_outputs=("amount summary",),
            shared_foundation_dependencies=("FOUND-SHARED",),
            ontology_needs=("metric-total", "definition-entity"),
            prepared_data_needs=("shared-id",),
            status="answered_with_limits",
            review=review_unavailable,
            outcome={"status": "answered_with_limits", "review": review_unavailable},
        ),
        RequirementRecord(
            "REQ-BETA",
            "Compare the fixture entity and event relationship.",
            business_objective="fixture relationship orientation",
            expected_analytical_outputs=("relationship diagnostic",),
            shared_foundation_dependencies=("FOUND-SHARED",),
            ontology_needs=("metric-total", "relationship-entity-event"),
            prepared_data_needs=("shared-id",),
            status="answered_with_limits",
            review=review_unavailable,
            outcome={"status": "answered_with_limits", "review": review_unavailable},
        ),
    )
    foundation = FoundationTask(
        "FOUND-SHARED",
        "Register, profile, and normalize the generic fixture sources.",
        supports_requirements=("REQ-ALPHA", "REQ-BETA"),
        capability_ids=("sources.register", "profiling.profile", "normalization.normalize"),
        status="completed",
    )
    assert len(requirements) == 2
    assert foundation.supports_requirements == ("REQ-ALPHA", "REQ-BETA")
    assert foundation.status == "completed"
    assert all(item.review == review_unavailable for item in requirements)

    # Identity evidence is generated by the real capability; only the review
    # decision is a tiny semantic fixture output.
    candidate_execution = runtime.execute(
        OperationSpec(
            "identity.candidates",
            parameters={
                "left_rows": [{"id": "left-1", "label": "North"}],
                "right_rows": [{"id": "right-1", "label": "North"}],
                "object_type": "entity",
                "compare_fields": ["label"],
                "threshold": 0.5,
            },
        )
    )
    candidate = IdentityCandidate.from_dict(candidate_execution.value[0])
    assert candidate.left_id == "left-1"
    assert candidate.right_id == "right-1"
    decision = IdentityDecision(
        candidate.candidate_id,
        "same_object",
        decision_id="decision-micro-entity",
        review_status="reviewed",
        reviewer_ref="fixture-reviewer",
        evidence_refs=("entities.jsonl",),
        rationale="The two fixture labels are intentionally identical.",
        scope="shared",
        canonical_id="canonical-entity-1",
    )
    mapping = apply_decision(candidate, decision)
    assert isinstance(mapping, CanonicalMapping)
    assert mapping.decision_id == decision.decision_id
    assert mapping.metadata["reviewed_trace"]["decision_id"] == decision.decision_id

    # The relationship capability reads two of the registered local sources,
    # including a full-set temporal diagnostic and a bounded pair sample.
    relationship_execution = runtime.execute(
        OperationSpec(
            "relationships.measure",
            parameters={
                "left_rows": "entities.jsonl",
                "right_rows": "events.jsonl",
                "left_key": "entity_id",
                "right_key": "entity_id",
                "left_time_field": "observed_date",
                "right_time_field": "event_date",
                "date_formats": ["%Y-%m-%d"],
                "sample_limit": 1,
            },
        )
    )
    relationship = relationship_execution.value
    assert relationship["overlap_count"] == 2
    assert relationship["total_matched_pair_count"] == 2
    assert relationship["sampled_pair_count"] == 1
    assert relationship["temporal"]["temporal_scope"] == "full"
    assert relationship["temporal"]["sample_coverage"] == 0.5

    model = LivingEnterpriseModel(run_id=context.run_id)
    model.register_identity_decision(decision)
    model.add_mapping(mapping)
    model.add_relationship(
        {
            "relationship_id": "relationship-entity-event",
            "label": "Entity to event",
            "source_id": "entity",
            "target_id": "event",
            "diagnostic": relationship,
            "scope": "shared",
            "effective_period": "2026",
        }
    )

    # The prepared result is a real products file and its descriptor carries
    # enough integrity/provenance information to verify reuse before use.
    prepared_spec = OperationSpec(
        "artifacts.write",
        inputs=(normalized.value["rows"],),
        parameters={
            "filename": "prepared/shared.json",
            "source_refs": [registered["ledger.csv"].to_dict()],
            "data": normalized.value["rows"],
        },
    )
    prepared_execution = runtime.execute(prepared_spec)
    prepared_ref = _result_ref(prepared_execution.value)
    prepared_path = Path(prepared_ref.location)
    assert prepared_path == context.product_root / "prepared" / "shared.json"
    assert prepared_path.is_file()
    operation_manifest = runtime.build_manifest(
        capability_id="artifacts.write",
        operation_spec=prepared_spec,
        inputs=(registered["ledger.csv"],),
        outputs=(prepared_ref,),
        metadata={"fixture": True},
    )
    prepared_descriptor = PreparedAssetDescriptor(
        prepared_asset_id="shared-id",
        source_refs=(registered["ledger.csv"],),
        source_hashes=(registered["ledger.csv"].content_hash or "",),
        location=str(prepared_path),
        schema={"event_date_normalized": "date", "amount_normalized": "number"},
        grain="one fixture row",
        transformations=("explicit date and number normalization",),
        relationship_mappings=("relationship-entity-event",),
        ontology_refs=("metric-total", "definition-entity"),
        scope="reusable",
        effective_period="2026",
        prepared_content_hash=prepared_ref.content_hash,
        operation_manifest_hash=hash_value(operation_manifest),
        core_version=context.core_version,
        row_count=1,
        byte_count=prepared_path.stat().st_size,
        created_at="2026-08-08T00:00:00+00:00",
        as_of="2026-08-08",
    )
    model.register_prepared_asset(prepared_descriptor)
    assert model.verify_prepared_asset_reuse("shared-id") is True
    assert prepared_descriptor.prepared_content_hash == _sha256(prepared_path)
    assert prepared_descriptor.operation_manifest_hash == hash_value(operation_manifest)
    assert prepared_descriptor.row_count == 1
    assert prepared_descriptor.byte_count == prepared_path.stat().st_size

    # Ontology semantics are typed and discoverable.  ``shared-id`` also
    # exists in the prepared registry; typed references keep that collision
    # safe and do not search the other namespace.
    semantic_items = (
        ("metric-total", "metric", "Fixture total amount"),
        ("shared-id", "metric", "Prepared reuse metric"),
        ("definition-entity", "definition", "Fixture entity definition"),
        ("rule-review", "rule", "Fixture review rule"),
        ("process-close", "process", "Fixture close process"),
    )
    for item_id, item_type, label in semantic_items:
        model.apply_delta(
            KnowledgeDelta(
                f"delta-{item_id}",
                f"add_{item_type}",
                {"item_id": item_id, "label": label, "scope": "shared", "effective_period": "2026"},
                accepted=True,
            )
        )
    ontology_types = {item["item_id"]: item["item_type"] for item in model.ontology_index}
    assert ontology_types["metric-total"] == "metric"
    assert ontology_types["shared-id"] == "metric"
    assert ontology_types["definition-entity"] == "definition"
    assert ontology_types["rule-review"] == "rule"
    assert ontology_types["process-close"] == "process"
    assert ontology_types["relationship-entity-event"] == "relationship"
    assert isinstance(model.resolve_ref(LEMRef("ontology", "shared-id")), OntologyItem)
    assert model.resolve_ref(LEMRef("prepared_asset", "shared-id")).prepared_asset_id == "shared-id"
    with pytest.raises(KeyError):
        model.resolve_ref(LEMRef("canonical_mapping", "shared-id"))

    # The second requirement asks for one exact, bounded bundle and reuses the
    # first requirement's prepared asset after an integrity check.
    second_bundle = model.relevant_bundle(
        refs=(
            LEMRef("ontology", "metric-total"),
            LEMRef("ontology", "shared-id"),
            LEMRef("ontology", "definition-entity"),
            LEMRef("ontology", "rule-review"),
            LEMRef("ontology", "process-close"),
            LEMRef("ontology", "relationship-entity-event"),
            LEMRef("prepared_asset", "shared-id"),
        ),
        per_layer_limits={"ontology": 6, "prepared_assets": 1},
        max_total_items=7,
        max_bytes=32_000,
        scope="shared",
        effective_period="2026",
    )
    assert second_bundle["metadata"]["total_count"] == 7
    assert second_bundle["exact_ids"]["ontology"] == [
        "metric-total",
        "shared-id",
        "definition-entity",
        "rule-review",
        "process-close",
        "relationship-entity-event",
    ]
    assert second_bundle["exact_ids"]["prepared_assets"] == ["shared-id"]
    assert model.verify_prepared_asset_reuse(second_bundle["exact_ids"]["prepared_assets"][0]) is True
    assert requirements[1].prepared_data_needs == ("shared-id",)

    # A reviewed-output-only fixture produces a traceable dashboard under the
    # same products subtree; no raw source is passed to the helper.
    fixture_path = run_root / "reviewed_widgets.json"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(
        json.dumps(
            {
                "title": "Micro reviewed fixture",
                "run_id": context.run_id,
                "review_status": "not_reviewed",
                "limitations": ["Reviewer unavailable; values are fixture-only."],
                "domains": [
                    {
                        "id": "fixture-domain",
                        "title": "Fixture domain",
                        "order": 1,
                        "decision_flow": [
                            {
                                "id": "fixture-flow",
                                "title": "Fixture decision flow",
                                "order": 1,
                                "widget_ids": ["amount-kpi", "relationship-table"],
                            }
                        ],
                    }
                ],
                "widgets": [
                    {
                        "id": "amount-kpi",
                        "type": "kpi",
                        "title": "Reviewed amount",
                        "value": "30",
                        "unit": "fixture units",
                        "review_status": "not_reviewed",
                        "reviewed_item_ref": "REQ-ALPHA",
                        "reviewed_output_ref": "requirements/REQ-ALPHA/final.json",
                        "evidence_refs": ["prepared/shared-id"],
                        "trace_refs": ["telemetry/events.jsonl"],
                    },
                    {
                        "id": "relationship-table",
                        "type": "table",
                        "title": "Reviewed relationship",
                        "columns": ["key", "overlap"],
                        "rows": [{"key": "entity-event", "overlap": "2"}],
                        "review_status": "not_reviewed",
                        "reviewed_item_ref": "REQ-BETA",
                        "reviewed_output_ref": "requirements/REQ-BETA/final.json",
                        "evidence_refs": ["relationship-entity-event"],
                        "trace_refs": ["requirements/REQ-BETA/final.json"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    dashboard_manifest = dashboard_renderer.render_fixture(
        context,
        "reviewed_widgets.json",
        "dashboard/micro.html",
        "dashboard/micro_manifest.json",
    )
    dashboard_path = context.product_root / "dashboard" / "micro.html"
    dashboard_manifest_path = context.product_root / "dashboard" / "micro_manifest.json"
    assert dashboard_path.is_file()
    assert dashboard_manifest_path.is_file()
    assert dashboard_manifest["new_analytics"] is False
    assert dashboard_manifest["internal_links_checked"] is True
    assert {item["reviewed_item_ref"] for item in dashboard_manifest["items"]} == {"REQ-ALPHA", "REQ-BETA"}
    assert all(item["trace_anchors"] for item in dashboard_manifest["items"])
    assert "prepared/shared-id" in dashboard_manifest["items"][0]["evidence_refs"]

    # Freeze markers are explicit, and the deterministic optimizer evidence
    # collector reads only run-relative evidence and writes only optimizer/*.
    products_manifest = {
        "run_id": context.run_id,
        "answers_frozen": True,
        "living_enterprise_model_frozen": True,
        "prepared_assets_frozen": True,
        "dashboard_frozen": True,
        "telemetry_frozen": True,
        "review_routing": {"fresh_sol_review_available": False},
        "review": review_unavailable,
    }
    runtime.write_manifest("products/product_manifest.json", products_manifest)
    traces_root = run_root / "traces"
    scripts_root = run_root / "scripts"
    traces_root.mkdir(parents=True, exist_ok=True)
    scripts_root.mkdir(parents=True, exist_ok=True)
    (traces_root / "fixture.md").write_text("repeated read context; capability gap is only a fixture observation\n", encoding="utf-8")
    (scripts_root / "one.py").write_text("print('same fixture script')\n", encoding="utf-8")
    (scripts_root / "two.py").write_text("print('same fixture script')\n", encoding="utf-8")
    products_before = {
        path: _sha256(path)
        for path in (fixture_path, dashboard_path, dashboard_manifest_path, context.run_root / "products" / "product_manifest.json")
    }
    optimizer_result = evidence_collector.collect_evidence(
        context,
        products_manifest="products/product_manifest.json",
        telemetry=["telemetry/events.jsonl"],
        traces=["traces"],
        scripts=["scripts"],
        analytical_inputs=["reviewed_widgets.json", "products/dashboard/micro.html"],
        analytical_complete=True,
    )
    assert optimizer_result["optimizer_status"] == "complete"
    assert optimizer_result["analytical_complete"] is True
    assert optimizer_result["input_hashes_unchanged"] is True
    assert optimizer_result["exact_duplicate_groups"] == [["scripts/one.py", "scripts/two.py"]]
    assert (run_root / "optimizer" / "optimizer_evidence_bundle.md").is_file()
    assert (run_root / "optimizer" / "optimizer_evidence_appendix.md").is_file()
    assert products_before == {path: _sha256(path) for path in products_before}
    non_blocking = evidence_collector.collect_evidence_non_blocking(
        context,
        products_manifest="products/missing.json",
        analytical_complete=True,
    )
    assert non_blocking["optimizer_status"] == "technical_failure"
    assert non_blocking["analytical_complete"] is True

    # Export terminal state under the run root, then prove deterministic
    # calculations remain callable after terminalization.
    terminal_state = {
        "run_id": context.run_id,
        "status": "completed",
        "lifecycle_state": "completed",
        "terminal": True,
        "requirements": {item.requirement_id: item.status for item in requirements},
        "foundation_task": foundation.to_dict(),
        "review": review_unavailable,
        "dashboard": "products/dashboard/micro.html",
        "optimizer": optimizer_result,
        "lem": model.export(),
    }
    runtime.write_manifest("run_state.json", terminal_state)
    exported_state = json.loads((run_root / "run_state.json").read_text(encoding="utf-8"))
    assert exported_state["status"] == "completed"
    assert exported_state["lifecycle_state"] == "completed"
    assert exported_state["terminal"] is True
    after_terminal = runtime.execute(
        OperationSpec(
            "aggregation.compute",
            parameters={"rows": [{"amount": 10}, {"amount": 20}], "operation": "sum", "value_field": "amount"},
        )
    )
    assert after_terminal.value == 30
    assert json.loads((run_root / "run_state.json").read_text(encoding="utf-8"))["status"] == "completed"

    # Neither the input fixtures nor the rejected sibling source changed.
    assert source_hashes_before == {path: _sha256(path) for path in source_hashes_before}
