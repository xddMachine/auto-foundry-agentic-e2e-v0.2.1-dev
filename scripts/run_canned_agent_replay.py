"""Fast deterministic offline replay of the real Auto Foundry APIs.

The replay consumes only the readable canned role fixture.  It never imports
an agent client and does not represent fixture conclusions as model output.
Each cycle owns a fresh input archive and run root, then drives the same
workspace, review, integration, product, optimizer, and reporting boundaries
used by the normal program path.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import socket
import zipfile
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
FIXTURE_PATH = ROOT / "benchmarks" / "benchmark_a" / "canned_replay" / "agent_outputs.json"
SCRIPTS = ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts"

from auto_foundry_core import (  # noqa: E402
    AgentInvocationReceipt,
    AnalystAnswer,
    AnalystWorkspace,
    BoundAnalysisContext,
    CoreRuntime,
    DataAssetRef,
    DataRoomWorkbench,
    EvidenceNote,
    FreezeMarkers,
    IntegrationSession,
    InvocationReceiptLedger,
    ItemWorkspace,
    OntologyItem,
    OperationSpec,
    PreparedAssetRegistry,
    RunContext,
    RunLifecycle,
    RunReportFinalizer,
    RunReportProjector,
    ReviewFinding,
    SpecialistMemo,
    SpecialistTask,
)
from auto_foundry_core.reporting import RunReportEventBindings, RunReportInputGatherer


REPLAY_LABEL = "deterministic_fixture_replay"
UNAVAILABLE = "unavailable"
FIXED_START = "2026-01-01T00:00:00+00:00"
FIXED_FINISH = "2026-01-01T00:00:01+00:00"
QUESTION_IDS = ("Q-001", "Q-002", "Q-003")
_VOLATILE_DIGEST_KEYS = {
    "run_id",
    "generated_at",
    "created_at",
    "updated_at",
    "start",
    "first_activity",
    "finish",
    "elapsed_ms",
    "manifest_path",
    "terminalization_receipt_hash",
    "manifest_hash",
    "report_hash",
    "receipt_hash",
    "device",
    "inode",
    "mtime_ns",
}
_FORBIDDEN_IMPORT_MARKERS = ("import socket", "import urllib", "import requests", "import openai", "import anthropic")


class _OfflineCallGuard:
    """Narrow per-cycle guard that makes accidental network calls fail closed."""

    def __init__(self) -> None:
        self.forbidden_attempts = 0
        self.completed_network_calls = 0
        self._socket_methods: dict[str, Any] = {}
        self._create_connection: Any = None

    def _blocked(self, *_args: Any, **_kwargs: Any) -> Any:
        self.forbidden_attempts += 1
        raise RuntimeError("offline replay blocked external socket call")

    def __enter__(self) -> "_OfflineCallGuard":
        for method_name in ("connect", "connect_ex", "sendto", "sendmsg"):
            method = getattr(socket.socket, method_name, None)
            if method is not None:
                self._socket_methods[method_name] = method
                setattr(socket.socket, method_name, self._blocked)
        self._create_connection = socket.create_connection
        socket.create_connection = self._blocked  # type: ignore[assignment]
        os.environ["AUTO_FOUNDRY_CANNED_REPLAY_OFFLINE_GUARD"] = "1"
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        for method_name, method in self._socket_methods.items():
            setattr(socket.socket, method_name, method)
        socket.create_connection = self._create_connection  # type: ignore[assignment]
        os.environ.pop("AUTO_FOUNDRY_CANNED_REPLAY_OFFLINE_GUARD", None)


@contextmanager
def no_external_call_guard() -> Any:
    """Public test hook for proving the replay rejects socket attempts."""

    with _OfflineCallGuard() as guard:
        yield guard


def _semantic_value(value: Any, *, run_root: Path | None = None) -> Any:
    """Normalize durable content while excluding only runtime volatility."""

    if isinstance(value, Mapping):
        return {
            str(key): _semantic_value(item, run_root=run_root)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _VOLATILE_DIGEST_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_value(item, run_root=run_root) for item in value]
    if isinstance(value, str) and run_root is not None:
        return (
            value.replace(str(run_root), "<RUN_ROOT>")
            .replace(str(run_root.parent), "<CYCLE_ROOT>")
            .replace(str(run_root.parent.parent), "<TEMP_ROOT>")
        )
    return value


def _semantic_hash(value: Any, *, run_root: Path | None = None) -> str:
    return _sha256_bytes(_json_bytes(_semantic_value(value, run_root=run_root)))


def _file_semantic_hash(path: Path, *, run_root: Path | None = None) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", "<HASH>", text, flags=re.IGNORECASE)
        return _semantic_hash(text, run_root=run_root)
    return _semantic_hash(value, run_root=run_root)


def _normalized_report_projection(report_path: Path) -> dict[str, Any]:
    """Project only stable report semantics for the replay digest.

    Final reports intentionally retain run-specific receipt and artifact hashes.
    Those bytes are useful audit evidence, but they are not part of the
    cross-cycle semantic identity.  Keep the projection explicit so a change
    to a review conclusion, finding, outcome, or role receipt still changes
    the digest while IDs, timestamps, and generated hashes do not.
    """

    report = json.loads(report_path.read_text(encoding="utf-8"))

    def review_projection(reviews: Any) -> list[dict[str, Any]]:
        def semantic_details(review: Mapping[str, Any], field: str) -> list[Any]:
            raw_values = review.get(field, [])
            if not isinstance(raw_values, list):
                return []
            normalized: list[Any] = []
            for value in raw_values:
                if isinstance(value, Mapping):
                    # Artifact/result hashes are generated from run-local
                    # bytes. Keep the actual scope, IDs, pointers, verdicts,
                    # and unchanged-proof fields while excluding those
                    # volatile hash values from the cross-cycle projection.
                    value = {
                        key: child
                        for key, child in value.items()
                        if key
                        not in {
                            "before_hash",
                            "after_hash",
                            "draft_hash",
                            "result_hash",
                            "artifact_hash",
                            "corrected_record_hashes",
                        }
                    }
                normalized.append(_semantic_value(value))
            return sorted(normalized, key=_json_bytes)

        projected: list[dict[str, Any]] = []
        for review in reviews if isinstance(reviews, list) else []:
            if not isinstance(review, Mapping):
                continue
            findings: list[dict[str, Any]] = []
            raw_findings = review.get("findings", [])
            for finding in raw_findings if isinstance(raw_findings, list) else []:
                if not isinstance(finding, Mapping):
                    continue
                findings.append(
                    _semantic_value(
                        {
                            key: finding.get(key)
                            for key in (
                                "finding_id",
                                "message",
                                "material",
                                "pointers",
                                "dependent_outputs",
                                "artifact_paths",
                                "record_ids",
                                "dependency_ids",
                                "parts",
                            )
                            if key in finding
                        }
                    )
                )
            findings.sort(key=lambda value: (str(value.get("finding_id", "")), _json_bytes(value)))
            projected.append(
                {
                    "item_id": review.get("item_id"),
                    "review_kind": review.get("review_kind"),
                    "fidelity_review_kind": review.get("fidelity_review_kind"),
                    "verdict": review.get("verdict"),
                    "record_pointer": review.get("record_pointer"),
                    "findings": findings,
                    "finding_count": int(review.get("finding_count", len(findings)) or 0),
                    "repair_count": int(review.get("repair_count", 0) or 0),
                    "business_repair_count": int(review.get("business_repair_count", 0) or 0),
                    "targeted_recheck_count": int(review.get("targeted_recheck_count", 0) or 0),
                    "repairs": semantic_details(review, "repairs"),
                    "targeted_rechecks": semantic_details(review, "targeted_rechecks"),
                }
            )
        projected.sort(key=lambda value: (str(value.get("item_id", "")), str(value.get("review_kind", ""))))
        return projected

    receipts: list[dict[str, Any]] = []
    raw_receipts = report.get("receipts", [])
    for receipt in raw_receipts if isinstance(raw_receipts, list) else []:
        if not isinstance(receipt, Mapping):
            continue
        artifact_delta = receipt.get("artifact_delta")
        conclusion = artifact_delta.get("conclusion") if isinstance(artifact_delta, Mapping) else None
        receipts.append(
            {
                "item_id": receipt.get("item_id"),
                "role": receipt.get("role"),
                "route": receipt.get("route"),
                "provider": receipt.get("provider"),
                "model": receipt.get("model"),
                "terminal_reason": receipt.get("terminal_reason"),
                "conclusion": conclusion,
            }
        )
    receipts.sort(
        key=lambda value: (
            str(value.get("item_id", "")),
            str(value.get("role", "")),
            str(value.get("route", "")),
            str(value.get("conclusion", "")),
        )
    )
    return {
        "lifecycle_status": report.get("lifecycle_status"),
        "item_outcomes": _semantic_value(report.get("item_outcomes", {})),
        "outcome_counts": _semantic_value(report.get("outcome_counts", {})),
        "record_kind_totals": _semantic_value(report.get("record_kind_totals", {})),
        "registry_counts": _semantic_value(report.get("registry_counts", {})),
        "lem_counts": _semantic_value(report.get("lem_counts", {})),
        "business_reviews": review_projection(report.get("business_reviews", [])),
        "fidelity_reviews": review_projection(report.get("fidelity_reviews", [])),
        "receipts": receipts,
    }


def _tree_fingerprint(path: Path) -> str:
    if not path.exists():
        return "<absent>"
    if path.is_file():
        return _sha256(path)
    entries: list[dict[str, str]] = []
    for child in sorted(path.rglob("*")):
        if child.is_file():
            entries.append({"path": child.relative_to(path).as_posix(), "hash": _sha256(child)})
    return _sha256_bytes(_json_bytes(entries))


def _fingerprint(paths: tuple[Path, ...] = (), snapshot: Any = None) -> str:
    payload: dict[str, Any] = {str(path): _tree_fingerprint(path) for path in paths}
    if snapshot is not None:
        payload["snapshot"] = _semantic_value(snapshot)
    return _semantic_hash(payload)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper {path}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses and other reflection-based helpers expect the module to be
    # registered while executing a file loaded outside the normal importer.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_fixture(path: Path = FIXTURE_PATH) -> Mapping[str, Any]:
    """Load and validate the human-readable canned role fixture."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("canned replay fixture is unreadable") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != "benchmark_a.canned_agent_replay.v2":
        raise ValueError("canned replay fixture schema is invalid")
    questions = payload.get("questions")
    if not isinstance(questions, list) or tuple(item.get("item_id") for item in questions) != QUESTION_IDS:
        raise ValueError("canned replay questions must be Q-001, Q-002, Q-003 in order")
    required_roles = {
        "Q-001": {"analytical_owner", "specialists", "business_reviewer", "integration", "fidelity_reviewer"},
        "Q-002": {
            "analytical_owner",
            "specialists",
            "business_reviewer",
            "targeted_business_reviewer",
            "second_targeted_business_reviewer",
            "integration",
            "fidelity_reviewer",
        },
        "Q-003": {"analytical_owner", "specialists", "business_reviewer", "integration", "fidelity_reviewer", "targeted_fidelity_reviewer"},
    }
    for question in questions:
        if not isinstance(question, Mapping) or not isinstance(question.get("text"), str) or not question["text"].strip():
            raise ValueError("canned replay question text is invalid")
        item_id = str(question.get("item_id"))
        if set(question) != {"item_id", "text", *required_roles[item_id]}:
            raise ValueError(f"canned replay {item_id} role keys are invalid")
        for role, value in question.items():
            if role in {"item_id", "text", "specialists"}:
                continue
            if not isinstance(value, Mapping) or not isinstance(value.get("conclusion"), str):
                raise ValueError(f"canned replay {question.get('item_id')} {role} response is invalid")
        specialists = question.get("specialists")
        if not isinstance(specialists, list) or len(specialists) > 3:
            raise ValueError(f"canned replay {item_id} specialists are invalid")
        task_ids: set[str] = set()
        for specialist in specialists:
            required = {
                "task_id", "specialty", "question", "expected_output", "conclusion",
                "method", "limitations", "open_questions", "confidence", "status",
            }
            if not isinstance(specialist, Mapping) or set(specialist) != required:
                raise ValueError(f"canned replay {item_id} specialist response is invalid")
            task_id = specialist.get("task_id")
            if not isinstance(task_id, str) or not task_id or task_id in task_ids:
                raise ValueError(f"canned replay {item_id} specialist task_id is invalid")
            task_ids.add(task_id)
        if item_id == "Q-003":
            fidelity = question["fidelity_reviewer"]
            if not isinstance(fidelity.get("record_pointer"), str) or not fidelity["record_pointer"].strip():
                raise ValueError("Q-003 fidelity response requires record_pointer")
            if fidelity.get("record_semantic_key") != "metric_id":
                raise ValueError("Q-003 fidelity response requires metric_id semantic key")
        if item_id == "Q-002":
            targeted = question["targeted_business_reviewer"]
            required_targeted = {
                "verdict",
                "second_finding_id",
                "second_target_sections",
                "second_semantic_categories",
                "second_problem",
                "second_evidence",
                "second_required_change",
                "conclusion",
                "status",
            }
            if set(targeted) != required_targeted or targeted.get("verdict") != "repair_once":
                raise ValueError("Q-002 targeted business response must authorize a second repair")
            final_targeted = question["second_targeted_business_reviewer"]
            if final_targeted.get("verdict") not in {"accept", "accept_with_limits"}:
                raise ValueError("Q-002 final targeted business response must accept")
    role_conclusions = payload.get("role_conclusions")
    if not isinstance(role_conclusions, Mapping) or set(role_conclusions) != {
        "analytical_owner",
        "specialist",
        "business_reviewer",
        "integration",
        "fidelity_reviewer",
        "product",
        "optimizer",
    }:
        raise ValueError("canned replay role conclusions are missing")
    return payload


