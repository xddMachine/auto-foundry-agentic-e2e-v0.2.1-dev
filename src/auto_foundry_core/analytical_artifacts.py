"""Immutable, source-backed analytical artifact contracts.

The core runtime deliberately does not calculate business metrics or render a
dashboard.  This module provides the small, JSON-safe boundary used by
owner-authored analytical work: every artifact carries source lineage,
method/validation context, bounded outputs, and a canonical content hash.

The contracts avoid a schema framework so they remain importable in a clean
environment.  Optional analytical dependencies are used by
``analytics_toolkit`` only and are never imported here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePath
from types import MappingProxyType
from typing import Any, ClassVar, Iterable, Mapping


ANALYTICAL_ARTIFACT_SCHEMA_VERSION = "1.0"
ANALYTICAL_ARTIFACT_TYPES = frozenset(
    {
        "data_profile",
        "kpi_table",
        "segmentation_model",
        "segment_profiles",
    }
)
_SHA256_HEX_LENGTH = 64
_PATH_KEYS = frozenset(
    {
        "path",
        "location",
        "uri",
        "output_ref",
        "output_path",
        "artifact_ref",
        "artifact_path",
    }
)


class AnalyticalArtifactError(ValueError):
    """Base error raised when an analytical artifact is malformed."""


class AnalyticalArtifactValidationError(AnalyticalArtifactError):
    """Raised when a serialized artifact fails strict validation."""


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain a NUL byte")
    return value


def _finite_number(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def _path_has_escape(value: str) -> bool:
    """Return whether a path-like value contains parent traversal."""

    # ``PurePath`` honours the host separator.  Also inspect the other common
    # separator so metadata produced on Windows cannot smuggle ``..`` through
    # a POSIX consumer (and vice versa).
    for candidate in (value, value.replace("\\", "/")):
        try:
            if ".." in PurePath(candidate).parts:
                return True
        except (TypeError, ValueError):
            return True
    return False


def _normalise(value: Any, *, key: str | None = None, metadata: bool = False) -> Any:
    """Convert a value to a finite, immutable JSON-compatible representation."""

    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("artifact values must not contain NUL bytes")
            if metadata and _path_has_escape(value):
                raise ValueError(f"metadata path escapes its allowed root: {value!r}")
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _finite_number(value, key or "artifact value")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        text = str(value)
        if metadata and _path_has_escape(text):
            raise ValueError(f"metadata path escapes its allowed root: {text!r}")
        return text
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise TypeError("artifact mapping keys must be strings")
            child_metadata = metadata or key == "metadata"
            result[raw_key] = _normalise(raw_value, key=raw_key, metadata=child_metadata)
        return MappingProxyType(result)
    if isinstance(value, (tuple, list)):
        return tuple(_normalise(item, key=key, metadata=metadata) for item in value)
    if isinstance(value, (set, frozenset)):
        raise TypeError("sets are not supported in analytical artifacts; use a sorted sequence")
    # Existing core references and user-defined typed references expose a
    # stable ``to_dict`` contract.  Convert them before rejecting arbitrary
    # objects; this keeps lineage typed on input while retaining JSON output.
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _normalise(to_dict(), key=key, metadata=metadata)
    # Numpy scalar values are intentionally handled without importing numpy.
    # ``item`` is a conventional scalar conversion and does not execute user
    # code for built-in values; objects that expose it are still type-checked
    # by the recursive result.
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if converted is not value:
            return _normalise(converted, key=key, metadata=metadata)
    raise TypeError(f"unsupported non-JSON artifact value: {type(value).__name__}")


def _jsonable(value: Any) -> Any:
    """Return mutable JSON data from recursively immutable contract values."""

    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def canonical_json(value: Any) -> str:
    """Serialize finite JSON-compatible data using the artifact hash format."""

    return json.dumps(
        _jsonable(value),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_content_hash(value: Mapping[str, Any]) -> str:
    """Hash analytical content using canonical JSON.

    ``created_at`` is observational envelope metadata and is deliberately
    excluded so rerunning the same source/method/payload yields the same
    analytical identity.  ``content_hash`` and ``envelope_hash`` are excluded
    as self-referential integrity fields.
    """

    excluded = {"content_hash", "envelope_hash", "created_at"}
    payload = {str(key): item for key, item in value.items() if key not in excluded}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _normalise_texts(values: Iterable[Any] | None, label: str) -> tuple[str, ...]:
    if values is None:
        values = ()
    elif isinstance(values, (str, bytes)):
        values = (values,)
    result = tuple(_required_text(value, label) for value in values)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicates")
    return result


def _normalise_sha(value: Any, label: str) -> str:
    text = _required_text(value, label).lower()
    if len(text) != _SHA256_HEX_LENGTH or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} must be a SHA-256 hex digest")
    return text


def _normalise_sources(values: Iterable[Any] | None) -> tuple[Any, ...]:
    if values is None:
        values = ()
    elif isinstance(values, (str, Path)):
        values = (values,)
    result: list[Any] = []
    for value in values or ():
        if isinstance(value, (str, Path)):
            text = _required_text(str(value), "source_ref")
            if _path_has_escape(text):
                raise ValueError(f"source_ref path escapes its allowed root: {text!r}")
            result.append(text)
            continue
        normalised = _normalise(value, key="source_ref")
        if isinstance(normalised, Mapping):
            for path_key in _PATH_KEYS:
                path_value = normalised.get(path_key)
                if isinstance(path_value, str) and _path_has_escape(path_value):
                    raise ValueError(f"source_ref path escapes its allowed root: {path_value!r}")
        result.append(normalised)
    return tuple(result)


def _sequence(value: Any) -> tuple[Any, ...]:
    """Treat one mapping/scalar as one item instead of iterating its keys."""

    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping, Path)):
        return (value,)
    return tuple(value)


def _validate_path_refs(values: Any, label: str) -> Any:
    """Reject parent traversal in output/reference paths after normalization."""

    if isinstance(values, str):
        if _path_has_escape(values):
            raise ValueError(f"{label} path escapes its allowed root: {values!r}")
        return values
    if isinstance(values, Mapping):
        for key in _PATH_KEYS:
            candidate = values.get(key)
            if isinstance(candidate, str) and _path_has_escape(candidate):
                raise ValueError(f"{label} path escapes its allowed root: {candidate!r}")
        return values
    if isinstance(values, tuple):
        return tuple(_validate_path_refs(item, label) for item in values)
    if isinstance(values, list):
        return [_validate_path_refs(item, label) for item in values]
    return values


def _mapping(value: Any, label: str, *, metadata: bool = False) -> Mapping[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    normalised = _normalise(value, key=label, metadata=metadata)
    if not isinstance(normalised, Mapping):  # pragma: no cover - defensive
        raise TypeError(f"{label} must be a mapping")
    return normalised


@dataclass(frozen=True, init=False)
class AnalyticalArtifact:
    """Schema-versioned immutable analytical artifact.

    ``payload`` is the typed body while the surrounding fields provide a
    common lineage/validation envelope.  Known payload kinds have convenience
    subclasses below; unknown future kinds remain valid as long as they use a
    non-empty type name and finite JSON values.  ``content_hash`` identifies
    analytical content and excludes observational ``created_at``; the separate
    ``envelope_hash`` binds that timestamp so timestamp edits cannot pass
    strict deserialization as if they were an unchanged envelope.
    """

    artifact_id: str
    artifact_type: str
    schema_version: str
    requirement_id: str
    dataset_fingerprint: str
    source_fingerprints: Mapping[str, str]
    source_refs: tuple[Any, ...]
    population: Any
    grain: str
    period: Any
    feature_definitions: tuple[Any, ...]
    metric_definitions: tuple[Any, ...]
    method: str
    parameters: Mapping[str, Any]
    random_seed: int | None
    validation_evidence: Any
    tables: tuple[Any, ...]
    output_refs: tuple[Any, ...]
    findings: tuple[Any, ...]
    visualization_intents: tuple[Any, ...]
    limitations: tuple[str, ...]
    created_at: str
    payload: Mapping[str, Any]
    metadata: Mapping[str, Any]
    content_hash: str
    envelope_hash: str

    # Strict wire fields.  ``type`` is retained as a concise interoperable
    # alias for callers that use the common artifact envelope vocabulary.
    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema_version",
            "artifact_id",
            "artifact_type",
            "type",
            "requirement_id",
            "dataset_fingerprint",
            "source_fingerprints",
            "source_refs",
            "population",
            "grain",
            "period",
            "feature_definitions",
            "metric_definitions",
            "method",
            "parameters",
            "random_seed",
            "validation_evidence",
            "tables",
            "output_refs",
            "findings",
            "visualization_intents",
            "limitations",
            "created_at",
            "payload",
            "metadata",
            "content_hash",
            "envelope_hash",
        }
    )

    def __init__(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        requirement_id: str,
        dataset_fingerprint: str,
        source_refs: Iterable[Any] = (),
        population: Any = None,
        grain: str = "row",
        period: Any = "unspecified",
        feature_definitions: Iterable[Any] = (),
        metric_definitions: Iterable[Any] = (),
        method: str = "unspecified",
        parameters: Mapping[str, Any] | None = None,
        random_seed: int | None = None,
        validation_evidence: Any = None,
        tables: Iterable[Any] = (),
        output_refs: Iterable[Any] = (),
        findings: Iterable[Any] = (),
        visualization_intents: Iterable[Any] = (),
        limitations: Iterable[Any] = (),
        created_at: str | datetime | None = None,
        payload: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        source_fingerprints: Mapping[str, str] | None = None,
        schema_version: str = ANALYTICAL_ARTIFACT_SCHEMA_VERSION,
        content_hash: str | None = None,
        envelope_hash: str | None = None,
    ) -> None:
        artifact_id = _required_text(artifact_id, "artifact_id")
        artifact_type = _required_text(artifact_type, "artifact_type")
        schema_version = _required_text(schema_version, "schema_version")
        if schema_version != ANALYTICAL_ARTIFACT_SCHEMA_VERSION:
            raise ValueError(f"unsupported analytical artifact schema version: {schema_version!r}")
        requirement_id = _required_text(requirement_id, "requirement_id")
        dataset_fingerprint = _required_text(dataset_fingerprint, "dataset_fingerprint")
        if source_fingerprints is None:
            source_fingerprints = {}
        if not isinstance(source_fingerprints, Mapping):
            raise TypeError("source_fingerprints must be a mapping")
        fingerprints: dict[str, str] = {}
        for key, value in source_fingerprints.items():
            fingerprints[_required_text(str(key), "source_fingerprint key")] = _required_text(value, "source_fingerprint")
        if not fingerprints:
            fingerprints = {"dataset": dataset_fingerprint}
        grain = _required_text(grain, "grain")
        method = _required_text(method, "method")
        if random_seed is not None:
            if isinstance(random_seed, bool) or not isinstance(random_seed, int):
                raise TypeError("random_seed must be an integer or None")
        if created_at is None:
            created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        elif isinstance(created_at, (datetime, date)):
            created_at = created_at.isoformat()
        created_at = _required_text(created_at, "created_at")

        normalised_values: dict[str, Any] = {
            "population": _normalise(population, key="population"),
            "period": _normalise(period, key="period"),
            "feature_definitions": _normalise(_sequence(feature_definitions), key="feature_definitions"),
            "metric_definitions": _normalise(_sequence(metric_definitions), key="metric_definitions"),
            "parameters": _mapping(parameters, "parameters"),
            "validation_evidence": _normalise(validation_evidence, key="validation_evidence"),
            "tables": _normalise(_sequence(tables), key="tables"),
            "output_refs": _validate_path_refs(_normalise(_sequence(output_refs), key="output_refs"), "output_ref"),
            "findings": _normalise(_sequence(findings), key="findings"),
            "visualization_intents": _normalise(_sequence(visualization_intents), key="visualization_intents"),
            "payload": _mapping(payload, "payload"),
            "metadata": _mapping(metadata, "metadata", metadata=True),
        }
        limitations_tuple = _normalise_texts(limitations, "limitations")
        source_refs_tuple = _normalise_sources(source_refs)
        fingerprint_proxy = MappingProxyType(dict(fingerprints))

        for name, value in (
            ("artifact_id", artifact_id),
            ("artifact_type", artifact_type),
            ("schema_version", schema_version),
            ("requirement_id", requirement_id),
            ("dataset_fingerprint", dataset_fingerprint),
            ("source_fingerprints", fingerprint_proxy),
            ("source_refs", source_refs_tuple),
            ("grain", grain),
            ("method", method),
            ("random_seed", random_seed),
            ("created_at", created_at),
            ("limitations", limitations_tuple),
            *normalised_values.items(),
        ):
            object.__setattr__(self, name, value)

        expected = self._compute_content_hash()
        if content_hash is not None and _normalise_sha(content_hash, "content_hash") != expected:
            raise AnalyticalArtifactValidationError("content_hash does not match canonical artifact content")
        object.__setattr__(self, "content_hash", expected)
        expected_envelope = self._compute_envelope_hash()
        if envelope_hash is not None and _normalise_sha(envelope_hash, "envelope_hash") != expected_envelope:
            raise AnalyticalArtifactValidationError("envelope_hash does not match artifact content and created_at")
        object.__setattr__(self, "envelope_hash", expected_envelope)

    def _wire_dict_without_hash(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "type": self.artifact_type,
            "requirement_id": self.requirement_id,
            "dataset_fingerprint": self.dataset_fingerprint,
            "source_fingerprints": self.source_fingerprints,
            "source_refs": self.source_refs,
            "population": self.population,
            "grain": self.grain,
            "period": self.period,
            "feature_definitions": self.feature_definitions,
            "metric_definitions": self.metric_definitions,
            "method": self.method,
            "parameters": self.parameters,
            "random_seed": self.random_seed,
            "validation_evidence": self.validation_evidence,
            "tables": self.tables,
            "output_refs": self.output_refs,
            "findings": self.findings,
            "visualization_intents": self.visualization_intents,
            "limitations": self.limitations,
            "created_at": self.created_at,
            "payload": self.payload,
            "metadata": self.metadata,
        }

    def _compute_content_hash(self) -> str:
        return canonical_content_hash(self._wire_dict_without_hash())

    def _compute_envelope_hash(self) -> str:
        payload = {**self._wire_dict_without_hash(), "content_hash": self.content_hash}
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    @property
    def canonical_hash(self) -> str:
        return self.content_hash

    @property
    def type(self) -> str:
        """Concise alias for the wire ``type`` discriminator."""

        return self.artifact_type

    def __hash__(self) -> int:
        """Hash immutable artifacts by their canonical content fingerprint."""

        return hash(self.content_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            **_jsonable(self._wire_dict_without_hash()),
            "content_hash": self.content_hash,
            "envelope_hash": self.envelope_hash,
        }

    def to_json(self, *, sort_keys: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=sort_keys, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_json(cls, text: str) -> "AnalyticalArtifact":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AnalyticalArtifactValidationError("artifact JSON is invalid") from exc
        return cls.from_dict(value)

    @classmethod
    def _from_dict_generic(cls, data: Mapping[str, Any]) -> "AnalyticalArtifact":
        if not isinstance(data, Mapping):
            raise TypeError("AnalyticalArtifact.from_dict expects a mapping")
        unknown = set(data) - cls._WIRE_FIELDS
        if unknown:
            raise AnalyticalArtifactValidationError(f"unknown analytical artifact fields: {sorted(unknown)!r}")
        required = {
            "schema_version",
            "artifact_id",
            "requirement_id",
            "dataset_fingerprint",
            "source_fingerprints",
            "source_refs",
            "population",
            "grain",
            "period",
            "feature_definitions",
            "metric_definitions",
            "method",
            "parameters",
            "random_seed",
            "validation_evidence",
            "tables",
            "output_refs",
            "findings",
            "visualization_intents",
            "limitations",
            "created_at",
            "payload",
            "metadata",
            "content_hash",
            "envelope_hash",
        }
        missing = required - set(data)
        if missing:
            raise AnalyticalArtifactValidationError(f"missing analytical artifact fields: {sorted(missing)!r}")
        artifact_type = data.get("artifact_type", data.get("type"))
        if artifact_type is None:
            raise AnalyticalArtifactValidationError("artifact requires artifact_type or type")
        if "artifact_type" in data and "type" in data and data["artifact_type"] != data["type"]:
            raise AnalyticalArtifactValidationError("artifact_type and type must agree")
        kwargs = dict(data)
        kwargs.pop("type", None)
        kwargs["artifact_type"] = artifact_type
        if cls is not AnalyticalArtifact:
            return cls(**kwargs)
        return cls(**kwargs)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnalyticalArtifact":
        # Dispatching from the base class keeps round-trips typed while still
        # allowing a generic future artifact kind to use AnalyticalArtifact.
        if cls is AnalyticalArtifact and isinstance(data, Mapping):
            artifact_type = data.get("artifact_type", data.get("type"))
            target = {
                "data_profile": DataProfileArtifact,
                "kpi_table": KpiTableArtifact,
                "segmentation_model": SegmentationModelArtifact,
                "segment_profiles": SegmentProfilesArtifact,
            }.get(artifact_type)
            if target is not None:
                return target.from_dict(data)
        return cls._from_dict_generic(data)

    def _constructor_kwargs(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "requirement_id": self.requirement_id,
            "dataset_fingerprint": self.dataset_fingerprint,
            "source_fingerprints": dict(self.source_fingerprints),
            "source_refs": self.source_refs,
            "population": self.population,
            "grain": self.grain,
            "period": self.period,
            "feature_definitions": self.feature_definitions,
            "metric_definitions": self.metric_definitions,
            "method": self.method,
            "parameters": self.parameters,
            "random_seed": self.random_seed,
            "validation_evidence": self.validation_evidence,
            "tables": self.tables,
            "output_refs": self.output_refs,
            "findings": self.findings,
            "visualization_intents": self.visualization_intents,
            "limitations": self.limitations,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "envelope_hash": self.envelope_hash,
        }


class _TypedArtifact(AnalyticalArtifact):
    """Internal helper for typed payload subclasses."""

    _EXPECTED_TYPE: ClassVar[str]
    _PAYLOAD_KEY: ClassVar[str]

    def __init__(self, *, payload_value: Any = None, **kwargs: Any) -> None:
        payload = kwargs.pop("payload", None)
        if payload is not None and payload_value is not None:
            raise TypeError("provide either payload_value or payload, not both")
        if payload is None:
            payload = {self._PAYLOAD_KEY: payload_value}
        elif self._PAYLOAD_KEY not in payload:
            payload = {self._PAYLOAD_KEY: payload, **dict(payload)}
        super().__init__(artifact_type=self._EXPECTED_TYPE, payload=payload, **kwargs)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "_TypedArtifact":
        base = AnalyticalArtifact._from_dict_generic(data)
        if base.artifact_type != cls._EXPECTED_TYPE:
            raise AnalyticalArtifactValidationError(
                f"expected {cls._EXPECTED_TYPE!r} artifact, got {base.artifact_type!r}"
            )
        if cls._PAYLOAD_KEY not in base.payload:
            raise AnalyticalArtifactValidationError(f"{cls._EXPECTED_TYPE} payload missing {cls._PAYLOAD_KEY!r}")
        kwargs = base._constructor_kwargs()
        # Preserve the complete payload (typed subclasses may carry more than
        # one body key, such as a segmentation model plus segment profiles).
        # Their convenience constructor accepts ``payload`` through the
        # internal helper when a wire representation is being restored.
        kwargs["payload"] = dict(base.payload)
        return cls(**kwargs)

    @property
    def value(self) -> Any:
        return self.payload[self._PAYLOAD_KEY]


@dataclass(frozen=True, init=False)
class DataProfileArtifact(_TypedArtifact):
    """Typed ``data_profile`` artifact."""

    _EXPECTED_TYPE: ClassVar[str] = "data_profile"
    _PAYLOAD_KEY: ClassVar[str] = "profile"
    __hash__ = AnalyticalArtifact.__hash__

    def __init__(self, *, profile: Any = None, **kwargs: Any) -> None:
        super().__init__(payload_value=profile, **kwargs)

    @property
    def profile(self) -> Any:
        return self.value


@dataclass(frozen=True, init=False)
class KpiTableArtifact(_TypedArtifact):
    """Typed ``kpi_table`` artifact."""

    _EXPECTED_TYPE: ClassVar[str] = "kpi_table"
    _PAYLOAD_KEY: ClassVar[str] = "rows"
    __hash__ = AnalyticalArtifact.__hash__

    def __init__(self, *, rows: Any = None, **kwargs: Any) -> None:
        if "payload" in kwargs:
            super().__init__(**kwargs)
        else:
            super().__init__(payload_value=rows if rows is not None else (), **kwargs)

    @property
    def rows(self) -> Any:
        return self.value


@dataclass(frozen=True, init=False)
class SegmentationModelArtifact(_TypedArtifact):
    """Typed ``segmentation_model`` artifact with model and profile payload."""

    _EXPECTED_TYPE: ClassVar[str] = "segmentation_model"
    _PAYLOAD_KEY: ClassVar[str] = "model"
    __hash__ = AnalyticalArtifact.__hash__

    def __init__(self, *, model: Any = None, segment_profiles: Any = None, **kwargs: Any) -> None:
        if segment_profiles is not None:
            payload = {"model": model, "segment_profiles": segment_profiles}
            super().__init__(payload=payload, **kwargs)
        else:
            super().__init__(payload_value=model, **kwargs)

    @property
    def model(self) -> Any:
        return self.payload.get("model")

    @property
    def segment_profiles(self) -> Any:
        return self.payload.get("segment_profiles", ())


@dataclass(frozen=True, init=False)
class SegmentProfilesArtifact(_TypedArtifact):
    """Typed ``segment_profiles`` artifact for profile-only publication."""

    _EXPECTED_TYPE: ClassVar[str] = "segment_profiles"
    _PAYLOAD_KEY: ClassVar[str] = "profiles"
    __hash__ = AnalyticalArtifact.__hash__

    def __init__(self, *, profiles: Any = None, **kwargs: Any) -> None:
        # ``_TypedArtifact.from_dict`` restores the complete payload mapping
        # so future fields remain intact.  Mirror ``KpiTableArtifact`` here:
        # when that wire payload is present, pass it through unchanged;
        # otherwise use the convenience ``profiles`` value.
        if "payload" in kwargs:
            super().__init__(**kwargs)
        else:
            super().__init__(payload_value=profiles if profiles is not None else (), **kwargs)

    @property
    def profiles(self) -> Any:
        return self.value


def artifact_from_dict(data: Mapping[str, Any]) -> AnalyticalArtifact:
    """Deserialize and dispatch a serialized artifact to its typed class."""

    return AnalyticalArtifact.from_dict(data)


__all__ = [
    "ANALYTICAL_ARTIFACT_SCHEMA_VERSION",
    "ANALYTICAL_ARTIFACT_TYPES",
    "AnalyticalArtifact",
    "AnalyticalArtifactError",
    "AnalyticalArtifactValidationError",
    "DataProfileArtifact",
    "KpiTableArtifact",
    "SegmentProfilesArtifact",
    "SegmentationModelArtifact",
    "artifact_from_dict",
    "canonical_content_hash",
    "canonical_json",
]
