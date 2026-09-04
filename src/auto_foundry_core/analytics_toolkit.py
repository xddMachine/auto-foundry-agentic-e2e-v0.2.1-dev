"""Portable, source-backed analytical operations for owner-authored scripts.

The functions in this module intentionally stop at durable analytical
artifacts.  They do not publish dashboards, infer causal effects, fetch remote
data, or register a planner method.  Core tabular/segmentation dependencies
are declared by the package; imports remain lazy so stripped child runtimes
can still report a precise dependency error.  Source-format readers remain an
optional ``io`` extra.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from .analytical_artifacts import (
    DataProfileArtifact,
    KpiTableArtifact,
    SegmentationModelArtifact,
    canonical_json,
)
from .contracts import DataAssetRef, TableRef
from .sources import (
    _quote_sqlite_identifier,
    _sqlite_connect,
    _sqlite_tables,
    register_source,
)
from .workspace import validate_allowed_path


class AnalyticsDependencyError(ImportError):
    """Raised when an operation needs a missing analytics/source-format package."""


class AnalyticsInputError(ValueError):
    """Raised for unsafe or semantically invalid analytical inputs."""


_SUPPORTED_FILE_FORMATS = frozenset({"csv", "tsv", "parquet", "xlsx", "xlsm", "sqlite"})
_FILTER_OPERATORS = frozenset({"eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "is_null", "not_null", "contains"})
_AGGREGATIONS = frozenset({"count", "nunique", "sum", "mean", "median", "min", "max", "std", "count_true", "share"})
_VALUE_AGGREGATIONS = frozenset({"nunique", "sum", "mean", "median", "min", "max", "std", "count_true"})
_NUMERIC_AGGREGATIONS = frozenset({"sum", "mean", "median", "min", "max", "std"})
_MAX_CANDIDATE_K = 12
_MAX_PROFILE_VALUES = 100
_MAX_ASSIGNMENT_ROWS = 100_000
_MAX_SILHOUETTE_ROWS = 2_048


def _require_pandas():
    try:
        return importlib.import_module("pandas")
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise AnalyticsDependencyError(
            "Tabular analytics requires pandas. Install the package dependencies "
            "with `pip install -e .` (or install pandas explicitly)."
        ) from exc


def _require_segmentation_dependencies():
    missing: list[str] = []
    modules: dict[str, Any] = {}
    for module_name, package_name in (
        ("pandas", "pandas"),
        ("numpy", "numpy"),
        ("scipy", "scipy"),
        ("sklearn", "scikit-learn"),
    ):
        try:
            modules[module_name] = importlib.import_module(module_name)
        except ImportError:
            missing.append(package_name)
    if missing:
        names = ", ".join(missing)
        raise AnalyticsDependencyError(
            "Customer segmentation requires core analytics dependencies: "
            f"{names}. Install the package dependencies with `pip install -e .`."
        )
    return modules


def _reject_remote_source(source: Any) -> None:
    if isinstance(source, (str, Path)):
        text = str(source).strip().lower()
        if "://" in text or text.startswith(("http:", "https:", "s3:", "gs:", "ftp:")):
            raise AnalyticsInputError("analytics ingestion accepts local paths or DataFrames, not network sources")
    if isinstance(source, DataAssetRef):
        _reject_remote_source(source.uri)
    if isinstance(source, TableRef):
        _reject_remote_source(source.asset)


def _format_for_path(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "xlsm":
        return "xlsx"
    if suffix in {"db", "sqlite", "sqlite3"}:
        return "sqlite"
    return suffix


def _read_sqlite_table(path: Path, table_name: str) -> Any:
    """Materialize one validated SQLite table for an explicit analytics call.

    The source/catalog layers intentionally expose SQLite tables as logical
    ``TableRef`` values.  This helper is only reached by ``ingest_tabular``
    (an explicit analytical operation); launch/catalog code never calls it.
    Table membership is checked before constructing the query and the table
    identifier is quoted as one SQLite identifier, so a caller cannot smuggle
    an arbitrary SQL expression through ``TableRef.name``.
    """

    pd = _require_pandas()
    try:
        names = _sqlite_tables(path)
    except (OSError, sqlite3.DatabaseError) as exc:
        raise AnalyticsInputError(f"cannot inspect SQLite database {path}: {exc}") from exc
    if table_name not in names:
        raise AnalyticsInputError(f"unknown SQLite table {table_name!r} in {path.name}")
    try:
        quoted = _quote_sqlite_identifier(table_name)
        connection = _sqlite_connect(path)
        try:
            cursor = connection.execute(f"SELECT * FROM {quoted}")
            rows = cursor.fetchall()
            columns = [str(description[0]) for description in (cursor.description or ())]
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        raise AnalyticsInputError(f"cannot read SQLite table {table_name!r}: {exc}") from exc
    # Constructing from the cursor description preserves an empty table's
    # schema, unlike ``DataFrame([])`` which would otherwise lose its columns.
    return pd.DataFrame.from_records([tuple(row) for row in rows], columns=columns)


def _column_names(values: Sequence[str] | str | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    if isinstance(values, str):
        values = (values,)
    return tuple(str(value) for value in values)


def _safe_scalar(value: Any) -> Any:
    """Convert pandas/numpy scalar values to finite JSON scalars."""

    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        number = float(value) if isinstance(value, float) else value
        if isinstance(number, float) and not math.isfinite(number):
            return None
        return number
    item = getattr(value, "item", None)
    if callable(item):
        try:
            converted = item()
        except Exception:
            converted = value
        if converted is not value:
            return _safe_scalar(converted)
    if isinstance(value, (list, tuple)):
        return [_safe_scalar(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _safe_scalar(item) for key, item in value.items()}
    return str(value)


def _stable_scalar(value: Any) -> Any:
    """Canonicalize floating-point artifacts across BLAS/thread variants."""

    converted = _safe_scalar(value)
    if isinstance(converted, float):
        return round(converted, 12)
    if isinstance(converted, list):
        return [_stable_scalar(item) for item in converted]
    if isinstance(converted, Mapping):
        return {str(key): _stable_scalar(item) for key, item in converted.items()}
    return converted


def _safe_frame_records(frame: Any) -> list[dict[str, Any]]:
    records = frame.to_dict(orient="records")
    return [{str(key): _safe_scalar(value) for key, value in row.items()} for row in records]


def _frame_fingerprint(frame: Any) -> str:
    payload = {
        "columns": [str(value) for value in frame.columns],
        "dtypes": [str(value) for value in frame.dtypes],
        "index": [_safe_scalar(value) for value in frame.index.tolist()],
        "rows": _safe_frame_records(frame),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TabularInput:
    """Materialized, lineage-bound tabular input used by toolkit operations."""

    frame: Any
    dataset_fingerprint: str
    source_fingerprints: Mapping[str, str]
    source_refs: tuple[Any, ...]
    selected_columns: tuple[str, ...]
    source_format: str


def _source_path(source: str | Path | DataAssetRef | TableRef, *, allowed_roots: Iterable[str | Path] | None = None) -> tuple[Path, Any, str | None]:
    _reject_remote_source(source)
    table_name: str | None = None
    if isinstance(source, TableRef):
        table_name = source.name
        source = source.asset
    if isinstance(source, DataAssetRef):
        path = Path(source.uri).expanduser().resolve(strict=False)
        if not path.is_file():
            raise FileNotFoundError(path)
        if source.content_hash:
            registered = register_source(path, allowed_roots=allowed_roots, format=source.format, metadata=source.metadata)
            if registered.content_hash != source.content_hash:
                raise AnalyticsInputError(f"source changed after registration: {path}")
            return path, registered, table_name
        registered = register_source(path, allowed_roots=allowed_roots, format=source.format, metadata=source.metadata)
        return path, registered, table_name
    candidate = Path(source).expanduser().resolve(strict=False)
    if allowed_roots is not None:
        candidate = validate_allowed_path(candidate, allowed_roots)
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    registered = register_source(candidate, allowed_roots=allowed_roots)
    return candidate, registered, table_name


def ingest_tabular(
    source: str | Path | DataAssetRef | TableRef | Any,
    *,
    columns: Sequence[str] | None = None,
    sheet_name: str | int | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> TabularInput:
    """Read an admitted local CSV/Parquet/XLSX/SQLite table or copy a DataFrame.

    No URL, cloud URI, shell command, or network-backed reader is accepted.
    ``columns`` is applied after ingestion and is retained in the returned
    lineage object so callers can bind exact feature/metric selection.
    """

    pd = _require_pandas()
    source_ref: Any = None
    table_ref: TableRef | None = source if isinstance(source, TableRef) else None
    source_format = "dataframe"
    if isinstance(source, pd.DataFrame):
        frame = source.copy(deep=True)
        dataset_fingerprint = _frame_fingerprint(frame)
        source_fingerprints = {"dataset": dataset_fingerprint}
    else:
        if not isinstance(source, (str, Path, DataAssetRef, TableRef)):
            raise TypeError("source must be a local CSV/Parquet/XLSX path, DataAssetRef/TableRef, or pandas DataFrame")
        path, source_ref, table_name = _source_path(source, allowed_roots=allowed_roots)
        source_format = (source_ref.format if isinstance(source_ref, DataAssetRef) else None) or _format_for_path(path)
        if source_format == "sqlite" and table_ref is None:
            raise AnalyticsInputError("SQLite analytics requires a TableRef naming one admitted table")
        if source_format not in _SUPPORTED_FILE_FORMATS:
            raise AnalyticsInputError(
                f"unsupported analytics tabular format {source_format!r}; use CSV, Parquet, XLSX, or SQLite TableRef"
            )
        if source_format == "csv":
            frame = pd.read_csv(path)
        elif source_format == "tsv":
            frame = pd.read_csv(path, sep="\t")
        elif source_format == "parquet":
            try:
                frame = pd.read_parquet(path)
            except ImportError as exc:  # pragma: no cover - dependency dependent
                raise AnalyticsDependencyError(
                    "Parquet analytics requires pyarrow. Install the optional source-format "
                    "extra with `pip install -e '.[io]'`."
                ) from exc
        elif source_format == "sqlite":
            if table_name is None:  # pragma: no cover - guarded above
                raise AnalyticsInputError("SQLite analytics requires a TableRef naming one admitted table")
            frame = _read_sqlite_table(path, table_name)
        else:
            try:
                frame = pd.read_excel(path, sheet_name=sheet_name if sheet_name is not None else (table_name or 0))
            except ImportError as exc:  # pragma: no cover - dependency dependent
                raise AnalyticsDependencyError(
                    "XLSX analytics requires openpyxl. Install the optional source-format "
                    "extra with `pip install -e '.[io]'`."
                ) from exc
        dataset_fingerprint = source_ref.content_hash
        source_fingerprints = {
            "dataset": str(dataset_fingerprint),
            "selected": _frame_fingerprint(frame),
        }
    frame.columns = [str(column) for column in frame.columns]
    if len(frame.columns) != len(set(frame.columns)):
        raise AnalyticsInputError("source columns must be unique")
    columns = _column_names(columns)
    if columns is None:
        selected_columns = tuple(str(column) for column in frame.columns)
    else:
        selected_columns = tuple(str(column) for column in columns)
        if not selected_columns:
            raise AnalyticsInputError("columns must not be empty when supplied")
        if len(selected_columns) != len(set(selected_columns)):
            raise AnalyticsInputError("columns must not contain duplicates")
        missing = [column for column in selected_columns if column not in frame.columns]
        if missing:
            raise AnalyticsInputError(f"selected columns are missing from source: {missing!r}")
        frame = frame.loc[:, list(selected_columns)].copy(deep=True)
    if source_ref is not None:
        # Preserve the logical table identity for SQLite lineage (and for
        # workbook sheets) instead of reducing a TableRef to its file asset.
        source_refs = (
            TableRef(
                asset=source_ref,
                name=table_ref.name,
                kind=table_ref.kind,
                metadata=table_ref.metadata,
            )
            if table_ref is not None
            else source_ref,
        )
    else:
        source_refs = ()
    source_fingerprints = dict(source_fingerprints)
    source_fingerprints["selected"] = _frame_fingerprint(frame)
    return TabularInput(
        frame=frame,
        dataset_fingerprint=str(dataset_fingerprint),
        source_fingerprints=source_fingerprints,
        source_refs=source_refs,
        selected_columns=selected_columns,
        source_format=source_format,
    )


def load_tabular(*args: Any, **kwargs: Any) -> Any:
    """Return a defensive DataFrame copy from :func:`ingest_tabular`."""

    return ingest_tabular(*args, **kwargs).frame.copy(deep=True)


def fingerprint_source(
    source: str | Path | DataAssetRef | TableRef | Any,
    *,
    columns: Sequence[str] | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> str:
    """Return the deterministic fingerprint for an admitted source slice."""

    return ingest_tabular(source, columns=columns, allowed_roots=allowed_roots).source_fingerprints["selected"]




def _artifact_id(prefix: str, requirement_id: str, fingerprint: str, details: Any) -> str:
    seed = {"requirement_id": requirement_id, "fingerprint": fingerprint, "details": details}
    digest = hashlib.sha256(canonical_json(seed).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _common_artifact_kwargs(
    table: TabularInput,
    *,
    requirement_id: str,
    population: Any,
    grain: str,
    period: Any,
    feature_definitions: Iterable[Any] = (),
    metric_definitions: Iterable[Any] = (),
    method: str,
    parameters: Mapping[str, Any] | None = None,
    random_seed: int | None = None,
    validation_evidence: Any = None,
    tables: Iterable[Any] = (),
    output_refs: Iterable[Any] = (),
    findings: Iterable[Any] = (),
    visualization_intents: Iterable[Any] = (),
    limitations: Iterable[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "requirement_id": requirement_id,
        "dataset_fingerprint": table.dataset_fingerprint,
        "source_fingerprints": table.source_fingerprints,
        "source_refs": table.source_refs,
        "population": population,
        "grain": grain,
        "period": period,
        "feature_definitions": tuple(feature_definitions),
        "metric_definitions": tuple(metric_definitions),
        "method": method,
        "parameters": parameters or {},
        "random_seed": random_seed,
        "validation_evidence": validation_evidence,
        "tables": tuple(tables),
        "output_refs": tuple(output_refs),
        "findings": tuple(findings),
        "visualization_intents": tuple(visualization_intents),
        "limitations": tuple(limitations),
        "metadata": {"selected_columns": table.selected_columns, "source_format": table.source_format, **dict(metadata or {})},
    }


def profile_data(
    source: str | Path | DataAssetRef | TableRef | Any,
    *,
    requirement_id: str = "unbound",
    columns: Sequence[str] | None = None,
    period: Any = "unspecified",
    population: Any = None,
    allowed_roots: Iterable[str | Path] | None = None,
    max_frequency_values: int = 20,
) -> DataProfileArtifact:
    """Build a finite source-backed data profile artifact."""

    if max_frequency_values < 0:
        raise ValueError("max_frequency_values cannot be negative")
    table = ingest_tabular(source, columns=columns, allowed_roots=allowed_roots)
    frame = table.frame
    profile_columns: dict[str, Any] = {}
    numeric_summaries: dict[str, Any] = {}
    pd = _require_pandas()
    for column in table.selected_columns:
        series = frame[column]
        non_null = series.dropna()
        values = [_safe_scalar(value) for value in non_null.tolist()]
        counts = Counter(canonical_json(value) for value in values)
        frequencies: dict[str, int] = {}
        for encoded, count in counts.most_common(max_frequency_values):
            try:
                frequencies[str(json.loads(encoded))] = int(count)
            except json.JSONDecodeError:
                frequencies[encoded] = int(count)
        info = {
            "dtype": str(series.dtype),
            "missing_count": int(series.isna().sum()),
            "missing_rate": (float(series.isna().mean()) if len(series) else 0.0),
            "unique_count": int(series.nunique(dropna=True)),
            "non_null_count": int(series.notna().sum()),
            "frequencies": frequencies,
        }
        if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
            numeric = pd.to_numeric(series, errors="coerce").dropna()
            summary = {
                "count": int(numeric.count()),
                "min": _safe_scalar(numeric.min()) if len(numeric) else None,
                "max": _safe_scalar(numeric.max()) if len(numeric) else None,
                "mean": _safe_scalar(numeric.mean()) if len(numeric) else None,
                "median": _safe_scalar(numeric.median()) if len(numeric) else None,
                "std": _safe_scalar(numeric.std(ddof=1)) if len(numeric) > 1 else None,
            }
            numeric_summaries[column] = summary
            info["numeric"] = summary
        profile_columns[column] = info
    # ``duplicate_count`` is the number of rows beyond the first occurrence;
    # ``duplicate_row_count`` additionally exposes the number of rows involved
    # in duplicate groups for consumers that need that denominator.
    duplicate_count = int(frame.duplicated().sum()) if len(frame) else 0
    duplicate_row_count = int(frame.duplicated(keep=False).sum()) if len(frame) else 0
    profile = {
        "row_count": int(len(frame)),
        "column_count": int(len(table.selected_columns)),
        "columns": profile_columns,
        "numeric_summaries": numeric_summaries,
        "duplicate_count": duplicate_count,
        "duplicate_row_count": duplicate_row_count,
        "selected_columns": list(table.selected_columns),
        "limitations": [
            "Profile statistics are descriptive and do not establish causal relationships.",
            "Frequency output is bounded to the requested top values.",
        ],
    }
    population_value = population if population is not None else {"row_count": len(frame), "kind": "rows"}
    feature_definitions = tuple(
        {"name": column, "role": "profiled", "dtype": str(frame[column].dtype)}
        for column in table.selected_columns
    )
    kwargs = _common_artifact_kwargs(
        table,
        requirement_id=requirement_id,
        population=population_value,
        grain="row",
        period=period,
        feature_definitions=feature_definitions,
        method="descriptive_profile",
        parameters={"max_frequency_values": max_frequency_values},
        validation_evidence={"row_count_exact": True, "finite_values": True},
        tables=({"name": "profile", "row_count": len(table.selected_columns)},),
        findings=(f"Profiled {len(frame)} rows across {len(table.selected_columns)} selected columns.",),
        visualization_intents=({"type": "missingness_bar", "table": "profile", "encoding": "missing_rate"},),
        limitations=tuple(profile["limitations"]),
    )
    kwargs["artifact_id"] = _artifact_id("profile", requirement_id, table.dataset_fingerprint, table.selected_columns)
    return DataProfileArtifact(profile=profile, **kwargs)


@dataclass(frozen=True)
class MetricDefinition:
    """Safe, explicit KPI definition; no arbitrary expression evaluation."""

    name: str
    aggregation: str
    value_column: str | None = None
    group_by: tuple[str, ...] = ()
    filters: tuple[Mapping[str, Any], ...] = ()
    description: str | None = None

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        if not name:
            raise ValueError("metric name must not be empty")
        aggregation = str(self.aggregation).strip().lower()
        if aggregation not in _AGGREGATIONS:
            raise ValueError(f"unsupported KPI aggregation {aggregation!r}; allowed={sorted(_AGGREGATIONS)!r}")
        if self.value_column is not None and not str(self.value_column).strip():
            raise ValueError("value_column must not be blank")
        if aggregation in _VALUE_AGGREGATIONS and self.value_column is None:
            raise ValueError(f"value_column is mandatory for {aggregation} aggregation")
        if aggregation == "share" and self.value_column is not None:
            raise ValueError("share aggregation is row share and does not accept value_column")
        raw_groups = (self.group_by,) if isinstance(self.group_by, str) else self.group_by
        groups = tuple(str(value).strip() for value in raw_groups)
        if any(not value for value in groups) or len(groups) != len(set(groups)):
            raise ValueError("group_by must contain unique non-empty column names")
        raw_filters = (self.filters,) if isinstance(self.filters, Mapping) else self.filters
        filters = tuple(dict(value) for value in raw_filters)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "aggregation", aggregation)
        object.__setattr__(self, "value_column", str(self.value_column).strip() if self.value_column is not None else None)
        object.__setattr__(self, "group_by", groups)
        object.__setattr__(self, "filters", filters)
        if self.description is not None:
            object.__setattr__(self, "description", str(self.description).strip() or None)

    @classmethod
    def from_value(cls, value: "MetricDefinition | Mapping[str, Any]") -> "MetricDefinition":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("metric definitions must be MetricDefinition values or mappings")
        raw = dict(value)
        count_semantics = raw.pop("count_semantics", None)
        if "filter" in raw and "filters" not in raw:
            raw["filters"] = (raw.pop("filter"),)
        filters = raw.get("filters", ())
        if isinstance(filters, Mapping):
            filters = (filters,)
        raw["filters"] = tuple(filters)
        groups = raw.get("group_by", raw.get("grouping", ()))
        if isinstance(groups, str):
            groups = (groups,)
        raw["group_by"] = tuple(groups)
        definition = cls(**raw)
        if count_semantics is not None:
            expected = "rows" if definition.value_column is None else "non_null_values"
            if definition.aggregation != "count" or str(count_semantics) != expected:
                raise ValueError("count_semantics does not agree with count/value_column")
        return definition

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "aggregation": self.aggregation,
            "value_column": self.value_column,
            "group_by": list(self.group_by),
            "filters": [dict(item) for item in self.filters],
        }
        if self.description is not None:
            value["description"] = self.description
        if self.aggregation == "count":
            value["count_semantics"] = "rows" if self.value_column is None else "non_null_values"
        return value


def _filter_frame(frame: Any, filters: Sequence[Mapping[str, Any]]) -> Any:
    pd = _require_pandas()
    result = frame
    for condition in filters:
        if not isinstance(condition, Mapping):
            raise TypeError("KPI filters must be mappings")
        column = condition.get("column")
        operator = str(condition.get("op", condition.get("operator", "eq"))).lower()
        if not isinstance(column, str) or column not in result.columns:
            raise AnalyticsInputError(f"KPI filter column is missing: {column!r}")
        if operator not in _FILTER_OPERATORS:
            raise AnalyticsInputError(f"unsupported KPI filter operator {operator!r}")
        series = result[column]
        if operator == "is_null":
            mask = series.isna()
        elif operator == "not_null":
            mask = series.notna()
        else:
            expected = condition.get("value")
            if operator == "eq":
                mask = series == expected
            elif operator == "ne":
                mask = series != expected
            elif operator == "in":
                if isinstance(expected, (str, bytes)) or not isinstance(expected, Iterable):
                    raise TypeError("KPI 'in' filter value must be a sequence")
                mask = series.isin(list(expected))
            elif operator == "not_in":
                if isinstance(expected, (str, bytes)) or not isinstance(expected, Iterable):
                    raise TypeError("KPI 'not_in' filter value must be a sequence")
                mask = ~series.isin(list(expected))
            elif operator == "gt":
                mask = series > expected
            elif operator == "gte":
                mask = series >= expected
            elif operator == "lt":
                mask = series < expected
            elif operator == "lte":
                mask = series <= expected
            else:  # contains
                mask = series.astype("string").str.contains(str(expected), regex=False, na=False)
        result = result.loc[mask.fillna(False)].copy()
    return result


def _aggregate_series(series: Any, aggregation: str) -> Any:
    if aggregation == "count":
        return int(series.notna().sum())
    if aggregation == "nunique":
        return int(series.nunique(dropna=True))
    if aggregation == "count_true":
        return int(series.fillna(False).astype(bool).sum())
    if aggregation == "share":
        return None
    if aggregation == "sum":
        return _safe_aggregate_value(series.sum(min_count=1), aggregation)
    if aggregation == "mean":
        return _safe_aggregate_value(series.mean(), aggregation)
    if aggregation == "median":
        return _safe_aggregate_value(series.median(), aggregation)
    if aggregation == "min":
        return _safe_aggregate_value(series.min(), aggregation)
    if aggregation == "max":
        return _safe_aggregate_value(series.max(), aggregation)
    if aggregation == "std":
        return _safe_aggregate_value(series.std(ddof=1), aggregation)
    raise AnalyticsInputError(f"unsupported KPI aggregation {aggregation!r}")


def _safe_aggregate_value(value: Any, aggregation: str) -> Any:
    """Convert an aggregate to a finite scalar, rejecting overflow."""

    if value is None:
        return None
    # Nullable pandas dtypes may return ``pd.NA`` for an all-missing
    # aggregate.  Treat that sentinel like ``None`` before attempting a
    # numeric conversion; otherwise it would leak an opaque scalar into the
    # artifact payload and fail strict JSON normalization later.
    try:
        pd = _require_pandas()
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        # ``pd.isna`` can return an array-like value for an unexpected object;
        # scalar conversion below remains the bounded validation path.
        pass
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return _safe_scalar(value)
    if math.isnan(numeric):
        return None
    if not math.isfinite(numeric):
        raise AnalyticsInputError(f"{aggregation} aggregation produced a non-finite result")
    return _safe_scalar(value)


def _validate_metric_column(frame: Any, definition: MetricDefinition) -> None:
    """Validate metric dtype/finite values before any aggregation occurs."""

    if definition.value_column is None:
        # ``count`` (rows) and ``share`` are intentionally value-free.  Every
        # value-based aggregation is rejected by MetricDefinition itself.
        return
    pd = _require_pandas()
    series = frame[definition.value_column]
    if definition.aggregation in _NUMERIC_AGGREGATIONS:
        if not pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
            raise AnalyticsInputError(
                f"metric {definition.name!r} with {definition.aggregation} requires a numeric value_column; "
                f"{definition.value_column!r} has dtype {series.dtype}"
            )
        for value in series.dropna().tolist():
            try:
                finite = math.isfinite(float(value))
            except (TypeError, ValueError, OverflowError) as exc:
                raise AnalyticsInputError(
                    f"metric {definition.name!r} value_column {definition.value_column!r} contains a non-finite value"
                ) from exc
            if not finite:
                raise AnalyticsInputError(
                    f"metric {definition.name!r} value_column {definition.value_column!r} contains a non-finite value"
                )
    elif definition.aggregation == "count_true":
        if pd.api.types.is_bool_dtype(series):
            return
        values = series.dropna().tolist()
        if not all(isinstance(value, bool) for value in values):
            raise AnalyticsInputError(
                f"metric {definition.name!r} with count_true requires a boolean value_column; "
                f"{definition.value_column!r} has dtype {series.dtype}"
            )
    elif definition.aggregation == "nunique":
        for value in series.dropna().tolist():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            try:
                finite = math.isfinite(float(value))
            except (TypeError, ValueError, OverflowError) as exc:
                raise AnalyticsInputError(
                    f"metric {definition.name!r} value_column {definition.value_column!r} contains a non-finite value"
                ) from exc
            if not finite:
                raise AnalyticsInputError(
                    f"metric {definition.name!r} value_column {definition.value_column!r} contains a non-finite value"
                )


def compute_kpi_table(
    source: str | Path | DataAssetRef | TableRef | Any,
    metric_definitions: Sequence[MetricDefinition | Mapping[str, Any]],
    *,
    requirement_id: str = "unbound",
    period: Any = "unspecified",
    population: Any = None,
    columns: Sequence[str] | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> KpiTableArtifact:
    """Compute a small explicit KPI vocabulary into a typed table artifact."""

    if not metric_definitions:
        raise ValueError("at least one metric definition is required")
    table = ingest_tabular(source, columns=columns, allowed_roots=allowed_roots)
    definitions = tuple(MetricDefinition.from_value(value) for value in metric_definitions)
    names = [definition.name for definition in definitions]
    if len(names) != len(set(names)):
        raise ValueError("metric names must be unique")
    groupings = {definition.group_by for definition in definitions}
    if len(groupings) > 1:
        raise ValueError("all KPI definitions must use the same group_by columns for one table artifact")
    group_by = next(iter(groupings), ())
    for definition in definitions:
        if definition.value_column is not None and definition.value_column not in table.frame.columns:
            raise AnalyticsInputError(f"metric value column is missing: {definition.value_column!r}")
        for column in definition.group_by:
            if column not in table.frame.columns:
                raise AnalyticsInputError(f"metric group_by column is missing: {column!r}")
        _validate_metric_column(table.frame, definition)
    rows: list[dict[str, Any]] = []
    if group_by:
        # Build a stable grouping key from the source frame, retaining missing
        # values as a valid group and sorting by their JSON representation.
        keys = table.frame.loc[:, list(group_by)].drop_duplicates().to_dict(orient="records")
        keys = sorted(keys, key=lambda row: canonical_json({str(k): _safe_scalar(v) for k, v in row.items()}))
        for key in keys:
            row = {column: _safe_scalar(key[column]) for column in group_by}
            for definition in definitions:
                filtered = _filter_frame(table.frame, definition.filters)
                mask = pd_mask_equal(filtered, key)
                subset = filtered.loc[mask]
                if definition.aggregation == "share":
                    numerator = len(subset)
                    denominator = len(filtered)
                    value = (float(numerator) / denominator) if denominator else None
                elif definition.aggregation == "count" and definition.value_column is None:
                    value = len(subset)
                else:
                    # ``value_column`` is mandatory for every aggregation
                    # except row-count/share; never aggregate an implicit
                    # DataFrame index.
                    series = subset[definition.value_column]  # type: ignore[index]
                    value = _aggregate_series(series, definition.aggregation)
                row[definition.name] = _safe_scalar(value)
            rows.append(row)
    else:
        row: dict[str, Any] = {}
        for definition in definitions:
            filtered = _filter_frame(table.frame, definition.filters)
            if definition.aggregation == "share":
                value = (float(len(filtered)) / len(table.frame)) if len(table.frame) else None
            elif definition.aggregation == "count" and definition.value_column is None:
                value = len(filtered)
            else:
                series = filtered[definition.value_column]  # type: ignore[index]
                value = _aggregate_series(series, definition.aggregation)
            row[definition.name] = _safe_scalar(value)
        rows.append(row)
    metric_columns = tuple(
        {"name": definition.name, "aggregation": definition.aggregation, "value_column": definition.value_column, "group_by": list(definition.group_by)}
        for definition in definitions
    )
    population_value = population if population is not None else {"row_count": len(table.frame), "kind": "rows"}
    kwargs = _common_artifact_kwargs(
        table,
        requirement_id=requirement_id,
        population=population_value,
        grain="grouped_row" if group_by else "dataset",
        period=period,
        metric_definitions=tuple(definition.to_dict() for definition in definitions),
        method="explicit_kpi_aggregation",
        parameters={"group_by": list(group_by), "aggregation_vocabulary": sorted(_AGGREGATIONS)},
        validation_evidence={"metric_count": len(definitions), "finite_values": True, "expression_eval": False},
        tables=({"name": "kpi_table", "row_count": len(rows), "columns": list(group_by) + names},),
        findings=(f"Computed {len(definitions)} explicit KPI definitions over {len(table.frame)} rows.",),
        visualization_intents=({"type": "kpi_table", "table": "kpi_table", "group_by": list(group_by)},),
        limitations=("KPI definitions use the bounded aggregation/filter vocabulary; arbitrary expressions are not evaluated.",),
    )
    kwargs["artifact_id"] = _artifact_id("kpi", requirement_id, table.dataset_fingerprint, [definition.to_dict() for definition in definitions])
    payload = {"rows": rows, "selected_columns": list(table.selected_columns), "row_count": len(table.frame)}
    return KpiTableArtifact(rows=rows, payload=payload, **kwargs)


def pd_mask_equal(frame: Any, key: Mapping[str, Any]) -> Any:
    """Compare group columns while treating missing values as equal."""

    mask = None
    for column, value in key.items():
        series = frame[column]
        try:
            missing_value = bool(_require_pandas().isna(value))
        except (TypeError, ValueError):
            missing_value = value is None
        current = series.isna() if missing_value else series == value
        mask = current if mask is None else (mask & current)
    return mask if mask is not None else frame.index.to_series().map(lambda _value: True)


def _assignment_path(
    requested: str | Path | None,
    *,
    assignment_signature: Mapping[str, Any],
    allowed_roots: Iterable[str | Path] | None,
) -> tuple[Path, str] | None:
    """Resolve a bounded path for an external assignment artifact.

    Relative paths are rooted at the controlled runner's explicit
    ``AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT`` (or an explicitly admitted
    ``allowed_roots`` root).  An omitted path is available only when the
    controlled output root is present; the caller can then choose complete
    in-memory assignments rather than creating an ambient file.  Parent
    traversal and symlink escapes are rejected before resolution.
    """

    output_root_text = os.environ.get("AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT", "").strip()
    output_root = Path(output_root_text).expanduser() if output_root_text else None
    roots = tuple(allowed_roots or ())
    if requested is None:
        if output_root is None:
            return None
        digest = hashlib.sha256(canonical_json(dict(assignment_signature)).encode("utf-8")).hexdigest()[:24]
        root = output_root
        candidate_input = root / f"segment_assignments-{digest}.jsonl"
    else:
        raw = Path(requested).expanduser()
        raw_parts = tuple(raw.parts) + tuple(str(raw).replace("\\", "/").split("/"))
        if ".." in raw_parts:
            raise AnalyticsInputError("assignment_output_ref must not contain parent traversal")
        if raw.is_absolute():
            candidate_input = raw
            root = output_root
            if root is None and roots:
                root = Path(roots[0]).expanduser()
            if root is None:
                # An explicit absolute file path is itself an explicit output
                # authority; its parent provides the relative output root.
                root = raw.parent
        else:
            if output_root is not None:
                root = output_root
            elif len(roots) == 1:
                root = Path(roots[0]).expanduser()
            else:
                raise AnalyticsInputError(
                    "relative assignment_output_ref requires AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT or one allowed root"
                )
            candidate_input = root / raw
    root = root.resolve(strict=False)
    for component in (candidate_input, *candidate_input.parents):
        if component.is_symlink():
            raise AnalyticsInputError(f"assignment_output_ref must not traverse a symlink: {component}")
    # Resolve the candidate before admission checking so an existing symlink
    # in a parent directory cannot escape the declared root.
    candidate = candidate_input.resolve(strict=False)
    try:
        candidate.relative_to(root)
        # ``allowed_roots`` bounds source admission.  When the controlled
        # runner supplies a distinct output root, that root is the authority
        # for generated assignment files and must not be conflated with the
        # input roots.
        if requested is not None and roots and output_root is None:
            candidate = validate_allowed_path(candidate, roots)
    except (OSError, ValueError) as exc:
        raise AnalyticsInputError(f"invalid assignment_output_ref {requested!r}: {exc}") from exc
    relative = candidate.relative_to(root).as_posix()
    if not relative or relative == ".":
        raise AnalyticsInputError("assignment_output_ref must identify a file")
    if candidate.exists() and candidate.is_dir():
        raise AnalyticsInputError(f"assignment_output_ref must identify a file, not a directory: {candidate}")
    return candidate, relative


def _write_assignment_artifact(
    path: Path,
    identifiers: Sequence[Any],
    labels: Sequence[Any],
    *,
    relative_path: str,
) -> dict[str, Any]:
    """Write complete deterministic JSONL assignments and return its descriptor."""

    if len(identifiers) != len(labels):  # pragma: no cover - defensive
        raise AnalyticsInputError("assignment identifiers and labels have different row counts")
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            try:
                for identifier, label in zip(identifiers, labels):
                    row = {"population_id": _safe_scalar(identifier), "segment": int(label)}
                    encoded = (canonical_json(row) + "\n").encode("utf-8")
                    stream.write(encoded)
                    digest.update(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            except Exception:
                temporary.unlink(missing_ok=True)
                temporary = None
                raise
        temporary.replace(path)
        temporary = None
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise AnalyticsInputError(f"could not write assignment_output_ref {path}: {exc}") from exc
    size_bytes = path.stat().st_size
    return {
        "path": relative_path,
        "format": "jsonl",
        "columns": ["population_id", "segment"],
        "row_count": len(identifiers),
        "sha256": digest.hexdigest(),
        "size_bytes": size_bytes,
        "complete": True,
    }


def _bounded_silhouette_score(matrix: Any, labels: Any, scorer: Any, np: Any) -> tuple[float, bool]:
    """Compute a deterministic full or sampled silhouette score.

    ``sklearn.metrics.silhouette_score`` is quadratic in the number of rows.
    A fixed, evenly spaced sample keeps large-source segmentation usable while
    retaining an explicit validation limitation in the emitted artifact.
    """

    row_count = int(len(matrix))
    if row_count <= _MAX_SILHOUETTE_ROWS:
        return float(scorer(matrix, labels)), False
    indices = np.linspace(0, row_count - 1, num=_MAX_SILHOUETTE_ROWS, dtype=int)
    # Ensure every fitted cluster is represented even when the source is
    # ordered by a feature and evenly spaced rows miss a tiny cluster.
    labels_array = np.asarray(labels)
    for cluster in sorted(set(int(value) for value in labels_array)):
        first = int(np.flatnonzero(labels_array == cluster)[0])
        indices = np.concatenate((indices, np.asarray([first], dtype=int)))
    indices = np.unique(indices)
    return float(scorer(matrix[indices], labels_array[indices])), True


def segment_customers(
    source: str | Path | DataAssetRef | TableRef | Any,
    *,
    population_id: str | None = None,
    numeric_features: Sequence[str] = (),
    categorical_features: Sequence[str] = (),
    requirement_id: str = "unbound",
    period: Any = "unspecified",
    population: Any = None,
    columns: Sequence[str] | None = None,
    requested_k: int | None = None,
    candidate_ks: Sequence[int] | None = None,
    k: int | None = None,
    population_id_column: str | None = None,
    agglomerative_comparison: bool | None = None,
    compare_agglomerative: bool = False,
    random_seed: int = 42,
    n_init: int = 20,
    profile_value_limit: int = 50,
    assignment_output_ref: str | Path | None = None,
    assignment_output_path: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> SegmentationModelArtifact:
    """Fit deterministic k-means segmentation and emit profile/validation tables.

    Assignments remain embedded for small populations.  For populations above
    the embedded-output bound, the complete assignment table is written as a
    deterministic, hashed JSONL output and referenced from the typed artifact;
    rows are never silently dropped.
    """

    if assignment_output_ref is not None and assignment_output_path is not None:
        if Path(assignment_output_ref).expanduser() != Path(assignment_output_path).expanduser():
            raise ValueError("assignment_output_ref and assignment_output_path must agree when both are supplied")
    if assignment_output_ref is None:
        assignment_output_ref = assignment_output_path

    if k is not None:
        if requested_k is not None and requested_k != k:
            raise ValueError("k and requested_k must agree when both are supplied")
        requested_k = k
    if population_id_column is not None:
        if population_id is not None and population_id != population_id_column:
            raise ValueError("population_id and population_id_column must agree when both are supplied")
        population_id = population_id_column
    if population_id is None or not str(population_id).strip():
        raise AnalyticsInputError("population_id is required")
    population_id = str(population_id).strip()
    if agglomerative_comparison is not None:
        compare_agglomerative = bool(agglomerative_comparison)
    modules = _require_segmentation_dependencies()
    pd = modules["pandas"]
    np = modules["numpy"]
    table = ingest_tabular(source, columns=columns, allowed_roots=allowed_roots)
    numeric_features = _column_names(numeric_features) or ()
    categorical_features = _column_names(categorical_features) or ()
    all_features = numeric_features + categorical_features
    if not all_features:
        raise AnalyticsInputError("at least one numeric or categorical feature is required")
    if len(all_features) != len(set(all_features)):
        raise AnalyticsInputError("numeric_features and categorical_features must not overlap or duplicate")
    if population_id not in table.frame.columns:
        raise AnalyticsInputError(f"population_id column is missing: {population_id!r}")
    missing = [column for column in all_features if column not in table.frame.columns]
    if missing:
        raise AnalyticsInputError(f"segmentation feature columns are missing: {missing!r}")
    empty_features = [column for column in all_features if int(table.frame[column].notna().sum()) == 0]
    if empty_features:
        raise AnalyticsInputError(f"segmentation features have no observed values: {empty_features!r}")
    ids = table.frame[population_id]
    if ids.isna().any():
        raise AnalyticsInputError("population_id must not contain missing values")
    if ids.duplicated().any():
        raise AnalyticsInputError("population_id values must be unique for assignment artifacts")
    if len(table.frame) < 3:
        raise AnalyticsInputError("segmentation requires at least three population rows")
    if isinstance(random_seed, bool) or not isinstance(random_seed, int):
        raise TypeError("random_seed must be an integer")
    if isinstance(n_init, bool) or not isinstance(n_init, int) or n_init <= 0:
        raise ValueError("n_init must be a positive integer")
    if profile_value_limit < 0:
        raise ValueError("profile_value_limit cannot be negative")
    max_k = min(len(table.frame) - 1, _MAX_CANDIDATE_K)
    if requested_k is not None:
        if isinstance(requested_k, bool) or not isinstance(requested_k, int):
            raise TypeError("requested_k must be an integer")
        candidate_values = [requested_k]
    elif candidate_ks is not None:
        candidate_values = list(candidate_ks)
    else:
        candidate_values = list(range(2, min(8, max_k) + 1))
    if not candidate_values:
        raise AnalyticsInputError("candidate_ks must not be empty")
    if len(candidate_values) > _MAX_CANDIDATE_K:
        raise AnalyticsInputError(f"candidate_ks is bounded to {_MAX_CANDIDATE_K} values")
    candidate_values = sorted(set(candidate_values))
    if any(isinstance(value, bool) or not isinstance(value, int) for value in candidate_values):
        raise TypeError("candidate_ks values must be integers")
    if any(value < 2 or value > max_k for value in candidate_values):
        raise AnalyticsInputError(f"candidate K values must be between 2 and {max_k}")

    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.cluster import AgglomerativeClustering, KMeans
    from sklearn.metrics import silhouette_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    feature_frame = table.frame.loc[:, list(all_features)].copy(deep=True)
    # The explicit numeric feature contract permits CSV/object inputs that are
    # numerically encoded as strings.  Coerce those values before deterministic
    # median imputation; categorical columns retain their source labels.
    for column in numeric_features:
        feature_frame[column] = pd.to_numeric(feature_frame[column], errors="coerce")
    coerced_empty = [column for column in numeric_features if int(feature_frame[column].notna().sum()) == 0]
    if coerced_empty:
        raise AnalyticsInputError(f"numeric segmentation features have no usable values: {coerced_empty!r}")
    transformers: list[tuple[str, Any, list[str]]] = []
    if numeric_features:
        numeric_pipeline = Pipeline([("imputer", SimpleImputer(strategy="median")), ("scaler", StandardScaler())])
        transformers.append(("numeric", numeric_pipeline, list(numeric_features)))
    if categorical_features:
        try:
            encoder = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
        except TypeError:  # pragma: no cover - old sklearn compatibility
            encoder = OneHotEncoder(handle_unknown="ignore", sparse=False)
        categorical_pipeline = Pipeline([("imputer", SimpleImputer(strategy="most_frequent")), ("onehot", encoder)])
        transformers.append(("categorical", categorical_pipeline, list(categorical_features)))
    preprocessor = ColumnTransformer(transformers=transformers, remainder="drop", verbose_feature_names_out=False)
    matrix = np.asarray(preprocessor.fit_transform(feature_frame), dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] == 0:
        raise AnalyticsInputError("segmentation preprocessing produced no usable features")
    candidate_results: list[dict[str, Any]] = []
    fitted_models: dict[int, Any] = {}
    silhouette_sampled = False
    for value in candidate_values:
        model = KMeans(n_clusters=value, random_state=random_seed, n_init=n_init)
        labels = model.fit_predict(matrix)
        try:
            score, sampled = _bounded_silhouette_score(matrix, labels, silhouette_score, np)
            silhouette_sampled = silhouette_sampled or sampled
        except ValueError as exc:
            raise AnalyticsInputError(f"cannot compute a silhouette score for K={value}: {exc}") from exc
        if not math.isfinite(score):
            raise AnalyticsInputError(f"cannot compute a finite silhouette score for K={value}")
        candidate_results.append({"k": value, "silhouette": _safe_scalar(score), "cluster_sizes": _cluster_sizes(labels)})
        fitted_models[value] = model
    if requested_k is not None:
        selected_k = requested_k
    else:
        best_result = min(candidate_results, key=lambda result: (-float(result["silhouette"]), int(result["k"])))
        selected_k = int(best_result["k"])
    model = fitted_models[selected_k]
    labels = model.labels_
    selected_silhouette = float(next(result["silhouette"] for result in candidate_results if result["k"] == selected_k))
    comparison: dict[str, Any] | None = None
    limitations: list[str] = [
        "Clusters are descriptive groupings; they do not establish causal drivers or treatment effects.",
        "Bootstrap/stability validation was not computed in this vertical slice.",
    ]
    if silhouette_sampled:
        limitations.append(
            f"Silhouette validation used a deterministic {_MAX_SILHOUETTE_ROWS}-row sample because full pairwise scoring is quadratic."
        )
    if compare_agglomerative:
        agglomerative = AgglomerativeClustering(n_clusters=selected_k, linkage="ward")
        agg_labels = agglomerative.fit_predict(matrix)
        try:
            agg_score, agg_sampled = _bounded_silhouette_score(matrix, agg_labels, silhouette_score, np)
            if agg_sampled and not silhouette_sampled:
                limitations.append(
                    f"Silhouette validation used a deterministic {_MAX_SILHOUETTE_ROWS}-row sample because full pairwise scoring is quadratic."
                )
        except ValueError as exc:
            raise AnalyticsInputError(f"cannot compute an agglomerative silhouette score: {exc}") from exc
        if not math.isfinite(agg_score):
            raise AnalyticsInputError("cannot compute a finite agglomerative silhouette score")
        comparison = {
            "algorithm": "agglomerative",
            "linkage": "ward",
            "n_clusters": selected_k,
            "silhouette": _safe_scalar(agg_score),
            "cluster_sizes": _cluster_sizes(agg_labels),
            "assignable": False,
        }
        limitations.append("Agglomerative comparison is retained for validation only and cannot score new rows without a separate assignment policy.")
    assignment_output: dict[str, Any] | None = None
    assignment_row_count = int(len(table.frame))
    if assignment_row_count <= _MAX_ASSIGNMENT_ROWS:
        assignments = [
            {"population_id": _safe_scalar(identifier), "segment": int(label)}
            for identifier, label in zip(ids.tolist(), labels)
        ]
        assignment_complete = True
    else:
        assignment_signature = {
            "dataset_fingerprint": table.dataset_fingerprint,
            "population_id": population_id,
            "numeric_features": numeric_features,
            "categorical_features": categorical_features,
            "selected_k": selected_k,
            "random_seed": random_seed,
            "n_init": n_init,
        }
        resolved_output = _assignment_path(
            assignment_output_ref,
            assignment_signature=assignment_signature,
            allowed_roots=allowed_roots,
        )
        if resolved_output is None:
            # No explicit output authority was supplied.  Preserve every
            # assignment in the typed payload rather than creating an
            # ambient file in the source directory, cwd, or system temp.
            assignments = [
                {"population_id": _safe_scalar(identifier), "segment": int(label)}
                for identifier, label in zip(ids.tolist(), labels)
            ]
            limitations.append(
                f"Assignment rows remain fully embedded because no explicit output root was supplied for externalization above the {_MAX_ASSIGNMENT_ROWS} row bound."
            )
        else:
            output_path, relative_output_path = resolved_output
            assignment_output = _write_assignment_artifact(
                output_path,
                ids.tolist(),
                labels,
                relative_path=relative_output_path,
            )
            assignments = []
            limitations.append(
                f"Assignment rows are externalized in a complete hashed JSONL artifact above the {_MAX_ASSIGNMENT_ROWS} row embedded-output bound."
            )
        assignment_complete = True
    limitations.append(
        "Floating-point model/profile values are rounded to 12 decimal places at artifact wire serialization for repeatable hashes."
    )
    segment_sizes = _cluster_sizes(labels)
    segment_profiles = _segment_profiles(table.frame, labels, numeric_features, categorical_features, profile_value_limit=profile_value_limit)
    transformed_names = [str(value) for value in preprocessor.get_feature_names_out()]
    preprocessor_state = _preprocessor_state(preprocessor, numeric_features, categorical_features)
    model_payload: dict[str, Any] = {
        "algorithm": "kmeans",
        "n_clusters": selected_k,
        "random_seed": random_seed,
        "n_init": n_init,
        "feature_names": transformed_names,
        "centers": [[_safe_scalar(value) for value in row] for row in np.asarray(model.cluster_centers_)],
        "preprocessing": preprocessor_state,
        "assignable": True,
        "population_id_column": population_id,
        "assignment_complete": assignment_complete,
        "assignments": assignments,
        "assignment_row_count": assignment_row_count,
        "candidate_k_validation": candidate_results,
        "selected_silhouette": _safe_scalar(selected_silhouette),
    }
    if assignment_output is not None:
        model_payload["assignment_output_ref"] = assignment_output
    if comparison is not None:
        model_payload["agglomerative_comparison"] = comparison
    profile_table = {
        "segment_sizes": segment_sizes,
        "segment_profiles": segment_profiles,
        "raw_denominators": {str(int(label)): int(count) for label, count in segment_sizes.items()},
    }
    # Keep full precision through fitting and profiling.  Canonicalize only
    # the values crossing the immutable artifact wire boundary; this avoids
    # BLAS/thread-level last-bit drift without changing in-memory calculations.
    model_payload = _stable_scalar(model_payload)
    profile_table = _stable_scalar(profile_table)
    feature_definitions = tuple(
        {"name": column, "role": "segmentation_feature", "kind": "numeric"} for column in numeric_features
    ) + tuple({"name": column, "role": "segmentation_feature", "kind": "categorical"} for column in categorical_features)
    population_value = population if population is not None else {"id_column": population_id, "row_count": len(table.frame)}
    findings = [
        f"Selected K={selected_k} using {'requested K' if requested_k is not None else 'highest silhouette with lowest-K tie break'}.",
        f"Largest segment is {max(segment_sizes, key=segment_sizes.get)} with {max(segment_sizes.values())} of {len(table.frame)} rows.",
        "Segment differences are descriptive action hypotheses and require domain review before intervention.",
    ]
    kwargs = _common_artifact_kwargs(
        table,
        requirement_id=requirement_id,
        population=population_value,
        grain="one_row_per_population_id",
        period=period,
        feature_definitions=feature_definitions,
        method="kmeans" + ("+agglomerative_comparison" if compare_agglomerative else ""),
        parameters={"requested_k": requested_k, "candidate_ks": candidate_values, "profile_value_limit": profile_value_limit, "population_id": population_id},
        random_seed=random_seed,
        validation_evidence=_stable_scalar({
            "silhouette": _safe_scalar(selected_silhouette),
            "candidate_k": candidate_results,
            "stability": {"status": "not_computed", "reason": "bootstrap validation is outside the initial vertical slice"},
            "agglomerative": comparison,
        }),
        tables=(
            {
                "name": "assignments",
                "row_count": assignment_row_count,
                "complete": assignment_complete,
                **({"output_ref": assignment_output} if assignment_output is not None else {}),
            },
            {"name": "segment_profiles", "row_count": len(segment_profiles)},
        ),
        output_refs=((assignment_output,) if assignment_output is not None else ()),
        findings=tuple(findings),
        visualization_intents=(
            {
                "type": "segment_size_column",
                "table": "segment_sizes",
                "encoding": {"x": "segment", "y": "count", "value_kind": "raw_count"},
            },
            {"type": "segment_profile_heatmap", "table": "segment_profiles", "encoding": {"x": "feature_value", "y": "segment", "color": "index"}},
        ),
        limitations=tuple(limitations),
        metadata={"selected_columns": table.selected_columns, "source_format": table.source_format},
    )
    kwargs["artifact_id"] = _artifact_id(
        "segmentation",
        requirement_id,
        table.dataset_fingerprint,
        {"numeric_features": numeric_features, "categorical_features": categorical_features, "k": selected_k, "seed": random_seed},
    )
    return SegmentationModelArtifact(model=model_payload, segment_profiles=profile_table, **kwargs)


def _cluster_sizes(labels: Any) -> dict[str, int]:
    counts = Counter(int(value) for value in labels)
    return {str(label): int(counts[label]) for label in sorted(counts)}


def _segment_profiles(
    frame: Any,
    labels: Any,
    numeric_features: Sequence[str],
    categorical_features: Sequence[str],
    *,
    profile_value_limit: int,
) -> list[dict[str, Any]]:
    pd = _require_pandas()
    working = frame.copy(deep=True)
    working["__segment__"] = labels
    total_rows = len(working)
    rows: list[dict[str, Any]] = []
    for segment in sorted(set(int(value) for value in labels)):
        subset = working.loc[working["__segment__"] == segment]
        denominator = len(subset)
        for feature in numeric_features:
            numeric = pd.to_numeric(subset[feature], errors="coerce").dropna()
            rows.append(
                {
                    "segment": segment,
                    "feature": feature,
                    "feature_kind": "numeric",
                    "feature_value": None,
                    "count": int(numeric.count()),
                    "denominator": denominator,
                    "rate": None,
                    "base_count": int(pd.to_numeric(working[feature], errors="coerce").notna().sum()),
                    "base_denominator": total_rows,
                    "base_rate": (float(pd.to_numeric(working[feature], errors="coerce").notna().sum()) / total_rows if total_rows else None),
                    "index": None,
                    "mean": _safe_scalar(numeric.mean()) if len(numeric) else None,
                    "median": _safe_scalar(numeric.median()) if len(numeric) else None,
                }
            )
        for feature in categorical_features:
            base_series = working[feature].map(_safe_scalar)
            segment_series = subset[feature].map(_safe_scalar)
            base_counts = Counter(canonical_json(value) for value in base_series.tolist())
            segment_counts = Counter(canonical_json(value) for value in segment_series.tolist())
            selected_keys = [key for key, _count in base_counts.most_common(profile_value_limit)]
            for key in selected_keys:
                try:
                    feature_value = _safe_scalar(json.loads(key))
                except json.JSONDecodeError:
                    feature_value = key
                segment_count = int(segment_counts.get(key, 0))
                base_count = int(base_counts.get(key, 0))
                rate = float(segment_count / denominator) if denominator else None
                base_rate = float(base_count / total_rows) if total_rows else None
                index = (float(rate / base_rate * 100.0) if rate is not None and base_rate not in (None, 0) else None)
                rows.append(
                    {
                        "segment": segment,
                        "feature": feature,
                        "feature_kind": "categorical",
                        "feature_value": feature_value,
                        "count": segment_count,
                        "denominator": denominator,
                        "rate": rate,
                        "base_count": base_count,
                        "base_denominator": total_rows,
                        "base_rate": base_rate,
                        "index": index,
                    }
                )
    return rows


def _preprocessor_state(preprocessor: Any, numeric_features: Sequence[str], categorical_features: Sequence[str]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "numeric_features": list(numeric_features),
        "categorical_features": list(categorical_features),
        "numeric_imputation": "median",
        "scaling": "standard",
        "categorical_imputation": "most_frequent",
        "one_hot": {"handle_unknown": "ignore"},
    }
    named = dict(preprocessor.named_transformers_)
    if numeric_features and "numeric" in named:
        pipeline = named["numeric"]
        state["numeric_medians"] = [_safe_scalar(value) for value in pipeline.named_steps["imputer"].statistics_]
        state["numeric_mean"] = [_safe_scalar(value) for value in pipeline.named_steps["scaler"].mean_]
        state["numeric_scale"] = [_safe_scalar(value) for value in pipeline.named_steps["scaler"].scale_]
    if categorical_features and "categorical" in named:
        pipeline = named["categorical"]
        state["categorical_fill_values"] = [_safe_scalar(value) for value in pipeline.named_steps["imputer"].statistics_]
        state["categorical_categories"] = {
            feature: [_safe_scalar(value) for value in categories]
            for feature, categories in zip(categorical_features, pipeline.named_steps["onehot"].categories_)
        }
    return state


def score_segments(
    artifact: SegmentationModelArtifact,
    source: str | Path | DataAssetRef | TableRef | Any,
    *,
    allowed_roots: Iterable[str | Path] | None = None,
) -> list[dict[str, Any]]:
    """Assign new rows using the serialized k-means centers and preprocessing state.

    This scorer is intentionally limited to the k-means model emitted by
    :func:`segment_customers`; agglomerative comparison output is validation
    evidence and is not treated as an assigner.
    """

    if not isinstance(artifact, SegmentationModelArtifact) or artifact.artifact_type != "segmentation_model":
        raise TypeError("score_segments requires a segmentation_model artifact")
    model = artifact.model
    if not isinstance(model, Mapping) or not model.get("assignable"):
        raise AnalyticsInputError("segmentation artifact does not contain an assignable k-means model")
    preprocessing = model.get("preprocessing")
    if not isinstance(preprocessing, Mapping):
        raise AnalyticsInputError("segmentation artifact is missing serialized preprocessing state")
    numeric_features = tuple(str(value) for value in preprocessing.get("numeric_features", ()))
    categorical_features = tuple(str(value) for value in preprocessing.get("categorical_features", ()))
    population_id = str(model.get("population_id_column", ""))
    if not population_id:
        raise AnalyticsInputError("segmentation artifact is missing population_id_column")
    table = ingest_tabular(
        source,
        columns=(population_id, *numeric_features, *categorical_features),
        allowed_roots=allowed_roots,
    )
    pd = _require_pandas()
    np = importlib.import_module("numpy")
    feature_parts: list[Any] = []
    if numeric_features:
        numeric = table.frame.loc[:, list(numeric_features)].copy(deep=True)
        numeric = numeric.apply(pd.to_numeric, errors="coerce")
        medians = list(preprocessing.get("numeric_medians", ()))
        means = list(preprocessing.get("numeric_mean", ()))
        scales = list(preprocessing.get("numeric_scale", ()))
        if not (len(medians) == len(numeric_features) == len(means) == len(scales)):
            raise AnalyticsInputError("serialized numeric preprocessing state is incomplete")
        for index, column in enumerate(numeric_features):
            numeric[column] = numeric[column].fillna(medians[index])
            scale = float(scales[index])
            numeric[column] = (numeric[column] - float(means[index])) / (scale if math.isfinite(scale) and scale != 0 else 1.0)
        feature_parts.append(numeric.to_numpy(dtype=float))
    if categorical_features:
        categories = preprocessing.get("categorical_categories", {})
        fills = list(preprocessing.get("categorical_fill_values", ()))
        if len(fills) != len(categorical_features) or any(column not in categories for column in categorical_features):
            raise AnalyticsInputError("serialized categorical preprocessing state is incomplete")
        encoded_parts: list[Any] = []
        for index, column in enumerate(categorical_features):
            values = table.frame[column].where(table.frame[column].notna(), fills[index])
            known = list(categories[column])
            lookup = {canonical_json(value): offset for offset, value in enumerate(known)}
            encoded = np.zeros((len(values), len(known)), dtype=float)
            for row_index, value in enumerate(values.tolist()):
                offset = lookup.get(canonical_json(_safe_scalar(value)))
                if offset is not None:
                    encoded[row_index, offset] = 1.0
            encoded_parts.append(encoded)
        feature_parts.append(np.concatenate(encoded_parts, axis=1) if encoded_parts else np.empty((len(table.frame), 0)))
    matrix = np.concatenate(feature_parts, axis=1) if feature_parts else np.empty((len(table.frame), 0))
    centers = np.asarray(model.get("centers", ()), dtype=float)
    if centers.ndim != 2 or centers.shape[1] != matrix.shape[1]:
        raise AnalyticsInputError("serialized k-means centers do not match preprocessing state")
    distances = ((matrix[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
    labels = distances.argmin(axis=1)
    return [
        {"population_id": _safe_scalar(identifier), "segment": int(label)}
        for identifier, label in zip(table.frame[population_id].tolist(), labels)
    ]


__all__ = [
    "AnalyticsDependencyError",
    "AnalyticsInputError",
    "MetricDefinition",
    "TabularInput",
    "compute_kpi_table",
    "fingerprint_source",
    "ingest_tabular",
    "load_tabular",
    "profile_data",
    "score_segments",
    "segment_customers",
]