def _response(question: Mapping[str, Any], role: str) -> dict[str, Any]:
    value = question.get(role)
    if not isinstance(value, Mapping):
        raise ValueError(f"fixture response is missing: {question.get('item_id')}:{role}")
    result = dict(value)
    result.update(
        {
            "replay_label": REPLAY_LABEL,
            "provider": UNAVAILABLE,
            "model": UNAVAILABLE,
            "model_calls": 0,
            "network_calls": 0,
        }
    )
    return result


def _role_response(fixture: Mapping[str, Any], role: str) -> dict[str, Any]:
    """Normalize a top-level fixture conclusion like a question response."""

    conclusions = fixture.get("role_conclusions")
    if not isinstance(conclusions, Mapping) or not isinstance(conclusions.get(role), str):
        raise ValueError(f"fixture role conclusion is missing: {role}")
    return {
        "conclusion": conclusions[role],
        "status": "pass",
        "replay_label": REPLAY_LABEL,
        "provider": UNAVAILABLE,
        "model": UNAVAILABLE,
        "model_calls": 0,
        "network_calls": 0,
    }


def _append_role_receipt(
    ledger: InvocationReceiptLedger,
    *,
    invocation_id: str,
    item_id: str,
    attempt_id: str,
    lane_id: str,
    role: str,
    response: Mapping[str, Any],
) -> str:
    receipt = AgentInvocationReceipt(
        invocation_id=invocation_id,
        item_id=item_id,
        attempt_id=attempt_id,
        lane_id=lane_id,
        role=role,
        route=REPLAY_LABEL,
        provider=UNAVAILABLE,
        model=UNAVAILABLE,
        start=FIXED_START,
        first_activity=FIXED_START,
        finish=FIXED_FINISH,
        terminal_reason="completed",
        artifact_delta={
            "replay_label": REPLAY_LABEL,
            "provider": UNAVAILABLE,
            "model": UNAVAILABLE,
            "model_calls": 0,
            "network_calls": 0,
            "conclusion": response.get("conclusion", ""),
        },
        tool_calls=0,
    )
    return ledger.append(receipt)


