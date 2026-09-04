"""Durable product candidate, review, and publication authorization.

The dashboard assembler owns rendering and its existing public delta boundary
owns publication.  This module deliberately does neither.  It records a
generation-scoped, hash-bound candidate and the independent review and policy
authorization that a caller may later pass to that publication boundary.

All writes are atomic and idempotent.  A retry with the same canonical bytes
returns the existing record; a conflicting retry fails closed instead of
overwriting a candidate, review, or authorization.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Iterable, Mapping

from .lifecycle import RunLifecycle
from .workspace import RunContext


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GENERATION = re.compile(r"^G-[0-9]{4,}$")
_CANDIDATE_FILENAME = "product_candidate.json"
_REVIEW_FILENAME = "product_review.json"
_AUTHORIZATION_FILENAME = "publish_authorization.json"
_LOCK_FILENAME = ".product_review.lock"
_REVISION_ROOT = "product_revisions"
_REVISION_POINTER_FILENAME = "product_revision.json"
_REVISION_STATE_FILENAME = "revision.json"
_REVISION_ID = re.compile(r"^rev-[0-9]{4,}$")
_OUTPUT_NAMES = ("manifest", "fixture", "chart_map", "chart_registry", "blueprint", "site", "receipt")
_OUTPUT_FILENAMES = {
    "manifest": "product_manifest.json",
    "fixture": "dashboard_fixture_v4.json",
    "chart_map": "dashboard_chart_map_v4.json",
    "chart_registry": "dashboard_chart_registry_v4.json",
    "blueprint": "dashboard_blueprint_v2.json",
    "site": "site",
    "receipt": "build_receipt.json",
}


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, Path):
        return str(value)
    return value


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def canonical_hash(value: Any) -> str:
    """Return the SHA-256 of one canonical JSON value."""

    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{label} must be a non-empty mapping")
    return dict(_jsonable(value))


def _validate_binding_hashes(value: Mapping[str, Any], label: str) -> None:
    """Validate explicit lineage/plan digest fields without inventing refs."""

    for key, item in value.items():
        if str(key).endswith("_hash") or str(key) in {"sha256", "hash"}:
            _sha(item, f"{label}.{key}")


def _validate_lineage_binding(value: Mapping[str, Any], label: str) -> None:
    """Validate the explicit, canonical lineage fields.

    Product candidates are consumed by a later publication boundary, so a
    generic ``ref``/``hash`` pair is not an authorization binding.  Keep the
    wire contract deliberately narrow: parent lineage has three named
    fields, while the planner binding has exactly its named reference and
    digest.  The only exception is the legacy root generation, which carries
    explicit ``root_generation=true`` null parent fields.
    """

    if label == "parent_lineage":
        required = {"parent_generation_id", "parent_manifest_ref", "parent_manifest_hash"}
        marker = value.get("root_generation") is True
        if marker:
            if not required.issubset(value):
                raise ValueError("parent_lineage root generation requires explicit null parent fields")
            if any(value.get(key) is not None for key in required):
                raise ValueError("parent_lineage root generation parent fields must be null")
        elif any(
            key not in value
            or not isinstance(value.get(key), str)
            or not str(value.get(key)).strip()
            for key in required
        ):
            raise ValueError("parent_lineage requires parent_generation_id, parent_manifest_ref, and parent_manifest_hash")
        if "parent_manifest_hash" in value and value.get("parent_manifest_hash") is not None:
            _sha(value.get("parent_manifest_hash"), "parent_lineage.parent_manifest_hash")
        unknown = set(value) - required - {"root_generation"}
        if unknown:
            raise ValueError("parent_lineage has unsupported fields: " + ", ".join(sorted(map(str, unknown))))
        return
    if label == "plan_binding":
        unknown = set(value) - {"plan_ref", "plan_hash"}
        if unknown:
            raise ValueError("plan_binding has unsupported fields: " + ", ".join(sorted(map(str, unknown))))
        if not isinstance(value.get("plan_ref"), str) or not value.get("plan_ref", "").strip():
            raise ValueError("plan_binding requires plan_ref")
        _sha(value.get("plan_hash"), "plan_binding.plan_hash")
        return
    refs = [key for key, item in value.items() if (str(key).endswith("_ref") or str(key) in {"ref", "path"}) and isinstance(item, str) and item.strip()]
    hashes = [key for key in value if str(key).endswith("_hash") or str(key) in {"hash", "sha256"}]
    if not refs or not hashes:
        raise ValueError(f"{label} requires a non-empty ref and SHA-256 binding")
    _validate_binding_hashes(value, label)


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    payload = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_copy(source: Path, destination: Path) -> None:
    """Copy immutable evidence bytes into a revision under the store lock."""

    if source.is_symlink() or not source.is_file():
        raise ValueError(f"revision source is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.tmp-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as source_stream, os.fdopen(descriptor, "wb") as destination_stream:
            while True:
                chunk = source_stream.read(1024 * 1024)
                if not chunk:
                    break
                destination_stream.write(chunk)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    """Durably publish a directory entry change."""

    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must contain an object")
    return dict(value)


def _safe_generation_id(value: Any) -> str:
    generation_id = _text(value, "generation_id")
    if not _GENERATION.fullmatch(generation_id):
        raise ValueError("generation_id must use the G-XXXX form")
    return generation_id


def _relative_ref(context: RunContext, path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        relative = resolved.relative_to(context.run_root.resolve())
    except ValueError as exc:
        raise ValueError("artifact path escapes the run root") from exc
    return str(Path(relative))


def _artifact_digest(path: Path) -> tuple[str, dict[str, str]]:
    """Hash one file or a deterministic file tree."""

    if path.is_symlink():
        raise ValueError(f"artifact cannot be a symlink: {path}")
    if path.is_file():
        return "file", {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
    if not path.is_dir():
        raise FileNotFoundError(path)
    files: dict[str, str] = {}
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise ValueError(f"artifact tree cannot contain symlink: {child}")
        if child.is_file():
            files[str(child.relative_to(path))] = hashlib.sha256(child.read_bytes()).hexdigest()
    return "tree", files


def hash_artifact(path: Path | str) -> tuple[str, str]:
    """Return ``(kind, digest)`` for a file or directory artifact."""

    candidate = Path(path)
    kind, files = _artifact_digest(candidate)
    if kind == "file":
        return kind, next(iter(files.values()))
    return kind, canonical_hash({"files": files})


def _artifact_binding(context: RunContext, value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        raw = dict(value)
        ref = raw.get("ref", raw.get("path"))
        supplied_kind = raw.get("kind")
        supplied_sha = raw.get("sha256", raw.get("hash"))
    else:
        ref, supplied_kind, supplied_sha = value, None, None
    ref = _text(ref, f"{label}.ref")
    path = context.resolve_run_path(ref)
    if not path.exists() or path.is_symlink():
        raise FileNotFoundError(path)
    kind, digest = hash_artifact(path)
    if supplied_kind is not None and str(supplied_kind) != kind:
        raise ValueError(f"{label} kind does not match the artifact")
    if supplied_sha is not None and _sha(str(supplied_sha), f"{label}.sha256") != digest:
        raise ValueError(f"{label} hash does not match the artifact")
    binding: dict[str, Any] = {"ref": _relative_ref(context, path), "kind": kind, "sha256": digest}
    if kind == "tree":
        _kind, files = _artifact_digest(path)
        binding["files"] = files
    return binding


@dataclass(frozen=True)
class ProductCandidate:
    run_id: str
    generation_id: str
    product_owner: str
    parent_lineage: Mapping[str, Any]
    plan_binding: Mapping[str, Any]
    publication_policy_hash: str
    artifact_bindings: Mapping[str, Mapping[str, Any]]
    created_at: str = ""
    schema_version: int = 1
    kind: str = "product_candidate"
    candidate_hash: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("product candidate schema_version must be 1")
        if self.kind != "product_candidate":
            raise ValueError("product candidate kind is invalid")
        object.__setattr__(self, "run_id", _text(self.run_id, "run_id"))
        object.__setattr__(self, "generation_id", _safe_generation_id(self.generation_id))
        object.__setattr__(self, "product_owner", _text(self.product_owner, "product_owner"))
        parent_lineage = _mapping(self.parent_lineage, "parent_lineage")
        plan_binding = _mapping(self.plan_binding, "plan_binding")
        _validate_lineage_binding(parent_lineage, "parent_lineage")
        _validate_lineage_binding(plan_binding, "plan_binding")
        if not isinstance(plan_binding.get("plan_ref"), str) or not plan_binding.get("plan_ref", "").strip():
            raise ValueError("plan_binding requires plan_ref")
        _sha(plan_binding.get("plan_hash"), "plan_binding.plan_hash")
        object.__setattr__(self, "parent_lineage", parent_lineage)
        object.__setattr__(self, "plan_binding", plan_binding)
        object.__setattr__(self, "publication_policy_hash", _sha(self.publication_policy_hash, "publication_policy_hash"))
        bindings = _mapping(self.artifact_bindings, "artifact_bindings")
        if set(bindings) != set(_OUTPUT_NAMES):
            raise ValueError(
                "artifact_bindings must contain manifest, fixture, chart_map, chart_registry, blueprint, site, and receipt"
            )
        object.__setattr__(self, "artifact_bindings", {name: _mapping(bindings[name], f"artifact_bindings.{name}") for name in _OUTPUT_NAMES})
        if self.created_at:
            object.__setattr__(self, "created_at", _text(self.created_at, "created_at"))
        if self.candidate_hash is not None:
            _sha(self.candidate_hash, "candidate_hash")

    def unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "run_id": self.run_id,
            "generation_id": self.generation_id,
            "product_owner": self.product_owner,
            "parent_lineage": dict(self.parent_lineage),
            "plan_binding": dict(self.plan_binding),
            "publication_policy_hash": self.publication_policy_hash,
            "artifact_bindings": {key: dict(value) for key, value in self.artifact_bindings.items()},
            "created_at": self.created_at,
        }

    @property
    def computed_hash(self) -> str:
        return canonical_hash(self.unsigned())

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned()
        value["candidate_hash"] = self.candidate_hash or self.computed_hash
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProductCandidate":
        if not isinstance(value, Mapping):
            raise TypeError("product candidate must be a mapping")
        expected_keys = {
            "schema_version",
            "kind",
            "run_id",
            "generation_id",
            "product_owner",
            "parent_lineage",
            "plan_binding",
            "publication_policy_hash",
            "artifact_bindings",
            "created_at",
            "candidate_hash",
        }
        actual_keys = set(value)
        missing = expected_keys - actual_keys
        extra = actual_keys - expected_keys
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append("missing fields: " + ", ".join(sorted(map(str, missing))))
            if extra:
                details.append("unsupported fields: " + ", ".join(sorted(map(str, extra))))
            raise ValueError("product candidate schema is not exact (" + "; ".join(details) + ")")
        candidate = cls(
            run_id=value["run_id"],
            generation_id=value["generation_id"],
            product_owner=value["product_owner"],
            parent_lineage=value["parent_lineage"],
            plan_binding=value["plan_binding"],
            publication_policy_hash=value["publication_policy_hash"],
            artifact_bindings=value["artifact_bindings"],
            created_at=value["created_at"],
            schema_version=value["schema_version"],
            kind=value["kind"],
            candidate_hash=value["candidate_hash"],
        )
        if candidate.candidate_hash is not None and candidate.candidate_hash != candidate.computed_hash:
            raise ValueError("product candidate hash does not match content")
        return candidate


@dataclass(frozen=True)
class ProductReview:
    run_id: str
    generation_id: str
    candidate_ref: str
    candidate_hash: str
    product_owner: str
    reviewer_ref: str
    verdict: str
    findings: tuple[Mapping[str, Any], ...] = ()
    reviewed_at: str = ""
    schema_version: int = 1
    kind: str = "product_review"
    review_hash: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.kind != "product_review":
            raise ValueError("product review schema or kind is invalid")
        object.__setattr__(self, "run_id", _text(self.run_id, "run_id"))
        object.__setattr__(self, "generation_id", _safe_generation_id(self.generation_id))
        object.__setattr__(self, "candidate_ref", _text(self.candidate_ref, "candidate_ref"))
        object.__setattr__(self, "candidate_hash", _sha(self.candidate_hash, "candidate_hash"))
        object.__setattr__(self, "product_owner", _text(self.product_owner, "product_owner"))
        object.__setattr__(self, "reviewer_ref", _text(self.reviewer_ref, "reviewer_ref"))
        if self.reviewer_ref == self.product_owner:
            raise ValueError("product reviewer must be independent from product owner")
        verdict = _text(self.verdict, "verdict").lower()
        if verdict not in {"accept", "accept_with_limits", "repair_once", "blocked_rethink", "block"}:
            raise ValueError("product review verdict is invalid")
        object.__setattr__(self, "verdict", verdict)
        if isinstance(self.findings, (str, bytes)):
            raise TypeError("product review findings must be a sequence")
        findings = tuple(_mapping(finding, "finding") for finding in self.findings)
        object.__setattr__(self, "findings", findings)
        if self.reviewed_at:
            object.__setattr__(self, "reviewed_at", _text(self.reviewed_at, "reviewed_at"))
        if self.review_hash is not None:
            _sha(self.review_hash, "review_hash")

    def unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "run_id": self.run_id,
            "generation_id": self.generation_id,
            "candidate_ref": self.candidate_ref,
            "candidate_hash": self.candidate_hash,
            "product_owner": self.product_owner,
            "reviewer_ref": self.reviewer_ref,
            "verdict": self.verdict,
            "findings": [dict(item) for item in self.findings],
            "reviewed_at": self.reviewed_at,
        }

    @property
    def computed_hash(self) -> str:
        return canonical_hash(self.unsigned())

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned()
        value["review_hash"] = self.review_hash or self.computed_hash
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProductReview":
        review = cls(
            run_id=value.get("run_id"), generation_id=value.get("generation_id"),
            candidate_ref=value.get("candidate_ref"), candidate_hash=value.get("candidate_hash"),
            product_owner=value.get("product_owner"), reviewer_ref=value.get("reviewer_ref"),
            verdict=value.get("verdict"), findings=tuple(value.get("findings", ())),
            reviewed_at=value.get("reviewed_at", ""), schema_version=value.get("schema_version", 1),
            kind=value.get("kind", "product_review"), review_hash=value.get("review_hash"),
        )
        if review.review_hash is not None and review.review_hash != review.computed_hash:
            raise ValueError("product review hash does not match content")
        return review


@dataclass(frozen=True)
class PublishAuthorization:
    run_id: str
    generation_id: str
    candidate_ref: str
    candidate_hash: str
    review_ref: str
    review_hash: str
    publication_policy_hash: str
    publisher_ref: str
    authorized_at: str = ""
    schema_version: int = 1
    kind: str = "product_publish_authorization"
    authorization_hash: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.kind != "product_publish_authorization":
            raise ValueError("publish authorization schema or kind is invalid")
        object.__setattr__(self, "run_id", _text(self.run_id, "run_id"))
        object.__setattr__(self, "generation_id", _safe_generation_id(self.generation_id))
        object.__setattr__(self, "candidate_ref", _text(self.candidate_ref, "candidate_ref"))
        object.__setattr__(self, "candidate_hash", _sha(self.candidate_hash, "candidate_hash"))
        object.__setattr__(self, "review_ref", _text(self.review_ref, "review_ref"))
        object.__setattr__(self, "review_hash", _sha(self.review_hash, "review_hash"))
        object.__setattr__(self, "publication_policy_hash", _sha(self.publication_policy_hash, "publication_policy_hash"))
        object.__setattr__(self, "publisher_ref", _text(self.publisher_ref, "publisher_ref"))
        if self.authorized_at:
            object.__setattr__(self, "authorized_at", _text(self.authorized_at, "authorized_at"))
        if self.authorization_hash is not None:
            _sha(self.authorization_hash, "authorization_hash")

    def unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "run_id": self.run_id,
            "generation_id": self.generation_id,
            "candidate_ref": self.candidate_ref,
            "candidate_hash": self.candidate_hash,
            "review_ref": self.review_ref,
            "review_hash": self.review_hash,
            "publication_policy_hash": self.publication_policy_hash,
            "publisher_ref": self.publisher_ref,
            "authorized_at": self.authorized_at,
        }

    @property
    def computed_hash(self) -> str:
        return canonical_hash(self.unsigned())

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned()
        value["authorization_hash"] = self.authorization_hash or self.computed_hash
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PublishAuthorization":
        authorization = cls(
            run_id=value.get("run_id"), generation_id=value.get("generation_id"),
            candidate_ref=value.get("candidate_ref"), candidate_hash=value.get("candidate_hash"),
            review_ref=value.get("review_ref"), review_hash=value.get("review_hash"),
            publication_policy_hash=value.get("publication_policy_hash", value.get("policy_hash")),
            publisher_ref=value.get("publisher_ref"), authorized_at=value.get("authorized_at", ""),
            schema_version=value.get("schema_version", 1), kind=value.get("kind", "product_publish_authorization"),
            authorization_hash=value.get("authorization_hash"),
        )
        if authorization.authorization_hash is not None and authorization.authorization_hash != authorization.computed_hash:
            raise ValueError("publish authorization hash does not match content")
        return authorization


@dataclass(frozen=True)
class ProductRevision:
    """Revision metadata whose candidate/review evidence is immutable."""

    run_id: str
    generation_id: str
    revision_id: str
    status: str
    request_id: str
    input_fingerprint: str | None = None
    implementation_identity: str | None = None
    prior_revision_id: str | None = None
    prior_candidate_hash: str | None = None
    prior_review_hash: str | None = None
    candidate_ref: str | None = None
    candidate_hash: str | None = None
    review_ref: str | None = None
    review_hash: str | None = None
    created_at: str = ""
    updated_at: str = ""
    schema_version: int = 1
    kind: str = "product_revision"
    revision_hash: str | None = None

    # ``activation_pending`` is a durable, recoverable state between the
    # target revision's review and the active-pointer CAS.  It is deliberately
    # not a pointer status: the pointer remains ``accepted`` only after its
    # target has passed the same hash-bound candidate/review checks, and the
    # loader understands the transitional pair during replay.
    _STATUSES = frozenset({"pending", "candidate", "reviewed", "activation_pending", "accepted", "failed", "superseded"})

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("product revision schema_version must be 1")
        if self.kind != "product_revision":
            raise ValueError("product revision kind is invalid")
        object.__setattr__(self, "run_id", _text(self.run_id, "run_id"))
        object.__setattr__(self, "generation_id", _safe_generation_id(self.generation_id))
        revision_id = _text(self.revision_id, "revision_id")
        if _REVISION_ID.fullmatch(revision_id) is None:
            raise ValueError("revision_id must use the rev-XXXX form")
        object.__setattr__(self, "revision_id", revision_id)
        status = _text(self.status, "status").lower()
        if status not in self._STATUSES:
            raise ValueError("product revision status is invalid")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "request_id", _text(self.request_id, "request_id"))
        for field_name in (
            "input_fingerprint", "implementation_identity", "prior_candidate_hash", "prior_review_hash",
            "candidate_hash", "review_hash",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _sha(value, field_name)
        for field_name in ("candidate_ref", "review_ref", "prior_revision_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _text(value, field_name))
        if (self.candidate_ref is None) != (self.candidate_hash is None):
            raise ValueError("product revision candidate ref/hash must be paired")
        if (self.review_ref is None) != (self.review_hash is None):
            raise ValueError("product revision review ref/hash must be paired")
        if self.created_at:
            object.__setattr__(self, "created_at", _text(self.created_at, "created_at"))
        if self.updated_at:
            object.__setattr__(self, "updated_at", _text(self.updated_at, "updated_at"))
        if self.revision_hash is not None:
            _sha(self.revision_hash, "revision_hash")

    def unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "run_id": self.run_id,
            "generation_id": self.generation_id,
            "revision_id": self.revision_id,
            "status": self.status,
            "request_id": self.request_id,
            "input_fingerprint": self.input_fingerprint,
            "implementation_identity": self.implementation_identity,
            "prior_revision_id": self.prior_revision_id,
            "prior_candidate_hash": self.prior_candidate_hash,
            "prior_review_hash": self.prior_review_hash,
            "candidate_ref": self.candidate_ref,
            "candidate_hash": self.candidate_hash,
            "review_ref": self.review_ref,
            "review_hash": self.review_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @property
    def computed_hash(self) -> str:
        return canonical_hash(self.unsigned())

    @property
    def origin(self) -> str:
        """Return the immutable provenance class of this revision."""

        return "adopted_initial" if self.request_id == "legacy_root_adoption" else "product_regeneration"

    @property
    def output_root_ref(self) -> str | None:
        """Return the target output namespace for non-legacy revisions."""

        if self.origin == "adopted_initial":
            return None
        return f"products/generations/{self.generation_id}/{_REVISION_ROOT}/{self.revision_id}/artifacts"

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned()
        value["revision_hash"] = self.revision_hash or self.computed_hash
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProductRevision":
        if not isinstance(value, Mapping):
            raise TypeError("product revision must be a mapping")
        expected = {
            "schema_version", "kind", "run_id", "generation_id", "revision_id", "status", "request_id",
            "input_fingerprint", "implementation_identity", "prior_revision_id", "prior_candidate_hash",
            "prior_review_hash", "candidate_ref", "candidate_hash", "review_ref", "review_hash", "created_at",
            "updated_at", "revision_hash",
        }
        missing = expected - set(value)
        extra = set(value) - expected
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append("missing fields: " + ", ".join(sorted(map(str, missing))))
            if extra:
                details.append("unsupported fields: " + ", ".join(sorted(map(str, extra))))
            raise ValueError("product revision schema is not exact (" + "; ".join(details) + ")")
        revision = cls(**{field: value[field] for field in expected})
        if revision.revision_hash is not None and revision.revision_hash != revision.computed_hash:
            raise ValueError("product revision hash does not match content")
        return revision


@dataclass(frozen=True)
class ProductRevisionPointer:
    """The generation's current product revision pointer.

    ``accepted`` is the publishable authoritative revision.  ``reviewed`` is
    retained only while an explicit repair/regeneration is pending so the
    prior candidate/review remains the auditable current evidence.
    """

    run_id: str
    generation_id: str
    revision_id: str
    status: str
    revision_ref: str
    revision_hash: str
    candidate_ref: str
    candidate_hash: str
    review_ref: str
    review_hash: str
    prior_revision_id: str | None = None
    prior_candidate_hash: str | None = None
    prior_review_hash: str | None = None
    updated_at: str = ""
    schema_version: int = 1
    kind: str = "product_revision_pointer"
    pointer_hash: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1 or self.kind != "product_revision_pointer":
            raise ValueError("product revision pointer schema or kind is invalid")
        object.__setattr__(self, "run_id", _text(self.run_id, "run_id"))
        object.__setattr__(self, "generation_id", _safe_generation_id(self.generation_id))
        revision_id = _text(self.revision_id, "revision_id")
        if _REVISION_ID.fullmatch(revision_id) is None:
            raise ValueError("revision_id must use the rev-XXXX form")
        object.__setattr__(self, "revision_id", revision_id)
        status = _text(self.status, "status").lower()
        if status not in {"accepted", "reviewed"}:
            raise ValueError("product revision pointer status is invalid")
        object.__setattr__(self, "status", status)
        for field_name in ("revision_hash", "candidate_hash", "review_hash", "prior_candidate_hash", "prior_review_hash"):
            value = getattr(self, field_name)
            if value is not None:
                _sha(value, field_name)
        for field_name in ("revision_ref", "candidate_ref", "review_ref"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        if self.prior_revision_id is not None:
            object.__setattr__(self, "prior_revision_id", _text(self.prior_revision_id, "prior_revision_id"))
        if self.updated_at:
            object.__setattr__(self, "updated_at", _text(self.updated_at, "updated_at"))
        if self.pointer_hash is not None:
            _sha(self.pointer_hash, "pointer_hash")

    def unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "run_id": self.run_id,
            "generation_id": self.generation_id,
            "revision_id": self.revision_id,
            "status": self.status,
            "revision_ref": self.revision_ref,
            "revision_hash": self.revision_hash,
            "candidate_ref": self.candidate_ref,
            "candidate_hash": self.candidate_hash,
            "review_ref": self.review_ref,
            "review_hash": self.review_hash,
            "prior_revision_id": self.prior_revision_id,
            "prior_candidate_hash": self.prior_candidate_hash,
            "prior_review_hash": self.prior_review_hash,
            "updated_at": self.updated_at,
        }

    @property
    def computed_hash(self) -> str:
        return canonical_hash(self.unsigned())

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned()
        value["pointer_hash"] = self.pointer_hash or self.computed_hash
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProductRevisionPointer":
        if not isinstance(value, Mapping):
            raise TypeError("product revision pointer must be a mapping")
        expected = {
            "schema_version", "kind", "run_id", "generation_id", "revision_id", "status", "revision_ref",
            "revision_hash", "candidate_ref", "candidate_hash", "review_ref", "review_hash", "prior_revision_id",
            "prior_candidate_hash", "prior_review_hash", "updated_at", "pointer_hash",
        }
        if expected != set(value):
            raise ValueError("product revision pointer schema is not exact")
        pointer = cls(**{field: value[field] for field in expected})
        if pointer.pointer_hash is not None and pointer.pointer_hash != pointer.computed_hash:
            raise ValueError("product revision pointer hash does not match content")
        return pointer


class ProductReviewStore:
    """Generation-scoped durable product review boundary."""

    def __init__(
        self,
        context: RunContext,
        generation_id: str | None = None,
        *,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(context, RunContext):
            raise TypeError("ProductReviewStore requires a RunContext")
        self.context = context
        active = RunLifecycle.active_generation_metadata(context)
        resolved_generation = generation_id or (active.generation_id if active is not None else None)
        self.generation_id = _safe_generation_id(resolved_generation)
        self.root = context.resolve_product_path(f"generations/{self.generation_id}")
        self.candidate_path = self.root / _CANDIDATE_FILENAME
        self.review_path = self.root / _REVIEW_FILENAME
        self.authorization_path = self.root / _AUTHORIZATION_FILENAME
        # ``candidate_path``/``review_path`` remain the legacy root names so
        # old generations can be adopted exactly once.  Once the revision
        # pointer exists every new write resolves through the immutable
        # revision namespace instead of mutating those root files.
        self.revisions_root = self.root / _REVISION_ROOT
        self.pointer_path = self.root / _REVISION_POINTER_FILENAME
        self.lock_path = self.root / _LOCK_FILENAME
        self._failpoint = failpoint

    def _invoke_failpoint(self, name: str) -> None:
        """Invoke an optional test-only interruption hook.

        The hook is intentionally private and inert for production callers.
        Tests use it to raise ``KeyboardInterrupt`` at the two durable
        activation boundaries; the store's replay method then converges the
        exact on-disk state without broad exception swallowing.
        """

        callback = self._failpoint
        if callable(callback):
            callback(name)

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if self.lock_path.is_symlink():
            raise ValueError("product review lock cannot be a symlink")
        with self.lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _revision_dir(self, revision_id: str) -> Path:
        """Resolve one revision directory without permitting path escapes."""

        revision_id = _text(revision_id, "revision_id")
        if _REVISION_ID.fullmatch(revision_id) is None:
            raise ValueError("revision_id must use the rev-XXXX form")
        candidate = self.revisions_root / revision_id
        # ``resolve`` must remain under the generation root even when a
        # pre-existing component is a symlink.  A revision namespace is
        # store-owned; symlink indirection is never accepted.
        if self.revisions_root.is_symlink() or candidate.is_symlink():
            raise ValueError("product revision namespace cannot contain symlinks")
        resolved = candidate.resolve(strict=False)
        if resolved != candidate and self.root.resolve() not in resolved.parents:
            raise ValueError("product revision path escapes generation root")
        try:
            resolved.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError("product revision path escapes generation root") from exc
        return candidate

    def _revision_file(self, revision_id: str, filename: str) -> Path:
        if filename not in {_REVISION_STATE_FILENAME, _CANDIDATE_FILENAME, _REVIEW_FILENAME, _AUTHORIZATION_FILENAME}:
            raise ValueError("unsupported product revision file")
        directory = self._revision_dir(revision_id)
        path = directory / filename
        if path.is_symlink():
            raise ValueError("product revision evidence cannot be a symlink")
        return path

    def revision_artifacts_root(self, revision_id: str) -> Path:
        """Return the immutable output namespace owned by one Product revision.

        ``rev-0001`` is the one explicitly adopted legacy revision and keeps
        its already-published generation-level artifact references.  Every
        later revision owns this deterministic child namespace; callers must
        create/publish only inside it and may never fall back to the legacy
        generation root.
        """

        revision_dir = self._revision_dir(revision_id)
        artifacts = revision_dir / "artifacts"
        if artifacts.is_symlink():
            raise ValueError("product revision artifact namespace cannot be a symlink")
        resolved = artifacts.resolve(strict=False)
        try:
            resolved.relative_to(revision_dir.resolve())
        except ValueError as exc:
            raise ValueError("product revision artifact namespace escapes its revision") from exc
        return artifacts

    def revision_artifacts_ref(self, revision_id: str) -> str:
        """Return the run-relative immutable output namespace reference."""

        return _relative_ref(self.context, self.revision_artifacts_root(revision_id))

    def _validate_revision_artifact_scope(
        self,
        candidate: ProductCandidate,
        revision_id: str,
        *,
        require_files: bool = True,
    ) -> None:
        """Ensure a target candidate binds only its own immutable artifacts.

        The adopted initial revision is intentionally exempt: its candidate
        is a hash-verified snapshot of the existing generation-level product
        and those paths become frozen by the fact that all future writes are
        revision-scoped.  New revisions must bind each canonical output leaf
        exactly under ``product_revisions/<rev>/artifacts``.
        """

        revision = self._read_revision_locked(revision_id)
        if revision.revision_id == "rev-0001" and revision.request_id == "legacy_root_adoption":
            return
        root = self.revision_artifacts_root(revision_id)
        if require_files and (not root.exists() or not root.is_dir()):
            raise FileNotFoundError(root)
        if root.is_symlink():
            raise ValueError("product revision artifact namespace cannot be a symlink")
        root_resolved = root.resolve(strict=False)
        for name in _OUTPUT_NAMES:
            binding = candidate.artifact_bindings.get(name)
            if not isinstance(binding, Mapping):
                raise ValueError(f"product revision artifact binding {name} is invalid")
            ref = binding.get("ref")
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError(f"product revision artifact binding {name} is missing")
            path = self.context.resolve_run_path(ref)
            expected = root / _OUTPUT_FILENAMES[name]
            if path.is_symlink():
                raise ValueError(f"product revision artifact {name} cannot be a symlink")
            try:
                if path.resolve(strict=False) != expected.resolve(strict=False):
                    raise ValueError(
                        f"product revision artifact {name} must remain under its revision namespace"
                    )
                path.resolve(strict=False).relative_to(root_resolved)
            except ValueError as exc:
                if "must remain" in str(exc):
                    raise
                raise ValueError(
                    f"product revision artifact {name} escapes its revision namespace"
                ) from exc
            if require_files and not path.exists():
                raise FileNotFoundError(path)

    def _revision_state_path(self, revision_id: str) -> Path:
        return self._revision_file(revision_id, _REVISION_STATE_FILENAME)

    def _revision_candidate_path(self, revision_id: str) -> Path:
        return self._revision_file(revision_id, _CANDIDATE_FILENAME)

    def _revision_review_path(self, revision_id: str) -> Path:
        return self._revision_file(revision_id, _REVIEW_FILENAME)

    def _revision_authorization_path(self, revision_id: str) -> Path:
        return self._revision_file(revision_id, _AUTHORIZATION_FILENAME)

    def _read_revision_locked(self, revision_id: str) -> ProductRevision:
        value = ProductRevision.from_dict(_load_json(self._revision_state_path(revision_id), "product revision"))
        if value.run_id != self.context.run_id or value.generation_id != self.generation_id or value.revision_id != revision_id:
            raise ValueError("product revision lineage is invalid")
        return value

    def _write_revision_locked(self, revision: ProductRevision) -> ProductRevision:
        persisted = ProductRevision.from_dict(revision.to_dict())
        path = self._revision_state_path(persisted.revision_id)
        if path.exists() or path.is_symlink():
            existing = self._read_revision_locked(persisted.revision_id)
            if existing.to_dict() == persisted.to_dict():
                return existing
            # State transitions are serialized by ``_locked``.  The previous
            # hash-verified state is replaced only with the caller's next
            # typed transition; candidate/review evidence itself is never
            # overwritten by this operation.
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, persisted.to_dict())
        _fsync_directory(path.parent)
        return persisted

    def _next_revision_id_locked(self) -> str:
        self.revisions_root.mkdir(parents=True, exist_ok=True)
        if self.revisions_root.is_symlink():
            raise ValueError("product revision namespace cannot be a symlink")
        used: set[int] = set()
        for child in self.revisions_root.iterdir():
            if child.is_symlink():
                raise ValueError("product revision namespace cannot contain symlinks")
            if child.is_dir():
                match = re.fullmatch(r"rev-(\d{4,})", child.name)
                if match is None:
                    raise ValueError("product revision namespace contains an unsupported entry")
                used.add(int(match.group(1)))
            else:
                raise ValueError("product revision namespace contains a non-directory entry")
        number = 1
        while number in used:
            number += 1
        return f"rev-{number:04d}"

    def _load_candidate_path(self, path: Path, *, revision_id: str | None = None) -> ProductCandidate:
        value = ProductCandidate.from_dict(_load_json(path, "product candidate"))
        rebound = self._check_candidate_scope(value, revision_id=revision_id)
        if rebound.to_dict() != value.to_dict():
            raise ValueError("product candidate artifact bindings are stale")
        return value

    def _load_review_path(
        self,
        review_path: Path,
        candidate_path: Path,
        *,
        revision_id: str | None = None,
    ) -> ProductReview:
        value = ProductReview.from_dict(_load_json(review_path, "product review"))
        candidate = self._load_candidate_path(candidate_path, revision_id=revision_id)
        if value.candidate_hash != candidate.computed_hash:
            raise ValueError("product review is stale against the candidate")
        return value

    def _load_pointer_locked(self) -> ProductRevisionPointer | None:
        if not self.pointer_path.exists() and not self.pointer_path.is_symlink():
            return None
        pointer = ProductRevisionPointer.from_dict(_load_json(self.pointer_path, "product revision pointer"))
        if pointer.run_id != self.context.run_id or pointer.generation_id != self.generation_id:
            raise ValueError("product revision pointer lineage is invalid")
        revision = self._read_revision_locked(pointer.revision_id)
        if revision.status != pointer.status:
            # Activation writes the fully hash-bound accepted pointer before
            # flipping the target revision from ``activation_pending`` to
            # ``accepted``.  This one transitional pair is replayable; every
            # other status mismatch remains a strict integrity failure.
            if not (revision.status == "activation_pending" and pointer.status == "accepted"):
                raise ValueError("product revision pointer is stale")
            expected_accepted = replace(
                revision,
                status="accepted",
                revision_hash=None,
            )
            if pointer.revision_hash != expected_accepted.computed_hash:
                raise ValueError("product revision activation pointer is stale")
        elif revision.revision_hash != pointer.revision_hash:
            raise ValueError("product revision pointer is stale")
        revision_ref = _relative_ref(self.context, self._revision_state_path(pointer.revision_id))
        if pointer.revision_ref != revision_ref:
            raise ValueError("product revision pointer reference is stale")
        candidate_path = self._revision_candidate_path(pointer.revision_id)
        review_path = self._revision_review_path(pointer.revision_id)
        candidate = self._load_candidate_path(candidate_path, revision_id=pointer.revision_id)
        review = self._load_review_path(review_path, candidate_path, revision_id=pointer.revision_id)
        if candidate.computed_hash != pointer.candidate_hash or review.computed_hash != pointer.review_hash:
            raise ValueError("product revision pointer evidence is stale")
        if revision.candidate_ref != _relative_ref(self.context, candidate_path) or revision.review_ref != _relative_ref(self.context, review_path):
            raise ValueError("product revision evidence references are stale")
        return pointer

    def _adopt_legacy_locked(self) -> ProductRevisionPointer | None:
        """Adopt one existing accepted root candidate/review exactly once."""

        pointer = self._load_pointer_locked()
        if pointer is not None:
            return pointer
        root_candidate_exists = self.candidate_path.exists() or self.candidate_path.is_symlink()
        root_review_exists = self.review_path.exists() or self.review_path.is_symlink()
        if not root_candidate_exists and not root_review_exists:
            return None
        if self.candidate_path.is_symlink() or self.review_path.is_symlink():
            raise ValueError("legacy product candidate/review cannot be a symlink")
        if not root_candidate_exists or not root_review_exists:
            raise ValueError("legacy product candidate and review must be adopted together")
        candidate = self._load_candidate_path(self.candidate_path)
        review = self._load_review_path(self.review_path, self.candidate_path)
        if review.verdict not in {"accept", "accept_with_limits", "repair_once", "blocked_rethink", "block"}:
            raise ValueError("legacy product root review verdict is invalid")
        revision_id = "rev-0001"
        revision_dir = self._revision_dir(revision_id)
        if revision_dir.exists() or revision_dir.is_symlink():
            raise ValueError("legacy revision adoption collides with existing revision")
        revision_dir.mkdir(parents=True)
        revision_candidate = self._revision_candidate_path(revision_id)
        revision_review = self._revision_review_path(revision_id)
        _atomic_copy(self.candidate_path, revision_candidate)
        _atomic_copy(self.review_path, revision_review)
        if self.authorization_path.exists() or self.authorization_path.is_symlink():
            if self.authorization_path.is_symlink():
                raise ValueError("legacy publication authorization cannot be a symlink")
            # Authorization is copied for immutable audit provenance.  It is
            # not part of the active pointer and is revalidated by callers.
            _atomic_copy(self.authorization_path, self._revision_authorization_path(revision_id))
        now = datetime.now(timezone.utc).isoformat()
        revision = ProductRevision(
            run_id=self.context.run_id,
            generation_id=self.generation_id,
            revision_id=revision_id,
            status=("accepted" if review.verdict in {"accept", "accept_with_limits"} else "reviewed"),
            request_id="legacy_root_adoption",
            prior_revision_id=None,
            candidate_ref=_relative_ref(self.context, revision_candidate),
            candidate_hash=candidate.computed_hash,
            review_ref=_relative_ref(self.context, revision_review),
            review_hash=review.computed_hash,
            created_at=now,
            updated_at=now,
        )
        self._write_revision_locked(revision)
        pointer = ProductRevisionPointer(
            run_id=self.context.run_id,
            generation_id=self.generation_id,
            revision_id=revision_id,
            status=revision.status,
            revision_ref=_relative_ref(self.context, self._revision_state_path(revision_id)),
            revision_hash=revision.computed_hash,
            candidate_ref=revision.candidate_ref or "",
            candidate_hash=candidate.computed_hash,
            review_ref=revision.review_ref or "",
            review_hash=review.computed_hash,
            updated_at=now,
        )
        if self.pointer_path.exists() or self.pointer_path.is_symlink():
            raise ValueError("product revision pointer appeared during adoption")
        _atomic_write(self.pointer_path, pointer.to_dict())
        _fsync_directory(self.root)
        return pointer

    def load_revision(self, revision_id: str) -> ProductRevision:
        with self._locked():
            return self._read_revision_locked(revision_id)

    def load_revision_candidate(self, revision_id: str) -> ProductCandidate:
        with self._locked():
            revision = self._read_revision_locked(revision_id)
            return self._load_candidate_path(
                self._revision_candidate_path(revision.revision_id),
                revision_id=revision.revision_id,
            )

    def load_revision_review(self, revision_id: str) -> ProductReview:
        with self._locked():
            revision = self._read_revision_locked(revision_id)
            return self._load_review_path(
                self._revision_review_path(revision.revision_id),
                self._revision_candidate_path(revision.revision_id),
                revision_id=revision.revision_id,
            )

    def load_active_revision(self) -> ProductRevisionPointer | None:
        with self._locked():
            return self._adopt_legacy_locked()

    def read_active_revision(self) -> ProductRevisionPointer | None:
        """Read the pointer without adopting legacy root evidence.

        Planner/status projections must stay read-only.  The explicit
        regeneration boundary calls :meth:`load_active_revision` to perform
        the one-time, hash-verified legacy adoption transaction.
        """

        with self._locked():
            return self._load_pointer_locked()

    def begin_revision(
        self,
        *,
        request_id: str,
        input_fingerprint: str,
        implementation_identity: str,
        prior_revision_id: str | None = None,
        prior_candidate_hash: str | None = None,
        prior_review_hash: str | None = None,
    ) -> ProductRevision:
        """Create or return one pending Product revision under the store lock."""

        request_id = _text(request_id, "request_id")
        input_fingerprint = _sha(input_fingerprint, "input_fingerprint")
        implementation_identity = _sha(implementation_identity, "implementation_identity")
        for label, value in (("prior_candidate_hash", prior_candidate_hash), ("prior_review_hash", prior_review_hash)):
            if value is not None:
                _sha(value, label)
        with self._locked():
            pointer = self._adopt_legacy_locked()
            if pointer is not None:
                expected_revision = pointer.revision_id
                expected_candidate = pointer.candidate_hash
                expected_review = pointer.review_hash
                if prior_revision_id is not None and prior_revision_id != expected_revision:
                    raise ValueError("prior product revision does not match active pointer")
                if prior_candidate_hash is not None and prior_candidate_hash != expected_candidate:
                    raise ValueError("prior product candidate does not match active pointer")
                if prior_review_hash is not None and prior_review_hash != expected_review:
                    raise ValueError("prior product review does not match active pointer")
                prior_revision_id = expected_revision
                prior_candidate_hash = expected_candidate
                prior_review_hash = expected_review
            elif any(value is not None for value in (prior_revision_id, prior_candidate_hash, prior_review_hash)):
                raise ValueError("prior product revision is unavailable")
            self.revisions_root.mkdir(parents=True, exist_ok=True)
            nonterminal_statuses = frozenset({"pending", "candidate", "reviewed", "activation_pending"})
            for child in self.revisions_root.iterdir():
                if child.is_symlink():
                    raise ValueError("product revision namespace cannot contain symlinks")
                if not child.is_dir() or not self._revision_state_path(child.name).exists():
                    raise ValueError("product revision namespace contains an incomplete revision")
                existing = self._read_revision_locked(child.name)
                if existing.request_id == request_id:
                    # Idempotent retries may only reconcile a still-open
                    # target.  A terminal revision is immutable evidence for a
                    # prior request, not a new admission target; callers must
                    # use a fresh request id (and therefore a fresh namespace)
                    # after a failed/accepted outcome.
                    if existing.status not in nonterminal_statuses:
                        raise ValueError("product revision request already reached a terminal outcome")
                    if (
                        existing.input_fingerprint != input_fingerprint
                        or existing.implementation_identity != implementation_identity
                        or existing.prior_revision_id != prior_revision_id
                        or existing.prior_candidate_hash != prior_candidate_hash
                        or existing.prior_review_hash != prior_review_hash
                    ):
                        raise ValueError("product revision request conflicts with existing revision")
                    if existing.revision_id != "rev-0001" or existing.request_id != "legacy_root_adoption":
                        artifacts = self.revision_artifacts_root(existing.revision_id)
                        if artifacts.exists() and not artifacts.is_dir():
                            raise ValueError("product revision artifact namespace is not a directory")
                        artifacts.mkdir(parents=True, exist_ok=True)
                    return existing
                if existing.status in nonterminal_statuses and existing.revision_id != "rev-0001":
                    # There is one Product revision transaction in flight per
                    # generation.  Reject a different request before any new
                    # revision directory is created; the exact same request
                    # above remains idempotent and returns its target.
                    raise ValueError("another Product revision request is still in progress")
            revision_id = self._next_revision_id_locked()
            now = datetime.now(timezone.utc).isoformat()
            revision = ProductRevision(
                run_id=self.context.run_id,
                generation_id=self.generation_id,
                revision_id=revision_id,
                status="pending",
                request_id=request_id,
                input_fingerprint=input_fingerprint,
                implementation_identity=implementation_identity,
                prior_revision_id=prior_revision_id,
                prior_candidate_hash=prior_candidate_hash,
                prior_review_hash=prior_review_hash,
                created_at=now,
                updated_at=now,
            )
            revision_dir = self._revision_dir(revision_id)
            revision_dir.mkdir(parents=True)
            artifacts = self.revision_artifacts_root(revision_id)
            if artifacts.exists() or artifacts.is_symlink():
                raise ValueError("product revision artifact namespace collides with existing data")
            artifacts.mkdir()
            _fsync_directory(revision_dir)
            return self._write_revision_locked(revision)

    def _activate_revision_locked(
        self,
        revision_id: str,
        *,
        invoke_failpoints: bool = True,
    ) -> ProductRevisionPointer:
        """Converge one accepted target and its active pointer under the lock.

        The accepted hash is computed while the target is still pending, then
        the pointer is atomically written with that hash.  Only after that
        durable CAS succeeds is the target state flipped to ``accepted``.
        Therefore an interruption on either side is represented by a typed,
        replayable state rather than an accepted target that is not current.
        """

        revision = self._read_revision_locked(revision_id)
        candidate_path = self._revision_candidate_path(revision_id)
        review_path = self._revision_review_path(revision_id)
        candidate = self._load_candidate_path(candidate_path, revision_id=revision_id)
        review = self._load_review_path(review_path, candidate_path, revision_id=revision_id)
        if review.verdict not in {"accept", "accept_with_limits"}:
            raise ValueError("only an accepted Product review can activate a revision")
        if revision.candidate_hash != candidate.computed_hash or revision.review_hash != review.computed_hash:
            raise ValueError("product revision evidence is stale")

        pointer = self._adopt_legacy_locked()
        if pointer is not None and pointer.revision_id == revision_id:
            # A previous attempt already published the pointer.  Reconstruct
            # the deterministic accepted state from the pending timestamp and
            # finish the transition idempotently.
            if revision.status == "activation_pending":
                accepted = ProductRevision.from_dict(
                    replace(revision, status="accepted", revision_hash=None).to_dict()
                )
                if pointer.revision_hash != accepted.computed_hash:
                    raise ValueError("product revision activation pointer is stale")
                self._write_revision_locked(accepted)
                revision = accepted
            elif revision.status != "accepted" or pointer.status != "accepted":
                raise ValueError("product revision pointer status is invalid")
            self._supersede_prior_locked(revision, revision.updated_at or datetime.now(timezone.utc).isoformat())
            return self._load_pointer_locked() or pointer

        if pointer is not None:
            if (
                revision.prior_revision_id != pointer.revision_id
                or revision.prior_candidate_hash != pointer.candidate_hash
                or revision.prior_review_hash != pointer.review_hash
            ):
                raise ValueError("product revision prior pointer compare-and-set failed")
        elif any(value is not None for value in (revision.prior_revision_id, revision.prior_candidate_hash, revision.prior_review_hash)):
            raise ValueError("product revision prior pointer is unavailable")

        if revision.status not in {"reviewed", "activation_pending", "accepted"}:
            raise ValueError("product revision is not ready for activation")
        now = revision.updated_at if revision.status == "activation_pending" and revision.updated_at else datetime.now(timezone.utc).isoformat()
        accepted = ProductRevision.from_dict(
            replace(revision, status="accepted", updated_at=now, revision_hash=None).to_dict()
        )
        if revision.status != "activation_pending":
            pending = ProductRevision.from_dict(
                replace(revision, status="activation_pending", updated_at=now, revision_hash=None).to_dict()
            )
            self._write_revision_locked(pending)
            revision = pending
            if invoke_failpoints:
                self._invoke_failpoint("after_activation_pending")

        new_pointer = ProductRevisionPointer(
            run_id=self.context.run_id,
            generation_id=self.generation_id,
            revision_id=revision_id,
            status="accepted",
            revision_ref=_relative_ref(self.context, self._revision_state_path(revision_id)),
            # Bind the pointer to the exact accepted state that will be
            # written immediately after the CAS.  ``_load_pointer_locked``
            # validates this expected transition while it is pending.
            revision_hash=accepted.computed_hash,
            candidate_ref=revision.candidate_ref or _relative_ref(self.context, candidate_path),
            candidate_hash=candidate.computed_hash,
            review_ref=revision.review_ref or _relative_ref(self.context, review_path),
            review_hash=review.computed_hash,
            prior_revision_id=revision.prior_revision_id,
            prior_candidate_hash=revision.prior_candidate_hash,
            prior_review_hash=revision.prior_review_hash,
            updated_at=now,
        )
        if self.pointer_path.exists() or self.pointer_path.is_symlink():
            current = self._load_pointer_locked()
            if current is None:
                raise ValueError("product revision pointer is invalid")
            if current.revision_id == revision_id:
                return self._activate_revision_locked(revision_id, invoke_failpoints=False)
            if current.revision_id != revision.prior_revision_id:
                raise ValueError("product revision pointer changed during activation")
        if invoke_failpoints:
            self._invoke_failpoint("before_pointer_write")
        _atomic_write(self.pointer_path, new_pointer.to_dict())
        _fsync_directory(self.root)
        if invoke_failpoints:
            self._invoke_failpoint("after_pointer_write_before_revision_accepted")
        self._write_revision_locked(accepted)
        if revision.prior_revision_id:
            self._supersede_prior_locked(accepted, now)
        return self._load_pointer_locked() or new_pointer

    def _supersede_prior_locked(self, revision: ProductRevision, now: str) -> None:
        if not revision.prior_revision_id:
            return
        previous = self._read_revision_locked(revision.prior_revision_id)
        if previous.status == "accepted":
            self._write_revision_locked(
                replace(previous, status="superseded", updated_at=now, revision_hash=None)
            )

    def activate_revision(self, revision_id: str) -> ProductRevisionPointer:
        """Advance the accepted pointer after a fully accepted review."""

        with self._locked():
            return self._activate_revision_locked(revision_id)

    def reconcile_revision_activation(self, revision_id: str) -> ProductRevisionPointer:
        """Replay an interrupted activation transaction idempotently.

        This explicit mutating boundary is used by Coordinator replay/startup;
        read-only projections continue to inspect the pointer without writing
        or adopting legacy evidence.
        """

        with self._locked():
            revision = self._read_revision_locked(revision_id)
            if revision.status not in {"activation_pending", "accepted"}:
                pointer = self._load_pointer_locked()
                if pointer is None or pointer.revision_id != revision_id:
                    raise ValueError("product revision is not pending activation")
                return pointer
            return self._activate_revision_locked(revision_id, invoke_failpoints=False)

    def fail_revision(self, revision_id: str) -> ProductRevision:
        with self._locked():
            revision = self._read_revision_locked(revision_id)
            pointer = self._load_pointer_locked()
            if pointer is not None and pointer.revision_id == revision_id:
                raise ValueError("active product revision cannot be failed")
            failed = replace(revision, status="failed", updated_at=datetime.now(timezone.utc).isoformat(), revision_hash=None)
            return self._write_revision_locked(failed)

    def load_candidate(self) -> ProductCandidate:
        pointer = self._load_pointer_locked()
        if pointer is not None:
            path = self.context.resolve_run_path(pointer.candidate_ref)
            return self._load_candidate_path(path, revision_id=pointer.revision_id)
        return self._load_candidate_path(self.candidate_path)

    def load_review(self) -> ProductReview:
        pointer = self._load_pointer_locked()
        if pointer is not None:
            review_path = self.context.resolve_run_path(pointer.review_ref)
            candidate_path = self.context.resolve_run_path(pointer.candidate_ref)
            return self._load_review_path(review_path, candidate_path, revision_id=pointer.revision_id)
        return self._load_review_path(self.review_path, self.candidate_path)

    def load_authorization(self) -> PublishAuthorization:
        pointer = self._load_pointer_locked()
        authorization_path = self.authorization_path
        candidate_path = self.candidate_path
        review_path = self.review_path
        if pointer is not None:
            candidate_path = self.context.resolve_run_path(pointer.candidate_ref)
            review_path = self.context.resolve_run_path(pointer.review_ref)
            authorization_path = self._revision_authorization_path(pointer.revision_id)
        value = PublishAuthorization.from_dict(_load_json(authorization_path, "publish authorization"))
        candidate = self._load_candidate_path(candidate_path, revision_id=(pointer.revision_id if pointer is not None else None))
        review = self._load_review_path(
            review_path,
            candidate_path,
            revision_id=(pointer.revision_id if pointer is not None else None),
        )
        if value.candidate_hash != candidate.computed_hash or value.review_hash != review.computed_hash:
            raise ValueError("publish authorization is stale against candidate or review")
        return value

    def _check_candidate_scope(
        self,
        candidate: ProductCandidate,
        *,
        revision_id: str | None = None,
    ) -> ProductCandidate:
        if candidate.run_id != self.context.run_id or candidate.generation_id != self.generation_id:
            raise ValueError("product candidate is bound to another run or generation")
        self._validate_generation_lineage(candidate)
        bindings: dict[str, Mapping[str, Any]] = {}
        for name, binding in candidate.artifact_bindings.items():
            ref = binding.get("ref")
            if not isinstance(ref, str):
                raise ValueError(f"candidate artifact {name} has no ref")
            bindings[name] = _artifact_binding(self.context, binding, f"candidate artifact {name}")
        # Persist the computed kind/digest/tree file map even when the caller
        # supplied only paths; the candidate is the durable binding consumed
        # by independent review and later publication authorization.
        rebound = replace(candidate, artifact_bindings=bindings, candidate_hash=None)
        if revision_id is not None:
            self._validate_revision_artifact_scope(rebound, revision_id)
        return rebound

    def _validate_generation_lineage(self, candidate: ProductCandidate) -> None:
        """Bind candidate lineage to the lifecycle's validated generation view."""

        metadata = RunLifecycle.active_generation_metadata(self.context)
        if metadata is None:
            lifecycle = RunLifecycle.load(self.context)
            if candidate.generation_id != "G-0001" or candidate.parent_lineage.get("root_generation") is not True:
                raise ValueError("product candidate parent generation metadata is missing for non-root generation")
            expected_parent = None
            expected_parent_ref = None
            expected_parent_hash = None
            plan_path = lifecycle.plan_path
        else:
            if metadata.generation_id != candidate.generation_id:
                raise ValueError("product candidate generation lineage is stale")
            if candidate.parent_lineage.get("root_generation") is True:
                raise ValueError("product candidate root marker is invalid for an extension generation")
            expected_parent = metadata.parent_generation_id
            expected_parent_ref = "run_state.json" if expected_parent == "G-0001" else f"extensions/{expected_parent}/generation_manifest.json"
            parent_path = self.context.resolve_run_path(expected_parent_ref)
            expected_parent_hash = hashlib.sha256(parent_path.read_bytes()).hexdigest()
            supplied_parent = candidate.parent_lineage.get("parent_generation_id")
            supplied_parent_ref = candidate.parent_lineage.get("parent_manifest_ref")
            supplied_parent_hash = candidate.parent_lineage.get("parent_manifest_hash")
            if supplied_parent != expected_parent or supplied_parent_ref != expected_parent_ref or supplied_parent_hash != expected_parent_hash:
                raise ValueError("product candidate parent generation binding is stale")
            plan_path = Path(metadata.plan_path)
        canonical_plan_ref = _relative_ref(self.context, plan_path)
        supplied_plan_ref = candidate.plan_binding.get("plan_ref")
        if supplied_plan_ref != canonical_plan_ref:
            raise ValueError("product candidate plan reference is stale")
        supplied_plan_hash = candidate.plan_binding.get("plan_hash")
        if not plan_path.is_file() or plan_path.is_symlink() or hashlib.sha256(plan_path.read_bytes()).hexdigest() != supplied_plan_hash:
            raise ValueError("product candidate plan hash is stale")
        if expected_parent is None:
            if any(candidate.parent_lineage.get(key) is not None for key in ("parent_generation_id", "parent_manifest_ref", "parent_manifest_hash")):
                raise ValueError("product candidate root parent fields must be null")

    @staticmethod
    def _binding_fields_well_formed(binding: Mapping[str, Any]) -> bool:
        if "kind" in binding and not isinstance(binding["kind"], str):
            return False
        if "sha256" in binding or "hash" in binding:
            if "sha256" in binding and "hash" in binding and binding["sha256"] != binding["hash"]:
                return False
            supplied_sha = binding.get("sha256", binding.get("hash"))
            if not isinstance(supplied_sha, str) or _SHA256.fullmatch(supplied_sha) is None:
                return False
        if "files" in binding:
            files = binding["files"]
            if not isinstance(files, Mapping):
                return False
            if any(not isinstance(path, str) or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None for path, digest in files.items()):
                return False
        return True

    @classmethod
    def _is_artifact_binding_drift(cls, error: ValueError, candidate: ProductCandidate) -> bool:
        message = str(error)
        if not message.startswith("candidate artifact "):
            return False
        name = message[len("candidate artifact ") :].split(" ", 1)[0]
        binding = candidate.artifact_bindings.get(name, {})
        if not cls._binding_fields_well_formed(binding):
            return False
        if message.endswith(" kind does not match the artifact"):
            supplied_kind = binding.get("kind")
            return isinstance(supplied_kind, str)
        if message.endswith(" hash does not match the artifact"):
            supplied_sha = binding.get("sha256", binding.get("hash"))
            return isinstance(supplied_sha, str) and _SHA256.fullmatch(supplied_sha) is not None
        return False

    @staticmethod
    def _has_explicit_artifact_binding_drift(candidate: ProductCandidate, rebound: ProductCandidate) -> bool:
        """Detect stale explicit binding fields without treating aliases as drift."""

        for name, raw_binding in candidate.artifact_bindings.items():
            canonical_binding = rebound.artifact_bindings[name]
            if "kind" in raw_binding:
                if not isinstance(raw_binding["kind"], str):
                    raise ValueError(f"candidate artifact {name} kind binding is malformed")
                if raw_binding["kind"] != canonical_binding["kind"]:
                    return True
            if "sha256" in raw_binding or "hash" in raw_binding:
                if "sha256" in raw_binding and "hash" in raw_binding and raw_binding["sha256"] != raw_binding["hash"]:
                    raise ValueError(f"candidate artifact {name} hash binding is malformed")
                supplied_sha = raw_binding.get("sha256", raw_binding.get("hash"))
                if not isinstance(supplied_sha, str) or _SHA256.fullmatch(supplied_sha) is None:
                    raise ValueError(f"candidate artifact {name} hash binding is malformed")
                if supplied_sha != canonical_binding["sha256"]:
                    return True
            if "files" in raw_binding:
                files = raw_binding["files"]
                if canonical_binding["kind"] != "tree" or not isinstance(files, Mapping):
                    raise ValueError(f"candidate artifact {name} files binding is malformed")
                if any(not isinstance(path, str) or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None for path, digest in files.items()):
                    raise ValueError(f"candidate artifact {name} files binding is malformed")
                if dict(files) != canonical_binding.get("files"):
                    return True
        return False

    def _archive_repair_once_review(self, candidate: ProductCandidate) -> bool:
        """Preserve and retire a review that explicitly authorizes rebuild.

        ``repair_once`` is the original product-repair verdict.  A
        ``blocked_rethink`` verdict is the equivalent explicit instruction
        when the reviewer cannot accept the current presentation and asks for
        a new candidate.  Both are archived before the candidate is retired;
        accepted (or otherwise non-repair) reviews remain protected.
        """

        if not self.review_path.exists():
            return False
        review = ProductReview.from_dict(_load_json(self.review_path, "product review"))
        if (
            review.run_id != self.context.run_id
            or review.generation_id != self.generation_id
            or review.candidate_hash != candidate.computed_hash
            or review.verdict not in {"repair_once", "blocked_rethink"}
        ):
            raise ValueError("cannot discard product candidate after review or authorization")
        archive = self.root / f"product_review.{review.verdict}.{review.computed_hash}.json"
        if archive.is_symlink():
            raise ValueError("archived product repair review cannot be a symlink")
        if archive.exists():
            archived = ProductReview.from_dict(_load_json(archive, "archived product repair review"))
            if archived.to_dict() != review.to_dict():
                raise ValueError("archived product repair review conflicts with current review")
        else:
            _atomic_write(archive, review.to_dict())
        self.review_path.unlink()
        return True

    def begin_repair_once_rebuild(self) -> bool:
        """Open a rebuild authorized by ``repair_once`` or ``blocked_rethink``."""

        with self._locked():
            if self.authorization_path.exists() or self.authorization_path.is_symlink():
                raise ValueError("cannot rebuild product candidate after authorization")
            if self.review_path.is_symlink() or self.candidate_path.is_symlink():
                raise ValueError("product candidate or review cannot be a symlink")
            if not self.candidate_path.exists() or not self.review_path.exists():
                return False
            candidate = ProductCandidate.from_dict(_load_json(self.candidate_path, "product candidate"))
            if candidate.run_id != self.context.run_id or candidate.generation_id != self.generation_id:
                raise ValueError("product candidate is bound to another run or generation")
            self._archive_repair_once_review(candidate)
            self.candidate_path.unlink()
            _fsync_directory(self.root)
            return True

    def discard_stale_candidate_for_rebuild(self) -> bool:
        """Retire only an exact stale candidate at the public repair boundary."""

        with self._locked():
            if self.authorization_path.exists() or self.authorization_path.is_symlink():
                raise ValueError("cannot discard product candidate after review or authorization")
            if self.review_path.is_symlink():
                raise ValueError("cannot discard product candidate after review or authorization")
            if self.candidate_path.is_symlink():
                raise ValueError("product candidate cannot be a symlink")
            if not self.candidate_path.exists():
                return False

            candidate = ProductCandidate.from_dict(_load_json(self.candidate_path, "product candidate"))
            if candidate.run_id != self.context.run_id or candidate.generation_id != self.generation_id:
                raise ValueError("product candidate is bound to another run or generation")
            drifted = False
            review: ProductReview | None = None
            if self.review_path.exists():
                review = ProductReview.from_dict(_load_json(self.review_path, "product review"))
                if review.candidate_hash != candidate.computed_hash:
                    raise ValueError("product review is stale against the candidate")
                # A product reviewer may explicitly request a fresh
                # candidate even when the existing artifact bytes are still
                # internally valid.  This is the narrow product-repair
                # boundary; accepted candidates remain protected below.
                if review.verdict in {"repair_once", "blocked_rethink"}:
                    drifted = True
            try:
                rebound = self._check_candidate_scope(candidate)
            except ValueError as error:
                if not self._is_artifact_binding_drift(error, candidate):
                    raise
                drifted = True
            else:
                drifted = drifted or self._has_explicit_artifact_binding_drift(candidate, rebound)
            if not drifted:
                return False

            if review is not None:
                self._archive_repair_once_review(candidate)
            self.candidate_path.unlink()
            _fsync_directory(self.root)
            return True

    def record_candidate(
        self,
        candidate: ProductCandidate | Mapping[str, Any],
        *,
        revision_id: str | None = None,
    ) -> ProductCandidate:
        value = candidate if isinstance(candidate, ProductCandidate) else ProductCandidate.from_dict(candidate)
        with self._locked():
            pointer = self._load_pointer_locked()
            if revision_id is not None:
                revision = self._read_revision_locked(revision_id)
                if revision.status not in {"pending", "candidate"}:
                    raise ValueError("product revision is not accepting a candidate")
                value = self._check_candidate_scope(value, revision_id=revision_id)
                persisted = ProductCandidate.from_dict(value.to_dict())
                path = self._revision_candidate_path(revision_id)
                if path.exists() or path.is_symlink():
                    existing = self._load_candidate_path(path, revision_id=revision_id)
                    if existing.to_dict() != persisted.to_dict():
                        raise ValueError("product candidate conflicts with existing durable revision candidate")
                    return existing
                _atomic_write(path, persisted.to_dict())
                updated = replace(
                    revision,
                    status="candidate",
                    candidate_ref=_relative_ref(self.context, path),
                    candidate_hash=persisted.computed_hash,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    revision_hash=None,
                )
                self._write_revision_locked(updated)
                return persisted
            value = self._check_candidate_scope(value)
            persisted = ProductCandidate.from_dict(value.to_dict())
            if pointer is not None:
                raise ValueError("product revision binding is required after revision adoption")
            if self.candidate_path.exists() or self.candidate_path.is_symlink():
                existing = self._load_candidate_path(self.candidate_path)
                if existing.to_dict() != persisted.to_dict():
                    raise ValueError("product candidate conflicts with existing durable candidate")
                return existing
            _atomic_write(self.candidate_path, persisted.to_dict())
        return persisted

    def record_review(
        self,
        *,
        reviewer_ref: str,
        verdict: str,
        findings: Iterable[Mapping[str, Any]] = (),
        candidate_hash: str | None = None,
        reviewed_at: str | None = None,
        revision_id: str | None = None,
    ) -> ProductReview:
        if revision_id is None:
            pointer = self._load_pointer_locked()
            if pointer is not None:
                raise ValueError("product revision binding is required after revision adoption")
            candidate_path = self.candidate_path
            review_path = self.review_path
            candidate = self._load_candidate_path(candidate_path)
        else:
            revision = self._read_revision_locked(revision_id)
            if revision.status not in {"candidate", "reviewed"}:
                raise ValueError("product revision is not accepting a review")
            candidate_path = self._revision_candidate_path(revision_id)
            review_path = self._revision_review_path(revision_id)
            candidate = self._load_candidate_path(candidate_path, revision_id=revision_id)
        expected_hash = candidate.computed_hash
        if candidate_hash is not None and _sha(candidate_hash, "candidate_hash") != expected_hash:
            raise ValueError("product review candidate hash is stale")
        requested_findings = tuple(findings)
        with self._locked():
            if review_path.exists() or review_path.is_symlink():
                existing = self._load_review_path(review_path, candidate_path, revision_id=revision_id)
                if (
                    existing.candidate_hash != expected_hash
                    or existing.reviewer_ref != _text(reviewer_ref, "reviewer_ref")
                    or existing.verdict != _text(verdict, "verdict").lower()
                    or tuple(existing.findings) != tuple(_mapping(item, "finding") for item in requested_findings)
                ):
                    raise ValueError("product review conflicts with existing durable review")
                return existing
            value = ProductReview(
                run_id=self.context.run_id,
                generation_id=self.generation_id,
                candidate_ref=_relative_ref(self.context, candidate_path),
                candidate_hash=expected_hash,
                product_owner=candidate.product_owner,
                reviewer_ref=reviewer_ref,
                verdict=verdict,
                findings=requested_findings,
                reviewed_at=reviewed_at or datetime.now(timezone.utc).isoformat(),
            )
            persisted = ProductReview.from_dict(value.to_dict())
            _atomic_write(review_path, persisted.to_dict())
            if revision_id is not None:
                revision = self._read_revision_locked(revision_id)
                updated = replace(
                    revision,
                    status="reviewed",
                    review_ref=_relative_ref(self.context, review_path),
                    review_hash=persisted.computed_hash,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    revision_hash=None,
                )
                self._write_revision_locked(updated)
        return persisted

    def authorize_publish(
        self,
        *,
        publisher_ref: str,
        publication_policy: Mapping[str, Any] | None = None,
        publication_policy_hash: str | None = None,
        authorized_at: str | None = None,
        revision_id: str | None = None,
    ) -> PublishAuthorization:
        pointer = self._load_pointer_locked()
        if revision_id is None and pointer is not None:
            revision_id = pointer.revision_id
        if revision_id is not None:
            revision = self._read_revision_locked(revision_id)
            if revision.status != "accepted":
                raise ValueError("publication requires an accepted product revision")
            candidate_path = self._revision_candidate_path(revision_id)
            review_path = self._revision_review_path(revision_id)
            authorization_path = self._revision_authorization_path(revision_id)
            candidate = self._load_candidate_path(candidate_path, revision_id=revision_id)
            review = self._load_review_path(review_path, candidate_path, revision_id=revision_id)
        else:
            candidate_path = self.candidate_path
            review_path = self.review_path
            authorization_path = self.authorization_path
            candidate = self._load_candidate_path(candidate_path)
            review = self._load_review_path(review_path, candidate_path)
        if review.candidate_hash != candidate.computed_hash:
            raise ValueError("product review is stale against the candidate")
        if review.verdict not in {"accept", "accept_with_limits"}:
            raise ValueError("publication requires an accepted product review")
        if publication_policy is None:
            raise PermissionError("publication requires the full canonical policy")
        if not isinstance(publication_policy, Mapping):
            raise TypeError("publication_policy must be a mapping")
        policy = dict(_jsonable(publication_policy))
        if not isinstance(policy, Mapping):
            raise TypeError("publication_policy must be a mapping")
        if policy.get("enabled") is not True:
            raise PermissionError("publication is denied by policy")
        computed_policy_hash = canonical_hash(policy)
        if publication_policy_hash is not None and _sha(publication_policy_hash, "publication_policy_hash") != computed_policy_hash:
            raise ValueError("publication policy hash does not match policy")
        policy_hash = computed_policy_hash
        if policy_hash != candidate.publication_policy_hash:
            raise ValueError("publication policy does not match candidate binding")
        with self._locked():
            if authorization_path.exists() or authorization_path.is_symlink():
                value = PublishAuthorization.from_dict(_load_json(authorization_path, "publish authorization"))
                if value.candidate_hash != candidate.computed_hash or value.review_hash != review.computed_hash:
                    raise ValueError("publish authorization is stale against candidate or review")
                existing = value
                if (
                    existing.candidate_hash != candidate.computed_hash
                    or existing.review_hash != review.computed_hash
                    or existing.publication_policy_hash != policy_hash
                    or existing.publisher_ref != _text(publisher_ref, "publisher_ref")
                ):
                    raise ValueError("publish authorization conflicts with existing authorization")
                return existing
            value = PublishAuthorization(
                run_id=self.context.run_id,
                generation_id=self.generation_id,
                candidate_ref=_relative_ref(self.context, candidate_path),
                candidate_hash=candidate.computed_hash,
                review_ref=_relative_ref(self.context, review_path),
                review_hash=review.computed_hash,
                publication_policy_hash=policy_hash,
                publisher_ref=publisher_ref,
                authorized_at=authorized_at or datetime.now(timezone.utc).isoformat(),
            )
            persisted = PublishAuthorization.from_dict(value.to_dict())
            _atomic_write(authorization_path, persisted.to_dict())
        return persisted


