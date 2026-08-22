"""Cognitive portfolio supervision contracts.

The host agent owns portfolio reasoning.  This module only provides the small
typed plan and run-local persistence boundary that the host can use to share a
portfolio decomposition with execution code.  It never invokes a model and it
does not make a plan authoritative through hashes, implementation identity, or
run lifecycle state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from .analysis import CatalogSnapshot
from .contracts import IncidentRecord, RequirementRecord
from .workbench import DataRoomCatalogEntry
from .workspace import RunContext


SUPERVISOR_PLAN_FILENAME = "requirement_supervisor_plan.json"
PLANNER_INCIDENT_FILENAME = "run_incidents.jsonl"


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    result = value.strip()
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


def _text_tuple(value: Iterable[Any] | None, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{field_name} must be an iterable of strings")
    result = tuple(_text(item, field_name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


def _jsonable(value: Any) -> Any:
    """Convert contract values to ordinary JSON-compatible containers."""

    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def inspect_integration_fidelity(
    context_or_item_root: RunContext | Path | str,
    item_id: str | None = None,
    *,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """Purely inspect one integration/fidelity boundary.

    The helper reads only the authoritative staging snapshot and typed
    ``integration_review`` records.  It never invokes ``IntegrationSession``
    (whose load path reconciles projections).  A forged/stale packet or
    result returns ``valid=False`` with diagnostics, allowing Planner to stop
    advancement and schedule repair/rethink without mutating the run.
    """

    try:
        from .durable import ItemWorkspace
        from .integration import AcceptedAnalysisBundle, IntegrationRecord
        from .integration_review import (
            FidelityRepairAuthorization,
            FidelityRepairProgress,
            FidelityResult,
            IntegrationFidelityPacket,
        )

        if isinstance(context_or_item_root, RunContext):
            if item_id is None:
                raise ValueError("item_id is required with a RunContext")
            expected_item_id = str(item_id)
            lexical_item_root = context_or_item_root.run_root / "requirements" / expected_item_id
            lexical_parent = context_or_item_root.run_root
            for component in ("requirements", expected_item_id):
                lexical_parent = lexical_parent / component
                if lexical_parent.is_symlink():
                    raise ValueError("item workspace path is symlinked")
            item_root = context_or_item_root.resolve_run_path(f"requirements/{item_id}")
            if item_root != lexical_item_root:
                raise ValueError("item workspace path is aliased")
        else:
            item_root = Path(context_or_item_root).expanduser().resolve(strict=False)
            expected_item_id = str(item_id or item_root.name)
        integration_root = item_root / "integration"
        staging = integration_root / "staging"
        review_root = integration_root / "review"
        session_path = staging / "session.json"
        snapshot_path = staging / "snapshot.json"
        if not session_path.exists() and not snapshot_path.exists():
            return {"valid": True, "stage": "not_started", "verdict": None, "diagnostics": []}
        diagnostics: list[str] = []
        if session_path.is_symlink() or snapshot_path.is_symlink() or not session_path.is_file() or not snapshot_path.is_file():
            raise ValueError("integration staging session/snapshot projection is incomplete")
        session = json.loads(session_path.read_text(encoding="utf-8"))
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(session, Mapping) or not isinstance(snapshot, Mapping):
            raise ValueError("integration staging artifacts must be objects")
        expected_snapshot_fields = {"schema_version", "state", "records", "snapshot_hash"}
        if set(snapshot) != expected_snapshot_fields or snapshot.get("schema_version") != "1":
            raise ValueError("integration staging snapshot fields are invalid")
        if snapshot.get("snapshot_hash") != _sha256_value({key: value for key, value in snapshot.items() if key != "snapshot_hash"}):
            raise ValueError("integration staging snapshot hash does not match content")
        expected_session_fields = {
            "schema_version", "session_id", "item_id", "owner_id", "invocation_id", "status",
            "accepted_content_hash", "accepted_manifest_hash", "records_count", "records_hash",
            "created_at", "updated_at", "state_hash",
        }
        if set(session) - (expected_session_fields | {"unreviewed_removed_record_hashes"}) or not expected_session_fields.issubset(session):
            raise ValueError("integration session fields are invalid")
        if session.get("schema_version") != "1" or session.get("item_id") != expected_item_id or session.get("status") not in {"open", "committed"}:
            raise ValueError("integration session identity is invalid")
        if session.get("state_hash") != _sha256_value({key: value for key, value in session.items() if key != "state_hash"}):
            raise ValueError("integration session state hash does not match content")
        if snapshot.get("state") != session:
            raise ValueError("integration staging snapshot and session projection diverge")
        # Validate the immutable accepted bundle through its public typed
        # boundary without loading ItemWorkspace (which reconciles/emits
        # telemetry).  The direct constructor is read-only; the bundle loader
        # performs the canonical terminal-intent/content/envelope checks.
        if isinstance(context_or_item_root, RunContext):
            raw_item_state = json.loads((item_root / "item_state.json").read_text(encoding="utf-8"))
            workspace = ItemWorkspace(
                context_or_item_root,
                expected_item_id,
                mode=str(raw_item_state.get("mode", "requirement")),
                original_text=str(raw_item_state.get("original_text", "")),
                state=raw_item_state,
            )
            # A blocked-by-evidence terminal item intentionally has no
            # accepted answer bundle.  Integration inspection remains pure
            # for that terminal-with-limits state; any actual staging or
            # committed artifact is rejected by the bindings below.
            bundle = (
                None
                if _status(raw_item_state.get("terminal_outcome")) == "blocked_by_evidence"
                else AcceptedAnalysisBundle.load(workspace)
            )
            if bundle is not None and (
                session.get("accepted_content_hash") != bundle.content_hash
                or session.get("accepted_manifest_hash") != bundle.manifest_hash
            ):
                raise ValueError("integration session accepted bundle binding is stale")
        raw_records = snapshot.get("records")
        if not isinstance(raw_records, list):
            raise ValueError("integration staging records are invalid")
        records = [IntegrationRecord.from_dict(value) for value in raw_records]
        if any(record.item_id != expected_item_id or record.accepted_content_hash != session.get("accepted_content_hash") for record in records):
            raise ValueError("integration staging records are bound to another accepted item")
        if len({record.record_id for record in records}) != len(records):
            raise ValueError("integration staging records contain duplicate IDs")
        records_bytes = b"".join(_canonical_json_bytes(record.to_dict()) for record in records)
        records_hash = hashlib.sha256(records_bytes).hexdigest()
        if session.get("records_count") != len(records) or session.get("records_hash") != records_hash:
            raise ValueError("integration staging record hash/count binding is stale")
        records_projection = staging / "records.jsonl"
        if records_projection.is_symlink() or not records_projection.is_file() or records_projection.read_bytes() != records_bytes:
            raise ValueError("integration staging records projection is stale")
        packet_path = review_root / "packet.json"
        result_path = review_root / "result.json"
        if not packet_path.exists() and not result_path.exists():
            return {
                "valid": True,
                "stage": "staged",
                "verdict": None,
                "diagnostics": [],
                "session_id": session.get("session_id"),
                "records_hash": records_hash,
            }
        if packet_path.is_symlink() or not packet_path.is_file():
            raise ValueError("integration fidelity packet is missing")
        packet = IntegrationFidelityPacket.from_dict(json.loads(packet_path.read_text(encoding="utf-8")))
        if (
            packet.item_id != expected_item_id
            or packet.session_id != session.get("session_id")
            or packet.invocation_id != session.get("invocation_id")
            or packet.accepted_content_hash != session.get("accepted_content_hash")
            or packet.accepted_manifest_hash != session.get("accepted_manifest_hash")
            or packet.records_hash != records_hash
        ):
            raise ValueError("integration fidelity packet is stale or unbound")
        if not result_path.exists():
            return {
                "valid": True,
                "stage": "awaiting_fidelity_review",
                "verdict": None,
                "diagnostics": [],
                "packet_hash": packet.packet_hash,
                "records_hash": records_hash,
            }
        if result_path.is_symlink() or not result_path.is_file():
            raise ValueError("integration fidelity result is invalid")
        result = FidelityResult.from_dict(json.loads(result_path.read_text(encoding="utf-8")))
        if (
            result.item_id != expected_item_id
            or result.session_id != session.get("session_id")
            or result.invocation_id != session.get("invocation_id")
        ):
            raise ValueError("integration fidelity result is stale or unbound")

        # An initial ``repair_once`` result remains the immutable authorization
        # record while the Integration Agent corrects its affected records.
        # The agent then rebuilds the packet/progress before a fresh targeted
        # reviewer is dispatched.  During that narrow transition the result
        # still points at the pre-repair packet and therefore cannot satisfy
        # the normal current-packet binding below.  Validate the complete
        # repair boundary here instead of classifying a valid handoff as a
        # forged/stale integration artifact.
        if (
            result.review_kind == "initial"
            and result.verdict == "repair_once"
            and (result.packet_hash != packet.packet_hash or result.records_hash != records_hash)
        ):
            auth_path = review_root / "repair_authorization.json"
            progress_path = review_root / "repair_progress.json"
            if (
                auth_path.is_symlink()
                or progress_path.is_symlink()
                or not auth_path.is_file()
                or not progress_path.is_file()
            ):
                raise ValueError("targeted fidelity repair bindings are missing")
            authorization = FidelityRepairAuthorization.from_dict(
                json.loads(auth_path.read_text(encoding="utf-8"))
            )
            progress = FidelityRepairProgress.from_dict(
                json.loads(progress_path.read_text(encoding="utf-8"))
            )
            if (
                authorization.item_id != expected_item_id
                or authorization.session_id != session.get("session_id")
                or authorization.invocation_id != session.get("invocation_id")
                or authorization.initial_packet_hash != result.packet_hash
                or authorization.initial_result_hash != result.result_hash
                or dict(authorization.baseline_record_hashes)
                != dict(result.baseline_record_hashes)
                or set(authorization.affected_record_ids)
                != set(result.affected_record_ids)
                or set(authorization.dependency_ids)
                != set(result.dependency_ids)
            ):
                raise ValueError("targeted fidelity authorization is stale or unbound")
            if (
                progress.item_id != expected_item_id
                or progress.session_id != session.get("session_id")
                or progress.invocation_id != session.get("invocation_id")
                or progress.authorization_hash != authorization.authorization_hash
                or progress.current_records_hash != records_hash
                or progress.current_packet_hash is None
                or progress.current_packet_hash != packet.packet_hash
                or packet.packet_hash == authorization.initial_packet_hash
            ):
                raise ValueError("targeted fidelity repair progress is stale")

            baseline_hashes = dict(authorization.baseline_record_hashes)
            current_hashes = {record.record_id: record.record_hash for record in records}
            affected = set(authorization.affected_record_ids)
            dependencies = set(authorization.dependency_ids)
            corrected = set(progress.corrected_record_hashes)
            removed = set(progress.removed_record_ids)
            if not affected:
                raise ValueError("initial fidelity repair authorization has no affected records")
            # Dependencies are recheck scope only.  They are never writable
            # under the one-repair authorization; only affected records may
            # appear in the correction map.
            if not corrected.issubset(affected):
                raise ValueError("targeted fidelity repair progress references unauthorized records")
            if not removed.issubset(affected):
                raise ValueError("targeted fidelity repair progress removals are outside the affected scope")
            if corrected & removed:
                raise ValueError("targeted fidelity repair progress cannot correct and remove one record")
            if not affected.issubset(corrected | removed):
                raise ValueError("targeted fidelity repair is incomplete")
            if set(current_hashes) != set(baseline_hashes) - removed:
                raise ValueError("targeted fidelity repair changed the staged record set")
            for record_id, baseline_hash in baseline_hashes.items():
                if record_id in removed:
                    continue
                expected_hash = progress.corrected_record_hashes.get(record_id, baseline_hash)
                if current_hashes.get(record_id) != expected_hash:
                    raise ValueError("targeted fidelity repair record hash is stale or tampered")
                if record_id in progress.corrected_record_hashes and expected_hash == baseline_hash:
                    raise ValueError("targeted fidelity repair correction does not differ from baseline")

            return {
                "valid": True,
                "stage": "awaiting_targeted_fidelity_review",
                "verdict": None,
                "review_kind": "targeted",
                "diagnostics": diagnostics,
                "packet_hash": packet.packet_hash,
                "result_hash": result.result_hash,
                "records_hash": records_hash,
                "initial_packet_hash": authorization.initial_packet_hash,
                "initial_result_hash": authorization.initial_result_hash,
                "authorization_hash": authorization.authorization_hash,
                "affected_record_ids": sorted(affected),
                "dependency_ids": sorted(dependencies),
            }

        if result.packet_hash != packet.packet_hash:
            raise ValueError("integration fidelity result is stale or unbound")
        if result.review_kind == "initial" and result.records_hash != records_hash:
            raise ValueError("initial fidelity result records hash is stale")
        if result.review_kind == "initial" and dict(result.baseline_record_hashes) != {record.record_id: record.record_hash for record in records}:
            raise ValueError("initial fidelity baseline record hashes are stale")
        if result.review_kind == "targeted":
            auth_path = review_root / "repair_authorization.json"
            progress_path = review_root / "repair_progress.json"
            if auth_path.is_symlink() or progress_path.is_symlink() or not auth_path.is_file() or not progress_path.is_file():
                raise ValueError("targeted fidelity result repair bindings are missing")
            authorization = FidelityRepairAuthorization.from_dict(json.loads(auth_path.read_text(encoding="utf-8")))
            progress = FidelityRepairProgress.from_dict(json.loads(progress_path.read_text(encoding="utf-8")))
            if (
                authorization.item_id != expected_item_id
                or authorization.session_id != session.get("session_id")
                or authorization.invocation_id != session.get("invocation_id")
                or dict(result.baseline_record_hashes) != dict(authorization.baseline_record_hashes)
                or set(result.affected_record_ids) != set(authorization.affected_record_ids)
                or set(result.dependency_ids) != set(authorization.dependency_ids)
            ):
                raise ValueError("targeted fidelity authorization is stale or unbound")
            if (
                progress.item_id != expected_item_id
                or progress.session_id != session.get("session_id")
                or progress.invocation_id != session.get("invocation_id")
                or progress.authorization_hash != authorization.authorization_hash
                or progress.current_records_hash != records_hash
                or progress.current_packet_hash != result.packet_hash
            ):
                raise ValueError("targeted fidelity repair progress is stale")
        return {
            "valid": True,
            "stage": "fidelity_reviewed",
            "verdict": result.verdict,
            "review_kind": result.review_kind,
            "diagnostics": diagnostics,
            "packet_hash": packet.packet_hash,
            "result_hash": result.result_hash,
            "records_hash": records_hash,
        }
    except Exception as exc:
        if raise_on_error:
            raise
        return {"valid": False, "stage": "invalid", "verdict": None, "diagnostics": [str(exc)]}


def inspect_committed_integration(
    context_or_item_root: RunContext | Path | str,
    item_id: str | None = None,
    *,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """Purely validate an item's immutable committed integration boundary.

    ``IntegrationSession.load`` is intentionally not used here because its
    recovery path may reconcile projections or replay the LEM.  This helper
    reads the committed manifest/records and the accepted bundle directly,
    validates every self-hash and item binding, and returns a diagnostic view
    suitable for Planner snapshots.
    """

    try:
        from .durable import ItemWorkspace
        from .integration import AcceptedAnalysisBundle, IntegrationRecord

        if isinstance(context_or_item_root, RunContext):
            if item_id is None:
                raise ValueError("item_id is required with a RunContext")
            item_root = context_or_item_root.resolve_run_path(f"requirements/{item_id}")
            expected_item_id = str(item_id)
            raw_state_path = item_root / "item_state.json"
            if raw_state_path.is_symlink() or not raw_state_path.is_file():
                raise ValueError("item state is missing for committed integration")
            raw_state = json.loads(raw_state_path.read_text(encoding="utf-8"))
            if not isinstance(raw_state, Mapping):
                raise ValueError("item state is invalid for committed integration")
            workspace = ItemWorkspace(
                context_or_item_root,
                expected_item_id,
                mode=str(raw_state.get("mode", "requirement")),
                original_text=str(raw_state.get("original_text", "")),
                state=raw_state,
            )
            # ``blocked_by_evidence`` is a valid terminal outcome but does
            # not publish an answer bundle.  Do not classify its empty
            # integration boundary as malformed merely because the normal
            # accepted-bundle loader quite correctly rejects that outcome.
            bundle = (
                None
                if _status(raw_state.get("terminal_outcome")) == "blocked_by_evidence"
                else AcceptedAnalysisBundle.load(workspace)
            )
            if bundle is None:
                snapshot, manifest = workspace._read_valid_terminal_snapshot()
                if snapshot.outcome != "blocked_by_evidence":
                    raise ValueError("blocked terminal snapshot outcome is invalid")
                workspace._validate_preterminal_binding(snapshot.outcome, manifest)
        else:
            item_root = Path(context_or_item_root).expanduser().resolve(strict=False)
            expected_item_id = str(item_id or item_root.name)
            bundle = None

        def _item_relative_manifest_ref(ref: Any, expected_path: Path, label: str) -> None:
            """Bind an item-state manifest ref to its exact item-local path."""

            if not isinstance(ref, str) or not ref or Path(ref).is_absolute() or ".." in Path(ref).parts:
                raise ValueError(f"{label} reference is not item-relative")
            try:
                expected_ref = expected_path.relative_to(item_root).as_posix()
            except ValueError as exc:
                raise ValueError(f"{label} target escaped the item workspace") from exc
            if ref != expected_ref:
                raise ValueError(f"{label} reference is stale or foreign")
            current = item_root
            for component in Path(ref).parts:
                current = current / component
                if current.is_symlink():
                    raise ValueError(f"{label} reference uses a symlink")
            if expected_path.is_symlink() or not expected_path.is_file():
                raise ValueError(f"{label} target is missing or symlinked")

        failure_root = item_root / "integration" / "technical_failure"
        failure_path = failure_root / "manifest.json"
        failure_present = failure_root.exists() or failure_root.is_symlink()
        state_value = raw_state if isinstance(context_or_item_root, RunContext) else None
        if failure_present:
            if failure_root.is_symlink() or not failure_root.is_dir() or failure_path.is_symlink() or not failure_path.is_file():
                raise ValueError("technical failure manifest is missing or symlinked")
            try:
                failure_manifest = json.loads(failure_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("technical failure manifest is invalid") from exc
            if not isinstance(failure_manifest, Mapping):
                raise ValueError("technical failure manifest is invalid")
            expected_failure_fields = {
                "schema_version", "session_id", "item_id", "owner_id", "status",
                "accepted_content_hash", "reason", "created_at", "manifest_hash",
            }
            if set(failure_manifest) != expected_failure_fields:
                raise ValueError("technical failure manifest fields are invalid")
            if failure_manifest.get("schema_version") != "1" or failure_manifest.get("status") != "technical_failure":
                raise ValueError("technical failure manifest fields are invalid")
            if failure_manifest.get("item_id") != expected_item_id:
                raise ValueError("technical failure manifest item binding is stale")
            if any(
                not isinstance(failure_manifest.get(key), str) or not str(failure_manifest.get(key)).strip()
                for key in ("session_id", "owner_id", "reason", "created_at")
            ):
                raise ValueError("technical failure manifest identity is invalid")
            accepted_content_hash = failure_manifest.get("accepted_content_hash")
            if not isinstance(accepted_content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", accepted_content_hash):
                raise ValueError("technical failure manifest accepted hash is invalid")
            if bundle is not None and accepted_content_hash != bundle.content_hash:
                raise ValueError("technical failure manifest accepted bundle binding is stale")
            unsigned_failure = {key: value for key, value in failure_manifest.items() if key != "manifest_hash"}
            if failure_manifest.get("manifest_hash") != _sha256_value(unsigned_failure):
                raise ValueError("technical failure manifest hash does not match content")
            if state_value is not None:
                if state_value.get("integration_state") != "technical_failure":
                    raise ValueError("technical failure manifest is foreign to item state")
                if state_value.get("integration_manifest_hash") != failure_manifest.get("manifest_hash"):
                    raise ValueError("technical failure item manifest hash binding is stale")
                ref = state_value.get("integration_manifest_ref")
                _item_relative_manifest_ref(ref, failure_path, "technical failure item manifest")
            committed_root = item_root / "integration" / "committed"
            if committed_root.exists() or committed_root.is_symlink():
                raise ValueError("technical failure and committed integration cannot coexist")
            return {
                "valid": True,
                "stage": "technical_failure",
                "verdict": "technical_failure",
                "diagnostics": [],
                "session_id": failure_manifest.get("session_id"),
                "records_count": 0,
                "accepted_content_hash": accepted_content_hash,
                "manifest_hash": failure_manifest.get("manifest_hash"),
            }
        if state_value is not None and state_value.get("integration_state") == "technical_failure":
            raise ValueError("technical failure item state has no validated manifest")

        committed = item_root / "integration" / "committed"
        manifest_path = committed / "manifest.json"
        if not committed.exists() and not committed.is_symlink():
            return {"valid": True, "stage": "not_committed", "verdict": None, "diagnostics": []}
        if committed.is_symlink() or not committed.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("committed integration manifest is missing or symlinked")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            raise ValueError("committed integration manifest is invalid")
        expected_fields = {
            "schema_version", "session_id", "item_id", "owner_id", "invocation_id", "status",
            "accepted_content_hash", "accepted_manifest_hash", "records_path", "records_hash",
            "records_count", "counts", "created_at", "committed_at", "manifest_hash",
        }
        if set(manifest) != expected_fields or manifest.get("schema_version") != "1" or manifest.get("status") != "committed":
            raise ValueError("committed integration manifest fields are invalid")
        if manifest.get("item_id") != expected_item_id or manifest.get("records_path") != "records.jsonl":
            raise ValueError("committed integration manifest item binding is stale")
        if bundle is not None and (
            manifest.get("accepted_content_hash") != bundle.content_hash
            or manifest.get("accepted_manifest_hash") != bundle.manifest_hash
        ):
            raise ValueError("committed integration accepted bundle binding is stale")
        unsigned_manifest = {key: value for key, value in manifest.items() if key != "manifest_hash"}
        if manifest.get("manifest_hash") != _sha256_value(unsigned_manifest):
            raise ValueError("committed integration manifest hash does not match content")
        records_path = committed / "records.jsonl"
        if records_path.is_symlink() or not records_path.is_file():
            raise ValueError("committed integration records are missing")
        records_bytes = records_path.read_bytes()
        if hashlib.sha256(records_bytes).hexdigest() != manifest.get("records_hash"):
            raise ValueError("committed integration records hash does not match manifest")
        records: list[Any] = []
        for line_number, line in enumerate(records_bytes.splitlines(), 1):
            try:
                record = IntegrationRecord.from_dict(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(f"committed integration record line {line_number} is invalid") from exc
            if record.item_id != expected_item_id or (
                bundle is not None and record.accepted_content_hash != bundle.content_hash
            ):
                raise ValueError("committed integration record accepted binding is stale")
            records.append(record)
        if manifest.get("records_count") != len(records) or len({record.record_id for record in records}) != len(records):
            raise ValueError("committed integration record count or IDs are invalid")
        if isinstance(context_or_item_root, RunContext):
            state = raw_state
            if state.get("integration_state") != "integrated" or state.get("integration_manifest_hash") != manifest.get("manifest_hash"):
                raise ValueError("committed integration item state binding is stale")
            ref = state.get("integration_manifest_ref")
            _item_relative_manifest_ref(ref, manifest_path, "committed integration item manifest")
        return {
            "valid": True,
            "stage": "committed",
            "verdict": "committed",
            "diagnostics": [],
            "session_id": manifest.get("session_id"),
            "records_count": len(records),
            "records_hash": manifest.get("records_hash"),
            "manifest_hash": manifest.get("manifest_hash"),
        }
    except Exception as exc:
        if raise_on_error:
            raise
        return {"valid": False, "stage": "invalid", "verdict": None, "diagnostics": [str(exc)]}


def _inspect_blocked_terminal_integration(
    item_root: Path,
    state: Mapping[str, Any],
    terminal: Any,
    fidelity_view: Mapping[str, Any],
    committed_view: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the no-integration terminal boundary for blocked items.

    ``blocked_by_evidence`` is a real terminal outcome, not a failed
    integration attempt.  It publishes the reviewed no-answer snapshot and
    intentionally leaves ``integration_state=pending`` without a staging,
    committed, or technical-failure projection.  This pure helper makes that
    state explicit so Planner can route the item to product/report work while
    still failing closed for forged pointers or leftover integration residue.
    """

    diagnostics: list[str] = []
    if _status(terminal) != "blocked_by_evidence":
        return {"valid": False, "stage": "not_applicable", "diagnostics": diagnostics}
    if state.get("integration_state", "pending") != "pending":
        diagnostics.append("blocked_by_evidence item must retain pending integration state")
    if state.get("integration_manifest_ref") not in (None, ""):
        diagnostics.append("blocked_by_evidence item must not bind an integration manifest")
    if state.get("integration_manifest_hash") not in (None, ""):
        diagnostics.append("blocked_by_evidence item must not bind an integration manifest hash")
    if fidelity_view.get("valid") is not True or fidelity_view.get("stage") != "not_started":
        diagnostics.append("blocked_by_evidence item has a forged or stale integration staging boundary")
    if committed_view.get("valid") is not True or committed_view.get("stage") != "not_committed":
        diagnostics.append("blocked_by_evidence item has a committed or technical-failure integration artifact")

    integration_root = item_root / "integration"
    if integration_root.is_symlink():
        diagnostics.append("blocked_by_evidence integration root is symlinked")
    elif integration_root.exists():
        if not integration_root.is_dir():
            diagnostics.append("blocked_by_evidence integration root is not a directory")
        else:
            try:
                residue = tuple(integration_root.iterdir())
            except OSError as exc:
                diagnostics.append(f"blocked_by_evidence integration residue cannot be inspected: {exc}")
            else:
                if residue:
                    diagnostics.append("blocked_by_evidence integration root contains residue")
    if diagnostics:
        return {"valid": False, "stage": "invalid", "diagnostics": diagnostics}
    return {"valid": True, "stage": "blocked_by_evidence", "diagnostics": []}