def _write_q1_scripts(item: ItemWorkspace) -> tuple[Path, Path, Path]:
    calculations = item.work_root / "calculations"
    calculations.mkdir(parents=True, exist_ok=True)
    invalid = calculations / "invalid.py"
    invalid.write_text("raise NameError('canned same-attempt repair')\n", encoding="utf-8")
    corrected = calculations / "corrected.py"
    corrected.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "import json\n"
        "from auto_foundry_core.analysis import load_bound_analysis_context\n"
        "assert os.environ.get('AUTO_FOUNDRY_CANNED_REPLAY_OFFLINE_GUARD') == '1'\n"
        "ctx = load_bound_analysis_context()\n"
        "Path('analysis.json').write_text(json.dumps({'item_id': ctx.item_workspace.item_id, 'status': 'pass'}, sort_keys=True), encoding='utf-8')\n",
        encoding="utf-8",
    )
    for script in (invalid, corrected):
        source = script.read_text(encoding="utf-8").lower()
        if any(marker in source for marker in _FORBIDDEN_IMPORT_MARKERS):
            raise AssertionError(f"generated replay script contains forbidden import: {script.name}")
    return invalid, corrected, item.work_root / "analysis.json"


def _create_archive(input_root: Path) -> Path:
    archive = input_root / "synthetic_fixture.zip"
    records = (
        "order_id,stage,amount\n"
        "O-001,warehouse,10\n"
        "O-002,carrier,20\n"
        "O-003,customer,30\n"
    ).encode("utf-8")
    # CoreRuntime's source preview resolves a run-relative source file while
    # DataRoomWorkbench indexes the immutable archive.  Keep the extracted
    # fixture beside the archive so both real boundaries use identical bytes.
    (input_root / "orders.csv").write_bytes(records)
    info = zipfile.ZipInfo("orders.csv")
    info.date_time = (2020, 1, 1, 0, 0, 0)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(info, records)
    return archive


def _materialize_owner_work(
    analyst: AnalystWorkspace,
    question: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    source_id: str,
) -> int:
    """Translate readable cassette conclusions through the analyst facade."""

    analyst.begin_analysis(
        objective=str(question["text"]),
        strategy=str(response.get("method", response["conclusion"])),
        expected_outputs=("complete business answer", "reviewed dashboard fact"),
        assumptions=("Synthetic fixture only",),
    )
    analyst.select_sources((source_id,), purpose="Bounded synthetic order evidence")
    analyst.record_evidence(
        EvidenceNote(
            evidence_id=f"E-{analyst.item_workspace.item_id}",
            conclusion=str(response["conclusion"]),
            method=str(response.get("method", "Inspect the bounded synthetic fixture.")),
            evidence_refs=(source_id,),
            limitations=("Synthetic fixture only; no production interpretation.",),
            facts={"fixture_row_count": 3, "model_calls": 0, "network_calls": 0},
        )
    )
    specialists = question.get("specialists", [])
    if not isinstance(specialists, list):
        raise ValueError("specialists must be a list")
    for index, value in enumerate(specialists, start=1):
        if not isinstance(value, Mapping):
            raise ValueError("specialist cassette value must be an object")
        task_id = str(value["task_id"])
        analyst.assign_specialist(
            SpecialistTask(
                task_id=task_id,
                specialty=str(value["specialty"]),
                question=str(value["question"]),
                expected_output=str(value["expected_output"]),
                source_ids=(source_id,),
                context="Use only the active synthetic fixture item.",
            )
        )
        analyst.record_specialist_memo(
            SpecialistMemo(
                memo_id=f"M-{analyst.item_workspace.item_id}-{index:02d}",
                task_id=task_id,
                conclusion=str(value["conclusion"]),
                method=str(value["method"]),
                evidence_refs=(source_id,),
                limitations=tuple(str(item) for item in value["limitations"]),
                open_questions=tuple(str(item) for item in value["open_questions"]),
                confidence=str(value["confidence"]),
            )
        )
    return len(specialists)


def _submit_owner_answer(
    analyst: AnalystWorkspace,
    response: Mapping[str, Any],
    *,
    repaired: bool = False,
    repair_round: int | None = None,
) -> dict[str, Any]:
    conclusion = str(response["conclusion"])
    answer_text = conclusion
    if repaired or repair_round is not None:
        round_number = int(repair_round or 1)
        answer_text = (
            conclusion
            + f" Corrected in business repair round {round_number}: "
            + "this is a descriptive fixture observation, not a causal claim."
        )
    return analyst.submit_answer(
        AnalystAnswer(
            answer=answer_text,
            headline_findings=(conclusion,),
            scope="Three synthetic fixture orders only.",
            method=str(response.get("method", "Inspect the bounded synthetic fixture.")),
            supported_components=("Fixture-level descriptive conclusion",),
            unsupported_components=("Production, causal, or predictive inference",),
            limitations=("Synthetic fixture only; no production interpretation.",),
            next_actions=("Use reviewed production evidence before operational action.",),
            visuals=(
                {
                    "visual_id": f"V-{analyst.item_workspace.item_id}",
                    "kind": "summary_card",
                    "title": "Reviewed fixture conclusion",
                },
            ),
            evidence_refs=(f"work/evidence.jsonl#E-{analyst.item_workspace.item_id}",),
        )
    )


def _integration_records(session: IntegrationSession, item_id: str, prepared_descriptor: Any) -> list[str]:
    evidence = ("work/plan.json",)
    records = [
        session.add_ontology_item(
            OntologyItem(
                item_id=f"entity-{item_id.lower()}",
                item_type="entity",
                label=f"Synthetic entity {item_id}",
                scope="question",
            ),
            evidence_refs=evidence,
        ),
        session.add_metric(
            {
                "metric_id": f"metric-{item_id.lower()}",
                "label": f"Synthetic metric {item_id}",
                "value": 1,
                "unit": "fixture units",
            },
            scope="question",
            evidence_refs=evidence,
        ),
        session.add_claim(
            {"claim_id": f"claim-{item_id.lower()}", "text": f"{item_id} fixture claim"},
            scope="question",
            evidence_refs=evidence,
        ),
        session.add_limitation(
            {"limitation_id": f"limit-{item_id.lower()}", "text": "Synthetic fixture only"},
            scope="question",
            evidence_refs=evidence,
        ),
        session.add_dashboard_fact(
            {"fact_id": f"fact-{item_id.lower()}", "value": "reviewed fixture"},
            scope="question",
            evidence_refs=evidence,
        ),
        session.register_prepared_asset(prepared_descriptor, evidence_refs=evidence),
    ]
    return records


