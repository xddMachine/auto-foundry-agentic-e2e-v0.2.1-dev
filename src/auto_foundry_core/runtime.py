"""The normal context-bound execution path for the local core.

``CoreRuntime`` intentionally stays small: it resolves the current run's
paths, hashes deterministic inputs, checks a run-local cache, dispatches one
catalog capability, and records a receipt plus passive telemetry.  It is not a
plugin framework, policy engine, or host sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Iterable, Mapping

from .artifacts import build_manifest, write_artifact, write_manifest, hash_value
from .cache import RunCache
from .capabilities import DESCRIPTORS, execute
from .contracts import DataAssetRef, OperationReceipt, OperationResultRef, OperationSpec
from .reproduction import compare_results, reproduce
from .sources import hash_file
from .telemetry import TelemetryRecorder
from .workspace import AllowedRootError, RunContext


@dataclass(frozen=True)
class CoreExecutionResult:
    """Value, receipt, and cache outcome returned by ``CoreRuntime.execute``."""

    value: Any
    receipt: OperationReceipt
    cache_status: str


_SOURCE_CAPABILITIES = frozenset(
    {
        "sources.register",
        "sources.preview",
        "profiling.profile",
        "normalization.normalize",
        "identity.candidates",
        "relationships.measure",
        "aggregation.compute",
    }
)
_PATH_KEYS = frozenset(
    {
        "path",
        "source",
        "left_path",
        "right_path",
        "expected_path",
        "actual_path",
    }
)


def _copy_asset(value: DataAssetRef, context: RunContext) -> DataAssetRef:
    path = context.resolve_input(value.uri)
    return DataAssetRef(
        uri=str(path),
        format=value.format,
        content_hash=value.content_hash,
        size_bytes=value.size_bytes,
        metadata=value.metadata,
    )


def _resolve_tagged(value: Mapping[str, Any], context: RunContext) -> dict[str, Any]:
    result = dict(value)
    if "uri" in result:
        result["uri"] = str(context.resolve_input(result["uri"]))
    if "location" in result:
        # Result/reproduction locations are system-owned outputs.
        result["location"] = str(context.resolve_run_path(result["location"]))
    return result


def _resolve_value(value: Any, context: RunContext, *, path_strings: bool = False) -> Any:
    """Resolve explicitly path-shaped values without guessing at plain text."""

    if isinstance(value, DataAssetRef):
        return _copy_asset(value, context)
    if isinstance(value, Path):
        return context.resolve_input(value)
    if isinstance(value, Mapping):
        if "uri" in value or "location" in value:
            return _resolve_tagged(value, context)
        return {key: _resolve_value(item, context, path_strings=False) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        converted = [_resolve_value(item, context, path_strings=path_strings) for item in value]
        return tuple(converted) if isinstance(value, tuple) else converted
    if path_strings and isinstance(value, str):
        return context.resolve_input(value)
    return value


def _resolve_source(value: Any, context: RunContext) -> Any:
    if isinstance(value, str):
        return context.resolve_input(value)
    if isinstance(value, (Path, DataAssetRef, Mapping)):
        return _resolve_value(value, context, path_strings=True)
    return value


def _prepare_spec(spec: OperationSpec, context: RunContext) -> OperationSpec:
    """Resolve only path-bearing fields and inject the context read roots."""

    parameters = dict(spec.parameters)
    inputs = list(spec.inputs)
    capability_id = spec.capability_id

    if capability_id in _SOURCE_CAPABILITIES:
        if "path" in parameters:
            parameters["path"] = _resolve_source(parameters["path"], context)
        if capability_id in {"identity.candidates", "relationships.measure"}:
            for key in ("left_rows", "right_rows"):
                if key in parameters and isinstance(parameters[key], (str, Path, DataAssetRef, Mapping)):
                    parameters[key] = _resolve_source(parameters[key], context)
            for index, value in enumerate(inputs):
                if isinstance(value, (str, Path, DataAssetRef, Mapping)):
                    inputs[index] = _resolve_source(value, context)
        elif inputs and isinstance(inputs[0], (str, Path, DataAssetRef, Mapping)):
            inputs[0] = _resolve_source(inputs[0], context)
    elif capability_id == "artifacts.write":
        if "source_refs" in parameters:
            parameters["source_refs"] = [
                _resolve_source(value, context)
                for value in parameters.get("source_refs", ())
            ]
        if "filename" in parameters:
            # Validate before dispatch and before any product directory is
            # created.  Keep the original spelling in the spec for stable
            # receipts and cache keys.
            context.resolve_product_path(parameters["filename"])
    elif capability_id == "artifacts.reproduce":
        for key in ("expected", "actual"):
            if key in parameters:
                parameters[key] = _resolve_value(parameters[key], context)

    # A few callers use tagged path values in otherwise generic parameters.
    # Resolve those tags and explicitly named path fields, while leaving
    # ordinary business strings and row values untouched.
    for key, value in list(parameters.items()):
        if key in _PATH_KEYS and key != "path" and isinstance(value, (str, Path, DataAssetRef, Mapping)):
            parameters[key] = _resolve_source(value, context)
        elif isinstance(value, Mapping) and ("uri" in value or "location" in value):
            parameters[key] = _resolve_tagged(value, context)

    read_roots = tuple(str(root) for root in context.read_roots)
    parameters["allowed_roots"] = read_roots
    return OperationSpec(
        capability_id=capability_id,
        inputs=tuple(inputs),
        parameters=parameters,
        version=spec.version,
        metadata=spec.metadata,
        allowed_roots=read_roots,
    )


def _collect_hashes(value: Any, context: RunContext, found: list[str]) -> None:
    if isinstance(value, DataAssetRef):
        path = context.resolve_input(value.uri)
        found.append(hash_file(path, allowed_roots=context.read_roots))
        return
    if isinstance(value, OperationResultRef):
        path = context.resolve_run_path(value.location)
        found.append(hash_file(path, allowed_roots=(context.run_root,)))
        return
    if isinstance(value, Path):
        path = context.resolve_input(value)
        found.append(hash_file(path, allowed_roots=context.read_roots))
        return
    if isinstance(value, Mapping):
        if "uri" in value or "location" in value:
            location = value.get("uri", value.get("location"))
            if "location" in value and "uri" not in value:
                path = context.resolve_run_path(location)
                roots = (context.run_root,)
            else:
                path = context.resolve_input(location)
                roots = context.read_roots
            found.append(hash_file(path, allowed_roots=roots))
            return
        for item in value.values():
            _collect_hashes(item, context, found)
        return
    if isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            _collect_hashes(item, context, found)


def _input_hashes(spec: OperationSpec, context: RunContext) -> tuple[str, ...]:
    values: list[str] = []
    _collect_hashes(spec.inputs, context, values)
    _collect_hashes(spec.parameters, context, values)
    if not values:
        # Inline rows/IDs are deterministic inputs too.  The normalized spec
        # already captures parameters, but an explicit value hash makes the
        # receipt useful to reproduction and diagnostics.
        payload = {
            "inputs": spec.inputs,
            "parameters": {key: value for key, value in spec.parameters.items() if key != "allowed_roots"},
        }
        if spec.inputs or len(payload["parameters"]) > 1 or any(
            key in payload["parameters"] for key in ("rows", "data", "base", "eligible", "excluded")
        ):
            values.append(hash_value(payload))
    # A source path can occur in both ``inputs`` and ``parameters.path``.  It
    # is one input fact, not two; retain order while deduplicating.
    return tuple(dict.fromkeys(values))


def _output_hash(value: Any) -> str:
    if isinstance(value, OperationResultRef):
        return value.content_hash or hash_value(value)
    if isinstance(value, Mapping) and "content_hash" in value:
        return str(value["content_hash"])
    return hash_value(value)


def _result_ref(value: Any) -> OperationResultRef | None:
    if isinstance(value, OperationResultRef):
        return value
    if isinstance(value, Mapping) and "location" in value:
        try:
            return OperationResultRef.from_dict(value)
        except (TypeError, ValueError):
            return None
    return None


class CoreRuntime:
    """Execute one operation inside one immutable :class:`RunContext`."""

    def __init__(
        self,
        context: RunContext,
        cache: RunCache | None = None,
        telemetry: TelemetryRecorder | None = None,
    ) -> None:
        self.context = context
        self.telemetry = telemetry or TelemetryRecorder(context=context)
        self._validate_component_root(getattr(self.telemetry, "root", None), "telemetry")
        self.cache = cache or RunCache(context=context, telemetry=self.telemetry)
        self._validate_component_root(getattr(self.cache, "root", None), "cache")
        # Keep cache observations in the same passive recorder as operation
        # receipts.  This avoids duplicate, divergent cache fact streams.
        self.cache.telemetry = self.telemetry

    def _validate_component_root(self, root: Any, label: str) -> None:
        if root is None:
            return
        candidate = Path(root).expanduser().resolve(strict=False)
        try:
            self.context.resolve_run_path(candidate)
        except Exception as exc:
            raise AllowedRootError(f"{label} root escapes run context: {candidate}") from exc

    @property
    def product_root(self) -> Path:
        return self.context.resolve_product_path("")

    @property
    def optimizer_root(self) -> Path:
        return self.context.resolve_optimizer_path("")

    def write_artifact(self, data: Any, path: str | Path, **kwargs: Any) -> OperationResultRef:
        return write_artifact(data, path, context=self.context, **kwargs)

    def build_manifest(self, **kwargs: Any) -> dict[str, Any]:
        return build_manifest(context=self.context, **kwargs)

    def write_manifest(self, path: str | Path, manifest: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return write_manifest(path, manifest, context=self.context, **kwargs)

    def compare_results(self, expected: Any, actual: Any) -> dict[str, Any]:
        return compare_results(expected, actual, context=self.context)

    def reproduce(self, manifest: Mapping[str, Any], operation: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return reproduce(manifest, operation, *args, context=self.context, **kwargs)

    def _cacheable(self, spec: OperationSpec) -> bool:
        metadata = dict(spec.metadata)
        if metadata.get("deterministic") is False:
            return False
        return not spec.capability_id.startswith(("agent.", "model.", "judgement.", "judgment."))

    def _receipt(
        self,
        spec: OperationSpec,
        input_hashes: tuple[str, ...],
        value: Any = None,
        *,
        cache_status: str,
        duration_ms: float,
        errors: Iterable[str] = (),
        spec_hash: str | None = None,
    ) -> OperationReceipt:
        descriptor = DESCRIPTORS.get(spec.capability_id)
        output_hashes = () if errors else (_output_hash(value),)
        return OperationReceipt(
            capability_id=spec.capability_id,
            spec_hash=spec_hash or spec.spec_hash,
            input_hashes=input_hashes,
            output=_result_ref(value),
            output_hashes=output_hashes,
            backend=descriptor.backend if descriptor is not None else "python",
            duration_ms=duration_ms,
            cache_status=cache_status,
            limitations=descriptor.limitations if descriptor is not None else (),
            errors=tuple(str(error) for error in errors),
            metadata={"run_id": self.context.run_id},
        )

    def _record_receipt(self, receipt: OperationReceipt) -> None:
        try:
            self.telemetry.record_operation(receipt)
        except Exception:
            # Telemetry is passive; never hide the operation's technical fact.
            pass

    def execute(self, spec: OperationSpec | Mapping[str, Any]) -> CoreExecutionResult:
        operation = spec if isinstance(spec, OperationSpec) else OperationSpec.from_dict(spec)
        original_spec_hash = operation.spec_hash
        started = time.perf_counter()
        input_hashes: tuple[str, ...] = ()
        prepared = operation
        cache_status = "bypassed"
        try:
            if operation.capability_id not in DESCRIPTORS:
                raise KeyError(f"unknown capability: {operation.capability_id}")
            prepared = _prepare_spec(operation, self.context)
            input_hashes = _input_hashes(prepared, self.context)
            cacheable = self._cacheable(prepared)
            cache_status = "miss" if cacheable else "bypassed"
            if cacheable:
                cached = self.cache.get(prepared, input_hashes)
                if cached is not None:
                    cache_status = "hit"
                    receipt = self._receipt(
                        prepared,
                        input_hashes,
                        cached.value,
                        cache_status=cache_status,
                        duration_ms=(time.perf_counter() - started) * 1000,
                        spec_hash=original_spec_hash,
                    )
                    self._record_receipt(receipt)
                    return CoreExecutionResult(cached.value, receipt, cache_status)

            output_dir = str(self.context.resolve_product_path(""))
            value = execute(prepared, output_dir=output_dir, context=self.context)
            if cacheable:
                try:
                    self.cache.put(prepared, input_hashes, value, kind="deterministic")
                except Exception:
                    # A cache write is an optimization and must not turn a
                    # successful capability result into a technical failure.
                    cache_status = "miss"
            receipt = self._receipt(
                prepared,
                input_hashes,
                value,
                cache_status=cache_status,
                duration_ms=(time.perf_counter() - started) * 1000,
                spec_hash=original_spec_hash,
            )
            self._record_receipt(receipt)
            return CoreExecutionResult(value, receipt, cache_status)
        except Exception as exc:
            receipt = self._receipt(
                prepared,
                input_hashes,
                cache_status="error",
                duration_ms=(time.perf_counter() - started) * 1000,
                errors=(f"{type(exc).__name__}: {exc}",),
                spec_hash=original_spec_hash,
            )
            self._record_receipt(receipt)
            # Preserve the original exception type, traceback, and message;
            # the receipt is available as a diagnostic attribute when callers
            # need it, but no wrapper masks the technical path error.
            try:
                setattr(exc, "receipt", receipt)
            except Exception:
                pass
            raise


__all__ = ["CoreExecutionResult", "CoreRuntime"]
