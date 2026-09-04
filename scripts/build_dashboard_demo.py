#!/usr/bin/env python3
"""Offline integration demonstration with explicitly synthetic reviewed inputs.

This is not a live-agent benchmark or a customer result. It exercises the real
accepted-bundle, integration, presentation, rendering and independent-review
storage APIs without API/model calls. It refuses an existing output directory.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src"), str(REPO / "skills/auto-foundry-agentic-e2e/scripts")]

from auto_foundry_core import (RunContext, RunLifecycle, ItemWorkspace, IntegrationSession,
                              RequirementRecord, RequirementExecutionGroup, RequirementExecutionPlan,
                              RequirementSupervisorWorkspace, RunCoordinator, CoordinatorRunSpec, PlannerAction)
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.product_review import ProductReviewStore
from auto_foundry_core.telemetry import TelemetryRecorder
from product_workspace import ProductWorkspace


def synthetic_answers() -> dict[str, dict]:
    common = {"period": "2025-01-01 – 2025-06-30", "as_of": "2025-06-30",
              "limitations": ["Synthetic demonstration data; descriptive, not causal."]}
    visual = lambda **kw: {**common, **kw}
    return {
        "REQ-SALES": {"title": "Commercial performance", "answer": "Synthetic commercial results.",
            "limitations": common["limitations"], "visuals": [
                visual(type="kpi",title="Revenue",value="€615,000",unit="EUR",grain="six-month total"),
                visual(type="kpi",title="Orders",value="3,075",unit="orders",grain="six-month total"),
                visual(type="kpi",title="Active customers",value=200,unit="customers",grain="distinct customer"),
                visual(type="kpi",title="Delivered orders",value=875,unit="orders",grain="fulfilment cohort",denominator=1000),
                visual(type="line", title="Monthly revenue", unit="EUR", grain="month", measure="value",
                       rows=[{"period":p, "value":v} for p,v in zip(["Jan","Feb","Mar","Apr","May","Jun"],[82000,91000,88000,109000,117000,128000])]),
                visual(type="bar", title="Sales by channel", unit="EUR", grain="channel",
                       rows=[{"label":"Direct", "value":282000},{"label":"Partners", "value":207000},{"label":"Online", "value":126000}]),
                visual(type="donut", title="Customer composition", unit="customers", grain="customer segment", denominator_value=200, denominator_label="customers",
                       categories=[{"label":"Enterprise","value":40,"size":"20%"},{"label":"Mid-market","value":90,"size":"45%"},{"label":"Small business","value":70,"size":"35%"}]),
                visual(type="area", title="Monthly order volume", unit="orders", grain="month", measure="value",
                       rows=[{"period":p,"value":v} for p,v in zip(["Jan","Feb","Mar","Apr","May","Jun"],[410,455,440,545,585,640])]),
            ]},
        "REQ-OPS": {"title":"Fulfilment performance", "answer":"Synthetic operations results.",
            "limitations":common["limitations"], "visuals":[
                visual(type="scatter", title="Delivery time and order value", unit="x: days; y: EUR", grain="order", x_label="Delivery time (days)", y_label="Order value (EUR)",
                       points=[{"label":f"Order {i+1}", "x":x, "y":y} for i,(x,y) in enumerate([(2,120),(3,260),(2.5,440),(5,300),(6,650),(4,720),(8,450),(7,900),(1,360),(5.5,1050),(9,870),(3.5,580)])]),
                visual(type="funnel", title="Order fulfilment stages", unit="orders", grain="nested order population", denominator=1000,
                       stages=[{"label":"Placed","value":1000,"size":"100%"},{"label":"Confirmed","value":960,"size":"96%"},{"label":"Shipped","value":920,"size":"92%"},{"label":"Delivered","value":875,"size":"87.5%"}]),
                visual(type="pie", title="Shipment status", unit="shipments", grain="shipment", denominator_value=100, denominator_label="shipments",
                       categories=[{"label":"On time","value":78,"size":"78%"},{"label":"Late","value":14,"size":"14%"},{"label":"In transit","value":8,"size":"8%"}]),
                visual(type="histogram", title="Delivery-time distribution", unit="orders", grain="delivery-day interval",
                       bins=[{"label":"0–2 days","count":120,"size":"40%"},{"label":"2–4 days","count":300,"size":"100%"},{"label":"4–6 days","count":210,"size":"70%"},{"label":"6–8 days","count":90,"size":"30%"},{"label":"8+ days","count":30,"size":"10%"}]),
                visual(type="table", title="Operational exceptions", unit="orders", grain="exception category", columns=["Exception","Orders","Owner"],
                       rows=[{"Exception":"Address mismatch","Orders":12,"Owner":"Customer operations"},{"Exception":"Carrier reschedule","Orders":8,"Owner":"Logistics"},{"Exception":"Allocation pending","Orders":5,"Owner":"Warehouse"}]),
            ]},
    }


def seed_reviewed_run(output: Path, *, complete: bool = True) -> RunContext:
    if output.exists():
        raise FileExistsError("use a new directory; the demo never overwrites a saved run")
    context = RunContext("RUN-SYNTHETIC-DASHBOARD", output)
    answers = synthetic_answers()
    RunLifecycle.create(context, tuple(answers), mode="requirement")
    records = tuple(RequirementRecord(requirement_id=k, original_text=v["title"], business_objective=v["title"],
                                    expected_analytical_outputs=("reviewed visual data",), data_needs=("synthetic observations",),
                                    limitations=("Synthetic offline fixture",), status="queued") for k,v in answers.items())
    plan = RequirementExecutionPlan(input_records=records,
        groups=tuple(RequirementExecutionGroup((k,), v["title"]) for k,v in answers.items()),
        planner_ref="offline-demo-planner", portfolio_strategy="Independent commercial and operations evidence", revision=1)
    RequirementSupervisorWorkspace(context).save(plan)
    for index,(item_id,answer) in enumerate(answers.items()):
        item=ItemWorkspace.create(context,item_id,mode="requirement",original_text=answer["title"])
        if index and not complete:
            continue
        accept_demo_item(context, item, answer)
    TelemetryRecorder(context=context).record("offline_fixture_created", facts={"synthetic":True,"model_calls":0})
    RunCoordinator(context).start(CoordinatorRunSpec(run_id=context.run_id,generation_id="G-0001",planner_ref=plan.planner_ref,
        planner_hash=hashlib.sha256(RunLifecycle.load(context).plan_path.read_bytes()).hexdigest(),publication_policy={"enabled":False}))
    return context


def accept_demo_item(context: RunContext, item: ItemWorkspace, answer: dict) -> None:
    item.write_plan({"item_id":item.item_id,"offline":True,"synthetic":True})
    item.write_draft(answer)
    # A recorded fixture decision, not a claim that an LLM independently reviewed it.
    item.record_review("accept_with_limits",reviewer_ref="offline-recorded-business-review")
    item.accept(accepted_refs=("work/plan.json",))
    session=IntegrationSession.create(context,item,PreparedAssetRegistry(context),"offline-integration",invocation_id="demo-"+item.item_id)
    session.add_limitation({"text":"Synthetic demonstration; no reusable business definition inferred."},scope=item.item_id,evidence_refs=("answer_content.json",))
    session.record_fidelity_review("accept",checked_record_ids=tuple(r.record_id for r in session.records))
    session.commit()


def choose_demo_views(workspace: ProductWorkspace) -> list[dict]:
    result=[];offset=0
    while True:
        inventory=workspace.inventory(offset=offset)
        for c in inventory["candidates"]:
            if not c.get("accepted_visual"):
                continue
            selection={"widget_id":c["widget_id"]}
            if c["visual"]:
                selection.update({k:c[k] for k in ("recipe_id","layout","renderer_type")})
                selection["layout"]="compact" if c.get("type")=="kpi" else "half"
                if c.get("type")=="table":
                    selection.update(recipe_id="table",renderer_type="table",layout="full")
                # Exact chart type is a recorded demo design decision, never a production rule.
                if c.get("type")=="pie": selection["renderer_type"]="pie"
                if c.get("type")=="area": selection["renderer_type"]="area"
            result.append(selection)
        offset=inventory["next_offset"]
        if offset is None: break
    return result


def build_demo(output: Path) -> dict:
    context=seed_reviewed_run(output)
    workspace=ProductWorkspace(context,PlannerAction("build_product_candidate","product_agent",context.run_id,"offline integration demonstration"))
    choices=choose_demo_views(workspace)
    result=workspace.build(choices,presentation={"title":"Operations cockpit", "subtitle":"Synthetic demonstration · January–June 2025 · No live model calls", "section_titles":{"REQ-SALES":"Commercial performance","REQ-OPS":"Fulfilment performance"}})
    review=ProductReviewStore(context,"G-0001").record_review(reviewer_ref="offline-recorded-product-review",verdict="accept_with_limits",candidate_hash=result["candidate_hash"])
    result.update({"synthetic":True,"model_calls":0,"review_hash":review.computed_hash,"html":str(output/result["site_ref"]/"index.html")})
    return result

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build_demo(args.output.resolve()),indent=2))