def _assert_fault(
    name: str,
    action: Any,
    faults: list[dict[str, str]],
    *,
    expected_type: type[BaseException] = ValueError,
    message_substring: str,
    paths: tuple[Path, ...] = (),
    snapshot: Any = None,
) -> None:
    before = _fingerprint(paths, snapshot() if callable(snapshot) else snapshot)
    try:
        action()
    except Exception as exc:  # expected fail-closed boundary
        if not isinstance(exc, expected_type):
            raise AssertionError(
                f"fault probe {name} raised {type(exc).__name__}, expected {expected_type.__name__}"
            ) from exc
        if message_substring not in str(exc):
            raise AssertionError(
                f"fault probe {name} message did not contain {message_substring!r}: {exc}"
            ) from exc
        after = _fingerprint(paths, snapshot() if callable(snapshot) else snapshot)
        if before != after:
            raise AssertionError(f"fault probe {name} mutated state or files")
        faults.append(
            {
                "probe": name,
                "status": "PASS",
                "error": type(exc).__name__,
                "message": message_substring,
                "rollback": "PASS",
            }
        )
        return
    raise AssertionError(f"fault probe did not reject: {name}")


def _run_item(
    *,
    context: RunContext,
    archive: Path,
    workbench: DataRoomWorkbench,
    lifecycle: RunLifecycle,
    ledger: InvocationReceiptLedger,
    question: Mapping[str, Any],
    registry: PreparedAssetRegistry,
    faults: list[dict[str, str]],
) -> tuple[ItemWorkspace, IntegrationSession, dict[str, Any], dict[str, Any]]:
    item_id = str(question["item_id"])
    item = ItemWorkspace.create(context, item_id, original_text=str(question["text"]))
    bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), item, workbench=workbench)
    analyst = AnalystWorkspace(bound, owner_ref=f"owner-{item_id}")
    runner = bound.script_runner
    base_environment = runner._environment

    def guarded_environment(phase: str, sample_limit: int, *, output_root: Path) -> dict[str, str]:
        environment = base_environment(phase, sample_limit, output_root=output_root)
        environment["AUTO_FOUNDRY_CANNED_REPLAY_OFFLINE_GUARD"] = "1"
        return environment

    runner._environment = guarded_environment
    members = workbench.data_room.search("orders.csv", catalog=workbench.catalog(), limit=1)
    if len(members) != 1:
        raise AssertionError("canned replay expected exactly one DataRoom orders member")
    prepared_descriptor = analyst.prepare_data(
        f"replay-order-facts-{item_id.lower()}",
        members[0],
        scope="reusable",
        transformations=("bounded_fixture_normalization",),
        limitations=("Synthetic fixture only; no production interpretation.",),
    )
    owner = _response(question, "analytical_owner")
    source_id = analyst.search_sources("orders.csv", limit=1)[0].source_id
    specialist_count = _materialize_owner_work(analyst, question, owner, source_id=source_id)
    attempt = item.begin_attempt(REPLAY_LABEL, "Analytical Owner")
    owner_receipt = f"I-{item_id}-ANALYTICAL-OWNER"
    script_phases: list[str] = []
    if item_id == "Q-001":
        invalid, corrected, output = _write_q1_scripts(item)
        invalid_report = analyst.run_analysis(invalid)
        if not invalid_report.same_attempt_feedback or invalid_report.receipts[0].error_type != "NameError":
            raise AssertionError("Q-001 NameError replay did not produce same-attempt feedback")
        script_phases.append("NameError")
        corrected_report = analyst.run_analysis(
            corrected,
            outputs=(output,),
            deterministic_outputs=(output,),
        )
        if not corrected_report.succeeded or corrected_report.deterministic_match is not True:
            raise AssertionError("Q-001 corrected controlled pipeline did not pass")
        script_phases.extend(receipt.phase for receipt in corrected_report.receipts)
    else:
        operation = CoreRuntime(context).execute(
            OperationSpec("sources.preview", parameters={"path": "orders.csv", "limit": 1})
        )
        if operation.receipt.capability_id != "sources.preview":
            raise AssertionError("analytical owner preview receipt is not a real CoreRuntime receipt")
    _append_role_receipt(
        ledger,
        invocation_id=owner_receipt,
        item_id=item_id,
        attempt_id=attempt.attempt_id,
        lane_id="canned-analytical-owner",
        role="Analytical Owner",
        response={**owner, "script_phases": script_phases},
    )
    for index, specialist in enumerate(question.get("specialists", []), start=1):
        _append_role_receipt(
            ledger,
            invocation_id=f"I-{item_id}-SPECIALIST-{index:02d}",
            item_id=item_id,
            attempt_id=attempt.attempt_id,
            lane_id=f"canned-specialist-{index:02d}",
            role="Specialist",
            response=specialist,
        )
    item.finish_attempt(attempt.attempt_id, status="completed")

    business = _response(question, "business_reviewer")
    business_initial_result: dict[str, Any] | None = None
    business_final_result: dict[str, Any]
    business_repair_packets: list[dict[str, Any]] = []
    business_targeted_results: list[dict[str, Any]] = []
    business_findings: list[dict[str, Any]] = []
    if item_id == "Q-002":
        _submit_owner_answer(analyst, owner)
        business_initial_result = analyst.review.record(
            "repair_once",
            reviewer_ref=f"{REPLAY_LABEL}:business-review",
            findings=(
                ReviewFinding(
                    finding_id=str(business["finding_id"]),
                    target_sections=tuple(str(section) for section in business["target_sections"]),
                    semantic_categories=tuple(str(category) for category in business["semantic_categories"]),
                    problem=str(business["problem"]),
                    evidence=str(business["evidence"]),
                    required_change=str(business["required_change"]),
                ),
            ),
        )
        business_findings.extend(business_initial_result.get("findings", []))
        _append_role_receipt(
            ledger,
            invocation_id=f"I-{item_id}-BUSINESS-INITIAL",
            item_id=item_id,
            attempt_id=attempt.attempt_id,
            lane_id="canned-business-review",
            role="Independent Business Reviewer",
            response=business,
        )
        first_packet = analyst.review.begin_repair()
        # Reviewer categories remain semantic provenance. Once the same owner
        # consumes the bounded repair, any artifact inside this item's work/
        # tree may be corrected; cross-item boundaries remain enforced by the
        # ItemWorkspace path resolver.
        item.write_open_issues({"status": "item-local repair permitted"})
        if first_packet["before_hash"] != business_initial_result["draft_hash"]:
            raise AssertionError("first business repair baseline is not the reviewed draft")
        business_repair_packets.append(first_packet)
        _submit_owner_answer(analyst, owner, repaired=True, repair_round=1)
        targeted_business = _response(question, "targeted_business_reviewer")
        first_targeted = analyst.review.record(
            "repair_once",
            reviewer_ref=f"{REPLAY_LABEL}:business-targeted-repair",
            findings=(
                ReviewFinding(
                    finding_id=str(targeted_business["second_finding_id"]),
                    target_sections=tuple(str(section) for section in targeted_business["second_target_sections"]),
                    semantic_categories=tuple(str(category) for category in targeted_business["second_semantic_categories"]),
                    problem=str(targeted_business["second_problem"]),
                    evidence=str(targeted_business["second_evidence"]),
                    required_change=str(targeted_business["second_required_change"]),
                ),
            ),
        )
        if not first_targeted.get("targeted_recheck") or first_targeted.get("verdict") != "repair_once":
            raise AssertionError("Q-002 first business targeted repair was not recorded")
        business_targeted_results.append(first_targeted)
        business_findings.extend(first_targeted.get("findings", []))
        _append_role_receipt(
            ledger,
            invocation_id=f"I-{item_id}-BUSINESS-TARGETED-REPAIR",
            item_id=item_id,
            attempt_id=attempt.attempt_id,
            lane_id="canned-business-review-targeted",
            role="Independent Business Reviewer",
            response=_response(question, "targeted_business_reviewer"),
        )
        second_packet = analyst.review.begin_repair()
        if second_packet["before_hash"] != first_targeted["draft_hash"]:
            raise AssertionError("second business repair did not reset to the current targeted baseline")
        if second_packet["before_hash"] == first_packet["before_hash"]:
            raise AssertionError("two business repair baselines are not distinct")
        business_repair_packets.append(second_packet)
        _submit_owner_answer(analyst, owner, repaired=True, repair_round=2)
        final_targeted_business = _response(question, "second_targeted_business_reviewer")
        business_final_result = analyst.review.record(
            str(final_targeted_business["verdict"]),
            reviewer_ref=f"{REPLAY_LABEL}:business-targeted-final",
        )
        if not business_final_result.get("targeted_recheck"):
            raise AssertionError("Q-002 second business targeted recheck was not recorded")
        business_targeted_results.append(business_final_result)
        _append_role_receipt(
            ledger,
            invocation_id=f"I-{item_id}-BUSINESS-TARGETED-FINAL",
            item_id=item_id,
            attempt_id=attempt.attempt_id,
            lane_id="canned-business-review-targeted-final",
            role="Independent Business Reviewer",
            response=_response(question, "second_targeted_business_reviewer"),
        )
        if item.state["business_repair_count"] != 2:
            raise AssertionError("Q-002 expected exactly two business repairs")
        business_stage = "iterative_repairs_then_targeted_accept"
    else:
        _submit_owner_answer(analyst, owner)
        verdict = str(business["verdict"])
        business_final_result = analyst.review.record(
            verdict,
            reviewer_ref=f"{REPLAY_LABEL}:business-review",
        )
        _append_role_receipt(
            ledger,
            invocation_id=f"I-{item_id}-BUSINESS",
            item_id=item_id,
            attempt_id=attempt.attempt_id,
            lane_id="canned-business-review",
            role="Independent Business Reviewer",
            response=business,
        )
        business_stage = verdict
    accepted_refs = ["work/plan.json", "work/source_map.json", "work/evidence.jsonl"]
    if specialist_count:
        accepted_refs.extend(("work/specialist_tasks.jsonl", "work/specialist_memos.jsonl"))
    analyst.accept(accepted_refs=tuple(accepted_refs))
    persisted_item = ItemWorkspace.load(context, item_id)
    persisted_packet = json.loads(persisted_item.business_review_path.read_text(encoding="utf-8"))
    business_record = {
        "review_id": f"BR-{item_id}",
        "item_id": item_id,
        "verdict": business_final_result["verdict"],
        "reviewer_ref": business_final_result.get("reviewer_ref"),
        "findings": business_findings or list((business_initial_result or business_final_result).get("findings", [])),
        "repairs": (
            [
                {
                    "repair_id": f"R-{item_id}-BUSINESS-{index:02d}",
                    "repair_round": index,
                    "before_hash": packet["before_hash"],
                    "after_hash": business_targeted_results[index - 1]["draft_hash"],
                    "allowed_pointers": packet["allowed_pointers"],
                    "allowed_artifact_paths": packet["allowed_artifact_paths"],
                    "allowed_dependencies": packet["allowed_dependencies"],
                    "unchanged_aggregate_hash": packet["unchanged_aggregate_hash"],
                    "review_scope": packet["review_scope"],
                }
                for index, packet in enumerate(business_repair_packets, start=1)
            ]
        ),
        "targeted_rechecks": (
            [
                {
                    "recheck_id": f"RR-{item_id}-BUSINESS-{index:02d}",
                    "recheck_round": index,
                    "verdict": result["verdict"],
                    "draft_hash": result["draft_hash"],
                    "changed_pointers": result.get("changed_pointers", []),
                    "review_scope": result.get("review_scope"),
                }
                for index, result in enumerate(business_targeted_results, start=1)
            ]
        ),
        "artifact_ref": "work/business_review.json",
        "artifact_hash": _sha256(persisted_item.business_review_path),
        "persisted_review_scope": persisted_packet["review_scope"],
        "business_repair_count": persisted_item.state["business_repair_count"],
    }

    integration_response = _response(question, "integration")
    integration = IntegrationSession.create(
        context,
        item,
        registry,
        "canned-result-integration",
        f"INTEGRATION-{item_id}",
    )
    record_ids = _integration_records(integration, item_id, prepared_descriptor)
    _append_role_receipt(
        ledger,
        invocation_id=f"I-{item_id}-INTEGRATION",
        item_id=item_id,
        attempt_id=attempt.attempt_id,
        lane_id="canned-result-integration",
        role="Result Integration Agent",
        response=integration_response,
    )
    fidelity_response = _response(question, "fidelity_reviewer")
    fidelity_initial_result: Any = None
    fidelity_final_result: Any = None
    if item_id == "Q-001":
        before_lem = json.dumps(integration.lem.export(), sort_keys=True)
        before_registry = tuple(registry.search())
        _assert_fault(
            "commit_before_fidelity",
            integration.commit,
            faults,
            expected_type=ValueError,
            message_substring="requires durable fidelity acceptance",
            paths=(integration.staging_root, item.item_root / "item_state.json"),
            snapshot=lambda: {"lem": integration.lem.export(), "registry": registry.search()},
        )
        if json.dumps(integration.lem.export(), sort_keys=True) != before_lem or tuple(registry.search()) != before_registry:
            raise AssertionError("commit-before-fidelity mutated LEM or registry")
        _assert_fault(
            "incomplete_fidelity_checked_ids",
            lambda: integration.record_fidelity_review("accept", checked_record_ids=(record_ids[0],)),
            faults,
            expected_type=ValueError,
            message_substring="checked_record_ids",
            paths=(integration.staging_root,),
            snapshot=lambda: {"records": [record.to_dict() for record in integration.records]},
        )
        integration.build_fidelity_packet()
        fidelity_final_result = integration.record_fidelity_review("accept", checked_record_ids=tuple(record_ids))
        fidelity_stage = "accept"
    elif item_id == "Q-003":
        integration.build_fidelity_packet()
        pointer = fidelity_response["record_pointer"]
        semantic_key = fidelity_response["record_semantic_key"]
        candidates = [
            record
            for record in integration.records
            if record.kind == "metric" and record.payload.get(semantic_key) == pointer
        ]
        if len(candidates) != 1:
            raise AssertionError(f"Q-003 fixture pointer did not resolve exactly one metric: {pointer}")
        risk_record = candidates[0]
        fidelity_initial_result = integration.record_fidelity_review(
            "repair_once",
            checked_record_ids=tuple(record_ids),
            findings=[
                {
                    "finding_id": fidelity_response["finding_id"],
                    "message": fidelity_response["conclusion"],
                    "record_ids": [risk_record.record_id],
                }
            ],
        )
        corrected_payload = dict(risk_record.payload)
        corrected_payload["label"] = "Corrected synthetic metric Q-003"
        integration.correct_record(risk_record.record_id, corrected_payload)
        integration.build_fidelity_packet()
        fidelity_final_result = integration.record_fidelity_review(
            "accept",
            review_kind="targeted",
            checked_record_ids=(risk_record.record_id,),
        )
        _append_role_receipt(
            ledger,
            invocation_id=f"I-{item_id}-FIDELITY-TARGETED",
            item_id=item_id,
            attempt_id=attempt.attempt_id,
            lane_id="canned-fidelity-targeted",
            role="Integration Fidelity Reviewer",
            response=_response(question, "targeted_fidelity_reviewer"),
        )
        fidelity_stage = "repair_once_then_targeted_accept"
    else:
        integration.build_fidelity_packet()
        fidelity_final_result = integration.record_fidelity_review("accept", checked_record_ids=tuple(record_ids))
        fidelity_stage = "accept"
    _append_role_receipt(
        ledger,
        invocation_id=f"I-{item_id}-FIDELITY",
        item_id=item_id,
        attempt_id=attempt.attempt_id,
        lane_id="canned-fidelity",
        role="Integration Fidelity Reviewer",
        response=fidelity_response,
    )
    fidelity_artifact_hash = _sha256(integration.fidelity_result_path)
    persisted_fidelity = fidelity_final_result.to_dict()
    fidelity_record = {
        "review_id": f"FR-{item_id}",
        "item_id": item_id,
        "verdict": persisted_fidelity["verdict"],
        "review_kind": "fidelity",
        "fidelity_review_kind": persisted_fidelity["review_kind"],
        "findings": (
            fidelity_initial_result.to_dict().get("findings", [])
            if fidelity_initial_result is not None
            else persisted_fidelity.get("findings", [])
        ),
        "repairs": [],
        "targeted_rechecks": [],
        "result_hash": persisted_fidelity["result_hash"],
        "artifact_ref": "integration/fidelity_result.json",
        "artifact_hash": fidelity_artifact_hash,
        "record_pointer": fidelity_response.get("record_pointer"),
    }
    if fidelity_initial_result is not None:
        authorization = json.loads(integration.fidelity_authorization_path.read_text(encoding="utf-8"))
        progress = json.loads(integration.fidelity_progress_path.read_text(encoding="utf-8"))
        fidelity_record["repairs"] = [
            {
                "repair_id": f"R-{item_id}-FIDELITY",
                "before_hash": fidelity_initial_result.result_hash,
                "after_hash": persisted_fidelity["result_hash"],
                "affected_record_ids": authorization["affected_record_ids"],
                "dependency_ids": authorization["dependency_ids"],
                "corrected_record_hashes": progress["corrected_record_hashes"],
            }
        ]
        fidelity_record["targeted_rechecks"] = [
            {
                "recheck_id": f"RR-{item_id}-FIDELITY",
                "verdict": persisted_fidelity["verdict"],
                "checked_record_ids": persisted_fidelity["checked_record_ids"],
                "record_pointer": fidelity_response.get("record_pointer"),
            }
        ]
    manifest = integration.commit()
    if manifest.get("status") != "committed":
        raise AssertionError(f"{item_id} integration did not commit")
    return item, integration, {
        "item_id": item_id,
        "business": business_stage,
        "fidelity": fidelity_stage,
        "integration": "committed",
        "script_phases": script_phases,
        "specialist_count": specialist_count,
        "analytical_owner": "complete_loop",
        "record_kind_totals": {
            kind: sum(record.kind == kind for record in integration.records)
            for kind in sorted({record.kind for record in integration.records})
        },
    }, {"business": business_record, "fidelity": fidelity_record}


