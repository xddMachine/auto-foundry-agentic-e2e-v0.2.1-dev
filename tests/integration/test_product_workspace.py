"""Real accepted-answer -> product workflow, without synthetic final manifests."""
from pathlib import Path
import hashlib
import json
import sys
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'skills/auto-foundry-agentic-e2e/scripts')]
from build_dashboard_demo import seed_reviewed_run, accept_demo_item, synthetic_answers, choose_demo_views
from product_workspace import ProductWorkspace
from auto_foundry_core import PlannerAction, ItemWorkspace, IntegrationSession
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.product_review import ProductReviewStore


def workspace(context,preview=False):
    return ProductWorkspace(context,PlannerAction('refresh_product_preview' if preview else 'build_product_candidate','product_agent',context.run_id,'offline boundary test'))


def frozen(context):
    return {str(p.relative_to(context.run_root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in context.run_root.glob('requirements/*/accepted/**/*') if p.is_file()}


def test_accepted_answers_render_real_geometry_and_retry_without_reanalysis(tmp_path):
    c=seed_reviewed_run(tmp_path/'run');w=workspace(c);before=frozen(c)
    inv=w.inventory()
    assert any(x.get('title')=='Monthly revenue' for x in inv['candidates'])
    result=w.build(choose_demo_views(w),presentation={'title':'Operations <reviewed>','section_titles':{'REQ-SALES':'Sales','REQ-OPS':'Operations'}})
    assert result['status']=='candidate' and not result['published']
    site=c.run_root/result['site_ref'];html=(site/'index.html').read_text()
    assert 'Operations &lt;reviewed&gt;' in html
    assert 'Delivery time (days)' in html and 'Order value (EUR)' in html
    assert 'chart-scatter-svg' in html or 'scatter' in html
    assert 'pie' in html and 'area' in html and '<svg' in html
    assert 'Operational exceptions' in html
    assert frozen(c)==before
    hashes={p.relative_to(site):p.read_bytes() for p in site.rglob('*') if p.is_file()}
    second=workspace(c).build(choose_demo_views(w))
    assert second['candidate_hash']==result['candidate_hash']
    assert {p.relative_to(site):p.read_bytes() for p in site.rglob('*') if p.is_file()}==hashes
    assert frozen(c)==before


def test_partial_preview_becomes_full_candidate_without_hiding_accepted_work(tmp_path):
    c=seed_reviewed_run(tmp_path/'run',complete=False)
    w=workspace(c,True); result=w.build(choose_demo_views(w))
    assert result['status']=='preview' and result['finalizable'] is False
    assert not (c.run_root/'products/product_manifest.json').exists()
    assert (c.run_root/result['site_ref']/'index.html').is_file()
    item=ItemWorkspace.load(c,'REQ-OPS',mode='requirement')
    accept_demo_item(c,item,synthetic_answers()['REQ-OPS'])
    with pytest.raises(ValueError,match='inputs changed'):
        w.build(choose_demo_views(w))
    new=workspace(c);result=new.build(choose_demo_views(new))
    assert result['status']=='candidate'
    assert 'Operational exceptions' in (c.run_root/result['site_ref']/'index.html').read_text()


def test_technical_integration_failure_does_not_discard_accepted_visuals(tmp_path):
    c=seed_reviewed_run(tmp_path/'run',complete=False)
    item=ItemWorkspace.load(c,'REQ-OPS',mode='requirement')
    item.write_plan({'item_id':item.item_id});item.write_draft(synthetic_answers()['REQ-OPS'])
    item.record_review('accept_with_limits',reviewer_ref='fixture-business-review');item.accept(accepted_refs=('work/plan.json',))
    session=IntegrationSession.create(c,item,PreparedAssetRegistry(c),'fixture-integration',invocation_id='failed-integration')
    session.finalize_technical_failure('fixture transport exhausted; no semantic conflict')
    before=frozen(c);w=workspace(c);r=w.build(choose_demo_views(w))
    assert 'Operational exceptions' in (c.run_root/r['site_ref']/'index.html').read_text()
    assert frozen(c)==before


def test_reject_agent_facts_and_requirement_omission(tmp_path):
    c=seed_reviewed_run(tmp_path/'run');w=workspace(c);choices=choose_demo_views(w)
    with pytest.raises(ValueError,match='facts are immutable'):
        w.build([{**choices[0],'value':999}])
    with pytest.raises(ValueError,match='without a decision surface'):
        w.build([choices[0]])
    with pytest.raises(ValueError,match='duplicate widget_id'):
        w.build(choices+[choices[0]])


def test_reviewed_candidate_cannot_be_mutated_by_redesign(tmp_path):
    c=seed_reviewed_run(tmp_path/'run');w=workspace(c);choices=choose_demo_views(w);r=w.build(choices)
    store=ProductReviewStore(c,'G-0001')
    store.record_review(reviewer_ref='independent-fixture',verdict='accept_with_limits',candidate_hash=r['candidate_hash'])
    plan=c.run_root/'extensions/G-0001/business_presentation_plan.json';before=plan.read_bytes()
    with pytest.raises(ValueError,match='cannot discard'):
        workspace(c).build(choices,presentation={'title':'Revised'})
    assert plan.read_bytes()==before


def test_scope_copy_and_presentation_membership_are_validated(tmp_path):
    c=seed_reviewed_run(tmp_path/'run');w=workspace(c);choices=choose_demo_views(w)
    with pytest.raises(ValueError):
        w.build(choices,presentation={'overview_widget_ids':['not-a-real-widget']})
    with pytest.raises(ValueError):
        w.build(choices,presentation={'arbitrary_instructions':'change numbers'})
    with pytest.raises(ValueError):
        workspace(c).build([{**row,'renderer_type':'made-up-chart'} for row in choices])


def test_successor_generation_preserves_parent_and_binds_new_title(tmp_path):
    from auto_foundry_core import RequirementRunExtension, RequirementRecord, RunLifecycle
    c=seed_reviewed_run(tmp_path/'run'); w=workspace(c); first=w.build(choose_demo_views(w),presentation={'title':'First generation'})
    parent=c.run_root/first['site_ref']
    before={p.relative_to(parent):p.read_bytes() for p in parent.rglob('*') if p.is_file()}
    RunLifecycle.load(c).reconcile_from_run(product_terminal_status='complete')
    RequirementRunExtension.append(c,[RequirementRecord('REQ-NEW','New requirement')])
    item=ItemWorkspace.load(c,'REQ-NEW',mode='requirement')
    accept_demo_item(c,item,{'answer':'A reviewed new result','visuals':[{'type':'kpi','title':'New orders','value':8,'unit':'orders','grain':'new cohort'}]})
    current=workspace(c)
    result=current.build(choose_demo_views(current),presentation={'title':'Second generation'})
    assert result['generation_id']=='G-0002'
    assert 'Second generation' in (c.run_root/result['site_ref']/'index.html').read_text()
    assert {p.relative_to(parent):p.read_bytes() for p in parent.rglob('*') if p.is_file()}==before


def test_product_revision_redesign_preserves_reviewed_predecessor(tmp_path):
    from auto_foundry_core.requirement_planning import RequirementSupervisorWorkspace
    c=seed_reviewed_run(tmp_path/'run');w=workspace(c);choices=choose_demo_views(w);first=w.build(choices)
    store=ProductReviewStore(c,'G-0001')
    store.record_review(reviewer_ref='fixture-reviewer',verdict='accept_with_limits',candidate_hash=first['candidate_hash'])
    parent=c.run_root/first['site_ref']; before={p.relative_to(parent):p.read_bytes() for p in parent.rglob('*') if p.is_file()}
    fingerprint=RequirementSupervisorWorkspace(c).phase_snapshot()['product']['preview_input_fingerprint']
    revision=store.begin_revision(request_id='offline-redesign',input_fingerprint=fingerprint,implementation_identity='a'*64)
    action=PlannerAction('build_product_candidate','product_agent',c.run_id,'offline redesign',metadata={
        'generation_id':'G-0001','input_fingerprint':fingerprint,'product_revision_id':revision.revision_id,'output_root_ref':revision.output_root_ref,
        'product_manifest_ref':revision.output_root_ref+'/product_manifest.json'})
    current=ProductWorkspace(c,action)
    result=current.build(choose_demo_views(current),presentation={'title':'Redesigned operations'})
    assert result['revision_id']==revision.revision_id
    assert result['site_ref'].startswith(revision.output_root_ref)
    assert 'Redesigned operations' in (c.run_root/result['site_ref']/'index.html').read_text()
    assert {p.relative_to(parent):p.read_bytes() for p in parent.rglob('*') if p.is_file()}==before
    assert store.load_revision_candidate(revision.revision_id).computed_hash==result['candidate_hash']


def test_all_failed_requirements_produce_honest_empty_state(tmp_path):
    from tests.core.test_item_failure_continuation import _run_with_items, _spec
    from auto_foundry_core import RunCoordinator
    from auto_foundry_core.telemetry import TelemetryRecorder
    c,items=_run_with_items(tmp_path,'REQ-A','REQ-B')
    for item in items: item.technical_failure('Fixture startup failed',recovery_exhausted=True)
    TelemetryRecorder(context=c).record('offline_fixture',facts={'synthetic':True})
    coord=RunCoordinator(c);coord.start(_spec(c.run_id))
    try:
        w=workspace(c)
        candidates=w.inventory()['candidates']
        assert candidates
        selected=[{k:v for k,v in candidate.items() if k in {'widget_id','recipe_id','layout','renderer_type'}} for candidate in candidates]
        result=w.build(selected)
        html=(c.run_root/result['site_ref']/'index.html').read_text()
        assert 'No accepted business visual' in html
        evidence=(c.run_root/result['site_ref']/'evidence.html').read_text()
        assert 'REQ-A' in evidence and 'REQ-B' in evidence
        assert result['status']=='candidate'
    finally: coord.close(wait_for_roles=True)


def test_missing_periods_break_each_line_not_interpolate_between_observations():
    import dashboard_renderer as renderer
    one={'id':'gaps','type':'line','title':'Observed series','unit':'orders','points':[
        {'period':p,'value':v,'series':'A'} for p,v in zip(['Jan','Feb','Mar','Apr','May'],[1,2,None,4,5])]}
    html=renderer._render_line(one)
    assert html.count('class="line-series"')==2
    assert html.count('class="line-point"')==4
    one['scale_groups']=[{'series':['A'],'unit':'orders'},{'series':['B'],'unit':'EUR'}]
    one['points'] += [{'period':p,'value':v,'series':'B'} for p,v in zip(['Jan','Feb','Mar','Apr','May'],[10,20,30,40,50])]
    html=renderer._render_line(one)
    assert html.count('class="line-series"')==3
    assert html.count('class="line-point"')==9


def test_composition_does_not_masquerade_as_temporal_stacked_area():
    import dashboard_runtime as runtime
    segment={'label':'A','value':100,'size':'100%'}
    renderers=runtime.renderer_types_for_recipe('stacked_bar',{'segments':[segment],'unit':'orders'})
    assert 'stacked_area' not in renderers
