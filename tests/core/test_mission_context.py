from __future__ import annotations

import pytest

from auto_foundry_core.document_ingestion import DocumentCatalog, normalize_document_bytes
from auto_foundry_core.mission_context import (
    ContextItem,
    MissionContext,
    MissionPlan,
    ProductBrief,
    SourceBinding,
    merge_mission_contexts,
)


def _item(text: str = "analysts") -> ContextItem:
    return ContextItem(
        text,
        source_bindings=(SourceBinding("INPUT-001", span={"start": 0, "end": len(text)}),),
    )


def test_mission_context_round_trips_and_hashes_content() -> None:
    item = _item()
    context = MissionContext(
        mission_intent="hybrid",
        product_brief=ProductBrief(audience=(item,), visual_expectations=(item,)),
        source_context=(item,),
        technical_constraints=(item,),
        additional_context=(ContextItem("not an AO requirement", (SourceBinding("INPUT-001", span={"start": 0, "end": 8}),), "context only"),),
    )
    restored = MissionContext.from_dict(context.to_dict())
    assert restored.context_hash == context.context_hash
    assert restored.source_bindings
    assert context.to_dict()["mission_intent"] == "hybrid"


def test_mission_context_rejects_unknown_intent_and_tampered_hash() -> None:
    with pytest.raises(ValueError):
        MissionContext("analysis")
    context = MissionContext("discovery")
    payload = context.to_dict()
    payload["context_hash"] = "0" * 64
    with pytest.raises(ValueError):
        MissionContext.from_dict(payload)


def test_mission_plan_binds_requirement_ids_to_context_hash() -> None:
    plan = MissionPlan(MissionContext("specification"), requirement_ids=("REQ-001",))
    restored = MissionPlan.from_dict(plan.to_dict())
    assert restored.requirement_ids == ("REQ-001",)
    assert restored.context_hash == plan.context_hash
    assert restored.plan_hash == plan.plan_hash
    assert plan.hash == plan.plan_hash
    changed = MissionPlan(plan.mission_context, requirement_ids=("REQ-002",))
    assert changed.plan_hash != plan.plan_hash


def test_merge_mission_contexts_is_cumulative_and_hash_bound() -> None:
    parent_item = _item("audience")
    child_item = _item("decision")
    parent = MissionContext("specification", product_brief=ProductBrief(audience=(parent_item,)))
    child = MissionContext("hybrid", product_brief=ProductBrief(decision=(child_item,)))
    merged = merge_mission_contexts(parent, child)
    assert merged.product_brief.audience == (parent_item,)
    assert merged.product_brief.decision == (child_item,)
    assert merged.metadata["parent_context_hash"] == parent.context_hash
    assert merged.metadata["context_lineage"][-1] == child.context_hash


def test_merge_keeps_same_path_revisions_and_rebinds_child_evidence() -> None:
    old_document = normalize_document_bytes(
        b"old dashboard brief",
        document_ref="brief.md",
        source_path="brief.md",
    )
    new_document = normalize_document_bytes(
        b"new dashboard brief",
        document_ref="brief.md",
        source_path="brief.md",
    )
    old_binding = SourceBinding(
        "brief.md",
        locator=old_document.sections[0].locator,
        content_hash=old_document.sections[0].content_hash,
        text=old_document.sections[0].text,
    )
    new_binding = SourceBinding(
        "brief.md",
        locator=new_document.sections[0].locator,
        content_hash=new_document.sections[0].content_hash,
        text=new_document.sections[0].text,
    )
    parent = MissionContext(
        "specification",
        product_brief=ProductBrief(audience=(ContextItem(old_document.sections[0].text, (old_binding,)),)),
        document_catalog=DocumentCatalog((old_document,)).to_dict(),
    )
    child = MissionContext(
        "hybrid",
        product_brief=ProductBrief(audience=(ContextItem(new_document.sections[0].text, (new_binding,)),)),
        document_catalog=DocumentCatalog((new_document,)).to_dict(),
    )
    merged = merge_mission_contexts(parent, child)
    documents = DocumentCatalog.from_dict(merged.document_catalog).documents
    assert len(documents) == 2
    assert {document.content_hash for document in documents} == {
        old_document.content_hash,
        new_document.content_hash,
    }
    child_ref = merged.product_brief.audience[-1].source_bindings[0].source_ref
    assert child_ref != old_document.document_ref
    assert child_ref.endswith(f"{new_document.content_hash}.md")
    # Reconstructing the cumulative context validates every binding against
    # exactly one immutable catalog section; stale text/hash must fail closed.
    tampered = merged.to_dict()
    tampered["product_brief"]["audience"][-1]["source_bindings"][0]["text"] = "tampered"
    tampered["context_hash"] = merged.context_hash
    with pytest.raises(ValueError):
        MissionContext.from_dict(tampered)