def _build_product_and_optimizer(
    context: RunContext,
    lifecycle: RunLifecycle,
    items: list[ItemWorkspace],
    ledger: InvocationReceiptLedger,
    fixture: Mapping[str, Any],
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    dashboard_renderer = _load_module(
        f"canned_dashboard_renderer_{context.run_id}",
        SCRIPTS / "dashboard_renderer.py",
    )
    evidence_collector = _load_module(
        f"canned_optimizer_collector_{context.run_id}",
        SCRIPTS / "optimizer_evidence_collector.py",
    )
    runtime = CoreRuntime(context)
    runtime.write_manifest(
        "reviewed_widgets.json",
        {
            "title": "Canned replay reviewed product",
            "run_id": context.run_id,
            "review_status": "reviewed",
            "limitations": ["Synthetic fixture only."],
            "freeze_markers": FreezeMarkers(True, True, True, True, True).to_dict(),
            "domains": [
                {
                    "id": "synthetic-domain",
                    "title": "Synthetic domain",
                    "order": 1,
                    "decision_flow": [
                        {
                            "id": "synthetic-flow",
                            "title": "Synthetic decision flow",
                            "order": 1,
                            "widget_ids": ["replay-kpi"],
                        }
                    ],
                }
            ],
            "widgets": [
                {
                    "id": "replay-kpi",
                    "type": "kpi",
                    "title": "Canned replay status",
                    "value": "3/3",
                    "unit": "items committed",
                    "review_status": "reviewed",
                    "reviewed_item_ref": "Q-001",
                    "reviewed_output_ref": "questions/Q-001/accepted/answer_content.json",
                    "evidence_refs": ["questions/Q-001/accepted/answer_content.json"],
                    "trace_refs": ["telemetry/invocation_receipts.jsonl"],
                }
            ],
        },
    )
    dashboard_manifest = dashboard_renderer.render_fixture(
        context,
        "reviewed_widgets.json",
        "dashboard/index.html",
        "dashboard/manifest.json",
    )
    runtime.write_manifest(
        "products/product_manifest.json",
        {
            "run_id": context.run_id,
            "freeze_markers": FreezeMarkers(True, True, True, True, True).to_dict(),
            "review_status": "reviewed",
            "dashboard": dashboard_manifest,
        },
    )
    traces = context.run_root / "traces"
    traces.mkdir(parents=True, exist_ok=True)
    (traces / "canned-replay.md").write_text(
        "deterministic fixture evidence; no model or network calls\n",
        encoding="utf-8",
    )
    scripts = context.run_root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "role_output.py").write_text("print('deterministic fixture replay')\n", encoding="utf-8")
    optimizer = evidence_collector.collect_evidence(
        context,
        products_manifest="products/product_manifest.json",
        telemetry=("telemetry/events.jsonl", "telemetry/invocation_receipts.jsonl"),
        traces=("traces",),
        scripts=("scripts",),
        analytical_inputs=("reviewed_widgets.json", "products/dashboard/index.html"),
        analytical_complete=True,
    )
    if optimizer.get("optimizer_status") != "complete":
        raise AssertionError("optimizer evidence collector did not complete")
    product_response = _role_response(fixture, "product")
    optimizer_response = _role_response(fixture, "optimizer")
    _append_role_receipt(
        ledger,
        invocation_id="I-PRODUCT-RENDERER",
        item_id="Q-001",
        attempt_id="ATT-PRODUCT",
        lane_id="canned-product",
        role="Product Renderer",
        response=product_response,
    )
    _append_role_receipt(
        ledger,
        invocation_id="I-OPTIMIZER-COLLECTOR",
        item_id="Q-001",
        attempt_id="ATT-OPTIMIZER",
        lane_id="canned-optimizer",
        role="Optimizer Evidence Collector",
        response=optimizer_response,
    )
    return dashboard_manifest, optimizer