def record_product_candidate(
    context: RunContext,
    candidate: ProductCandidate | Mapping[str, Any],
    *,
    generation_id: str | None = None,
    revision_id: str | None = None,
) -> ProductCandidate:
    return ProductReviewStore(context, generation_id).record_candidate(candidate, revision_id=revision_id)


def begin_product_revision(
    context: RunContext,
    *,
    request_id: str,
    input_fingerprint: str,
    implementation_identity: str,
    prior_revision_id: str | None = None,
    prior_candidate_hash: str | None = None,
    prior_review_hash: str | None = None,
    generation_id: str | None = None,
) -> ProductRevision:
    return ProductReviewStore(context, generation_id).begin_revision(
        request_id=request_id,
        input_fingerprint=input_fingerprint,
        implementation_identity=implementation_identity,
        prior_revision_id=prior_revision_id,
        prior_candidate_hash=prior_candidate_hash,
        prior_review_hash=prior_review_hash,
    )


def activate_product_revision(
    context: RunContext,
    revision_id: str,
    *,
    generation_id: str | None = None,
) -> ProductRevisionPointer:
    return ProductReviewStore(context, generation_id).activate_revision(revision_id)


def discard_stale_product_candidate(context: RunContext, *, generation_id: str | None = None) -> bool:
    return ProductReviewStore(context, generation_id).discard_stale_candidate_for_rebuild()


