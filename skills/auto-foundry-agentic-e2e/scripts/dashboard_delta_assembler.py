#!/usr/bin/env python3
"""Atomically assemble a deterministic dashboard delta for one active run generation.

The ordinary V4 assembler intentionally creates a new reproducibility
namespace.  This module is the append-only companion used after
``RequirementRunExtension`` has admitted a cumulative generation.  It reads
only accepted bundles, committed integration records, the public LEM
projection, the active plan/generation metadata, parent product artifacts,
and frozen registry/telemetry metadata.  A parent generation is never edited;
the candidate is staged below the active generation and published with one
directory rename.
"""

from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import secrets
import sys
import tempfile
import threading
from typing import Any, Mapping, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS/Linux provide fcntl
    fcntl = None  # type: ignore[assignment]

try:
    from auto_foundry_core.lifecycle import RunLifecycle
    from auto_foundry_core.workspace import AllowedRootError, RunContext
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    _SRC = Path(__file__).resolve().parents[3] / "src"
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    from auto_foundry_core.lifecycle import RunLifecycle
    from auto_foundry_core.workspace import AllowedRootError, RunContext


DELTA_SCHEMA = "dashboard.delta_receipt.v1"
FIXTURE_SCHEMA = "dashboard.reviewed_fixture.v4"
CHART_MAP_SCHEMA = "dashboard.chart_map.v4"
ASSEMBLER_SCHEMA = "dashboard.assembler_receipt.v1"
_TRANSACTION_SCHEMA = "dashboard.generation_transaction.v2"
_TRANSACTION_FILE = ".dashboard_transaction.json"
_TRANSACTION_BACKUP_DIR = ".dashboard_transaction.previous"
_PRODUCT_MANIFEST_KEYS = {
    "schema_version",
    "product_type",
    "run_id",
    "status",
    "terminal",
    "source_status",
    "new_analytics",
    "freeze_markers",
    "lifecycle",
    "dashboard",
    "lem",
    "assets",
    "lineage",
    "limitations",
    "presentation_plan_ref",
    "presentation_plan_sha256",
    "manager_widget_ids",
}
_PRODUCT_LIMITATIONS = [
    "Presentation values are copied from accepted answer/integration records only.",
    "No source reads, model calls, or new analytics are performed by the delta assembler.",
]
_DELTA_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "run_id",
        "generation_id",
        "generation_ordinal",
        "parent_generation_id",
        "source_policy",
        "new_analytics",
        "parent",
        "request_binding",
        "plan_binding",
        "input_items",
        "outputs",
        "output_hashes",
        "site_binding",
        "freeze_inputs",
        "old_projection",
        "new_projection",
        "affected_paths",
        "unchanged_paths",
        "rollback_parent",
        "widget_count",
        "domain_count",
        "retry",
        "presentation_plan_ref",
        "presentation_plan_sha256",
        "manager_widget_ids",
    }
)
_DELTA_PARENT_KEYS = frozenset(
    {"product_manifest_ref", "product_manifest_sha256", "receipt_ref", "receipt_sha256", "site_binding", "site_tree_sha256"}
)
_DELTA_REQUEST_KEYS = frozenset(
    {
        "parent_receipt_ref",
        "parent_receipt_sha256",
        "parent_site_tree_sha256",
        "generation_id",
        "generation_ordinal",
        "parent_generation_id",
        "generation_manifest_hash",
        "admission_state_hash",
        "parent_state_ref",
        "parent_state_hash",
        "parent_state_sha256",
        "parent_plan_ref",
        "parent_plan_hash",
        "state_ref",
        "plan_ref",
        "plan_sha256",
        "admission_plan_sha256",
        "output_root_ref",
        "route",
        "new_items",
        "projection_hash",
        "presentation_plan_ref",
        "presentation_plan_sha256",
        "manager_widget_ids",
    }
)
# A planless receipt is a narrowly scoped historical shape used only as the
# immutable old target during an explicit same-generation plan migration.
# Keep this separate from the current schema instead of making the ordinary
# validator permissive: every field other than the additive presentation-plan
# fields must still be present and exact.
_LEGACY_DELTA_RECEIPT_KEYS = frozenset(
    _DELTA_RECEIPT_KEYS - {"presentation_plan_ref", "presentation_plan_sha256", "manager_widget_ids"}
)
_LEGACY_DELTA_REQUEST_KEYS = frozenset(
    _DELTA_REQUEST_KEYS - {"presentation_plan_ref", "presentation_plan_sha256", "manager_widget_ids"}
)
_DELTA_PLAN_KEYS = frozenset({"ref", "sha256", "admission_sha256", "generation_id", "revision", "route"})
_DELTA_OUTPUT_KEYS = frozenset({"fixture_ref", "chart_map_ref", "chart_registry_ref", "site_ref", "receipt_ref"})
_DELTA_HASH_KEYS = frozenset({"fixture_sha256", "chart_map_sha256", "chart_registry_sha256", "site_manifest_sha256"})
_DELTA_PROJECTION_KEYS = frozenset({"projection_hash", "export_sha256"})
_DELTA_ROLLBACK_KEYS = frozenset(
    {"generation_id", "product_manifest_ref", "product_manifest_sha256", "receipt_ref", "receipt_sha256", "site_tree_sha256"}
)
_DELTA_INPUT_KEYS = frozenset(
    {"item_id", "accepted_content_hash", "accepted_manifest_hash", "integration_manifest_hash", "record_count"}
)
_DELTA_PATH_KEYS = frozenset({"path", "sha256"})
_DELTA_FREEZE_KEYS = frozenset(
    {
        "projection_hash",
        "export_sha256",
        "item_order",
        "bindings",
        "summary",
        "prepared_registry",
        "prepared_index",
        "telemetry",
        "product_manifest_ref",
        "product_manifest_sha256",
        "freeze_markers",
    }
)
_DELTA_REGISTRY_KEYS = frozenset({"ref", "present", "descriptor_count", "sha256"})
_DELTA_INDEX_KEYS = frozenset({"ref", "present", "sha256"})
_DELTA_TELEMETRY_KEYS = frozenset({"sha256", "assets"})

_THREAD_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


class DashboardDeltaError(ValueError):
    """Raised when an active-generation dashboard delta is not safe to publish."""


class _GenerationChanged(DashboardDeltaError):
    """Internal retry signal when the active generation changed during assembly."""


_ASSEMBLER: Any | None = None
_LEGACY_BRIDGE: Any | None = None