def _project_and_finalize(
    context: RunContext,
    lifecycle: RunLifecycle,
    items: list[ItemWorkspace],
    sessions: list[IntegrationSession],
    ledger: InvocationReceiptLedger,
    business_reviews: list[dict[str, Any]],
    fidelity_reviews: list[dict[str, Any]],
    optimizer: Mapping[str, Any],
    faults: list[dict[str, str]],
) -> tuple[str, str]:
    item_reports = []
    for item, session in zip(items, sessions):
        state = item.state
        item_reports.append(
            {
                "item_id": item.item_id,
                "outcome": "accepted_with_limits" if item.item_id == "Q-001" else "accepted",
                "lifecycle_state": state["lifecycle_state"],
                "record_kind_totals": {
                    kind: sum(record.kind == kind for record in session.records)
                    for kind in sorted({record.kind for record in session.records})
                },
                "implementation_sha": "a" * 40,
                "implementation_tree": "b" * 40,
                "implementation_version": "0.8.0",
            }
        )
    receipts = [receipt.to_dict() for receipt in ledger.receipts]
    reports = RunReportProjector(run_id=context.run_id).project(
        item_reports,
        receipts=receipts,
        business_reviews=business_reviews,
        fidelity_reviews=fidelity_reviews,
        lifecycle_status=lifecycle.state,
    )
    bad_report = json.loads(json.dumps(reports))
    bad_report["outcome_counts"] = {"accepted": 999}
    _assert_fault(
        "stale_report_rejected",
        lambda: RunReportFinalizer().finalize(bad_report, lifecycle_status=lifecycle.state),
        faults,
        expected_type=ValueError,
        message_substring="outcome_counts are stale",
        snapshot=lambda: bad_report,
    )
    # The replay now crosses the same persisted preflight boundary as a
    # production run.  Every required binding is sourced from the run's own
    # accepted manifests, registry/LEM projections, receipts, and review
    # events; no synthetic analytics are introduced.
    item_manifests: list[dict[str, Any]] = []
    for item, item_report in zip(items, item_reports):
        manifest = json.loads((item.accepted_root / "manifest.json").read_text(encoding="utf-8"))
        manifest["terminal_outcome"] = {"outcome": item_report["outcome"]}
        manifest["record_kind_totals"] = dict(item_report["record_kind_totals"])
        item_manifests.append(manifest)
    registry = sessions[0].prepared_registry
    registry_snapshot = {
        "counts": {"prepared_assets": len(registry.search())},
    }
    lem_export = sessions[-1].lem.export()
    lem_snapshot = {
        "counts": {
            str(key): len(value)
            for key, value in lem_export.items()
            if isinstance(value, (list, tuple, Mapping))
        }
    }
    timings = [
        {
            "timing_id": f"TIM-{receipt['invocation_id']}",
            "phase": str(receipt.get("role") or receipt.get("route") or "replay"),
            "item_id": receipt.get("item_id"),
            "start": receipt.get("start"),
            "finish": receipt.get("finish"),
            "wall_time_ms": 1000.0,
            "receipt_ref": receipt["invocation_id"],
        }
        for receipt in receipts
    ]
    implementation_identity = {"sha": "a" * 40, "tree": "b" * 40, "version": "0.8.0"}
    preflight = RunReportInputGatherer(context.run_root, run_id=context.run_id).gather(
        item_reports,
        item_manifests=item_manifests,
        registry_snapshot=registry_snapshot,
        lem_snapshot=lem_snapshot,
        event_bindings=RunReportEventBindings(
            invocation_receipts=receipts,
            timings=timings,
            incidents=(),
            business_reviews=business_reviews,
            fidelity_reviews=fidelity_reviews,
            implementation_transitions=(),
            implementation_metadata={"final": implementation_identity},
            implementation_identity=implementation_identity,
        ),
        lifecycle_status=lifecycle.state,
    )
    finalizer = RunReportFinalizer(context.run_root)
    receipt = finalizer.finalize(
        preflight,
        run_manifest={"run_id": context.run_id, "fixture": REPLAY_LABEL},
        authoritative_business_reviews=reports["business_reviews"],
        authoritative_fidelity_reviews=reports["fidelity_reviews"],
        lifecycle_status=lifecycle.state,
    )
    return receipt["report_hash"], receipt["receipt_hash"]


