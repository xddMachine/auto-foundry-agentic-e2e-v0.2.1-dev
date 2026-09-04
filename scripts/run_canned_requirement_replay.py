"""Fast deterministic offline replay for one parent requirement.

The cassette is synthetic readable input, not model output.  One requirement
workspace owns a persisted semantic plan, one deterministic calculation, one
parent answer, review, acceptance, and typed integration.  The replay never
contacts a provider or network service.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import zipfile
from typing import Any, Iterator, Mapping


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from auto_foundry_core import (  # noqa: E402
    AnalystAnswer,
    AnalystWorkspace,
    BoundAnalysisContext,
    CoreRuntime,
    DataAssetRef,
    DataRoomWorkbench,
    EvidenceNote,
    IntegrationSession,
    InvocationReceiptLedger,
    ItemWorkspace,
    PreparedAssetRegistry,
    RequirementAnalysisPlan,
    RequirementAnalysisTask,
    RunContext,
    RunLifecycle,
)
from scripts.run_canned_agent_replay import (  # noqa: E402
    UNAVAILABLE,
    no_external_call_guard as _question_no_external_call_guard,
)


FIXTURE_PATH = ROOT / "benchmarks" / "benchmark_a" / "canned_requirement_replay" / "fixture.json"
REQUIREMENT_TEXT = (
    "Dashboard should show the ratio of milk fat content to the procurement price of the raw material for that milk."
)
REQUIREMENT_ID = "R-001"
REPLAY_LABEL = "deterministic_requirement_fixture_replay"


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _semantic_value(value: Any, *, run_root: Path | None = None) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_value(child, run_root=run_root)
            for key, child in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in {"run_id", "created_at", "updated_at", "committed_at", "generated_at", "elapsed_ms"}
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_value(child, run_root=run_root) for child in value]
    if isinstance(value, str) and run_root is not None:
        return value.replace(str(run_root), "<RUN_ROOT>").replace(str(run_root.parent), "<CYCLE_ROOT>")
    return value


def _semantic_hash(value: Any, *, run_root: Path | None = None) -> str:
    return _sha256_bytes(_json_bytes(_semantic_value(value, run_root=run_root)))


def _derive_call_counters(ledger: InvocationReceiptLedger, runtime: CoreRuntime, guard: Any) -> dict[str, int]:
    counters = {"agent_calls": 0, "model_calls": 0, "network_calls": int(getattr(guard, "completed_network_calls", 0) or 0)}
    for receipt in ledger.receipts:
        metadata = receipt.artifact_delta if isinstance(receipt.artifact_delta, Mapping) else {}
        for key in counters:
            counters[key] += int(metadata.get(key, 0) or 0)
    for event in runtime.telemetry.events:
        capability = str(event.capability_id or "")
        if capability.startswith(("agent.", "model.")):
            facts = event.facts if isinstance(event.facts, Mapping) else {}
            for key in counters:
                counters[key] += int(facts.get(key, 0) or 0)
    return counters


def _append_receipt(
    ledger: InvocationReceiptLedger,
    *,
    invocation_id: str,
    role: str,
    attempt_id: str,
    conclusion: str,
) -> str:
    from auto_foundry_core import AgentInvocationReceipt

    return ledger.append(
        AgentInvocationReceipt(
            invocation_id=invocation_id,
            item_id=REQUIREMENT_ID,
            attempt_id=attempt_id,
            lane_id=f"canned-requirement-{role.lower().replace(' ', '-')}",
            role=role,
            route=REPLAY_LABEL,
            provider=UNAVAILABLE,
            model=UNAVAILABLE,
            start="2026-01-01T00:00:00+00:00",
            first_activity="2026-01-01T00:00:00+00:00",
            finish="2026-01-01T00:00:01+00:00",
            terminal_reason="completed",
            artifact_delta={
                "replay_label": REPLAY_LABEL,
                "provider": UNAVAILABLE,
                "model": UNAVAILABLE,
                "conclusion": conclusion,
                "agent_calls": 0,
                "model_calls": 0,
                "network_calls": 0,
            },
            tool_calls=0,
        )
    )


@contextmanager
def no_external_call_guard() -> Iterator[Any]:
    """Public test hook for proving requirement replay network rejection."""

    with _question_no_external_call_guard() as guard:
        yield guard


def _json_number(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if number < 0:
        raise ValueError(f"{field} must be nonnegative")
    return number


def load_fixture(path: Path = FIXTURE_PATH) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("canned requirement fixture is unreadable") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("canned requirement fixture must be an object")
    if payload.get("schema_version") != "benchmark_a.canned_requirement_replay.v1":
        raise ValueError("canned requirement fixture schema is invalid")
    if payload.get("requirement_id") != REQUIREMENT_ID:
        raise ValueError("canned requirement fixture requirement_id is invalid")
    if payload.get("requirement_text") != REQUIREMENT_TEXT:
        raise ValueError("canned requirement fixture must preserve the exact parent requirement")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("canned requirement fixture requires at least two milk rows")
    row_keys = {"milk_lot_id", "milk_fat_percent", "procurement_price_eur_per_kg"}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != row_keys:
            raise ValueError("canned requirement fixture row fields are invalid")
        lot_id = str(row["milk_lot_id"]).strip()
        if not lot_id or lot_id in seen:
            raise ValueError("canned requirement fixture milk_lot_id values must be unique")
        seen.add(lot_id)
        _json_number(row["milk_fat_percent"], field="milk_fat_percent")
        if _json_number(row["procurement_price_eur_per_kg"], field="procurement_price_eur_per_kg") <= 0:
            raise ValueError("procurement price must be positive")
    return payload


def _create_archive(input_root: Path, fixture: Mapping[str, Any]) -> Path:
    archive = input_root / "milk_fixture.zip"
    rows = fixture["rows"]
    lines = ["milk_lot_id,milk_fat_percent,procurement_price_eur_per_kg"]
    lines.extend(
        f"{row['milk_lot_id']},{row['milk_fat_percent']},{row['procurement_price_eur_per_kg']}"
        for row in rows
    )
    csv_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    info = zipfile.ZipInfo("milk_lots.csv")
    info.date_time = (2020, 1, 1, 0, 0, 0)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(info, csv_bytes)
    return archive


def _write_ratio_script(item: ItemWorkspace) -> tuple[Path, Path]:
    script = item.work_root / "calculations" / "milk_ratio.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "from decimal import Decimal\n"
        "import json\n"
        "from pathlib import Path\n"
        "from auto_foundry_core.analysis import load_bound_analysis_context\n"
        "\n"
        "bound = load_bound_analysis_context()\n"
        "entry = next(value for value in bound.source_catalog.entries if value.path == 'milk_lots.csv')\n"
        "rows = bound.data_room.sample(entry, limit=1000)\n"
        "if not rows:\n"
        "    raise ValueError('milk fixture has no rows')\n"
        "fat_total = sum((Decimal(str(row['milk_fat_percent'])) for row in rows), Decimal('0'))\n"
        "price_total = sum((Decimal(str(row['procurement_price_eur_per_kg'])) for row in rows), Decimal('0'))\n"
        "if price_total <= 0:\n"
        "    raise ValueError('procurement price denominator must be positive')\n"
        "ratio = (fat_total / price_total).quantize(Decimal('0.0001'))\n"
        "payload = {\n"
        "    'status': 'complete',\n"
        "    'population': {'name': 'supplied milk lots', 'row_count': len(rows)},\n"
        "    'numerator': {'value': float(fat_total), 'unit': 'percent', 'definition': 'sum of milk fat percentages'},\n"
        "    'denominator': {'value': float(price_total), 'unit': 'EUR/kg', 'definition': 'sum of raw-material procurement prices'},\n"
        "    'ratio': {'value': float(ratio), 'formatted_value': format(ratio, 'f'), 'unit': 'percentage-points per EUR/kg'},\n"
        "    'denominator_definition': 'Raw-material procurement price in EUR per kilogram for the same supplied milk lots.',\n"
        "    'limits': ['Synthetic fixture only.', 'Descriptive ratio; no causal or time-series interpretation.', 'No production pricing or coverage claim.'],\n"
        "}\n"
        "Path('analysis.json').write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding='utf-8')\n",
        encoding="utf-8",
    )
    return script, item.work_root / "analysis.json"


def _plan() -> RequirementAnalysisPlan:
    return RequirementAnalysisPlan(
        tasks=(
            RequirementAnalysisTask(
                task_id="T-FAT",
                question="Measure milk fat content for the supplied milk-lot population.",
                objective="Produce the numerator with an explicit percent unit and population.",
                expected_analytical_outputs=("milk-fat numerator",),
            ),
            RequirementAnalysisTask(
                task_id="T-PRICE",
                question="Measure raw-material procurement price for the same milk-lot population.",
                objective="Produce the denominator with an explicit EUR/kg unit and population.",
                expected_analytical_outputs=("procurement-price denominator",),
            ),
            RequirementAnalysisTask(
                task_id="T-RATIO",
                question="Synthesize the ratio of milk fat content to procurement price.",
                objective="Join the two parent measurements without changing their denominator.",
                dependencies=("T-FAT", "T-PRICE"),
                expected_analytical_outputs=("milk-fat-to-price ratio",),
                expected_visual_outputs=("dashboard ratio fact",),
            ),
        ),
        synthesis_intent=(
            "Synthesize one parent dashboard answer from the two measurements and their dependent ratio, "
            "retaining explicit numerator, denominator, units, population, and limits."
        ),
        original_text=REQUIREMENT_TEXT,
    )


def _run_cycle(
    cycle_index: int,
    root: Path,
    fixture: Mapping[str, Any],
    guard: Any,
) -> dict[str, Any]:
    started = time.monotonic()
    input_root = root / "input"
    run_root = root / "run"
    input_root.mkdir(parents=True, exist_ok=True)
    run_root.mkdir(parents=True, exist_ok=True)
    archive = _create_archive(input_root, fixture)
    archive_hash_before = _sha256_bytes(archive.read_bytes())
    context = RunContext(
        f"RUN-CANNED-REQUIREMENT-{cycle_index:03d}",
        run_root,
        (input_root,),
        core_version="0.8.0",
        skill_version="0.7.1",
    )
    lifecycle = RunLifecycle.create(context, (REQUIREMENT_ID,), mode="requirement")
    runtime = CoreRuntime(context)
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    item = ItemWorkspace.create(
        context,
        REQUIREMENT_ID,
        mode="requirement",
        original_text=REQUIREMENT_TEXT,
    )
    bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), item, workbench=workbench)
    analyst = AnalystWorkspace(bound, owner_ref=f"owner-{REQUIREMENT_ID}")

    # The public facade rejects analysis until the parent semantic plan exists.
    try:
        analyst.begin_analysis(
            objective="Compute the milk-fat-to-procurement-price ratio.",
            strategy="Use one bounded deterministic script over the selected milk-lot source.",
        )
    except ValueError as exc:
        if "persisted semantic plan" not in str(exc):
            raise
        plan_gate_error = str(exc)
    else:
        raise AssertionError("requirement begin_analysis unexpectedly bypassed the plan gate")
    plan = analyst.plan_requirement(_plan())
    plan_path = item.work_root / "requirement_plan.json"
    if not plan_path.is_file():
        raise AssertionError("requirement plan was not persisted")
    analyst.select_semantic_scope(
        no_reuse_reason="The fresh canned replay has no accepted reusable semantics.",
        purpose="Record the required first-run semantic decision.",
    )
    analyst.begin_analysis(
        objective="Compute the milk-fat-to-procurement-price ratio.",
        strategy="Use one bounded deterministic script over the selected milk-lot source.",
        expected_outputs=("one ratio value", "one dashboard fact"),
        assumptions=("Synthetic fixture only",),
    )
    source = analyst.search_sources("milk_lots.csv", limit=1)
    if len(source) != 1:
        raise AssertionError("requirement replay expected one milk_lots.csv source")
    analyst.select_sources((source[0].source_id,), purpose="Shared milk-lot population for all semantic tasks")
    script, output = _write_ratio_script(item)
    attempt = item.begin_attempt(REPLAY_LABEL, "Analytical Owner")
    report = analyst.run_analysis(script, outputs=(output,), deterministic_outputs=(output,))
    if not report.succeeded or report.deterministic_match is not True:
        raise AssertionError("requirement deterministic calculation did not pass")
    result = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(result, Mapping) or result.get("status") != "complete":
        raise AssertionError("requirement calculation output is invalid")
    analyst.record_evidence(
        EvidenceNote(
            evidence_id="E-R001-RATIO",
            conclusion=(
                f"The supplied milk-lot population yields a ratio of {result['ratio']['formatted_value']} "
                f"{result['ratio']['unit']}."
            ),
            method="Run one deterministic Decimal calculation over the bounded milk-lot source.",
            evidence_refs=("work/analysis.json", source[0].source_id),
            limitations=tuple(str(value) for value in result["limits"]),
            facts={"ratio": result["ratio"], "numerator": result["numerator"], "denominator": result["denominator"]},
        )
    )
    owner_answer = analyst.submit_answer(
        AnalystAnswer(
            answer=(
                f"For the supplied milk lots, the ratio is {result['ratio']['formatted_value']} "
                f"{result['ratio']['unit']}. The numerator is {result['numerator']['value']} {result['numerator']['unit']}; "
                f"the denominator is {result['denominator']['value']} {result['denominator']['unit']}."
            ),
            headline_findings=("The bounded milk-fat-to-price ratio is directly computable.",),
            scope="Supplied synthetic milk lots only.",
            method="Use the same milk-lot population for the numerator and denominator, then divide.",
            supported_components=("One descriptive ratio with explicit units and denominator.",),
            unsupported_components=("Production pricing, causal interpretation, and time-series trend.",),
            limitations=tuple(str(value) for value in result["limits"]),
            next_actions=("Recompute against reviewed production pricing before operational use.",),
            visuals=({"visual_id": "V-R001-RATIO", "kind": "kpi", "title": "Milk fat to procurement price ratio"},),
            evidence_refs=("work/evidence.jsonl#E-R001-RATIO",),
        )
    )
    item.finish_attempt(attempt.attempt_id, status="completed")
    ledger = InvocationReceiptLedger(context)
    _append_receipt(
        ledger,
        invocation_id="I-R001-ANALYTICAL-OWNER",
        role="Analytical Owner",
        attempt_id=attempt.attempt_id,
        conclusion="One parent answer and one deterministic ratio output were produced.",
    )
    review = analyst.review.record(
        "accept_with_limits",
        reviewer_ref=f"{REPLAY_LABEL}:business-review",
    )
    _append_receipt(
        ledger,
        invocation_id="I-R001-BUSINESS-REVIEW",
        role="Independent Business Reviewer",
        attempt_id=attempt.attempt_id,
        conclusion="Accept with explicit synthetic-population and denominator limits.",
    )
    accepted = item.accept(
        accepted_refs=(
            "work/requirement_plan.json",
            "work/plan.json",
            "work/source_map.json",
            "work/evidence.jsonl",
            "work/analysis.json",
        )
    )
    persisted = ItemWorkspace.load(context, REQUIREMENT_ID, mode="requirement")
    accepted_manifest = json.loads((persisted.accepted_root / "manifest.json").read_text(encoding="utf-8"))
    try:
        persisted.accept()
    except FileExistsError:
        acceptance_immutable = True
    else:
        raise AssertionError("accepted requirement was mutable")

    registry = PreparedAssetRegistry(context)
    integration = IntegrationSession.create(
        context,
        persisted,
        registry,
        "requirement-result-integration",
        "I-R001-INTEGRATION",
    )
    evidence = ("work/analysis.json", "answer_content.json")
    metric_id = integration.add_metric(
        {
            "metric_id": "metric-r001-milk-fat-price-ratio",
            "label": "Milk fat content to raw-material procurement price ratio",
            "value": result["ratio"]["value"],
            "formatted_value": result["ratio"]["formatted_value"],
            "unit": result["ratio"]["unit"],
            "numerator": result["numerator"],
            "denominator": result["denominator"],
            "denominator_definition": result["denominator_definition"],
            "limits": result["limits"],
        },
        scope="requirement",
        evidence_refs=evidence,
    )
    dashboard_fact_id = integration.add_dashboard_fact(
        {
            "fact_id": "fact-r001-milk-fat-price-ratio",
            "metric_id": metric_id,
            "value": result["ratio"]["value"],
            "unit": result["ratio"]["unit"],
            "denominator": result["denominator"],
            "limits": result["limits"],
        },
        scope="requirement",
        evidence_refs=evidence,
    )
    if not integration.validate().valid:
        raise AssertionError("requirement integration validation failed")
    fidelity_packet = integration.build_fidelity_packet()
    fidelity = integration.record_fidelity_review(
        "accept",
        checked_record_ids=(metric_id, dashboard_fact_id),
    )
    integration_manifest = integration.commit()
    _append_receipt(
        ledger,
        invocation_id="I-R001-INTEGRATION",
        role="Result Integration Agent",
        attempt_id=attempt.attempt_id,
        conclusion="One metric and one dashboard fact were committed through IntegrationSession.",
    )
    _append_receipt(
        ledger,
        invocation_id="I-R001-FIDELITY",
        role="Integration Fidelity Reviewer",
        attempt_id=attempt.attempt_id,
        conclusion="The metric and dashboard fact match the accepted parent answer.",
    )
    # Reconcile through the public workspace so the reducer verifies the
    # committed integration boundary instead of trusting an ``integrated``
    # item-state label.
    lifecycle_snapshot = lifecycle.reconcile([persisted])
    if lifecycle_snapshot.state != "integration_complete":
        raise AssertionError(f"unexpected requirement lifecycle state: {lifecycle_snapshot.state}")
    calls = _derive_call_counters(ledger, runtime, guard)
    if any(calls.values()):
        raise AssertionError(f"requirement replay made external calls: {calls}")
    if any(path.suffix == ".pyc" for path in run_root.rglob("*")):
        raise AssertionError("requirement replay created pyc under run root")
    if _sha256_bytes(archive.read_bytes()) != archive_hash_before:
        raise AssertionError("requirement input archive changed during replay")

    records = [
        {
            "record_id": record.record_id,
            "kind": record.kind,
            "scope": record.scope,
            "payload": _semantic_value(record.payload, run_root=run_root),
            "record_hash": record.record_hash,
        }
        for record in integration.records
    ]
    records.sort(key=lambda value: value["record_id"])
    accepted_manifest_semantic = {
        "item_id": accepted_manifest["item_id"],
        "outcome": accepted_manifest["outcome"],
        "content_path": accepted_manifest["content_path"],
        "content_hash": accepted_manifest["content_hash"],
    }
    integration_manifest_value = json.loads(integration.committed_manifest_path.read_text(encoding="utf-8"))
    integration_manifest_semantic = {
        key: integration_manifest_value[key]
        for key in (
            "session_id",
            "item_id",
            "owner_id",
            "invocation_id",
            "status",
            "accepted_content_hash",
            "records_hash",
            "records_count",
            "counts",
        )
    }
    fidelity_packet_semantic = {
        "item_id": REQUIREMENT_ID,
        "record_ids": [metric_id, dashboard_fact_id],
        "records_hash": _semantic_hash(records, run_root=run_root),
    }
    fidelity_result_semantic = {
        "item_id": REQUIREMENT_ID,
        "review_kind": fidelity.review_kind,
        "verdict": fidelity.verdict,
        "checked_record_ids": sorted(fidelity.checked_record_ids),
        "affected_record_ids": sorted(fidelity.affected_record_ids),
        "dependency_ids": sorted(fidelity.dependency_ids),
    }
    stable = {
        "status": lifecycle_snapshot.state,
        "mode": "requirement",
        "item_id": REQUIREMENT_ID,
        "requirement_text": REQUIREMENT_TEXT,
        "plan": _semantic_value(plan.to_dict(), run_root=run_root),
        "plan_task_ids": [task.task_id for task in plan.tasks],
        "plan_task_count": len(plan.tasks),
        "plan_gate_error": plan_gate_error,
        "result": _semantic_value(result, run_root=run_root),
        "input_archive_hash": archive_hash_before,
        "accepted_content_hash": accepted.content_hash,
        "accepted_manifest_hash": _semantic_hash(accepted_manifest_semantic, run_root=run_root),
        "integration_records": records,
        "integration_manifest_hash": _semantic_hash(integration_manifest_semantic, run_root=run_root),
        "integration_record_hash": _semantic_hash(records, run_root=run_root),
        "fidelity_packet_hash": _semantic_hash(fidelity_packet_semantic, run_root=run_root),
        "fidelity_result_hash": _semantic_hash(fidelity_result_semantic, run_root=run_root),
        "acceptance_immutable": acceptance_immutable,
        "review_verdict": review["verdict"],
        "call_counters": calls,
    }
    digest = _sha256_bytes(_json_bytes(stable))
    return {
        "cycle": cycle_index,
        "status": lifecycle_snapshot.state,
        "item_status": accepted_manifest["outcome"],
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "plan_before_analysis_enforced": True,
        "plan_task_ids": [task.task_id for task in plan.tasks],
        "plan_task_count": len(plan.tasks),
        "requirement_text": REQUIREMENT_TEXT,
        "result": result,
        "accepted_hash": accepted.content_hash,
        "accepted_manifest_hash": stable["accepted_manifest_hash"],
        "integration_hash": str(integration_manifest["manifest_hash"]),
        "integration_semantic_hash": stable["integration_manifest_hash"],
        "fidelity_hash": fidelity.result_hash,
        "record_ids": [metric_id, dashboard_fact_id],
        "acceptance_immutable": acceptance_immutable,
        "call_counters": calls,
        "offline_guard_forbidden_attempts": guard.forbidden_attempts,
        "script_phases": [receipt.phase for receipt in report.receipts],
        "stable_payload": stable,
        "stable_digest": digest,
    }


def run_cycle(cycle_index: int, root: Path, fixture: Mapping[str, Any] | None = None) -> dict[str, Any]:
    selected = load_fixture() if fixture is None else fixture
    with no_external_call_guard() as guard:
        try:
            socket.create_connection(("127.0.0.1", 9), timeout=0.01)
        except RuntimeError as exc:
            if "offline replay blocked external socket call" not in str(exc):
                raise
        return _run_cycle(cycle_index, root, selected, guard)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    args = parser.parse_args(argv)
    if args.cycles < 1:
        parser.error("--cycles must be >= 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    failures: list[dict[str, Any]] = []
    cycles: list[dict[str, Any]] = []
    try:
        fixture = load_fixture(args.fixture)
    except Exception as exc:
        summary = {
            "schema_version": "benchmark_a.canned_requirement_replay.summary.v1",
            "fixture": REPLAY_LABEL,
            "cycle_count": args.cycles,
            "cycles": [],
            "failures": [{"cycle": "fixture", "status": "failed", "error_type": type(exc).__name__, "error": str(exc)}],
            "deterministic": False,
            "stable_digest": None,
            "call_counters": {"agent_calls": 0, "model_calls": 0, "network_calls": 0},
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 1
    if args.output_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="auto-foundry-canned-requirement-replay-")
        root = Path(temporary.name)
    else:
        temporary = None
        root = args.output_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
    try:
        for cycle_index in range(1, args.cycles + 1):
            try:
                cycles.append(run_cycle(cycle_index, root / f"cycle-{cycle_index:03d}", fixture))
            except Exception as exc:
                failures.append(
                    {
                        "cycle": cycle_index,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        digests = [value["stable_digest"] for value in cycles]
        counters = {key: sum(int((value.get("call_counters") or {}).get(key, 0) or 0) for value in cycles) for key in ("agent_calls", "model_calls", "network_calls")}
        if any(counters.values()):
            failures.append({"cycle": "aggregate", "status": "failed", "error_type": "ExternalCallError", "error": str(counters)})
        if len(set(digests)) > 1:
            failures.append({"cycle": "aggregate", "status": "failed", "error_type": "DeterminismError", "error": "cycle stable digests disagree"})
        summary = {
            "schema_version": "benchmark_a.canned_requirement_replay.summary.v1",
            "fixture": REPLAY_LABEL,
            "cycle_count": args.cycles,
            "cycles": cycles,
            "failures": failures,
            "deterministic": bool(cycles) and not failures and len(set(digests)) == 1,
            "stable_digest": digests[0] if digests and len(set(digests)) == 1 else None,
            "call_counters": counters,
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 1 if failures else 0
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