def _assembler() -> Any:
    """Load the existing assembler as a sibling module without package writes."""

    global _ASSEMBLER
    if _ASSEMBLER is not None:
        return _ASSEMBLER
    path = Path(__file__).with_name("dashboard_assembler.py")
    spec = importlib.util.spec_from_file_location("dashboard_assembler_for_delta", path)
    if spec is None or spec.loader is None:
        raise DashboardDeltaError("dashboard assembler cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _ASSEMBLER = module
    return module


def _legacy_bridge() -> Any:
    """Load the legacy G-0001 bridge validator without package writes."""

    global _LEGACY_BRIDGE
    if _LEGACY_BRIDGE is not None:
        return _LEGACY_BRIDGE
    path = Path(__file__).with_name("dashboard_legacy_parent_bootstrap.py")
    spec = importlib.util.spec_from_file_location("dashboard_legacy_parent_bridge_for_delta", path)
    if spec is None or spec.loader is None:
        raise DashboardDeltaError("legacy parent bridge cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _LEGACY_BRIDGE = module
    return module
    return module


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_hash(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _lexical_candidate(
    context: RunContext,
    reference: str | Path,
    *,
    product: bool,
    label: str,
) -> Path:
    """Reject lexical symlink aliases before ``RunContext`` resolves a path.

    ``RunContext`` deliberately resolves symlinks for containment.  Product
    publication has a stricter boundary: a receipt/reference that names a
    symlink alias is rejected even when its target remains inside the run.
    This check walks the un-resolved components first, so an external target
    is never opened while validating a supplied reference.
    """

    raw = Path(reference).expanduser()
    base = context.product_root if product else context.run_root
    candidate = raw if raw.is_absolute() else base / raw
    if any(part in {".."} for part in raw.parts):
        raise DashboardDeltaError(f"{label} contains traversal: {reference}")
    try:
        relative = candidate.relative_to(base)
    except ValueError as exc:
        raise DashboardDeltaError(f"{label} escapes its run boundary: {reference}") from exc
    current = base
    if current.is_symlink():
        raise DashboardDeltaError(f"{label} root is symlinked: {base}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise DashboardDeltaError(f"{label} uses a symlink alias: {reference}")
    return candidate


def _safe_run_path(context: RunContext, reference: str | Path, *, label: str = "run path") -> Path:
    _lexical_candidate(context, reference, product=False, label=label)
    try:
        return context.resolve_run_path(reference)
    except (AllowedRootError, OSError, ValueError) as exc:
        raise DashboardDeltaError(f"{label} is outside the run boundary: {reference}") from exc


def _safe_product_path(context: RunContext, reference: str | Path, *, label: str = "product path") -> Path:
    _lexical_candidate(context, reference, product=True, label=label)
    try:
        return context.resolve_product_path(reference)
    except (AllowedRootError, OSError, ValueError) as exc:
        raise DashboardDeltaError(f"{label} is outside the product boundary: {reference}") from exc


def _fsync_directory(directory: Path) -> None:
    """Durably publish a rename/entry update on the containing directory."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    """Durably flush every staged file and directory before publication."""

    if root.is_symlink() or not root.is_dir():
        raise DashboardDeltaError(f"staged tree is not a regular directory: {root}")
    directories: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda value: (len(value.parts), value.as_posix())):
        if path.is_symlink():
            raise DashboardDeltaError(f"staged tree contains a symlink: {path}")
        if path.is_file():
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        elif path.is_dir():
            directories.append(path)
        else:
            raise DashboardDeltaError(f"staged tree contains an unsupported entry: {path}")
    for directory in sorted(directories, key=lambda value: (len(value.parts), value.as_posix()), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(root)


@contextmanager
def _generation_lock(context: RunContext, generation_id: str):
    """Serialize one active generation across threads and processes."""

    if fcntl is None:
        raise DashboardDeltaError("dashboard delta requires process file-lock support")
    generation_root = _safe_product_path(
        context,
        f"generations/{generation_id}",
        label="generation lock namespace",
    )
    transaction_intent = generation_root.parent / f".{generation_id}.dashboard_transaction.json"
    # During v2 recovery the target may be absent between the two renames.
    # Do not recreate an empty target merely to place a lock marker; doing so
    # would hide the authoritative backup/candidate classification.
    recovering_missing_target = transaction_intent.is_file() and not generation_root.exists()
    if not recovering_missing_target:
        generation_root.mkdir(parents=True, exist_ok=True)
    _lexical_candidate(context, generation_root, product=True, label="generation lock namespace")
    _fsync_directory(generation_root.parent)
    # Keep the advisory lock outside the swappable generation directory.  A
    # descriptor held on a lock file that is renamed with the old generation
    # would otherwise serialize only the old inode while a concurrent process
    # opens a fresh lock in the newly published tree.  A local marker remains
    # in the generation root and is excluded from product bindings.
    lock_path = generation_root.parent / f".{generation_id}.dashboard_delta.lock"
    _lexical_candidate(context, lock_path, product=True, label="generation lock")
    marker_path = generation_root / ".dashboard_delta.lock"
    if generation_root.exists():
        _lexical_candidate(context, marker_path, product=True, label="generation lock marker")
        if marker_path.exists() and marker_path.is_symlink():
            raise DashboardDeltaError("generation lock marker is symlinked")
        if not marker_path.exists():
            marker_path.touch(mode=0o600, exist_ok=False)
            _fsync_directory(generation_root)
    key = str(lock_path)
    with _THREAD_LOCK_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    thread_lock.acquire()
    descriptor: int | None = None
    try:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        os.fsync(descriptor)
        _fsync_directory(generation_root if generation_root.exists() else generation_root.parent)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise DashboardDeltaError(f"generation lock cannot be acquired: {lock_path}") from exc
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        thread_lock.release()


def _read_json(context: RunContext, reference: str | Path, *, label: str) -> Mapping[str, Any]:
    path = _safe_run_path(context, reference, label=label)
    if not path.is_file() or path.is_symlink():
        raise DashboardDeltaError(f"{label} is missing or symlinked: {reference}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDeltaError(f"{label} is invalid: {reference}") from exc
    if not isinstance(value, Mapping):
        raise DashboardDeltaError(f"{label} must be a JSON object: {reference}")
    return value


def _read_bytes(context: RunContext, reference: str | Path, *, label: str) -> tuple[Path, bytes]:
    path = _safe_run_path(context, reference, label=label)
    if not path.is_file() or path.is_symlink():
        raise DashboardDeltaError(f"{label} is missing or symlinked: {reference}")
    try:
        return path, path.read_bytes()
    except OSError as exc:
        raise DashboardDeltaError(f"{label} cannot be read: {reference}") from exc


def _relative_run_ref(context: RunContext, path: Path) -> str:
    try:
        return path.relative_to(context.run_root).as_posix()
    except ValueError as exc:
        raise DashboardDeltaError(f"path escapes run root: {path}") from exc


def _resolve_parent_receipt(context: RunContext, parent_ref: str | Path | None, lifecycle: RunLifecycle) -> tuple[Path, Mapping[str, Any]]:
    """Resolve an explicit parent receipt, with a narrow generation-manifest fallback."""

    candidates: list[Path] = []
    if parent_ref is not None:
        raw = _safe_run_path(context, parent_ref, label="parent receipt reference")
        if raw.is_dir():
            candidates.append(raw / "build_receipt.json")
        else:
            candidates.append(raw)
    else:
        metadata = lifecycle.generation_metadata
        parent_id = metadata.parent_generation_id if metadata is not None else None
        if parent_id is None:
            raise DashboardDeltaError("parent_receipt_ref is required for the legacy generation")
        parent_manifest = _parent_manifest_path(context, parent_id)
        if parent_manifest.is_file() and not parent_manifest.is_symlink():
            try:
                parent_value = json.loads(parent_manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DashboardDeltaError("parent product manifest is invalid") from exc
            if isinstance(parent_value, Mapping):
                dashboard = parent_value.get("dashboard")
                if isinstance(dashboard, Mapping):
                    value = dashboard.get("receipt_ref")
                    if isinstance(value, str) and value:
                        candidates.append(_safe_run_path(context, value, label="parent receipt reference"))
                assets = parent_value.get("assets")
                if isinstance(assets, list):
                    for asset in assets:
                        if not isinstance(asset, Mapping):
                            continue
                        value = asset.get("ref")
                        role = _text(asset.get("role")).lower()
                        if isinstance(value, str) and (value.endswith("build_receipt.json") or "receipt" in role):
                            candidates.append(_safe_run_path(context, value, label="parent receipt reference"))
    seen: set[Path] = set()
    for path in candidates:
        path = path.resolve(strict=False)
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file() or path.is_symlink():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DashboardDeltaError(f"parent receipt is invalid: {path}") from exc
        if not isinstance(value, Mapping):
            raise DashboardDeltaError("parent receipt must be a JSON object")
        if value.get("status") != "complete" or value.get("schema_version") not in {ASSEMBLER_SCHEMA, DELTA_SCHEMA}:
            continue
        return path, value
    raise DashboardDeltaError("a complete parent dashboard receipt is required")


def _validate_site_binding(site_root: Path, expected: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    assembler = _assembler()
    try:
        actual = assembler._site_tree_binding(site_root)
    except Exception as exc:
        raise DashboardDeltaError(f"{label} site binding is invalid") from exc
    if actual != dict(expected):
        raise DashboardDeltaError(f"{label} site tree/hash does not match its receipt")
    return actual


def _validate_delta_receipt_shape(receipt: Mapping[str, Any]) -> None:
    """Reject unknown/missing receipt fields before any retry recovery."""

    if set(receipt) != _DELTA_RECEIPT_KEYS:
        raise DashboardDeltaError("delta receipt schema fields are not exact")
    plan_ref = receipt.get("presentation_plan_ref")
    plan_hash = receipt.get("presentation_plan_sha256")
    manager_ids = receipt.get("manager_widget_ids")
    if plan_ref is not None and (not isinstance(plan_ref, str) or not plan_ref):
        raise DashboardDeltaError("delta receipt presentation plan reference is invalid")
    if plan_hash is not None and not _is_sha256(plan_hash):
        raise DashboardDeltaError("delta receipt presentation plan hash is invalid")
    if not isinstance(manager_ids, list) or len(set(manager_ids)) != len(manager_ids) or any(not isinstance(value, str) or not value for value in manager_ids):
        raise DashboardDeltaError("delta receipt manager_widget_ids are invalid")
    nested = (
        ("parent", _DELTA_PARENT_KEYS),
        ("request_binding", _DELTA_REQUEST_KEYS),
        ("plan_binding", _DELTA_PLAN_KEYS),
        ("outputs", _DELTA_OUTPUT_KEYS),
        ("output_hashes", _DELTA_HASH_KEYS),
        ("rollback_parent", _DELTA_ROLLBACK_KEYS),
    )
    for field, expected in nested:
        value = receipt.get(field)
        if not isinstance(value, Mapping) or set(value) != expected:
            raise DashboardDeltaError(f"delta receipt {field} schema is not exact")
    for field in ("old_projection", "new_projection"):
        value = receipt.get(field)
        if not isinstance(value, Mapping) or set(value) != _DELTA_PROJECTION_KEYS:
            raise DashboardDeltaError(f"delta receipt {field} schema is not exact")
    for field in ("input_items", "affected_paths", "unchanged_paths"):
        value = receipt.get(field)
        if not isinstance(value, list):
            raise DashboardDeltaError(f"delta receipt {field} must be a list")
    for item in receipt["input_items"]:
        if not isinstance(item, Mapping) or set(item) != _DELTA_INPUT_KEYS:
            raise DashboardDeltaError("delta receipt input item schema is not exact")
    for field in ("affected_paths", "unchanged_paths"):
        for item in receipt[field]:
            if not isinstance(item, Mapping) or set(item) != _DELTA_PATH_KEYS:
                raise DashboardDeltaError(f"delta receipt {field} entry schema is not exact")
    freeze = receipt.get("freeze_inputs")
    if not isinstance(freeze, Mapping) or set(freeze) != _DELTA_FREEZE_KEYS:
        raise DashboardDeltaError("delta receipt freeze schema is not exact")
    value = freeze.get("prepared_registry")
    if not isinstance(value, Mapping) or set(value) != _DELTA_REGISTRY_KEYS:
        raise DashboardDeltaError("delta receipt freeze prepared_registry schema is not exact")
    value = freeze.get("prepared_index")
    if not isinstance(value, Mapping) or set(value) != _DELTA_INDEX_KEYS:
        raise DashboardDeltaError("delta receipt freeze prepared_index schema is not exact")
    telemetry = freeze.get("telemetry")
    if not isinstance(telemetry, Mapping) or set(telemetry) != _DELTA_TELEMETRY_KEYS:
        raise DashboardDeltaError("delta receipt freeze telemetry schema is not exact")
    markers = freeze.get("freeze_markers")
    if not isinstance(markers, Mapping) or set(markers) != {"answers_frozen", "living_enterprise_model_frozen", "prepared_data_registry_frozen", "dashboard_frozen", "telemetry_frozen"}:
        raise DashboardDeltaError("delta receipt freeze marker schema is not exact")


def _parent_output_root(context: RunContext, receipt_path: Path, receipt: Mapping[str, Any]) -> tuple[Path, dict[str, Path]]:
    outputs = receipt.get("outputs")
    hashes = receipt.get("output_hashes")
    if not isinstance(outputs, Mapping) or not isinstance(hashes, Mapping):
        raise DashboardDeltaError("parent receipt lacks output bindings")
    refs = {}
    for key in ("fixture_ref", "chart_map_ref", "chart_registry_ref", "site_ref", "receipt_ref"):
        value = outputs.get(key)
        if not isinstance(value, str) or not value:
            raise DashboardDeltaError(f"parent receipt output reference is missing: {key}")
        path = _safe_run_path(context, value, label=f"parent {key}")
        if path.is_symlink():
            raise DashboardDeltaError(f"parent output is symlinked: {key}")
        refs[key] = path
    if refs["receipt_ref"].resolve(strict=False) != receipt_path.resolve(strict=False):
        raise DashboardDeltaError("parent receipt path does not match its output binding")
    expected_hashes = {
        "fixture_ref": hashes.get("fixture_sha256"),
        "chart_map_ref": hashes.get("chart_map_sha256"),
        "chart_registry_ref": hashes.get("chart_registry_sha256"),
    }
    for key, expected in expected_hashes.items():
        if not _is_sha256(expected) or not refs[key].is_file() or _sha256_bytes(refs[key].read_bytes()) != expected:
            raise DashboardDeltaError(f"parent output hash mismatch: {key}")
    site = refs["site_ref"]
    _validate_site_binding(site, receipt.get("site_binding") or {}, label="parent")
    site_manifest = site / "site_manifest.json"
    if not site_manifest.is_file() or site_manifest.is_symlink() or not _is_sha256(hashes.get("site_manifest_sha256")):
        raise DashboardDeltaError("parent site manifest binding is missing")
    if _sha256_bytes(site_manifest.read_bytes()) != hashes["site_manifest_sha256"]:
        raise DashboardDeltaError("parent site manifest hash mismatch")
    root = receipt_path.parent
    try:
        for value in refs.values():
            value.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise DashboardDeltaError("parent output references must share one product namespace") from exc
    return root, refs


def _validate_delta_output(
    context: RunContext,
    receipt_path: Path,
    receipt: Mapping[str, Any],
    *,
    legacy_raw_receipt: Mapping[str, Any] | None = None,
    legacy_raw_bytes: bytes | None = None,
) -> None:
    """Revalidate an already-published child without rewriting it."""

    _validate_delta_receipt_shape(receipt)
    receipt_bytes = receipt_path.read_bytes()
    if receipt_bytes != _canonical_bytes(receipt):
        # The only permitted non-canonical input is a historical planless
        # receipt being used as the immutable old target while an explicit
        # presentation plan is published.  Its raw parsed shape is checked
        # independently below; all ordinary retries and any plan-bearing
        # non-canonical receipt remain fail-closed.
        if legacy_raw_receipt is None:
            raise DashboardDeltaError("delta receipt is not canonical")
        _validate_planless_legacy_receipt_shape(legacy_raw_receipt, raw_bytes=legacy_raw_bytes)
    outputs = receipt.get("outputs")
    hashes = receipt.get("output_hashes")
    if not isinstance(outputs, Mapping) or not isinstance(hashes, Mapping):
        raise DashboardDeltaError("delta receipt lacks output bindings")
    expected = {
        "fixture_ref": (outputs.get("fixture_ref"), hashes.get("fixture_sha256")),
        "chart_map_ref": (outputs.get("chart_map_ref"), hashes.get("chart_map_sha256")),
        "chart_registry_ref": (outputs.get("chart_registry_ref"), hashes.get("chart_registry_sha256")),
    }
    for label, (reference, digest) in expected.items():
        if not isinstance(reference, str) or not _is_sha256(digest):
            raise DashboardDeltaError(f"delta output binding is invalid: {label}")
        path = _safe_run_path(context, reference, label=f"delta {label}")
        if not path.is_file() or path.is_symlink() or _sha256_bytes(path.read_bytes()) != digest:
            raise DashboardDeltaError(f"delta output hash mismatch: {label}")
    site_ref = outputs.get("site_ref")
    if not isinstance(site_ref, str) or not site_ref:
        raise DashboardDeltaError("delta site reference is missing")
    site = _safe_run_path(context, site_ref, label="delta site reference")
    _validate_site_binding(site, receipt.get("site_binding") or {}, label="delta")
    site_manifest = site / "site_manifest.json"
    if not site_manifest.is_file() or site_manifest.is_symlink() or not _is_sha256(hashes.get("site_manifest_sha256")):
        raise DashboardDeltaError("delta site manifest binding is missing")
    if _sha256_bytes(site_manifest.read_bytes()) != hashes["site_manifest_sha256"]:
        raise DashboardDeltaError("delta site manifest hash mismatch")
    receipt_ref = outputs.get("receipt_ref")
    if not isinstance(receipt_ref, str) or _safe_run_path(context, receipt_ref, label="delta receipt reference").resolve(strict=False) != receipt_path.resolve(strict=False):
        raise DashboardDeltaError("delta receipt output reference is invalid")


def _load_plan(context: RunContext, lifecycle: RunLifecycle) -> tuple[Mapping[str, Any], Path, str]:
    plan_path = _safe_run_path(context, lifecycle.plan_path, label="active generation plan")
    if plan_path.is_symlink() or not plan_path.is_file():
        raise DashboardDeltaError("active generation plan is missing")
    try:
        value = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDeltaError("active generation plan is invalid") from exc
    if not isinstance(value, Mapping):
        raise DashboardDeltaError("active generation plan must be an object")
    return value, plan_path, _sha256_bytes(plan_path.read_bytes())


def _assert_live_plan_binding(
    context: RunContext,
    lifecycle: RunLifecycle,
    expected_path: Path,
    expected_hash: str,
) -> None:
    """Re-read the authoritative active plan at a publication boundary."""

    live_path = _safe_run_path(context, _relative_run_ref(context, lifecycle.plan_path), label="active generation plan")
    if live_path != expected_path:
        raise _GenerationChanged("active generation plan reference changed during delta publication")
    if live_path.is_symlink() or not live_path.is_file():
        raise DashboardDeltaError("active generation plan is missing or symlinked")
    try:
        live_bytes = live_path.read_bytes()
        live_value = json.loads(live_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDeltaError("active generation plan is invalid during publication") from exc
    if not isinstance(live_value, Mapping) or live_bytes != _canonical_bytes(live_value):
        raise DashboardDeltaError("active generation plan is not canonical during publication")
    if _sha256_bytes(live_bytes) != expected_hash:
        raise _GenerationChanged("active generation plan changed during delta publication")


def _assert_live_presentation_plan_binding(
    context: RunContext,
    reference: str,
    expected_bytes: bytes,
    expected_hash: str,
) -> None:
    """Reject a presentation-plan change after scratch rendering.

    Presentation plans are immutable inputs to a candidate render, but their
    writer does not necessarily share the product publication lock.  Re-read
    the canonical run-relative reference immediately before the final
    equality/staging decision so a plan edited during scratch rendering can
    never be paired with the candidate produced from its predecessor bytes.
    """

    path = _safe_run_path(context, reference, label="business presentation plan")
    if path.is_symlink() or not path.is_file():
        raise DashboardDeltaError("business presentation plan is missing or symlinked during publication")
    try:
        live_bytes = path.read_bytes()
        live_value = json.loads(live_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDeltaError("business presentation plan is invalid during publication") from exc
    if not isinstance(live_value, Mapping) or live_bytes != _canonical_bytes(live_value):
        raise DashboardDeltaError("business presentation plan is not canonical during publication")
    live_hash = _sha256_bytes(live_bytes)
    if live_hash != expected_hash or live_bytes != expected_bytes:
        raise DashboardDeltaError("business presentation plan changed during delta publication")


def _state_manifest_hash(value: Mapping[str, Any]) -> str:
    """Compute the lifecycle state manifest hash without its self field."""

    unsigned = {key: item for key, item in value.items() if key != "manifest_hash"}
    return _json_hash(unsigned)


def _authoritative_state_binding(context: RunContext, lifecycle: RunLifecycle) -> tuple[Path, Mapping[str, Any], str, str]:
    """Load the current state bytes and prove both logical and byte hashes."""

    path = _safe_run_path(context, _relative_run_ref(context, lifecycle.state_path), label="active lifecycle state")
    if path.is_symlink() or not path.is_file():
        raise DashboardDeltaError("active lifecycle state is missing or symlinked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDeltaError("active lifecycle state is invalid") from exc
    if not isinstance(value, Mapping) or path.read_bytes() != _canonical_bytes(value):
        raise DashboardDeltaError("active lifecycle state is not canonical")
    logical_hash = _state_manifest_hash(value)
    if value.get("manifest_hash") != logical_hash or lifecycle.snapshot.manifest_hash != logical_hash:
        raise DashboardDeltaError("active lifecycle state hash does not match authoritative bytes")
    return path, value, logical_hash, _sha256_bytes(raw)


def _parent_generation_bindings(
    context: RunContext,
    metadata: Any,
) -> tuple[Path, str, str, Path, str]:
    """Validate generation parent hashes against its immediate state/plan files."""

    parent_id = _text(metadata.parent_generation_id)
    if parent_id == "G-0001":
        state_ref = "run_state.json"
        plan_ref = "requirement_supervisor_plan.json"
    else:
        state_ref = f"extensions/{parent_id}/run_state.json"
        plan_ref = f"extensions/{parent_id}/requirement_supervisor_plan.json"
    state_path = _safe_run_path(context, state_ref, label="immediate parent lifecycle state")
    plan_path = _safe_run_path(context, plan_ref, label="immediate parent requirement plan")
    if state_path.is_symlink() or not state_path.is_file() or plan_path.is_symlink() or not plan_path.is_file():
        raise DashboardDeltaError("immediate parent state or plan is missing/symlinked")
    try:
        state_raw = state_path.read_bytes()
        state_value = json.loads(state_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDeltaError("immediate parent lifecycle state is invalid") from exc
    if not isinstance(state_value, Mapping) or state_raw != _canonical_bytes(state_value):
        raise DashboardDeltaError("immediate parent lifecycle state is not canonical")
    state_hash = _state_manifest_hash(state_value)
    if state_value.get("manifest_hash") != state_hash or metadata.parent_state_hash != state_hash:
        raise DashboardDeltaError("generation parent_state_hash does not match its parent state file")
    plan_hash = _sha256_bytes(plan_path.read_bytes())
    if metadata.parent_plan_hash != plan_hash:
        raise DashboardDeltaError("generation parent_plan_hash does not match its parent plan file")
    return state_path, state_hash, _sha256_bytes(state_raw), plan_path, plan_hash


def _route_map(route: Mapping[str, Any], added_ids: Sequence[str]) -> dict[str, Mapping[str, Any]]:
    if not isinstance(route, Mapping):
        raise DashboardDeltaError("explicit route metadata is required")
    routes: dict[str, Mapping[str, Any]] = {}
    raw_routes = route.get("routes")
    if raw_routes is not None:
        if not isinstance(raw_routes, Mapping):
            raise DashboardDeltaError("route.routes must be an object keyed by requirement ID")
        for item_id in added_ids:
            value = raw_routes.get(item_id)
            if not isinstance(value, Mapping):
                raise DashboardDeltaError(f"route is missing explicit metadata for {item_id}")
            routes[item_id] = value
    else:
        for item_id in added_ids:
            routes[item_id] = route
    for item_id, value in routes.items():
        group_id = value.get("group_id") or value.get("section_id")
        if not isinstance(group_id, str) or not group_id.strip() or not re.fullmatch(r"[A-Za-z0-9_.-]+", group_id.strip()):
            raise DashboardDeltaError(f"route for {item_id} requires an explicit safe group_id")
        kind = value.get("kind", "existing")
        if kind not in {"existing", "new"}:
            raise DashboardDeltaError(f"route kind for {item_id} must be existing or new")
        if kind == "new":
            title = value.get("title")
            order = value.get("order")
            if not isinstance(title, str) or not title.strip():
                raise DashboardDeltaError(f"new route for {item_id} requires a stable title")
            if isinstance(order, bool) or not isinstance(order, int) or order < 1:
                raise DashboardDeltaError(f"new route for {item_id} requires a positive order")
    return routes


def _validate_plan_membership(plan: Mapping[str, Any], added_ids: Sequence[str]) -> None:
    records = plan.get("input_records")
    groups = plan.get("groups")
    if not isinstance(records, list) or not isinstance(groups, list):
        raise DashboardDeltaError("active generation plan lacks typed input_records/groups")
    ids = [record.get("requirement_id") for record in records if isinstance(record, Mapping)]
    if any(item_id not in ids for item_id in added_ids):
        raise DashboardDeltaError("active generation plan does not contain every added requirement")
    grouped: set[str] = set()
    for group in groups:
        if isinstance(group, Mapping):
            values = group.get("requirement_ids")
            if isinstance(values, list):
                grouped.update(str(value) for value in values)
    if any(item_id not in grouped for item_id in added_ids):
        raise DashboardDeltaError("active generation plan does not route every added requirement")


def _parent_domain_for_item(parent_fixture: Mapping[str, Any], item_id: str) -> str | None:
    assembler = _assembler()
    slug = assembler._slug(item_id)
    domains = parent_fixture.get("domains")
    if not isinstance(domains, list):
        return None
    for domain in domains:
        if not isinstance(domain, Mapping):
            continue
        domain_id = _text(domain.get("id"))
        flows = domain.get("decision_flow")
        if not isinstance(flows, list):
            continue
        for flow in flows:
            if not isinstance(flow, Mapping):
                continue
            if _text(flow.get("id")) == f"{domain_id}-{slug}":
                return domain_id
            widget_ids = flow.get("widget_ids")
            if isinstance(widget_ids, list):
                widgets = parent_fixture.get("widgets")
                if isinstance(widgets, list) and any(
                    isinstance(widget, Mapping) and widget.get("requirement_id") == item_id and widget.get("id") in widget_ids
                    for widget in widgets
                ):
                    return domain_id
    return None


def _validate_routes_against_plan(
    plan: Mapping[str, Any],
    parent_fixture: Mapping[str, Any],
    old_ids: Sequence[str],
    added_ids: Sequence[str],
    routes: Mapping[str, Mapping[str, Any]],
) -> None:
    """Require explicit dashboard routes to agree with active plan groups."""

    raw_groups = plan.get("groups")
    if not isinstance(raw_groups, list):
        raise DashboardDeltaError("active generation plan groups are invalid")
    old_set = set(old_ids)
    new_set = set(added_ids)
    plan_groups: list[set[str]] = []
    for raw in raw_groups:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("requirement_ids"), list):
            continue
        plan_groups.append({str(value) for value in raw["requirement_ids"]})
    for item_id in added_ids:
        containing = [group for group in plan_groups if item_id in group]
        if len(containing) != 1:
            raise DashboardDeltaError(f"active plan route for {item_id} is ambiguous")
        group = containing[0]
        prior = group & old_set
        route = routes[item_id]
        kind = route.get("kind", "existing")
        group_id = _text(route.get("group_id") or route.get("section_id"))
        if prior:
            if kind != "existing":
                raise DashboardDeltaError(f"plan group containing prior items requires an existing route: {item_id}")
            expected = {_parent_domain_for_item(parent_fixture, value) for value in sorted(prior)}
            expected.discard(None)
            if len(expected) != 1 or group_id not in expected:
                raise DashboardDeltaError(f"route for {item_id} disagrees with its active plan group")
        else:
            if kind != "new":
                raise DashboardDeltaError(f"plan group containing only new items requires a new route: {item_id}")
            if not group <= new_set:
                raise DashboardDeltaError(f"new route for {item_id} contains an unadmitted requirement")
    # Sibling new items in one plan group must share one explicit section.
    for group in plan_groups:
        siblings = sorted(group & new_set)
        if not siblings or group & old_set:
            continue
        signatures = {
            (_text(routes[item].get("group_id") or routes[item].get("section_id")), _text(routes[item].get("title")), routes[item].get("order"))
            for item in siblings
        }
        if len(signatures) != 1:
            raise DashboardDeltaError("new requirements in one plan group must share one section route")


def _load_delta_projection_metadata(context: RunContext, item_ids: Sequence[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read cumulative public projection/registry/telemetry without stale parent checks."""

    try:
        from auto_foundry_core.lem_projection import LivingEnterpriseModelProjector
        from auto_foundry_core.prepared import PreparedAssetRegistry
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise DashboardDeltaError("public LEM/prepared registry types are unavailable") from exc
    try:
        projection = LivingEnterpriseModelProjector.project(context, item_ids=item_ids)
    except Exception as exc:
        raise DashboardDeltaError("accepted/committed cumulative LEM projection failed") from exc
    exported = projection.model.export()
    summary = {
        "ontology_items": len(exported.get("ontology", [])) if isinstance(exported.get("ontology"), list) else 0,
        "relationships": len(exported.get("relationships", {})) if isinstance(exported.get("relationships"), Mapping) else 0,
        "canonical_mappings": len(exported.get("canonical_mappings", [])) if isinstance(exported.get("canonical_mappings"), list) else 0,
        "identity_decisions": len(exported.get("identity_decisions", [])) if isinstance(exported.get("identity_decisions"), list) else 0,
        "prepared_assets": len(exported.get("prepared_assets", [])) if isinstance(exported.get("prepared_assets"), list) else 0,
        "knowledge": len(exported.get("knowledge", {})) if isinstance(exported.get("knowledge"), Mapping) else 0,
        "resolution_bindings": len(projection.resolution_bindings),
        "item_bindings": len(projection.bindings),
    }
    registry = PreparedAssetRegistry(context)
    descriptors = registry.search(include_superseded=True)
    registry_path = _safe_run_path(context, registry.registry_path, label="prepared registry")
    index_path = _safe_run_path(context, registry.index_path, label="prepared index")
    registry_present = registry_path.is_file() and not registry_path.is_symlink()
    index_present = index_path.is_file() and not index_path.is_symlink()
    registry_hash = _sha256_bytes(registry_path.read_bytes()) if registry_present else _json_hash([item.to_dict() for item in descriptors])
    index_hash = _sha256_bytes(index_path.read_bytes()) if index_present else _json_hash({"entries": [item.to_dict() for item in descriptors]})
    telemetry_assets: dict[str, str] = {}
    for reference in ("telemetry/events.jsonl", "telemetry/inventory_counters.json"):
        path = _safe_run_path(context, reference, label=reference)
        if path.is_file() and not path.is_symlink():
            telemetry_assets[reference] = _sha256_bytes(path.read_bytes())
    telemetry_hash = _json_hash(telemetry_assets)
    markers = {
        "answers_frozen": True,
        "living_enterprise_model_frozen": True,
        "prepared_data_registry_frozen": bool(registry_present or (not descriptors)),
        "dashboard_frozen": True,
        "telemetry_frozen": bool(telemetry_assets),
    }
    metadata = {
        "projection_hash": projection.projection_hash,
        "export_sha256": _json_hash(exported),
        "item_order": list(projection.item_order),
        "bindings": [binding.to_dict() for binding in projection.bindings],
        "summary": summary,
        "prepared_registry": {"ref": "lem/prepared_data_registry.jsonl", "present": registry_present, "descriptor_count": len(descriptors), "sha256": registry_hash},
        "prepared_index": {"ref": "indexes/prepared_index.json", "present": index_present, "sha256": index_hash},
        "telemetry": {"sha256": telemetry_hash, "assets": telemetry_assets},
        "product_manifest_ref": None,
        "product_manifest_sha256": None,
        "freeze_markers": markers,
    }
    return summary, metadata


def _copy_tree(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise DashboardDeltaError(f"candidate namespace already exists: {destination}")
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("qa"))


def _copy_bound_parent(parent_root: Path, parent_refs: Mapping[str, Path], destination: Path) -> None:
    """Copy only receipt-bound dashboard inputs into a child staging root."""

    if destination.exists() or destination.is_symlink():
        raise DashboardDeltaError(f"candidate namespace already exists: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    for key in ("fixture_ref", "chart_map_ref", "chart_registry_ref"):
        source = parent_refs[key]
        relative = source.relative_to(parent_root)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    site_source = parent_refs["site_ref"]
    site_target = destination / site_source.relative_to(parent_root)
    shutil.copytree(site_source, site_target)


def _copy_bytes_tree(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise DashboardDeltaError(f"destination already exists: {destination}")
    shutil.copytree(source, destination)


def _site_manifest_update(site_path: Path, chart_map_path: Path) -> dict[str, Any]:
    manifest_path = site_path / "site_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise DashboardDeltaError("candidate renderer did not produce site_manifest.json")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDeltaError("candidate site manifest is invalid") from exc
    if not isinstance(value, Mapping):
        raise DashboardDeltaError("candidate site manifest is invalid")
    assembler = _assembler()
    non_manifest = assembler._site_tree_binding(site_path, exclude={"site_manifest.json"})
    updated = dict(value)
    updated["chart_map_sha256"] = _sha256_bytes(chart_map_path.read_bytes())
    updated["site_file_hashes"] = non_manifest["files"]
    updated["site_tree_sha256"] = non_manifest["tree_sha256"]
    updated["site_tree_file_count"] = non_manifest["file_count"]
    manifest_path.write_bytes(_canonical_bytes(updated))
    return updated


def _transaction_path(context: RunContext, generation_id: str) -> Path:
    return _safe_product_path(
        context,
        f"generations/{generation_id}/{_TRANSACTION_FILE}",
        label="generation dashboard transaction intent",
    )


def _relative_product_ref(context: RunContext, path: Path) -> str:
    try:
        return path.relative_to(context.product_root).as_posix()
    except ValueError as exc:
        raise DashboardDeltaError(f"transaction product path escaped products root: {path}") from exc


def _transaction_backup_dir(context: RunContext, generation_id: str) -> Path:
    return _safe_product_path(
        context,
        f"generations/{generation_id}/{_TRANSACTION_BACKUP_DIR}",
        label="generation dashboard transaction backup",
    )


def _transaction_tree_binding(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _assembler()._site_tree_binding(path)


def _transaction_file_hash(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise DashboardDeltaError(f"transaction-bound file is missing, non-regular, or symlinked: {path}")
    return _sha256_bytes(path.read_bytes())


def _transaction_write(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise DashboardDeltaError("generation dashboard transaction intent is symlinked")
    _write_atomic_json(path, value)


def _transaction_phase(path: Path, intent: Mapping[str, Any], phase: str, **updates: Any) -> dict[str, Any]:
    value = dict(intent)
    value["phase"] = phase
    value.update(updates)
    _transaction_write(path, value)
    return value


def _remove_transaction_path(path: Path) -> None:
    if path.is_symlink():
        raise DashboardDeltaError(f"transaction cleanup refuses symlink: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _copy_transaction_file(source: Path, destination: Path) -> str | None:
    digest = _transaction_file_hash(source)
    if digest is None:
        return None
    if destination.exists() or destination.is_symlink():
        raise DashboardDeltaError(f"transaction backup already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    descriptor = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(destination.parent)
    return digest


def _prepare_generation_transaction(
    context: RunContext,
    *,
    metadata: Any,
    final_root: Path,
    receipt_path: Path,
    product_manifest_path: Path,
    lifecycle: RunLifecycle,
    new_dashboard_binding: Mapping[str, Any],
    new_receipt_hash: str,
    staging_root: Path | None = None,
    preparing: bool = False,
) -> dict[str, Any]:
    """Durably record old/new bytes before copy or swap of a dashboard."""

    intent_path = _transaction_path(context, metadata.generation_id)
    backup_dir = _transaction_backup_dir(context, metadata.generation_id)
    if intent_path.exists() or intent_path.is_symlink() or backup_dir.exists() or backup_dir.is_symlink():
        raise DashboardDeltaError("generation dashboard transaction is already in progress")
    old_dashboard_binding = _transaction_tree_binding(final_root)
    old_manifest_hash = _transaction_file_hash(product_manifest_path)
    old_state_hash = _transaction_file_hash(lifecycle.state_path)
    backup_dir.mkdir(parents=True, exist_ok=False)
    _fsync_directory(backup_dir.parent)
    backup_manifest = backup_dir / "product_manifest.json"
    backup_state = backup_dir / "run_state.json"
    manifest_backup_hash = _copy_transaction_file(product_manifest_path, backup_manifest)
    state_backup_hash = _copy_transaction_file(lifecycle.state_path, backup_state)
    old_tree_hash = old_dashboard_binding.get("tree_sha256") if isinstance(old_dashboard_binding, Mapping) else None
    old_receipt_hash = None
    if receipt_path.is_file() and not receipt_path.is_symlink():
        old_receipt_hash = _sha256_bytes(receipt_path.read_bytes())
    intent = {
        "schema_version": _TRANSACTION_SCHEMA,
        "generation_id": metadata.generation_id,
        "phase": "prepared",
        "dashboard": {
            "path": _relative_product_ref(context, final_root),
            "staging_path": _relative_product_ref(context, staging_root) if staging_root is not None else None,
            "receipt_path": _relative_product_ref(context, receipt_path),
            "old_tree_sha256": old_tree_hash,
            "new_tree_sha256": new_dashboard_binding.get("tree_sha256"),
            "old_receipt_sha256": old_receipt_hash,
            "new_receipt_sha256": new_receipt_hash,
            "backup_path": _relative_product_ref(context, final_root.parent / f".{final_root.name}.previous"),
        },
        "product_manifest": {
            "root": "product",
            "path": _relative_product_ref(context, product_manifest_path),
            "old_sha256": old_manifest_hash,
            "new_sha256": None,
            "backup_path": _relative_product_ref(context, backup_manifest) if manifest_backup_hash else None,
        },
        "lifecycle": {
            "root": "run",
            "path": _relative_run_ref(context, lifecycle.state_path),
            "old_sha256": old_state_hash,
            "new_sha256": None,
            "backup_path": _relative_product_ref(context, backup_state) if state_backup_hash else None,
        },
        "backup_dir": _relative_product_ref(context, backup_dir),
    }
    if preparing:
        intent["phase"] = "preparing"
    try:
        _transaction_write(intent_path, intent)
    except Exception:
        _remove_transaction_path(backup_dir)
        raise
    return intent


def _load_generation_transaction(context: RunContext, metadata: Any) -> tuple[Path, dict[str, Any]] | None:
    intent_path = _transaction_path(context, metadata.generation_id)
    if not intent_path.exists() and not intent_path.is_symlink():
        return None
    if intent_path.is_symlink() or not intent_path.is_file():
        raise DashboardDeltaError("generation dashboard transaction intent is missing or symlinked")
    try:
        value = json.loads(intent_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDeltaError("generation dashboard transaction intent is invalid") from exc
    if not isinstance(value, Mapping) or value.get("schema_version") != _TRANSACTION_SCHEMA or value.get("generation_id") != metadata.generation_id:
        raise DashboardDeltaError("generation dashboard transaction intent binding is invalid")
    if value.get("phase") not in {"preparing", "prepared", "dashboard_published", "lifecycle_reconciled", "manifest_published"}:
        raise DashboardDeltaError("generation dashboard transaction phase is invalid")
    for section in ("dashboard", "product_manifest", "lifecycle"):
        if not isinstance(value.get(section), Mapping):
            raise DashboardDeltaError(f"generation dashboard transaction {section} binding is invalid")
    if value.get("phase") in {"preparing", "prepared"}:
        dashboard = value["dashboard"]
        if not isinstance(dashboard.get("staging_path"), str) or not dashboard["staging_path"]:
            raise DashboardDeltaError("generation dashboard transaction staging binding is missing")
    return intent_path, dict(value)


def _transaction_candidate_valid(context: RunContext, intent: Mapping[str, Any]) -> bool:
    dashboard = intent.get("dashboard")
    if not isinstance(dashboard, Mapping):
        return False
    expected_hash = dashboard.get("new_tree_sha256")
    candidates = [_safe_product_path(context, dashboard.get("path", ""), label="transaction dashboard candidate")]
    staging_ref = dashboard.get("staging_path")
    if isinstance(staging_ref, str) and staging_ref:
        candidates.insert(0, _safe_product_path(context, staging_ref, label="transaction dashboard staging"))
    for candidate in candidates:
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        binding = _transaction_tree_binding(candidate)
        if isinstance(binding, Mapping) and binding.get("tree_sha256") == expected_hash:
            return True
    return False


def _transaction_old_backup_valid(context: RunContext, intent: Mapping[str, Any]) -> bool:
    dashboard = intent.get("dashboard")
    if not isinstance(dashboard, Mapping):
        return False
    old_hash = dashboard.get("old_tree_sha256")
    backup = _safe_product_path(context, dashboard.get("backup_path", ""), label="transaction dashboard backup")
    if old_hash is None:
        return not backup.exists() and not backup.is_symlink()
    if not backup.is_dir() or backup.is_symlink():
        return False
    binding = _transaction_tree_binding(backup)
    return isinstance(binding, Mapping) and binding.get("tree_sha256") == old_hash


def _transaction_current_file_hash(context: RunContext, section: Mapping[str, Any]) -> str | None:
    if section.get("root") == "product":
        path = _safe_product_path(context, section.get("path", ""), label="transaction-bound product file")
    else:
        path = _safe_run_path(context, section.get("path", ""), label="transaction-bound run file")
    return _transaction_file_hash(path)


def _restore_generation_transaction(context: RunContext, intent_path: Path, intent: Mapping[str, Any]) -> None:
    """Restore all old bytes after a candidate cannot be proven valid."""

    dashboard = intent["dashboard"]
    candidate = _safe_product_path(context, dashboard["path"], label="transaction dashboard candidate")
    staging_ref = dashboard.get("staging_path")
    staging = _safe_product_path(context, staging_ref, label="transaction dashboard staging") if isinstance(staging_ref, str) and staging_ref else None
    if staging is not None and staging != candidate and (staging.exists() or staging.is_symlink()):
        if staging.is_symlink():
            raise DashboardDeltaError("transaction dashboard staging is symlinked")
        _remove_transaction_path(staging)
    backup = _safe_product_path(context, dashboard["backup_path"], label="transaction dashboard backup")
    if candidate.exists() or candidate.is_symlink():
        _remove_transaction_path(candidate)
    old_hash = dashboard.get("old_tree_sha256")
    if old_hash is not None:
        if not _transaction_old_backup_valid(context, intent):
            raise DashboardDeltaError("cannot restore unproven generation dashboard backup")
        os.replace(backup, candidate)
        _fsync_directory(candidate.parent)
    elif backup.exists() or backup.is_symlink():
        raise DashboardDeltaError("transaction has an unexpected dashboard backup for an empty prior output")
    for section_name in ("product_manifest", "lifecycle"):
        section = intent[section_name]
        path = (
            _safe_product_path(context, section["path"], label=f"transaction {section_name}")
            if section.get("root") == "product"
            else _safe_run_path(context, section["path"], label=f"transaction {section_name}")
        )
        old_file = section.get("backup_path")
        old_hash = section.get("old_sha256")
        if old_file:
            backup_file = _safe_product_path(context, old_file, label=f"transaction {section_name} backup")
            if not backup_file.is_file() or backup_file.is_symlink() or _sha256_bytes(backup_file.read_bytes()) != old_hash:
                raise DashboardDeltaError(f"cannot restore unproven transaction {section_name} bytes")
            _write_atomic_bytes(path, backup_file.read_bytes())
            if _transaction_file_hash(path) != old_hash:
                raise DashboardDeltaError(f"transaction {section_name} restore hash mismatch")
        elif old_hash is None and (path.exists() or path.is_symlink()):
            _remove_transaction_path(path)
    _commit_generation_transaction(context, intent_path, intent, require_new=False)


def _commit_generation_transaction(context: RunContext, intent_path: Path, intent: Mapping[str, Any], *, require_new: bool = True) -> None:
    """Delete transaction intent/backups only after terminal consistency."""

    dashboard = intent["dashboard"]
    candidate = _safe_product_path(context, dashboard["path"], label="transaction dashboard candidate")
    staging_ref = dashboard.get("staging_path")
    staging = _safe_product_path(context, staging_ref, label="transaction dashboard staging") if isinstance(staging_ref, str) and staging_ref else None
    if require_new:
        if not _transaction_candidate_valid(context, intent):
            raise DashboardDeltaError("cannot commit unproven generation dashboard candidate")
        for section_name in ("product_manifest", "lifecycle"):
            section = intent[section_name]
            expected = section.get("new_sha256")
            if not expected or _transaction_current_file_hash(context, section) != expected:
                raise DashboardDeltaError(f"cannot commit unproven transaction {section_name} bytes")
    else:
        dashboard_old_hash = dashboard.get("old_tree_sha256")
        if dashboard_old_hash is None:
            if candidate.exists() or candidate.is_symlink():
                raise DashboardDeltaError("transaction rollback left an unexpected dashboard")
        else:
            binding = _transaction_tree_binding(candidate)
            if not isinstance(binding, Mapping) or binding.get("tree_sha256") != dashboard_old_hash:
                raise DashboardDeltaError("transaction rollback dashboard hash is not authoritative")
        for section_name in ("product_manifest", "lifecycle"):
            section = intent[section_name]
            if _transaction_current_file_hash(context, section) != section.get("old_sha256"):
                raise DashboardDeltaError(f"transaction rollback {section_name} hash is not authoritative")
    backup = _safe_product_path(context, dashboard["backup_path"], label="transaction dashboard backup")
    if backup.exists() or backup.is_symlink():
        _remove_transaction_path(backup)
    backup_dir = _safe_product_path(context, intent.get("backup_dir", ""), label="transaction backup directory")
    if backup_dir.exists() or backup_dir.is_symlink():
        _remove_transaction_path(backup_dir)
    if intent_path.exists() or intent_path.is_symlink():
        _remove_transaction_path(intent_path)
    if staging is not None and staging != candidate and (staging.exists() or staging.is_symlink()):
        if staging.is_symlink():
            raise DashboardDeltaError("transaction dashboard staging is symlinked")
        _remove_transaction_path(staging)
    _fsync_directory(candidate.parent)


def _recover_generation_transaction(
    context: RunContext,
    *,
    metadata: Any,
    final_root: Path,
) -> tuple[Path, dict[str, Any]] | None:
    """Reconcile a durable intent before validating or rebuilding a candidate."""

    loaded = _load_generation_transaction(context, metadata)
    if loaded is None:
        return None
    intent_path, intent = loaded
    dashboard = intent.get("dashboard")
    if not isinstance(dashboard, Mapping) or _safe_product_path(context, dashboard.get("path", ""), label="transaction dashboard candidate") != final_root:
        raise DashboardDeltaError("generation transaction dashboard path does not match active output")
    staging_ref = dashboard.get("staging_path")
    staging_root = None
    if isinstance(staging_ref, str) and staging_ref:
        staging_root = _safe_product_path(context, staging_ref, label="transaction dashboard staging")
        if staging_root.parent != final_root.parent or staging_root.name != ".dashboard.staging":
            raise DashboardDeltaError("generation transaction dashboard staging path is outside managed namespace")
    if intent.get("phase") == "preparing":
        # The target must not have been renamed while the owned copy was in
        # flight.  Remove only the intent-bound staging path and backups, then
        # retry rendering from scratch.
        old_hash = dashboard.get("old_tree_sha256")
        target_old = _transaction_tree_binding(final_root)
        if old_hash is None:
            if target_old is not None:
                raise DashboardDeltaError("preparing generation dashboard target changed")
        elif not isinstance(target_old, Mapping) or target_old.get("tree_sha256") != old_hash:
            raise DashboardDeltaError("preparing generation dashboard target changed")
        if staging_root is not None and staging_root.is_symlink():
            raise DashboardDeltaError("preparing generation dashboard staging is symlinked")
        if staging_root is not None and staging_root.exists():
            _remove_transaction_path(staging_root)
        _restore_generation_transaction(context, intent_path, intent)
        return None
    if intent.get("phase") == "prepared" and staging_root is not None and staging_root.exists() and not final_root.exists():
        # The copy completed but publication did not begin.  Releasing the
        # owned candidate and rebuilding is safer than publishing outside the
        # lifecycle lock that guards the normal final CAS boundary.
        _restore_generation_transaction(context, intent_path, intent)
        return None
    candidate_valid = _transaction_candidate_valid(context, intent)
    if candidate_valid:
        old_hash = dashboard.get("old_tree_sha256")
        if old_hash is not None and not _transaction_old_backup_valid(context, intent):
            # A candidate without its retained old backup cannot be rolled back
            # safely until its manifest/state are proven complete.
            manifest = intent.get("product_manifest")
            lifecycle = intent.get("lifecycle")
            complete = (
                isinstance(manifest, Mapping)
                and isinstance(lifecycle, Mapping)
                and manifest.get("new_sha256")
                and lifecycle.get("new_sha256")
                and _transaction_current_file_hash(context, manifest) == manifest.get("new_sha256")
                and _transaction_current_file_hash(context, lifecycle) == lifecycle.get("new_sha256")
            )
            if not complete:
                raise DashboardDeltaError("generation transaction lost retained dashboard backup")
        if intent.get("phase") == "prepared":
            intent = _transaction_phase(intent_path, intent, "dashboard_published")
        return intent_path, intent
    # If the candidate was only staged, or a crash left a partial replacement,
    # restore the retained old bytes and clear the intent before rebuilding.
    _restore_generation_transaction(context, intent_path, intent)
    return None


def _finish_generation_transaction(
    context: RunContext,
    *,
    intent_path: Path,
    intent: Mapping[str, Any],
    receipt: Mapping[str, Any],
    receipt_path: Path,
    product_manifest_path: Path,
    lifecycle: RunLifecycle,
    metadata: Any,
    plan_path: Path,
    plan_hash: str,
    parent_manifest_ref: str,
    parent_manifest_hash: str,
    failpoint: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Finish state/manifest publication while a generation lock is held."""

    latest = RunLifecycle._load_unlocked(context)  # noqa: SLF001 - publication CAS boundary
    latest_meta = latest.generation_metadata
    if latest_meta is None or latest_meta.generation_id != metadata.generation_id or latest_meta.manifest_hash != metadata.manifest_hash:
        raise _GenerationChanged("active generation changed during dashboard transaction recovery")
    latest_product_manifest_path = _active_product_manifest_path(context, latest)
    if latest_product_manifest_path != product_manifest_path:
        raise _GenerationChanged("active product manifest reference changed during dashboard transaction recovery")
    _assert_live_plan_binding(context, latest, plan_path, plan_hash)
    dashboard = intent.get("dashboard")
    if not isinstance(dashboard, Mapping) or not _transaction_candidate_valid(context, intent):
        raise DashboardDeltaError("dashboard transaction candidate is not proven before manifest recovery")
    if _sha256_bytes(receipt_path.read_bytes()) != dashboard.get("new_receipt_sha256"):
        raise DashboardDeltaError("dashboard transaction receipt hash is not authoritative")
    recovered_receipt = dict(receipt)
    recovered_receipt["receipt_sha256"] = _sha256_bytes(receipt_path.read_bytes())
    _require_terminal_freeze(recovered_receipt)
    _failpoint(failpoint, "before_product_manifest")
    status = _terminal_product_status(context, latest)
    from auto_foundry_core.durable import ItemWorkspace

    items = tuple(ItemWorkspace.load(context, item_id, mode=latest.snapshot.mode) for item_id in latest.item_ids)
    lifecycle_section = intent.get("lifecycle")
    if not isinstance(lifecycle_section, Mapping):
        raise DashboardDeltaError("dashboard transaction lifecycle binding is invalid")
    current_state_hash = _transaction_file_hash(latest.state_path)
    old_state_hash = lifecycle_section.get("old_sha256")
    new_state_hash = lifecycle_section.get("new_sha256")
    if new_state_hash is None or current_state_hash != new_state_hash:
        if old_state_hash is not None and current_state_hash not in {old_state_hash, None} and current_state_hash != new_state_hash:
            # A state written by this transaction has a fresh generation/hash;
            # any other bytes indicate a concurrent or external mutation.
            raise DashboardDeltaError("active lifecycle state changed outside dashboard transaction")
        latest._reconcile_unlocked(items, product_terminal_status={"status": status, "terminal": True})  # noqa: SLF001
        current_state_hash = _transaction_file_hash(latest.state_path)
        intent = dict(intent)
        lifecycle_section = dict(lifecycle_section)
        lifecycle_section["new_sha256"] = current_state_hash
        intent["lifecycle"] = lifecycle_section
        intent = _transaction_phase(intent_path, intent, "lifecycle_reconciled")
    else:
        intent = dict(intent)
    _failpoint(failpoint, "after_lifecycle_reconciliation")

    manifest_section = intent.get("product_manifest")
    if not isinstance(manifest_section, Mapping):
        raise DashboardDeltaError("dashboard transaction product manifest binding is invalid")
    current_manifest_hash = _transaction_file_hash(latest_product_manifest_path)
    expected_manifest_hash = manifest_section.get("new_sha256")
    if expected_manifest_hash is not None and current_manifest_hash == expected_manifest_hash:
        product_value = _read_json(context, _relative_run_ref(context, latest_product_manifest_path), label="active generation product manifest")
    else:
        if current_manifest_hash not in {manifest_section.get("old_sha256"), None, expected_manifest_hash}:
            raise DashboardDeltaError("active generation product manifest changed outside dashboard transaction")
        product_value = _new_product_manifest(
            context,
            latest,
            latest_meta,
            recovered_receipt,
            _relative_run_ref(context, receipt_path),
            parent_manifest_ref,
            parent_manifest_hash,
            status,
        )
        try:
            from auto_foundry_core.product_contracts import validate_product_manifest

            validate_product_manifest(product_value, require_all=True)
        except Exception as exc:
            raise DashboardDeltaError("delta product manifest freeze markers are not fully frozen") from exc
        expected_manifest_hash = _json_hash(product_value)
        manifest_section = dict(manifest_section)
        manifest_section["new_sha256"] = expected_manifest_hash
        intent = dict(intent)
        intent["product_manifest"] = manifest_section
        intent = _transaction_phase(intent_path, intent, "lifecycle_reconciled")
        _write_atomic_json(latest_product_manifest_path, product_value)
        _failpoint(failpoint, "after_manifest_write")
    intent = dict(intent)
    intent["product_manifest"] = {**dict(intent.get("product_manifest", {})), "new_sha256": expected_manifest_hash}
    intent = _transaction_phase(intent_path, intent, "manifest_published")
    _commit_generation_transaction(context, intent_path, intent)
    return dict(product_value), intent


# ---------------------------------------------------------------------------
# Whole-generation replacement transaction
# ---------------------------------------------------------------------------
#
# Same-generation rebuilds replace a product *generation* (dashboard plus its
# generation product manifest), not an individual dashboard child.  The
# transaction intent and unique sibling paths live under ``generations/`` so
# renaming the generation target cannot hide the recovery record.  The older
# dashboard-only helpers above remain available solely for first publication
# recovery of pre-v2 runs; v2 replacement never calls them.

_PRODUCT_TRANSACTION_SCHEMA = "dashboard.generation_product_transaction.v2"


def _product_transaction_path(context: RunContext, generation_id: str) -> Path:
    return _safe_product_path(
        context,
        f"generations/.{generation_id}.dashboard_transaction.json",
        label="generation product transaction intent",
    )


def _generation_product_binding(path: Path) -> dict[str, Any] | None:
    """Hash every regular generation-product file, rejecting symlinks.

    The lock marker and legacy in-target transaction remnants are controlled
    metadata rather than product bytes.  Everything else—including immutable
    generation manifests and unowned local files—is included in the binding
    and copied into a replacement candidate byte-for-byte.
    """

    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_dir():
        raise DashboardDeltaError(f"generation product root is missing, non-directory, or symlinked: {path}")
    excluded = {
        ".dashboard_delta.lock",
        _TRANSACTION_FILE,
        ".dashboard.previous",
        ".dashboard.staging",
        _TRANSACTION_BACKUP_DIR,
    }
    files: dict[str, str] = {}
    for entry in sorted(path.rglob("*"), key=lambda value: value.relative_to(path).as_posix()):
        relative = entry.relative_to(path).as_posix()
        first_component = relative.split("/", 1)[0]
        if first_component in excluded or first_component.startswith(f"{_TRANSACTION_FILE}."):
            continue
        if entry.is_symlink():
            raise DashboardDeltaError(f"generation product contains a symlink: {relative}")
        if entry.is_file():
            try:
                files[relative] = _sha256_bytes(entry.read_bytes())
            except OSError as exc:
                raise DashboardDeltaError(f"generation product file cannot be read: {relative}") from exc
        elif not entry.is_dir():
            raise DashboardDeltaError(f"generation product contains unsupported entry: {relative}")
    return {"files": files, "tree_sha256": _json_hash(files), "file_count": len(files)}


def _copy_generation_product(source: Path, destination: Path) -> None:
    """Stage a complete generation root while excluding controlled markers."""

    if destination.exists() or destination.is_symlink():
        raise DashboardDeltaError(f"generation product candidate already exists: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    if not source.exists() and not source.is_symlink():
        _fsync_directory(destination)
        return
    if source.is_symlink() or not source.is_dir():
        raise DashboardDeltaError(f"generation product source is not a regular directory: {source}")
    # Validate nested entries before ``copytree`` so a symlink cannot be
    # followed into an otherwise-contained target.
    _generation_product_binding(source)
    excluded = {".dashboard_delta.lock", _TRANSACTION_FILE, ".dashboard.previous", ".dashboard.staging", _TRANSACTION_BACKUP_DIR}
    for entry in sorted(source.iterdir(), key=lambda value: value.name):
        if entry.name in excluded or entry.name.startswith(f"{_TRANSACTION_FILE}."):
            continue
        if entry.is_symlink():
            raise DashboardDeltaError(f"generation product source contains a symlink: {entry.name}")
        target = destination / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, symlinks=False)
        elif entry.is_file():
            target.write_bytes(entry.read_bytes())
        else:
            raise DashboardDeltaError(f"generation product source contains unsupported entry: {entry.name}")
    _fsync_tree(destination)


def _product_transaction_write(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise DashboardDeltaError("generation product transaction intent is symlinked")
    _write_atomic_json(path, value)


def _product_transaction_phase(path: Path, intent: Mapping[str, Any], phase: str) -> dict[str, Any]:
    updated = dict(intent)
    updated["phase"] = phase
    _product_transaction_write(path, updated)
    return updated


def _product_transaction_remove(path: Path) -> None:
    if path.is_symlink():
        raise DashboardDeltaError(f"transaction cleanup refuses symlink: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _product_transaction_path_value(context: RunContext, intent: Mapping[str, Any], key: str) -> Path:
    value = intent.get(key)
    if not isinstance(value, str) or not value:
        raise DashboardDeltaError(f"generation product transaction {key} binding is invalid")
    return _safe_product_path(context, value, label=f"generation product transaction {key}")


def _prepare_generation_product_transaction(
    context: RunContext,
    *,
    metadata: Any,
    target_root: Path,
    candidate_root: Path,
    receipt_path: Path,
    product_manifest_path: Path,
    old_receipt_hash: str | None,
    new_receipt_hash: str,
    new_binding: Mapping[str, Any] | None = None,
    candidate_binding: Mapping[str, Any] | None = None,
    preparing: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Write v2 intent before either target rename or candidate copy.

    A preparing intent may name a not-yet-created candidate path.  In that
    phase ``candidate_binding`` is the complete tree hash observed in the
    system-temp render; startup recovery can therefore prove the owned path
    before the copy begins and clean a partial process-death orphan safely.
    """

    intent_path = _product_transaction_path(context, metadata.generation_id)
    if intent_path.exists() or intent_path.is_symlink():
        raise DashboardDeltaError("generation product transaction is already in progress")
    old_binding = _generation_product_binding(target_root)
    observed_candidate_binding = (
        dict(candidate_binding)
        if candidate_binding is not None
        else _generation_product_binding(candidate_root)
    )
    if not isinstance(observed_candidate_binding, Mapping):
        raise DashboardDeltaError("generation product candidate binding is missing")
    expected_binding = dict(new_binding) if new_binding is not None else dict(observed_candidate_binding)
    try:
        manifest_relative = product_manifest_path.relative_to(target_root)
    except ValueError as exc:
        raise DashboardDeltaError("generation product manifest is outside target root") from exc
    token = secrets.token_hex(10)
    backup_root = target_root.parent / f".{target_root.name}.product.previous-{token}"
    if backup_root.exists() or backup_root.is_symlink():
        raise DashboardDeltaError("generation product transaction backup already exists")
    intent = {
        "schema_version": _PRODUCT_TRANSACTION_SCHEMA,
        "generation_id": metadata.generation_id,
        "phase": "preparing" if preparing else "prepared",
        "target_path": _relative_product_ref(context, target_root),
        "candidate_path": _relative_product_ref(context, candidate_root),
        "backup_path": _relative_product_ref(context, backup_root),
        "old_binding": old_binding,
        "new_binding": expected_binding,
        "dashboard": {
            "receipt_path": _relative_run_ref(context, receipt_path),
            "old_receipt_sha256": old_receipt_hash,
            "new_receipt_sha256": new_receipt_hash,
        },
        "product_manifest": {
            "path": _relative_product_ref(context, product_manifest_path),
            "old_sha256": _transaction_file_hash(product_manifest_path),
            "new_sha256": expected_binding.get("files", {}).get(manifest_relative.as_posix()) if isinstance(expected_binding.get("files"), Mapping) else None,
        },
    }
    if preparing:
        intent["candidate_pre_manifest_binding"] = observed_candidate_binding
    if not _is_sha256(intent["product_manifest"]["new_sha256"]):
        raise DashboardDeltaError("generation product candidate manifest hash is missing")
    try:
        _product_transaction_write(intent_path, intent)
    except Exception:
        if backup_root.exists() or backup_root.is_symlink():
            _product_transaction_remove(backup_root)
        raise
    _fsync_directory(intent_path.parent)
    return intent_path, intent


def _load_generation_product_transaction_for_id(
    context: RunContext,
    generation_id: str,
) -> tuple[Path, dict[str, Any]] | None:
    """Load a v2 intent by its bound generation, even after pointer advance.

    Recovery normally runs for the active generation.  A process can, however,
    die after publishing G-0002 and before cleanup, then a requirement append
    can advance the active pointer to G-0003.  The durable intent still names
    G-0002 and must be classified without pretending the newer pointer is the
    transaction owner.
    """

    path = _product_transaction_path(context, generation_id)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise DashboardDeltaError("generation product transaction intent is missing or symlinked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDeltaError("generation product transaction intent is invalid") from exc
    if not isinstance(value, Mapping) or value.get("schema_version") != _PRODUCT_TRANSACTION_SCHEMA or value.get("generation_id") != generation_id:
        raise DashboardDeltaError("generation product transaction intent binding is invalid")
    if value.get("phase") not in {"preparing", "prepared", "old_target_renamed", "target_published", "lifecycle_reconciled"}:
        raise DashboardDeltaError("generation product transaction phase is invalid")
    for key in ("target_path", "candidate_path", "backup_path", "old_binding", "new_binding", "dashboard", "product_manifest"):
        if key not in value:
            raise DashboardDeltaError(f"generation product transaction {key} binding is missing")
    if value.get("phase") == "preparing":
        if not isinstance(value.get("candidate_pre_manifest_binding"), Mapping):
            raise DashboardDeltaError("generation product preparing candidate binding is missing")
    return path, dict(value)


def _load_generation_product_transaction(context: RunContext, metadata: Any) -> tuple[Path, dict[str, Any]] | None:
    loaded = _load_generation_product_transaction_for_id(context, metadata.generation_id)
    if loaded is None:
        return None
    return loaded


def _product_transaction_binding_matches(path: Path, expected: Any) -> bool:
    if expected is None:
        return not path.exists() and not path.is_symlink()
    try:
        binding = _generation_product_binding(path)
    except DashboardDeltaError:
        return False
    return isinstance(binding, Mapping) and binding == expected


def _product_transaction_pre_manifest_binding(intent: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Expected staged tree after rendering but before manifest replacement."""

    new_binding = intent.get("new_binding")
    manifest = intent.get("product_manifest")
    target_ref = intent.get("target_path")
    manifest_ref = manifest.get("path") if isinstance(manifest, Mapping) else None
    old_manifest_hash = manifest.get("old_sha256") if isinstance(manifest, Mapping) else None
    if not isinstance(new_binding, Mapping) or not isinstance(new_binding.get("files"), Mapping):
        return None
    if not isinstance(target_ref, str) or not isinstance(manifest_ref, str) or not isinstance(old_manifest_hash, str):
        return None
    try:
        relative = Path(manifest_ref).relative_to(Path(target_ref)).as_posix()
    except ValueError:
        return None
    files = dict(new_binding["files"])
    files[relative] = old_manifest_hash
    return {"files": files, "tree_sha256": _json_hash(files), "file_count": len(files)}


def _generation_lineage_binding(context: RunContext, generation_id: str) -> dict[str, Any]:
    """Read immutable generation metadata for stale-transaction lineage.

    ``RequirementRunExtension.append`` records the immediate parent state and
    plan hashes in the next generation manifest.  Those hashes are the
    durable bridge available even when the product transaction's generation
    is no longer the active pointer and its product manifest cannot be
    reconciled against the newer lifecycle.
    """

    path = _safe_run_path(
        context,
        f"extensions/{generation_id}/generation_manifest.json",
        label="transaction generation manifest",
    )
    if path.is_symlink() or not path.is_file():
        raise DashboardDeltaError("transaction generation manifest is missing or symlinked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDeltaError("transaction generation manifest is invalid") from exc
    if not isinstance(value, Mapping) or raw != _canonical_bytes(value):
        raise DashboardDeltaError("transaction generation manifest is not canonical")
    expected_fields = {
        "schema_version", "kind", "run_id", "run_root", "generation_id", "generation_ordinal",
        "parent_generation_id", "parent_state_hash", "parent_plan_hash", "added_item_ids",
        "cumulative_item_ids", "state_ref", "plan_ref", "state_manifest_hash", "plan_hash",
        "request_hash", "product_manifest_ref", "created_at", "manifest_hash",
    }
    if set(value) != expected_fields or value.get("generation_id") != generation_id or value.get("run_id") != context.run_id:
        raise DashboardDeltaError("transaction generation manifest identity is invalid")
    if value.get("manifest_hash") != _json_hash({key: item for key, item in value.items() if key != "manifest_hash"}):
        raise DashboardDeltaError("transaction generation manifest hash is invalid")
    for key in ("parent_state_hash", "parent_plan_hash", "state_manifest_hash", "plan_hash", "request_hash", "manifest_hash"):
        if not _is_sha256(value.get(key)):
            raise DashboardDeltaError(f"transaction generation manifest {key} is invalid")
    state_path = _safe_run_path(
        context,
        f"extensions/{generation_id}/run_state.json",
        label="transaction generation state",
    )
    if state_path.is_symlink() or not state_path.is_file():
        raise DashboardDeltaError("transaction generation state is missing or symlinked")
    try:
        state_raw = state_path.read_bytes()
        state_value = json.loads(state_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDeltaError("transaction generation state is invalid") from exc
    if not isinstance(state_value, Mapping) or state_raw != _canonical_bytes(state_value):
        raise DashboardDeltaError("transaction generation state is not canonical")
    current_state_hash = _state_manifest_hash(state_value)
    if state_value.get("manifest_hash") != current_state_hash:
        raise DashboardDeltaError("transaction generation state hash is invalid")
    return {
        "generation_id": generation_id,
        "generation_manifest_hash": value["manifest_hash"],
        # The next generation binds the parent's *current* lifecycle state,
        # which may have advanced from the generation admission snapshot by
        # the time a product transaction is being recovered.
        "state_manifest_hash": current_state_hash,
        "admission_state_manifest_hash": value["state_manifest_hash"],
        "plan_hash": value["plan_hash"],
        "parent_generation_id": value["parent_generation_id"],
        "parent_state_hash": value["parent_state_hash"],
        "parent_plan_hash": value["parent_plan_hash"],
    }


def _safe_remove_product_transaction_entry(path: Path, expected_bindings: Sequence[Mapping[str, Any] | None]) -> None:
    """Remove an intent-bound candidate/backup only after exact proof."""

    if not path.exists() and not path.is_symlink():
        return
    if any(_product_transaction_binding_matches(path, expected) for expected in expected_bindings):
        _product_transaction_remove(path)
        return
    raise DashboardDeltaError(f"generation product transaction entry cannot be proven for cleanup: {path}")


def _next_generation_binds_published_product(
    context: RunContext,
    *,
    latest: RunLifecycle,
    transaction_generation_id: str,
    target: Path,
    intent: Mapping[str, Any],
) -> bool:
    """Prove that a newer active generation has adopted the new target.

    A stale transaction must not roll back a published G-0002 product after
    G-0003 admission has made it the immediate parent.  The next generation's
    parent state/plan hashes are compared to the immutable G-0002 manifest;
    the published product itself is also checked for its bound generation and
    receipt hash before cleanup is allowed.
    """

    latest_meta = latest.generation_metadata
    if latest_meta is None or latest_meta.parent_generation_id != transaction_generation_id:
        return False
    try:
        transaction_lineage = _generation_lineage_binding(context, transaction_generation_id)
    except DashboardDeltaError:
        return False
    if latest_meta.parent_state_hash != transaction_lineage["state_manifest_hash"] or latest_meta.parent_plan_hash != transaction_lineage["plan_hash"]:
        return False
    if not _product_transaction_binding_matches(target, intent.get("new_binding")):
        return False
    manifest_binding = intent.get("product_manifest")
    manifest_ref = manifest_binding.get("path") if isinstance(manifest_binding, Mapping) else None
    if not isinstance(manifest_ref, str) or not manifest_ref:
        return False
    manifest_path = _safe_product_path(context, manifest_ref, label="generation product manifest")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return False
    try:
        raw = manifest_path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, Mapping) or raw != _canonical_bytes(value):
        return False
    lifecycle = value.get("lifecycle")
    lineage = value.get("lineage")
    dashboard = value.get("dashboard")
    expected_receipt_hash = intent.get("dashboard", {}).get("new_receipt_sha256") if isinstance(intent.get("dashboard"), Mapping) else None
    child_manifest_path = _active_product_manifest_path(context, latest)
    if not child_manifest_path.is_file() or child_manifest_path.is_symlink():
        # The active child may be admitted before its first product assembly.
        # Startup recovery defers cleanup in that state; the child publication
        # boundary retries this check after its own parent binding is durable.
        return False
    try:
        child_raw = child_manifest_path.read_bytes()
        child_value = json.loads(child_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    child_lineage = child_value.get("lineage") if isinstance(child_value, Mapping) else None
    expected_parent_ref = _relative_run_ref(context, manifest_path)
    expected_parent_hash = _sha256_bytes(manifest_path.read_bytes())
    return bool(
        isinstance(lifecycle, Mapping)
        and lifecycle.get("generation_id") == transaction_generation_id
        and isinstance(lineage, Mapping)
        and lineage.get("generation_manifest_hash") == transaction_lineage["generation_manifest_hash"]
        and isinstance(dashboard, Mapping)
        and dashboard.get("receipt_sha256") == expected_receipt_hash
        and isinstance(child_value, Mapping)
        and child_raw == _canonical_bytes(child_value)
        and isinstance(child_lineage, Mapping)
        and child_lineage.get("parent_generation_id") == transaction_generation_id
        and child_lineage.get("parent_product_manifest_ref") == expected_parent_ref
        and child_lineage.get("parent_product_manifest_sha256") == expected_parent_hash
    )


def _abort_inactive_generation_product_transaction(
    context: RunContext,
    *,
    latest: RunLifecycle,
    transaction_generation_id: str,
    target_root: Path,
    defer_unbound_new: bool = False,
) -> None:
    """Resolve an intent whose generation lost the active pointer.

    Old-only trees are aborted in place.  A missing target restores the
    proven old backup.  A new target is retained only when the immediate next
    generation's parent lineage proves it adopted that product; otherwise the
    function fails closed without deleting or renaming bytes.
    """

    loaded = _load_generation_product_transaction_for_id(context, transaction_generation_id)
    if loaded is None:
        return
    intent_path, intent = loaded
    target = _product_transaction_path_value(context, intent, "target_path")
    candidate = _product_transaction_path_value(context, intent, "candidate_path")
    backup = _product_transaction_path_value(context, intent, "backup_path")
    if target != target_root:
        raise DashboardDeltaError("inactive generation transaction target does not match expected output")
    if candidate.parent != target.parent or not candidate.name.startswith(f".{target.name}.product.staging-"):
        raise DashboardDeltaError("inactive generation transaction candidate path is outside managed sibling namespace")
    if backup.parent != target.parent or not backup.name.startswith(f".{target.name}.product.previous-"):
        raise DashboardDeltaError("inactive generation transaction backup path is outside managed sibling namespace")
    old_ok = _product_transaction_binding_matches(target, intent.get("old_binding"))
    new_ok = _product_transaction_binding_matches(target, intent.get("new_binding"))
    backup_ok = _product_transaction_binding_matches(backup, intent.get("old_binding"))
    candidate_expected = [
        intent.get("new_binding"),
        _product_transaction_pre_manifest_binding(intent),
        intent.get("old_binding"),
    ]
    if intent.get("phase") == "preparing":
        if not old_ok:
            raise DashboardDeltaError("inactive preparing generation product transaction target changed")
        if candidate.is_symlink():
            raise DashboardDeltaError("inactive preparing generation product candidate is symlinked")
        if candidate.exists():
            _product_transaction_remove(candidate)
        if backup.exists() or backup.is_symlink():
            raise DashboardDeltaError("inactive preparing generation product backup is unexpected")
        _product_transaction_remove(intent_path)
        _fsync_directory(target.parent)
        return
    if old_ok:
        _safe_remove_product_transaction_entry(candidate, candidate_expected)
        if backup_ok:
            _product_transaction_remove(backup)
        elif backup.exists() or backup.is_symlink():
            raise DashboardDeltaError("inactive generation transaction backup cannot be proven for cleanup")
        _product_transaction_remove(intent_path)
        _fsync_directory(target.parent)
        return
    if not target.exists() and backup_ok:
        os.replace(backup, target)
        _fsync_directory(target.parent)
        if not _product_transaction_binding_matches(target, intent.get("old_binding")):
            raise DashboardDeltaError("inactive generation transaction old backup did not restore exactly")
        _safe_remove_product_transaction_entry(candidate, candidate_expected)
        _product_transaction_remove(intent_path)
        _fsync_directory(target.parent)
        return
    if new_ok and _next_generation_binds_published_product(
        context,
        latest=latest,
        transaction_generation_id=transaction_generation_id,
        target=target,
        intent=intent,
    ):
        _safe_remove_product_transaction_entry(candidate, candidate_expected)
        if backup_ok:
            _product_transaction_remove(backup)
        elif backup.exists() or backup.is_symlink():
            raise DashboardDeltaError("adopted generation backup cannot be proven for cleanup")
        _product_transaction_remove(intent_path)
        _fsync_directory(target.parent)
        return
    if new_ok and defer_unbound_new:
        child_manifest_path = _active_product_manifest_path(context, latest)
        if not child_manifest_path.exists() and not child_manifest_path.is_symlink():
            # G-0003 admission can precede its first product publication.  Do
            # not roll back the already-visible G-0002 product; the child
            # publication boundary will finalize this parent intent once its
            # persisted parent manifest hash exists.
            return
    # If a target is neither the bound old tree nor a next-generation-bound
    # new tree, do not guess which bytes are authoritative.
    raise DashboardDeltaError("inactive generation product transaction lineage is not authoritative")


def _reconcile_immediate_parent_transaction_locked(context: RunContext, latest: RunLifecycle) -> None:
    """Reconcile only the active generation's immediate parent's v2 intent.

    This runs at the beginning of a fresh invocation while the lifecycle lock
    is already held.  It deliberately does not scan older generations: an
    unrelated historical intent must never mutate during a current child run.
    """

    metadata = latest.generation_metadata
    if metadata is None or not metadata.parent_generation_id:
        return
    parent_id = metadata.parent_generation_id
    if _load_generation_product_transaction_for_id(context, parent_id) is None:
        return
    # Re-read active authorities before touching the parent transaction.  The
    # caller owns _run_lock, so admission cannot advance the pointer between
    # these checks and the intent-bound rename/cleanup below.
    _load_plan(context, latest)
    _parent_generation_bindings(context, metadata)
    parent_root = _safe_product_path(
        context,
        f"generations/{parent_id}",
        label="immediate parent generation product namespace",
    )
    _abort_inactive_generation_product_transaction(
        context,
        latest=latest,
        transaction_generation_id=parent_id,
        target_root=parent_root,
        defer_unbound_new=True,
    )


def _recover_generation_product_transaction(
    context: RunContext,
    *,
    metadata: Any,
    target_root: Path,
) -> tuple[Path, dict[str, Any]] | None:
    """Classify sibling target/candidate/backup paths and recover safely.

    Recovery never removes a path unless it is bound by the durable intent and
    its complete tree hash is known.  If a candidate cannot be proven, the old
    backup is restored (when available) and the intent is cleared.
    """

    loaded = _load_generation_product_transaction(context, metadata)
    if loaded is None:
        return None
    intent_path, intent = loaded
    target = _product_transaction_path_value(context, intent, "target_path")
    candidate = _product_transaction_path_value(context, intent, "candidate_path")
    backup = _product_transaction_path_value(context, intent, "backup_path")
    if target != target_root:
        raise DashboardDeltaError("generation product transaction target does not match active output")
    if candidate.parent != target_root.parent or not candidate.name.startswith(f".{target_root.name}.product.staging-"):
        raise DashboardDeltaError("generation product transaction candidate path is outside managed sibling namespace")
    if backup.parent != target_root.parent or not backup.name.startswith(f".{target_root.name}.product.previous-"):
        raise DashboardDeltaError("generation product transaction backup path is outside managed sibling namespace")
    old_ok = _product_transaction_binding_matches(target, intent.get("old_binding"))
    new_ok = _product_transaction_binding_matches(target, intent.get("new_binding"))
    candidate_ok = _product_transaction_binding_matches(candidate, intent.get("new_binding"))
    backup_ok = _product_transaction_binding_matches(backup, intent.get("old_binding"))

    if intent.get("phase") == "preparing":
        # No target rename is permitted in the preparing phase.  The intent
        # itself owns the explicit random sibling path, so a process-death
        # partial copy can be removed without guessing at unrelated product
        # entries.  A changed target or symlinked candidate fails closed.
        if not old_ok:
            raise DashboardDeltaError("preparing generation product transaction target changed")
        if candidate.is_symlink():
            raise DashboardDeltaError("preparing generation product candidate is symlinked")
        if candidate.exists():
            _product_transaction_remove(candidate)
        _product_transaction_remove(intent_path)
        _fsync_directory(target.parent)
        return None

    # Before the first rename the old target is authoritative.  Keep the
    # candidate and intent; the caller's normal publication path will perform
    # the two renames atomically on retry.  Recovery itself performs that
    # retry so the remainder of assembly always validates the visible target
    # rather than the stale pre-crash receipt.
    if old_ok and candidate_ok and not backup.exists() and not backup.is_symlink():
        return _publish_generation_product_transaction(
            context,
            intent_path=intent_path,
            intent=intent,
            failpoint=None,
        )
    # A crash between the two renames leaves only backup + candidate.  Publish
    # the proven candidate and continue reconciliation.
    if not target.exists() and backup_ok and candidate_ok:
        os.replace(candidate, target)
        _fsync_directory(target.parent)
        marker = target / ".dashboard_delta.lock"
        if not marker.exists():
            marker.touch(mode=0o600, exist_ok=False)
            _fsync_directory(target)
        intent = _product_transaction_phase(intent_path, intent, "target_published")
        return intent_path, intent
    # The new target is already visible; retained backup is required until the
    # lifecycle and product manifest checks commit the transaction.
    if new_ok and (backup_ok or not backup.exists()):
        if candidate.exists() or candidate.is_symlink():
            if candidate_ok:
                _product_transaction_remove(candidate)
            else:
                raise DashboardDeltaError("generation product transaction has an unexpected candidate")
        return intent_path, intent
    # If the candidate was never proven, restore the old bytes whenever the
    # backup proves them.  Never delete the only old copy.
    if backup_ok and not new_ok:
        if target.exists() or target.is_symlink():
            if old_ok:
                # Target already contains the old bytes; retain it and remove
                # only the bound backup below.
                pass
            else:
                raise DashboardDeltaError("generation product transaction has an unexpected target")
        else:
            os.replace(backup, target)
            _fsync_directory(target.parent)
        if candidate.exists() or candidate.is_symlink():
            if candidate_ok:
                _product_transaction_remove(candidate)
            else:
                raise DashboardDeltaError("generation product candidate cannot be proven for cleanup")
        _product_transaction_remove(intent_path)
        if backup.exists() or backup.is_symlink():
            _product_transaction_remove(backup)
        _fsync_directory(target.parent)
        return None
    candidate_old_ok = _product_transaction_binding_matches(candidate, intent.get("old_binding"))
    candidate_pre_manifest_ok = _product_transaction_binding_matches(
        candidate,
        _product_transaction_pre_manifest_binding(intent),
    )
    if old_ok and candidate_pre_manifest_ok and not backup.exists() and not backup.is_symlink():
        _product_transaction_remove(candidate)
        _product_transaction_remove(intent_path)
        return None
    if old_ok and candidate_old_ok and not backup.exists() and not backup.is_symlink():
        _product_transaction_remove(candidate)
        _product_transaction_remove(intent_path)
        return None
    raise DashboardDeltaError("generation product transaction state is externally inconsistent")


def _publish_generation_product_transaction(
    context: RunContext,
    *,
    intent_path: Path,
    intent: Mapping[str, Any],
    failpoint: str | None,
) -> tuple[Path, dict[str, Any]]:
    """Rename old target to backup, then candidate to target."""

    target = _product_transaction_path_value(context, intent, "target_path")
    candidate = _product_transaction_path_value(context, intent, "candidate_path")
    backup = _product_transaction_path_value(context, intent, "backup_path")
    if _product_transaction_binding_matches(target, intent.get("new_binding")):
        return intent_path, dict(intent)
    if not _product_transaction_binding_matches(candidate, intent.get("new_binding")):
        raise DashboardDeltaError("generation product candidate is not authoritative before rename")
    _failpoint(failpoint, "before_first_rename")
    if target.exists() or target.is_symlink():
        if not _product_transaction_binding_matches(target, intent.get("old_binding")):
            raise DashboardDeltaError("generation product target changed before rename")
        os.replace(target, backup)
        _fsync_directory(target.parent)
    intent = _product_transaction_phase(intent_path, intent, "old_target_renamed")
    _failpoint(failpoint, "between_renames")
    os.replace(candidate, target)
    _fsync_directory(target.parent)
    marker = target / ".dashboard_delta.lock"
    if not marker.exists():
        marker.touch(mode=0o600, exist_ok=False)
        _fsync_directory(target)
    intent = _product_transaction_phase(intent_path, intent, "target_published")
    _failpoint(failpoint, "after_new_target_publish")
    # Historical callers use after_dashboard_publish to mean a crash after
    # the new dashboard is visible; keep that alias for process-death tests.
    _failpoint(failpoint, "after_dashboard_publish")
    return intent_path, intent


def _finish_generation_product_transaction(
    context: RunContext,
    *,
    intent_path: Path,
    intent: Mapping[str, Any],
    receipt: Mapping[str, Any],
    receipt_path: Path,
    lifecycle: RunLifecycle,
    metadata: Any,
    plan_path: Path,
    plan_hash: str,
    parent_manifest_ref: str,
    parent_manifest_hash: str,
    failpoint: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconcile terminal lifecycle and commit only after full-tree proof."""

    latest = RunLifecycle._load_unlocked(context)  # noqa: SLF001
    latest_meta = latest.generation_metadata
    if latest_meta is None or latest_meta.generation_id != metadata.generation_id or latest_meta.manifest_hash != metadata.manifest_hash:
        raise _GenerationChanged("active generation changed during product transaction recovery")
    _assert_live_plan_binding(context, latest, plan_path, plan_hash)
    target = _product_transaction_path_value(context, intent, "target_path")
    if not _product_transaction_binding_matches(target, intent.get("new_binding")):
        raise DashboardDeltaError("generation product target is not the bound candidate")
    dashboard = intent.get("dashboard")
    if not isinstance(dashboard, Mapping) or _sha256_bytes(receipt_path.read_bytes()) != dashboard.get("new_receipt_sha256"):
        raise DashboardDeltaError("generation product receipt hash is not authoritative")
    recovered_receipt = dict(receipt)
    recovered_receipt["receipt_sha256"] = _sha256_bytes(receipt_path.read_bytes())
    _require_terminal_freeze(recovered_receipt)
    _failpoint(failpoint, "before_lifecycle_reconcile")
    status = _terminal_product_status(context, latest)
    from auto_foundry_core.durable import ItemWorkspace

    items = tuple(ItemWorkspace.load(context, item_id, mode=latest.snapshot.mode) for item_id in latest.item_ids)
    latest._reconcile_unlocked(items, product_terminal_status={"status": status, "terminal": True})  # noqa: SLF001
    intent = _product_transaction_phase(intent_path, intent, "lifecycle_reconciled")
    _failpoint(failpoint, "during_lifecycle_reconcile")
    _failpoint(failpoint, "after_lifecycle_reconciliation")
    manifest_path = _safe_product_path(context, intent["product_manifest"]["path"], label="generation product manifest")
    expected_manifest_hash = intent["product_manifest"].get("new_sha256")
    if _transaction_file_hash(manifest_path) != expected_manifest_hash:
        raise DashboardDeltaError("generation product manifest hash is not authoritative")
    product_value = _read_json(context, _relative_run_ref(context, manifest_path), label="active generation product manifest")
    _validate_product_manifest(context, manifest_path, product_value, recovered_receipt, latest, latest_meta, parent_manifest_ref, parent_manifest_hash)
    backup = _product_transaction_path_value(context, intent, "backup_path")
    candidate = _product_transaction_path_value(context, intent, "candidate_path")
    if candidate.exists() or candidate.is_symlink():
        if _product_transaction_binding_matches(candidate, intent.get("new_binding")):
            _product_transaction_remove(candidate)
        else:
            raise DashboardDeltaError("generation product candidate changed before commit")
    if backup.exists() or backup.is_symlink():
        if not _product_transaction_binding_matches(backup, intent.get("old_binding")):
            raise DashboardDeltaError("generation product backup changed before commit")
        _product_transaction_remove(backup)
    if not _product_transaction_binding_matches(target, intent.get("new_binding")):
        raise DashboardDeltaError("generation product target changed before commit")
    _product_transaction_remove(intent_path)
    _fsync_directory(target.parent)
    return product_value, intent


def _merge_ontology(parent_fixture: Mapping[str, Any], new_records: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge typed ontology additions without rereading old work artifacts."""

    nodes = [copy.deepcopy(value) for value in parent_fixture.get("ontology_objects", []) if isinstance(value, Mapping)]
    edges = [copy.deepcopy(value) for value in parent_fixture.get("ontology_relationships", []) if isinstance(value, Mapping)]
    groups = [copy.deepcopy(value) for value in parent_fixture.get("ontology_groups", []) if isinstance(value, Mapping)]
    node_by_id = {_text(value.get("id")): value for value in nodes if _text(value.get("id"))}
    edge_keys = {(_text(value.get("source")), _text(value.get("target")), _text(value.get("label"))) for value in edges}
    group_by_id = {_text(value.get("id")): value for value in groups if _text(value.get("id"))}
    group_nodes: dict[str, list[str]] = {key: list(value.get("node_ids", [])) for key, value in group_by_id.items()}
    for record in new_records:
        payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
        kind = _text(record.get("kind"))
        if kind == "ontology_item" and (payload.get("ontology_projection") is True or (payload.get("ontology_id") and payload.get("kind"))):
            node_id = _text(payload.get("ontology_id") or payload.get("id"))
            label = _text(payload.get("label") or node_id)
            node_kind = _text(payload.get("kind") or payload.get("object_type"))
            if not node_id or not label or not node_kind:
                raise DashboardDeltaError("new ontology node lacks stable id/label/kind")
            candidate = {"id": node_id, "label": label, "kind": node_kind}
            if node_id in node_by_id and node_by_id[node_id] != candidate:
                raise DashboardDeltaError(f"ontology node collision: {node_id}")
            if node_id not in node_by_id:
                node_by_id[node_id] = candidate
                nodes.append(candidate)
            group_id = _text(payload.get("group_id") or payload.get("group"))
            if group_id:
                group_nodes.setdefault(group_id, [])
                if node_id not in group_nodes[group_id]:
                    group_nodes[group_id].append(node_id)
                group = group_by_id.setdefault(group_id, {"id": group_id, "label": group_id})
                if payload.get("group_label"):
                    group["label"] = _text(payload.get("group_label"))
                if payload.get("group_order") is not None:
                    group["order"] = payload.get("group_order")
        elif kind == "relationship" and payload.get("ontology_projection") is True:
            source = _text(payload.get("source_id") or payload.get("source"))
            target = _text(payload.get("target_id") or payload.get("target"))
            label = _text(payload.get("label") or payload.get("relationship"))
            if not source or not target or not label:
                continue
            key = (source, target, label)
            if key not in edge_keys:
                edges.append({"source": source, "target": target, "label": label})
                edge_keys.add(key)
    if any(_text(edge.get("source")) not in node_by_id or _text(edge.get("target")) not in node_by_id for edge in edges):
        raise DashboardDeltaError("ontology relationship references an unknown endpoint")
    for group_id, node_ids in group_nodes.items():
        group = group_by_id.setdefault(group_id, {"id": group_id, "label": group_id})
        group["node_ids"] = node_ids
    nodes.sort(key=lambda value: _text(value.get("id")))
    edges.sort(key=lambda value: (_text(value.get("source")), _text(value.get("target")), _text(value.get("label"))))
    groups = sorted(group_by_id.values(), key=lambda value: (value.get("order", 10**9), _text(value.get("id"))) )
    return nodes, edges, groups


def _input_item(context: RunContext, assembler: Any, item_id: str) -> tuple[dict[str, Any], list[Mapping[str, Any]], dict[str, Any]]:
    try:
        content, accepted_manifest, accepted_meta = assembler._load_public_accepted_bundle(context, item_id)
        integration_manifest, records = assembler._load_committed_records(context, item_id, accepted_manifest, accepted_meta["bundle"])
    except Exception as exc:
        raise DashboardDeltaError(f"{item_id} accepted/committed input validation failed") from exc
    state = accepted_meta["state"]
    raw_scope = assembler._text(content.get("scope") or content.get("method"))
    compact_scope = ""
    item = {
        "item_id": item_id,
        "content": content,
        "requirement_scope": assembler._text(state.get("original_text") or content.get("scope") or item_id),
        "requirement_title": assembler._manager_requirement_title(
            item_id,
            content,
            assembler._text(state.get("original_text") or content.get("scope") or item_id),
            records,
        ),
        "requirement_subtitle": compact_scope,
        "takeaway": assembler._manager_takeaway(
            content,
            assembler._text(state.get("original_text") or content.get("scope") or item_id),
        ),
        "limitations": assembler._manager_limitations(
            content,
            records,
            assembler._text(state.get("original_text") or content.get("scope") or item_id),
        ),
        "accepted_manifest_hash": accepted_meta["bundle"].manifest_hash,
        "accepted_content_hash": accepted_meta["bundle"].content_hash,
        "integration_manifest_hash": integration_manifest["manifest_hash"],
        "records": records,
    }
    return item, records, integration_manifest


def _rebuild_cumulative_dashboard_projection(
    *,
    assembler: Any,
    plan: Mapping[str, Any],
    cumulative_ids: Sequence[str],
    old_ids: Sequence[str],
    added_ids: Sequence[str],
    loaded: Mapping[str, Mapping[str, Any]],
    parent_fixture: Mapping[str, Any],
    parent_map: Mapping[str, Any],
    routes: Mapping[str, Mapping[str, Any]],
    manager_widget_ids: Sequence[str],
    manager_entries: Mapping[str, Mapping[str, Any]],
    presentation_plan_ref: str | None,
    presentation_plan_sha256: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Rebuild every cumulative item from current accepted/integrated inputs.

    A delta is a new generation product, not a patch over the previous
    presentation.  The immediate parent fixture remains the only legacy chart
    hint source: this keeps reviewed geometry stable without accidentally
    borrowing the active generation's partially rebuilt child presentation.
    Parent widget/chart IDs are required to survive the rebuild; any changed
    identity fails closed instead of silently dropping an old record.
    """

    parent_domains = parent_fixture.get("domains")
    parent_widgets = parent_fixture.get("widgets")
    if not isinstance(parent_domains, list) or not isinstance(parent_widgets, list):
        raise DashboardDeltaError("parent fixture domains/widgets are invalid")
    parent_domain_by_id = {
        _text(value.get("id")): value
        for value in parent_domains
        if isinstance(value, Mapping) and _text(value.get("id"))
    }
    parent_item_domain: dict[str, str] = {}
    for domain_id, domain in parent_domain_by_id.items():
        flows = domain.get("decision_flow")
        if not isinstance(flows, list):
            continue
        for flow in flows:
            if not isinstance(flow, Mapping):
                continue
            flow_id = _text(flow.get("id"))
            for item_id in old_ids:
                if flow_id == f"{domain_id}-{assembler._slug(item_id)}":
                    parent_item_domain[item_id] = domain_id
    # Parent fixture bytes are already bound by the parent receipt.  Reusing
    # them as hints preserves chart values/types while keeping current record
    # provenance and manager metadata authoritative.
    legacy_hints = {
        _text(widget.get("id")): widget
        for widget in parent_widgets
        if isinstance(widget, Mapping) and _text(widget.get("id"))
    }
    parent_widget_ids = set(legacy_hints)
    parent_chart_entries = parent_map.get("charts")
    if not isinstance(parent_chart_entries, list):
        raise DashboardDeltaError("parent chart map charts are invalid")
    chart_by_id: dict[str, Mapping[str, Any]] = {}
    chart_order: list[str] = []
    for entry in parent_chart_entries:
        if not isinstance(entry, Mapping) or not _text(entry.get("id")):
            raise DashboardDeltaError("parent chart map entry is invalid")
        entry_id = _text(entry.get("id"))
        if entry_id in chart_by_id:
            raise DashboardDeltaError(f"parent chart map contains duplicate widget ID: {entry_id}")
        chart_by_id[entry_id] = entry
        chart_order.append(entry_id)
    if not parent_widget_ids <= set(chart_by_id):
        missing = sorted(parent_widget_ids - set(chart_by_id))[:5]
        raise DashboardDeltaError(f"parent chart map is missing stable widget IDs: {missing}")

    route_by_item = {
        item_id: routes[item_id]
        for item_id in added_ids
        if item_id in routes
    }
    groups = assembler._group_definitions(plan, cumulative_ids)
    assigned: set[str] = set()
    widgets: list[dict[str, Any]] = []
    domains: list[dict[str, Any]] = []
    route_manifest: list[dict[str, Any]] = []
    generated_chart_entries: dict[str, dict[str, Any]] = {}
    for group in groups:
        group_items = [item_id for item_id in group.get("requirement_ids", []) if item_id in cumulative_ids]
        if not group_items:
            continue
        if any(item_id in assigned for item_id in group_items):
            raise DashboardDeltaError("active plan assigns a cumulative item more than once")
        assigned.update(group_items)

        # Existing groups are identified by their parent flow membership, not
        # by a freshly generated plan slug.  A new route may use an explicit
        # group ID that differs from the synthetic plan-group fallback.
        existing_group_ids = {parent_item_domain[item_id] for item_id in group_items if item_id in parent_item_domain}
        routed_group_ids = {
            _text(route_by_item[item_id].get("group_id") or route_by_item[item_id].get("section_id"))
            for item_id in group_items
            if item_id in route_by_item
        }
        if len(existing_group_ids) > 1:
            raise DashboardDeltaError("cumulative plan group spans multiple parent dashboard sections")
        if existing_group_ids:
            group_id = next(iter(existing_group_ids))
            if routed_group_ids and routed_group_ids != {group_id}:
                raise DashboardDeltaError("existing route section drifted during cumulative rebuild")
            parent_domain = parent_domain_by_id.get(group_id)
            if parent_domain is None:
                raise DashboardDeltaError(f"parent dashboard section is missing: {group_id}")
            domain_title = _text(parent_domain.get("title") or group.get("title") or group_id)
            domain_summary = _text(parent_domain.get("summary") or group.get("summary"))
            domain_order = parent_domain.get("order", group.get("order"))
        else:
            if not routed_group_ids or len(routed_group_ids) != 1:
                raise DashboardDeltaError("new cumulative route lacks one explicit section ID")
            group_id = next(iter(routed_group_ids))
            if group_id in parent_domain_by_id:
                raise DashboardDeltaError(f"new route section already exists: {group_id}")
            route_info = next(route_by_item[item_id] for item_id in group_items if item_id in route_by_item)
            if route_info.get("kind", "existing") != "new":
                raise DashboardDeltaError(f"new cumulative route is not marked new: {group_id}")
            domain_title = _text(route_info.get("title")).strip()
            domain_summary = _text(route_info.get("summary"))
            domain_order = route_info.get("order")
            if not domain_title or isinstance(domain_order, bool) or not isinstance(domain_order, int) or domain_order < 1:
                raise DashboardDeltaError(f"new cumulative route metadata is invalid: {group_id}")

        flow_defs: list[dict[str, Any]] = []
        for flow_order, item_id in enumerate(group_items, 1):
            item = loaded.get(item_id)
            if not isinstance(item, Mapping):
                raise DashboardDeltaError(f"cumulative item input is missing: {item_id}")
            records = item.get("records")
            if not isinstance(records, list):
                raise DashboardDeltaError(f"cumulative item records are invalid: {item_id}")
            widget_content = dict(item["content"])
            widget_content["__manager_requirement_scope"] = item["requirement_scope"]
            item_widgets = assembler._build_widgets(
                item_id,
                widget_content,
                records,
                legacy_hints=legacy_hints,
                manager_widget_ids=manager_widget_ids,
                manager_entries=manager_entries,
            )
            assembler._collision_safe_widget_ids(item_id, group_id, item_widgets, records)
            admitted_widget_ids = {
                _text(widget.get("id"))
                for widget in item_widgets
                if isinstance(widget.get("manager_admission"), Mapping)
                and widget["manager_admission"].get("status") == "admitted"
            }
            item_manager_admission = {
                "status": "admitted" if admitted_widget_ids else "audit_only",
                "presentation_audience": "business_manager" if admitted_widget_ids else "technical_audit",
                "policy": "explicit_business_presentation_plan",
                "admitted_widget_ids": sorted(admitted_widget_ids),
                "presentation_plan_ref": presentation_plan_ref,
                "presentation_plan_sha256": presentation_plan_sha256,
            }
            for widget_index, widget in enumerate(item_widgets, 1):
                widget["requirement_id"] = item_id
                widget["requirement_title"] = item["requirement_title"]
                if item.get("requirement_subtitle"):
                    widget["requirement_subtitle"] = item["requirement_subtitle"]
                if item.get("takeaway"):
                    widget["takeaway"] = item["takeaway"]
                if item.get("requirement_scope"):
                    widget["requirement_scope"] = item["requirement_scope"]
                if item.get("limitations"):
                    widget["requirement_limitations"] = list(item["limitations"])
                widget["domain_id"] = group_id
                widget["manager_admission"] = dict(widget.get("manager_admission") or {})
                widget["manager_admission"]["requirement_status"] = item_manager_admission["status"]
                widget["requirement_order"] = flow_order
                widget["manager_anchor"] = f"{assembler._slug(item_id)}-{assembler._slug(widget.get('presentation_role') or 'decision-view')}-{widget_index}"
                widget_id = _text(widget.get("id"))
                if not widget_id or widget_id in {entry.get("id") for entry in widgets}:
                    raise DashboardDeltaError(f"cumulative rebuild produced duplicate widget ID: {widget_id}")
                widgets.append(widget)
                generated_chart_entries[widget_id] = assembler._chart_map_entry(widget, item_id, item)
            flow = {
                "id": f"{group_id}-{assembler._slug(item_id)}",
                "title": item["requirement_title"],
                "order": flow_order,
                "widget_ids": [_text(widget.get("id")) for widget in item_widgets],
                "manager_admission": item_manager_admission,
                "presentation_audience": item_manager_admission["presentation_audience"],
            }
            if item.get("requirement_subtitle"):
                flow["subtitle"] = item["requirement_subtitle"]
            if item.get("takeaway"):
                flow["takeaway"] = item["takeaway"]
            if item.get("requirement_scope"):
                flow["scope"] = item["requirement_scope"]
            if item.get("limitations"):
                flow["limitations"] = list(item["limitations"])
            flow_defs.append(flow)
            if item_id in route_by_item:
                route_info = route_by_item[item_id]
                route_manifest.append({
                    "item_id": item_id,
                    "kind": route_info.get("kind", "existing"),
                    "group_id": group_id,
                    **({"title": _text(route_info["title"]).strip(), "order": route_info["order"]} if route_info.get("kind", "existing") == "new" else {}),
                })
        domain = {
            "id": group_id,
            "title": domain_title,
            "summary": domain_summary,
            "order": domain_order,
            "decision_flow": flow_defs,
        }
        domains.append(domain)

    if assigned != set(cumulative_ids):
        missing = sorted(set(cumulative_ids) - assigned)
        raise DashboardDeltaError(f"active plan omitted cumulative items: {missing}")
    generated_ids = {widget["id"] for widget in widgets}
    missing_parent_ids = parent_widget_ids - generated_ids
    if missing_parent_ids:
        raise DashboardDeltaError(f"cumulative rebuild changed stable widget IDs: {sorted(missing_parent_ids)[:5]}")
    # Parent chart entries remain byte-identical.  Current entries for newly
    # projected widgets are appended in deterministic fixture order.
    charts: list[dict[str, Any]] = []
    for entry_id in chart_order:
        prior = chart_by_id[entry_id]
        rebuilt = generated_chart_entries.get(entry_id)
        # Preserve byte-stable legacy geometry whenever the chart family is
        # unchanged.  Manager-only type changes (for example a relationship
        # progress projection becoming a semantic table) must use the rebuilt
        # map entry so renderer validation cannot observe a fixture/map split.
        if rebuilt is not None and rebuilt.get("type") != prior.get("type"):
            charts.append(copy.deepcopy(rebuilt))
        else:
            charts.append(copy.deepcopy(prior))
    for widget in widgets:
        widget_id = _text(widget.get("id"))
        if widget_id not in chart_by_id:
            charts.append(generated_chart_entries[widget_id])
    candidate_map = copy.deepcopy(dict(parent_map))
    candidate_map["charts"] = charts
    return domains, widgets, route_manifest, candidate_map


def _product_assets(receipt: Mapping[str, Any], receipt_ref: str) -> list[dict[str, Any]]:
    outputs = receipt.get("outputs")
    hashes = receipt.get("output_hashes")
    if not isinstance(outputs, Mapping) or not isinstance(hashes, Mapping):
        raise DashboardDeltaError("product receipt output bindings are missing")
    receipt_sha256 = receipt.get("receipt_sha256")
    if not _is_sha256(receipt_sha256):
        raise DashboardDeltaError("product receipt hash is missing")
    assets = [
        {"ref": outputs.get("fixture_ref"), "role": "reviewed_dashboard_fixture", "sha256": hashes.get("fixture_sha256")},
        {"ref": outputs.get("chart_map_ref"), "role": "dashboard_chart_map", "sha256": hashes.get("chart_map_sha256")},
        {"ref": outputs.get("chart_registry_ref"), "role": "dashboard_chart_registry", "sha256": hashes.get("chart_registry_sha256")},
        {"ref": _text(outputs.get("site_ref")).rstrip("/") + "/site_manifest.json", "role": "dashboard_site_manifest", "sha256": hashes.get("site_manifest_sha256")},
        {"ref": receipt_ref, "role": "dashboard_delta_receipt", "sha256": receipt_sha256},
    ]
    if any(not isinstance(asset["ref"], str) or not _is_sha256(asset["sha256"]) for asset in assets):
        raise DashboardDeltaError("product receipt asset bindings are incomplete")
    return assets


def _parent_manifest_path(context: RunContext, generation_id: str) -> Path:
    if generation_id == "G-0001":
        # A legacy root manifest has no receipt binding.  Once the explicit
        # bridge exists it is the only acceptable G-0001 parent authority;
        # malformed/symlinked bridges are returned to the strict validator
        # below rather than silently falling back to the legacy root.
        bridge = _safe_product_path(
            context,
            "generations/G-0001/product_manifest.json",
            label="legacy G-0001 parent bridge",
        )
        if bridge.exists() or bridge.is_symlink():
            return bridge
        return _safe_product_path(context, "product_manifest.json", label="parent product manifest")
    return _safe_product_path(context, f"generations/{generation_id}/product_manifest.json", label="parent product manifest")


def _active_product_manifest_path(context: RunContext, lifecycle: RunLifecycle) -> Path:
    """Resolve the active generation manifest through the lexical product boundary."""

    metadata = lifecycle.generation_metadata
    if metadata is None:
        raise DashboardDeltaError("active generation product manifest requires generation metadata")
    reference = _text(metadata.product_manifest_ref)
    if not reference.startswith("products/"):
        raise DashboardDeltaError("active generation product manifest reference is invalid")
    return _safe_product_path(
        context,
        reference.removeprefix("products/"),
        label="active generation product manifest",
    )


def _validate_parent_product_manifest(
    context: RunContext,
    path: Path,
    parent_generation_id: str,
    parent_receipt_path: Path,
) -> tuple[str, str, str, str]:
    """Require the immediate parent manifest to bind this exact receipt.

    Manifest and receipt references/hashes are returned independently.  They
    are separate lineage authorities; a child must never copy a receipt hash
    into its parent-manifest fields.
    """

    if not path.is_file() or path.is_symlink():
        raise DashboardDeltaError("immediate parent product manifest is missing or symlinked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDeltaError("immediate parent product manifest is invalid") from exc
    if not isinstance(value, Mapping) or path.read_bytes() != _canonical_bytes(value):
        raise DashboardDeltaError("immediate parent product manifest is not canonical")
    if parent_generation_id == "G-0001" and path == _safe_product_path(
        context,
        "generations/G-0001/product_manifest.json",
        label="legacy G-0001 parent bridge",
    ):
        try:
            _legacy_bridge().validate_legacy_parent_bridge(
                context,
                path,
                receipt_path=_relative_run_ref(context, parent_receipt_path),
            )
        except Exception as exc:
            raise DashboardDeltaError("legacy G-0001 parent bridge is invalid") from exc
    if value.get("run_id") != context.run_id or value.get("status") not in {"complete", "complete_with_limits"} or value.get("terminal") is not True:
        raise DashboardDeltaError("immediate parent product manifest is not terminal")
    if value.get("new_analytics") is not False:
        raise DashboardDeltaError("immediate parent product manifest claims new analytics")
    try:
        from auto_foundry_core.product_contracts import validate_product_manifest

        validate_product_manifest(value, require_all=True)
    except Exception as exc:
        raise DashboardDeltaError("immediate parent product manifest freeze markers are incomplete") from exc
    manifest_ref = _relative_run_ref(context, path)
    manifest_hash = _sha256_bytes(path.read_bytes())
    dashboard = value.get("dashboard")
    expected_receipt_ref = _relative_run_ref(context, parent_receipt_path)
    expected_receipt_sha256 = _sha256_bytes(parent_receipt_path.read_bytes())
    if manifest_ref == expected_receipt_ref:
        raise DashboardDeltaError("parent product manifest and receipt references must remain distinct")
    if not isinstance(dashboard, Mapping) or dashboard.get("receipt_ref") != expected_receipt_ref or dashboard.get("receipt_sha256") != expected_receipt_sha256:
        raise DashboardDeltaError("immediate parent product manifest does not bind the exact parent receipt")
    lifecycle = value.get("lifecycle")
    if not isinstance(lifecycle, Mapping) or lifecycle.get("generation_id") != parent_generation_id:
        raise DashboardDeltaError("immediate parent product manifest generation lineage is invalid")
    assets = value.get("assets")
    if not isinstance(assets, list) or sum(
        isinstance(asset, Mapping)
        and asset.get("ref") == expected_receipt_ref
        and asset.get("sha256") == expected_receipt_sha256
        for asset in assets
    ) != 1:
        raise DashboardDeltaError("immediate parent product manifest receipt asset is invalid")
    return manifest_ref, manifest_hash, expected_receipt_ref, expected_receipt_sha256


def _new_product_manifest(
    context: RunContext,
    lifecycle: RunLifecycle,
    metadata: Any,
    receipt: Mapping[str, Any],
    receipt_ref: str,
    parent_manifest_ref: str | None,
    parent_manifest_hash: str | None,
    status: str,
) -> dict[str, Any]:
    outputs = receipt["outputs"]
    assets = _product_assets(receipt, receipt_ref)
    _state_path, _state_value, active_state_hash, active_state_sha256 = _authoritative_state_binding(context, lifecycle)
    return {
        "schema_version": "1",
        "product_type": "reviewed_run_product_bundle",
        "run_id": context.run_id,
        "status": status,
        "terminal": True,
        "source_status": "reviewed_outputs_only",
        "new_analytics": False,
        "freeze_markers": dict(receipt["freeze_inputs"]["freeze_markers"]),
        "lifecycle": {
            "generation_id": metadata.generation_id,
            "generation_ordinal": metadata.generation_ordinal,
            "all_items_terminal": True,
            "all_items_integrated": True,
            "state_at_product_freeze": lifecycle.state,
        },
        "dashboard": {
            "assets_local": True,
            "internal_links_checked": True,
            "external_asset_refs": 0,
            "missing_evidence_refs": 0,
            "domain_count": receipt["domain_count"],
            "widget_count": receipt["widget_count"],
            "receipt_ref": receipt_ref,
            "receipt_sha256": receipt["receipt_sha256"],
        },
        "lem": receipt["freeze_inputs"]["summary"],
        "assets": assets,
        "lineage": {
            "parent_generation_id": metadata.parent_generation_id,
            "generation_manifest_hash": metadata.manifest_hash,
            "active_state_hash": active_state_hash,
            "active_state_sha256": active_state_sha256,
            "admission_state_hash": metadata.state_manifest_hash,
            "active_state_ref": _relative_run_ref(context, lifecycle.state_path),
            "active_plan_hash": receipt["plan_binding"]["sha256"],
            "admission_plan_hash": metadata.plan_hash,
            "parent_product_manifest_ref": parent_manifest_ref,
            "parent_product_manifest_sha256": parent_manifest_hash,
            "parent_receipt_ref": receipt["parent"]["receipt_ref"],
            "parent_receipt_sha256": receipt["parent"]["receipt_sha256"],
            "delta_receipt_ref": receipt_ref,
        },
        "limitations": list(_PRODUCT_LIMITATIONS),
        "presentation_plan_ref": receipt.get("presentation_plan_ref"),
        "presentation_plan_sha256": receipt.get("presentation_plan_sha256"),
        "manager_widget_ids": list(receipt.get("manager_widget_ids") or []),
    }


def _validate_product_manifest(
    context: RunContext,
    path: Path,
    value: Mapping[str, Any],
    receipt: Mapping[str, Any],
    lifecycle: RunLifecycle,
    metadata: Any,
    parent_manifest_ref: str,
    parent_manifest_hash: str,
) -> None:
    """Validate an already-published generation product without rewriting it."""

    raw_value = dict(value)
    missing_manifest_fields = _PRODUCT_MANIFEST_KEYS - set(raw_value)
    if missing_manifest_fields:
        if not missing_manifest_fields <= {"presentation_plan_ref", "presentation_plan_sha256", "manager_widget_ids"}:
            raise DashboardDeltaError("active generation product manifest schema is not exact")
        if path.read_bytes() != _canonical_bytes(raw_value):
            raise DashboardDeltaError("active generation product manifest is not canonical")
        value = {
            **raw_value,
            "presentation_plan_ref": raw_value.get("presentation_plan_ref"),
            "presentation_plan_sha256": raw_value.get("presentation_plan_sha256"),
            "manager_widget_ids": list(raw_value.get("manager_widget_ids") or []),
        }
    elif set(value) != _PRODUCT_MANIFEST_KEYS:
        raise DashboardDeltaError("active generation product manifest schema is not exact")
    if value.get("schema_version") != "1" or value.get("product_type") != "reviewed_run_product_bundle" or value.get("source_status") != "reviewed_outputs_only":
        raise DashboardDeltaError("active generation product manifest identity is invalid")
    if value.get("run_id") != context.run_id or value.get("status") not in {"complete", "complete_with_limits"} or value.get("terminal") is not True:
        raise DashboardDeltaError("active generation product manifest is not terminal")
    if value.get("new_analytics") is not False:
        raise DashboardDeltaError("active generation product manifest claims new analytics")
    if value.get("presentation_plan_ref") != receipt.get("presentation_plan_ref") or value.get("presentation_plan_sha256") != receipt.get("presentation_plan_sha256") or value.get("manager_widget_ids") != list(receipt.get("manager_widget_ids") or []):
        raise DashboardDeltaError("active generation product presentation plan binding is invalid")
    if value.get("presentation_plan_ref") is not None and (not isinstance(value.get("presentation_plan_ref"), str) or not value.get("presentation_plan_ref")):
        raise DashboardDeltaError("active generation product presentation plan reference is invalid")
    if value.get("presentation_plan_sha256") is not None and not _is_sha256(value.get("presentation_plan_sha256")):
        raise DashboardDeltaError("active generation product presentation plan hash is invalid")
    if not isinstance(value.get("manager_widget_ids"), list) or len(set(value.get("manager_widget_ids"))) != len(value.get("manager_widget_ids")) or any(not isinstance(item, str) or not item for item in value.get("manager_widget_ids")):
        raise DashboardDeltaError("active generation product manager_widget_ids are invalid")
    try:
        from auto_foundry_core.product_contracts import validate_product_manifest

        validate_product_manifest(value, require_all=True)
    except Exception as exc:
        raise DashboardDeltaError("active generation product freeze markers are not fully frozen") from exc
    if not missing_manifest_fields and path.read_bytes() != _canonical_bytes(value):
        raise DashboardDeltaError("active generation product manifest is not canonical")
    markers = value.get("freeze_markers")
    expected_markers = receipt.get("freeze_inputs", {}).get("freeze_markers")
    if not isinstance(markers, Mapping) or dict(markers) != dict(expected_markers or {}):
        raise DashboardDeltaError("active generation product freeze markers do not match delta receipt")
    lifecycle_meta = value.get("lifecycle")
    if not isinstance(lifecycle_meta, Mapping) or set(lifecycle_meta) != {"generation_id", "generation_ordinal", "all_items_terminal", "all_items_integrated", "state_at_product_freeze"} or lifecycle_meta.get("generation_id") != metadata.generation_id or lifecycle_meta.get("generation_ordinal") != metadata.generation_ordinal or lifecycle_meta.get("all_items_terminal") is not True or lifecycle_meta.get("all_items_integrated") is not True:
        raise DashboardDeltaError("active generation product generation binding is invalid")
    lineage = value.get("lineage")
    if not isinstance(lineage, Mapping) or set(lineage) != {
        "parent_generation_id", "generation_manifest_hash", "active_state_hash", "active_state_sha256", "admission_state_hash",
        "active_state_ref", "active_plan_hash", "admission_plan_hash", "parent_product_manifest_ref", "parent_product_manifest_sha256",
        "parent_receipt_ref", "parent_receipt_sha256", "delta_receipt_ref",
    }:
        raise DashboardDeltaError("active generation product lineage is invalid")
    expected_parent_receipt = receipt.get("parent") if isinstance(receipt.get("parent"), Mapping) else {}
    expected_lineage = {
        "parent_generation_id": metadata.parent_generation_id,
        "generation_manifest_hash": metadata.manifest_hash,
        "admission_state_hash": metadata.state_manifest_hash,
        "active_state_ref": _relative_run_ref(context, lifecycle.state_path),
        "active_plan_hash": receipt.get("plan_binding", {}).get("sha256") if isinstance(receipt.get("plan_binding"), Mapping) else None,
        "admission_plan_hash": metadata.plan_hash,
        "parent_product_manifest_ref": parent_manifest_ref,
        "parent_product_manifest_sha256": parent_manifest_hash,
        "parent_receipt_ref": expected_parent_receipt.get("receipt_ref"),
        "parent_receipt_sha256": expected_parent_receipt.get("receipt_sha256"),
        "delta_receipt_ref": receipt.get("outputs", {}).get("receipt_ref") if isinstance(receipt.get("outputs"), Mapping) else None,
    }
    for key, expected in expected_lineage.items():
        if lineage.get(key) != expected:
            raise DashboardDeltaError(f"active generation product lineage mismatch: {key}")
    _state_path, _state_value, active_state_hash, active_state_sha256 = _authoritative_state_binding(context, lifecycle)
    if lineage.get("active_state_hash") != active_state_hash or lineage.get("active_state_sha256") != active_state_sha256:
        raise DashboardDeltaError("active generation product active state hash is not authoritative")
    dashboard = value.get("dashboard")
    outputs = receipt.get("outputs")
    if not isinstance(dashboard, Mapping) or set(dashboard) != {
        "assets_local", "internal_links_checked", "external_asset_refs", "missing_evidence_refs",
        "domain_count", "widget_count", "receipt_ref", "receipt_sha256",
    } or not isinstance(outputs, Mapping) or dashboard.get("receipt_ref") != outputs.get("receipt_ref"):
        raise DashboardDeltaError("active generation product receipt binding is invalid")
    receipt_ref = outputs.get("receipt_ref")
    if not isinstance(receipt_ref, str):
        raise DashboardDeltaError("active generation product receipt reference is invalid")
    receipt_path = _safe_run_path(context, receipt_ref, label="delta receipt reference")
    receipt_sha256 = _sha256_bytes(receipt_path.read_bytes())
    if dashboard.get("receipt_sha256") != receipt_sha256:
        raise DashboardDeltaError("active generation product receipt hash is invalid")
    if dashboard.get("assets_local") is not True or dashboard.get("internal_links_checked") is not True or dashboard.get("external_asset_refs") != 0 or dashboard.get("missing_evidence_refs") != 0 or dashboard.get("domain_count") != receipt.get("domain_count") or dashboard.get("widget_count") != receipt.get("widget_count"):
        raise DashboardDeltaError("active generation product dashboard binding is invalid")
    expected_assets = _product_assets({**dict(receipt), "receipt_sha256": receipt_sha256}, receipt_ref)
    assets = value.get("assets")
    if assets != expected_assets:
        raise DashboardDeltaError("active generation product asset list is not exact")
    for asset in assets:
        if not isinstance(asset, Mapping):
            raise DashboardDeltaError("active generation product asset is invalid")
        reference = asset.get("ref")
        digest = asset.get("sha256")
        if not isinstance(reference, str) or not _is_sha256(digest):
            raise DashboardDeltaError("active generation product asset binding is invalid")
        asset_path = _safe_run_path(context, reference, label="product asset")
        if not asset_path.is_file() or asset_path.is_symlink() or _sha256_bytes(asset_path.read_bytes()) != digest:
            raise DashboardDeltaError(f"active generation product asset hash mismatch: {reference}")
    if value.get("lem") != receipt.get("freeze_inputs", {}).get("summary") or value.get("limitations") != _PRODUCT_LIMITATIONS:
        raise DashboardDeltaError("active generation product summary/limitations are not exact")


def _failpoint(value: str | None, name: str) -> None:
    aliases = {name}
    if name == "before_dashboard_publish":
        aliases.add("before_publish")
    if name == "before_first_rename":
        aliases.update({"before_dashboard_publish", "before_publish"})
    if name == "between_renames":
        aliases.add("after_old_target_rename")
    if name == "after_new_target_publish":
        aliases.add("after_dashboard_publish")
    if name == "before_lifecycle_reconcile":
        aliases.add("before_lifecycle_reconciliation")
    if name == "during_lifecycle_reconcile":
        aliases.update({"during_lifecycle_reconciliation", "during_lifecycle"})
    if name == "after_lifecycle_reconciliation":
        aliases.add("after_lifecycle_reconcile")
    if value in aliases:
        raise RuntimeError(f"dashboard delta failpoint: {value or name}")


def _require_terminal_freeze(receipt: Mapping[str, Any]) -> None:
    markers = receipt.get("freeze_inputs", {}).get("freeze_markers")
    try:
        from auto_foundry_core.product_contracts import validate_product_manifest

        validate_product_manifest({"freeze_markers": markers}, require_all=True)
    except Exception as exc:
        raise DashboardDeltaError("delta product freeze is incomplete; all canonical markers are required") from exc


def _assert_generation_current(context: RunContext, expected: Any) -> RunLifecycle:
    """Reload the pointer and reject a transition before publication."""

    try:
        current = RunLifecycle.load(context)
    except Exception as exc:
        raise _GenerationChanged("active generation could not be reloaded") from exc
    metadata = current.generation_metadata
    if metadata is None or metadata.generation_id != expected.generation_id or metadata.manifest_hash != expected.manifest_hash:
        raise _GenerationChanged("active generation changed during delta assembly")
    return current


def _validate_planless_legacy_receipt_shape(
    receipt: Mapping[str, Any],
    *,
    raw_bytes: bytes | None = None,
) -> None:
    """Validate the exact pre-plan receipt shape used for one migration.

    This is deliberately not a compatibility mode for current retries.  It
    accepts only the historical schema/version, with both top-level and
    request-level presentation fields absent.  The normalized receipt is
    passed through the current nested shape validator so all output, freeze,
    parent, route, and lineage fields remain mandatory.
    """

    if not isinstance(receipt, Mapping) or set(receipt) != _LEGACY_DELTA_RECEIPT_KEYS:
        raise DashboardDeltaError("legacy planless delta receipt schema is not exact")
    request_binding = receipt.get("request_binding")
    if not isinstance(request_binding, Mapping) or set(request_binding) != _LEGACY_DELTA_REQUEST_KEYS:
        raise DashboardDeltaError("legacy planless delta request binding schema is not exact")
    if receipt.get("schema_version") != DELTA_SCHEMA or receipt.get("status") != "complete":
        raise DashboardDeltaError("legacy planless delta receipt version/status is invalid")
    if raw_bytes is not None and raw_bytes != _canonical_bytes(receipt):
        raise DashboardDeltaError("legacy planless delta receipt is not canonical under its recorded schema")
    normalized = _normalize_existing_presentation_receipt(receipt)
    _validate_delta_receipt_shape(normalized)


def _normalize_existing_presentation_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only the additive presentation-plan fields of an old receipt.

    Older same-generation products predate the explicit plan contract.  Their
    immutable lineage remains validated exactly; these three presentation
    fields are the sole permitted absence for the documented plan migration.
    No other schema field is defaulted or ignored.
    """

    value = dict(receipt)
    value.setdefault("presentation_plan_ref", None)
    value.setdefault("presentation_plan_sha256", None)
    value.setdefault("manager_widget_ids", [])
    request = value.get("request_binding")
    if isinstance(request, Mapping):
        request_value = dict(request)
        request_value.setdefault("presentation_plan_ref", None)
        request_value.setdefault("presentation_plan_sha256", None)
        request_value.setdefault("manager_widget_ids", [])
        value["request_binding"] = request_value
    return value


def _validate_existing_delta_receipt(
    context: RunContext,
    receipt: Mapping[str, Any],
    *,
    receipt_path: Path,
    final_root: Path,
    fixture_rel: Path,
    map_rel: Path,
    registry_rel: Path,
    site_rel: Path,
    parent_receipt_path: Path,
    parent_receipt: Mapping[str, Any],
    parent_manifest_ref: str,
    parent_manifest_hash: str,
    parent_site_binding: Mapping[str, Any],
    parent_items: Sequence[Mapping[str, Any]],
    new_input_items: Sequence[Mapping[str, Any]],
    request_binding: Mapping[str, Any],
    plan: Mapping[str, Any],
    plan_path: Path,
    plan_hash: str,
    metadata: Any,
    route_manifest: Sequence[Mapping[str, Any]],
    projection_metadata: Mapping[str, Any],
) -> None:
    """Validate a published receipt against its own bytes and stable lineage.

    A receipt is a durable description of one already-published projection.
    The immutable identity/source bindings must still agree with the active
    generation, but renderer/projection summaries and other output-derived
    fields are intentionally validated against the receipt's *own* files.
    This lets a same-generation rebuild replace an older presentation after
    the code learns a new summary field without treating that older receipt as
    corrupted or version-incompatible.
    """

    _validate_delta_receipt_shape(receipt)
    expected_fixture = final_root / fixture_rel
    expected_map = final_root / map_rel
    expected_registry = final_root / registry_rel
    expected_site = final_root / site_rel
    expected_receipt_ref = _relative_run_ref(context, receipt_path)
    expected_outputs = {
        "fixture_ref": _relative_run_ref(context, expected_fixture),
        "chart_map_ref": _relative_run_ref(context, expected_map),
        "chart_registry_ref": _relative_run_ref(context, expected_registry),
        "site_ref": _relative_run_ref(context, expected_site),
        "receipt_ref": expected_receipt_ref,
    }
    paths = {
        key: _safe_run_path(context, value, label=f"existing delta {key}")
        for key, value in expected_outputs.items()
    }
    for key, path in paths.items():
        valid = path.is_dir() if key == "site_ref" else path.is_file()
        if not valid or path.is_symlink():
            raise DashboardDeltaError(f"existing delta output is missing or symlinked: {key}")
    fixture = _read_json(context, expected_outputs["fixture_ref"], label="existing delta fixture")
    site_manifest = paths["site_ref"] / "site_manifest.json"
    if not site_manifest.is_file() or site_manifest.is_symlink():
        raise DashboardDeltaError("existing delta site manifest is missing or symlinked")
    child_site_binding = _validate_site_binding(paths["site_ref"], receipt.get("site_binding") or {}, label="existing delta")
    old_files = parent_site_binding.get("files", {}) if isinstance(parent_site_binding.get("files"), Mapping) else {}
    new_files = child_site_binding.get("files", {})
    affected = [
        {"path": value, "sha256": new_files.get(value)}
        for value in sorted(set(old_files) | set(new_files))
        if old_files.get(value) != new_files.get(value)
    ]
    unchanged = [
        {"path": value, "sha256": new_files.get(value)}
        for value in sorted(set(old_files) | set(new_files))
        if old_files.get(value) == new_files.get(value)
    ]
    output_hashes = {
        "fixture_sha256": _sha256_bytes(paths["fixture_ref"].read_bytes()),
        "chart_map_sha256": _sha256_bytes(paths["chart_map_ref"].read_bytes()),
        "chart_registry_sha256": _sha256_bytes(paths["chart_registry_ref"].read_bytes()),
        "site_manifest_sha256": _sha256_bytes(site_manifest.read_bytes()),
    }
    expected_parent = {
        "product_manifest_ref": parent_manifest_ref,
        "product_manifest_sha256": parent_manifest_hash,
        "receipt_ref": _relative_run_ref(context, parent_receipt_path),
        "receipt_sha256": _sha256_bytes(parent_receipt_path.read_bytes()),
        "site_binding": dict(parent_site_binding),
        "site_tree_sha256": parent_site_binding.get("tree_sha256"),
    }
    expected_parent_receipt_hash = expected_parent["receipt_sha256"]
    expected_plan = {
        "ref": _relative_run_ref(context, plan_path),
        "sha256": plan_hash,
        "admission_sha256": metadata.plan_hash,
        "generation_id": metadata.generation_id,
        "revision": plan.get("revision"),
        "route": list(route_manifest),
    }
    expected_receipt = {
        "schema_version": DELTA_SCHEMA,
        "status": "complete",
        "run_id": context.run_id,
        "generation_id": metadata.generation_id,
        "generation_ordinal": metadata.generation_ordinal,
        "parent_generation_id": metadata.parent_generation_id,
        "source_policy": "accepted_and_committed_only",
        "new_analytics": False,
        "parent": expected_parent,
        "request_binding": dict(request_binding),
        "plan_binding": expected_plan,
        "input_items": [*parent_items, *new_input_items],
        "outputs": expected_outputs,
        "output_hashes": output_hashes,
        "site_binding": child_site_binding,
        "freeze_inputs": dict(projection_metadata),
        "old_projection": {
            "projection_hash": parent_receipt.get("freeze_inputs", {}).get("projection_hash"),
            "export_sha256": parent_receipt.get("freeze_inputs", {}).get("export_sha256"),
        },
        "new_projection": {
            "projection_hash": projection_metadata["projection_hash"],
            "export_sha256": projection_metadata["export_sha256"],
        },
        "affected_paths": affected,
        "unchanged_paths": unchanged,
        "rollback_parent": {
            "generation_id": metadata.parent_generation_id,
            "product_manifest_ref": parent_manifest_ref,
            "product_manifest_sha256": parent_manifest_hash,
            "receipt_ref": expected_parent["receipt_ref"],
            "receipt_sha256": expected_parent_receipt_hash,
            "site_tree_sha256": parent_site_binding.get("tree_sha256"),
        },
        "widget_count": len(fixture.get("widgets", [])) if isinstance(fixture.get("widgets"), list) else None,
        "domain_count": len(fixture.get("domains", [])) if isinstance(fixture.get("domains"), list) else None,
        "retry": "idempotent only when parent/input/route/projection/output hashes match",
    }

    # These fields are the durable identity and source lineage of the run.
    # They are not renderer products and therefore must be reconstructed from
    # the current authoritative generation before an existing output can be
    # used as the base for a corrected same-generation build.
    stable_fields = (
        "schema_version",
        "status",
        "run_id",
        "generation_id",
        "generation_ordinal",
        "parent_generation_id",
        "source_policy",
        "new_analytics",
        "parent",
        "request_binding",
        "plan_binding",
        "input_items",
        "outputs",
        "old_projection",
        "new_projection",
        "rollback_parent",
        "retry",
    )
    for field in stable_fields:
        actual_field = receipt.get(field)
        expected_field = expected_receipt.get(field)
        if field == "request_binding" and isinstance(actual_field, Mapping) and isinstance(expected_field, Mapping):
            actual_field = {key: value for key, value in actual_field.items() if key not in {"presentation_plan_ref", "presentation_plan_sha256", "manager_widget_ids"}}
            expected_field = {key: value for key, value in expected_field.items() if key not in {"presentation_plan_ref", "presentation_plan_sha256", "manager_widget_ids"}}
        if actual_field != expected_field:
            raise DashboardDeltaError(f"existing delta receipt stable binding is invalid: {field}")

    # The remaining fields are checked for internal consistency with the
    # already-published bytes above, rather than reconstructed from the new
    # renderer.  A changed renderer is precisely what a same-generation
    # rebuild is allowed to correct.
    own_fields = (
        "output_hashes",
        "site_binding",
        "affected_paths",
        "unchanged_paths",
        "widget_count",
        "domain_count",
    )
    for field in own_fields:
        if receipt.get(field) != expected_receipt.get(field):
            raise DashboardDeltaError(f"existing delta receipt output binding is invalid: {field}")

    existing_freeze = receipt.get("freeze_inputs")
    expected_freeze = projection_metadata
    if not isinstance(existing_freeze, Mapping):
        raise DashboardDeltaError("existing delta freeze inputs are invalid")
    # Summary is a code-derived presentation projection.  Its shape and
    # values may grow or change between rebuilds (for example, a newly exposed
    # knowledge count), while all source/projection/asset bindings remain
    # immutable and are checked exactly.
    for field in _DELTA_FREEZE_KEYS - {"summary"}:
        if existing_freeze.get(field) != expected_freeze.get(field):
            raise DashboardDeltaError(f"existing delta receipt freeze binding is invalid: {field}")
    if not isinstance(existing_freeze.get("summary"), Mapping) or not isinstance(expected_freeze.get("summary"), Mapping):
        raise DashboardDeltaError("existing delta freeze summary is invalid")


def _assemble_dashboard_delta_locked(
    context: RunContext,
    *,
    parent_receipt_ref: str | Path | None = None,
    route: Mapping[str, Any],
    output_dir: str | Path | None = None,
    failpoint: str | None = None,
    presentation_plan_ref: str | Path | None = None,
    expected_generation: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Build and atomically publish the active generation's dashboard delta."""

    if not isinstance(context, RunContext):
        raise TypeError("assemble_dashboard_delta requires a RunContext")
    try:
        lifecycle = RunLifecycle.load(context)
    except Exception as exc:
        raise DashboardDeltaError("active lifecycle cannot be loaded") from exc
    metadata = lifecycle.generation_metadata
    if metadata is None:
        raise DashboardDeltaError("dashboard delta requires an active appended generation")
    if expected_generation is not None and (
        metadata.generation_id != expected_generation[0] or metadata.manifest_hash != expected_generation[1]
    ):
        raise _GenerationChanged("active generation changed before delta assembly")
    if lifecycle.snapshot.mode != "requirement":
        raise DashboardDeltaError("dashboard delta requires Requirement Mode")
    added_ids = tuple(metadata.added_item_ids)
    if not added_ids:
        raise DashboardDeltaError("active generation has no added requirements")
    routes = _route_map(route, added_ids)
    plan, plan_path, plan_hash = _load_plan(context, lifecycle)
    assembler = _assembler()
    resolved_presentation_plan_ref: str | None = None
    presentation_plan: Mapping[str, Any] | None = None
    presentation_plan_sha256: str | None = None
    presentation_plan_bytes: bytes | None = None
    manager_widget_ids: list[str] = []
    manager_entries: dict[str, Mapping[str, Any]] = {}
    if presentation_plan_ref is not None:
        resolved_presentation_plan_ref = assembler._presentation_plan_ref(
            context,
            metadata.generation_id,
            presentation_plan_ref,
        )
        presentation_plan, presentation_plan_sha256 = assembler._load_business_presentation_plan(
            context,
            resolved_presentation_plan_ref,
        )
        presentation_plan_path = _safe_run_path(
            context,
            resolved_presentation_plan_ref,
            label="business presentation plan",
        )
        presentation_plan_bytes = presentation_plan_path.read_bytes()
        if _sha256_bytes(presentation_plan_bytes) != presentation_plan_sha256:
            raise DashboardDeltaError("business presentation plan hash changed during validation")
        if presentation_plan_bytes != assembler._canonical_bytes(presentation_plan):
            raise DashboardDeltaError("business presentation plan is not canonical")
        plan_bytes = plan_path.read_bytes() if plan_path.is_file() else b""
        if plan_bytes != assembler._canonical_bytes(plan):
            raise DashboardDeltaError("active generation plan is not canonical")
        if presentation_plan.get("schema_version") == assembler.PRESENTATION_PLAN_V2_SCHEMA:
            manager_widget_ids = list(presentation_plan["manager_widget_ids"])
        manager_entries = {entry["widget_id"]: entry for entry in presentation_plan["manager_entries"]}
    # ``metadata.plan_hash`` is immutable admission lineage.  The active plan
    # may legitimately be revised while the generation is running; receipt
    # and child product bindings must retain both hashes separately.
    if not _is_sha256(metadata.plan_hash):
        raise DashboardDeltaError("active generation admission plan hash is invalid")
    parent_state_path, parent_state_hash, parent_state_sha256, parent_plan_path, parent_plan_hash = _parent_generation_bindings(context, metadata)
    _validate_plan_membership(plan, added_ids)
    plan_records = plan.get("input_records")
    plan_ids = tuple(
        _text(value.get("requirement_id"))
        for value in plan_records
        if isinstance(value, Mapping)
    ) if isinstance(plan_records, list) else ()
    if plan_ids != tuple(metadata.cumulative_item_ids):
        raise DashboardDeltaError("active generation plan item order does not match lifecycle cumulative IDs")
    parent_receipt_path, parent_receipt = _resolve_parent_receipt(context, parent_receipt_ref, lifecycle)
    if parent_receipt.get("run_id") != context.run_id:
        raise DashboardDeltaError("parent receipt run identity mismatch")
    parent_root, parent_refs = _parent_output_root(context, parent_receipt_path, parent_receipt)
    parent_site_binding = dict(parent_receipt["site_binding"])
    parent_fixture = _read_json(context, _relative_run_ref(context, parent_refs["fixture_ref"]), label="parent dashboard fixture")
    parent_map = _read_json(context, _relative_run_ref(context, parent_refs["chart_map_ref"]), label="parent chart map")
    if parent_fixture.get("schema_version") != FIXTURE_SCHEMA or parent_map.get("schema_version") != CHART_MAP_SCHEMA:
        raise DashboardDeltaError("parent dashboard fixture/map schema is unsupported")
    parent_items = parent_receipt.get("input_items")
    if not isinstance(parent_items, list):
        raise DashboardDeltaError("parent receipt input_items are missing")
    old_ids = tuple(_text(value.get("item_id")) for value in parent_items if isinstance(value, Mapping))
    if parent_fixture.get("run_id", context.run_id) != context.run_id:
        raise DashboardDeltaError("parent dashboard run identity mismatch")
    if set(added_ids).intersection(old_ids):
        raise DashboardDeltaError("parent receipt already contains an added requirement")
    if tuple(parent_receipt.get("freeze_inputs", {}).get("item_order", old_ids)) != old_ids:
        raise DashboardDeltaError("parent receipt item order is inconsistent")
    _validate_routes_against_plan(plan, parent_fixture, old_ids, added_ids, routes)

    cumulative_ids = old_ids + added_ids
    if tuple(metadata.cumulative_item_ids) != cumulative_ids:
        raise DashboardDeltaError("active generation cumulative item order does not match parent plus added IDs")
    loaded: dict[str, dict[str, Any]] = {}
    authoritative_roots: dict[str, Mapping[str, Any]] = {}
    record_file_hashes: dict[str, str] = {}
    new_records: list[Mapping[str, Any]] = []
    new_input_items: list[dict[str, Any]] = []
    parent_item_bindings = {
        _text(value.get("item_id")): value
        for value in parent_items
        if isinstance(value, Mapping) and _text(value.get("item_id"))
    }
    if set(parent_item_bindings) != set(old_ids):
        raise DashboardDeltaError("parent receipt item bindings are not an exact ordered set")
    # Re-read every cumulative accepted/integrated bundle.  The old parent
    # fixture is only a chart-hint/input boundary; it is never the semantic
    # source for the rebuilt manager presentation.
    for item_id in cumulative_ids:
        item, records, integration_manifest = _input_item(context, assembler, item_id)
        loaded[item_id] = item
        records_path = context.resolve_run_path(f"requirements/{item_id}/integration/committed/records.jsonl")
        records_file_sha = assembler._sha256_bytes(records_path.read_bytes())
        for record in records:
            record_id = assembler._text(record.get("record_id")).strip()
            if record_id:
                authoritative_roots[record_id] = assembler._presentation_authoritative_root(record, item["content"])
                record_file_hashes[record_id] = records_file_sha
        if item_id in old_ids:
            bound = parent_item_bindings[item_id]
            if (
                bound.get("accepted_content_hash") != item["accepted_content_hash"]
                or bound.get("accepted_manifest_hash") != item["accepted_manifest_hash"]
                or bound.get("integration_manifest_hash") != item["integration_manifest_hash"]
                or bound.get("record_count") != len(records)
            ):
                raise DashboardDeltaError(f"parent cumulative input binding drifted: {item_id}")
        else:
            new_records.extend(records)
            new_input_items.append({"item_id": item_id, "accepted_content_hash": item["accepted_content_hash"], "accepted_manifest_hash": item["accepted_manifest_hash"], "integration_manifest_hash": item["integration_manifest_hash"], "record_count": len(records)})

    lem_summary, projection_metadata = _load_delta_projection_metadata(context, cumulative_ids)
    # Freeze readiness is an admission check, not a post-publication cleanup.
    # In particular, missing telemetry must leave no child dashboard/receipt
    # that would later conflict with the changed projection hash.  The same
    # active generation can retry after telemetry is frozen.
    _require_terminal_freeze({"freeze_inputs": projection_metadata})

    current_input_items = [
        *parent_items,
        *new_input_items,
    ]
    presentation_parent = (
        assembler._presentation_parent_binding(context, metadata.generation_id, metadata)
        if presentation_plan is not None
        else None
    )
    if presentation_plan is not None:
        if presentation_plan.get("schema_version") == assembler.PRESENTATION_PLAN_V2_SCHEMA:
            assembler._validate_v2_plan_lineage(
                context,
                presentation_plan,
                generation_id=metadata.generation_id,
                supervisor_ref=_relative_run_ref(context, plan_path),
                input_items=current_input_items,
                parent=presentation_parent,
            )
        else:
            manager_widget_ids = assembler._validate_business_presentation_plan(
                context,
                presentation_plan,
                generation_id=metadata.generation_id,
                supervisor_ref=_relative_run_ref(context, plan_path),
                supervisor_plan=plan,
                input_items=current_input_items,
                parent=presentation_parent,
                authoritative_roots=authoritative_roots,
                record_file_hashes=record_file_hashes,
            )

    domains, widgets, route_manifest, candidate_map = _rebuild_cumulative_dashboard_projection(
        assembler=assembler,
        plan=plan,
        cumulative_ids=cumulative_ids,
        old_ids=old_ids,
        added_ids=added_ids,
        loaded=loaded,
        parent_fixture=parent_fixture,
        parent_map=parent_map,
        routes=routes,
        manager_widget_ids=manager_widget_ids,
        manager_entries=manager_entries,
        presentation_plan_ref=resolved_presentation_plan_ref,
        presentation_plan_sha256=presentation_plan_sha256,
    )

    if presentation_plan is not None:
        if presentation_plan.get("schema_version") == assembler.PRESENTATION_PLAN_V2_SCHEMA:
            assembler._validate_business_presentation_plan_v2(
                context,
                presentation_plan,
                fixture={"widgets": widgets},
                fixture_ref=_relative_run_ref(context, parent_refs["fixture_ref"]),
                chart_map=candidate_map,
                chart_map_ref=_relative_run_ref(context, parent_refs["chart_map_ref"]),
                widgets=widgets,
                strict_source_hash=False,
            )
        else:
            assembler._validate_business_presentation_plan(
                context,
                presentation_plan,
                generation_id=metadata.generation_id,
                supervisor_ref=_relative_run_ref(context, plan_path),
                supervisor_plan=plan,
                input_items=current_input_items,
                parent=presentation_parent,
                widgets=widgets,
                authoritative_roots=authoritative_roots,
                record_file_hashes=record_file_hashes,
            )

    candidate_overview_ids = assembler._apply_overview_selection(widgets)
    audit_records = assembler._audit_record_entries(
        {item_id: loaded[item_id]["records"] for item_id in cumulative_ids},
        widgets,
    )
    audit_widgets = assembler._audit_widget_entries(widgets)

    candidate_fixture = copy.deepcopy(dict(parent_fixture))
    candidate_fixture["domains"] = domains
    candidate_fixture["widgets"] = widgets
    candidate_fixture["audit_records"] = audit_records
    candidate_fixture["audit_widgets"] = audit_widgets
    candidate_fixture["audit_widget_entry_count"] = len(audit_widgets)
    candidate_fixture["run_id"] = context.run_id
    candidate_fixture["lem_projection_hash"] = projection_metadata["projection_hash"]
    candidate_fixture["lem_export_sha256"] = projection_metadata["export_sha256"]
    candidate_fixture["ontology_summary"] = lem_summary
    candidate_fixture["prepared_registry_hash"] = projection_metadata["prepared_registry"]["sha256"]
    candidate_fixture["telemetry_metadata_hash"] = projection_metadata["telemetry"]["sha256"]
    nodes, edges, groups = _merge_ontology(parent_fixture, new_records)
    candidate_fixture["ontology_objects"] = nodes
    candidate_fixture["ontology_relationships"] = edges
    candidate_fixture["ontology_groups"] = groups
    candidate_fixture["freeze_markers"] = dict(projection_metadata["freeze_markers"])
    candidate_fixture["overview_widget_ids"] = candidate_overview_ids
    candidate_fixture["presentation_plan_ref"] = resolved_presentation_plan_ref
    candidate_fixture["presentation_plan_sha256"] = presentation_plan_sha256
    candidate_fixture["manager_widget_ids"] = list(manager_widget_ids)
    candidate_fixture["manager_entries"] = [copy.deepcopy(manager_entries[key]) for key in manager_widget_ids]
    candidate_fixture["manager_admission"] = {
        "policy": "explicit_business_presentation_plan",
        "presentation_plan_ref": resolved_presentation_plan_ref,
        "presentation_plan_sha256": presentation_plan_sha256,
        "business_requirements": sorted({
            _text(widget.get("requirement_id"))
            for widget in widgets
            if isinstance(widget.get("manager_admission"), Mapping)
            and widget["manager_admission"].get("status") == "admitted"
        }),
        "technical_requirements": sorted({
            _text(widget.get("requirement_id"))
            for widget in widgets
            if _text(widget.get("requirement_id"))
            and (not isinstance(widget.get("manager_admission"), Mapping) or widget["manager_admission"].get("status") != "admitted")
        }),
    }
    if presentation_plan is not None and presentation_plan.get("schema_version") == assembler.PRESENTATION_PLAN_V2_SCHEMA:
        candidate_fixture["manager_visual_widget_ids"] = list(presentation_plan["manager_visual_widget_ids"])
        candidate_fixture["audit_visual_widget_ids"] = list(presentation_plan["audit_visual_widget_ids"])
        candidate_fixture["visual_entries"] = copy.deepcopy(presentation_plan["visual_entries"])
        candidate_fixture["presentation_plan_schema"] = assembler.PRESENTATION_PLAN_V2_SCHEMA

    generation_root = _safe_product_path(context, f"generations/{metadata.generation_id}", label="generation product namespace")
    final_root = _safe_product_path(context, output_dir or f"generations/{metadata.generation_id}/dashboard", label="delta output namespace")
    canonical_root = _safe_product_path(context, f"generations/{metadata.generation_id}/dashboard", label="generation dashboard namespace")
    if final_root != canonical_root:
        raise DashboardDeltaError("output_dir must be the active generation dashboard namespace")
    try:
        final_root.relative_to(generation_root)
    except ValueError as exc:
        raise DashboardDeltaError("output_dir must remain inside the active generation product namespace") from exc
    if final_root == generation_root:
        raise DashboardDeltaError("output_dir must be a dashboard child namespace")
    fixture_rel = parent_refs["fixture_ref"].relative_to(parent_root)
    map_rel = parent_refs["chart_map_ref"].relative_to(parent_root)
    registry_rel = parent_refs["chart_registry_ref"].relative_to(parent_root)
    site_rel = parent_refs["site_ref"].relative_to(parent_root)
    # Validate the generation-scoped terminal manifest leaf/components before
    # creating any candidate output.  RunLifecycle stores the reference, but
    # its resolved convenience property is not an admission boundary.
    product_manifest_path = _active_product_manifest_path(context, lifecycle)
    child_manifest_ref = _relative_run_ref(context, product_manifest_path)
    parent_manifest_path = _parent_manifest_path(context, metadata.parent_generation_id)
    receipt_path = final_root / "build_receipt.json"
    parent_manifest_ref, parent_manifest_hash, parent_receipt_ref_bound, parent_receipt_sha256_bound = _validate_parent_product_manifest(
        context,
        parent_manifest_path,
        metadata.parent_generation_id,
        parent_receipt_path,
    )

    parent_receipt_hash = _sha256_bytes(parent_receipt_path.read_bytes())
    if parent_receipt_ref_bound != _relative_run_ref(context, parent_receipt_path) or parent_receipt_sha256_bound != parent_receipt_hash:
        raise DashboardDeltaError("immediate parent manifest receipt binding drifted")
    request_binding = {
        "parent_receipt_ref": _relative_run_ref(context, parent_receipt_path),
        "parent_receipt_sha256": parent_receipt_hash,
        "parent_site_tree_sha256": parent_site_binding.get("tree_sha256"),
        "generation_id": metadata.generation_id,
        "generation_ordinal": metadata.generation_ordinal,
        "parent_generation_id": metadata.parent_generation_id,
        "generation_manifest_hash": metadata.manifest_hash,
        "admission_state_hash": metadata.state_manifest_hash,
        "parent_state_ref": _relative_run_ref(context, parent_state_path),
        "parent_state_hash": parent_state_hash,
        "parent_state_sha256": parent_state_sha256,
        "parent_plan_ref": _relative_run_ref(context, parent_plan_path),
        "parent_plan_hash": parent_plan_hash,
        "state_ref": _relative_run_ref(context, lifecycle.state_path),
        "plan_ref": _relative_run_ref(context, plan_path),
        "plan_sha256": plan_hash,
        "admission_plan_sha256": metadata.plan_hash,
        "output_root_ref": _relative_run_ref(context, final_root),
        "route": route_manifest,
        "new_items": new_input_items,
        "projection_hash": projection_metadata["projection_hash"],
        "presentation_plan_ref": resolved_presentation_plan_ref,
        "presentation_plan_sha256": presentation_plan_sha256,
        "manager_widget_ids": list(manager_widget_ids),
    }

    # Recover a v2 whole-generation replacement first.  All v2 recovery
    # filesystem mutations are guarded by the lifecycle run lock.  The
    # immediate reload closes the race where RequirementRunExtension admits
    # G-0003 after the wrapper's initial reload but before this recovery point.
    # The generation lock remains the outer lock, matching the publication
    # boundary below; admission only takes the run lock and therefore cannot
    # deadlock with this order.
    transaction_state: tuple[Path, dict[str, Any]] | None
    with RunLifecycle._run_lock(context):  # noqa: SLF001 - recovery CAS boundary
        latest = RunLifecycle._load_unlocked(context)  # noqa: SLF001
        latest_meta = latest.generation_metadata
        if latest_meta is None:
            raise _GenerationChanged("active generation disappeared before transaction recovery")
        if latest_meta.generation_id != metadata.generation_id or latest_meta.manifest_hash != metadata.manifest_hash:
            # The old generation no longer owns the pointer.  Resolve only its
            # bound intent: old bytes stay old, a missing target is restored,
            # and a new target survives only when the next generation's
            # parent state/plan lineage proves adoption.
            _abort_inactive_generation_product_transaction(
                context,
                latest=latest,
                transaction_generation_id=metadata.generation_id,
                target_root=generation_root,
                defer_unbound_new=True,
            )
            raise _GenerationChanged("active generation changed before transaction recovery")
        _assert_live_plan_binding(context, latest, plan_path, plan_hash)
        # Validate the parent state/plan lineage while the pointer is stable;
        # no rename is permitted if either authority drifted.
        _parent_generation_bindings(context, latest_meta)
        transaction_state = _recover_generation_product_transaction(
            context,
            metadata=latest_meta,
            target_root=generation_root,
        )
    if transaction_state is None:
        transaction_state = _recover_generation_transaction(
            context,
            metadata=metadata,
            final_root=final_root,
        )
    existing_receipt: dict[str, Any] | None = None
    v1_to_v2_migration = False
    if final_root.exists() or final_root.is_symlink():
        if not receipt_path.is_file() or receipt_path.is_symlink():
            raise DashboardDeltaError("candidate output exists without a complete delta receipt")
        try:
            existing_raw_bytes = receipt_path.read_bytes()
            existing_raw = json.loads(existing_raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DashboardDeltaError("existing delta receipt is invalid") from exc
        if not isinstance(existing_raw, Mapping) or existing_raw.get("schema_version") != DELTA_SCHEMA or existing_raw.get("status") != "complete":
            raise DashboardDeltaError("existing candidate output has an invalid delta receipt")
        raw_plan_fields = {"presentation_plan_ref", "presentation_plan_sha256", "manager_widget_ids"}
        raw_request_binding = existing_raw.get("request_binding")
        # A planless receipt is recognized only by the historical shape: the
        # additive presentation fields are absent at both receipt levels.  A
        # current producer must omit those fields when no plan is supplied;
        # accepting null/[] placeholders here would turn a non-canonical
        # self-produced receipt into an implicit compatibility mode.
        planless_legacy = (
            isinstance(raw_request_binding, Mapping)
            and not any(key in existing_raw for key in raw_plan_fields)
            and not any(key in raw_request_binding for key in raw_plan_fields)
        )
        if planless_legacy:
            _validate_planless_legacy_receipt_shape(existing_raw, raw_bytes=existing_raw_bytes)
        existing = _normalize_existing_presentation_receipt(existing_raw)
        existing_plan_ref = existing.get("presentation_plan_ref")
        existing_plan_hash = existing.get("presentation_plan_sha256")
        existing_manager_ids = existing.get("manager_widget_ids")
        if (
            presentation_plan is not None
            and presentation_plan.get("schema_version") == assembler.PRESENTATION_PLAN_V2_SCHEMA
            and not planless_legacy
            and existing_plan_ref not in (None, "")
            and (
                existing_plan_ref != resolved_presentation_plan_ref
                or existing_plan_hash != presentation_plan_sha256
                or existing_manager_ids != list(manager_widget_ids)
            )
        ):
            # A live G3 product may have been published under the predecessor
            # V1 plan before the public V1->V2 CAS revision.  Permit exactly
            # that one lineage transition: the successor explicitly names
            # the predecessor path/hash, and the existing fixture carries the
            # same old ref/hash/IDs.  The old product manifest is revalidated
            # below against this unchanged receipt before any replacement.
            try:
                predecessor_ids, predecessor_entries = assembler._v2_predecessor_manager_contract(presentation_plan)
            except Exception as exc:
                raise DashboardDeltaError("existing V1 presentation plan predecessor binding is invalid") from exc
            source_bindings = presentation_plan.get("source_bindings")
            predecessor_ref = source_bindings.get("previous_plan_ref") if isinstance(source_bindings, Mapping) else None
            predecessor_hash = source_bindings.get("previous_plan_sha256") if isinstance(source_bindings, Mapping) else None
            if (
                existing_plan_ref != predecessor_ref
                or existing_plan_hash != predecessor_hash
                or existing_manager_ids != predecessor_ids
            ):
                raise DashboardDeltaError("existing V1 presentation plan is not the approved V2 predecessor")
            predecessor_request = existing.get("request_binding")
            if (
                not isinstance(predecessor_request, Mapping)
                or predecessor_request.get("presentation_plan_ref") != existing_plan_ref
                or predecessor_request.get("presentation_plan_sha256") != existing_plan_hash
                or predecessor_request.get("manager_widget_ids") != predecessor_ids
            ):
                raise DashboardDeltaError("existing V1 request presentation binding is not the approved predecessor")
            legacy_fixture = _read_json(context, _relative_run_ref(context, final_root / fixture_rel), label="existing V1 dashboard fixture")
            if (
                legacy_fixture.get("presentation_plan_ref") != existing_plan_ref
                or legacy_fixture.get("presentation_plan_sha256") != existing_plan_hash
                or legacy_fixture.get("manager_widget_ids") != predecessor_ids
                or legacy_fixture.get("manager_entries") != predecessor_entries
                or legacy_fixture.get("presentation_plan_schema") == assembler.PRESENTATION_PLAN_V2_SCHEMA
                or legacy_fixture.get("manager_visual_widget_ids") not in (None, [])
                or legacy_fixture.get("audit_visual_widget_ids") not in (None, [])
            ):
                raise DashboardDeltaError("existing V1 dashboard fixture binding is invalid")
            fixture_path = context.resolve_run_path(_relative_run_ref(context, final_root / fixture_rel))
            if fixture_path.read_bytes() != assembler._canonical_bytes(legacy_fixture):
                raise DashboardDeltaError("existing V1 dashboard fixture is not canonical")
            v1_to_v2_migration = True
        if (
            presentation_plan is not None
            and not planless_legacy
            and existing_plan_ref in (None, "")
            and existing_plan_hash in (None, "")
            and existing_manager_ids in (None, [])
        ):
            raise DashboardDeltaError("explicit presentation-plan migration requires a planless legacy receipt")
        if presentation_plan is None and (existing_plan_ref not in (None, "") or existing_plan_hash not in (None, "") or existing_manager_ids not in (None, [])):
            raise DashboardDeltaError("existing delta output requires its explicit presentation plan")
        if presentation_plan is not None and existing_plan_ref not in (None, "") and not v1_to_v2_migration and (
            existing_plan_ref != resolved_presentation_plan_ref or existing_plan_hash != presentation_plan_sha256 or existing_manager_ids != list(manager_widget_ids)
        ):
            raise DashboardDeltaError("existing delta output presentation plan binding conflicts")
        if existing.get("request_binding") != request_binding:
            # A pre-plan receipt may be migrated exactly once to a validated
            # plan.  Stable lineage is still checked below; only the additive
            # presentation fields may differ.
            old_request = existing.get("request_binding")
            stable_request = dict(request_binding)
            for key in ("presentation_plan_ref", "presentation_plan_sha256", "manager_widget_ids"):
                stable_request.pop(key, None)
            old_stable_request = dict(old_request) if isinstance(old_request, Mapping) else None
            if isinstance(old_stable_request, Mapping):
                for key in ("presentation_plan_ref", "presentation_plan_sha256", "manager_widget_ids"):
                    old_stable_request.pop(key, None)
            if old_stable_request != stable_request or not (
                presentation_plan is not None
                and (
                    (
                        existing_plan_ref in (None, "")
                        and existing_plan_hash in (None, "")
                        and existing_manager_ids in (None, [])
                    )
                    or v1_to_v2_migration
                )
            ):
                raise DashboardDeltaError("existing delta output conflicts with parent/input/route/projection")
        if existing.get("run_id") != context.run_id or existing.get("generation_id") != metadata.generation_id or existing.get("generation_ordinal") != metadata.generation_ordinal or existing.get("parent_generation_id") != metadata.parent_generation_id or existing.get("new_analytics") is not False:
            raise DashboardDeltaError("existing delta receipt generation binding is invalid")
        if existing.get("input_items") != [*parent_items, *new_input_items]:
            raise DashboardDeltaError("existing delta receipt input binding is invalid")
        existing_parent = existing.get("parent")
        if (
            not isinstance(existing_parent, Mapping)
            or existing_parent.get("product_manifest_ref") != parent_manifest_ref
            or existing_parent.get("product_manifest_sha256") != parent_manifest_hash
            or existing_parent.get("receipt_ref") != _relative_run_ref(context, parent_receipt_path)
            or existing_parent.get("receipt_sha256") != parent_receipt_hash
            or existing_parent.get("site_tree_sha256") != parent_site_binding.get("tree_sha256")
            or existing_parent.get("site_binding") != parent_site_binding
        ):
            raise DashboardDeltaError("existing delta receipt parent lineage is invalid")
        existing_plan = existing.get("plan_binding")
        if (
            not isinstance(existing_plan, Mapping)
            or existing_plan.get("ref") != _relative_run_ref(context, plan_path)
            or existing_plan.get("sha256") != plan_hash
            or existing_plan.get("admission_sha256") != metadata.plan_hash
            or existing_plan.get("generation_id") != metadata.generation_id
            or existing_plan.get("route") != route_manifest
        ):
            raise DashboardDeltaError("existing delta receipt plan binding is invalid")
        _validate_delta_output(
            context,
            receipt_path,
            existing,
            # The normalized in-memory view carries additive presentation
            # defaults for current validators, while the on-disk planless
            # receipt remains the canonical historical shape.  Validate that
            # raw shape on every retry/migration; it is not a permissive
            # placeholder compatibility path.
            legacy_raw_receipt=existing_raw if planless_legacy else None,
            legacy_raw_bytes=existing_raw_bytes if planless_legacy else None,
        )
        _validate_existing_delta_receipt(
            context,
            existing,
            receipt_path=receipt_path,
            final_root=final_root,
            fixture_rel=fixture_rel,
            map_rel=map_rel,
            registry_rel=registry_rel,
            site_rel=site_rel,
            parent_receipt_path=parent_receipt_path,
            parent_receipt=parent_receipt,
            parent_manifest_ref=parent_manifest_ref,
            parent_manifest_hash=parent_manifest_hash,
            parent_site_binding=parent_site_binding,
            parent_items=parent_items,
            new_input_items=new_input_items,
            request_binding=request_binding,
            plan=plan,
            plan_path=plan_path,
            plan_hash=plan_hash,
            metadata=metadata,
            route_manifest=route_manifest,
            projection_metadata=projection_metadata,
        )
        if v1_to_v2_migration:
            # The predecessor product manifest is an immutable old-target
            # binding.  Validate it against the old V1 receipt before the
            # replacement path can publish a V2 candidate.
            if not product_manifest_path.is_file() or product_manifest_path.is_symlink():
                raise DashboardDeltaError("existing V1 product manifest is missing")
            legacy_product = _read_json(context, child_manifest_ref, label="existing V1 product manifest")
            if legacy_product.get("manager_widget_ids") != predecessor_ids:
                raise DashboardDeltaError("existing V1 product manager binding is not the approved predecessor")
            _validate_product_manifest(
                context,
                product_manifest_path,
                legacy_product,
                existing,
                lifecycle,
                metadata,
                parent_manifest_ref or "",
                parent_manifest_hash or "",
            )
        _parent_output_root(context, parent_receipt_path, parent_receipt)
        if transaction_state is not None and transaction_state[1].get("schema_version") == _PRODUCT_TRANSACTION_SCHEMA:
            # v2 intents already bind a complete candidate generation tree,
            # including its product manifest.  Recovery only needs the
            # idempotent lifecycle reconciliation and final binding checks.
            with RunLifecycle._run_lock(context):  # noqa: SLF001 - recovery CAS boundary
                _finish_generation_product_transaction(
                    context,
                    intent_path=transaction_state[0],
                    intent=transaction_state[1],
                    receipt=existing,
                    receipt_path=receipt_path,
                    lifecycle=lifecycle,
                    metadata=metadata,
                    plan_path=plan_path,
                    plan_hash=plan_hash,
                    parent_manifest_ref=parent_manifest_ref,
                    parent_manifest_hash=parent_manifest_hash,
                    failpoint=failpoint,
                )
            # Keep the public result byte/shape-identical to the canonical
            # on-disk planless receipt.  ``existing`` is normalized only for
            # internal current-shape validation; returning that view would
            # make crash recovery differ from an exact retry by reintroducing
            # additive null/[] presentation fields.
            return dict(existing_raw if planless_legacy else existing)
        product_value = _read_json(context, child_manifest_ref, label="active generation product manifest") if product_manifest_path.is_file() else None
        transaction_needs_finish = False
        if transaction_state is not None:
            transaction_needs_finish = True
        if product_value is not None:
            live_status = _terminal_product_status(context, lifecycle)
            transaction_old_manifest_hash = (
                transaction_state[1].get("product_manifest", {}).get("old_sha256")
                if transaction_state is not None and isinstance(transaction_state[1].get("product_manifest"), Mapping)
                else None
            )
            current_manifest_hash = _sha256_bytes(product_manifest_path.read_bytes())
            # During recovery the old generation manifest is intentionally
            # still present while the new dashboard receipt is already live.
            # Validate it against the old receipt only after transaction
            # recovery has proven the candidate; the finish helper writes the
            # candidate-bound manifest under the same lifecycle lock.
            if transaction_state is not None and current_manifest_hash == transaction_old_manifest_hash:
                transaction_needs_finish = True
            elif product_value.get("status") != live_status:
                raise DashboardDeltaError("active generation product manifest does not match current terminal item status")
            elif not transaction_needs_finish:
                _validate_product_manifest(
                    context,
                    product_manifest_path,
                    product_value,
                    existing,
                    lifecycle,
                    metadata,
                    parent_manifest_ref or "",
                    parent_manifest_hash or "",
                )
        if product_value is None or transaction_needs_finish:
            # A crash after dashboard publication, during lifecycle
            # reconciliation, or before product-manifest publication is
            # recoverable without rewriting the candidate tree.
            if transaction_state is None:
                raise DashboardDeltaError("candidate product manifest is missing without a durable transaction intent")
            with RunLifecycle._run_lock(context):  # noqa: SLF001 - recovery CAS boundary
                product_value, transaction_state_value = _finish_generation_transaction(
                    context,
                    intent_path=transaction_state[0],
                    intent=transaction_state[1],
                    receipt=existing,
                    receipt_path=receipt_path,
                    product_manifest_path=product_manifest_path,
                    lifecycle=lifecycle,
                    metadata=metadata,
                    plan_path=plan_path,
                    plan_hash=plan_hash,
                    parent_manifest_ref=parent_manifest_ref,
                    parent_manifest_hash=parent_manifest_hash,
                    failpoint=failpoint,
                )
                transaction_state = (transaction_state[0], transaction_state_value)
        existing_receipt = dict(existing_raw if planless_legacy else existing)
        if transaction_state is not None:
            return existing_receipt

    # Render the complete candidate in a system-temp run first.  Creating a
    # sibling under ``products/generations`` before exact-retry comparison
    # changes that parent directory's mtime even when the candidate is
    # discarded.  The scratch run mirrors the canonical relative layout, so
    # all generated references already have their final run-relative values.
    # Only a differing candidate is copied into the same-filesystem
    # transaction staging namespace below the publication lock.
    replacement_candidate = final_root.exists() and product_manifest_path.is_file()
    scratch_workspace = tempfile.TemporaryDirectory(prefix="dashboard-delta-")
    scratch_run_root = Path(scratch_workspace.name)
    scratch_context = RunContext(run_id=context.run_id, run_root=scratch_run_root)
    scratch_generation_root = scratch_context.resolve_product_path(f"generations/{metadata.generation_id}")
    scratch_final_root = scratch_generation_root / "dashboard"
    if replacement_candidate:
        staging_root = scratch_generation_root
        _copy_generation_product(generation_root, staging_root)
        staging_dashboard_root = scratch_final_root
    else:
        staging_root = scratch_final_root
        staging_dashboard_root = scratch_final_root
        _copy_bound_parent(parent_root, parent_refs, staging_dashboard_root)
    if presentation_plan is not None:
        # The renderer validates the plan through its RunContext.  Copy only
        # the immutable plan input into scratch; no live run path is written.
        scratch_plan_path = scratch_context.resolve_run_path(resolved_presentation_plan_ref or "")
        scratch_plan_path.parent.mkdir(parents=True, exist_ok=True)
        presentation_plan_path = _safe_run_path(
            context,
            resolved_presentation_plan_ref or "",
            label="business presentation plan",
        )
        scratch_plan_path.write_bytes(presentation_plan_path.read_bytes())
    staging_prefix = _relative_run_ref(scratch_context, staging_dashboard_root)
    final_prefix = _relative_run_ref(context, final_root)
    staged_fixture_path = staging_dashboard_root / fixture_rel
    staged_map_path = staging_dashboard_root / map_rel
    staged_registry_path = staging_dashboard_root / registry_rel
    staged_site_path = staging_dashboard_root / site_rel
    staged_receipt_path = staging_dashboard_root / receipt_path.relative_to(final_root)
    staged_fixture_ref = _relative_run_ref(scratch_context, staged_fixture_path)
    staged_map_ref = _relative_run_ref(scratch_context, staged_map_path)
    staged_registry_ref = _relative_run_ref(scratch_context, staged_registry_path)
    # Renderer output/manifest refs are product-relative; fixture/map/registry
    # refs are run-relative because the renderer resolves those as run files.
    try:
        product_root = scratch_context.product_root
        staged_site_ref = staged_site_path.relative_to(product_root).as_posix()
        staged_manifest_ref = (staged_site_path / "site_manifest.json").relative_to(product_root).as_posix()
    except ValueError as exc:
        raise DashboardDeltaError("staged site path escaped the product root") from exc
    candidate_fixture["chart_registry_ref"] = staged_registry_ref
    candidate_fixture["chart_map_ref"] = staged_map_ref
    candidate_map["chart_registry_ref"] = staged_registry_ref
    candidate_map["fixture_ref"] = staged_fixture_ref
    _write_atomic_json(staged_fixture_path, candidate_fixture)
    _write_atomic_json(staged_map_path, candidate_map)
    _failpoint(failpoint, "before_dashboard_render")
    renderer_path = Path(__file__).with_name("dashboard_renderer.py")
    spec = importlib.util.spec_from_file_location("dashboard_renderer_for_delta", renderer_path)
    if spec is None or spec.loader is None:
        raise DashboardDeltaError("dashboard renderer cannot be loaded")
    renderer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = renderer
    spec.loader.exec_module(renderer)
    renderer.render_site_fixture(scratch_context, staged_fixture_ref, staged_site_ref, staged_manifest_ref)
    _replace_prefix(staging_dashboard_root, staging_prefix, final_prefix)
    final_fixture_path = staging_dashboard_root / fixture_rel
    final_map_path = staging_dashboard_root / map_rel
    candidate_fixture = json.loads(final_fixture_path.read_text(encoding="utf-8"))
    candidate_map = json.loads(final_map_path.read_text(encoding="utf-8"))
    if not isinstance(candidate_fixture, Mapping) or not isinstance(candidate_map, Mapping):
        raise DashboardDeltaError("candidate fixture/map became invalid after path normalization")
    _site_manifest_update(staging_dashboard_root / site_rel, staging_dashboard_root / map_rel)
    candidate_site_binding = _assembler()._site_tree_binding(staging_dashboard_root / site_rel)
    old_site_binding = parent_site_binding
    affected_paths: list[str] = []
    unchanged_paths: list[str] = []
    old_files = old_site_binding.get("files", {}) if isinstance(old_site_binding.get("files"), Mapping) else {}
    new_files = candidate_site_binding.get("files", {})
    for relative in sorted(set(old_files) | set(new_files)):
        if old_files.get(relative) == new_files.get(relative):
            unchanged_paths.append(relative)
        else:
            affected_paths.append(relative)
    # The staged tree was copied from the parent first.  Replace only changed
    # site bytes; this preserves old pages byte-for-byte even when the full
    # renderer candidate naturally emitted equivalent content.
    rendered_site = staging_dashboard_root / site_rel
    parent_site = parent_refs["site_ref"]
    for relative in unchanged_paths:
        source = parent_site / relative
        target = rendered_site / relative
        if source.is_file() and target.is_file() and source.read_bytes() != target.read_bytes():
            target.write_bytes(source.read_bytes())
    candidate_site_binding = _assembler()._site_tree_binding(rendered_site)
    _site_manifest_update(rendered_site, staging_dashboard_root / map_rel)
    candidate_site_binding = _assembler()._site_tree_binding(rendered_site)
    _fsync_tree(staging_root)
    _fsync_directory(staging_root.parent)

    # Materialize the complete child receipt in staging before the directory
    # rename.  If a failpoint fires immediately after publication, retry sees
    # a valid delta receipt (rather than the copied parent receipt) and can
    # finish only the generation product manifest.
    staged_fixture_final_ref = _relative_run_ref(context, final_root / fixture_rel)
    staged_map_final_ref = _relative_run_ref(context, final_root / map_rel)
    staged_registry_final_ref = _relative_run_ref(context, final_root / registry_rel)
    staged_site_final_ref = _relative_run_ref(context, final_root / site_rel)
    staged_receipt_final_ref = _relative_run_ref(context, receipt_path)
    receipt = {
        "schema_version": DELTA_SCHEMA,
        "status": "complete",
        "run_id": context.run_id,
        "generation_id": metadata.generation_id,
        "generation_ordinal": metadata.generation_ordinal,
        "parent_generation_id": metadata.parent_generation_id,
        "source_policy": "accepted_and_committed_only",
        "new_analytics": False,
        "parent": {
            "product_manifest_ref": parent_manifest_ref,
            "product_manifest_sha256": parent_manifest_hash,
            "receipt_ref": _relative_run_ref(context, parent_receipt_path),
            "receipt_sha256": parent_receipt_hash,
            "site_binding": parent_site_binding,
            "site_tree_sha256": parent_site_binding.get("tree_sha256"),
        },
        "request_binding": request_binding,
        "plan_binding": {
            "ref": _relative_run_ref(context, plan_path),
            "sha256": plan_hash,
            "admission_sha256": metadata.plan_hash,
            "generation_id": metadata.generation_id,
            "revision": plan.get("revision"),
            "route": route_manifest,
        },
        "input_items": [*parent_items, *new_input_items],
        "outputs": {"fixture_ref": staged_fixture_final_ref, "chart_map_ref": staged_map_final_ref, "chart_registry_ref": staged_registry_final_ref, "site_ref": staged_site_final_ref, "receipt_ref": staged_receipt_final_ref},
        "output_hashes": {"fixture_sha256": _sha256_bytes((staging_dashboard_root / fixture_rel).read_bytes()), "chart_map_sha256": _sha256_bytes((staging_dashboard_root / map_rel).read_bytes()), "chart_registry_sha256": _sha256_bytes((staging_dashboard_root / registry_rel).read_bytes()), "site_manifest_sha256": _sha256_bytes((staging_dashboard_root / site_rel / "site_manifest.json").read_bytes())},
        "site_binding": candidate_site_binding,
        "freeze_inputs": projection_metadata,
        "old_projection": {"projection_hash": parent_receipt.get("freeze_inputs", {}).get("projection_hash"), "export_sha256": parent_receipt.get("freeze_inputs", {}).get("export_sha256")},
        "new_projection": {"projection_hash": projection_metadata["projection_hash"], "export_sha256": projection_metadata["export_sha256"]},
        "affected_paths": [{"path": value, "sha256": candidate_site_binding["files"].get(value)} for value in affected_paths],
        "unchanged_paths": [{"path": value, "sha256": candidate_site_binding["files"].get(value)} for value in unchanged_paths],
        "rollback_parent": {
            "generation_id": metadata.parent_generation_id,
            "product_manifest_ref": parent_manifest_ref,
            "product_manifest_sha256": parent_manifest_hash,
            "receipt_ref": _relative_run_ref(context, parent_receipt_path),
            "receipt_sha256": parent_receipt_hash,
            "site_tree_sha256": parent_site_binding.get("tree_sha256"),
        },
        "widget_count": len(candidate_fixture.get("widgets", [])),
        "domain_count": len(candidate_fixture.get("domains", [])),
        "retry": "idempotent only when parent/input/route/projection/output hashes match",
        "presentation_plan_ref": resolved_presentation_plan_ref,
        "presentation_plan_sha256": presentation_plan_sha256,
        "manager_widget_ids": list(manager_widget_ids),
    }
    if presentation_plan is None:
        # Keep first publication planless receipts in the historical
        # canonical shape.  The explicit presentation fields are additive
        # contract data, not null/empty placeholders; omitting them is what
        # permits the one-time immutable legacy -> explicit-plan migration.
        for field in ("presentation_plan_ref", "presentation_plan_sha256", "manager_widget_ids"):
            receipt.pop(field, None)
            request_binding.pop(field, None)
        _validate_planless_legacy_receipt_shape(receipt)
    else:
        _validate_delta_receipt_shape(receipt)
    _write_atomic_json(staged_receipt_path, receipt)
    _fsync_tree(staging_root)
    _fsync_directory(staging_root.parent)
    candidate_dashboard_binding = _generation_product_binding(staging_root) if replacement_candidate else _transaction_tree_binding(staging_root)
    if not isinstance(candidate_dashboard_binding, Mapping):
        raise DashboardDeltaError("staged dashboard tree binding is invalid")

    # Keep the run lifecycle lock from the final active-generation check
    # through dashboard rename and product-manifest publication.  Requirement
    # admission uses this same lock, so a new generation cannot appear between
    # the child tree and its terminal manifest.
    with RunLifecycle._run_lock(context):  # noqa: SLF001 - publication CAS boundary
        latest = RunLifecycle._load_unlocked(context)  # noqa: SLF001
        latest_meta = latest.generation_metadata
        if latest_meta is None or latest_meta.generation_id != metadata.generation_id or latest_meta.manifest_hash != metadata.manifest_hash:
            raise _GenerationChanged("active generation changed before dashboard publication")
        latest_product_manifest_path = _active_product_manifest_path(context, latest)
        if latest_product_manifest_path != product_manifest_path:
            raise _GenerationChanged("active product manifest reference changed before dashboard publication")
        _assert_live_plan_binding(context, latest, plan_path, plan_hash)
        if presentation_plan is not None:
            if presentation_plan_bytes is None or resolved_presentation_plan_ref is None or presentation_plan_sha256 is None:
                raise DashboardDeltaError("business presentation plan binding is incomplete")
            _assert_live_presentation_plan_binding(
                context,
                resolved_presentation_plan_ref,
                presentation_plan_bytes,
                presentation_plan_sha256,
            )
            if existing_receipt is not None and not v1_to_v2_migration:
                existing_plan_ref = existing_receipt.get("presentation_plan_ref")
                existing_plan_hash = existing_receipt.get("presentation_plan_sha256")
                if existing_plan_ref not in (None, resolved_presentation_plan_ref) or existing_plan_hash not in (None, presentation_plan_sha256):
                    raise DashboardDeltaError("existing receipt presentation plan binding changed before publication")
        # Exact retries return the validated receipt without touching the
        # generation tree.  A changed renderer/product candidate for the same
        # generation is otherwise published as one staged namespace swap;
        # the parent generation, accepted answers, integration, LEM, and
        # source artifacts remain untouched.
        if existing_receipt is not None and existing_receipt == receipt:
            scratch_workspace.cleanup()
            return existing_receipt

        # The candidate differs, so (and only so) create the same-filesystem
        # sibling transaction staging namespace.  Copying from scratch keeps
        # all renderer work outside the live product tree while preserving the
        # existing v1/v2 transaction and rollback paths below.
        if replacement_candidate:
            # Reserve the explicit sibling name without creating it.  The
            # v2 intent below becomes the ownership record before any bytes
            # enter the live product filesystem.
            staging_root = generation_root.parent / f".{generation_root.name}.product.staging-{secrets.token_hex(10)}"
            if staging_root.exists() or staging_root.is_symlink():
                raise DashboardDeltaError("generation product staging path already exists")
            staging_dashboard_root = staging_root / "dashboard"
            candidate_dashboard_binding = _generation_product_binding(scratch_generation_root)
        else:
            staging_root = final_root.parent / f".{final_root.name}.staging"
            if staging_root.exists() or staging_root.is_symlink():
                raise DashboardDeltaError("generation dashboard staging path already exists")
            staging_dashboard_root = staging_root
            candidate_dashboard_binding = _transaction_tree_binding(scratch_final_root)
        if not isinstance(candidate_dashboard_binding, Mapping):
            raise DashboardDeltaError("scratch dashboard tree binding is invalid")
        staged_receipt_path = scratch_final_root / receipt_path.relative_to(final_root)
        new_receipt_hash = _sha256_bytes(staged_receipt_path.read_bytes())
        if replacement_candidate:
            # The candidate contains dashboard and product_manifest before any
            # target rename.  Lifecycle reconciliation is idempotent for an
            # already-published same-generation product and therefore does
            # not require a post-mutation state hash in the intent.
            receipt_for_product = dict(receipt)
            receipt_for_product["receipt_sha256"] = new_receipt_hash
            product_value = _new_product_manifest(
                context,
                latest,
                latest_meta,
                receipt_for_product,
                _relative_run_ref(context, receipt_path),
                parent_manifest_ref,
                parent_manifest_hash,
                _terminal_product_status(context, latest),
            )
            try:
                from auto_foundry_core.product_contracts import validate_product_manifest

                validate_product_manifest(product_value, require_all=True)
            except Exception as exc:
                raise DashboardDeltaError("delta product manifest freeze markers are not fully frozen") from exc
            try:
                manifest_relative = latest_product_manifest_path.relative_to(generation_root)
            except ValueError as exc:
                raise DashboardDeltaError("active product manifest escaped generation root") from exc
            staged_manifest_path = staging_root / manifest_relative
            staged_old_binding = candidate_dashboard_binding
            expected_files = dict(staged_old_binding.get("files", {}))
            expected_files[manifest_relative.as_posix()] = _json_hash(product_value)
            candidate_product_binding = {
                "files": expected_files,
                "tree_sha256": _json_hash(expected_files),
                "file_count": len(expected_files),
            }
            transaction_path, transaction_intent = _prepare_generation_product_transaction(
                context,
                metadata=latest_meta,
                target_root=generation_root,
                candidate_root=staging_root,
                receipt_path=receipt_path,
                product_manifest_path=latest_product_manifest_path,
                old_receipt_hash=(
                    _sha256_bytes(receipt_path.read_bytes())
                    if receipt_path.is_file() and not receipt_path.is_symlink()
                    else None
                ),
                new_receipt_hash=new_receipt_hash,
                new_binding=candidate_product_binding,
                candidate_binding=staged_old_binding,
                preparing=True,
            )
            try:
                _failpoint(failpoint, "before_product_copy")
                _copy_generation_product(scratch_generation_root, staging_root)
                if _generation_product_binding(staging_root) != staged_old_binding:
                    raise DashboardDeltaError("copied generation product binding differs from scratch")
                _failpoint(failpoint, "after_product_copy_before_prepared")
                transaction_intent = _product_transaction_phase(transaction_path, transaction_intent, "prepared")
            except Exception:
                try:
                    if staging_root.exists() or staging_root.is_symlink():
                        _product_transaction_remove(staging_root)
                    if transaction_path.exists() or transaction_path.is_symlink():
                        _product_transaction_remove(transaction_path)
                    _fsync_directory(staging_root.parent)
                finally:
                    scratch_workspace.cleanup()
                raise
            # The durable intent exists before this failpoint.  If the
            # manifest write is interrupted, recovery sees target+candidate
            # old bytes and safely removes only the intent-bound candidate.
            _failpoint(failpoint, "before_product_manifest")
            _write_atomic_json(staged_manifest_path, product_value)
            _fsync_tree(staging_root)
            if not _product_transaction_binding_matches(staging_root, candidate_product_binding):
                raise DashboardDeltaError("staged generation product manifest changed candidate binding")
            _failpoint(failpoint, "after_manifest_write")
            transaction_path, transaction_intent = _publish_generation_product_transaction(
                context,
                intent_path=transaction_path,
                intent=transaction_intent,
                failpoint=failpoint,
            )
            _finish_generation_product_transaction(
                context,
                intent_path=transaction_path,
                intent=transaction_intent,
                receipt=receipt,
                receipt_path=receipt_path,
                lifecycle=latest,
                metadata=latest_meta,
                plan_path=plan_path,
                plan_hash=plan_hash,
                parent_manifest_ref=parent_manifest_ref,
                parent_manifest_hash=parent_manifest_hash,
                failpoint=failpoint,
            )
            scratch_workspace.cleanup()
        else:
            # First publication has no pre-existing generation product to
            # preserve.  Record ownership before the same-filesystem copy so
            # a process death cannot strand an untracked `.dashboard.staging`.
            transaction_intent = _prepare_generation_transaction(
                context,
                metadata=latest_meta,
                final_root=final_root,
                receipt_path=receipt_path,
                product_manifest_path=latest_product_manifest_path,
                lifecycle=latest,
                new_dashboard_binding=candidate_dashboard_binding,
                new_receipt_hash=new_receipt_hash,
                staging_root=staging_root,
                preparing=True,
            )
            transaction_path = _transaction_path(context, latest_meta.generation_id)
            try:
                _failpoint(failpoint, "before_dashboard_copy")
                shutil.copytree(scratch_final_root, staging_root, symlinks=False)
                if _transaction_tree_binding(staging_root) != candidate_dashboard_binding:
                    raise DashboardDeltaError("copied dashboard binding differs from scratch")
                _failpoint(failpoint, "after_dashboard_copy_before_prepared")
                transaction_intent = _transaction_phase(transaction_path, transaction_intent, "prepared")
            except Exception:
                try:
                    if staging_root.exists() or staging_root.is_symlink():
                        _remove_transaction_path(staging_root)
                    if transaction_path.exists() or transaction_path.is_symlink():
                        _remove_transaction_path(transaction_path)
                    _fsync_directory(staging_root.parent)
                finally:
                    scratch_workspace.cleanup()
                raise
            scratch_workspace.cleanup()
            _failpoint(failpoint, "before_dashboard_publish")
            final_root.parent.mkdir(parents=True, exist_ok=True)
            _assembler()._publish_staged_output(staging_root, final_root, retain_backup=True)
            _fsync_directory(final_root.parent)
            _fsync_directory(final_root)
            _failpoint(failpoint, "after_dashboard_publish")
            transaction_intent = _transaction_phase(
                transaction_path,
                transaction_intent,
                "dashboard_published",
            )
            _finish_generation_transaction(
                context,
                intent_path=transaction_path,
                intent=transaction_intent,
                receipt=receipt,
                receipt_path=receipt_path,
                product_manifest_path=latest_product_manifest_path,
                lifecycle=latest,
                metadata=latest_meta,
                plan_path=plan_path,
                plan_hash=plan_hash,
                parent_manifest_ref=parent_manifest_ref,
                parent_manifest_hash=parent_manifest_hash,
                failpoint=failpoint,
            )
        # A newly published child may be the first durable holder of its
        # parent product-manifest hash.  Finalize a deferred immediate-parent
        # transaction now, still under the lifecycle lock and without scanning
        # unrelated generations.
        _reconcile_immediate_parent_transaction_locked(context, latest)
    return receipt


def assemble_dashboard_delta(
    context: RunContext,
    *,
    parent_receipt_ref: str | Path | None = None,
    route: Mapping[str, Any],
    output_dir: str | Path | None = None,
    failpoint: str | None = None,
    presentation_plan_ref: str | Path | None = None,
) -> dict[str, Any]:
    """Serialize one active-generation delta before any staging/publish work.

    The pointer is reloaded after acquiring the generation lock.  If
    RequirementRunExtension advanced the run meanwhile, release the stale
    lock and retry against the newly active generation instead of staging a
    child under the wrong lineage.
    """

    if not isinstance(context, RunContext):
        raise TypeError("assemble_dashboard_delta requires a RunContext")
    for _attempt in range(8):
        # Startup recovery owns the lifecycle lock while it reloads the
        # authoritative pointer and inspects only the immediate parent's
        # external v2 intent.  No nested run-lock acquisition is attempted;
        # the generation lock is acquired only after this block releases.
        try:
            with RunLifecycle._run_lock(context):  # noqa: SLF001 - startup recovery CAS boundary
                lifecycle = RunLifecycle._load_unlocked(context)  # noqa: SLF001
                metadata = lifecycle.generation_metadata
                if metadata is None:
                    raise DashboardDeltaError("dashboard delta requires an active appended generation")
                _reconcile_immediate_parent_transaction_locked(context, lifecycle)
                expected_generation = (metadata.generation_id, metadata.manifest_hash)
        except Exception as exc:
            if isinstance(exc, DashboardDeltaError):
                raise
            raise DashboardDeltaError("active lifecycle cannot be loaded") from exc
        try:
            with _generation_lock(context, metadata.generation_id):
                latest = RunLifecycle.load(context)
                latest_meta = latest.generation_metadata
                if latest_meta is None or (latest_meta.generation_id, latest_meta.manifest_hash) != expected_generation:
                    # The pointer may advance in the small window between
                    # this wrapper reload and the locked assembly entry.  If
                    # the stale generation left a v2 intent, reconcile that
                    # intent now under the same lifecycle lock used by
                    # admission/publication; otherwise retrying on the new
                    # generation would strand the old transaction siblings.
                    with RunLifecycle._run_lock(context):  # noqa: SLF001 - stale recovery CAS boundary
                        authoritative = RunLifecycle._load_unlocked(context)  # noqa: SLF001
                        authoritative_meta = authoritative.generation_metadata
                        if authoritative_meta is not None and authoritative_meta.generation_id != metadata.generation_id:
                            _load_plan(context, authoritative)
                            _parent_generation_bindings(context, authoritative_meta)
                            stale_root = _safe_product_path(
                                context,
                                f"generations/{metadata.generation_id}",
                                label="stale generation product namespace",
                            )
                            _abort_inactive_generation_product_transaction(
                                context,
                                latest=authoritative,
                                transaction_generation_id=metadata.generation_id,
                                target_root=stale_root,
                                defer_unbound_new=True,
                            )
                    continue
                return _assemble_dashboard_delta_locked(
                    context,
                    parent_receipt_ref=parent_receipt_ref,
                    route=route,
                    output_dir=output_dir,
                    failpoint=failpoint,
                    presentation_plan_ref=presentation_plan_ref,
                    expected_generation=expected_generation,
                )
        except _GenerationChanged:
            continue
    raise DashboardDeltaError("active generation kept changing during dashboard delta assembly")


def _terminal_product_status(context: RunContext, lifecycle: RunLifecycle) -> str:
    """Derive the non-analytic terminal product status from terminal item facts."""

    limited = False
    for item_id in lifecycle.item_ids:
        path = _safe_run_path(context, f"requirements/{item_id}/item_state.json", label=f"{item_id} item state")
        if not path.is_file() or path.is_symlink():
            raise DashboardDeltaError(f"item state is missing: {item_id}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DashboardDeltaError(f"item state is invalid: {item_id}") from exc
        if not isinstance(value, Mapping) or value.get("integration_state") != "integrated":
            raise DashboardDeltaError(f"item is not integrated: {item_id}")
        terminal = value.get("terminal_outcome") if isinstance(value, Mapping) else None
        outcome = terminal.get("outcome") if isinstance(terminal, Mapping) else value.get("lifecycle_state") if isinstance(value, Mapping) else None
        if outcome not in {"accepted", "accepted_with_limits", "technical_failure", "blocked_by_evidence"}:
            raise DashboardDeltaError(f"item is not terminal: {item_id}")
        if outcome in {"accepted_with_limits", "technical_failure", "blocked_by_evidence"}:
            limited = True
    return "complete_with_limits" if limited else "complete"


def _write_atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_prefix(root: Path, old: str, new: str) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if old.encode("utf-8") in data:
            path.write_bytes(data.replace(old.encode("utf-8"), new.encode("utf-8")))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--parent-receipt", required=False)
    parser.add_argument("--route", type=Path, required=True, help="run-relative JSON route object")
    parser.add_argument("--output-dir", required=False)
    parser.add_argument("--failpoint", required=False)
    parser.add_argument("--presentation-plan-ref", required=False, help="run-relative business presentation plan")
    args = parser.parse_args(argv)
    try:
        context = RunContext(run_id=args.run_id, run_root=args.run_root)
        route = _read_json(context, args.route, label="dashboard delta route")
        result = assemble_dashboard_delta(context, parent_receipt_ref=args.parent_receipt, route=route, output_dir=args.output_dir, failpoint=args.failpoint, presentation_plan_ref=args.presentation_plan_ref)
    except (OSError, ValueError, DashboardDeltaError, AllowedRootError, RuntimeError) as exc:
        print(f"dashboard delta assembler: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


__all__ = ["DELTA_SCHEMA", "DashboardDeltaError", "assemble_dashboard_delta", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
