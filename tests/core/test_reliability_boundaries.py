"""Hermetic regressions for failures discovered in persisted run boundaries."""
from dataclasses import replace
import json
from pathlib import Path
import pytest
from auto_foundry_core import RunContext, RunCoordinator, PlannerAction, ItemWorkspace, SupervisorRepairResult, FoundrySupervisor
from auto_foundry_core.coordinator import CoordinatorIntegrityError
from auto_foundry_core.enterprise_model import LivingEnterpriseModel, OntologyConflictError
from tests.core.test_integration_semantic_policy import _setup
from tests.core.test_item_failure_continuation import _run_with_items, _spec
from tests.core.test_supervisor import _Coordinator


def test_snapshot_only_identity_recovers_projection(tmp_path):
    context, item, _, _, session = _setup(tmp_path)
    session.add_limitation({"text":"Known scope"}, scope="question", evidence_refs=("answer_content.json",))
    staging = item.item_root / "integration/staging"
    expected = (staging / "session.json").read_bytes()
    (staging / "session.json").unlink()
    (staging / "records.jsonl").unlink()
    assert RunCoordinator._integration_identity(item) == ("owner", "inv-1")
    assert (staging / "session.json").read_bytes() == expected
    assert (staging / "records.jsonl").stat().st_size > 0


def test_snapshot_tamper_is_not_repaired_as_missing_projection(tmp_path):
    _, item, _, _, _ = _setup(tmp_path)
    path = item.item_root / "integration/staging/snapshot.json"
    value = json.loads(path.read_text())
    value["state"]["owner_id"] = "different-owner"
    path.write_text(json.dumps(value))
    with pytest.raises(CoordinatorIntegrityError):
        RunCoordinator._integration_identity(item)


@pytest.mark.parametrize('field,old,new', [('grain','order','order-line'),('unit','EUR','USD'),('formula','sum(net)','sum(gross)'),('primary_key',['order'],['order','line'])])
def test_ontology_conflict_is_atomic_and_order_independent(field,old,new):
    for first, second in ((old,new),(new,old)):
        model = LivingEnterpriseModel()
        common = dict(item_id='orders', item_type='object',label='Orders')
        model.add_ontology_item({**common,'properties':{field:first}})
        before = model.export()
        with pytest.raises(OntologyConflictError,match='properties.'+field):
            model.add_ontology_item({**common,'properties':{'new_key':'must-not-leak',field:second}})
        assert model.export() == before


def test_ontology_exact_retries_and_compatible_extensions_remain_supported():
    model = LivingEnterpriseModel()
    item=dict(item_id='orders',item_type='object',label='Orders',properties={'grain':'order'},source_refs=['a'])
    first=model.add_ontology_item(item)
    assert model.add_ontology_item(item) == first
    extended=model.add_ontology_item({**item,'label':'Order headers','source_refs':['b'], 'properties':{'grain':'order','unit':'EUR'}})
    assert extended.label=='Orders' and extended.source_refs==('a','b')
    assert extended.properties['unit']=='EUR'


def test_integration_transport_failure_before_session_preserves_accepted_answer(tmp_path):
    context, (item,) = _run_with_items(tmp_path, 'REQ-A')
    item.write_plan({'item_id':item.item_id})
    item.write_draft({'answer':'Accepted result','visuals':[{'type':'kpi','title':'Orders','value':3,'unit':'orders'}]})
    item.record_review('accept',reviewer_ref='independent-fixture')
    item.accept(accepted_refs=('work/plan.json',))
    accepted=(item.item_root/'accepted/answer_content.json').read_bytes()
    coordinator=RunCoordinator(context)
    coordinator.start(_spec(context.run_id))
    try:
        assert coordinator._terminalize_exhausted_requirement_locked({},PlannerAction('integrate_requirement','integration_agent','REQ-A','fixture exhaustion'))
    finally:
        coordinator.close(wait_for_roles=True)
    assert ItemWorkspace.load(context,'REQ-A',mode='requirement').integration_state=='technical_failure'
    assert (item.item_root/'accepted/answer_content.json').read_bytes()==accepted


def test_supervisor_diagnostic_change_is_not_progress(tmp_path):
    coordinator=_Coordinator()
    def agent(*args,**kwargs):
        coordinator.current=replace(coordinator.current,diagnostics=({'message':'A different error text'},))
        return SupervisorRepairResult(repaired=True,tests_passed=True,durable_progress=True)
    result=FoundrySupervisor(RunContext('RUN-SUPERVISOR',tmp_path),coordinator=coordinator,repository_root=tmp_path,agent=agent).run()
    assert result.action=='repair_no_progress' and not result.repaired
