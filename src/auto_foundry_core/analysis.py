"""Program-bound analysis context and bounded script execution.

This module is the narrow boundary between an analyst-authored calculation and
the run-owned workbench.  Scripts receive the context through the
``AUTO_FOUNDRY_ANALYSIS_CONTEXT`` environment variable and can load it with
``load_bound_analysis_context``; they do not need (or get encouraged) to copy
dataset paths, run paths, or source hashes into source code.

The runner provides process, path, timeout, and output bounds.  It is *not* a
security sandbox: hostile code still requires operating-system/container
isolation outside this package.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
import uuid

from .contracts import DataAssetRef
from .durable import ItemWorkspace
from .prepared import PreparedAssetRegistry
from .workbench import CatalogCounts, DataRoomCatalogEntry, DataRoomWorkbench
from .workspace import AllowedRootError, RunContext


ANALYSIS_CONTEXT_SCHEMA_VERSION = "1"
ANALYSIS_CONTEXT_ENV = "AUTO_FOUNDRY_ANALYSIS_CONTEXT"
ANALYSIS_PHASE_ENV = "AUTO_FOUNDRY_ANALYSIS_PHASE"
ANALYSIS_SAMPLE_LIMIT_ENV = "AUTO_FOUNDRY_SAMPLE_LIMIT"
ANALYSIS_OUTPUT_ROOT_ENV = "AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT"
_MANIFEST_FILENAME = "analysis_context.json"
_RECEIPT_DIR = Path("script_receipts")
_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_OUTPUT_BYTES = 256 * 1024
_DEFAULT_SAMPLE_LIMIT = 100
_VALID_PHASES = frozenset({"smoke", "full"})
_SAME_ATTEMPT_ERRORS = frozenset(
    {
        "SyntaxError",
        "NameError",
        "TypeError",
        "ModuleNotFoundError",
        "ImportError",
        "AttributeError",
        "KeyError",
        "ValueError",
    }
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, DataAssetRef):
        return value.to_dict()
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple | set | frozenset):
        return tuple(_freeze(item) for item in value)
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _assert_no_symlink_components(path: Path, *, root: Path) -> Path:
    """Validate lexical containment and reject symlink components."""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise AllowedRootError(f"path escapes bound root: {path}") from exc
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if not resolved_path.is_relative_to(resolved_root):
        raise AllowedRootError(f"path escapes bound root: {path}")
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise AllowedRootError(f"bound path cannot use symlink: {current}")
    return path


def _regular_file(path: Path, *, root: Path, label: str) -> Path:
    _assert_no_symlink_components(path, root=root)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular file: {path}")
    return path


@dataclass(frozen=True)
class CatalogSnapshot:
    """Hash-bound view of one canonical physical catalog."""

    path: Path
    content_hash: str
    catalog_key: str
    catalog_schema_version: str
    source_hash: str
    core_version: str
    entries: tuple[DataRoomCatalogEntry, ...]
    counts: CatalogCounts

    @property
    def catalog_path(self) -> Path:
        return self.path

    @property
    def archive_hash(self) -> str:
        return self.source_hash

    @classmethod
    def from_workbench(cls, workbench: DataRoomWorkbench) -> "CatalogSnapshot":
        entries = workbench.catalog()
        room = workbench.data_room
        path = room.catalog_path
        if not path.is_file():
            raise ValueError("canonical catalog was not materialized")
        return cls(
            path=path,
            content_hash=_sha256_file(path),
            catalog_key=room.catalog_key,
            catalog_schema_version=room.catalog_schema_version,
            source_hash=room.archive_ref.content_hash or "",
            core_version=workbench.context.core_version,
            entries=tuple(entries),
            counts=room.catalog_counts(entries),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "content_hash": self.content_hash,
            "catalog_key": self.catalog_key,
            "catalog_schema_version": self.catalog_schema_version,
            "source_hash": self.source_hash,
            "core_version": self.core_version,
            "counts": self.counts.to_dict(),
        }


@dataclass(frozen=True)
class ScriptExecutionReceipt:
    """One compile, dependency, smoke, full, or deterministic script event."""

    receipt_id: str
    phase: str
    script_path: str
    script_hash: str | None
    context_path: str
    context_hash: str
    source_hash: str
    started_at: str
    finished_at: str
    wall_seconds: float
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    output_limited: bool = False
    error_type: str | None = None
    error_category: str | None = None
    traceback: str | None = None
    output_hashes: Mapping[str, str] = field(default_factory=dict)
    receipt_path: str | None = None

    @property
    def same_attempt_feedback(self) -> bool:
        """All local script failures stay in the current analyst attempt."""

        return self.error_type is not None or self.error_category is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "phase": self.phase,
            "script_path": self.script_path,
            "script_hash": self.script_hash,
            "context_path": self.context_path,
            "context_hash": self.context_hash,
            "source_hash": self.source_hash,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "wall_seconds": self.wall_seconds,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "timed_out": self.timed_out,
            "output_limited": self.output_limited,
            "error_type": self.error_type,
            "error_category": self.error_category,
            "traceback": self.traceback,
            "output_hashes": dict(self.output_hashes),
            "receipt_path": self.receipt_path,
        }


@dataclass(frozen=True)
class ScriptRunReport:
    """Aggregate result for one bounded script pipeline."""

    status: str
    same_attempt_feedback: bool
    receipts: tuple[ScriptExecutionReceipt, ...]
    output_hashes: Mapping[str, str] = field(default_factory=dict)
    deterministic_match: bool | None = None
    error_category: str | None = None
    error_type: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "passed"


class BoundAnalysisContext:
    """Immutable program-owned context exposed to an analysis script."""

    def __init__(
        self,
        *,
        context: RunContext,
        source_identity: DataAssetRef,
        workbench: DataRoomWorkbench,
        source_catalog: CatalogSnapshot,
        item_workspace: ItemWorkspace,
        ontology_bundle: Any,
        manifest_path: Path,
        manifest_hash: str,
        telemetry: Any = None,
    ) -> None:
        self.context = context
        self._source_identity = source_identity
        self._workbench = workbench
        self._source_catalog = source_catalog
        self._item_workspace = item_workspace
        self._ontology_bundle = _freeze(ontology_bundle)
        self.manifest_path = manifest_path
        self.manifest_hash = manifest_hash
        self.telemetry = telemetry
        self._script_runner: ControlledScriptRunner | None = None

    @classmethod
    def create(
        cls,
        context: RunContext,
        archive: str | Path | DataAssetRef,
        item_workspace: ItemWorkspace,
        *,
        ontology_bundle: Any = (),
        telemetry: Any = None,
        workbench: DataRoomWorkbench | None = None,
    ) -> "BoundAnalysisContext":
        if not isinstance(context, RunContext):
            raise TypeError("BoundAnalysisContext requires one RunContext")
        if not isinstance(item_workspace, ItemWorkspace):
            raise TypeError("item_workspace must be an ItemWorkspace")
        if item_workspace.context is not context:
            raise ValueError("item_workspace must use the same RunContext")
        if workbench is None:
            workbench = DataRoomWorkbench(context, archive, telemetry=telemetry)
        elif workbench.context is not context:
            raise ValueError("workbench must use the same RunContext")
        source_identity = workbench.data_room.archive_ref
        snapshot = CatalogSnapshot.from_workbench(workbench)
        manifest_path = item_workspace.work_root / _MANIFEST_FILENAME
        _assert_no_symlink_components(manifest_path, root=item_workspace.item_root)
        unsigned = {
            "schema_version": ANALYSIS_CONTEXT_SCHEMA_VERSION,
            "kind": "bound_analysis_context",
            "run_id": context.run_id,
            "run_root": str(context.run_root),
            "input_roots": [str(root) for root in context.input_roots],
            "core_version": context.core_version,
            "skill_version": context.skill_version,
            "item_id": item_workspace.item_id,
            "item_mode": item_workspace.mode,
            "source_identity": source_identity.to_dict(),
            "catalog": snapshot.to_dict(),
            "ontology_bundle": _jsonable(ontology_bundle),
            "manifest_path": str(manifest_path),
        }
        manifest_hash = _sha256_bytes(_json_bytes(unsigned))
        manifest = {**unsigned, "manifest_hash": manifest_hash}
        _atomic_write_json(manifest_path, manifest)
        bound = cls(
            context=context,
            source_identity=source_identity,
            workbench=workbench,
            source_catalog=snapshot,
            item_workspace=item_workspace,
            ontology_bundle=ontology_bundle,
            manifest_path=manifest_path,
            manifest_hash=manifest_hash,
            telemetry=telemetry,
        )
        bound._script_runner = ControlledScriptRunner(bound)
        return bound

    @property
    def data_room(self):
        return self._workbench.data_room

    @property
    def workbench(self) -> DataRoomWorkbench:
        return self._workbench

    @property
    def source_identity(self) -> DataAssetRef:
        return self._source_identity

    @property
    def source_catalog(self) -> CatalogSnapshot:
        return self._source_catalog

    @property
    def item_workspace(self) -> ItemWorkspace:
        return self._item_workspace

    @property
    def prepared_assets(self) -> PreparedAssetRegistry:
        return self._workbench.prepared_registry

    @property
    def ontology_bundle(self) -> Any:
        return self._ontology_bundle

    @property
    def script_runner(self) -> "ControlledScriptRunner":
        if self._script_runner is None:
            self._script_runner = ControlledScriptRunner(self)
        return self._script_runner

    def ensure_valid(self) -> None:
        """Fail closed if source, catalog, or context identity changed."""

        if not self.manifest_path.is_file() or _sha256_file(self.manifest_path) != self._manifest_file_hash():
            raise ValueError("analysis context manifest changed")
        source_path = self.context.resolve_input(self.source_identity.uri)
        if not source_path.is_file() or _sha256_file(source_path) != self.source_identity.content_hash:
            raise ValueError("analysis source changed after binding")
        if not self.source_catalog.path.is_file() or _sha256_file(self.source_catalog.path) != self.source_catalog.content_hash:
            raise ValueError("analysis catalog changed after binding")
        if self.source_catalog.source_hash != self.source_identity.content_hash:
            raise ValueError("analysis source/catalog hash mismatch")

    def _manifest_file_hash(self) -> str:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("analysis context manifest is unreadable") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("analysis context manifest must be an object")
        unsigned = dict(payload)
        actual = unsigned.pop("manifest_hash", None)
        expected = _sha256_bytes(_json_bytes(unsigned))
        if actual != expected or actual != self.manifest_hash:
            raise ValueError("analysis context manifest hash does not match")
        return _sha256_file(self.manifest_path)

    @classmethod
    def load(
        cls,
        context: RunContext | None = None,
        *,
        path: str | Path | None = None,
        item_workspace: ItemWorkspace | None = None,
        telemetry: Any = None,
    ) -> "BoundAnalysisContext":
        return load_bound_analysis_context(context, path=path, item_workspace=item_workspace, telemetry=telemetry)


def _manifest_path_for(
    context: RunContext | None,
    *,
    path: str | Path | None,
    item_workspace: ItemWorkspace | None,
) -> Path:
    selected = path or os.environ.get(ANALYSIS_CONTEXT_ENV)
    if selected is None:
        if item_workspace is None:
            raise ValueError(f"{ANALYSIS_CONTEXT_ENV} is not set and no item workspace was supplied")
        selected = item_workspace.work_root / _MANIFEST_FILENAME
    if context is None:
        raw = Path(selected).expanduser()
        if not raw.is_absolute():
            raise ValueError("an explicit context is required for relative manifest paths")
        if not raw.is_file() or raw.is_symlink():
            raise ValueError("analysis context manifest must be a regular file")
        return raw
    resolved = context.resolve_run_path(selected)
    return _regular_file(resolved, root=context.run_root, label="analysis context manifest")


def load_bound_analysis_context(
    context: RunContext | None = None,
    *,
    path: str | Path | None = None,
    item_workspace: ItemWorkspace | None = None,
    telemetry: Any = None,
) -> BoundAnalysisContext:
    """Load a hash-bound context from the environment or an explicit safe path."""

    if context is not None and not isinstance(context, RunContext):
        raise TypeError("load_bound_analysis_context requires one RunContext")
    manifest_path = _manifest_path_for(context, path=path, item_workspace=item_workspace)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("analysis context manifest is unreadable") from exc
    if not isinstance(manifest, Mapping):
        raise ValueError("analysis context manifest must be an object")
    unsigned = dict(manifest)
    manifest_hash = unsigned.pop("manifest_hash", None)
    if not isinstance(manifest_hash, str) or manifest_hash != _sha256_bytes(_json_bytes(unsigned)):
        raise ValueError("analysis context manifest hash does not match")
    if unsigned.get("schema_version") != ANALYSIS_CONTEXT_SCHEMA_VERSION or unsigned.get("kind") != "bound_analysis_context":
        raise ValueError("analysis context manifest schema is unsupported")
    if context is None:
        run_root = unsigned.get("run_root")
        input_roots = unsigned.get("input_roots", ())
        if not isinstance(run_root, str) or not isinstance(input_roots, list) or any(not isinstance(root, str) for root in input_roots):
            raise ValueError("analysis context roots are invalid")
        context = RunContext(
            str(unsigned.get("run_id", "")),
            run_root,
            tuple(input_roots),
            core_version=str(unsigned.get("core_version", "")),
            skill_version=unsigned.get("skill_version"),
        )
        manifest_path = _regular_file(
            context.resolve_run_path(manifest_path),
            root=context.run_root,
            label="analysis context manifest",
        )
    if unsigned.get("run_id") != context.run_id or unsigned.get("core_version") != context.core_version:
        raise ValueError("analysis context run/core identity does not match")
    item_id = unsigned.get("item_id")
    item_mode = unsigned.get("item_mode", "question")
    if not isinstance(item_id, str) or not item_id:
        raise ValueError("analysis context item identity is invalid")
    if item_workspace is None:
        item_workspace = ItemWorkspace.load(context, item_id, mode=item_mode, telemetry=telemetry)
    if item_workspace.context is not context or item_workspace.item_id != item_id or item_workspace.mode != item_mode:
        raise ValueError("analysis context item workspace identity does not match")
    expected_manifest = item_workspace.work_root / _MANIFEST_FILENAME
    if manifest_path != expected_manifest:
        raise ValueError("analysis context manifest is not inside the bound item workspace")
    source_payload = unsigned.get("source_identity")
    if not isinstance(source_payload, Mapping):
        raise ValueError("analysis source identity is missing")
    source_identity = DataAssetRef.from_dict(source_payload)
    if not source_identity.content_hash:
        raise ValueError("analysis source identity must contain a content hash")
    source_path = context.resolve_input(source_identity.uri)
    if not source_path.is_file() or _sha256_file(source_path) != source_identity.content_hash:
        raise ValueError("analysis source changed after context binding")
    workbench = DataRoomWorkbench(context, source_identity, telemetry=telemetry)
    snapshot = CatalogSnapshot.from_workbench(workbench)
    catalog_payload = unsigned.get("catalog")
    if not isinstance(catalog_payload, Mapping):
        raise ValueError("analysis catalog binding is missing")
    if (
        str(catalog_payload.get("path")) != str(snapshot.path)
        or catalog_payload.get("content_hash") != snapshot.content_hash
        or catalog_payload.get("catalog_key") != snapshot.catalog_key
        or catalog_payload.get("source_hash") != snapshot.source_hash
    ):
        raise ValueError("analysis catalog binding does not match")
    bound = BoundAnalysisContext(
        context=context,
        source_identity=source_identity,
        workbench=workbench,
        source_catalog=snapshot,
        item_workspace=item_workspace,
        ontology_bundle=unsigned.get("ontology_bundle", ()),
        manifest_path=manifest_path,
        manifest_hash=manifest_hash,
        telemetry=telemetry,
    )
    bound._script_runner = ControlledScriptRunner(bound)
    bound.ensure_valid()
    return bound


def _module_names(source: str) -> tuple[str, ...]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module.split(".")[0])
    return tuple(dict.fromkeys(names))


def _module_available(module: str, script_path: Path) -> bool:
    """Check imports without executing them, including local script helpers."""

    try:
        if importlib.util.find_spec(module) is not None:
            return True
    except (ImportError, ModuleNotFoundError, ValueError):
        pass
    local_file = script_path.parent / f"{module}.py"
    local_package = script_path.parent / module / "__init__.py"
    return local_file.is_file() or local_package.is_file()


def _exception_from_text(stderr: str) -> tuple[str | None, str | None]:
    for name in _SAME_ATTEMPT_ERRORS:
        if f"{name}:" in stderr or stderr.rstrip().endswith(name):
            return name, "same_attempt_feedback"
    return None, None


class ControlledScriptRunner:
    """Run one exact script path under the bound item workspace."""

    def __init__(
        self,
        analysis_context: BoundAnalysisContext,
        *,
        python_executable: str | Path | None = None,
        default_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        default_output_bytes: int = _DEFAULT_OUTPUT_BYTES,
    ) -> None:
        if not isinstance(analysis_context, BoundAnalysisContext):
            raise TypeError("ControlledScriptRunner requires a BoundAnalysisContext")
        self.analysis_context = analysis_context
        self.python_executable = str(python_executable or sys.executable)
        self.default_timeout_seconds = float(default_timeout_seconds)
        self.default_output_bytes = int(default_output_bytes)
        if self.default_timeout_seconds <= 0 or self.default_output_bytes <= 0:
            raise ValueError("runner bounds must be positive")

    @property
    def context(self) -> BoundAnalysisContext:
        return self.analysis_context

    def _script_path(self, script: str | Path) -> Path:
        work = self.context.item_workspace.work_root
        raw = Path(script)
        candidate = raw if raw.is_absolute() else work / raw
        _assert_no_symlink_components(candidate, root=work)
        return _regular_file(candidate, root=work, label="analysis script")

    def _output_paths(self, outputs: Iterable[str | Path]) -> tuple[Path, ...]:
        item_root = self.context.item_workspace.item_root
        work_root = self.context.item_workspace.work_root
        result: list[Path] = []
        for value in outputs:
            raw = Path(value)
            if raw.is_absolute():
                candidate = raw
            elif raw.parts and raw.parts[0] in {"work", "questions", "requirements"}:
                candidate = item_root / raw if raw.parts[0] == "work" else self.context.context.resolve_run_path(raw)
            else:
                candidate = work_root / raw
            _assert_no_symlink_components(candidate, root=work_root)
            if not candidate.is_relative_to(work_root):
                raise AllowedRootError(f"script output escapes item work: {candidate}")
            if candidate.exists() and candidate.is_symlink():
                raise AllowedRootError(f"script output cannot be a symlink: {candidate}")
            if candidate.exists() and not candidate.is_file():
                raise ValueError(f"script output must be a file: {candidate}")
            result.append(candidate)
        return tuple(dict.fromkeys(result))

    def _environment(self, phase: str, sample_limit: int, *, output_root: Path) -> dict[str, str]:
        # Do not leak the parent process's credentials or unrelated service
        # configuration into an analyst script.  The child needs only normal
        # Python lookup/localization/temp settings plus the explicit bindings
        # below.  Host/container isolation remains a separate responsibility.
        env: dict[str, str] = {}
        for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP"):
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env[ANALYSIS_CONTEXT_ENV] = str(self.context.manifest_path)
        env[ANALYSIS_PHASE_ENV] = phase
        env[ANALYSIS_SAMPLE_LIMIT_ENV] = str(sample_limit)
        env[ANALYSIS_OUTPUT_ROOT_ENV] = str(output_root)
        source_root = str(Path(__file__).resolve().parents[1])
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = source_root if not existing_pythonpath else source_root + os.pathsep + existing_pythonpath
        return env

    def _write_receipt(self, receipt: ScriptExecutionReceipt) -> ScriptExecutionReceipt:
        receipt_dir = self.context.item_workspace.work_root / _RECEIPT_DIR
        _assert_no_symlink_components(receipt_dir, root=self.context.item_workspace.item_root)
        receipt_path = receipt_dir / f"{receipt.receipt_id}.json"
        _atomic_write_json(receipt_path, {**receipt.to_dict(), "receipt_path": str(receipt_path)})
        return ScriptExecutionReceipt(**{**receipt.to_dict(), "receipt_path": str(receipt_path)})

    def _failure_receipt(
        self,
        *,
        phase: str,
        script_path: Path,
        script_hash: str | None,
        started: str,
        start_mono: float,
        error_type: str,
        error_category: str,
        traceback: str,
        stderr: str = "",
    ) -> ScriptExecutionReceipt:
        receipt = ScriptExecutionReceipt(
            receipt_id=f"receipt-{uuid.uuid4().hex}",
            phase=phase,
            script_path=str(script_path),
            script_hash=script_hash,
            context_path=str(self.context.manifest_path),
            context_hash=self.context.manifest_hash,
            source_hash=self.context.source_identity.content_hash or "",
            started_at=started,
            finished_at=_utc_now(),
            wall_seconds=max(0.0, time.monotonic() - start_mono),
            exit_code=None,
            stderr=stderr,
            error_type=error_type,
            error_category=error_category,
            traceback=traceback,
        )
        return self._write_receipt(receipt)

    def _compile_and_check(self, script_path: Path) -> tuple[str | None, ScriptExecutionReceipt | None]:
        started = _utc_now()
        start_mono = time.monotonic()
        script_hash: str | None = None
        try:
            source_bytes = script_path.read_bytes()
            script_hash = _sha256_bytes(source_bytes)
            source = source_bytes.decode("utf-8")
            ast.parse(source, filename=str(script_path))
        except SyntaxError as exc:
            return None, self._failure_receipt(
                phase="compile",
                script_path=script_path,
                script_hash=script_hash,
                started=started,
                start_mono=start_mono,
                error_type="SyntaxError",
                error_category="same_attempt_feedback",
                traceback=str(exc),
            )
        except (OSError, UnicodeDecodeError) as exc:
            return None, self._failure_receipt(
                phase="compile",
                script_path=script_path,
                script_hash=script_hash,
                started=started,
                start_mono=start_mono,
                error_type=type(exc).__name__,
                error_category="same_attempt_feedback",
                traceback=str(exc),
            )
        missing: list[str] = []
        for module in _module_names(source):
            try:
                if not _module_available(module, script_path):
                    missing.append(module)
            except (ImportError, ModuleNotFoundError, ValueError):
                missing.append(module)
        if missing:
            return None, self._failure_receipt(
                phase="dependency_check",
                script_path=script_path,
                script_hash=script_hash,
                started=started,
                start_mono=start_mono,
                error_type="ModuleNotFoundError",
                error_category="same_attempt_feedback",
                traceback="missing dependencies: " + ", ".join(sorted(missing)),
            )
        return script_hash, None

    def execute(
        self,
        script: str | Path,
        *,
        phase: str = "full",
        sample_limit: int = _DEFAULT_SAMPLE_LIMIT,
        allowed_outputs: Iterable[str | Path] = (),
        timeout_seconds: float | None = None,
        output_bytes: int | None = None,
        _cwd: Path | None = None,
        _output_root: Path | None = None,
    ) -> ScriptExecutionReceipt:
        """Compile/check and execute one script phase without shell access."""

        if phase not in _VALID_PHASES:
            raise ValueError("phase must be smoke or full")
        if sample_limit < 0:
            raise ValueError("sample_limit cannot be negative")
        timeout = self.default_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        cap = self.default_output_bytes if output_bytes is None else int(output_bytes)
        if timeout <= 0 or cap <= 0:
            raise ValueError("timeout_seconds and output_bytes must be positive")
        outputs = self._output_paths(allowed_outputs)
        self.context.ensure_valid()
        script_path = self._script_path(script)
        output_snapshot = self._snapshot_outputs(outputs)
        cwd = self.context.item_workspace.work_root if _cwd is None else _cwd
        output_root = self.context.item_workspace.work_root if _output_root is None else _output_root
        _assert_no_symlink_components(cwd, root=self.context.item_workspace.work_root)
        if not cwd.is_dir():
            cwd.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_components(output_root, root=self.context.item_workspace.work_root)
        if not output_root.is_dir():
            output_root.mkdir(parents=True, exist_ok=True)
        script_hash, preflight = self._compile_and_check(script_path)
        if preflight is not None:
            return preflight
        started = _utc_now()
        start_mono = time.monotonic()
        receipt_id = f"receipt-{uuid.uuid4().hex}"
        temp_dir = self.context.item_workspace.work_root / ".analysis-run"
        _assert_no_symlink_components(temp_dir, root=self.context.item_workspace.item_root)
        temp_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = temp_dir / f"{receipt_id}.stdout"
        stderr_path = temp_dir / f"{receipt_id}.stderr"
        timed_out = False
        output_limited = False
        exit_code: int | None = None
        try:
            with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
                process = subprocess.Popen(
                    [self.python_executable, str(script_path)],
                    cwd=str(cwd),
                    env=self._environment(phase, sample_limit, output_root=output_root),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    shell=False,
                )
                while process.poll() is None:
                    if time.monotonic() - start_mono > timeout:
                        timed_out = True
                        process.terminate()
                        try:
                            process.wait(timeout=1.0)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=1.0)
                        break
                    try:
                        if stdout_path.stat().st_size + stderr_path.stat().st_size > cap:
                            output_limited = True
                            process.terminate()
                            try:
                                process.wait(timeout=1.0)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=1.0)
                            break
                    except FileNotFoundError:
                        pass
                    time.sleep(0.01)
                if process.poll() is None:
                    process.wait(timeout=1.0)
                exit_code = process.returncode
        except OSError as exc:
            finished = _utc_now()
            receipt = ScriptExecutionReceipt(
                receipt_id=receipt_id,
                phase=phase,
                script_path=str(script_path),
                script_hash=script_hash,
                context_path=str(self.context.manifest_path),
                context_hash=self.context.manifest_hash,
                source_hash=self.context.source_identity.content_hash or "",
                started_at=started,
                finished_at=finished,
                wall_seconds=max(0.0, time.monotonic() - start_mono),
                exit_code=None,
                error_type=type(exc).__name__,
                error_category="same_attempt_feedback",
                traceback=str(exc),
            )
            return self._write_receipt(receipt)
        stdout_bytes = stdout_path.read_bytes() if stdout_path.exists() else b""
        stderr_bytes = stderr_path.read_bytes() if stderr_path.exists() else b""
        if len(stdout_bytes) + len(stderr_bytes) > cap:
            output_limited = True
        stdout_truncated = len(stdout_bytes) > cap
        stderr_truncated = len(stderr_bytes) > cap
        stdout = stdout_bytes[:cap].decode("utf-8", errors="replace")
        stderr = stderr_bytes[:cap].decode("utf-8", errors="replace")
        error_type: str | None = None
        error_category: str | None = None
        traceback_text: str | None = None
        context_error: Exception | None = None
        try:
            self.context.ensure_valid()
        except Exception as exc:  # fail closed after a child-side mutation
            context_error = exc
        if context_error is not None:
            error_type, error_category, traceback_text = type(context_error).__name__, "context_integrity_failure", str(context_error)
        elif timed_out:
            error_type, error_category, traceback_text = "TimeoutExpired", "runtime_timeout", "script timed out"
        elif output_limited:
            error_type, error_category, traceback_text = "OutputLimitExceeded", "runtime_output_limit", "script output exceeded cap"
        elif exit_code not in (0, None):
            error_type, error_category = _exception_from_text(stderr)
            traceback_text = stderr or None
            if error_category is None:
                error_type, error_category = "ScriptError", "script_failure"
        if error_category is not None:
            self._restore_outputs(output_snapshot)
        output_hashes = self._hash_outputs(outputs)
        receipt = ScriptExecutionReceipt(
            receipt_id=receipt_id,
            phase=phase,
            script_path=str(script_path),
            script_hash=script_hash,
            context_path=str(self.context.manifest_path),
            context_hash=self.context.manifest_hash,
            source_hash=self.context.source_identity.content_hash or "",
            started_at=started,
            finished_at=_utc_now(),
            wall_seconds=max(0.0, time.monotonic() - start_mono),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            timed_out=timed_out,
            output_limited=output_limited,
            error_type=error_type,
            error_category=error_category,
            traceback=traceback_text,
            output_hashes=output_hashes,
        )
        return self._write_receipt(receipt)

    def _hash_outputs(
        self,
        outputs: Sequence[Path],
        *,
        require_all: bool = False,
    ) -> dict[str, str]:
        if require_all:
            contents = self._read_outputs(outputs)
        else:
            contents = {}
            for path in outputs:
                if path.exists():
                    _regular_file(path, root=self.context.item_workspace.work_root, label="script output")
                    contents[path] = path.read_bytes()
        return {str(path): _sha256_bytes(content) for path, content in contents.items()}

    def _read_outputs(self, outputs: Sequence[Path]) -> dict[Path, bytes]:
        """Read and validate every declared output before publication.

        Reading the complete scratch set first is important: a later missing
        or symlinked output must not be discovered after an earlier target has
        already been replaced.
        """

        result: dict[Path, bytes] = {}
        work_root = self.context.item_workspace.work_root
        for path in outputs:
            _assert_no_symlink_components(path, root=work_root)
            _regular_file(path, root=work_root, label="script output")
            result[path] = path.read_bytes()
        return result

    def _scratch_paths(self, outputs: Sequence[Path], scratch_root: Path) -> tuple[Path, ...]:
        work_root = self.context.item_workspace.work_root
        _assert_no_symlink_components(scratch_root, root=work_root)
        return tuple(scratch_root / path.relative_to(work_root) for path in outputs)

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _snapshot_outputs(outputs: Sequence[Path]) -> dict[Path, bytes | None]:
        snapshot: dict[Path, bytes | None] = {}
        for path in outputs:
            snapshot[path] = path.read_bytes() if path.is_file() else None
        return snapshot

    @staticmethod
    def _restore_outputs(snapshot: Mapping[Path, bytes | None]) -> None:
        for path, content in snapshot.items():
            if content is None:
                path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def _materialize_outputs(
        self,
        source_paths: Sequence[Path],
        target_paths: Sequence[Path],
        *,
        snapshot: Mapping[Path, bytes | None] | None = None,
    ) -> None:
        """Publish a complete scratch set or restore every target.

        All sources are read and all target paths are checked before the first
        atomic replacement.  If any replacement fails, the supplied pre-run
        snapshot is restored (or a local snapshot is taken when called
        directly), so no partial output can survive a normal script failure.
        """

        if len(source_paths) != len(target_paths):
            raise ValueError("source and target output declarations differ")
        source_bytes = self._read_outputs(source_paths)
        work_root = self.context.item_workspace.work_root
        target_snapshot = dict(snapshot) if snapshot is not None else self._snapshot_outputs(target_paths)
        for target in target_paths:
            _assert_no_symlink_components(target, root=work_root)
            if target.exists() and (target.is_symlink() or not target.is_file()):
                raise ValueError(f"script output target must be a regular file: {target}")
        try:
            for source, target in zip(source_paths, target_paths):
                self._atomic_write_bytes(target, source_bytes[source])
        except Exception:
            self._restore_outputs(target_snapshot)
            raise

    def run_pipeline(
        self,
        script: str | Path,
        *,
        allowed_outputs: Iterable[str | Path] = (),
        deterministic_outputs: Iterable[str | Path] | Mapping[str | Path, str] = (),
        sample_limit: int = _DEFAULT_SAMPLE_LIMIT,
        timeout_seconds: float | None = None,
        output_bytes: int | None = None,
    ) -> ScriptRunReport:
        """Run compile/dependency, smoke, full, and optional deterministic rerun."""

        outputs = self._output_paths(allowed_outputs)
        expected_hashes: dict[str, str] = {}
        if isinstance(deterministic_outputs, Mapping):
            expected_hashes = {str(path): str(value) for path, value in deterministic_outputs.items()}
            deterministic_values: Iterable[str | Path] = deterministic_outputs.keys()
        else:
            deterministic_values = deterministic_outputs
        deterministic = self._output_paths(deterministic_values)
        all_outputs = tuple(dict.fromkeys((*outputs, *deterministic)))
        self.context.ensure_valid()
        script_path = self._script_path(script)
        script_hash, preflight = self._compile_and_check(script_path)
        if preflight is not None:
            return ScriptRunReport(
                status="failed",
                same_attempt_feedback=preflight.error_category == "same_attempt_feedback",
                receipts=(preflight,),
                error_category=preflight.error_category,
                error_type=preflight.error_type,
            )
        snapshot = self._snapshot_outputs(all_outputs)
        smoke = self.execute(
            script_path,
            phase="smoke",
            sample_limit=sample_limit,
            allowed_outputs=outputs,
            timeout_seconds=timeout_seconds,
            output_bytes=output_bytes,
        )
        receipts: list[ScriptExecutionReceipt] = [smoke]
        smoke_ok = smoke.exit_code == 0 and not smoke.timed_out and not smoke.output_limited and smoke.error_category is None
        if not smoke_ok:
            self._restore_outputs(snapshot)
            return ScriptRunReport(
                status="failed",
                same_attempt_feedback=True,
                receipts=tuple(receipts),
                output_hashes=smoke.output_hashes,
                error_category=smoke.error_category,
                error_type=smoke.error_type,
            )
        deterministic_match: bool | None = None
        output_hashes: dict[str, str] = {}
        if not deterministic:
            full = self.execute(
                script_path,
                phase="full",
                sample_limit=sample_limit,
                allowed_outputs=outputs,
                timeout_seconds=timeout_seconds,
                output_bytes=output_bytes,
            )
            receipts.append(full)
            full_ok = full.exit_code == 0 and not full.timed_out and not full.output_limited and full.error_category is None
            if not full_ok:
                self._restore_outputs(snapshot)
                return ScriptRunReport(
                    status="failed",
                    same_attempt_feedback=True,
                    receipts=tuple(receipts),
                    output_hashes=full.output_hashes,
                    error_category=full.error_category,
                    error_type=full.error_type,
                )
            output_hashes = dict(full.output_hashes)
        if deterministic:
            # Full deterministic executions happen in disposable runner-owned
            # directories.  Nothing is copied into the item work tree until
            # both runs have succeeded and their declared hashes agree.
            scratch_base = self.context.item_workspace.work_root / ".analysis-run"
            first_root = scratch_base / f"deterministic-{uuid.uuid4().hex}-first"
            second_root = scratch_base / f"deterministic-{uuid.uuid4().hex}-second"
            first_root.mkdir(parents=True, exist_ok=False)
            second_root.mkdir(parents=True, exist_ok=False)
            first_all = self._scratch_paths(all_outputs, first_root)
            second_all = self._scratch_paths(all_outputs, second_root)
            first_det = self._scratch_paths(deterministic, first_root)
            second_det = self._scratch_paths(deterministic, second_root)
            try:
                # The ordinary full run above is retained as the first
                # receipt/validation pass; deterministic comparison itself is
                # isolated and therefore cannot overwrite an accepted output.
                first = self.execute(
                    script_path,
                    phase="full",
                    sample_limit=sample_limit,
                    allowed_outputs=first_all,
                    timeout_seconds=timeout_seconds,
                    output_bytes=output_bytes,
                    _cwd=first_root,
                    _output_root=first_root,
                )
                receipts.append(first)
                first_ok = first.exit_code == 0 and not first.timed_out and not first.output_limited and first.error_category is None
                first_hashes: dict[str, str] = {}
                first_output_error: Exception | None = None
                if first_ok:
                    try:
                        first_contents = self._read_outputs(first_all)
                        first_hashes = {
                            str(path): _sha256_bytes(first_contents[path])
                            for path in first_det
                        }
                    except Exception as exc:
                        first_output_error = exc
                if not first_ok or first_output_error is not None or len(first_hashes) != len(first_det):
                    self._restore_outputs(snapshot)
                    return ScriptRunReport(
                        status="failed",
                        same_attempt_feedback=True,
                        receipts=tuple(receipts),
                        output_hashes={},
                        deterministic_match=False,
                        error_category=first.error_category or "deterministic_output_missing",
                        error_type=first.error_type or (type(first_output_error).__name__ if first_output_error else None),
                    )
                shutil.rmtree(first_root, ignore_errors=True)
                second = self.execute(
                    script_path,
                    phase="full",
                    sample_limit=sample_limit,
                    allowed_outputs=second_all,
                    timeout_seconds=timeout_seconds,
                    output_bytes=output_bytes,
                    _cwd=second_root,
                    _output_root=second_root,
                )
                receipts.append(second)
                second_ok = second.exit_code == 0 and not second.timed_out and not second.output_limited and second.error_category is None
                second_hashes: dict[str, str] = {}
                second_output_error: Exception | None = None
                if second_ok:
                    try:
                        second_contents = self._read_outputs(second_all)
                        second_hashes = {
                            str(path): _sha256_bytes(second_contents[path])
                            for path in second_det
                        }
                    except Exception as exc:
                        second_output_error = exc
                # Compare by declared relative path, not scratch directory.
                first_relative = {str(path.relative_to(first_root)): first_hashes[str(path)] for path in first_det}
                second_relative = {str(path.relative_to(second_root)): second_hashes[str(path)] for path in second_det}
                deterministic_match = second_ok and second_output_error is None and first_relative == second_relative
                if deterministic_match and expected_hashes:
                    normalized_expected = {
                        str(self._output_paths((key,))[0]): value for key, value in expected_hashes.items()
                    }
                    deterministic_match = all(
                        second_hashes.get(str(second_path)) == expected
                        for final_path, expected in normalized_expected.items()
                        for second_path in (second_root / Path(final_path).relative_to(self.context.item_workspace.work_root),)
                    )
                if not deterministic_match:
                    self._restore_outputs(snapshot)
                    return ScriptRunReport(
                        status="failed",
                        same_attempt_feedback=True,
                        receipts=tuple(receipts),
                        output_hashes={},
                        deterministic_match=False,
                        error_category=second.error_category or ("deterministic_output_missing" if second_output_error else "deterministic_mismatch"),
                        error_type=second.error_type or (type(second_output_error).__name__ if second_output_error else None),
                    )
                try:
                    self._materialize_outputs(second_all, all_outputs, snapshot=snapshot)
                except Exception as exc:
                    self._restore_outputs(snapshot)
                    return ScriptRunReport(
                        status="failed",
                        same_attempt_feedback=True,
                        receipts=tuple(receipts),
                        output_hashes={},
                        deterministic_match=False,
                        error_category="same_attempt_feedback",
                        error_type=type(exc).__name__,
                    )
                output_hashes = self._hash_outputs(outputs)
            finally:
                shutil.rmtree(first_root, ignore_errors=True)
                shutil.rmtree(second_root, ignore_errors=True)
        return ScriptRunReport(
            status="passed",
            same_attempt_feedback=False,
            receipts=tuple(receipts),
            output_hashes=output_hashes,
            deterministic_match=deterministic_match,
        )

    def run(self, script: str | Path, **kwargs: Any) -> ScriptRunReport:
        """Explicit alias for :meth:`run_pipeline`."""

        return self.run_pipeline(script, **kwargs)


__all__ = [
    "ANALYSIS_CONTEXT_ENV",
    "ANALYSIS_CONTEXT_SCHEMA_VERSION",
    "ANALYSIS_OUTPUT_ROOT_ENV",
    "ANALYSIS_PHASE_ENV",
    "ANALYSIS_SAMPLE_LIMIT_ENV",
    "BoundAnalysisContext",
    "CatalogSnapshot",
    "ControlledScriptRunner",
    "ScriptExecutionReceipt",
    "ScriptRunReport",
    "load_bound_analysis_context",
]