def _run_cycle(
    cycle_index: int,
    root: Path,
    fixture: Mapping[str, Any],
    guard: _OfflineCallGuard,
) -> dict[str, Any]:
    started = time.monotonic()
    input_root = root / "input"
    run_root = root / "run"
    input_root.mkdir(parents=True, exist_ok=True)
    run_root.mkdir(parents=True, exist_ok=True)
    archive = _create_archive(input_root)
    archive_before = _sha256(archive)
    context = RunContext(
        f"RUN-CANNED-REPLAY-{cycle_index:03d}",
        run_root,
        (input_root,),
        core_version="0.8.0",
        skill_version="0.7.1",
    )
    lifecycle = RunLifecycle.create(context, QUESTION_IDS)
    runtime = CoreRuntime(context)
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    catalog = workbench.catalog()
    preview_spec = OperationSpec("sources.preview", parameters={"path": "orders.csv", "limit": 2})
    preview_miss = runtime.execute(preview_spec)
    preview_hit = runtime.execute(preview_spec)
    if preview_miss.cache_status != "miss" or preview_hit.cache_status != "hit":
        raise AssertionError("canned replay cache miss/hit proof failed")
    ledger = InvocationReceiptLedger(context)
    registry = PreparedAssetRegistry(context)
    faults: list[dict[str, str]] = []
    _assert_fault(
        "offline_socket_connect",
        lambda: socket.create_connection(("127.0.0.1", 9), timeout=0.01),
        faults,
        expected_type=RuntimeError,
        message_substring="offline replay blocked external socket call",
    )
    questions = {str(value["item_id"]): value for value in fixture["questions"]}
    items: list[ItemWorkspace] = []
    sessions: list[IntegrationSession] = []
    business_reviews: list[dict[str, Any]] = []
    fidelity_reviews: list[dict[str, Any]] = []
    stage_facts: list[dict[str, Any]] = []
    for item_id in QUESTION_IDS:
        item, session, facts, review_records = _run_item(
            context=context,
            archive=archive,
            workbench=workbench,
            lifecycle=lifecycle,
            ledger=ledger,
            question=questions[item_id],
            registry=registry,
            faults=faults,
        )
        items.append(item)
        sessions.append(session)
        business_reviews.append(review_records["business"])
        fidelity_reviews.append(review_records["fidelity"])
        stage_facts.append(facts)
    conflicting = AgentInvocationReceipt(
        invocation_id="I-Q-001-ANALYTICAL-OWNER",
        item_id="Q-001",
        attempt_id="A-001",
        lane_id="canned-conflicting-replay",
        role="Analytical Owner",
        route="conflicting_fixture_replay",
        provider=UNAVAILABLE,
        model=UNAVAILABLE,
        start=FIXED_START,
        first_activity=FIXED_START,
        finish=FIXED_FINISH,
        terminal_reason="completed",
        artifact_delta={"replay_label": "conflicting_duplicate"},
        tool_calls=0,
    )
    _assert_fault(
        "conflicting_duplicate_invocation_id",
        lambda: ledger.append(conflicting),
        faults,
        expected_type=ValueError,
        message_substring="invocation_id is already recorded",
        paths=(ledger.path,),
    )
    # Reconciliation derives every currently valid transition in one locked
    # pass, so terminal business facts advance through analytical_complete to
    # integration_complete before products are published.
    if lifecycle.reconcile([item.state for item in items]).state != "integration_complete":
        raise AssertionError("analytical/integration lifecycle reconciliation failed")
    dashboard_manifest, optimizer = _build_product_and_optimizer(context, lifecycle, items, ledger, fixture)
    product_snapshot = lifecycle.reconcile(
        [item.state for item in items],
        product_status={"status": "complete"},
        optimizer_status=optimizer,
    )
    # The lifecycle reducer advances all currently valid product and terminal
    # transitions in the same authoritative pass; Q-001's accepted-with-limits
    # outcome therefore settles directly at complete_with_limits.
    if product_snapshot.state != "complete_with_limits":
        raise AssertionError("product lifecycle reconciliation failed")
    lifecycle.reconcile(
        [item.state for item in items],
        product_status={"status": "complete"},
        optimizer_status=optimizer,
    )
    report_hash, terminal_receipt_hash = _project_and_finalize(
        context,
        lifecycle,
        items,
        sessions,
        ledger,
        business_reviews,
        fidelity_reviews,
        optimizer,
        faults,
    )
    if lifecycle.state != "complete_with_limits":
        raise AssertionError(f"unexpected final lifecycle state: {lifecycle.state}")
    if _sha256(archive) != archive_before:
        raise AssertionError("input archive changed during replay")
    if any(event.capability_id and str(event.capability_id).startswith(("agent.", "model.")) for event in runtime.telemetry.events):
        raise AssertionError("agent/model telemetry appeared in canned replay")
    if any(path.suffix == ".pyc" for path in run_root.rglob("*")):
        raise AssertionError("canned replay created pyc under run root")
    if len(faults) != 5 or any(value["status"] != "PASS" for value in faults):
        raise AssertionError("not all fail-closed probes passed")
    call_counters = _derive_call_counters(ledger, runtime, guard)
    elapsed_ms = round((time.monotonic() - started) * 1000, 3)
    accepted_answer_hashes = {
        item.item_id: _sha256(item.accepted_root / "answer_content.json") for item in items
    }
    committed_records = [
        {
            "record_id": (
                f"prepared_asset:{record.payload.get('prepared_asset_id')}"
                if record.kind == "prepared_asset"
                else record.record_id
            ),
            "kind": record.kind,
            "payload_hash": _semantic_hash(record.payload, run_root=run_root),
            "payload": _semantic_value(record.payload, run_root=run_root),
        }
        for session in sessions
        for record in session.records
    ]
    committed_records.sort(key=lambda value: value["record_id"])
    dashboard_manifest_hash = _file_semantic_hash(
        run_root / "products/dashboard/manifest.json", run_root=run_root
    )
    dashboard_index_hash = _file_semantic_hash(
        run_root / "products/dashboard/index.html", run_root=run_root
    )
    product_manifest_hash = _file_semantic_hash(run_root / "products/product_manifest.json", run_root=run_root)
    optimizer_file_hashes = {
        name: _file_semantic_hash(run_root / "optimizer" / name, run_root=run_root)
        for name in ("optimizer_evidence_bundle.md", "optimizer_evidence_appendix.md")
    }
    report_projection = _normalized_report_projection(run_root / "reporting/final_report.json")
    report_semantic_hash = _semantic_hash(report_projection, run_root=run_root)
    optimizer_for_digest = dict(optimizer)
    optimizer_for_digest.pop("input_hashes", None)
    stable = {
        "status": lifecycle.state,
        "catalog_entries": len(catalog),
        "cache": [preview_miss.cache_status, preview_hit.cache_status],
        "items": stage_facts,
        "fault_probes": [value["probe"] + ":" + value["status"] for value in faults],
        "dashboard": "complete",
        "optimizer": optimizer.get("optimizer_status"),
        "offline_guard_forbidden_attempts": guard.forbidden_attempts,
        "fixture_hash": _semantic_hash(fixture),
        "fixture_role_conclusions": _semantic_value(fixture["role_conclusions"]),
        "accepted_answer_hashes": accepted_answer_hashes,
        "committed_records": committed_records,
        "dashboard_manifest_hash": dashboard_manifest_hash,
        "dashboard_index_hash": dashboard_index_hash,
        "product_manifest_hash": product_manifest_hash,
        "optimizer_result_hash": _semantic_hash(optimizer_for_digest, run_root=run_root),
        "optimizer_file_hashes": optimizer_file_hashes,
        "report_semantic_hash": report_semantic_hash,
        "call_counters": call_counters,
        "reporting": "finalized",
    }
    stable_digest = _sha256_bytes(_json_bytes(stable))
    return {
        "cycle": cycle_index,
        "status": lifecycle.state,
        "elapsed_ms": elapsed_ms,
        "call_counters": call_counters,
        "stage_facts": stage_facts,
        "fault_probes": faults,
        "catalog_entries": len(catalog),
        "cache": {"first": preview_miss.cache_status, "second": preview_hit.cache_status},
        "report_hash": report_hash,
        "terminal_receipt_hash": terminal_receipt_hash,
        "offline_guard_forbidden_attempts": guard.forbidden_attempts,
        "stable_payload": stable,
        "stable_digest": stable_digest,
    }