def record_product_review(context: RunContext, *, reviewer_ref: str, verdict: str, findings: Iterable[Mapping[str, Any]] = (), candidate_hash: str | None = None, generation_id: str | None = None, reviewed_at: str | None = None, revision_id: str | None = None) -> ProductReview:
    return ProductReviewStore(context, generation_id).record_review(
        reviewer_ref=reviewer_ref, verdict=verdict, findings=findings, candidate_hash=candidate_hash, reviewed_at=reviewed_at,
        revision_id=revision_id,
    )


def authorize_product_publish(context: RunContext, *, publisher_ref: str, publication_policy: Mapping[str, Any] | None = None, publication_policy_hash: str | None = None, generation_id: str | None = None, authorized_at: str | None = None, revision_id: str | None = None) -> PublishAuthorization:
    return ProductReviewStore(context, generation_id).authorize_publish(
        publisher_ref=publisher_ref, publication_policy=publication_policy, publication_policy_hash=publication_policy_hash, authorized_at=authorized_at,
        revision_id=revision_id,
    )


__all__ = [
    "ProductCandidate",
    "ProductReview",
    "ProductRevision",
    "ProductRevisionPointer",
    "PublishAuthorization",
    "ProductReviewStore",
    "activate_product_revision",
    "authorize_product_publish",
    "begin_product_revision",
    "canonical_hash",
    "discard_stale_product_candidate",
    "hash_artifact",
    "record_product_candidate",
    "record_product_review",
]
