"""Capability metadata and the small local execution backend."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Mapping

from .contracts import CapabilityDescriptor, DataAssetRef, IdentityCandidate, IdentityDecision, OperationResultRef, OperationSpec
from .references import decode_explicit_reference, is_data_asset_mapping, is_explicit_reference_mapping
from .workspace import RunContext, require_allowed_roots


CapabilityHandler = Callable[[OperationSpec, str | None, RunContext | None], Any]


def _allowed_roots(spec: OperationSpec):
    parameters = dict(spec.parameters)
    roots = spec.allowed_roots or parameters.get("allowed_roots")
    if roots is None and isinstance(spec.metadata, Mapping):
        roots = spec.metadata.get("allowed_roots")
    return roots


def _required_roots(spec: OperationSpec, *, context: str = "catalog filesystem operation"):
    return require_allowed_roots(_allowed_roots(spec), context=context)


def _read_rows(
    value: Any,
    *,
    limit: int | None = None,
    offset: int = 0,
    table: str | None = None,
    max_json_bytes: int | None = None,
    parquet_batch_size: int | None = None,
    allowed_roots=None,
) -> list[dict[str, Any]]:
    read_options: dict[str, Any] = {"limit": limit, "offset": offset}
    if table is not None:
        read_options["table"] = table
    if max_json_bytes is not None:
        read_options["max_json_bytes"] = max_json_bytes
    if parquet_batch_size is not None:
        read_options["parquet_batch_size"] = parquet_batch_size
    if isinstance(value, list):
        return [dict(v) for v in value]
    if isinstance(value, tuple):
        return [dict(v) for v in value]
    if isinstance(value, Mapping):
        if is_explicit_reference_mapping(value):
            decoded = decode_explicit_reference(value)
            if not isinstance(decoded, DataAssetRef):
                raise TypeError("source rows require a data_asset reference")
            value = decoded
        elif "rows" in value:
            return [dict(v) for v in value["rows"]]
    read_options["allowed_roots"] = require_allowed_roots(allowed_roots)
    if isinstance(value, Mapping) and is_data_asset_mapping(value):
        from .sources import read_rows
        return read_rows(DataAssetRef.from_dict(value), **read_options)
    if isinstance(value, (str, Path, DataAssetRef)):
        from .sources import read_rows
        return read_rows(value, **read_options)
    raise TypeError("rows or a local source path is required")


def _source(spec: OperationSpec) -> Any:
    params = dict(spec.parameters)
    if "path" in params:
        value = params["path"]
        if isinstance(value, Mapping) and is_explicit_reference_mapping(value):
            value = decode_explicit_reference(value)
            if not isinstance(value, DataAssetRef):
                raise TypeError("source path requires a data_asset reference")
        elif isinstance(value, Mapping) and is_data_asset_mapping(value):
            return DataAssetRef.from_dict(value)
        return value
    if spec.inputs:
        value = spec.inputs[0]
        if isinstance(value, Mapping) and is_explicit_reference_mapping(value):
            value = decode_explicit_reference(value)
            if not isinstance(value, DataAssetRef):
                raise TypeError("source path requires a data_asset reference")
        elif isinstance(value, Mapping) and is_data_asset_mapping(value):
            return DataAssetRef.from_dict(value)
        return value
    raise ValueError("operation requires a source path/input")


def _register(spec: OperationSpec, output_dir: str | None, context: RunContext | None = None) -> Any:
    from .sources import register_source
    return register_source(_source(spec), allowed_roots=_required_roots(spec, context="sources.register"))


def _preview(spec: OperationSpec, output_dir: str | None, context: RunContext | None = None) -> Any:
    from .sources import preview
    parameters = dict(spec.parameters)
    return preview(
        _source(spec),
        limit=int(parameters.get("limit", 20)),
        max_json_bytes=int(parameters.get("max_json_bytes", 16 * 1024 * 1024)),
        parquet_batch_size=int(parameters.get("parquet_batch_size", 1024)),
        allowed_roots=_required_roots(spec, context="sources.preview"),
    )


def _profile(spec: OperationSpec, output_dir: str | None, context: RunContext | None = None) -> Any:
    from .profiling import profile_source
    parameters = dict(spec.parameters)
    return profile_source(_source(spec), sample_limit=int(parameters.get("sample_limit", 1000)), frequency_limit=int(parameters.get("frequency_limit", 20)), allowed_roots=_required_roots(spec, context="profiling.profile"))


def _normalize(spec: OperationSpec, output_dir: str | None, context: RunContext | None = None) -> Any:
    from .normalization import normalize_rows
    p = dict(spec.parameters)
    rows = p.get("rows")
    if rows is None:
        rows = _read_rows(_source(spec), limit=p.get("limit"), allowed_roots=_required_roots(spec, context="normalization.normalize"))
    return normalize_rows(
        rows,
        fields=p.get("fields"),
        case=p.get("case", "lower"),
        date_formats=p.get("date_formats"),
        formats=p.get("formats"),
        return_metadata=bool(p.get("return_metadata", True)),
    )


def _identity_candidates(spec: OperationSpec, output_dir: str | None, context: RunContext | None = None) -> Any:
    from .identity import generate_candidates
    p = dict(spec.parameters)
    left = p.get("left_rows")
    right = p.get("right_rows")
    if left is None and spec.inputs:
        left = spec.inputs[0]
    if right is None and len(spec.inputs) > 1:
        right = spec.inputs[1]
    roots = _required_roots(spec, context="identity.candidates") if _is_path_reference(left, strings_are_paths=True) or _is_path_reference(right, strings_are_paths=True) else None
    return [candidate.to_dict() for candidate in generate_candidates(_read_rows(left, allowed_roots=roots), _read_rows(right, allowed_roots=roots), object_type=p.get("object_type", "object"), compare_fields=p.get("compare_fields"), threshold=float(p.get("threshold", 0.55)), max_candidates=p.get("max_candidates"))]


def _identity_apply(spec: OperationSpec, output_dir: str | None, context: RunContext | None = None) -> Any:
    from .identity import apply_decision
    p = dict(spec.parameters)
    candidate = IdentityCandidate.from_dict(p.get("candidate") or spec.inputs[0])
    decision = IdentityDecision.from_dict(p.get("decision") or spec.inputs[1])
    result = apply_decision(candidate, decision, canonical_id=p.get("canonical_id"), rows=p.get("rows"), id_field=p.get("id_field"))
    return result.to_dict() if hasattr(result, "to_dict") else {"mapping": result["mapping"].to_dict(), "rows": result["rows"], "raw_preserved": True}


def _relationship(spec: OperationSpec, output_dir: str | None, context: RunContext | None = None) -> Any:
    from .relationships import measure_relationship
    p = dict(spec.parameters)
    left = p.get("left_rows", spec.inputs[0] if spec.inputs else None)
    right = p.get("right_rows", spec.inputs[1] if len(spec.inputs) > 1 else None)
    roots = _required_roots(spec, context="relationships.measure") if _is_path_reference(left, strings_are_paths=True) or _is_path_reference(right, strings_are_paths=True) else None
    return measure_relationship(
        _read_rows(
            left,
            limit=p.get("left_limit"),
            max_json_bytes=p.get("max_json_bytes"),
            parquet_batch_size=p.get("parquet_batch_size"),
            allowed_roots=roots,
        ),
        _read_rows(
            right,
            limit=p.get("right_limit"),
            max_json_bytes=p.get("max_json_bytes"),
            parquet_batch_size=p.get("parquet_batch_size"),
            allowed_roots=roots,
        ),
        left_key=p["left_key"],
        right_key=p.get("right_key"),
        left_time_field=p.get("left_time_field"),
        right_time_field=p.get("right_time_field"),
        date_formats=p.get("date_formats"),
        left_date_formats=p.get("left_date_formats"),
        right_date_formats=p.get("right_date_formats"),
        sample_limit=int(p.get("sample_limit", 20)),
    )


def _population(spec: OperationSpec, output_dir: str | None, context: RunContext | None = None) -> Any:
    from .populations import PopulationLedger
    p = dict(spec.parameters)
    if "base_ids" in p and "base" not in p:
        p["base"] = p.pop("base_ids")
    return PopulationLedger(**p).reconcile()


def _aggregation(spec: OperationSpec, output_dir: str | None, context: RunContext | None = None) -> Any:
    from .aggregation import aggregate_rows
    p = dict(spec.parameters)
    # Execution context roots are consumed by the reader, not by the
    # aggregation contract itself.
    p.pop("allowed_roots", None)
    rows = p.pop("rows", None)
    if rows is None:
        rows = _read_rows(_source(spec), allowed_roots=_required_roots(spec, context="aggregation.compute"))
    operation = p.pop("operation", p.pop("aggregation", "count"))
    return aggregate_rows(rows, operation, **p)


def _artifact(spec: OperationSpec, output_dir: str | None, context: RunContext | None = None) -> Any:
    from .artifacts import write_artifact
    p = dict(spec.parameters)
    data = p.pop("data", None)
    if data is None and spec.inputs:
        data = spec.inputs[0]
    if data is None:
        raise ValueError("artifacts.write requires data")
    filename = str(p.pop("filename", "result.json"))
    path = (context.resolve_product_path(filename) if context is not None else Path(output_dir or ".") / filename)
    p.setdefault("allowed_roots", _required_roots(spec, context="artifacts.write"))
    result = write_artifact(data, path, operation_spec=spec, context=context, **p)
    # Keep the public result typed.  CoreRuntime/cache/receipt serialization
    # tags the object explicitly, while the CLI can still render ``to_dict``.
    return result


def _reproduce(spec: OperationSpec, output_dir: str | None, context: RunContext | None = None) -> Any:
    from .reproduction import compare_results
    p = dict(spec.parameters)
    expected, actual = p.get("expected"), p.get("actual")
    roots = _required_roots(spec, context="artifacts.reproduce") if _is_path_reference(expected) or _is_path_reference(actual) else None
    return compare_results(expected, actual, allowed_roots=roots, context=context)


def _descriptor(
    capability_id: str,
    purpose: str,
    when_to_use: str,
    when_not_to_use: str,
    input_contract: Mapping[str, Any],
    output_contract: Mapping[str, Any],
    handler: CapabilityHandler,
    *,
    limitations: tuple[str, ...] = (),
    cache_behavior: str = "content-addressed when inputs are deterministic",
    examples: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
) -> tuple[CapabilityDescriptor, CapabilityHandler]:
    capability_examples = examples or (f"Run {capability_id} with its declared input contract and a bounded local fixture.",)
    capability_limitations = tuple(limitations) + ("Does not decide semantic meaning; it returns only the declared deterministic result.",)
    descriptor = CapabilityDescriptor(
        capability_id=capability_id,
        version="0.1.0",
        purpose=purpose,
        when_to_use=when_to_use,
        when_not_to_use=when_not_to_use,
        input_contract=input_contract,
        output_contract=output_contract,
        limitations=capability_limitations,
        side_effects=("Writes only explicitly requested derived outputs." if capability_id.startswith("artifacts.") else "Read-only source access.",),
        cache_behavior=cache_behavior,
        examples=capability_examples,
        backend="python-local",
        handler=handler.__name__,
        metadata=metadata or {},
    )
    return descriptor, handler


_PAIRS = [
    _descriptor("sources.register", "Register a local source and content hash.", "You need an immutable source reference.", "Do not use for remote access or source mutation.", {"path": "local path"}, {"type": "DataAssetRef"}, _register, examples=("register_source(path)",)),
    _descriptor(
        "sources.preview",
        "Read a bounded source preview and discovered columns.",
        "You need quick source orientation.",
        "Do not use as a complete unbounded read.",
        {"source": "DataAssetRef or local path", "limit": "integer"},
        {"type": "preview mapping"},
        _preview,
        examples=("preview(source, limit=20)",),
        metadata={
            "bounded_formats": {
                "csv": "streamed rows; only returned slice is materialized",
                "tsv": "streamed rows; only returned slice is materialized",
                "jsonl": "streamed lines; only returned slice is materialized",
                "xlsx": "openpyxl read-only row iterator; only returned slice is materialized",
                "parquet": "pyarrow ParquetFile.iter_batches; bounded batches and returned slice",
                "json": "whole document materialized only after max_json_bytes boundary",
            },
            "unsupported_formats": ("xls",),
            "json_materialization_default_max_bytes": 16 * 1024 * 1024,
        },
    ),
    _descriptor("profiling.profile", "Produce bounded schema and value diagnostics.", "You need question-supporting source facts.", "Do not treat this as a semantic quality or business decision.", {"source": "DataAssetRef or local path"}, {"type": "profile mapping"}, _profile, examples=("profile_source(source, sample_limit=1000)",)),
    _descriptor("normalization.normalize", "Add provenance-preserving normalized representations.", "Formatting and parse preparation is needed.", "Do not overwrite raw values or infer identity.", {"rows": "sequence of mappings", "fields": "field to kind", "date_formats": "explicit field formats"}, {"type": "rows plus lineage"}, _normalize, examples=("normalize_rows(rows, fields={'code': 'identifier'})",)),
    _descriptor("identity.candidates", "Generate object-generic identity evidence and contradictions.", "Reviewed identity work needs deterministic candidate facts.", "Do not use a similarity score as an automatic merge.", {"left_rows": "mappings", "right_rows": "mappings"}, {"type": "IdentityCandidate list"}, _identity_candidates, limitations=("Semantic identity decisions remain outside this capability.",), examples=("generate_candidates(left_rows, right_rows, compare_fields=['label'])",)),
    _descriptor("identity.apply-decision", "Apply an explicit identity decision to a derived mapping.", "A reviewed decision is available.", "Do not call without a reviewed semantic decision.", {"candidate": "IdentityCandidate", "decision": "IdentityDecision"}, {"type": "CanonicalMapping and optional derived rows"}, _identity_apply, examples=("apply_decision(candidate, reviewed_decision)",)),
    _descriptor("relationships.measure", "Measure generic key overlap, coverage, cardinality and full matched-set temporal diagnostics with a bounded pair sample.", "You need relationship evidence.", "Do not infer business meaning from diagnostics alone.", {"left_rows": "mappings", "right_rows": "mappings", "left_key": "field", "date_formats": "explicit formats"}, {"type": "relationship diagnostic mapping"}, _relationship, examples=("measure_relationship(left, right, left_key='key')",), metadata={"search_terms": ("join", "coverage", "overlap"), "temporal_scope": "full matched set; sample_pairs is bounded"}),
    _descriptor("populations.reconcile", "Reconcile base IDs, eligible, excluded and unresolved accounting exactly once.", "A requirement-scoped population ledger is needed.", "Do not pass an integer count as base; ID reconciliation is required.", {"base": "iterable of IDs", "excluded": "reason to IDs"}, {"type": "reconciliation mapping"}, _population, examples=("PopulationLedger(base_ids).reconcile()",)),
    _descriptor("aggregation.compute", "Compute generic count, distinct, numeric, grouping, ranking and period operations.", "Typed generic aggregation fits the task.", "Do not use for domain recipes or cross-currency conversion.", {"rows": "mappings", "operation": "AggregationSpec operation"}, {"type": "scalar/list/mapping"}, _aggregation, examples=("aggregate_rows(rows, 'sum', value_field='amount')",)),
    _descriptor("artifacts.write", "Write a deterministic derived artifact and result hash.", "A generic local output is required.", "Do not mutate raw sources.", {"data": "JSON-compatible data", "filename": "relative output name"}, {"type": "OperationResultRef"}, _artifact, cache_behavior="Result can be cached when source/spec hashes are stable.", examples=("write_artifact(rows, output_path)",)),
    _descriptor("artifacts.reproduce", "Compare expected and actual deterministic result fingerprints.", "A prior manifest/result needs reproduction evidence.", "Do not use for lifecycle state or semantic review.", {"expected": "result", "actual": "result"}, {"type": "comparison mapping"}, _reproduce, examples=("compare_results(expected, actual)",)),
]

DESCRIPTORS: dict[str, CapabilityDescriptor] = {descriptor.capability_id: descriptor for descriptor, _ in _PAIRS}
HANDLERS: dict[str, CapabilityHandler] = {descriptor.capability_id: handler for descriptor, handler in _PAIRS}


def descriptors() -> tuple[CapabilityDescriptor, ...]:
    return tuple(DESCRIPTORS[key] for key in sorted(DESCRIPTORS))


def _is_path_reference(value: Any, *, strings_are_paths: bool = False) -> bool:
    """Identify explicit path references without guessing from string syntax.

    Source arguments are strings by contract, hence ``strings_are_paths`` is
    used only by source-backed identity/relationship operations.  Reproduction
    values require an explicit ``Path``, ref contract, or tagged mapping.
    """

    if isinstance(value, str):
        return strings_are_paths
    if isinstance(value, (Path, DataAssetRef, OperationResultRef)):
        return True
    if isinstance(value, Mapping):
        if is_explicit_reference_mapping(value):
            decode_explicit_reference(value)  # validate malformed/unknown tags before dispatch
            return True
        # Do not apply ``strings_are_paths`` to arbitrary row fields.  Only a
        # nested actual/explicit reference should make a collection path-like.
        return any(_is_path_reference(item, strings_are_paths=False) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_is_path_reference(item, strings_are_paths=False) for item in value)
    return False


def execute(spec: OperationSpec | Mapping[str, Any], *, output_dir: str | None = None, allowed_roots=None, context: RunContext | None = None) -> Any:
    operation = spec if isinstance(spec, OperationSpec) else OperationSpec.from_dict(spec)
    if allowed_roots is not None:
        parameters = dict(operation.parameters)
        parameters["allowed_roots"] = tuple(allowed_roots)
        operation = OperationSpec(operation.capability_id, inputs=operation.inputs, parameters=parameters, version=operation.version, metadata=operation.metadata, allowed_roots=tuple(str(value) for value in allowed_roots))
    try:
        handler = HANDLERS[operation.capability_id]
    except KeyError as exc:
        raise KeyError(f"unknown capability: {operation.capability_id}") from exc
    return handler(operation, output_dir, context)


__all__ = ["DESCRIPTORS", "HANDLERS", "descriptors", "execute"]
