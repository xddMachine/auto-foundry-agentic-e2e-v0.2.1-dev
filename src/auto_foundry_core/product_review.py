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
from typing import Any, Iterable, Mapping

from .lifecycle import RunLifecycle
from .workspace import RunContext


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GENERATION = re.compile(r"^G-[0-9]{4,}$")
_CANDIDATE_FILENAME = "product_candidate.json"
_REVIEW_FILENAME = "product_review.json"
_AUTHORIZATION_FILENAME = "publish_authorization.json"
_LOCK_FILENAME = ".product_review.lock"
_OUTPUT_NAMES = ("manifest", "fixture", "chart_map", "chart_registry", "site", "receipt")


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
            raise ValueError("artifact_bindings must contain manifest, fixture, chart_map, chart_registry, site, and receipt")
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


class ProductReviewStore:
    """Generation-scoped durable product review boundary."""

    def __init__(self, context: RunContext, generation_id: str | None = None) -> None:
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
        self.lock_path = self.root / _LOCK_FILENAME

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

    def load_candidate(self) -> ProductCandidate:
        value = ProductCandidate.from_dict(_load_json(self.candidate_path, "product candidate"))
        rebound = self._check_candidate_scope(value)
        if rebound.to_dict() != value.to_dict():
            raise ValueError("product candidate artifact bindings are stale")
        return value

    def load_review(self) -> ProductReview:
        value = ProductReview.from_dict(_load_json(self.review_path, "product review"))
        candidate = self.load_candidate()
        if value.candidate_hash != candidate.computed_hash:
            raise ValueError("product review is stale against the candidate")
        return value

    def load_authorization(self) -> PublishAuthorization:
        value = PublishAuthorization.from_dict(_load_json(self.authorization_path, "publish authorization"))
        candidate = self.load_candidate()
        review = self.load_review()
        if value.candidate_hash != candidate.computed_hash or value.review_hash != review.computed_hash:
            raise ValueError("publish authorization is stale against candidate or review")
        return value

    def _check_candidate_scope(self, candidate: ProductCandidate) -> ProductCandidate:
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
        return replace(candidate, artifact_bindings=bindings, candidate_hash=None)

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
            expected_parent_ref = f"extensions/{expected_parent}/generation_manifest.json"
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

    def discard_stale_candidate_for_rebuild(self) -> bool:
        """Delete only an exact candidate whose artifact bindings have drifted."""

        with self._locked():
            if self.review_path.exists() or self.review_path.is_symlink() or self.authorization_path.exists() or self.authorization_path.is_symlink():
                raise ValueError("cannot discard product candidate after review or authorization")
            if self.candidate_path.is_symlink():
                raise ValueError("product candidate cannot be a symlink")
            if not self.candidate_path.exists():
                return False

            candidate = ProductCandidate.from_dict(_load_json(self.candidate_path, "product candidate"))
            if candidate.run_id != self.context.run_id or candidate.generation_id != self.generation_id:
                raise ValueError("product candidate is bound to another run or generation")
            try:
                rebound = self._check_candidate_scope(candidate)
            except ValueError as error:
                if not self._is_artifact_binding_drift(error, candidate):
                    raise
                self.candidate_path.unlink()
                _fsync_directory(self.root)
                return True
            if not self._has_explicit_artifact_binding_drift(candidate, rebound):
                return False
            self.candidate_path.unlink()
            _fsync_directory(self.root)
            return True

    def record_candidate(self, candidate: ProductCandidate | Mapping[str, Any]) -> ProductCandidate:
        value = candidate if isinstance(candidate, ProductCandidate) else ProductCandidate.from_dict(candidate)
        value = self._check_candidate_scope(value)
        persisted = ProductCandidate.from_dict(value.to_dict())
        with self._locked():
            if self.candidate_path.exists() or self.candidate_path.is_symlink():
                existing = self.load_candidate()
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
    ) -> ProductReview:
        candidate = self.load_candidate()
        expected_hash = candidate.computed_hash
        if candidate_hash is not None and _sha(candidate_hash, "candidate_hash") != expected_hash:
            raise ValueError("product review candidate hash is stale")
        requested_findings = tuple(findings)
        with self._locked():
            if self.review_path.exists() or self.review_path.is_symlink():
                existing = self.load_review()
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
                candidate_ref=_relative_ref(self.context, self.candidate_path),
                candidate_hash=expected_hash,
                product_owner=candidate.product_owner,
                reviewer_ref=reviewer_ref,
                verdict=verdict,
                findings=requested_findings,
                reviewed_at=reviewed_at or datetime.now(timezone.utc).isoformat(),
            )
            persisted = ProductReview.from_dict(value.to_dict())
            _atomic_write(self.review_path, persisted.to_dict())
        return persisted

    def authorize_publish(
        self,
        *,
        publisher_ref: str,
        publication_policy: Mapping[str, Any] | None = None,
        publication_policy_hash: str | None = None,
        authorized_at: str | None = None,
    ) -> PublishAuthorization:
        candidate = self.load_candidate()
        review = self.load_review()
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
            if self.authorization_path.exists() or self.authorization_path.is_symlink():
                existing = self.load_authorization()
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
                candidate_ref=_relative_ref(self.context, self.candidate_path),
                candidate_hash=candidate.computed_hash,
                review_ref=_relative_ref(self.context, self.review_path),
                review_hash=review.computed_hash,
                publication_policy_hash=policy_hash,
                publisher_ref=publisher_ref,
                authorized_at=authorized_at or datetime.now(timezone.utc).isoformat(),
            )
            persisted = PublishAuthorization.from_dict(value.to_dict())
            _atomic_write(self.authorization_path, persisted.to_dict())
        return persisted


def record_product_candidate(context: RunContext, candidate: ProductCandidate | Mapping[str, Any], *, generation_id: str | None = None) -> ProductCandidate:
    return ProductReviewStore(context, generation_id).record_candidate(candidate)


def discard_stale_product_candidate(context: RunContext, *, generation_id: str | None = None) -> bool:
    return ProductReviewStore(context, generation_id).discard_stale_candidate_for_rebuild()


def record_product_review(context: RunContext, *, reviewer_ref: str, verdict: str, findings: Iterable[Mapping[str, Any]] = (), candidate_hash: str | None = None, generation_id: str | None = None, reviewed_at: str | None = None) -> ProductReview:
    return ProductReviewStore(context, generation_id).record_review(
        reviewer_ref=reviewer_ref, verdict=verdict, findings=findings, candidate_hash=candidate_hash, reviewed_at=reviewed_at
    )


def authorize_product_publish(context: RunContext, *, publisher_ref: str, publication_policy: Mapping[str, Any] | None = None, publication_policy_hash: str | None = None, generation_id: str | None = None, authorized_at: str | None = None) -> PublishAuthorization:
    return ProductReviewStore(context, generation_id).authorize_publish(
        publisher_ref=publisher_ref, publication_policy=publication_policy, publication_policy_hash=publication_policy_hash, authorized_at=authorized_at
    )


__all__ = [
    "ProductCandidate",
    "ProductReview",
    "PublishAuthorization",
    "ProductReviewStore",
    "authorize_product_publish",
    "canonical_hash",
    "discard_stale_product_candidate",
    "hash_artifact",
    "record_product_candidate",
    "record_product_review",
]