def _derive_call_counters(
    ledger: InvocationReceiptLedger,
    runtime: CoreRuntime,
    guard: _OfflineCallGuard,
) -> dict[str, int]:
    """Derive counters from persisted receipt metadata, telemetry, and guard facts."""

    agent_calls = 0
    model_calls = 0
    network_calls = guard.completed_network_calls
    for receipt in ledger.receipts:
        metadata = receipt.artifact_delta if isinstance(receipt.artifact_delta, Mapping) else {}
        agent_calls += int(metadata.get("agent_calls", 0) or 0)
        model_calls += int(metadata.get("model_calls", 0) or 0)
        network_calls += int(metadata.get("network_calls", 0) or 0)
    for event in runtime.telemetry.events:
        capability = str(event.capability_id or "")
        if capability.startswith(("agent.", "model.")):
            facts = event.facts if isinstance(event.facts, Mapping) else {}
            agent_calls += int(facts.get("agent_calls", 0) or 0)
            model_calls += int(facts.get("model_calls", 0) or 0)
            network_calls += int(facts.get("network_calls", 0) or 0)
    return {
        "agent_calls": agent_calls,
        "model_calls": model_calls,
        "network_calls": network_calls,
    }


def run_cycle(cycle_index: int, root: Path, fixture: Mapping[str, Any]) -> dict[str, Any]:
    with _OfflineCallGuard() as guard:
        return _run_cycle(cycle_index, root, fixture, guard)


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
    fixture = load_fixture(args.fixture)
    failures: list[dict[str, Any]] = []
    cycles: list[dict[str, Any]] = []
    if args.output_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="auto-foundry-canned-replay-")
        root = Path(temporary.name)
    else:
        temporary = None
        root = args.output_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
    try:
        for cycle_index in range(1, args.cycles + 1):
            cycle_root = root / f"cycle-{cycle_index:03d}"
            try:
                cycles.append(run_cycle(cycle_index, cycle_root, fixture))
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
        counter_keys = ("agent_calls", "model_calls", "network_calls")
        aggregate_counters = {
            key: sum(
                int((value.get("call_counters") or {}).get(key, 0) or 0)
                for value in cycles
            )
            for key in counter_keys
        }
        if any(aggregate_counters.values()):
            failures.append(
                {
                    "cycle": "aggregate",
                    "status": "failed",
                    "error_type": "ExternalCallError",
                    "error": f"nonzero external call counters: {aggregate_counters}",
                }
            )
        if len(set(digests)) > 1:
            failures.append(
                {
                    "cycle": "aggregate",
                    "status": "failed",
                    "error_type": "DeterminismError",
                    "error": "cycle stable digests disagree",
                }
            )
        summary = {
            "schema_version": "benchmark_a.canned_agent_replay.summary.v1",
            "fixture": REPLAY_LABEL,
            "cycle_count": args.cycles,
            "cycles": cycles,
            "failures": failures,
            "deterministic": bool(cycles) and not failures and len(set(digests)) == 1,
            "stable_digest": digests[0] if digests and len(set(digests)) == 1 else None,
            "call_counters": aggregate_counters,
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 1 if failures else 0
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