def inspect_product_manifest(
    context: RunContext,
    generation_id: str,
    manifest_ref: str,
    *,
    metadata: Any = None,
) -> dict[str, Any]:
    """Purely validate the active generation's published product manifest."""

    try:
        from .product_contracts import validate_product_manifest

        path = context.resolve_run_path(manifest_ref)
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                raise ValueError("product manifest is missing or symlinked")
            return {"valid": False, "stage": "missing", "manifest": None, "diagnostics": ["product manifest is missing"]}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("product manifest is invalid")
        validate_product_manifest(value, require_all=True)
        if value.get("run_id") not in {None, context.run_id}:
            raise ValueError("product manifest run identity is stale")
        extension_product = generation_id != "G-0001"
        if extension_product and value.get("run_id") != context.run_id:
            raise ValueError("product manifest run identity is missing")
        lifecycle = value.get("lifecycle")
        if not isinstance(lifecycle, Mapping) or lifecycle.get("generation_id") != generation_id:
            raise ValueError("product manifest generation lineage is stale")
        if value.get("terminal") is not True or value.get("new_analytics") is not False:
            raise ValueError("product manifest terminal/new_analytics binding is invalid")
        dashboard = value.get("dashboard")
        if not isinstance(dashboard, Mapping):
            raise ValueError("product manifest dashboard binding is missing")
        receipt_ref = dashboard.get("receipt_ref")
        receipt_hash = dashboard.get("receipt_sha256")
        if not isinstance(receipt_ref, str) or not receipt_ref.strip() or not isinstance(receipt_hash, str):
            raise ValueError("product manifest dashboard receipt binding is missing")
        receipt_path = context.resolve_run_path(receipt_ref)
        if receipt_path.is_symlink() or not receipt_path.is_file() or hashlib.sha256(receipt_path.read_bytes()).hexdigest() != receipt_hash:
            raise ValueError("product manifest dashboard receipt binding is stale")
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes.decode("utf-8"))
        if not isinstance(receipt, Mapping) or receipt.get("status") != "complete" or receipt.get("new_analytics") is not False:
            raise ValueError("product manifest dashboard receipt is invalid")
        if receipt_bytes != _canonical_json_bytes(receipt):
            raise ValueError("product manifest dashboard receipt is not canonical")
        if receipt.get("run_id") != context.run_id or receipt.get("generation_id") != generation_id:
            raise ValueError("product manifest dashboard receipt run/generation lineage is stale")
        plan_binding = receipt.get("plan_binding")
        required_plan_fields = {"ref", "sha256", "admission_sha256", "generation_id"}
        if not isinstance(plan_binding, Mapping) or not required_plan_fields.issubset(plan_binding):
            raise ValueError("product manifest dashboard receipt plan binding is missing")
        plan_ref = plan_binding.get("ref")
        plan_hash = plan_binding.get("sha256")
        admission_plan_hash = plan_binding.get("admission_sha256")
        if (
            not isinstance(plan_ref, str)
            or not plan_ref.strip()
            or not isinstance(plan_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", plan_hash)
            or not isinstance(admission_plan_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", admission_plan_hash)
            or plan_binding.get("generation_id") != generation_id
        ):
            raise ValueError("product manifest dashboard receipt plan binding is invalid")
        plan_path = context.resolve_run_path(plan_ref)
        if plan_path.is_symlink() or not plan_path.is_file():
            raise ValueError("product manifest dashboard receipt plan binding is missing")
        actual_plan_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        if plan_hash != actual_plan_hash or admission_plan_hash != actual_plan_hash:
            raise ValueError("product manifest dashboard receipt plan binding is stale")
        if metadata is not None:
            expected_plan_path = Path(metadata.plan_path)
            try:
                expected_plan_ref = expected_plan_path.resolve(strict=False).relative_to(context.run_root.resolve()).as_posix()
            except ValueError as exc:
                raise ValueError("validated lifecycle plan path escapes the run root") from exc
            if plan_ref != expected_plan_ref or admission_plan_hash != metadata.plan_hash:
                raise ValueError("product manifest dashboard receipt plan binding does not match lifecycle metadata")
        elif generation_id == "G-0001":
            from .lifecycle import RunLifecycle

            lifecycle = RunLifecycle.load(context)
            if lifecycle.generation_id != generation_id:
                raise ValueError("root lifecycle generation binding is stale")
            expected_plan_path = lifecycle.plan_path
            try:
                expected_plan_ref = expected_plan_path.resolve(strict=False).relative_to(context.run_root.resolve()).as_posix()
            except ValueError as exc:
                raise ValueError("root lifecycle plan path escapes the run root") from exc
            if plan_ref != expected_plan_ref:
                raise ValueError("product manifest dashboard receipt root plan binding is stale")
        else:
            expected_plan_ref = f"extensions/{generation_id}/requirement_supervisor_plan.json"
            if plan_ref != expected_plan_ref:
                raise ValueError("product manifest dashboard receipt generation plan binding is stale")
        if extension_product:
            parent_generation_id = receipt.get("parent_generation_id")
            parent = receipt.get("parent")
            if not isinstance(parent_generation_id, str) or not parent_generation_id.strip() or not isinstance(parent, Mapping):
                raise ValueError("product manifest dashboard receipt parent lineage is missing")
            parent_ref = parent.get("product_manifest_ref")
            parent_hash = parent.get("product_manifest_sha256")
            if not isinstance(parent_ref, str) or not parent_ref.strip() or not isinstance(parent_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", parent_hash):
                raise ValueError("product manifest dashboard receipt parent manifest binding is invalid")
            parent_path = context.resolve_run_path(parent_ref)
            if parent_path.is_symlink() or not parent_path.is_file() or hashlib.sha256(parent_path.read_bytes()).hexdigest() != parent_hash:
                raise ValueError("product manifest dashboard receipt parent manifest binding is stale")
            presentation_ref = receipt.get("presentation_plan_ref")
            presentation_hash = receipt.get("presentation_plan_sha256")
            if not isinstance(presentation_ref, str) or not presentation_ref.strip() or not isinstance(presentation_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", presentation_hash):
                raise ValueError("product manifest dashboard receipt presentation plan binding is missing")
            presentation_path = context.resolve_run_path(presentation_ref)
            if presentation_path.is_symlink() or not presentation_path.is_file() or hashlib.sha256(presentation_path.read_bytes()).hexdigest() != presentation_hash:
                raise ValueError("product manifest dashboard receipt presentation plan binding is stale")
            if value.get("presentation_plan_ref") != presentation_ref or value.get("presentation_plan_sha256") != presentation_hash:
                raise ValueError("product manifest presentation plan binding is stale")
            lineage = value.get("lineage")
            if not isinstance(lineage, Mapping):
                raise ValueError("product manifest lineage is missing")
            if lineage.get("parent_generation_id") != parent_generation_id or lineage.get("parent_product_manifest_ref") != parent_ref or lineage.get("parent_product_manifest_sha256") != parent_hash:
                raise ValueError("product manifest parent lineage does not match dashboard receipt")
            if lineage.get("delta_receipt_ref") != receipt.get("outputs", {}).get("receipt_ref"):
                raise ValueError("product manifest receipt lineage is stale")
        else:
            root_parent = receipt.get("parent")
            required_root_parent = {"root_generation", "parent_generation_id", "parent_manifest_ref", "parent_manifest_hash"}
            if (
                not isinstance(root_parent, Mapping)
                or not required_root_parent.issubset(root_parent)
                or root_parent.get("root_generation") is not True
                or root_parent.get("parent_generation_id") is not None
                or root_parent.get("parent_manifest_ref") is not None
                or root_parent.get("parent_manifest_hash") is not None
            ):
                raise ValueError("product manifest dashboard receipt root parent binding is invalid")
        outputs = receipt.get("outputs")
        output_hashes = receipt.get("output_hashes")
        if not isinstance(outputs, Mapping) or not isinstance(output_hashes, Mapping):
            raise ValueError("product manifest dashboard output bindings are missing")
        expected_output_hashes = {"fixture_sha256", "chart_map_sha256", "chart_registry_sha256", "site_manifest_sha256"}
        if set(output_hashes) != expected_output_hashes:
            raise ValueError("product manifest dashboard output hashes are not exact")
        if outputs.get("receipt_ref") != receipt_ref:
            raise ValueError("product manifest dashboard receipt reference is stale")
        hash_key_by_ref = {
            "fixture_ref": "fixture_sha256",
            "chart_map_ref": "chart_map_sha256",
            "chart_registry_ref": "chart_registry_sha256",
            "site_ref": "site_manifest_sha256",
            "receipt_ref": None,
        }
        for ref_key, hash_key in hash_key_by_ref.items():
            ref = outputs.get(ref_key)
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError(f"product manifest output {ref_key} is missing")
            target = context.resolve_run_path(ref)
            if target.is_symlink() or not target.exists():
                raise ValueError(f"product manifest output {ref_key} is missing")
            if hash_key is not None:
                expected = output_hashes.get(hash_key)
                if not isinstance(expected, str):
                    raise ValueError(f"product manifest output hash {hash_key} is missing")
                if ref_key == "site_ref" and target.is_dir():
                    site_manifest_path = target / "site_manifest.json"
                    if site_manifest_path.is_symlink() or not site_manifest_path.is_file():
                        raise ValueError("product manifest site manifest is missing")
                    actual = hashlib.sha256(site_manifest_path.read_bytes()).hexdigest()
                elif target.is_file():
                    actual = hashlib.sha256(target.read_bytes()).hexdigest()
                else:
                    files = {
                        str(child.relative_to(target)): hashlib.sha256(child.read_bytes()).hexdigest()
                        for child in sorted(target.rglob("*"))
                        if child.is_file() and not child.is_symlink()
                    }
                    actual = _sha256_value({"files": files})
                if actual != expected:
                    raise ValueError(f"product manifest output {ref_key} hash is stale")
        lineage = value.get("lineage")
        if not isinstance(lineage, Mapping):
            raise ValueError("product manifest lineage is missing")
        if metadata is not None:
            if lineage.get("parent_generation_id") != metadata.parent_generation_id:
                raise ValueError("product manifest parent generation lineage is stale")
            if lineage.get("generation_manifest_hash") != metadata.manifest_hash:
                raise ValueError("product manifest generation manifest lineage is stale")
            if lineage.get("active_plan_hash") != metadata.plan_hash:
                raise ValueError("product manifest active plan lineage is stale")
        return {
            "valid": True,
            "stage": "published",
            "manifest": dict(value),
            "manifest_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
            "receipt_ref": receipt_ref,
            "receipt_hash": receipt_hash,
            "diagnostics": [],
        }
    except Exception as exc:
        return {"valid": False, "stage": "invalid", "manifest": None, "diagnostics": [str(exc)]}


def _record(value: RequirementRecord | Mapping[str, Any]) -> RequirementRecord:
    if isinstance(value, RequirementRecord):
        return value
    if isinstance(value, Mapping):
        return RequirementRecord.from_dict(value)
    raise TypeError("input_records must contain RequirementRecord values")


def _records(values: Iterable[RequirementRecord | Mapping[str, Any]]) -> tuple[RequirementRecord, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("input_records must be an iterable of RequirementRecord values")
    result = tuple(_record(value) for value in values)
    ids = tuple(record.requirement_id for record in result)
    if len(ids) != len(set(ids)):
        raise ValueError("input RequirementRecord IDs must be unique")
    return result


@dataclass(frozen=True)
class RequirementExecutionGroup:
    """A cognitive execution unit containing one or more requirements."""

    requirement_ids: tuple[str, ...]
    rationale: str
    shared_analysis_intent: str | None = None
    suggested_specialists: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        requirement_ids = _text_tuple(self.requirement_ids, "requirement_ids")
        if not requirement_ids:
            raise ValueError("an execution group needs at least one requirement_id")
        specialists = _text_tuple(self.suggested_specialists, "suggested_specialists")
        if len(specialists) > 3:
            raise ValueError("suggested_specialists may contain at most three names")
        object.__setattr__(self, "requirement_ids", requirement_ids)
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale"))
        if self.shared_analysis_intent is not None:
            object.__setattr__(
                self,
                "shared_analysis_intent",
                _text(self.shared_analysis_intent, "shared_analysis_intent"),
            )
        object.__setattr__(self, "suggested_specialists", specialists)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_ids": list(self.requirement_ids),
            "rationale": self.rationale,
            "shared_analysis_intent": self.shared_analysis_intent,
            "suggested_specialists": list(self.suggested_specialists),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RequirementExecutionGroup":
        if not isinstance(data, Mapping):
            raise TypeError("execution group must be a mapping")
        allowed = {
            "requirement_ids",
            "rationale",
            "shared_analysis_intent",
            "suggested_specialists",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"execution group has unknown fields: {', '.join(sorted(map(str, unknown)))}")
        return cls(
            requirement_ids=data.get("requirement_ids", ()),
            rationale=data.get("rationale", ""),
            shared_analysis_intent=data.get("shared_analysis_intent"),
            suggested_specialists=data.get("suggested_specialists", ()),
        )


@dataclass(frozen=True)
class RequirementExecutionPlan:
    """A planner-selected grouping and order for an exact requirement set."""

    input_records: tuple[RequirementRecord, ...]
    groups: tuple[RequirementExecutionGroup, ...]
    planner_ref: str
    portfolio_strategy: str
    revision: int

    def __post_init__(self) -> None:
        records = _records(self.input_records)
        if isinstance(self.groups, (str, bytes)):
            raise TypeError("groups must be an iterable of RequirementExecutionGroup values")
        groups = tuple(
            RequirementExecutionGroup.from_dict(group) if isinstance(group, Mapping) else group
            for group in self.groups
        )
        if records and not groups:
            raise ValueError("a non-empty requirement execution plan needs at least one group")
        if not records and groups:
            raise ValueError("an empty requirement execution plan cannot contain groups")
        if any(not isinstance(group, RequirementExecutionGroup) for group in groups):
            raise TypeError("groups must contain RequirementExecutionGroup values")

        known_ids = {record.requirement_id for record in records}
        flattened: list[str] = []
        for group in groups:
            flattened.extend(group.requirement_ids)
        if len(flattened) != len(set(flattened)):
            raise ValueError("each requirement must occur in exactly one execution group")
        if set(flattened) != known_ids or len(flattened) != len(known_ids):
            raise ValueError("execution groups must cover every input requirement exactly once")

        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision <= 0:
            raise ValueError("revision must be a positive integer")
        object.__setattr__(self, "input_records", records)
        object.__setattr__(self, "groups", groups)
        object.__setattr__(self, "planner_ref", _text(self.planner_ref, "planner_ref"))
        object.__setattr__(self, "portfolio_strategy", _text(self.portfolio_strategy, "portfolio_strategy"))

    @classmethod
    def from_requirements(
        cls,
        requirements: Iterable[RequirementRecord | Mapping[str, Any]],
        *,
        planner_ref: str = "requirement-planner",
        portfolio_strategy: str = (
            "automatic explicit-priority, dependency-aware, stable-order planning"
        ),
        revision: int = 1,
    ) -> "RequirementExecutionPlan":
        """Build a complete plan directly from the supplied requirement list.

        Dependencies between supplied requirement IDs are honoured before a
        dependent item is made runnable.  Among currently eligible items,
        explicit numeric priority wins and original input position provides a
        deterministic tie-break.  Dependencies that name external sources
        rather than another requirement remain metadata and do not block the
        initial portfolio.  The resulting groups contain the exact input set;
        no requirement is dropped or synthesized.
        """

        records = _records(requirements)
        by_id = {record.requirement_id: record for record in records}
        positions = {record.requirement_id: index for index, record in enumerate(records)}

        def priority_key(record: RequirementRecord) -> tuple[int, float, int]:
            value = record.explicit_priority
            if value is None:
                return (1, float("inf"), positions[record.requirement_id])
            if isinstance(value, bool):
                raise ValueError(f"requirement {record.requirement_id} priority must not be boolean")
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                # Non-numeric explicit priorities are still explicit and are
                # ordered deterministically without guessing their semantics.
                return (0, float("inf"), positions[record.requirement_id])
            if numeric != numeric or numeric in {float("inf"), float("-inf")}:
                raise ValueError(f"requirement {record.requirement_id} priority must be finite")
            return (0, numeric, positions[record.requirement_id])

        dependencies: dict[str, set[str]] = {}
        dependents: dict[str, set[str]] = {record.requirement_id: set() for record in records}
        for record in records:
            declared = list(record.dependencies)
            metadata = record.metadata if isinstance(record.metadata, Mapping) else {}
            observed = metadata.get(
                "observed_dependencies",
                metadata.get(
                    "observed_dependency_ids",
                    metadata.get(
                        "depends_on_requirement_ids",
                        metadata.get("depends_on", metadata.get("dependencies", ())),
                    ),
                ),
            )
            if isinstance(observed, str):
                observed = (observed,)
            if observed:
                declared.extend(str(value) for value in observed)
            known = {value for value in declared if value in by_id and value != record.requirement_id}
            dependencies[record.requirement_id] = known
            for dependency in known:
                dependents[dependency].add(record.requirement_id)

        ready = [record.requirement_id for record in records if not dependencies[record.requirement_id]]
        order: list[str] = []
        while ready:
            ready.sort(key=lambda item_id: priority_key(by_id[item_id]))
            item_id = ready.pop(0)
            order.append(item_id)
            for dependent in sorted(dependents[item_id], key=positions.__getitem__):
                dependencies[dependent].discard(item_id)
                if not dependencies[dependent] and dependent not in order and dependent not in ready:
                    ready.append(dependent)
        if len(order) != len(records):
            cycle = sorted(set(by_id) - set(order), key=positions.__getitem__)
            raise ValueError("requirement dependencies contain a cycle: " + ", ".join(cycle))

        # Keep parent requirements as the execution units.  Shared data needs
        # are exposed as rationale/intent so a host can reuse prepared work
        # without introducing child workspaces or synthetic requirement IDs.
        groups: list[RequirementExecutionGroup] = []
        previous_reusable: tuple[str, ...] | None = None
        for item_id in order:
            record = by_id[item_id]
            reusable = tuple(
                dict.fromkeys(
                    str(value)
                    for value in (
                        *record.data_needs,
                        *record.ontology_needs,
                        *record.prepared_data_needs,
                    )
                )
            )
            if reusable and reusable == previous_reusable and groups:
                prior = groups[-1]
                groups[-1] = RequirementExecutionGroup(
                    prior.requirement_ids + (item_id,),
                    prior.rationale,
                    shared_analysis_intent=prior.shared_analysis_intent,
                    suggested_specialists=prior.suggested_specialists,
                )
            else:
                if reusable:
                    rationale = "Run requirements with reusable needs: " + ", ".join(reusable)
                    intent = "Reuse compatible prepared/evidence inputs where available."
                else:
                    rationale = "Run requirement in deterministic Planner order."
                    intent = None
                groups.append(
                    RequirementExecutionGroup(
                        (item_id,),
                        rationale,
                        shared_analysis_intent=intent,
                    )
                )
            previous_reusable = reusable
        return cls(
            input_records=records,
            groups=tuple(groups),
            planner_ref=planner_ref,
            portfolio_strategy=portfolio_strategy,
            revision=revision,
        )

    @property
    def execution_order(self) -> tuple[str, ...]:
        """Flatten group order into requirement IDs for the executor."""

        return tuple(requirement_id for group in self.groups for requirement_id in group.requirement_ids)

    def group_for(self, requirement_id: str) -> RequirementExecutionGroup:
        item_id = _text(requirement_id, "requirement_id")
        for group in self.groups:
            if item_id in group.requirement_ids:
                return group
        raise KeyError(f"requirement is not in execution plan: {item_id}")

    def record_for(self, requirement_id: str) -> RequirementRecord:
        item_id = _text(requirement_id, "requirement_id")
        for record in self.input_records:
            if record.requirement_id == item_id:
                return record
        raise KeyError(f"requirement is not in execution plan: {item_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_records": [_jsonable(record) for record in self.input_records],
            "groups": [_jsonable(group) for group in self.groups],
            "planner_ref": self.planner_ref,
            "portfolio_strategy": self.portfolio_strategy,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RequirementExecutionPlan":
        if not isinstance(data, Mapping):
            raise TypeError("execution plan must be a mapping")
        allowed = {"input_records", "groups", "planner_ref", "portfolio_strategy", "revision"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"execution plan has unknown fields: {', '.join(sorted(map(str, unknown)))}")
        raw_records = data.get("input_records", ())
        raw_groups = data.get("groups", ())
        return cls(
            input_records=tuple(
                RequirementRecord.from_dict(record) if isinstance(record, Mapping) else record
                for record in raw_records
            ),
            groups=tuple(
                RequirementExecutionGroup.from_dict(group) if isinstance(group, Mapping) else group
                for group in raw_groups
            ),
            planner_ref=data.get("planner_ref", ""),
            portfolio_strategy=data.get("portfolio_strategy", ""),
            revision=data.get("revision", 0),
        )


@dataclass(frozen=True)
class RequirementRunSnapshot:
    """Typed read-only scheduler state for hosts and Planner agents."""

    run_status: str
    scheduler_status: str
    next_requirement_id: str | None
    runnable_requirement_ids: tuple[str, ...]
    item_outcomes: Mapping[str, str]
    runtime_statuses: Mapping[str, str | None]
    active_analytical_owner_count: int
    active_resolver_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_status": self.run_status,
            "scheduler_status": self.scheduler_status,
            "next_requirement_id": self.next_requirement_id,
            "runnable_requirement_ids": list(self.runnable_requirement_ids),
            "item_outcomes": dict(self.item_outcomes),
            "runtime_statuses": dict(self.runtime_statuses),
            "active_analytical_owner_count": self.active_analytical_owner_count,
            "active_resolver_count": self.active_resolver_count,
        }


@dataclass(frozen=True)
class PlannerAction:
    """One host-dispatchable action selected from current durable state.

    The action does not contain a prompt, answer, calculation, or filesystem
    path.  Planner chooses and dispatches a role; that role still owns the
    actual work and its ordinary technical corrections.
    """

    action: str
    role: str
    subject_id: str
    reason: str
    priority: int = 100
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", _text(self.action, "action"))
        object.__setattr__(self, "role", _text(self.role, "role"))
        object.__setattr__(self, "subject_id", _text(self.subject_id, "subject_id"))
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or self.priority < 0:
            raise ValueError("priority must be a non-negative integer")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "metadata", dict(_jsonable(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "role": self.role,
            "subject_id": self.subject_id,
            "reason": self.reason,
            "priority": self.priority,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class RequirementPhaseSnapshot:
    """Pure, read-only phase projection for one requirement or product.

    The projection is assembled directly from persisted JSON and artifact
    presence.  It never loads an adapter, reconciles lifecycle state, acquires
    a worker lease, or writes a recovery marker.  Hosts can therefore use it
    for status/validation without accidentally scheduling work.
    """

    subject_id: str
    phase: str
    state: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"subject_id": self.subject_id, "phase": self.phase, "state": _jsonable(self.state)}


def _catalog_metadata(entry: DataRoomCatalogEntry | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(entry, DataRoomCatalogEntry):
        if entry.sample_values or entry.sample_rows:
            raise ValueError("planner catalog must not contain samples or rows")
        return {
            "path": entry.path,
            "format": entry.format,
            "kind": entry.kind,
            "size_bytes": entry.size_bytes,
            "compressed_size_bytes": entry.compressed_size_bytes,
            "table_name": entry.table_name,
            "sheet_name": entry.sheet_name,
            "columns": list(entry.columns),
            "row_count": entry.row_count,
            "row_count_exact": entry.row_count_exact,
            "row_count_lower_bound": entry.row_count_lower_bound,
        }
    if not isinstance(entry, Mapping):
        raise TypeError("catalog entries must be DataRoomCatalogEntry values or mappings")

    # Accept a canonical entry mapping while allowlisting metadata.  Any
    # sample/row payload is rejected instead of accidentally crossing the
    # planner boundary.
    forbidden = {"sample_values", "sample_rows", "samples", "rows"}
    nonempty_forbidden = {
        key
        for key in forbidden.intersection(entry)
        if entry.get(key) not in (None, (), [], {}, "")
    }
    if nonempty_forbidden:
        raise ValueError("planner catalog must not contain samples or rows")
    member = entry.get("member")
    if isinstance(member, Mapping):
        source = {**member, **entry}
    else:
        source = entry
    return {
        "path": _text(source.get("path"), "catalog entry path"),
        "format": _text(source.get("format"), "catalog entry format"),
        "kind": _text(source.get("kind"), "catalog entry kind"),
        "size_bytes": source.get("size_bytes"),
        "compressed_size_bytes": source.get("compressed_size_bytes"),
        "table_name": source.get("table_name"),
        "sheet_name": source.get("sheet_name"),
        "columns": list(source.get("columns", ())),
        "row_count": source.get("row_count"),
        "row_count_exact": bool(source.get("row_count_exact", False)),
        "row_count_lower_bound": source.get("row_count_lower_bound"),
    }


def compact_catalog_payload(catalog: CatalogSnapshot | Mapping[str, Any]) -> dict[str, Any]:
    """Return physical catalog metadata without content, hashes, or rows."""

    if isinstance(catalog, CatalogSnapshot):
        raw_entries: Iterable[Any] = catalog.entries
    elif isinstance(catalog, Mapping):
        raw_entries = catalog.get("entries", ())
    else:
        raise TypeError("catalog must be a CatalogSnapshot or mapping")
    return {"entries": [_catalog_metadata(entry) for entry in raw_entries]}


def _copy_item_outcomes(item_outcomes: Any) -> Any:
    if item_outcomes is None:
        return ()
    if isinstance(item_outcomes, Mapping):
        return {key: _jsonable(value) for key, value in item_outcomes.items()}
    if isinstance(item_outcomes, (str, bytes)):
        raise TypeError("item_outcomes must be a mapping or iterable of outcomes")
    return tuple(_jsonable(value) for value in item_outcomes)


def _status(value: Any) -> str | None:
    if isinstance(value, RequirementRecord):
        if value.outcome is not None:
            nested = _status(value.outcome)
            if nested is not None:
                return nested
        return _status(value.status)
    if isinstance(value, Mapping):
        for key in ("status", "outcome", "result", "state"):
            if key in value:
                nested = _status(value[key])
                if nested is not None:
                    return nested
        if value.get("limited") is True:
            return "limited"
        if value.get("succeeded") is True or value.get("ok") is True:
            return "succeeded"
        if value.get("failed") is True:
            return "failed"
        return None
    if value is None:
        return None
    if isinstance(value, bool):
        return "succeeded" if value else "failed"
    if isinstance(value, str):
        return value.strip().lower().replace("-", "_").replace(" ", "_")
    return None


def _runtime_status(value: Any) -> str | None:
    """Read the exact runtime resolver token without normalizing aliases."""

    if isinstance(value, Mapping):
        for key in ("state", "status", "outcome", "result"):
            if key in value:
                nested = _runtime_status(value[key])
                if nested is not None:
                    return nested
        return None
    if isinstance(value, str):
        return value if value in {_RUNTIME_WAITING_STATUS, _RUNTIME_READY_TO_RESUME_STATUS} else None
    for key in ("state", "status", "outcome", "result"):
        nested_value = getattr(value, key, None)
        if nested_value is not None:
            nested = _runtime_status(nested_value)
            if nested is not None:
                return nested
    return None


def _outcome_map(item_outcomes: Any) -> dict[str, str | None]:
    if item_outcomes is None:
        return {}
    if isinstance(item_outcomes, Mapping):
        # A single outcome record is also useful when passed as a one-item
        # mapping; otherwise mappings are interpreted as ID -> outcome.
        if "requirement_id" in item_outcomes or "item_id" in item_outcomes:
            item_id = item_outcomes.get("requirement_id", item_outcomes.get("item_id"))
            return {_text(item_id, "outcome requirement_id"): _status(item_outcomes)}
        return {_text(item_id, "outcome requirement_id"): _status(outcome) for item_id, outcome in item_outcomes.items()}
    if isinstance(item_outcomes, (str, bytes)):
        raise TypeError("item_outcomes must be a mapping or iterable of outcomes")
    result: dict[str, str | None] = {}
    for outcome in item_outcomes:
        if isinstance(outcome, RequirementRecord):
            result[outcome.requirement_id] = _status(outcome)
            continue
        if isinstance(outcome, Mapping):
            item_id = outcome.get("requirement_id", outcome.get("item_id", outcome.get("id")))
            if item_id is None:
                continue
            result[_text(item_id, "outcome requirement_id")] = _status(outcome)
            continue
        item_id = getattr(outcome, "requirement_id", getattr(outcome, "item_id", None))
        if item_id is not None:
            result[_text(item_id, "outcome requirement_id")] = _status(outcome)
    return result


# A group is a shared-investigation unit, not a lifecycle unit. Terminal and
# processed item outcomes do not suppress pending siblings in the same group;
# semantic blocking is owned by the runtime resolution ledger below.
_PENDING_STATUSES = frozenset({"pending", "queued", "ready", "available", "not_started"})
# ``waiting`` remains a normal item outcome.  Runtime/entity-resolution
# scheduling has a separate, exact contract below so similarly named runtime
# aliases cannot accidentally suppress or release a requirement.
_ITEM_WAITING_STATUSES = frozenset({"waiting"})
# A known in-plan dependency is released only by a durable terminal boundary.
# External/unknown dependency IDs are deliberately excluded from the runtime
# map and therefore remain non-blocking, as they were during initial planning.
_DEPENDENCY_TERMINAL_STATUSES = frozenset(
    {
        "accepted",
        "accepted_with_limits",
        "blocked",
        "blocked_by_evidence",
        "complete",
        "completed",
        "done",
        "failed",
        "integrated",
        "limited",
        "processed",
        "succeeded",
        "success",
        "technical_failure",
        "terminal",
    }
)
_RUNTIME_WAITING_STATUS = "waiting_on_resolution"
_RUNTIME_READY_TO_RESUME_STATUS = "ready_to_resume"


def _validated_terminal_integration_boundary(phase: Mapping[str, Any]) -> bool:
    """Return whether an item is a validated terminal integration boundary.

    Failed identity-domain evidence remains actionable until the discovering
    requirement has both a terminal outcome and a validated downstream
    integration boundary.  This predicate consumes only the already-pure
    ``phase_snapshot`` projection; it never loads or reconciles an adapter.
    """

    if not isinstance(phase.get("terminal_outcome"), Mapping):
        return False
    integration_state = phase.get("integration_state")
    validation = phase.get("committed_integration_validation")
    if integration_state == "integrated":
        return (
            phase.get("integration_stage") == "committed"
            and isinstance(validation, Mapping)
            and validation.get("valid") is True
            and validation.get("stage") == "committed"
        )
    if integration_state == "technical_failure":
        return (
            phase.get("integration_stage") == "technical_failure"
            and isinstance(validation, Mapping)
            and validation.get("valid") is True
            and validation.get("stage") == "technical_failure"
        )
    if _status(phase.get("terminal_outcome")) == "blocked_by_evidence":
        blocked_validation = phase.get("blocked_integration_validation")
        return (
            phase.get("integration_stage") == "blocked_by_evidence"
            and isinstance(blocked_validation, Mapping)
            and blocked_validation.get("valid") is True
            and blocked_validation.get("stage") == "blocked_by_evidence"
        )
    return False


class RequirementSupervisorWorkspace:
    """Run-level persistence and scheduling view for the current plan."""

    def __init__(self, context: RunContext) -> None:
        if not isinstance(context, RunContext):
            raise TypeError("RequirementSupervisorWorkspace requires a RunContext")
        self.context = context

    @property
    def plan_path(self) -> Path:
        # The active generation pointer is the sole authority after the first
        # append.  Import lazily to keep lifecycle/planner imports acyclic.
        from .lifecycle import RunLifecycle

        return RunLifecycle.active_plan_path(self.context)

    @staticmethod
    def planner_input(
        requirements: Iterable[RequirementRecord | Mapping[str, Any]],
        catalog: CatalogSnapshot | Mapping[str, Any],
        item_outcomes: Any = (),
    ) -> dict[str, Any]:
        """Build the planner's exact-record, compact-catalog input payload."""

        records = _records(requirements)
        return {
            "requirements": records,
            "catalog": compact_catalog_payload(catalog),
            "item_outcomes": _copy_item_outcomes(item_outcomes),
        }

    def plan_requirements(
        self,
        requirements: Iterable[RequirementRecord | Mapping[str, Any]],
        *,
        planner_ref: str = "requirement-planner",
        portfolio_strategy: str = (
            "automatic explicit-priority, dependency-aware, stable-order planning"
        ),
        revision: int = 1,
        persist: bool = True,
    ) -> RequirementExecutionPlan:
        """Plan the complete caller-supplied requirement list.

        The Planner is the only scheduler of the initial list.  Persistence is
        enabled by default so a coordinator can hand the method raw records
        and immediately use :meth:`next_actions`; callers doing pure analysis
        can request ``persist=False``.
        """

        plan = RequirementExecutionPlan.from_requirements(
            requirements,
            planner_ref=planner_ref,
            portfolio_strategy=portfolio_strategy,
            revision=revision,
        )
        return self.save(plan) if persist else plan

    def save(self, plan: RequirementExecutionPlan | Mapping[str, Any]) -> RequirementExecutionPlan:
        """Save a replan or publish a new mutable portfolio revision.

        Changing requirement records is a normal run edit.  It creates a new
        active generation so the previous portfolio and product remain
        available as history; ordering/group-only changes stay in place.
        """

        candidate = RequirementExecutionPlan.from_dict(plan) if isinstance(plan, Mapping) else plan
        if not isinstance(candidate, RequirementExecutionPlan):
            raise TypeError("plan must be a RequirementExecutionPlan")
        # Plan writes participate in the same run-level CAS boundary as
        # lifecycle reconciliation and generation admission. Resolve the
        # active pointer only after acquiring that lock so a replan cannot
        # race an append between path selection and atomic replacement.
        from .lifecycle import RunLifecycle

        current_path = RunLifecycle.active_plan_path(self.context)
        if current_path.exists() and not current_path.is_symlink():
            current = self._load_path(current_path)
            if current.input_records != candidate.input_records:
                from .run_extension import RequirementRunExtension

                try:
                    RunLifecycle.load(self.context)
                except FileNotFoundError:
                    # Planning can be authored before a durable run is
                    # created.  In that case there is no item history to
                    # revise; save the latest requested portfolio directly.
                    pass
                else:
                    RequirementRunExtension.revise(self.context, plan=candidate)
                    return self.load()

        with RunLifecycle._run_lock(self.context):
            destination = RunLifecycle._active_plan_path_unlocked(self.context)
            return self._save_unlocked(candidate, destination)

    def _save_unlocked(
        self,
        candidate: RequirementExecutionPlan,
        destination: Path,
    ) -> RequirementExecutionPlan:
        """Persist ``candidate`` while the caller owns the run lock."""

        if destination.exists() or destination.is_symlink():
            if destination.is_symlink():
                raise ValueError("supervisor plan path cannot be a symlink")
            existing = self._load_path(destination)
            if existing == candidate:
                return existing
            if candidate.revision <= existing.revision:
                raise ValueError("a revised plan must have a higher revision")
        payload = json.dumps(candidate.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=destination.parent, prefix=f".{destination.name}.", delete=False) as stream:
                temporary_name = stream.name
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, destination)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return candidate

    @staticmethod
    def _load_path(path: Path) -> RequirementExecutionPlan:
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("supervisor plan is unreadable") from exc
        return RequirementExecutionPlan.from_dict(value)

    def load(self) -> RequirementExecutionPlan:
        return self._load_path(self.plan_path)

    @staticmethod
    def _runtime_outcome_map(runtime_statuses: Any) -> dict[str, str | None]:
        """Read an external runtime status view without normalizing aliases.

        Runtime/entity-resolution state intentionally remains outside the
        cognitive plan.  The planner only consumes a detached mapping for one
        scheduling decision and never predicts or writes physical domains.
        """

        if runtime_statuses is None:
            return {}
        if isinstance(runtime_statuses, Mapping):
            if "requirement_id" in runtime_statuses or "item_id" in runtime_statuses:
                item_id = runtime_statuses.get("requirement_id", runtime_statuses.get("item_id"))
                return {_text(item_id, "runtime requirement_id"): _runtime_status(runtime_statuses)}
            return {
                _text(item_id, "runtime requirement_id"): _runtime_status(status)
                for item_id, status in runtime_statuses.items()
            }
        if isinstance(runtime_statuses, (str, bytes)):
            raise TypeError("runtime_statuses must be a mapping or iterable of statuses")
        result: dict[str, str | None] = {}
        for status in runtime_statuses:
            if isinstance(status, Mapping):
                item_id = status.get("requirement_id", status.get("item_id", status.get("id")))
                if item_id is not None:
                    result[_text(item_id, "runtime requirement_id")] = _runtime_status(status)
                    continue
            item_id = getattr(status, "requirement_id", getattr(status, "item_id", None))
            if item_id is not None:
                result[_text(item_id, "runtime requirement_id")] = _runtime_status(status)
        return result

    @staticmethod
    def _runtime_allows_resume(status: str | None) -> bool:
        return status == _RUNTIME_READY_TO_RESUME_STATUS

    @staticmethod
    def _runtime_waits(status: str | None) -> bool:
        return status == _RUNTIME_WAITING_STATUS

    def _runnable_requirement_ids(
        self,
        plan: RequirementExecutionPlan,
        item_outcomes: Any = (),
        runtime_statuses: Any = (),
    ) -> tuple[str, ...]:
        """Return runnable IDs in the plan's flattened cognitive order."""

        outcomes = _outcome_map(item_outcomes)
        runtime = self._runtime_outcome_map(runtime_statuses)
        known_ids = {record.requirement_id for record in plan.input_records}
        dependencies: dict[str, tuple[str, ...]] = {}
        for record in plan.input_records:
            declared = list(record.dependencies)
            metadata = record.metadata if isinstance(record.metadata, Mapping) else {}
            observed = metadata.get(
                "observed_dependencies",
                metadata.get(
                    "observed_dependency_ids",
                    metadata.get(
                        "depends_on_requirement_ids",
                        metadata.get("depends_on", metadata.get("dependencies", ())),
                    ),
                ),
            )
            if isinstance(observed, str):
                observed = (observed,)
            if observed:
                declared.extend(str(value) for value in observed)
            dependencies[record.requirement_id] = tuple(
                dict.fromkeys(
                    value
                    for value in declared
                    if value in known_ids and value != record.requirement_id
                )
            )
        runnable: set[str] = set()
        for group in plan.groups:
            for requirement_id in group.requirement_ids:
                outcome = outcomes.get(requirement_id)
                runtime_status = runtime.get(requirement_id)
                known_dependencies = dependencies.get(requirement_id, ())
                if known_dependencies and any(
                    self._runtime_waits(runtime.get(dependency_id))
                    or _status(outcomes.get(dependency_id)) not in _DEPENDENCY_TERMINAL_STATUSES
                    for dependency_id in known_dependencies
                ):
                    # Runtime dependency state is authoritative after the
                    # initial topological order.  A waiting/active/pending
                    # dependency therefore keeps this item out of the offer;
                    # only an explicit terminal/accepted boundary releases it.
                    continue
                if self._runtime_allows_resume(runtime_status):
                    # A resolver has explicitly released this requirement.
                    # Its prior waiting marker is not a terminal outcome.
                    if outcome is None or outcome in _PENDING_STATUSES or outcome in _ITEM_WAITING_STATUSES:
                        runnable.add(requirement_id)
                    continue
                if self._runtime_waits(runtime_status):
                    continue
                if outcome is None or outcome in _PENDING_STATUSES:
                    runnable.add(requirement_id)
                # Runtime-active and other nonterminal statuses own the item
                # until an external status makes it runnable again.
        return tuple(requirement_id for requirement_id in plan.execution_order if requirement_id in runnable)

    def ready_groups(
        self,
        item_outcomes: Any = (),
        runtime_statuses: Any = (),
    ) -> tuple[RequirementExecutionGroup, ...]:
        """Return pending groups in advisory cognitive plan order.

        ``runtime_statuses`` is an external, read-only scheduling view.  In
        particular, ``waiting_on_resolution`` suppresses a requirement while
        ``ready_to_resume`` releases it at its original plan position.
        """

        plan = self.load()
        runnable = set(self._runnable_requirement_ids(plan, item_outcomes, runtime_statuses))
        ready: list[RequirementExecutionGroup] = []
        for group in plan.groups:
            pending_ids = tuple(requirement_id for requirement_id in group.requirement_ids if requirement_id in runnable)
            if not pending_ids:
                continue
            if pending_ids == group.requirement_ids:
                ready.append(group)
            else:
                # A mixed-outcome/waiting group is projected to a scheduling
                # view containing only requirements that can run now.  Group
                # rationale and advisory group metadata are preserved.
                ready.append(
                    RequirementExecutionGroup(
                        pending_ids,
                        group.rationale,
                        shared_analysis_intent=group.shared_analysis_intent,
                        suggested_specialists=group.suggested_specialists,
                    )
                )
        return tuple(ready)

    def next_group(self, item_outcomes: Any = (), runtime_statuses: Any = ()) -> RequirementExecutionGroup | None:
        groups = self.ready_groups(item_outcomes, runtime_statuses)
        return groups[0] if groups else None

    def next_requirement(self, item_outcomes: Any = (), runtime_statuses: Any = ()) -> str | None:
        """Return the earliest runnable requirement ID in cognitive plan order."""

        plan = self.load()
        runnable = self._runnable_requirement_ids(plan, item_outcomes, runtime_statuses)
        return runnable[0] if runnable else None

    def runtime_snapshot(self) -> RequirementRunSnapshot:
        """Return one typed, current scheduling snapshot for this run.

        The method joins the three program-owned authorities that a host used
        to have to assemble manually: item state, entity-resolution waits, and
        top-level lifecycle state.  It never launches work; it only reports the
        next requirement when the single Analytical Owner slot is available.
        """

        from .durable import ItemWorkspace
        from .entity_resolution import EntityResolutionWorkspace
        from .lifecycle import RunLifecycle

        lifecycle = RunLifecycle.load(self.context)
        items = tuple(
            ItemWorkspace.load(self.context, item_id, mode="requirement")
            for item_id in lifecycle.item_ids
        )
        run_snapshot = lifecycle.snapshot if lifecycle.paused else lifecycle.reconcile(items)

        try:
            resolution = EntityResolutionWorkspace.load(self.context)
        except FileNotFoundError:
            resolution = None
            runtime_statuses: Mapping[str, Mapping[str, Any]] = {}
            active_resolver_count = 0
            active_owner_leases = 0
        else:
            runtime_statuses = resolution.requirement_runtime_statuses()
            active_resolver_count = resolution.active_resolution_count
            active_owner_leases = sum(
                lease.worker_type == "analytical_owner"
                for lease in resolution.active_leases
            )

        item_outcomes: dict[str, str] = {}
        active_owner_items = 0
        for item in items:
            state = item.state
            terminal = state.get("terminal_outcome")
            if isinstance(terminal, Mapping):
                item_outcomes[item.item_id] = _status(terminal) or "accepted"
                continue
            runtime_status = _runtime_status(runtime_statuses.get(item.item_id))
            if runtime_status in {_RUNTIME_WAITING_STATUS, _RUNTIME_READY_TO_RESUME_STATUS}:
                item_outcomes[item.item_id] = "waiting"
                continue
            if state.get("active_attempt_id") is not None:
                item_outcomes[item.item_id] = "active"
                active_owner_items += 1
                continue
            attempts = state.get("attempts")
            lifecycle_state = state.get("lifecycle_state")
            item_outcomes[item.item_id] = (
                "pending"
                if lifecycle_state == "work" and (not isinstance(attempts, list) or not attempts)
                else "active"
            )

        plan = self.load()
        runnable = self._runnable_requirement_ids(plan, item_outcomes, runtime_statuses)
        owner_busy = active_owner_items > 0 or active_owner_leases > 0
        next_requirement_id = None if lifecycle.paused or owner_busy or not runnable else runnable[0]
        if lifecycle.paused:
            scheduler_status = "paused"
            runnable = ()
        elif owner_busy:
            scheduler_status = "at_capacity" if runnable else "running"
        else:
            scheduler_status = self.all_waiting_status(
                item_outcomes,
                runtime_statuses,
                active_resolver_count=active_resolver_count,
            )
        return RequirementRunSnapshot(
            run_status=run_snapshot.state,
            scheduler_status=scheduler_status,
            next_requirement_id=next_requirement_id,
            runnable_requirement_ids=tuple(runnable),
            item_outcomes=dict(item_outcomes),
            runtime_statuses={
                requirement_id: _runtime_status(status)
                for requirement_id, status in runtime_statuses.items()
            },
            active_analytical_owner_count=max(active_owner_items, active_owner_leases),
            active_resolver_count=active_resolver_count,
        )

    def scheduling_tick(self) -> dict[str, Any]:
        """Backward-free dictionary projection of :meth:`runtime_snapshot`.

        Hosts that want stable attribute access should use
        :meth:`runtime_snapshot`; the dictionary remains the compact payload
        passed to a model-facing Planner.
        """

        return self.runtime_snapshot().to_dict()

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, Any] | None:
        """Read one optional phase artifact without reconciling or writing."""

        if path.is_symlink() or not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"_invalid": True}
        return dict(value) if isinstance(value, Mapping) else {"_invalid": True}

    def _pure_lifecycle_view(self) -> dict[str, Any]:
        """Return a read-only view from the lifecycle's validated authority."""

        from .lifecycle import RunLifecycle

        metadata = RunLifecycle.active_generation_metadata(self.context)
        lifecycle = RunLifecycle.load(self.context)
        state = lifecycle.to_dict()
        if metadata is None:
            return {
                "valid": True,
                "state": state,
                "item_ids": tuple(lifecycle.item_ids),
                "generation_id": lifecycle.generation_id,
                "product_manifest_ref": lifecycle.product_manifest_ref,
                "metadata": None,
                "plan_path": str(lifecycle.plan_path),
            }
        return {
            "valid": True,
            "state": state,
            "item_ids": tuple(metadata.cumulative_item_ids),
            "generation_id": metadata.generation_id,
            "product_manifest_ref": metadata.product_manifest_ref,
            "metadata": metadata,
            "plan_path": metadata.plan_path,
        }

    def phase_snapshot(self) -> dict[str, Any]:
        """Return a non-mutating persisted phase view for Planner/status APIs.

        This helper intentionally bypasses ``ItemWorkspace.load`` and
        ``RunLifecycle.reconcile``.  A malformed or missing artifact is
        represented as ``invalid``/``missing`` so the caller can schedule a
        repair or report a block instead of treating adapter output as truth.
        """

        try:
            lifecycle = self._pure_lifecycle_view()
        except Exception as exc:
            diagnostic = {"valid": False, "stage": "invalid", "diagnostics": [str(exc)]}
            return {
                "run_id": self.context.run_id,
                "generation_id": None,
                "lifecycle_state": None,
                "lifecycle_validation": diagnostic,
                "items": {},
                "all_items_integrated": False,
                "product": {"validation": diagnostic, "manifest": None},
                "report": {"validation": diagnostic, "preflight": None, "final_report": None},
            }
        item_ids = lifecycle["item_ids"]
        items: dict[str, dict[str, Any]] = {}
        for item_id in item_ids:
            item_root = self.context.resolve_run_path(f"requirements/{item_id}")
            state_path = item_root / "item_state.json"
            state = self._read_json_object(state_path)
            state_value = state or {"_missing": True}
            integration_root = item_root / "integration"
            staging = integration_root / "staging"
            fidelity_view = inspect_integration_fidelity(self.context, item_id)
            session = self._read_json_object(staging / "session.json")
            snapshot = self._read_json_object(staging / "snapshot.json")
            committed_view = inspect_committed_integration(self.context, item_id)
            terminal = state_value.get("terminal_outcome") if isinstance(state_value, Mapping) else None
            review = state_value.get("review") if isinstance(state_value.get("review"), Mapping) else {}
            if not isinstance(review, Mapping):
                review = {}
            blocked_integration_view = _inspect_blocked_terminal_integration(
                item_root,
                state_value,
                terminal,
                fidelity_view,
                committed_view,
            )
            if blocked_integration_view.get("valid") is True:
                integration_stage = "blocked_by_evidence"
            elif _status(terminal) == "blocked_by_evidence":
                # A blocked terminal with any stale pointer, foreign
                # integration artifact, or other residue is not a valid
                # no-op terminal. Keep it in repair, never product routing.
                integration_stage = "invalid"
            elif committed_view.get("stage") == "technical_failure" and committed_view.get("valid"):
                integration_stage = "technical_failure"
            elif not fidelity_view.get("valid") or not committed_view.get("valid"):
                integration_stage = "invalid"
            elif committed_view.get("stage") == "committed":
                integration_stage = "committed"
            elif session is None and snapshot is None:
                integration_stage = "not_started"
            elif fidelity_view.get("stage") == "awaiting_targeted_fidelity_review":
                integration_stage = "awaiting_targeted_fidelity_review"
            else:
                integration_stage = "staged"
            fidelity_verdict = fidelity_view.get("verdict") if fidelity_view.get("valid") else None
            items[item_id] = {
                "item_id": item_id,
                "terminal_outcome": _jsonable(terminal),
                "terminal_status": _status(terminal),
                "lifecycle_state": state_value.get("lifecycle_state"),
                "integration_state": state_value.get("integration_state", "pending"),
                "integration_stage": integration_stage,
                "fidelity_verdict": fidelity_verdict,
                "integration_validation": fidelity_view,
                "committed_integration_validation": committed_view,
                "blocked_integration_validation": blocked_integration_view,
                "business_review_status": review.get("status"),
                "business_review_verdict": review.get("verdict"),
                "paths": {
                    "item_state": str(state_path.relative_to(self.context.run_root)),
                    "integration_session": str((staging / "session.json").relative_to(self.context.run_root)),
                    "integration_fidelity": str((integration_root / "review" / "result.json").relative_to(self.context.run_root)),
                    "integration_manifest": str((integration_root / "committed" / "manifest.json").relative_to(self.context.run_root)),
                },
            }

        generation_id = lifecycle["generation_id"]
        product_root = self.context.resolve_product_path(f"generations/{generation_id}")
        manifest_ref = lifecycle["product_manifest_ref"]
        manifest_path = self.context.resolve_run_path(manifest_ref)
        product_view = inspect_product_manifest(
            self.context,
            generation_id,
            manifest_ref,
            metadata=lifecycle.get("metadata"),
        )
        product_manifest = product_view.get("manifest")
        candidate_path = product_root / "product_candidate.json"
        review_path = product_root / "product_review.json"
        authorization_path = product_root / "publish_authorization.json"
        candidate = self._read_json_object(candidate_path)
        product_review = self._read_json_object(review_path)
        authorization = self._read_json_object(authorization_path)
        product_validation: dict[str, Any] = product_view
        if candidate is not None or product_review is not None or authorization is not None:
            try:
                from .product_review import ProductReviewStore

                product_store = ProductReviewStore(self.context, generation_id)
                candidate = product_store.load_candidate().to_dict() if candidate is not None else None
                product_review = product_store.load_review().to_dict() if product_review is not None else None
                authorization = product_store.load_authorization().to_dict() if authorization is not None else None
            except Exception as exc:
                product_validation = {"valid": False, "diagnostics": [str(exc)]}
        report_preflight_path = self.context.resolve_run_path("reporting/report_preflight.json")
        final_report_path = self.context.resolve_run_path("reporting/final_report.json")
        from .reporting import inspect_report_artifacts

        report_view = inspect_report_artifacts(self.context, run_id=self.context.run_id)
        report_preflight = report_view.get("preflight")
        report = report_view.get("report")
        return {
            "run_id": self.context.run_id,
            "generation_id": generation_id,
            "lifecycle_state": lifecycle["state"].get("state", lifecycle["state"].get("status")),
            "lifecycle_validation": {"valid": True, "diagnostics": []},
            "items": items,
            "all_items_integrated": bool(items) and all(
                (
                    item.get("integration_state") == "technical_failure"
                    and item.get("integration_stage") == "technical_failure"
                    and item.get("committed_integration_validation", {}).get("valid") is True
                )
                or (
                    item.get("integration_state") == "integrated"
                    and item.get("committed_integration_validation", {}).get("valid") is True
                )
                or (
                    item.get("terminal_status") == "blocked_by_evidence"
                    and item.get("integration_stage") == "blocked_by_evidence"
                    and item.get("blocked_integration_validation", {}).get("valid") is True
                )
                for item in items.values()
            ),
            "product": {
                "manifest_ref": manifest_ref,
                "manifest": product_manifest,
                "manifest_status": product_manifest.get("status") if isinstance(product_manifest, Mapping) else None,
                "candidate_ref": str(candidate_path.relative_to(self.context.run_root)),
                "candidate": candidate,
                "review_ref": str(review_path.relative_to(self.context.run_root)),
                "review": product_review,
                "authorization_ref": str(authorization_path.relative_to(self.context.run_root)),
                "authorization": authorization,
                "validation": product_validation,
            },
            "report": {
                "preflight_ref": str(report_preflight_path.relative_to(self.context.run_root)),
                "preflight": report_preflight,
                "final_report_ref": str(final_report_path.relative_to(self.context.run_root)),
                "final_report": report,
                "validation": report_view,
            },
        }

    def phase_snapshots(self) -> tuple[RequirementPhaseSnapshot, ...]:
        """Typed convenience projection over :meth:`phase_snapshot`."""

        snapshot = self.phase_snapshot()
        values = [
            RequirementPhaseSnapshot(item_id, "requirement", value)
            for item_id, value in snapshot["items"].items()
        ]
        values.append(RequirementPhaseSnapshot(snapshot["generation_id"], "product", snapshot["product"]))
        values.append(RequirementPhaseSnapshot(snapshot["run_id"], "report", snapshot["report"]))
        return tuple(values)

    def reconcile_identity_requests(self) -> tuple[Mapping[str, Any], ...]:
        """Ingest item-local AO proposals into the shared domain registry.

        This is the sole Planner-side admission path for semantic identity
        requests.  It is intentionally idempotent: exact proposal retries
        return the existing reservation and wait record, while a second
        requirement attaches a new request record to the same canonical
        domain.  Historical proposal owner labels remain provenance; the
        current program-owned item binding authorizes admission.  No Entity
        Resolution Owner is launched here; dispatch remains the responsibility
        of :meth:`next_actions`.
        """

        from .analyst_workspace import IdentityDomainProposal
        from .durable import ItemWorkspace
        from .entity_resolution import EntityResolutionWorkspace
        from .lifecycle import RunLifecycle

        lifecycle = RunLifecycle.load(self.context)
        if lifecycle.paused:
            return ()
        items = {
            item_id: ItemWorkspace.load(self.context, item_id, mode="requirement")
            for item_id in lifecycle.item_ids
        }
        phase_items = self.phase_snapshot().get("items", {})
        proposal_batches: list[tuple[str, str, tuple[IdentityDomainProposal, ...]]] = []
        for item_id in lifecycle.item_ids:
            if _validated_terminal_integration_boundary(phase_items.get(item_id, {})):
                # Historical failed/accepted domains remain durable evidence,
                # but they must not be reattached to a newly runnable plan.
                continue
            item = items[item_id]
            raw_rows = item.read_identity_domain_proposals()
            proposals: list[IdentityDomainProposal] = []
            seen: dict[str, IdentityDomainProposal] = {}
            for raw in raw_rows:
                if raw.get("item_id") != item_id:
                    raise ValueError("identity domain proposal item binding is invalid")
                owner_ref = raw.get("owner_ref")
                if not isinstance(owner_ref, str) or not owner_ref.strip():
                    raise ValueError("identity domain proposal owner binding is invalid")
                proposal = IdentityDomainProposal.from_dict(raw)
                prior = seen.get(proposal.domain_id)
                if prior is not None and prior != proposal:
                    raise ValueError("identity domain proposal conflicts with prior item proposal")
                if prior is None:
                    seen[proposal.domain_id] = proposal
                    proposals.append(proposal)
            if proposals:
                proposal_batches.append((item_id, item.analysis_owner_ref(), tuple(proposals)))
        if not proposal_batches:
            return ()

        try:
            resolution = EntityResolutionWorkspace.load(self.context)
        except FileNotFoundError:
            resolution = EntityResolutionWorkspace.create(self.context)

        reconciled: list[Mapping[str, Any]] = []
        for item_id, owner_ref, proposals in proposal_batches:
            unresolved: list[str] = []
            states: dict[str, str] = {}
            for proposal in proposals:
                reservation = resolution.reserve_identity_domain(
                    proposal.domain_id,
                    proposal.object_type,
                    item_id,
                    proposal.rationale,
                    source_hints=proposal.source_hints,
                    representation_item_ids=proposal.representation_item_ids,
                    request_owner_ref=owner_ref,
                )
                states[proposal.domain_id] = reservation.state
                # A failed domain is not a satisfied request.  Keep every
                # exact requester waiting so the Planner can expose one
                # repair/escalation while unrelated requirements continue.
                if reservation.state != "ready":
                    unresolved.append(proposal.domain_id)
            if unresolved:
                waiting = resolution.mark_waiting_on_resolution(
                    item_id,
                    tuple(unresolved),
                    "; ".join(
                        f"identity domain {domain_id} is {states[domain_id]}"
                        for domain_id in unresolved
                    ),
                    owner_ref=owner_ref,
                )
            else:
                waiting = None
            reconciled.append(
                {
                    "requirement_id": item_id,
                    "domain_ids": tuple(proposal.domain_id for proposal in proposals),
                    "unresolved_domain_ids": tuple(unresolved),
                    "states": dict(states),
                    "waiting": waiting,
                }
            )
        return tuple(reconciled)

    def next_actions(self) -> tuple[PlannerAction, ...]:
        """Derive role dispatches from durable run state without doing work.

        Entity resolution, reviews, integration, and product construction are
        first-class actions.  A waiting requirement never occupies the single
        Analytical Owner slot, so another runnable requirement can be returned
        in the same decision.  The Planner may dispatch these actions; it must
        not perform their calculations or reviews itself.
        """

        from .durable import ItemWorkspace
        from .entity_resolution import EntityResolutionWorkspace
        from .lifecycle import RunLifecycle

        # Validate lifecycle pointers/state before asking the runtime adapter
        # to reconcile or classify work.  A forged pointer must yield only a
        # diagnostic repair action, never an analytical/integration advance.
        phase_view = self.phase_snapshot()
        if not (phase_view.get("lifecycle_validation") or {}).get("valid", False):
            return (
                PlannerAction(
                    "repair_run_lifecycle",
                    "planner",
                    self.context.run_id,
                    "validated lifecycle pointer/state is unavailable; no phase advancement is authorized",
                    priority=1,
                    metadata={"validation": phase_view.get("lifecycle_validation")},
                ),
            )
        # ``ItemWorkspace.load`` correctly rejects a pending item state that
        # carries a forged integration pointer.  Surface that persisted
        # phase defect as an integration repair instead of letting the
        # runtime adapter exception prevent a truthful Planner action.
        invalid_blocked_repairs = tuple(
            PlannerAction(
                "repair_integration_fidelity",
                "integration_agent",
                item_id,
                "blocked terminal has a forged, stale, or residual integration boundary; no product routing is authorized",
                priority=31,
                metadata={
                    "integration_stage": phase.get("integration_stage"),
                    "validation": phase.get("blocked_integration_validation") or {},
                    "requires_rethink": True,
                },
            )
            for item_id, phase in phase_view.get("items", {}).items()
            if phase.get("terminal_status") == "blocked_by_evidence"
            and phase.get("integration_stage") == "invalid"
        )
        if invalid_blocked_repairs:
            return invalid_blocked_repairs
        try:
            self.reconcile_identity_requests()
        except (KeyError, TypeError, ValueError) as exc:
            # Proposal ingestion is a Planner admission boundary.  A malformed
            # or conflicting semantic request must be repairable and must not
            # be mistaken for a runnable requirement.
            return (
                PlannerAction(
                    "repair_identity_request",
                    "planner",
                    self.context.run_id,
                    "item-local identity-domain proposal could not be reconciled",
                    priority=4,
                    metadata={"error": str(exc), "requires_rethink": True},
                ),
            )
        snapshot = self.runtime_snapshot()
        if snapshot.scheduler_status == "paused":
            return ()
        lifecycle = RunLifecycle.load(self.context)
        items = {
            item_id: ItemWorkspace.load(self.context, item_id, mode="requirement")
            for item_id in lifecycle.item_ids
        }
        # Read phase artifacts once, without adapter reconciliation.  The
        # scheduling snapshot above still owns capacity/waiting semantics;
        # phase transitions below are derived only from these persisted facts.
        # The read-only phase projection above is reused for all downstream
        # decisions; no adapter reconciliation occurs during this step.
        phase_items = phase_view["items"]
        actions: list[PlannerAction] = []

        try:
            resolution = EntityResolutionWorkspace.load(self.context)
        except FileNotFoundError:
            resolution = None
            runtime_statuses: Mapping[str, Mapping[str, Any]] = {}
            active_leases = ()
        else:
            runtime_statuses = resolution.requirement_runtime_statuses()
            active_leases = resolution.active_leases
            active_resolution_subjects = {
                lease.subject_id
                for lease in active_leases
                if lease.worker_type == "entity_resolution"
            }
            plan = self.load()
            plan_positions = {
                requirement_id: index
                for index, requirement_id in enumerate(plan.execution_order)
            }

            def domain_order(domain: Any) -> tuple[int, str]:
                positions = [plan_positions[item_id] for item_id in domain.requested_by if item_id in plan_positions]
                return (min(positions) if positions else len(plan_positions), domain.domain_id)

            planned_resolution_slots = resolution.active_resolution_count
            for domain in sorted(resolution.domains(), key=domain_order):
                request_metadata = {
                    "discovered_by_item_id": domain.discovered_by_item_id,
                }
                if len(domain.requested_by) > 1:
                    request_metadata.update(
                        {
                            "requested_by": list(domain.requested_by),
                            "requests": [dict(value) for value in domain.requests],
                        }
                    )
                if domain.state == "reserved":
                    if planned_resolution_slots >= resolution.capacity.entity_resolution:
                        continue
                    actions.append(
                        PlannerAction(
                            "resolve_identity",
                            "entity_resolution_owner",
                            domain.domain_id,
                            "identity domain is reserved and has no submitted result",
                            priority=10,
                            metadata=request_metadata,
                        )
                    )
                    planned_resolution_slots += 1
                elif domain.state == "repair":
                    if domain.domain_id in active_resolution_subjects:
                        continue
                    if planned_resolution_slots >= resolution.capacity.entity_resolution:
                        continue
                    actions.append(
                        PlannerAction(
                            "repair_identity_result",
                            "entity_resolution_owner",
                            domain.domain_id,
                            "independent identity review authorized one targeted repair",
                            priority=10,
                            metadata={**request_metadata, "repair_count": domain.repair_count},
                        )
                    )
                    planned_resolution_slots += 1
                elif domain.state == "review_pending" and domain.accepted_pending_commit:
                    actions.append(
                        PlannerAction(
                            "commit_identity_result",
                            "identity_reviewer",
                            domain.domain_id,
                            "independent identity review accepted the validated result",
                            priority=20,
                            metadata=request_metadata,
                        )
                    )
                elif domain.state == "review_pending":
                    actions.append(
                        PlannerAction(
                            "review_identity_result",
                            "identity_reviewer",
                            domain.domain_id,
                            "validated identity result is waiting for independent review",
                            priority=20,
                            metadata=request_metadata,
                        )
                    )
                elif domain.state == "resolving" and domain.domain_id not in active_resolution_subjects:
                    if planned_resolution_slots >= resolution.capacity.entity_resolution:
                        continue
                    actions.append(
                        PlannerAction(
                            "resume_identity_resolution",
                            "entity_resolution_owner",
                            domain.domain_id,
                            "identity domain is resolving but no resolver lease is active",
                            priority=10,
                            metadata=request_metadata,
                        )
                    )
                    planned_resolution_slots += 1
                elif domain.state == "failed":
                    # ``requested_by`` is the authoritative run-level
                    # requester collection.  The first discoverer remains
                    # useful provenance, but cannot suppress a failure for a
                    # later active requester sharing the canonical domain.
                    requester_ids = tuple(
                        dict.fromkeys(
                            (
                                *domain.requested_by,
                                *(
                                    value.get("item_id")
                                    for value in domain.requests
                                    if isinstance(value, Mapping) and value.get("item_id") is not None
                                ),
                            )
                        )
                    )
                    active_requesters = tuple(
                        item_id
                        for item_id in requester_ids
                        if item_id in lifecycle.item_ids
                        and not _validated_terminal_integration_boundary(phase_items.get(item_id, {}))
                    )
                    if not active_requesters and any(item_id not in lifecycle.item_ids for item_id in requester_ids):
                        # A failed domain bound to an item outside the active
                        # cumulative lifecycle is not historical evidence we
                        # may silently discard. Keep the escalation and make
                        # the foreign binding explicit for a Planner repair.
                        actions.append(
                            PlannerAction(
                                "escalate_identity_failure",
                                "planner",
                                domain.domain_id,
                                "identity domain is terminally failed with a foreign discovering-item binding",
                                priority=5,
                                metadata={
                                    **request_metadata,
                                    "binding_status": "foreign",
                                    "requires_rethink": True,
                                },
                            )
                        )
                        continue
                    if not active_requesters:
                        # The domain remains in the run-level entity registry
                        # as durable historical evidence. Its already
                        # terminal, validated requesters must not let that
                        # evidence preempt a newly appended requirement.
                        continue
                    actions.append(
                        PlannerAction(
                            "escalate_identity_failure",
                            "planner",
                            domain.domain_id,
                            "identity domain is terminally failed",
                            priority=5,
                            metadata={
                                **request_metadata,
                                "binding_status": "active_unresolved",
                            },
                        )
                    )

        # A terminal analytical item can be integrated independently of the
        # single AO lane, while the next AO requirement proceeds.
        for item_id in lifecycle.item_ids:
            item = items[item_id]
            state = item.state
            phase = phase_items.get(item_id, {})
            terminal = state.get("terminal_outcome")
            if _status(terminal) == "blocked_by_evidence":
                blocked_validation = phase.get("blocked_integration_validation") or {}
                if phase.get("integration_stage") != "blocked_by_evidence" or blocked_validation.get("valid") is not True:
                    actions.append(
                        PlannerAction(
                            "repair_integration_fidelity",
                            "integration_agent",
                            item_id,
                            "blocked terminal has a forged, stale, or residual integration boundary; no product routing is authorized",
                            priority=31,
                            metadata={
                                "integration_stage": phase.get("integration_stage"),
                                "validation": blocked_validation,
                                "requires_rethink": True,
                            },
                        )
                    )
                # A validated blocked-by-evidence terminal is a legitimate
                # no-integration outcome. It never receives integrate,
                # fidelity-review, or integration-commit actions.
                continue
            if isinstance(terminal, Mapping) and phase.get("integration_state") == "technical_failure":
                technical_validation = phase.get("committed_integration_validation") or {}
                if phase.get("integration_stage") != "technical_failure" or technical_validation.get("valid") is not True:
                    actions.append(
                        PlannerAction(
                            "repair_integration_fidelity",
                            "integration_agent",
                            item_id,
                            "technical-failure integration manifest is missing, foreign, stale, or hash-invalid; no product routing is authorized",
                            priority=31,
                            metadata={
                                "integration_stage": phase.get("integration_stage"),
                                "validation": technical_validation,
                                "requires_rethink": True,
                            },
                        )
                    )
                continue
            if isinstance(terminal, Mapping) and phase.get("integration_state") == "pending":
                integration_stage = phase.get("integration_stage")
                fidelity_verdict = phase.get("fidelity_verdict")
                if integration_stage == "invalid":
                    actions.append(
                        PlannerAction(
                            "repair_integration_fidelity",
                            "integration_agent",
                            item_id,
                            "integration boundary is forged, stale, or hash-invalid; no commit or fresh staging is authorized",
                            priority=31,
                            metadata={"integration_stage": integration_stage, "requires_rethink": True},
                        )
                    )
                elif integration_stage == "not_started":
                    actions.append(
                        PlannerAction(
                            "integrate_requirement",
                            "integration_agent",
                            item_id,
                            "reviewed requirement is terminal and awaits integration staging",
                            priority=30,
                            metadata={"outcome": _status(terminal), "integration_stage": integration_stage},
                        )
                    )
                elif fidelity_verdict is None:
                    targeted_recheck = integration_stage == "awaiting_targeted_fidelity_review"
                    actions.append(
                        PlannerAction(
                            "review_integration_fidelity",
                            "integration_fidelity_reviewer",
                            item_id,
                            (
                                "authorized integration repair is complete and the rebuilt packet is waiting "
                                "for one targeted independent fidelity recheck"
                                if targeted_recheck
                                else "staged integration is waiting for one independent fidelity review"
                            ),
                            priority=31,
                            metadata={
                                "integration_stage": integration_stage,
                                **({"targeted_recheck": True} if targeted_recheck else {}),
                            },
                        )
                    )
                elif fidelity_verdict == "repair_once":
                    actions.append(
                        PlannerAction(
                            "repair_integration_fidelity",
                            "integration_agent",
                            item_id,
                            "fidelity review authorized one targeted integration repair",
                            priority=31,
                            metadata={"fidelity_verdict": fidelity_verdict},
                        )
                    )
                elif fidelity_verdict in {"accept", "accept_with_limits", "unavailable"}:
                    actions.append(
                        PlannerAction(
                            "commit_integration_requirement",
                            "integration_agent",
                            item_id,
                            "fidelity boundary is terminal and the integration commit is pending",
                            priority=32,
                            metadata={"fidelity_verdict": fidelity_verdict},
                        )
                    )
                elif fidelity_verdict in {"fail", "blocked", "blocked_rethink"}:
                    actions.append(
                        PlannerAction(
                            "repair_integration_fidelity",
                            "integration_agent",
                            item_id,
                            "fidelity review did not authorize a commit",
                            priority=31,
                            metadata={"fidelity_verdict": fidelity_verdict, "requires_rethink": True},
                        )
                    )
                continue
            if terminal is not None:
                continue
            runtime_status = _runtime_status(runtime_statuses.get(item_id))
            if runtime_status == _RUNTIME_WAITING_STATUS:
                continue
            active_attempt = state.get("active_attempt_id")
            has_active_owner = any(
                lease.worker_type == "analytical_owner" and lease.subject_id == item_id
                for lease in active_leases
            )
            if active_attempt is not None and not has_active_owner:
                actions.append(
                    PlannerAction(
                        "resume_requirement_analysis",
                        "analytical_owner",
                        item_id,
                        "active analytical attempt has no active owner lease",
                        priority=40,
                        metadata={
                            "attempt_id": active_attempt,
                            "no_progress_count": int(state.get("consecutive_no_progress", 0)),
                        },
                    )
                )
                continue
            review = state.get("review") if isinstance(state.get("review"), Mapping) else {}
            repair_packet = self._read_json_object(item.item_root / "work" / "business_review.json")
            repair_active = isinstance(repair_packet, Mapping) and repair_packet.get("repair_active") is True
            attempts = state.get("attempts") if isinstance(state.get("attempts"), list) else []
            completed_attempts = sum(
                isinstance(record, Mapping) and record.get("status") == "completed"
                for record in attempts
            )
            repair_count = int(state.get("business_repair_count", 0) or 0)
            # ``use_business_repair`` keeps the authorization packet active
            # while the same AO action is between dispatch and its next
            # attempt.  The prior completed attempt is still present, so a
            # pending review at that instant would be a stale concurrent
            # offer.  Once the repair attempt is completed, the targeted
            # reviewer is intentionally re-offered while the packet remains
            # active until that re-check closes it.
            repair_waiting_for_attempt = (
                repair_active
                and repair_count > 0
                and review.get("status") == "pending"
                and completed_attempts <= repair_count
            )
            if active_attempt is None and repair_waiting_for_attempt:
                actions.append(
                    PlannerAction(
                        "resume_requirement_analysis",
                        "analytical_owner",
                        item_id,
                        "active business-repair authorization has no completed repair attempt; resume the same-owner attempt",
                        priority=40,
                        metadata={
                            "repair_active": True,
                            "repair_count": repair_count,
                        },
                    )
                )
            elif active_attempt is None and review.get("status") == "pending":
                if attempts and any(record.get("status") == "completed" for record in attempts) and (item.item_root / "draft.json").is_file():
                    actions.append(
                        PlannerAction(
                            "review_requirement",
                            "business_reviewer",
                            item_id,
                            "completed analytical attempt has a draft awaiting independent review",
                            priority=50,
                        )
                    )
                elif item_id == snapshot.next_requirement_id:
                    actions.append(
                        PlannerAction(
                            "analyze_requirement",
                            "analytical_owner",
                            item_id,
                            "requirement is the next runnable item in the cognitive plan",
                            priority=40,
                        )
                    )
            elif active_attempt is None and review.get("status") == "reviewed" and review.get("verdict") in {"repair_once", "repair"}:
                actions.append(
                    PlannerAction(
                        "repair_requirement",
                        "analytical_owner",
                        item_id,
                        "business review durably authorized a same-owner repair",
                        priority=45,
                        metadata={"review_verdict": review.get("verdict"), "repair_count": state.get("business_repair_count", 0)},
                    )
                )
            elif active_attempt is None and review.get("status") in {"reviewed", "unavailable"}:
                actions.append(
                    PlannerAction(
                        "finalize_requirement_review",
                        "business_reviewer",
                        item_id,
                        "review is recorded but the immutable terminal item has not been published",
                        priority=25,
                    )
                )

        if phase_view["all_items_integrated"]:
            product = phase_view["product"]
            product_metadata = {
                "generation_id": lifecycle.generation_id,
                "generation_ordinal": lifecycle.generation_ordinal,
                "product_manifest_ref": lifecycle.product_manifest_ref,
                "candidate_ref": product["candidate_ref"],
                "review_ref": product["review_ref"],
                "authorization_ref": product["authorization_ref"],
            }
            if not (product.get("validation") or {}).get("valid", True):
                # A terminal legacy generation with no product namespace has
                # no candidate to repair yet; retain the explicit bootstrap
                # action.  Any present/tampered candidate still fails closed
                # into the reviewed candidate boundary.
                if (
                    (product.get("validation") or {}).get("stage") == "missing"
                    and product.get("candidate") is None
                    and lifecycle.state in {"complete", "complete_with_limits"}
                ):
                    action = "build_final_product"
                    reason = "legacy terminal generation has no validated product manifest"
                else:
                    action = "build_product_candidate"
                    reason = "durable product candidate/review binding is stale or tampered"
                actions.append(
                    PlannerAction(
                        action,
                        "product_agent",
                        self.context.run_id,
                        reason,
                        priority=60,
                        metadata={**product_metadata, "validation": product.get("validation")},
                    )
                )
            elif product.get("candidate") is None:
                # Keep the historical action only for a terminal legacy
                # generation that has no product namespace at all. New and
                # resumed generations always use the explicit candidate gate.
                if lifecycle.state in {"complete", "complete_with_limits"} and product.get("manifest") is None:
                    actions.append(
                        PlannerAction(
                            "build_final_product",
                            "product_agent",
                            self.context.run_id,
                            "legacy terminal generation has no product candidate binding",
                            priority=60,
                            metadata=product_metadata,
                        )
                    )
                else:
                    actions.append(
                        PlannerAction(
                            "build_product_candidate",
                            "product_agent",
                            self.context.run_id,
                            "all requirements are integrated and no durable product candidate exists",
                            priority=60,
                            metadata=product_metadata,
                        )
                    )
            elif product.get("review") is None:
                actions.append(
                    PlannerAction(
                        "review_final_product",
                        "product_reviewer",
                        self.context.run_id,
                        "durable product candidate awaits an independent final-product review",
                        priority=61,
                        metadata={**product_metadata, "candidate_hash": product["candidate"].get("candidate_hash")},
                    )
                )
            elif product.get("authorization") is None:
                review = product.get("review") or {}
                if review.get("verdict") in {"accept", "accept_with_limits"}:
                    actions.append(
                        PlannerAction(
                            "publish_final_product",
                            "product_agent",
                            self.context.run_id,
                            "accepted product review awaits explicit publication authorization",
                            priority=62,
                            metadata={
                                **product_metadata,
                                "candidate_hash": product["candidate"].get("candidate_hash"),
                                "review_hash": review.get("review_hash"),
                                "publication_policy_required": True,
                            },
                        )
                    )
                else:
                    actions.append(
                        PlannerAction(
                            "build_product_candidate",
                            "product_agent",
                            self.context.run_id,
                            "product review requires repair before publication",
                            priority=60,
                            metadata={**product_metadata, "review_verdict": review.get("verdict")},
                        )
                    )
            else:
                report = phase_view["report"]
                report_validation = report.get("validation") or {}
                if report_validation.get("stage") in {"transaction_pending", "recovery_required"}:
                    actions.append(
                        PlannerAction(
                            "recover_final_report",
                            "reporting_agent",
                            self.context.run_id,
                            "final-report transaction intent or owned staging/backup residue requires a finalizer retry before publication",
                            priority=69,
                            metadata={
                                "preflight_ref": report["preflight_ref"],
                                "generation_id": lifecycle.generation_id,
                                "validation": report_validation,
                                "recovery_via": "RunReportFinalizer.finalize",
                            },
                        )
                    )
                elif not report_validation.get("valid", True):
                    actions.append(
                        PlannerAction(
                            "preflight_final_report",
                            "reporting_agent",
                            self.context.run_id,
                            "persisted report preflight/finalization artifacts are stale or partial",
                            priority=70,
                            metadata={"preflight_ref": report["preflight_ref"], "generation_id": lifecycle.generation_id, "validation": report.get("validation")},
                        )
                    )
                elif report.get("preflight") is None:
                    actions.append(
                        PlannerAction(
                            "preflight_final_report",
                            "reporting_agent",
                            self.context.run_id,
                            "published product exists and the final report lacks an authoritative preflight",
                            priority=70,
                            metadata={"preflight_ref": report["preflight_ref"], "generation_id": lifecycle.generation_id},
                        )
                    )
                elif report.get("final_report") is None:
                    actions.append(
                        PlannerAction(
                            "finalize_final_report",
                            "reporting_agent",
                            self.context.run_id,
                            "authoritative report preflight is present and finalization is pending",
                            priority=71,
                            metadata={"preflight_ref": report["preflight_ref"], "generation_id": lifecycle.generation_id},
                        )
                    )

        # Preserve the Planner's insertion order for equal-priority actions:
        # identity domains are ordered by the earliest requesting requirement,
        # and the AO action remains at its plan position.  Priority is the
        # only cross-lane ordering key.
        return tuple(sorted(actions, key=lambda value: value.priority))

    def record_incident(self, incident: IncidentRecord | Mapping[str, Any]) -> Mapping[str, Any]:
        """Append one canonical, idempotent Planner-visible run incident."""

        value = incident if isinstance(incident, IncidentRecord) else IncidentRecord.from_dict(incident)
        destination = self.context.resolve_run_path(PLANNER_INCIDENT_FILENAME)
        existing: list[dict[str, Any]] = []
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file():
                raise ValueError("run incident log must be a regular file")
            for line in destination.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                parsed = json.loads(line)
                if not isinstance(parsed, Mapping):
                    raise ValueError("run incident log contains an invalid record")
                existing.append(dict(parsed))
        record = {
            **value.to_dict(),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        matches = [entry for entry in existing if entry.get("incident_id") == value.incident_id]
        if matches:
            comparable = dict(matches[0])
            comparable.pop("recorded_at", None)
            if comparable != value.to_dict():
                raise ValueError("incident_id conflicts with an existing run incident")
            return matches[0]
        payload = b"".join(
            json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            for entry in (*existing, record)
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=destination.parent, prefix=f".{destination.name}.", delete=False) as stream:
                temporary_name = stream.name
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, destination)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return record

    def all_waiting_status(
        self,
        item_outcomes: Any = (),
        runtime_statuses: Any = (),
        *,
        active_resolver_count: int = 0,
        resolver_progressed: bool = False,
    ) -> str:
        """Classify a no-runnable view without poisoning item lifecycle state.

        ``waiting_on_resolution`` is returned while a resolver is active (or
        has just made progress).  ``blocked`` is reserved for the case where
        no requirement is runnable and no resolver can make progress.  A
        plan with no pending requirements is reported as ``complete``.
        """

        if isinstance(active_resolver_count, bool) or not isinstance(active_resolver_count, int) or active_resolver_count < 0:
            raise ValueError("active_resolver_count must be a non-negative integer")
        plan = self.load()
        outcomes = _outcome_map(item_outcomes)
        runtime = self._runtime_outcome_map(runtime_statuses)
        if self.next_requirement(outcomes, runtime) is not None:
            return "runnable"
        pending = False
        for requirement_id in plan.execution_order:
            outcome = outcomes.get(requirement_id)
            runtime_status = runtime.get(requirement_id)
            if self._runtime_allows_resume(runtime_status) or self._runtime_waits(runtime_status):
                pending = True
            elif outcome is None or outcome in _PENDING_STATUSES or outcome in _ITEM_WAITING_STATUSES:
                pending = True
        if not pending:
            return "complete"
        if active_resolver_count > 0 or bool(resolver_progressed):
            return _RUNTIME_WAITING_STATUS
        return "blocked"


__all__ = [
    "SUPERVISOR_PLAN_FILENAME",
    "RequirementExecutionGroup",
    "RequirementExecutionPlan",
    "RequirementPhaseSnapshot",
    "RequirementSupervisorWorkspace",
    "compact_catalog_payload",
    "inspect_committed_integration",
    "inspect_integration_fidelity",
]
