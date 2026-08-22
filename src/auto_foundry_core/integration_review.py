"""Item-local Result Integration fidelity packets and typed review results.

The review boundary is intentionally small and data-only.  A packet is built
from one accepted item and one live :class:`IntegrationSession`; it never
walks the cumulative LEM, prepared registry search, sibling item paths, or
telemetry.  Persistence is hash-bound so a crash or later record mutation
cannot be mistaken for an accepted review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .durable import _atomic_write_json


_SCHEMA_VERSION = "1"
_SHA256_HEX = frozenset("0123456789abcdef")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    return value


def _digest(value: Any) -> str:
    payload = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _content_bytes(value: Any) -> bytes:
    try:
        payload = base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        raise ValueError("fidelity packet answer_content_b64 is invalid") from None
    if not isinstance(value, str) or not value or base64.b64encode(payload).decode("ascii") != value:
        raise ValueError("fidelity packet answer_content_b64 is invalid")
    return payload


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _SHA256_HEX for char in value.lower())


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} is invalid")
    return value


@dataclass(frozen=True)
class IntegrationFidelityPacket:
    """Hash-bound, item-only material presented to the fidelity reviewer."""

    schema_version: str
    item_id: str
    session_id: str
    invocation_id: str
    accepted_content_hash: str
    accepted_manifest_hash: str
    answer_content: Any
    answer_content_b64: str
    accepted_answer_bytes_hash: str
    acceptance_envelope: Mapping[str, Any]
    manifest: Mapping[str, Any]
    records_hash: str
    records: tuple[Mapping[str, Any], ...]
    evidence: tuple[Mapping[str, Any], ...]
    candidates: tuple[Mapping[str, Any], ...]
    created_at: str
    packet_hash: str

    def unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "session_id": self.session_id,
            "invocation_id": self.invocation_id,
            "accepted_content_hash": self.accepted_content_hash,
            "accepted_manifest_hash": self.accepted_manifest_hash,
            "answer_content": _jsonable(self.answer_content),
            "answer_content_b64": self.answer_content_b64,
            "accepted_answer_bytes_hash": self.accepted_answer_bytes_hash,
            "acceptance_envelope": _jsonable(self.acceptance_envelope),
            "manifest": _jsonable(self.manifest),
            "records_hash": self.records_hash,
            "records": [_jsonable(value) for value in self.records],
            "evidence": [_jsonable(value) for value in self.evidence],
            "candidates": [_jsonable(value) for value in self.candidates],
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned()
        value["packet_hash"] = self.packet_hash
        return value

    @property
    def accepted_answer_content(self) -> Any:
        """Compatibility/readability alias for the exact accepted JSON value."""

        return self.answer_content

    @property
    def answer_content_bytes(self) -> bytes:
        return _content_bytes(self.answer_content_b64)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IntegrationFidelityPacket":
        expected = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("fidelity packet fields are invalid")
        for name in ("item_id", "session_id", "invocation_id", "created_at"):
            _safe_id(value.get(name), f"fidelity packet {name}")
        for name in (
            "accepted_content_hash", "accepted_manifest_hash", "accepted_answer_bytes_hash",
            "records_hash", "packet_hash",
        ):
            if not _sha256(value.get(name)):
                raise ValueError(f"fidelity packet {name} is invalid")
        if value.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("fidelity packet schema_version is invalid")
        records = value.get("records")
        evidence = value.get("evidence")
        candidates = value.get("candidates")
        answer_content = value.get("answer_content")
        answer_content_b64 = value.get("answer_content_b64")
        if not isinstance(answer_content_b64, str):
            raise ValueError("fidelity packet answer_content_b64 is invalid")
        answer_bytes = _content_bytes(answer_content_b64)
        if hashlib.sha256(answer_bytes).hexdigest() != value.get("accepted_answer_bytes_hash") or value.get("accepted_answer_bytes_hash") != value.get("accepted_content_hash"):
            raise ValueError("fidelity packet answer content hash does not match content")
        try:
            decoded_answer = json.loads(answer_bytes.decode("utf-8"))
            json.dumps(decoded_answer, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("fidelity packet answer content is not JSON-safe UTF-8") from exc
        if _jsonable(decoded_answer) != _jsonable(answer_content):
            raise ValueError("fidelity packet answer content does not match exact bytes")
        envelope = value.get("acceptance_envelope")
        manifest = value.get("manifest")
        if not isinstance(envelope, Mapping) or not isinstance(manifest, Mapping):
            raise ValueError("fidelity packet acceptance bindings are invalid")
        if (
            envelope.get("content_hash") != value.get("accepted_content_hash")
            or envelope.get("draft_hash") != value.get("accepted_content_hash")
            or manifest.get("content_hash") != value.get("accepted_content_hash")
            or manifest.get("manifest_hash") != value.get("accepted_manifest_hash")
            or manifest.get("envelope_hash") != hashlib.sha256(_json_bytes(envelope)).hexdigest()
            or manifest.get("manifest_hash") != hashlib.sha256(_json_bytes({key: item for key, item in manifest.items() if key != "manifest_hash"})).hexdigest()
        ):
            raise ValueError("fidelity packet acceptance bindings do not match hashes")
        if not isinstance(records, list) or not isinstance(evidence, list) or not isinstance(candidates, list):
            raise ValueError("fidelity packet collections are invalid")
        if any(not isinstance(item, Mapping) for item in (*records, *evidence, *candidates)):
            raise ValueError("fidelity packet collection item is invalid")
        records_bytes = b"".join(_json_bytes(item) for item in records)
        if hashlib.sha256(records_bytes).hexdigest() != value.get("records_hash"):
            raise ValueError("fidelity packet records hash does not match records")
        unsigned = {key: value[key] for key in value if key != "packet_hash"}
        if value.get("packet_hash") != _digest(unsigned):
            raise ValueError("fidelity packet hash does not match content")
        return cls(
            schema_version=str(value["schema_version"]),
            item_id=str(value["item_id"]),
            session_id=str(value["session_id"]),
            invocation_id=str(value["invocation_id"]),
            accepted_content_hash=str(value["accepted_content_hash"]),
            accepted_manifest_hash=str(value["accepted_manifest_hash"]),
            answer_content=decoded_answer,
            answer_content_b64=answer_content_b64,
            accepted_answer_bytes_hash=str(value["accepted_answer_bytes_hash"]),
            acceptance_envelope=dict(envelope),
            manifest=dict(manifest),
            records=tuple(dict(item) for item in records),
            evidence=tuple(dict(item) for item in evidence),
            candidates=tuple(dict(item) for item in candidates),
            records_hash=str(value["records_hash"]),
            created_at=str(value["created_at"]),
            packet_hash=str(value["packet_hash"]),
        )


@dataclass(frozen=True)
class FidelityFinding:
    finding_id: str
    message: str
    record_ids: tuple[str, ...] = ()
    parts: tuple[str, ...] = ()
    dependency_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "message": self.message,
            "record_ids": list(self.record_ids),
            "parts": list(self.parts),
            "dependency_ids": list(self.dependency_ids),
        }

    @classmethod
    def from_value(cls, value: Any, index: int) -> "FidelityFinding":
        if isinstance(value, str):
            return cls(f"finding-{index + 1}", value)
        if not isinstance(value, Mapping):
            raise ValueError("fidelity finding must be a mapping or string")
        message = str(value.get("message", value.get("finding", ""))).strip()
        if not message:
            raise ValueError("fidelity finding message is required")
        raw_record_ids = value.get("record_ids", value.get("affected_record_ids", value.get("record_id", ())))
        if isinstance(raw_record_ids, str):
            raw_record_ids = (raw_record_ids,)
        raw_parts = value.get("parts", value.get("affected_parts", ()))
        if isinstance(raw_parts, str):
            raw_parts = (raw_parts,)
        raw_dependencies = value.get("dependency_ids", value.get("dependencies", ()))
        if isinstance(raw_dependencies, str):
            raw_dependencies = (raw_dependencies,)
        record_ids = tuple(_safe_id(item, "fidelity finding record_id") for item in (raw_record_ids or ()))
        dependency_ids = tuple(_safe_id(item, "fidelity finding dependency_id") for item in (raw_dependencies or ()))
        parts = tuple(str(item).strip() for item in (raw_parts or ()) if str(item).strip())
        finding_id = _safe_id(value.get("finding_id", f"finding-{index + 1}"), "fidelity finding finding_id")
        return cls(finding_id, message, record_ids, parts, dependency_ids)


@dataclass(frozen=True)
class FidelityResult:
    """Typed durable result for one initial or targeted fidelity review."""

    schema_version: str
    item_id: str
    session_id: str
    invocation_id: str
    review_kind: str
    verdict: str
    packet_hash: str
    records_hash: str
    findings: tuple[FidelityFinding, ...]
    affected_record_ids: tuple[str, ...]
    dependency_ids: tuple[str, ...]
    baseline_record_hashes: Mapping[str, str]
    checked_record_ids: tuple[str, ...]
    created_at: str
    result_hash: str

    def unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "session_id": self.session_id,
            "invocation_id": self.invocation_id,
            "review_kind": self.review_kind,
            "verdict": self.verdict,
            "packet_hash": self.packet_hash,
            "records_hash": self.records_hash,
            "findings": [finding.to_dict() for finding in self.findings],
            "affected_record_ids": list(self.affected_record_ids),
            "dependency_ids": list(self.dependency_ids),
            "baseline_record_hashes": dict(self.baseline_record_hashes),
            "checked_record_ids": list(self.checked_record_ids),
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned()
        value["result_hash"] = self.result_hash
        return value

    @property
    def accepted(self) -> bool:
        return self.verdict == "accept"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FidelityResult":
        expected = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("fidelity result fields are invalid")
        if value.get("schema_version") != _SCHEMA_VERSION or value.get("review_kind") not in {"initial", "targeted"}:
            raise ValueError("fidelity result identity is invalid")
        if value.get("verdict") not in {"accept", "repair_once", "unavailable", "fail"}:
            raise ValueError("fidelity result verdict is invalid")
        for name in ("item_id", "session_id", "invocation_id", "created_at"):
            _safe_id(value.get(name), f"fidelity result {name}")
        for name in ("packet_hash", "records_hash", "result_hash"):
            if not _sha256(value.get(name)):
                raise ValueError(f"fidelity result {name} is invalid")
        findings = value.get("findings")
        if not isinstance(findings, list):
            raise ValueError("fidelity result findings are invalid")
        parsed_findings = tuple(FidelityFinding.from_value(item, index) for index, item in enumerate(findings))
        for name in ("affected_record_ids", "dependency_ids", "checked_record_ids"):
            raw = value.get(name)
            if not isinstance(raw, list):
                raise ValueError(f"fidelity result {name} is invalid")
            for item in raw:
                _safe_id(item, f"fidelity result {name}")
            if len(raw) != len(set(raw)):
                raise ValueError(f"fidelity result {name} contain duplicates")
        baseline = value.get("baseline_record_hashes")
        if not isinstance(baseline, Mapping) or any(not _sha256(item) for item in baseline.values()):
            raise ValueError("fidelity result baseline hashes are invalid")
        for record_id in baseline:
            _safe_id(record_id, "fidelity result baseline record_id")
        unsigned = {key: value[key] for key in value if key != "result_hash"}
        if value.get("result_hash") != _digest(unsigned):
            raise ValueError("fidelity result hash does not match content")
        return cls(
            schema_version=str(value["schema_version"]),
            item_id=str(value["item_id"]),
            session_id=str(value["session_id"]),
            invocation_id=str(value["invocation_id"]),
            review_kind=str(value["review_kind"]),
            verdict=str(value["verdict"]),
            packet_hash=str(value["packet_hash"]),
            records_hash=str(value["records_hash"]),
            findings=parsed_findings,
            affected_record_ids=tuple(str(item) for item in value["affected_record_ids"]),
            dependency_ids=tuple(str(item) for item in value["dependency_ids"]),
            baseline_record_hashes={str(key): str(item) for key, item in baseline.items()},
            checked_record_ids=tuple(str(item) for item in value["checked_record_ids"]),
            created_at=str(value["created_at"]),
            result_hash=str(value["result_hash"]),
        )


@dataclass(frozen=True)
class FidelityRepairAuthorization:
    """Immutable authorization for one bounded, item-local repair.

    The authorization is written beside the initial ``repair_once`` result and
    is never overwritten by correction progress or the eventual targeted
    recheck.  This gives correction retries a stable binding even while the
    live staging record hash changes after each authorized correction.
    """

    schema_version: str
    item_id: str
    session_id: str
    invocation_id: str
    initial_packet_hash: str
    initial_result_hash: str
    baseline_record_hashes: Mapping[str, str]
    affected_record_ids: tuple[str, ...]
    dependency_ids: tuple[str, ...]
    created_at: str
    authorization_hash: str

    def unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "session_id": self.session_id,
            "invocation_id": self.invocation_id,
            "initial_packet_hash": self.initial_packet_hash,
            "initial_result_hash": self.initial_result_hash,
            "baseline_record_hashes": dict(self.baseline_record_hashes),
            "affected_record_ids": list(self.affected_record_ids),
            "dependency_ids": list(self.dependency_ids),
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned()
        value["authorization_hash"] = self.authorization_hash
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FidelityRepairAuthorization":
        expected = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("fidelity repair authorization fields are invalid")
        if value.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("fidelity repair authorization schema_version is invalid")
        for name in ("item_id", "session_id", "invocation_id", "created_at"):
            _safe_id(value.get(name), f"fidelity repair authorization {name}")
        for name in ("initial_packet_hash", "initial_result_hash", "authorization_hash"):
            if not _sha256(value.get(name)):
                raise ValueError(f"fidelity repair authorization {name} is invalid")
        baseline = value.get("baseline_record_hashes")
        if not isinstance(baseline, Mapping):
            raise ValueError("fidelity repair authorization baseline hashes are invalid")
        for record_id, digest in baseline.items():
            _safe_id(record_id, "fidelity repair authorization record_id")
            if not _sha256(digest):
                raise ValueError("fidelity repair authorization baseline hash is invalid")
        parsed_ids: dict[str, tuple[str, ...]] = {}
        for name in ("affected_record_ids", "dependency_ids"):
            raw = value.get(name)
            if not isinstance(raw, list):
                raise ValueError(f"fidelity repair authorization {name} are invalid")
            ids = tuple(_safe_id(item, f"fidelity repair authorization {name[:-4]}id") for item in raw)
            if len(ids) != len(set(ids)):
                raise ValueError(f"fidelity repair authorization {name} contain duplicates")
            if any(item not in baseline for item in ids):
                raise ValueError(f"fidelity repair authorization {name} reference unknown records")
            parsed_ids[name] = ids
        unsigned = {key: value[key] for key in value if key != "authorization_hash"}
        if value.get("authorization_hash") != _digest(unsigned):
            raise ValueError("fidelity repair authorization hash does not match content")
        return cls(
            schema_version=str(value["schema_version"]),
            item_id=str(value["item_id"]),
            session_id=str(value["session_id"]),
            invocation_id=str(value["invocation_id"]),
            initial_packet_hash=str(value["initial_packet_hash"]),
            initial_result_hash=str(value["initial_result_hash"]),
            baseline_record_hashes={str(key): str(item) for key, item in baseline.items()},
            affected_record_ids=parsed_ids["affected_record_ids"],
            dependency_ids=parsed_ids["dependency_ids"],
            created_at=str(value["created_at"]),
            authorization_hash=str(value["authorization_hash"]),
        )


@dataclass(frozen=True)
class FidelityRepairProgress:
    """Mutable correction progress bound to one immutable authorization."""

    schema_version: str
    item_id: str
    session_id: str
    invocation_id: str
    authorization_hash: str
    corrected_record_hashes: Mapping[str, str]
    removed_record_ids: tuple[str, ...]
    current_records_hash: str
    current_packet_hash: str | None
    progress_hash: str

    def unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "session_id": self.session_id,
            "invocation_id": self.invocation_id,
            "authorization_hash": self.authorization_hash,
            "corrected_record_hashes": dict(self.corrected_record_hashes),
            "removed_record_ids": list(self.removed_record_ids),
            "current_records_hash": self.current_records_hash,
            "current_packet_hash": self.current_packet_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned()
        value["progress_hash"] = self.progress_hash
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FidelityRepairProgress":
        expected = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        # The removal field was added while the current repair artifacts were
        # still active.  Accepting its absence as an empty set keeps those
        # artifacts loadable without introducing a deprecated schema path;
        # every subsequent durable write emits the explicit field.
        required = expected - {"removed_record_ids"}
        if not isinstance(value, Mapping) or set(value) not in (required, expected):
            raise ValueError("fidelity repair progress fields are invalid")
        if value.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("fidelity repair progress schema_version is invalid")
        for name in ("item_id", "session_id", "invocation_id"):
            _safe_id(value.get(name), f"fidelity repair progress {name}")
        for name in ("authorization_hash", "current_records_hash", "progress_hash"):
            if not _sha256(value.get(name)):
                raise ValueError(f"fidelity repair progress {name} is invalid")
        if value.get("current_packet_hash") is not None and not _sha256(value.get("current_packet_hash")):
            raise ValueError("fidelity repair progress current_packet_hash is invalid")
        raw_hashes = value.get("corrected_record_hashes")
        if not isinstance(raw_hashes, Mapping):
            raise ValueError("fidelity repair progress corrected hashes are invalid")
        for record_id, digest in raw_hashes.items():
            _safe_id(record_id, "fidelity repair progress record_id")
            if not _sha256(digest):
                raise ValueError("fidelity repair progress record hash is invalid")
        raw_removed = value.get("removed_record_ids", [])
        if not isinstance(raw_removed, list):
            raise ValueError("fidelity repair progress removed record IDs are invalid")
        removed = tuple(_safe_id(item, "fidelity repair progress removed_record_id") for item in raw_removed)
        if len(removed) != len(set(removed)):
            raise ValueError("fidelity repair progress removed record IDs contain duplicates")
        unsigned = {key: value[key] for key in value if key != "progress_hash"}
        if value.get("progress_hash") != _digest(unsigned):
            raise ValueError("fidelity repair progress hash does not match content")
        return cls(
            schema_version=str(value["schema_version"]),
            item_id=str(value["item_id"]),
            session_id=str(value["session_id"]),
            invocation_id=str(value["invocation_id"]),
            authorization_hash=str(value["authorization_hash"]),
            corrected_record_hashes={str(key): str(item) for key, item in raw_hashes.items()},
            removed_record_ids=removed,
            current_records_hash=str(value["current_records_hash"]),
            current_packet_hash=(
                str(value["current_packet_hash"])
                if value["current_packet_hash"] is not None
                else None
            ),
            progress_hash=str(value["progress_hash"]),
        )


def write_packet(path: Path, packet: IntegrationFidelityPacket) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, packet.to_dict())


def write_result(path: Path, result: FidelityResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, result.to_dict())


def write_repair_authorization(path: Path, authorization: FidelityRepairAuthorization) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, authorization.to_dict())


def write_repair_progress(path: Path, progress: FidelityRepairProgress) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, progress.to_dict())


FidelityPacket = IntegrationFidelityPacket
FidelityReviewResult = FidelityResult

__all__ = [
    "FidelityFinding", "FidelityPacket", "FidelityResult", "FidelityReviewResult",
    "FidelityRepairAuthorization", "FidelityRepairProgress",
    "IntegrationFidelityPacket", "write_packet", "write_result",
    "write_repair_authorization", "write_repair_progress",
]
