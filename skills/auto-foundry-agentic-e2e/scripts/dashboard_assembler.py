#!/usr/bin/env python3
"""Deterministically assemble an offline V4 dashboard from frozen run facts.

This module is the Product Agent's presentation boundary.  It consumes only
accepted answer bundles, committed integration records, frozen planning/LEM
metadata, and the renderer's local assets.  It never reads raw sources,
``work``/calculation artifacts, calls a model, or mutates an existing product.
"""

from __future__ import annotations

import argparse
import copy
import csv
from contextlib import contextmanager
import fcntl
import hashlib
import io
import importlib.util
import json
import math
import os
import posixpath
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from auto_foundry_core.workspace import AllowedRootError, RunContext
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    _SRC = Path(__file__).resolve().parents[3] / "src"
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    from auto_foundry_core.workspace import AllowedRootError, RunContext


ASSEMBLER_SCHEMA = "dashboard.assembler_receipt.v1"
FIXTURE_SCHEMA = "dashboard.reviewed_fixture.v4"
CHART_MAP_SCHEMA = "dashboard.chart_map.v4"
REGISTRY_SCHEMA = "dashboard.chart_registry.v1"
RENDERING_IDENTITY_SCHEMA = "dashboard.rendering_identity.v1"
SKILL_NAME = "auto-foundry-agentic-e2e"
CORE_NAME = "auto_foundry_core"
PRESENTATION_PLAN_V2_SCHEMA = "dashboard.business_presentation_plan.v2"
PRESENTATION_PLAN_FILENAME = "business_presentation_plan.json"
BLUEPRINT_FILENAME = "dashboard_blueprint_v2.json"


def _dashboard_runtime() -> Any:
    """Load the single portable blueprint/runtime helper from this skill."""

    module_name = "auto_foundry_dashboard_runtime"
    loaded = sys.modules.get(module_name)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().with_name("dashboard_runtime.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssemblyError("dashboard runtime cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module

# Every presentation plan carries the same complete accepted / committed input
# projection.  The analytical-artifact list is part of the immutable binding,
# rather than an optional receipt-only extension: a plan must describe exactly
# the records that the canonical assembler will use.
_PRESENTATION_INPUT_KEYS = frozenset(
    {
        "item_id",
        "accepted_content_hash",
        "accepted_manifest_hash",
        "integration_manifest_hash",
        "record_count",
        "analytical_artifacts",
    }
)
_PRESENTATION_ARTIFACT_KEYS = frozenset(
    {
        "item_id",
        "artifact_id",
        "artifact_type",
        "schema_version",
        "requirement_id",
        "content_hash",
        "envelope_hash",
        "canonical_bytes_sha256",
        "artifact_ref",
        "integration_record_id",
        "integration_record_hash",
    }
)

class AssemblyError(ValueError):
    """Raised when frozen presentation inputs are incomplete or inconsistent."""


class BusinessPresentationPlanError(AssemblyError):
    """Raised when an explicit manager presentation plan is not admissible."""


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _blueprint_fixture_digest(fixture: Mapping[str, Any]) -> str:
    """Hash fixture semantics without the non-semantic blueprint backpointer."""

    payload = {
        key: copy.deepcopy(value)
        for key, value in fixture.items()
        if key not in {"blueprint_ref", "blueprint_sha256"}
    }
    return _sha256_bytes(_canonical_bytes(payload))


def _slug(value: Any) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", _text(value, "item")).strip("-")
    return slug or "item"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _read_json(context: RunContext, reference: str | Path, *, label: str) -> Mapping[str, Any]:
    """Read one run-relative JSON object after containment validation."""

    path = context.resolve_run_path(reference)
    if not path.is_file() or path.is_symlink():
        raise AssemblyError(f"{label} is missing or symlinked: {reference}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"{label} is invalid: {reference}") from exc
    if not isinstance(value, Mapping):
        raise AssemblyError(f"{label} must be a JSON object: {reference}")
    return value


def _active_generation_id(context: RunContext) -> str:
    """Read the active generation identifier without requiring a live call."""

    for reference in ("active_generation.json", "run_state.json"):
        path = context.run_root / reference
        if not path.is_file() or path.is_symlink():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        candidate = payload.get("generation_id")
        generation = payload.get("generation")
        if candidate is None and isinstance(generation, Mapping):
            candidate = generation.get("generation_id") or generation.get("id")
        if candidate is None and isinstance(generation, str):
            candidate = generation
        value = _text(candidate).strip()
        if value and re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            return value
    return ""


def _load_legacy_chart_hints(context: RunContext) -> dict[str, Mapping[str, Any]]:
    """Load the active generation's prior dashboard chart projections, if any.

    A same-generation rebuild may be asked to republish manager presentation
    fields while preserving already-reviewed chart geometry.  The prior
    fixture is itself a frozen product input; no source/work data is read and
    a missing prior fixture simply means there are no hints.
    """

    generation_id = _active_generation_id(context)
    if not generation_id:
        return {}
    # A cumulative rebuild must inherit geometry from the immutable immediate
    # parent, never from a stale/current child fixture.  The active-generation
    # fixture may already contain the same new dashboard facts with an earlier
    # projection and would otherwise overwrite the freshly bound geometry.
    manifest_path = context.run_root / "extensions" / generation_id / "generation_manifest.json"
    try:
        generation_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() and not manifest_path.is_symlink() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        generation_manifest = {}
    parent_generation_id = _text(generation_manifest.get("parent_generation_id")).strip() if isinstance(generation_manifest, Mapping) else ""
    generation_id = parent_generation_id or generation_id
    reference = Path("products") / "generations" / generation_id / "dashboard" / "dashboard_fixture_v4.json"
    path = context.run_root / reference
    try:
        path.relative_to(context.run_root)
    except ValueError:
        return {}
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    raw_widgets = fixture.get("widgets") if isinstance(fixture, Mapping) else None
    if not isinstance(raw_widgets, list):
        return {}
    return {
        _text(widget.get("id")): widget
        for widget in raw_widgets
        if isinstance(widget, Mapping) and _text(widget.get("id")).strip()
    }


def _load_legacy_chart_map_hints(context: RunContext) -> dict[str, Mapping[str, Any]]:
    """Load immutable parent chart-map entries for cumulative rebuilds.

    The chart map is part of the reviewed presentation envelope: rebuilding a
    child generation must not add current-generation presentation metadata to
    inherited entries or silently change their bound geometry.  New records
    are still projected by the current assembler, while IDs present in the
    immediate parent's map are copied byte-for-byte into the candidate map.
    """

    generation_id = _active_generation_id(context)
    if not generation_id:
        return {}
    manifest_path = context.run_root / "extensions" / generation_id / "generation_manifest.json"
    try:
        generation_manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.is_file() and not manifest_path.is_symlink()
            else {}
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        generation_manifest = {}
    parent_generation_id = (
        _text(generation_manifest.get("parent_generation_id")).strip()
        if isinstance(generation_manifest, Mapping)
        else ""
    )
    generation_id = parent_generation_id or generation_id
    reference = (
        Path("products")
        / "generations"
        / generation_id
        / "dashboard"
        / "dashboard_chart_map_v4.json"
    )
    path = context.run_root / reference
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        chart_map = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    raw_charts = chart_map.get("charts") if isinstance(chart_map, Mapping) else None
    if not isinstance(raw_charts, list):
        return {}
    return {
        _text(chart.get("id")): chart
        for chart in raw_charts
        if isinstance(chart, Mapping) and _text(chart.get("id")).strip()
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(_canonical_bytes(value))


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _validate_presentation_input_binding(item: Mapping[str, Any], *, label: str) -> None:
    """Validate one canonical plan input binding.

    The nested artifact provenance is deliberately carried in the public plan
    so the plan, receipt, and assembler all bind the same accepted analytical
    output.  There is one shape: omitting the list is not an alternate legacy
    form, and no field is inferred at assembly time.
    """

    if not isinstance(item, Mapping) or set(item) != _PRESENTATION_INPUT_KEYS:
        raise BusinessPresentationPlanError(f"{label} input binding is invalid")
    item_id = item.get("item_id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise BusinessPresentationPlanError(f"{label} input binding item_id is invalid")
    if any(not _is_sha256(item.get(key)) for key in (
        "accepted_content_hash", "accepted_manifest_hash", "integration_manifest_hash",
    )):
        raise BusinessPresentationPlanError(f"{label} input hash is invalid")
    record_count = item.get("record_count")
    if isinstance(record_count, bool) or not isinstance(record_count, int) or record_count < 0:
        raise BusinessPresentationPlanError(f"{label} input record_count is invalid")
    artifacts = item.get("analytical_artifacts")
    if not isinstance(artifacts, list):
        raise BusinessPresentationPlanError(f"{label} input analytical_artifacts must be a list")
    seen_ids: set[str] = set()
    seen_records: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or set(artifact) != _PRESENTATION_ARTIFACT_KEYS:
            raise BusinessPresentationPlanError(f"{label} analytical artifact binding is invalid")
        if artifact.get("item_id") != item_id:
            raise BusinessPresentationPlanError(f"{label} analytical artifact item binding is invalid")
        for key in (
            "artifact_id", "artifact_type", "schema_version", "requirement_id",
            "artifact_ref", "integration_record_id",
        ):
            value = artifact.get(key)
            if not isinstance(value, str) or not value.strip():
                raise BusinessPresentationPlanError(f"{label} analytical artifact identity is invalid")
        if artifact.get("requirement_id") != item_id:
            raise BusinessPresentationPlanError(f"{label} analytical artifact requirement binding is invalid")
        if any(not _is_sha256(artifact.get(key)) for key in (
            "content_hash", "envelope_hash", "canonical_bytes_sha256", "integration_record_hash",
        )):
            raise BusinessPresentationPlanError(f"{label} analytical artifact hash is invalid")
        artifact_ref = artifact["artifact_ref"]
        if (
            not artifact_ref.startswith("integration/committed/artifacts/")
            or "\\" in artifact_ref
            or "\x00" in artifact_ref
            or Path(artifact_ref).is_absolute()
            or ".." in Path(artifact_ref).parts
        ):
            raise BusinessPresentationPlanError(f"{label} analytical artifact reference is invalid")
        artifact_id = artifact["artifact_id"]
        record_id = artifact["integration_record_id"]
        if artifact_id in seen_ids or record_id in seen_records:
            raise BusinessPresentationPlanError(f"{label} analytical artifact bindings are duplicated")
        seen_ids.add(artifact_id)
        seen_records.add(record_id)


def _json_hash(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _preflight_input_fingerprint(
    input_items: Sequence[Mapping[str, Any]],
    rendering_identity: Mapping[str, Any],
) -> str:
    """Bind preflight cache identity to both frozen inputs and renderer code.

    The fixture/chart bytes are a source inventory rather than a published
    product.  Reusing them is nevertheless a semantic cache hit: a renderer
    repair must invalidate an older inventory even when accepted inputs are
    unchanged.  Keep the binding deterministic and source-bound while leaving
    final product input hashes unchanged.
    """

    if not isinstance(rendering_identity, Mapping):
        raise AssemblyError("preflight rendering identity is invalid")
    return _json_hash({
        "input_items": copy.deepcopy(list(input_items)),
        "rendering_identity": copy.deepcopy(dict(rendering_identity)),
    })


def _rendering_identity(context: RunContext) -> dict[str, Any]:
    """Return the exact source/release identity used by dashboard rendering.

    The root product is reusable only when the same renderer release is still
    loaded.  Version strings alone cannot detect a repaired renderer shipped
    under the same release pair, so bind the skill's canonical source tree and
    the core implementation identity already tracked by the lifecycle module.
    The tree is read-only and contains only the local skill package; no run
    data or generated product bytes participate in this identity.
    """

    if not isinstance(context, RunContext):
        raise TypeError("dashboard rendering identity requires a RunContext")
    skill_root = Path(__file__).resolve().parents[1]
    files: dict[str, str] = {}
    for path in sorted(skill_root.rglob("*"), key=lambda value: value.relative_to(skill_root).as_posix()):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"} or path.name == ".DS_Store":
            continue
        relative = path.relative_to(skill_root).as_posix()
        if path.is_symlink():
            raise AssemblyError(f"dashboard skill source contains a symlink: {relative}")
        if path.is_file():
            files[relative] = _sha256_bytes(path.read_bytes())
    if not files:
        raise AssemblyError("dashboard skill source manifest is empty")
    try:
        from auto_foundry_core.lifecycle import current_implementation_identity

        core_sha, core_tree = current_implementation_identity(context)
    except Exception as exc:
        raise AssemblyError("dashboard core implementation identity is unavailable") from exc
    return {
        "schema_version": RENDERING_IDENTITY_SCHEMA,
        "skill_name": SKILL_NAME,
        "skill_version": context.skill_version or "0.8.0",
        "core_name": CORE_NAME,
        "core_version": str(context.core_version),
        "skill_tree_sha256": _json_hash(files),
        "skill_file_count": len(files),
        "core_implementation_sha": core_sha,
        "core_implementation_tree": core_tree,
    }


def _safe_record_ref(value: Any) -> str:
    ref = _text(value).strip()
    if not ref or "\x00" in ref or "\\" in ref:
        raise AssemblyError("integration reference is invalid")
    return ref


def _read_bytes(context: RunContext, reference: str | Path, *, label: str) -> tuple[Path, bytes]:
    path = context.resolve_run_path(reference)
    if not path.is_file() or path.is_symlink():
        raise AssemblyError(f"{label} is missing or symlinked: {reference}")
    try:
        return path, path.read_bytes()
    except OSError as exc:
        raise AssemblyError(f"{label} cannot be read: {reference}") from exc


def _assert_no_symlink_chain(context: RunContext, reference: str | Path, *, label: str) -> None:
    """Reject lexical symlinks in a committed reference's parent chain.

    ``RunContext.resolve_run_path`` deliberately resolves links to enforce
    containment.  Product provenance additionally requires the committed
    namespace itself to be symlink-free, including parent directories, so a
    link that happens to stay inside the run cannot replace the immutable
    artifact boundary.
    """

    raw = Path(reference)
    if raw.is_absolute():
        try:
            relative = raw.relative_to(context.run_root)
        except ValueError as exc:
            raise AssemblyError(f"{label} is outside the run root: {reference}") from exc
    else:
        relative = raw
    current = context.run_root
    for component in relative.parts:
        if component in {"", "."}:
            continue
        current = current / component
        if current.is_symlink():
            raise AssemblyError(f"{label} is symlinked: {reference}")


def _validate_product_asset_reference(reference: str) -> str:
    """Validate a frozen product asset ref before any filesystem open.

    Product manifests may mention old source/work artifacts for audit history,
    but the presentation assembler must never probe those paths.  Reject
    absolute paths, escapes, and known raw/calculation/data-room components at
    the reference boundary so a bad ref cannot reach ``_read_bytes``.
    """

    value = _text(reference).strip()
    if not value or "\x00" in value or "\\" in value:
        raise AssemblyError("forbidden product asset reference")
    raw = Path(value)
    if raw.is_absolute() or not raw.parts:
        raise AssemblyError(f"forbidden product asset reference: {value}")
    forbidden = {"work", "calculations", "data-room", "source", "sources", "raw", "data", "dataset", "datasets"}
    for part in raw.parts:
        normalized = part.strip().lower().replace("_", "-")
        if normalized in {".", ".."} or normalized in forbidden or normalized.startswith(("source-", "raw-", "calculation-")):
            raise AssemblyError(f"forbidden product asset reference: {value}")
    return value


def _load_public_accepted_bundle(context: RunContext, item_id: str) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    """Load an accepted bundle through core's immutable public validator.

    Constructing an ``ItemWorkspace`` directly avoids ``ItemWorkspace.load``
    telemetry/state reconciliation side effects during a product-only build.
    ``AcceptedAnalysisBundle.load`` still owns the exact accepted manifest,
    envelope, content, and terminal-intent/hash checks.
    """

    try:
        from auto_foundry_core.durable import ItemWorkspace
        from auto_foundry_core.integration import AcceptedAnalysisBundle
    except ModuleNotFoundError as exc:  # pragma: no cover - package install guard
        raise AssemblyError("auto_foundry_core public accepted-bundle types are unavailable") from exc
    state_ref = f"requirements/{item_id}/item_state.json"
    state = _read_json(context, state_ref, label=f"{item_id} item state")
    if state.get("item_id") != item_id or state.get("mode") != "requirement":
        raise AssemblyError(f"{item_id} item state identity/mode mismatch")
    if state.get("lifecycle_state") not in {"accepted", "integrated", "complete"}:
        raise AssemblyError(f"{item_id} is not terminally accepted")
    if state.get("integration_state") not in {"integrated", "technical_failure"}:
        raise AssemblyError(f"{item_id} has no settled integration boundary")
    workspace = ItemWorkspace(
        context,
        item_id,
        mode="requirement",
        original_text=_text(state.get("original_text")),
        telemetry=None,
        state=state,
    )
    try:
        bundle = AcceptedAnalysisBundle.load(workspace)
    except Exception as exc:
        raise AssemblyError(f"{item_id} accepted bundle validation failed") from exc
    try:
        content = json.loads(bundle.answer_content_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"{item_id} accepted answer content is not JSON") from exc
    if not isinstance(content, Mapping):
        raise AssemblyError(f"{item_id} accepted answer content must be an object")
    manifest = dict(bundle.manifest)
    envelope = dict(bundle.acceptance_envelope)
    terminal = state.get("terminal_intent")
    if not isinstance(terminal, Mapping) or terminal.get("manifest_hash") != bundle.manifest_hash:
        raise AssemblyError(f"{item_id} terminal intent does not bind accepted manifest")
    if terminal.get("outcome") != bundle.outcome:
        raise AssemblyError(f"{item_id} terminal intent outcome mismatch")
    return content, manifest, {"state": state, "envelope": envelope, "bundle": bundle}


def _load_technical_failure_manifest(
    context: RunContext,
    item_id: str,
    accepted_meta: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate one explicit exhausted integration-failure boundary."""

    reference = f"requirements/{item_id}/integration/technical_failure/manifest.json"
    _path, payload = _read_bytes(context, reference, label=f"{item_id} technical failure manifest")
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"{item_id} technical failure manifest is invalid") from exc
    required = {
        "schema_version", "session_id", "item_id", "owner_id", "status",
        "accepted_content_hash", "reason", "created_at", "recovery_exhausted", "manifest_hash",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != required:
        raise AssemblyError(f"{item_id} technical failure manifest fields are invalid")
    if manifest.get("schema_version") != "1" or manifest.get("status") != "technical_failure" or manifest.get("recovery_exhausted") is not True:
        raise AssemblyError(f"{item_id} technical failure manifest is not an exhausted boundary")
    bundle = accepted_meta.get("bundle")
    if manifest.get("item_id") != item_id or manifest.get("accepted_content_hash") != getattr(bundle, "content_hash", None):
        raise AssemblyError(f"{item_id} technical failure accepted hash binding mismatch")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if manifest.get("manifest_hash") != _json_hash(unsigned):
        raise AssemblyError(f"{item_id} technical failure manifest hash mismatch")
    state = accepted_meta.get("state")
    if not isinstance(state, Mapping) or state.get("integration_state") != "technical_failure" or state.get("integration_manifest_hash") != manifest.get("manifest_hash") or state.get("integration_manifest_ref") != "integration/technical_failure/manifest.json":
        raise AssemblyError(f"{item_id} technical failure item binding is stale")
    return dict(manifest)


def _load_item_technical_failure_manifest(
    context: RunContext,
    item_id: str,
    state: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate a pre-acceptance item terminal-failure snapshot."""

    reference = f"requirements/{item_id}/accepted/manifest.json"
    _path, payload = _read_bytes(context, reference, label=f"{item_id} terminal failure manifest")
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"{item_id} terminal failure manifest is invalid") from exc
    required = {
        "item_id", "outcome", "reason", "recovery_exhausted", "hashes",
        "artifact_progress", "refs", "content_hash", "manifest_hash",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != required:
        raise AssemblyError(f"{item_id} terminal failure manifest fields are invalid")
    if manifest.get("item_id") != item_id or manifest.get("outcome") != "technical_failure" or manifest.get("recovery_exhausted") is not True:
        raise AssemblyError(f"{item_id} terminal failure manifest is not exhausted")
    content_hash = manifest.get("content_hash")
    if not _is_sha256(content_hash):
        raise AssemblyError(f"{item_id} terminal failure content hash is invalid")
    unsigned_content = {
        key: value
        for key, value in manifest.items()
        if key not in {"content_hash", "manifest_hash"}
    }
    if content_hash != _json_hash(unsigned_content):
        raise AssemblyError(f"{item_id} terminal failure content hash mismatch")
    progress = manifest.get("artifact_progress")
    if not isinstance(progress, Mapping) or set(progress) != {
        "files", "hashes", "finding_count", "source_map_count", "script_count", "draft_count", "handoff_present"
    }:
        raise AssemblyError(f"{item_id} terminal failure artifact progress is invalid")
    files = progress.get("files")
    hashes = progress.get("hashes")
    if not isinstance(files, list) or files != sorted(set(files)) or any(
        not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts
        for path in files
    ):
        raise AssemblyError(f"{item_id} terminal failure artifact progress files are invalid")
    if not isinstance(hashes, Mapping) or set(hashes) != set(files) or any(not _is_sha256(value) for value in hashes.values()):
        raise AssemblyError(f"{item_id} terminal failure artifact progress hashes are invalid")
    if manifest.get("hashes") != dict(hashes):
        raise AssemblyError(f"{item_id} terminal failure hashes are inconsistent")
    for field in ("finding_count", "source_map_count", "script_count", "draft_count"):
        value = progress.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AssemblyError(f"{item_id} terminal failure artifact progress counts are invalid")
    if not isinstance(progress.get("handoff_present"), bool):
        raise AssemblyError(f"{item_id} terminal failure artifact progress handoff is invalid")
    refs = manifest.get("refs")
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
        raise AssemblyError(f"{item_id} terminal failure refs are invalid")
    terminal = state.get("terminal_outcome")
    if state.get("lifecycle_state") != "technical_failure" or not isinstance(terminal, Mapping) or terminal.get("outcome") != "technical_failure":
        raise AssemblyError(f"{item_id} terminal failure item binding is stale")
    if state.get("integration_state") != "pending" or state.get("integration_manifest_hash") is not None or state.get("integration_manifest_ref") is not None:
        raise AssemblyError(f"{item_id} terminal failure integration binding is stale")
    integration_root = context.resolve_run_path(f"requirements/{item_id}/integration")
    if integration_root.is_symlink() or (integration_root.exists() and not integration_root.is_dir()):
        raise AssemblyError(f"{item_id} terminal failure integration root is invalid")
    for leaf in ("committed", "technical_failure", "staging"):
        residue = integration_root / leaf
        if residue.exists() or residue.is_symlink():
            raise AssemblyError(f"{item_id} terminal failure has integration residue")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if manifest.get("manifest_hash") != _json_hash(unsigned):
        raise AssemblyError(f"{item_id} terminal failure manifest hash mismatch")
    terminal_ref = terminal.get("manifest_path") if isinstance(terminal, Mapping) else None
    terminal_path_matches = (
        isinstance(terminal_ref, str)
        and (
            terminal_ref == reference
            or (Path(terminal_ref).is_absolute() and Path(terminal_ref).resolve(strict=False) == _path.resolve(strict=False))
        )
    )
    if (
        not isinstance(terminal, Mapping)
        or terminal.get("status") != "technical_failure"
        or terminal.get("outcome") != "technical_failure"
        or terminal.get("item_id") != item_id
        or terminal.get("content_hash") != manifest.get("content_hash")
        or not terminal_path_matches
    ):
        raise AssemblyError(f"{item_id} terminal failure binding is stale")
    return dict(manifest)


def _load_committed_records(
    context: RunContext,
    item_id: str,
    accepted_manifest: Mapping[str, Any],
    bundle: Any,
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    manifest_ref = f"requirements/{item_id}/integration/committed/manifest.json"
    _manifest_path, manifest_bytes = _read_bytes(context, manifest_ref, label=f"{item_id} committed integration manifest")
    try:
        integration_manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"{item_id} committed integration manifest is invalid") from exc
    if not isinstance(integration_manifest, Mapping):
        raise AssemblyError(f"{item_id} committed integration manifest is invalid")
    expected = {
        "schema_version", "session_id", "item_id", "owner_id", "invocation_id", "status",
        "accepted_content_hash", "accepted_manifest_hash", "records_path", "records_hash",
        "records_count", "counts", "created_at", "committed_at", "manifest_hash",
    }
    if set(integration_manifest) != expected or integration_manifest.get("schema_version") != "1" or integration_manifest.get("status") != "committed":
        raise AssemblyError(f"{item_id} committed integration manifest fields are invalid")
    if integration_manifest.get("item_id") != item_id:
        raise AssemblyError(f"{item_id} committed integration item identity mismatch")
    if integration_manifest.get("accepted_content_hash") != bundle.content_hash or integration_manifest.get("accepted_manifest_hash") != bundle.manifest_hash:
        raise AssemblyError(f"{item_id} committed integration accepted hash binding mismatch")
    if integration_manifest.get("records_path") != "records.jsonl" or not _is_sha256(integration_manifest.get("records_hash")):
        raise AssemblyError(f"{item_id} committed integration records binding is invalid")
    if not isinstance(integration_manifest.get("records_count"), int) or isinstance(integration_manifest.get("records_count"), bool) or integration_manifest["records_count"] < 0:
        raise AssemblyError(f"{item_id} committed integration records_count is invalid")
    if not isinstance(integration_manifest.get("counts"), Mapping) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in integration_manifest["counts"].values()
    ):
        raise AssemblyError(f"{item_id} committed integration counts are invalid")
    unsigned = {key: value for key, value in integration_manifest.items() if key != "manifest_hash"}
    if integration_manifest.get("manifest_hash") != _json_hash(unsigned):
        raise AssemblyError(f"{item_id} committed integration manifest hash mismatch")
    records_ref = f"requirements/{item_id}/integration/committed/records.jsonl"
    _records_path, records_bytes = _read_bytes(context, records_ref, label=f"{item_id} committed integration records")
    if _sha256_bytes(records_bytes) != integration_manifest["records_hash"]:
        raise AssemblyError(f"{item_id} committed integration records hash mismatch")
    try:
        from auto_foundry_core.integration import IntegrationRecord
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise AssemblyError("auto_foundry_core IntegrationRecord is unavailable") from exc
    records: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(records_bytes.decode("utf-8").splitlines(), 1):
        try:
            raw = json.loads(line)
            record = IntegrationRecord.from_dict(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise AssemblyError(f"{item_id} committed integration record {line_number} is invalid") from exc
        if record.item_id != item_id or record.accepted_content_hash != bundle.content_hash:
            raise AssemblyError(f"{item_id} committed integration record {line_number} hash/identity mismatch")
        records.append(record.to_dict())
    if len(records) != integration_manifest["records_count"]:
        raise AssemblyError(f"{item_id} committed integration records_count does not match bytes")
    _validate_committed_analytical_artifacts(context, item_id, records)
    return dict(integration_manifest), records


def _validate_committed_analytical_artifacts(
    context: RunContext,
    item_id: str,
    records: Sequence[Mapping[str, Any]],
) -> None:
    """Verify typed artifact files before they can become dashboard inputs.

    Product assembly is deliberately read-only over the committed namespace.
    An ``analytical_artifact`` record carries the canonical wire document and
    an immutable committed reference; this check verifies that the referenced
    bytes still match both before mapping and on every retry.  No work/raw
    paths are consulted and no analytics are run here.
    """

    try:
        from auto_foundry_core.analytical_artifacts import AnalyticalArtifact
    except ModuleNotFoundError as exc:  # pragma: no cover - package guard
        raise AssemblyError("auto_foundry_core analytical artifact types are unavailable") from exc
    prefix = "integration/committed/artifacts/"
    seen_ids: dict[str, tuple[str, str, str]] = {}
    seen_refs: set[str] = set()
    for record in records:
        if _text(record.get("kind")) != "analytical_artifact":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise AssemblyError(f"{item_id} analytical artifact payload is invalid")
        try:
            # IntegrationRecord.from_dict already validates the exact payload
            # shape; parse once more here so this module's committed-file
            # authority is explicit and independent of staging/work paths.
            artifact = AnalyticalArtifact.from_dict(payload.get("artifact"))
        except Exception as exc:
            raise AssemblyError(f"{item_id} analytical artifact payload is invalid") from exc
        if artifact.requirement_id != item_id:
            raise AssemblyError(f"{item_id} analytical artifact requirement binding is invalid")
        if payload.get("artifact_id") != artifact.artifact_id or payload.get("artifact_type") != artifact.artifact_type or payload.get("schema_version") != artifact.schema_version or payload.get("requirement_id") != artifact.requirement_id:
            raise AssemblyError(f"{item_id} analytical artifact identity binding is invalid")
        if payload.get("content_hash") != artifact.content_hash or payload.get("envelope_hash") != artifact.envelope_hash:
            raise AssemblyError(f"{item_id} analytical artifact typed hash binding is invalid")
        artifact_bytes = artifact.to_json().encode("utf-8")
        if payload.get("canonical_bytes_sha256") != _sha256_bytes(artifact_bytes):
            raise AssemblyError(f"{item_id} analytical artifact canonical bytes hash is invalid")
        artifact_ref = payload.get("artifact_ref")
        if not isinstance(artifact_ref, str) or not artifact_ref.startswith(prefix) or not artifact_ref.endswith(".json") or artifact_ref in seen_refs:
            raise AssemblyError(f"{item_id} analytical artifact committed reference is invalid")
        relative = artifact_ref[len(prefix):]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", relative):
            raise AssemblyError(f"{item_id} analytical artifact committed reference is invalid")
        seen_refs.add(artifact_ref)
        identity = (
            artifact.content_hash,
            artifact.envelope_hash,
            str(payload["canonical_bytes_sha256"]),
        )
        prior = seen_ids.get(artifact.artifact_id)
        if prior is not None and prior != identity:
            raise AssemblyError(f"{item_id} analytical artifact ID collision")
        seen_ids[artifact.artifact_id] = identity
        ref = f"requirements/{item_id}/{artifact_ref}"
        _assert_no_symlink_chain(context, ref, label=f"{item_id} committed analytical artifact")
        path, actual = _read_bytes(context, ref, label=f"{item_id} committed analytical artifact")
        if actual != artifact_bytes or _sha256_bytes(actual) != payload.get("canonical_bytes_sha256"):
            raise AssemblyError(f"{item_id} committed analytical artifact bytes do not match record")
        try:
            restored = AnalyticalArtifact.from_json(actual.decode("utf-8"))
        except Exception as exc:
            raise AssemblyError(f"{item_id} committed analytical artifact JSON is invalid") from exc
        if restored.to_dict() != artifact.to_dict():
            raise AssemblyError(f"{item_id} committed analytical artifact content drifted")


def _discover_item_ids(context: RunContext, explicit: Sequence[str] | None, plan: Mapping[str, Any] | None) -> list[str]:
    if explicit:
        item_ids = [_text(value).strip() for value in explicit]
    elif plan and isinstance(plan.get("groups"), list):
        item_ids = [
            _text(item_id).strip()
            for group in plan["groups"]
            if isinstance(group, Mapping)
            for item_id in _as_list(group.get("requirement_ids"))
        ]
    else:
        requirements = context.resolve_run_path("requirements")
        item_ids = sorted(path.name for path in requirements.iterdir() if path.is_dir()) if requirements.is_dir() else []
    if not item_ids:
        raise AssemblyError("no requirement IDs were supplied or discoverable")
    seen: set[str] = set()
    normalized: list[str] = []
    for item_id in item_ids:
        if not item_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", item_id) or item_id in seen:
            raise AssemblyError(f"requirement IDs must be unique safe components: {item_id!r}")
        seen.add(item_id)
        normalized.append(item_id)
    return normalized


def _load_plan(context: RunContext, plan_ref: str | Path | None) -> Mapping[str, Any] | None:
    reference = plan_ref or "requirement_supervisor_plan.json"
    path = context.resolve_run_path(reference)
    if not path.exists():
        return None
    return _read_json(context, reference, label="requirement supervisor plan")


def _presentation_generation_metadata(
    context: RunContext,
    generation_id: str | None = None,
    *,
    _lock_held: bool = False,
) -> tuple[str, Any | None]:
    """Resolve the generation identity without changing lifecycle state.

    The dashboard skill deliberately does not extend ``RunLifecycle``.  When
    an active generation exists, its public metadata is the authority.  A
    caller may name a generation for an offline/root product (for example a
    copied G-0001 fixture); otherwise the active pointer is used.  Missing
    metadata is never synthesized into a manager admission.
    """

    metadata = None
    try:
        from auto_foundry_core.lifecycle import RunLifecycle

        if _lock_held:
            pointer = RunLifecycle._read_generation_pointer_unlocked(context)  # noqa: SLF001 - lock is held by caller
            metadata = (
                RunLifecycle._load_generation_unlocked(context, pointer)  # noqa: SLF001 - lock is held by caller
                if pointer is not None
                else None
            )
        else:
            metadata = RunLifecycle.active_generation_metadata(context)
    except Exception:
        metadata = None
    candidate = _text(generation_id).strip() or _text(getattr(metadata, "generation_id", "")).strip() or _active_generation_id(context)
    if not candidate:
        candidate = "G-0001"
    if not re.fullmatch(r"G-[0-9]{4}", candidate):
        raise BusinessPresentationPlanError("generation_id must be a canonical G-0000 identifier")
    if metadata is not None and _text(getattr(metadata, "generation_id", "")) != candidate:
        raise BusinessPresentationPlanError("presentation plan generation does not match the active generation")
    return candidate, metadata


def _presentation_supervisor_binding(
    context: RunContext,
    generation_id: str,
    metadata: Any | None,
) -> tuple[str, Mapping[str, Any], str]:
    """Return the authoritative supervisor-plan ref, value, and hash."""

    if metadata is not None:
        path = Path(str(metadata.plan_path))
        try:
            reference = path.relative_to(context.run_root).as_posix()
        except ValueError as exc:
            raise BusinessPresentationPlanError("active supervisor plan escaped the run root") from exc
    elif generation_id == "G-0001":
        reference = "requirement_supervisor_plan.json"
    else:
        reference = f"extensions/{generation_id}/requirement_supervisor_plan.json"
    value = _read_json(context, reference, label="presentation supervisor plan")
    path = context.resolve_run_path(reference)
    return reference, value, _sha256_bytes(path.read_bytes())


def _presentation_parent_binding(context: RunContext, generation_id: str, metadata: Any | None) -> dict[str, Any] | None:
    """Bind the immediate product parent without reading unrelated generations."""

    parent_id = _text(getattr(metadata, "parent_generation_id", "")).strip() if metadata is not None else ""
    if not parent_id:
        if generation_id == "G-0001":
            return None
        ordinal = int(generation_id[2:])
        parent_id = f"G-{ordinal - 1:04d}"
    manifest_ref = f"products/generations/{parent_id}/product_manifest.json"
    manifest_path = context.resolve_run_path(manifest_ref)
    # The first appended generation may not have a generation-scoped product
    # bridge: its immutable parent product is still the legacy root manifest.
    # Resolve that one canonical parent boundary exactly as the generation
    # delta assembler does, without manufacturing a G-0001 successor path.
    if (
        parent_id == "G-0001"
        and not manifest_path.is_file()
        and not manifest_path.is_symlink()
    ):
        manifest_ref = "products/product_manifest.json"
        manifest_path = context.resolve_run_path(manifest_ref)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BusinessPresentationPlanError("immediate parent product manifest is missing or symlinked")
    parent = _read_json(context, manifest_ref, label="presentation parent product manifest")
    dashboard = parent.get("dashboard")
    receipt_ref = dashboard.get("receipt_ref") if isinstance(dashboard, Mapping) else None
    if not isinstance(receipt_ref, str) or not receipt_ref:
        raise BusinessPresentationPlanError("immediate parent product manifest lacks a dashboard receipt")
    receipt_path = context.resolve_run_path(receipt_ref)
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise BusinessPresentationPlanError("immediate parent dashboard receipt is missing or symlinked")
    return {
        "generation_id": parent_id,
        "product_manifest_ref": manifest_ref,
        "product_manifest_sha256": _sha256_bytes(manifest_path.read_bytes()),
        "receipt_ref": receipt_ref,
        "receipt_sha256": _sha256_bytes(receipt_path.read_bytes()),
    }


def _presentation_input_bindings(context: RunContext, item_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Load the exact accepted/committed bindings used by a plan."""

    bindings: list[dict[str, Any]] = []
    for item_id in item_ids:
        state = _read_json(context, f"requirements/{item_id}/item_state.json", label=f"{item_id} item state")
        if state.get("lifecycle_state") == "technical_failure":
            # A pre-acceptance terminal failure has no accepted bundle or
            # committed integration.  Its validated public terminal manifest
            # is the only source-bound input available to a limited dashboard.
            terminal_manifest = _load_item_technical_failure_manifest(context, item_id, state)
            accepted_content_hash = terminal_manifest["content_hash"]
            accepted_manifest_hash = terminal_manifest["manifest_hash"]
            integration_manifest_hash = terminal_manifest["manifest_hash"]
            records: list[Mapping[str, Any]] = []
            artifact_bindings: list[dict[str, Any]] = []
        else:
            content, accepted_manifest, accepted_meta = _load_public_accepted_bundle(context, item_id)
            accepted_bundle = accepted_meta["bundle"]
            accepted_content_hash = accepted_bundle.content_hash
            accepted_manifest_hash = accepted_bundle.manifest_hash
            if state.get("integration_state") == "technical_failure":
                # Accepted content remains immutable, while the exhausted
                # integration failure manifest is the settled integration
                # boundary; no unavailable records are inferred.
                failure_manifest = _load_technical_failure_manifest(context, item_id, accepted_meta)
                integration_manifest_hash = failure_manifest["manifest_hash"]
                records = []
            else:
                integration_manifest, records = _load_committed_records(
                    context,
                    item_id,
                    accepted_manifest,
                    accepted_bundle,
                )
                integration_manifest_hash = integration_manifest["manifest_hash"]
            # Typed analytical artifacts are accepted business outputs too.
            # Keep their complete provenance in the plan input binding so the
            # writer and assembler share one authoritative projection.
            artifact_bindings = _analytical_artifact_input_entries({item_id: records})
        binding = {
            "item_id": item_id,
            "accepted_content_hash": accepted_content_hash,
            "accepted_manifest_hash": accepted_manifest_hash,
            "integration_manifest_hash": integration_manifest_hash,
            "record_count": len(records),
            "analytical_artifacts": artifact_bindings,
        }
        _validate_presentation_input_binding(binding, label="presentation plan")
        bindings.append(binding)
    return bindings


def _presentation_widget_binding(widget: Mapping[str, Any]) -> dict[str, Any]:
    """Return stable widget identity used by inventory and plan validation.

    Manager text is deliberately *not* copied into a plan from this helper.
    Every selected display value must be bound to a JSON pointer into the
    authoritative committed record (see ``display_projection`` below).
    """

    record_ids = sorted({
        _text(value).strip()
        for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
        if _text(value).strip()
    })
    record_refs = sorted({
        _text(value).strip()
        for value in (_as_list(widget.get("integration_record_refs")) or [widget.get("integration_record_ref")])
        if _text(value).strip()
    })
    binding = {
        "widget_id": _text(widget.get("id")),
        "requirement_id": _text(widget.get("requirement_id")),
        "presentation_role": _text(widget.get("presentation_role") or "decision_view"),
        "integration_record_ids": record_ids,
        "integration_record_refs": record_refs,
    }
    # Preserve explicit origin metadata in the public inventory.  No
    # classification is inferred from labels, values, roles, or chart kind.
    if "technical_surface" in widget:
        binding["technical_surface"] = widget.get("technical_surface") is True
    if _text(widget.get("technical_surface_reason")).strip():
        binding["technical_surface_reason"] = _text(widget.get("technical_surface_reason"))
    return binding


_PRESENTATION_PROJECTION_FIELDS = frozenset({
    "title", "label", "body", "value", "display_value", "denominator", "unit",
    "rows", "period", "as_of", "status", "note", "subtitle", "scope", "answer_scope",
})


def _presentation_pointer_escape(value: Any) -> str:
    return _text(value).replace("~", "~0").replace("/", "~1")


def _presentation_pointer_value(root: Mapping[str, Any], pointer: Any) -> Any:
    """Resolve one bounded JSON pointer into an authoritative record root."""

    if not isinstance(pointer, str) or not pointer.startswith("/") or pointer in {"/", "/payload", "/accepted"}:
        raise BusinessPresentationPlanError("display projection pointer must be a non-root JSON pointer")
    raw_parts = pointer[1:].split("/")
    parts: list[str] = []
    for raw in raw_parts:
        if re.search(r"~(?![01])", raw):
            raise BusinessPresentationPlanError("display projection pointer has invalid escaping")
        parts.append(raw.replace("~1", "/").replace("~0", "~"))
    if parts[0] not in {"payload", "accepted"}:
        raise BusinessPresentationPlanError("display projection pointers must target payload or accepted fields")
    current: Any = root
    for part in parts:
        if isinstance(current, Mapping):
            if part not in current:
                raise BusinessPresentationPlanError(f"display projection pointer is missing: {pointer}")
            current = current[part]
        elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            index = int(part)
            if index >= len(current):
                raise BusinessPresentationPlanError(f"display projection pointer is out of range: {pointer}")
            current = current[index]
        else:
            raise BusinessPresentationPlanError(f"display projection pointer cannot descend: {pointer}")
    return current


def _presentation_projection_value_equal(actual: Any, expected: Any) -> bool:
    """Compare JSON values without Python's bool/int equality ambiguity."""

    return _canonical_bytes(actual) == _canonical_bytes(expected)


def _visual_pointer_value(root: Mapping[str, Any], pointer: Any) -> Any:
    """Resolve a JSON pointer into one immutable visual source object.

    V2 visual entries deliberately bind title fields to the widget snapshot
    and chart geometry/value fields to the chart-map entry.  The resolver is
    kept separate from the accepted-record pointer resolver so a visual plan
    can never accidentally bind to an unreviewed source/work field.
    """

    if not isinstance(pointer, str) or not pointer.startswith("/") or pointer == "/":
        raise BusinessPresentationPlanError("visual projection pointer must be a non-root JSON pointer")
    parts: list[str] = []
    for raw in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise BusinessPresentationPlanError("visual projection pointer has invalid escaping")
        parts.append(raw.replace("~1", "/").replace("~0", "~"))
    current: Any = root
    for part in parts:
        if isinstance(current, Mapping):
            if part not in current:
                raise BusinessPresentationPlanError(f"visual projection pointer is missing: {pointer}")
            current = current[part]
        elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            index = int(part)
            if index >= len(current):
                raise BusinessPresentationPlanError(f"visual projection pointer is out of range: {pointer}")
            current = current[index]
        else:
            raise BusinessPresentationPlanError(f"visual projection pointer cannot descend: {pointer}")
    return current


def _visual_snapshot_hash(widget: Mapping[str, Any]) -> str:
    # The widget snapshot binds the reviewed visual payload and immutable
    # identity, while excluding the presentation-plan envelope itself.  The
    # assembler attaches ``manager_admission``/``manager_presentation`` only
    # after a plan is selected (the latter necessarily contains this hash), so
    # hashing those fields would create a self-referential, non-reproducible
    # plan.  The exact full widget remains available in ``audit_widgets``.
    snapshot = {
        key: copy.deepcopy(value)
        for key, value in widget.items()
        if key not in {
            "manager_admission", "manager_presentation", "presentation_audience",
            "presentation_tier", "manager_anchor", "presentation_plan_ref",
            "presentation_plan_sha256", "overview",
        }
    }
    return _sha256_bytes(_canonical_bytes(snapshot))


def _visual_chart_hash(chart: Mapping[str, Any]) -> str:
    # Chart role/tier are plan-derived metadata, not chart geometry or values.
    # Keep the hash stable when a fixture is rebound to the reviewed visual
    # partition while retaining exact type/fields/provenance binding.
    snapshot = copy.deepcopy(dict(chart))
    fields = snapshot.get("fields_or_values_used")
    if isinstance(fields, Mapping):
        snapshot["fields_or_values_used"] = {
            key: copy.deepcopy(value)
            for key, value in fields.items()
            if key not in {"presentation_role", "presentation_tier"}
        }
    return _sha256_bytes(_canonical_bytes(snapshot))


def _visual_entry_shape(entry: Mapping[str, Any]) -> None:
    required = {
        "widget_id", "requirement_id", "record_ids", "presentation_audience",
        "visual_type", "chart_family", "widget_snapshot_sha256",
        "chart_entry_sha256", "allowed_visual_fields", "title_projection",
        "visual_projection",
    }
    optional = {
        "recipe_id", "layout", "renderer_type", "accepted_visual_pointer",
        "accepted_content_hash", "accepted_manifest_hash", "accepted_artifact_ref",
        "accepted_artifact_sha256",
    }
    if not required <= set(entry) <= required | optional:
        raise BusinessPresentationPlanError("v2 visual entry fields are invalid")
    if not isinstance(entry.get("widget_id"), str) or not entry["widget_id"].strip():
        raise BusinessPresentationPlanError("v2 visual entry widget identity is invalid")
    if not isinstance(entry.get("requirement_id"), str) or not entry["requirement_id"].strip():
        raise BusinessPresentationPlanError("v2 visual entry requirement identity is invalid")
    if entry.get("presentation_audience") not in {"business_manager", "technical_audit_gallery"}:
        raise BusinessPresentationPlanError("v2 visual entry audience is invalid")
    if not isinstance(entry.get("record_ids"), list) or len(set(entry["record_ids"])) != len(entry["record_ids"]):
        raise BusinessPresentationPlanError("v2 visual entry record bindings are invalid")
    if any(not isinstance(value, str) or not value.strip() for value in entry["record_ids"]):
        raise BusinessPresentationPlanError("v2 visual entry record bindings are invalid")
    if not isinstance(entry.get("visual_type"), str) or not entry["visual_type"].strip():
        raise BusinessPresentationPlanError("v2 visual entry type is invalid")
    if not isinstance(entry.get("chart_family"), str) or not entry["chart_family"].strip():
        raise BusinessPresentationPlanError("v2 visual entry family is invalid")
    for field in ("widget_snapshot_sha256", "chart_entry_sha256"):
        if not _is_sha256(entry.get(field)):
            raise BusinessPresentationPlanError(f"v2 visual entry {field} is invalid")
    if "accepted_visual_pointer" in entry:
        pointer = entry.get("accepted_visual_pointer")
        if not isinstance(pointer, str) or not re.fullmatch(r"/accepted/visuals/(?:0|[1-9][0-9]*)", pointer):
            raise BusinessPresentationPlanError("v2 accepted visual pointer is invalid")
        if not _is_sha256(entry.get("accepted_content_hash")) or not _is_sha256(entry.get("accepted_manifest_hash")):
            raise BusinessPresentationPlanError("v2 accepted visual content/manifest hash is invalid")
    if "accepted_artifact_ref" in entry:
        if not isinstance(entry.get("accepted_artifact_ref"), str) or not entry["accepted_artifact_ref"].strip() or not _is_sha256(entry.get("accepted_artifact_sha256")):
            raise BusinessPresentationPlanError("v2 accepted visual artifact binding is invalid")
    allowed = entry.get("allowed_visual_fields")
    projection = entry.get("visual_projection")
    if not isinstance(allowed, list) or len(set(allowed)) != len(allowed) or any(not isinstance(value, str) or not value for value in allowed):
        raise BusinessPresentationPlanError("v2 visual entry allowed fields are invalid")
    if not isinstance(projection, Mapping) or set(projection) != set(allowed):
        raise BusinessPresentationPlanError("v2 visual entry projection fields do not match allowed fields")
    title = entry.get("title_projection")
    if not isinstance(title, Mapping) or set(title) != {"pointer", "value"} or not isinstance(title.get("pointer"), str):
        raise BusinessPresentationPlanError("v2 visual entry title projection is invalid")
    for field, binding in projection.items():
        if not isinstance(binding, Mapping) or set(binding) != {"pointer", "value"} or not isinstance(binding.get("pointer"), str):
            raise BusinessPresentationPlanError(f"v2 visual entry projection is invalid: {field}")
    if "recipe_id" in entry and (not isinstance(entry.get("recipe_id"), str) or not entry["recipe_id"].strip()):
        raise BusinessPresentationPlanError("v2 visual entry recipe_id is invalid")
    if "layout" in entry:
        layouts = getattr(_dashboard_runtime(), "SUPPORTED_LAYOUTS", {"full", "wide", "half", "compact"})
        if not isinstance(entry.get("layout"), str) or entry["layout"] not in layouts:
            raise BusinessPresentationPlanError("v2 visual entry layout is invalid")
    if "renderer_type" in entry:
        renderer_types = getattr(_dashboard_runtime(), "SUPPORTED_RENDERER_TYPES", set())
        if not isinstance(entry.get("renderer_type"), str) or entry["renderer_type"] not in renderer_types:
            raise BusinessPresentationPlanError("v2 visual entry renderer_type is invalid")


def _candidate_recipe_ids(candidate: Mapping[str, Any]) -> list[str]:
    """Return registry recipe IDs marked eligible by the read-only inventory."""

    recipes = candidate.get("recipes")
    if not isinstance(recipes, list):
        return []
    return [
        _text(recipe.get("id")).strip()
        for recipe in recipes
        if isinstance(recipe, Mapping)
        and recipe.get("eligible") is True
        and _text(recipe.get("id")).strip()
    ]


def _default_recipe_id(candidate: Mapping[str, Any]) -> str | None:
    """Choose a semantic/data-shape default for an initial plan only."""

    eligible = _candidate_recipe_ids(candidate)
    current = _text(candidate.get("chart_family")).strip()
    # A Product Agent may explicitly retain the reviewed family, but the
    # inventory's initial choice should expose an executable business chart
    # whenever the exact supplied shape supports one.  Historically this
    # helper preferred ``table`` whenever the accepted declaration's family
    # was table, which made richer source-bound bars/lines invisible to the
    # plan author even though the registry had already proved them eligible.
    # Registry order is committed and deterministic; choosing its first
    # non-table recipe is therefore a stable semantic preference rather than
    # a per-requirement chart rule.  Tables remain the truthful fallback when
    # no chart family can consume the exact values.
    richer = [recipe_id for recipe_id in eligible if recipe_id != "table"]
    if current in eligible and current != "table":
        return current
    if richer:
        return richer[0]
    if current in eligible:
        return current
    if "table" in eligible:
        return "table"
    # No eligible recipe means the visual has no executable geometry.  Leave
    # it as an audit-only declaration rather than inventing a table choice;
    # the one requirement fallback carries any reviewed findings/limitations.
    return eligible[0] if eligible else None


def _validated_plan_selection(
    candidate: Mapping[str, Any],
    requested: Mapping[str, Any] | None = None,
    *,
    require_explicit: bool = False,
) -> tuple[str, str, str]:
    """Validate Product Agent recipe/layout/type choices against inventory."""

    requested = requested or {}
    if require_explicit:
        missing = [
            key
            for key in ("recipe_id", "layout", "renderer_type")
            if not isinstance(requested.get(key), str) or not requested[key].strip()
        ]
        if missing:
            raise BusinessPresentationPlanError(
                "v2 successor visual selection requires explicit "
                f"recipe_id, layout, and renderer_type: {_text(candidate.get('widget_id'))}"
            )
    eligible = _candidate_recipe_ids(candidate)
    requested_recipe = _text(requested.get("recipe_id")).strip()
    recipe_id = requested_recipe or _default_recipe_id(candidate)
    # A malformed/empty visual still receives the canonical table recipe as a
    # truthful empty-state fallback.  An explicit Product Agent choice must,
    # however, be present in the eligible inventory.
    if recipe_id not in eligible and not (not requested_recipe and recipe_id == "table" and not eligible):
        raise BusinessPresentationPlanError(
            f"visual recipe is not eligible for exact supplied shape: {_text(candidate.get('widget_id'))}:{recipe_id or '<missing>'}"
        )
    layout = _text(requested.get("layout")).strip() or _dashboard_runtime().default_layout_for_recipe(recipe_id)
    layouts = getattr(_dashboard_runtime(), "SUPPORTED_LAYOUTS", {"full", "wide", "half", "compact"})
    if layout not in layouts:
        raise BusinessPresentationPlanError(
            f"visual layout is not supported: {_text(candidate.get('widget_id'))}:{layout}"
        )
    recipe_entry = next(
        (
            recipe for recipe in candidate.get("recipes", [])
            if isinstance(recipe, Mapping) and _text(recipe.get("id")) == recipe_id
        ),
        None,
    )
    renderer_types = (
        tuple(_text(value) for value in recipe_entry.get("renderer_types", []) if _text(value).strip())
        if isinstance(recipe_entry, Mapping)
        else tuple()
    )
    if not renderer_types:
        # A table fallback is always executable; for current recipes this
        # branch is only reachable with a private predecessor inventory that
        # predates renderer metadata, so derive the closed default from the
        # same runtime helper used by the design inventory.
        renderer_types = _dashboard_runtime().renderer_types_for_recipe(
            recipe_id,
            candidate.get("widget") if isinstance(candidate.get("widget"), Mapping) else None,
        ) or (("table",) if recipe_id == "table" else tuple())
    requested_renderer = _text(requested.get("renderer_type")).strip()
    renderer_type = requested_renderer or (renderer_types[0] if renderer_types else "")
    if renderer_type not in renderer_types:
        raise BusinessPresentationPlanError(
            f"visual renderer_type is not eligible for exact recipe: {_text(candidate.get('widget_id'))}:{recipe_id}:{renderer_type or '<missing>'}"
        )
    return recipe_id, layout, renderer_type


def _v2_manager_entry_from_visual(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one visual entry into the renderer's manager envelope binding."""

    result = {
        "widget_id": entry["widget_id"],
        "requirement_id": entry["requirement_id"],
        "record_ids": list(entry["record_ids"]),
        "presentation_role": "decision_view",
        "visual_type": entry["visual_type"],
        "chart_family": entry["chart_family"],
        "widget_snapshot_sha256": entry["widget_snapshot_sha256"],
        "chart_entry_sha256": entry["chart_entry_sha256"],
        "allowed_visual_fields": list(entry["allowed_visual_fields"]),
        "title_projection": copy.deepcopy(entry["title_projection"]),
        "visual_projection": copy.deepcopy(entry["visual_projection"]),
    }
    # Recipe/layout are declarative plan choices.  Keep them optional for
    # private predecessor fixtures that predate the choice contract, while
    # every current writer-produced V2 visual carries both fields.
    for key in ("recipe_id", "layout", "renderer_type"):
        if key in entry:
            result[key] = copy.deepcopy(entry[key])
    for key in (
        "accepted_visual_pointer", "accepted_content_hash", "accepted_manifest_hash",
        "accepted_artifact_ref", "accepted_artifact_sha256",
    ):
        if key in entry:
            result[key] = copy.deepcopy(entry[key])
    return result


def _v2_predecessor_plan_manager_contract(
    plan: Mapping[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Validate the complete manager envelope of a V2 predecessor.

    ``previous_plan_manager_*`` binds the complete manager envelope of a V2
    predecessor.  The entries may contain visual projection fields in
    addition to pointer-bound record fields, so the exact prior manager order
    and bytes are retained for CAS verification without constraining the
    successor's new manager selection.
    """

    source = plan.get("source_bindings")
    if not isinstance(source, Mapping):
        return [], []
    ids = source.get("previous_plan_manager_widget_ids")
    entries = source.get("previous_plan_manager_entries")
    if ids is None and entries is None:
        return [], []
    if (
        not isinstance(ids, list)
        or not ids
        or len(set(ids)) != len(ids)
        or any(not isinstance(value, str) or not value.strip() for value in ids)
        or not isinstance(entries, list)
        or len(entries) != len(ids)
        or [entry.get("widget_id") for entry in entries if isinstance(entry, Mapping)] != ids
    ):
        raise BusinessPresentationPlanError("v2 predecessor plan-manager binding is invalid")
    predecessor_entries: list[dict[str, Any]] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping) or not raw_entry.get("widget_id"):
            raise BusinessPresentationPlanError("v2 predecessor plan-manager entry is invalid")
        # Keep the exact key/value envelope.  V2 visual entries intentionally
        # carry visual projection fields in addition to pointer-bound record
        # fields; validating the mapping as a whole prevents a candidate from
        # replacing one with a semantically similar but structurally weaker
        # entry.
        predecessor_entries.append(copy.deepcopy(dict(raw_entry)))
    # This is an immutable source binding for the predecessor plan, not a
    # successor selection constraint.  Membership/order can change when the
    # Product Agent regenerates the manager surface; CAS validation below
    # checks that this envelope names the actual predecessor bytes.
    return list(ids), predecessor_entries


def _v2_predecessor_visual_contract(
    plan: Mapping[str, Any],
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Validate and return an optional exact predecessor visual contract.

    G3's original V2 plan predates these fields, so all three are optional
    for that already-frozen plan.  A successor plan writes them explicitly;
    when present they bind the predecessor's ordered manager/audit partition
    and every pointer/hash entry, preventing a same-generation rebuild from
    silently changing an inherited visual payload while allowing its audience
    and declarative presentation recipe to be reconsidered.
    """

    source = plan.get("source_bindings")
    if not isinstance(source, Mapping):
        return [], [], []
    keys = (
        "previous_manager_visual_widget_ids",
        "previous_audit_visual_widget_ids",
        "previous_visual_entries",
    )
    present = [key in source for key in keys]
    if not any(present):
        return [], [], []
    if not all(present):
        raise BusinessPresentationPlanError("v2 predecessor visual binding is incomplete")
    manager = source.get(keys[0])
    audit = source.get(keys[1])
    entries = source.get(keys[2])
    if (
        not isinstance(manager, list)
        or not isinstance(audit, list)
        or not isinstance(entries, list)
        or len(set(manager)) != len(manager)
        or len(set(audit)) != len(audit)
        or set(manager).intersection(audit)
        or any(not isinstance(value, str) or not value.strip() for value in manager + audit)
        or [entry.get("widget_id") for entry in entries if isinstance(entry, Mapping)] != manager + audit
        or len(set(manager + audit)) != len(entries)
    ):
        raise BusinessPresentationPlanError("v2 predecessor visual binding is invalid")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise BusinessPresentationPlanError("v2 predecessor visual entry is invalid")
        _visual_entry_shape(entry)
    current_by_id = {
        _text(entry.get("widget_id")): entry
        for entry in plan.get("visual_entries", [])
        if isinstance(entry, Mapping)
    }
    for predecessor in entries:
        widget_id = _text(predecessor.get("widget_id"))
        current = current_by_id.get(widget_id)
        if not isinstance(current, Mapping):
            raise BusinessPresentationPlanError(f"v2 successor omits predecessor visual: {widget_id}")
        # Source/widget/chart/value bindings are immutable across a successor;
        # manager audience and recipe/layout/renderer choices are deliberately
        # free to change with the new explicit Product selection.
        for field in (
            "requirement_id", "record_ids", "visual_type", "chart_family",
            "widget_snapshot_sha256", "chart_entry_sha256",
            "allowed_visual_fields", "title_projection", "visual_projection",
            "accepted_visual_pointer", "accepted_content_hash", "accepted_manifest_hash",
            "accepted_artifact_ref", "accepted_artifact_sha256",
        ):
            if field in predecessor and current.get(field) != predecessor.get(field):
                raise BusinessPresentationPlanError(f"v2 predecessor visual drifted: {widget_id}:{field}")
    return list(manager), list(audit), [copy.deepcopy(dict(entry)) for entry in entries]


def _presentation_authoritative_root(
    record: Mapping[str, Any],
    accepted_content: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root: dict[str, Any] = {"payload": copy.deepcopy(_record_payload(record))}
    if accepted_content is not None:
        root["accepted"] = copy.deepcopy(dict(accepted_content))
    return root


def _manager_entry_shape(entry: Mapping[str, Any]) -> None:
    required = {
        "widget_id", "record_id", "requirement_id", "presentation_role",
        "file_sha256", "canonical_payload_sha256", "display_projection",
    }
    if set(entry) != required:
        raise BusinessPresentationPlanError("v2 manager entry fields are invalid")
    for key in ("widget_id", "record_id", "requirement_id", "presentation_role"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise BusinessPresentationPlanError("v2 manager entry identity is invalid")
    if not _is_sha256(entry.get("file_sha256")) or not _is_sha256(entry.get("canonical_payload_sha256")):
        raise BusinessPresentationPlanError("v2 manager entry hashes are invalid")
    projection = entry.get("display_projection")
    if not isinstance(projection, Mapping) or "title" not in projection:
        raise BusinessPresentationPlanError("v2 manager entry requires a title projection")
    if not ("body" in projection or "value" in projection or "rows" in projection or "display_value" in projection):
        raise BusinessPresentationPlanError("v2 manager entry requires a display projection")
    if any(not isinstance(key, str) or key not in _PRESENTATION_PROJECTION_FIELDS for key in projection):
        raise BusinessPresentationPlanError("v2 manager display projection field is unsupported")
    for field, binding in projection.items():
        if not isinstance(binding, Mapping) or set(binding) != {"pointer", "value"}:
            raise BusinessPresentationPlanError(f"presentation plan projection {field} must bind pointer and value")
        if not isinstance(binding.get("pointer"), str):
            raise BusinessPresentationPlanError(f"presentation plan projection {field} pointer is invalid")


def _candidate_display_projection(
    root: Mapping[str, Any],
    record: Mapping[str, Any],
) -> dict[str, dict[str, Any]] | None:
    """Suggest a pointer-only manager projection for reviewer selection.

    This helper never invents text or derives metrics.  It only exposes
    existing scalar/list fields from the committed payload.  Reviewers may
    narrow the projection, but cannot supply a value that is not present at a
    validated JSON pointer.
    """

    payload = root.get("payload") if isinstance(root, Mapping) else None
    if not isinstance(payload, Mapping):
        return None
    kind = _text(record.get("kind")).lower()
    title_key = next((key for key in ("label", "title", "name", "claim") if key in payload and isinstance(payload[key], str) and payload[key].strip()), None)
    if title_key is None:
        return None
    projection: dict[str, dict[str, Any]] = {
        "title": {"pointer": f"/payload/{_presentation_pointer_escape(title_key)}", "value": copy.deepcopy(payload[title_key])},
    }
    if kind in {"claim", "limitation"} and "claim" in payload and isinstance(payload.get("claim"), str):
        projection["body"] = {"pointer": "/payload/claim", "value": copy.deepcopy(payload["claim"])}
    elif "value" in payload and not isinstance(payload.get("value"), (Mapping, list)):
        projection["value"] = {"pointer": "/payload/value", "value": copy.deepcopy(payload.get("value"))}
    elif "display_value" in payload and not isinstance(payload.get("display_value"), (Mapping, list)):
        projection["display_value"] = {"pointer": "/payload/display_value", "value": copy.deepcopy(payload.get("display_value"))}
    else:
        return None
    for output_field, payload_keys in (
        ("unit", ("unit", "units", "distinct_unit")),
        ("denominator", ("denominator", "population")),
        ("period", ("period", "as_of")),
        ("status", ("status",)),
    ):
        key = next((candidate for candidate in payload_keys if candidate in payload and payload[candidate] not in (None, "")), None)
        if key is not None and not isinstance(payload[key], (Mapping, list)):
            projection[output_field] = {"pointer": f"/payload/{_presentation_pointer_escape(key)}", "value": copy.deepcopy(payload[key])}
    return projection


def _accepted_evidence_display_projection(
    widget: Mapping[str, Any],
) -> dict[str, dict[str, Any]] | None:
    """Expose one exact, source-bound projection for an accepted ledger row.

    Accepted-evidence widgets do not have committed integration record IDs,
    but their audit payload is the exact hash-bound ledger record.  The public
    inventory therefore exposes the same pointer/value envelope used by
    ordinary record candidates, using only the ledger's own fields.  The
    renderer consumes the V2 visual projection; this envelope exists so a
    Product Agent can select the candidate without guessing an ID or value.
    """

    payload = widget.get("audit_payload")
    if not isinstance(payload, Mapping):
        return None
    conclusion = payload.get("conclusion")
    if isinstance(conclusion, str) and conclusion.strip():
        projection: dict[str, dict[str, Any]] = {
            "title": {"pointer": "/payload/conclusion", "value": copy.deepcopy(conclusion)},
            "body": {"pointer": "/payload/conclusion", "value": copy.deepcopy(conclusion)},
        }
    else:
        # Evidence IDs remain metadata/provenance only.  They are a truthful
        # source field for identifying a ledger row when it has no conclusion;
        # the manager-facing title remains the pointer-derived widget title
        # and never uses this value.
        evidence_id = payload.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            return None
        projection = {
            "title": {
                "pointer": "/widget_snapshot/title",
                "value": copy.deepcopy(widget.get("title") or "Business metrics"),
            },
        }
        facts = payload.get("facts")
        if facts is not None:
            projection["value"] = {"pointer": "/payload/facts", "value": copy.deepcopy(facts)}
    candidate_kind = _text(widget.get("accepted_evidence_candidate_kind")).strip()
    pointer = _text(widget.get("accepted_evidence_pointer")).strip()
    rows = widget.get("rows")
    if candidate_kind == "table" and pointer.startswith("/facts/") and isinstance(rows, list):
        projection["rows"] = {
            "pointer": f"/payload{pointer}",
            "value": copy.deepcopy(rows),
        }
    return projection


def _presentation_plan_ref(context: RunContext, generation_id: str, reference: str | Path | None = None) -> str:
    value = Path(reference or Path("extensions") / generation_id / PRESENTATION_PLAN_FILENAME)
    if value.is_absolute() or ".." in value.parts:
        raise BusinessPresentationPlanError("presentation plan reference must remain run-relative")
    path = context.resolve_run_path(value)
    try:
        path.relative_to(context.run_root)
    except ValueError as exc:
        raise BusinessPresentationPlanError("presentation plan reference escaped the run root") from exc
    return value.as_posix()


def _validate_presentation_plan_v2_shape(plan: Mapping[str, Any]) -> None:
    expected = {
        "schema_version", "run_id", "generation_id", "supervisor_plan_ref", "supervisor_plan_sha256",
        "item_order", "input_items", "parent", "reviewer_ref", "manager_widget_ids", "manager_entries",
        "manager_visual_widget_ids", "audit_visual_widget_ids", "visual_entries", "source_bindings",
    }
    if set(plan) - {"presentation"} != expected or plan.get("schema_version") != PRESENTATION_PLAN_V2_SCHEMA:
        raise BusinessPresentationPlanError("v2 presentation plan fields are invalid")
    if "presentation" in plan:
        _dashboard_runtime().validate_presentation_copy(
            plan["presentation"], widget_ids=plan.get("manager_widget_ids", []),
            requirement_ids=plan.get("item_order", []),
        )
    # Validate the immutable lifecycle/input envelope directly.  V2 manager
    # entries may carry visual projection fields, so only non-visual entries
    # use the pointer-bound manager record shape below.
    if not isinstance(plan.get("run_id"), str) or not plan["run_id"]:
        raise BusinessPresentationPlanError("v2 presentation plan run_id is invalid")
    if not isinstance(plan.get("generation_id"), str) or not re.fullmatch(r"G-[0-9]{4}", plan["generation_id"]):
        raise BusinessPresentationPlanError("v2 presentation plan generation_id is invalid")
    if not _is_sha256(plan.get("supervisor_plan_sha256")):
        raise BusinessPresentationPlanError("v2 supervisor hash is invalid")
    item_order = plan.get("item_order")
    input_items = plan.get("input_items")
    if not isinstance(item_order, list) or len(set(item_order)) != len(item_order) or any(not isinstance(value, str) or not value for value in item_order):
        raise BusinessPresentationPlanError("v2 item_order is invalid")
    if not isinstance(input_items, list) or [value.get("item_id") for value in input_items if isinstance(value, Mapping)] != item_order:
        raise BusinessPresentationPlanError("v2 input bindings do not match item_order")
    for item in input_items:
        _validate_presentation_input_binding(item, label="v2 presentation plan")
    parent = plan.get("parent")
    if parent is not None and (not isinstance(parent, Mapping) or set(parent) != {"generation_id", "product_manifest_ref", "product_manifest_sha256", "receipt_ref", "receipt_sha256"}):
        raise BusinessPresentationPlanError("v2 parent binding is invalid")
    if isinstance(parent, Mapping) and (not _is_sha256(parent.get("product_manifest_sha256")) or not _is_sha256(parent.get("receipt_sha256"))):
        raise BusinessPresentationPlanError("v2 parent binding hashes are invalid")
    if not isinstance(plan.get("reviewer_ref"), str) or not plan["reviewer_ref"].strip():
        raise BusinessPresentationPlanError("v2 reviewer_ref is required")
    manager_visual = plan.get("manager_visual_widget_ids")
    audit_visual = plan.get("audit_visual_widget_ids")
    if (
        not isinstance(manager_visual, list)
        or not isinstance(audit_visual, list)
        or len(set(manager_visual)) != len(manager_visual)
        or len(set(audit_visual)) != len(audit_visual)
        or set(manager_visual).intersection(audit_visual)
        or any(not isinstance(value, str) or not value.strip() for value in manager_visual + audit_visual)
    ):
        raise BusinessPresentationPlanError("v2 visual partition is invalid")
    entries = plan.get("visual_entries")
    if not isinstance(entries, list) or len(entries) != len(set(manager_visual + audit_visual)):
        raise BusinessPresentationPlanError("v2 visual entries must cover the visual partition")
    entry_ids = [entry.get("widget_id") for entry in entries if isinstance(entry, Mapping)]
    if entry_ids != manager_visual + audit_visual or len(set(entry_ids)) != len(entry_ids):
        raise BusinessPresentationPlanError("v2 visual entries must preserve the reviewed partition order")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise BusinessPresentationPlanError("v2 visual entry is invalid")
        _visual_entry_shape(entry)
    manager_ids = plan.get("manager_widget_ids")
    manager_entries = plan.get("manager_entries")
    if not isinstance(manager_ids, list) or not isinstance(manager_entries, list) or len(manager_ids) != len(manager_entries):
        raise BusinessPresentationPlanError("v2 manager entries are invalid")
    if len(set(manager_ids)) != len(manager_ids) or [entry.get("widget_id") for entry in manager_entries if isinstance(entry, Mapping)] != manager_ids:
        raise BusinessPresentationPlanError("v2 manager entries must preserve manager order")
    manager_visual_set = set(manager_visual)
    if not manager_visual_set <= set(manager_ids):
        raise BusinessPresentationPlanError("v2 manager visuals are missing manager entries")
    visual_by_id = {entry["widget_id"]: entry for entry in entries}
    for entry in manager_entries:
        if not isinstance(entry, Mapping):
            raise BusinessPresentationPlanError("v2 manager entry is invalid")
        widget_id = _text(entry.get("widget_id"))
        if widget_id in manager_visual_set:
            visual = visual_by_id[widget_id]
            # Every visual manager entry must carry the exact chart projection
            # in addition to its immutable envelope.  A non-visual manager
            # entry retains its pointer-bound record projection.
            visual_keys = ("visual_type", "chart_family", "widget_snapshot_sha256", "chart_entry_sha256", "allowed_visual_fields", "title_projection", "visual_projection")
            for accepted_key in ("accepted_visual_pointer", "accepted_content_hash", "accepted_manifest_hash", "accepted_artifact_ref", "accepted_artifact_sha256"):
                if accepted_key in visual or accepted_key in entry:
                    visual_keys += (accepted_key,)
            if "recipe_id" in visual or "recipe_id" in entry:
                visual_keys += ("recipe_id",)
            if "layout" in visual or "layout" in entry:
                visual_keys += ("layout",)
            if "renderer_type" in visual or "renderer_type" in entry:
                visual_keys += ("renderer_type",)
            for key in visual_keys:
                if entry.get(key) != visual.get(key):
                    raise BusinessPresentationPlanError(f"v2 manager visual entry drifted: {widget_id}:{key}")
        else:
            # Non-visual manager entries retain the pointer-bound record
            # projection; they are not chart entries.
            _manager_entry_shape(entry)
    source_bindings = plan.get("source_bindings")
    if not isinstance(source_bindings, Mapping):
        raise BusinessPresentationPlanError("v2 source bindings are invalid")
    for key in ("fixture_ref", "fixture_sha256", "chart_map_ref", "chart_map_sha256"):
        if not isinstance(source_bindings.get(key), str) or not source_bindings[key].strip():
            raise BusinessPresentationPlanError(f"v2 source binding is missing: {key}")
    for key in ("fixture_sha256", "chart_map_sha256"):
        if not _is_sha256(source_bindings.get(key)):
            raise BusinessPresentationPlanError(f"v2 source binding hash is invalid: {key}")
    # V2 is the canonical declarative blueprint. It is valid as a direct root
    # contract; no predecessor is required. When an older V2 successor carries
    # an explicit predecessor envelope, validate that envelope as an integrity
    # check, but never require or synthesize one for a new root blueprint.
    if isinstance(source_bindings, Mapping) and "previous_plan_manager_widget_ids" in source_bindings:
        _v2_predecessor_plan_manager_contract(plan)
        _v2_predecessor_visual_contract(plan)


def _validate_business_presentation_plan_v2(
    context: RunContext,
    plan: Mapping[str, Any],
    *,
    fixture: Mapping[str, Any],
    fixture_ref: str,
    chart_map: Mapping[str, Any],
    chart_map_ref: str,
    widgets: Sequence[Mapping[str, Any]],
    strict_source_hash: bool = True,
) -> list[str]:
    """Validate V2 visual snapshots/projections against the candidate bytes."""

    _validate_presentation_plan_v2_shape(plan)
    source = plan["source_bindings"]
    fixture_path = context.resolve_run_path(fixture_ref)
    chart_map_path = context.resolve_run_path(chart_map_ref)
    if strict_source_hash and (source["fixture_ref"] != fixture_ref or source["chart_map_ref"] != chart_map_ref):
        raise BusinessPresentationPlanError("v2 visual source references drifted")
    if strict_source_hash and (_sha256_bytes(fixture_path.read_bytes()) != source["fixture_sha256"] or _sha256_bytes(chart_map_path.read_bytes()) != source["chart_map_sha256"]):
        raise BusinessPresentationPlanError("v2 visual source hashes drifted")
    raw_charts = chart_map.get("charts")
    if not isinstance(raw_charts, list):
        raise BusinessPresentationPlanError("v2 chart map charts are invalid")
    charts_by_id = {_text(value.get("id")): value for value in raw_charts if isinstance(value, Mapping)}
    widgets_by_id = {_text(widget.get("id")): widget for widget in widgets}
    if set(charts_by_id) != set(widgets_by_id):
        raise BusinessPresentationPlanError("v2 chart map/widget IDs drifted")
    visual_ids = set(plan["manager_visual_widget_ids"] + plan["audit_visual_widget_ids"])
    current_visual_ids = set(_true_visual_ids(widgets_by_id, charts_by_id))
    if current_visual_ids != visual_ids:
        raise BusinessPresentationPlanError("v2 visual partition does not cover the current fixture visual universe")
    # Recipe/layout choices are validated against the exact fixture/chart
    # containers and the committed registry before a site can be rendered.
    # Older private shape fixtures may omit the optional fields; current
    # writer-produced V2 plans always include them and therefore take this
    # strict path.
    registry: Mapping[str, Any] | None = None
    registry_ref = _text(chart_map.get("chart_registry_ref") or fixture.get("chart_registry_ref")).strip()
    if registry_ref:
        registry = _read_json(context, registry_ref, label="v2 visual chart registry")
    visual_by_id = {entry["widget_id"]: entry for entry in plan["visual_entries"]}
    for widget_id in visual_ids:
        widget = widgets_by_id.get(widget_id)
        chart = charts_by_id.get(widget_id)
        entry = visual_by_id.get(widget_id)
        if widget is None or chart is None or entry is None:
            raise BusinessPresentationPlanError(f"v2 visual widget is missing: {widget_id}")
        if _visual_snapshot_hash(widget) != entry["widget_snapshot_sha256"] or _visual_chart_hash(chart) != entry["chart_entry_sha256"]:
            raise BusinessPresentationPlanError(f"v2 visual snapshot/chart hash drifted: {widget_id}")
        if "recipe_id" in entry or "layout" in entry or "renderer_type" in entry:
            if registry is None:
                raise BusinessPresentationPlanError(f"v2 visual recipe registry is missing: {widget_id}")
            candidate = {
                "widget_id": widget_id,
                "chart_family": _text(chart.get("family")),
                "recipes": _dashboard_runtime().eligible_chart_recipes(widget, chart, registry),
            }
            try:
                selected_recipe, selected_layout, selected_renderer_type = _validated_plan_selection(candidate, entry)
            except BusinessPresentationPlanError:
                raise
            if (
                entry.get("recipe_id") != selected_recipe
                or entry.get("layout") != selected_layout
                or entry.get("renderer_type") != selected_renderer_type
            ):
                raise BusinessPresentationPlanError(f"v2 visual recipe/layout/renderer selection drifted: {widget_id}")
        if entry["requirement_id"] != _text(widget.get("requirement_id")) or entry["visual_type"] != _text(widget.get("type") or widget.get("kind")):
            raise BusinessPresentationPlanError(f"v2 visual identity drifted: {widget_id}")
        record_ids = sorted({
            _text(value).strip()
            for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
            if _text(value).strip()
        })
        if entry["record_ids"] != record_ids:
            raise BusinessPresentationPlanError(f"v2 visual record bindings drifted: {widget_id}")
        title_binding = entry["title_projection"]
        title_root = {"widget_snapshot": dict(widget)}
        title_value = _visual_pointer_value(title_root, title_binding["pointer"])
        if not _presentation_projection_value_equal(title_value, title_binding["value"]):
            raise BusinessPresentationPlanError(f"v2 visual title projection drifted: {widget_id}")
        chart_root = dict(chart)
        chart_root["title"] = widget.get("title")
        root = {"widget_snapshot": dict(widget), "chart_entry": chart_root}
        for field, binding in entry["visual_projection"].items():
            pointer = binding["pointer"]
            if pointer.startswith("/widget_snapshot"):
                actual = _visual_pointer_value(root, pointer)
            elif pointer.startswith("/chart_entry"):
                actual = _visual_pointer_value(root, pointer)
            else:
                raise BusinessPresentationPlanError(f"v2 visual projection pointer source is invalid: {widget_id}:{field}")
            if not _presentation_projection_value_equal(actual, binding["value"]):
                raise BusinessPresentationPlanError(f"v2 visual projection drifted: {widget_id}:{field}")
    return list(plan["manager_widget_ids"])


def _validate_v2_plan_lineage(
    context: RunContext,
    plan: Mapping[str, Any],
    *,
    generation_id: str,
    supervisor_ref: str,
    input_items: Sequence[Mapping[str, Any]],
    parent: Mapping[str, Any] | None,
) -> None:
    """Validate V2 lifecycle/input bindings before candidate staging."""

    _validate_presentation_plan_v2_shape(plan)
    if plan.get("run_id") != context.run_id or plan.get("generation_id") != generation_id:
        raise BusinessPresentationPlanError("v2 presentation plan run/generation binding is stale")
    supervisor_path = context.resolve_run_path(supervisor_ref)
    if plan.get("supervisor_plan_ref") != supervisor_ref or not supervisor_path.is_file() or _sha256_bytes(supervisor_path.read_bytes()) != plan.get("supervisor_plan_sha256"):
        raise BusinessPresentationPlanError("v2 supervisor plan binding drifted")
    planned = {value.get("item_id"): dict(value) for value in plan.get("input_items", []) if isinstance(value, Mapping)}
    current = {value.get("item_id"): dict(value) for value in input_items if isinstance(value, Mapping)}
    if len(planned) != len(plan.get("input_items", [])) or len(current) != len(input_items) or planned != current:
        raise BusinessPresentationPlanError("v2 accepted/committed input bindings drifted")
    if plan.get("parent") != parent:
        raise BusinessPresentationPlanError("v2 immediate parent lineage drifted")


def _load_business_presentation_plan(context: RunContext, reference: str | Path) -> tuple[Mapping[str, Any], str]:
    path = context.resolve_run_path(reference)
    if path.is_symlink() or not path.is_file():
        raise BusinessPresentationPlanError("presentation plan is missing or symlinked")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("presentation plan is invalid") from exc
    if not isinstance(raw, Mapping):
        raise BusinessPresentationPlanError("presentation plan must be an object")
    if raw.get("schema_version") != PRESENTATION_PLAN_V2_SCHEMA:
        # Older plan shapes are never migration inputs.  Keep the binding
        # phrase in the diagnostic so callers can classify a stale
        # accepted/committed contract; no legacy bytes are consumed or
        # rewritten.
        raise BusinessPresentationPlanError(
            "presentation plan must use V2 schema; accepted/committed input bindings drifted"
        )
    _validate_presentation_plan_v2_shape(raw)
    return dict(raw), _sha256_bytes(path.read_bytes())


def business_presentation_inventory(
    context: RunContext,
    *,
    fixture_ref: str | Path,
    generation_id: str | None = None,
    item_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return a read-only exact widget inventory for reviewer selection."""

    if not isinstance(context, RunContext):
        raise TypeError("business_presentation_inventory requires one RunContext")
    generation, metadata = _presentation_generation_metadata(context, generation_id)
    supervisor_ref, supervisor_plan, supervisor_hash = _presentation_supervisor_binding(context, generation, metadata)
    fixture = _read_json(context, fixture_ref, label="presentation inventory fixture")
    widgets = fixture.get("widgets")
    if not isinstance(widgets, list):
        raise BusinessPresentationPlanError("presentation inventory fixture widgets are invalid")
    # A preview inventory may intentionally cover only the currently
    # committed subset while later requirements remain non-terminal.  The
    # caller-provided IDs are still validated by the same safe discovery
    # boundary; omitting them retains the cumulative/final behaviour.
    item_order = _discover_item_ids(context, item_ids, supervisor_plan)
    input_items = _presentation_input_bindings(context, item_order)
    parent = _presentation_parent_binding(context, generation, metadata)
    # Build the authoritative record index once.  Inventory values are
    # suggestions only; a recorder revalidates every pointer and hash against
    # these immutable committed bytes before writing a plan.
    authoritative: dict[str, dict[str, Any]] = {}
    record_file_hashes: dict[str, str] = {}
    for item_id in item_order:
        state_probe = _read_json(context, f"requirements/{item_id}/item_state.json", label=f"{item_id} item state")
        if state_probe.get("lifecycle_state") == "technical_failure":
            # Pre-acceptance terminal failures intentionally have no accepted
            # bundle/records.  Their validated manifest is represented by the
            # limited empty-state widget, not by an invented inventory row.
            _load_item_technical_failure_manifest(context, item_id, state_probe)
            continue
        content, accepted_manifest, accepted_meta = _load_public_accepted_bundle(context, item_id)
        if state_probe.get("integration_state") == "technical_failure":
            _load_technical_failure_manifest(context, item_id, accepted_meta)
            records = []
        else:
            _integration_manifest, records = _load_committed_records(
                context,
                item_id,
                accepted_manifest,
                accepted_meta["bundle"],
            )
        if not records:
            continue
        records_ref = f"requirements/{item_id}/integration/committed/records.jsonl"
        records_path = context.resolve_run_path(records_ref)
        records_file_sha = _sha256_bytes(records_path.read_bytes())
        for record in records:
            record_id = _text(record.get("record_id")).strip()
            if not record_id:
                continue
            authoritative[record_id] = {
                "root": _presentation_authoritative_root(record, content),
                "record": record,
                "file_sha256": records_file_sha,
                "canonical_payload_sha256": _sha256_bytes(_canonical_bytes(_record_payload(record))),
            }
            record_file_hashes[record_id] = records_file_sha
    candidates = []
    for widget in widgets:
        if not isinstance(widget, Mapping) or not _text(widget.get("id")):
            continue
        binding = _presentation_widget_binding(widget)
        for field in ("title", "period", "as_of", "grain", "limitations", "x_label", "y_label"):
            if field in widget:
                binding[field] = copy.deepcopy(widget[field])
        binding["type"] = _text(widget.get("type") or widget.get("kind"))
        binding["value"] = widget.get("value") if "value" in widget else None
        binding["unit"] = widget.get("unit")
        binding["denominator"] = widget.get("denominator")
        if "scale_groups" in widget:
            binding["scale_groups"] = copy.deepcopy(widget.get("scale_groups"))
        binding["evidence_refs"] = list(_as_list(widget.get("evidence_refs")))
        binding["trace_refs"] = list(_as_list(widget.get("trace_refs")))
        binding["audit_payload"] = copy.deepcopy(widget.get("audit_payload"))
        # Product Agent quality metadata is descriptive only: all values and
        # source hashes still come from the exact fixture/widget binding.  No
        # derived semantic key or content score is used for selection.
        binding["technical_surface"] = _presentation_surface_is_technical(widget)
        if _text(widget.get("technical_surface_reason")).strip():
            binding["technical_surface_reason"] = _text(widget.get("technical_surface_reason"))
        # Geometry-less duplicate declarations are intentionally audit-only;
        # expose that deterministic marker to Product selection so an
        # explicit V2 request cannot re-promote them.
        if widget.get("no_geometry_fallback_duplicate") is True:
            binding["no_geometry_fallback_duplicate"] = True
        if widget.get("accepted_visual"):
            binding.update({
                "accepted_visual": True,
                "accepted_visual_index": widget.get("accepted_visual_index"),
                "accepted_visual_pointer": _text(widget.get("accepted_visual_pointer")),
                "accepted_visual_type": _text(widget.get("accepted_visual_type")),
                "accepted_content_hash": _text(widget.get("accepted_content_hash")),
                "accepted_manifest_hash": _text(widget.get("accepted_manifest_hash")),
            })
            for accepted_key in ("accepted_source_ref", "accepted_artifact_ref", "accepted_artifact_sha256"):
                accepted_value = _text(widget.get(accepted_key)).strip()
                if accepted_value:
                    binding[accepted_key] = accepted_value
        if widget.get("accepted_evidence"):
            # Accepted evidence is a first-class source-bound Product input,
            # even when integration produced no committed records.  Keep the
            # evidence ID/ref/hash in inventory metadata while exposing a
            # pointer-bound projection so the Product Agent can select the
            # table or fact sheet without interpreting opaque identifiers.
            binding.update({
                "accepted_evidence": True,
                "accepted_evidence_id": _text(widget.get("accepted_evidence_id")),
                "accepted_evidence_candidate_kind": _text(widget.get("accepted_evidence_candidate_kind")),
                "accepted_evidence_pointer": _text(widget.get("accepted_evidence_pointer")),
                "accepted_evidence_source_pointer": _text(widget.get("accepted_evidence_source_pointer")),
                "accepted_evidence_table_pointer": _text(widget.get("accepted_evidence_table_pointer")),
                "accepted_evidence_ref": _text(widget.get("accepted_evidence_ref")),
                "accepted_evidence_sha256": _text(widget.get("accepted_evidence_sha256")),
                "source_type": "accepted_evidence",
                "source_bound": bool(widget.get("source_bound")),
                "title": _text(widget.get("title") or widget.get("label")),
                "rows": copy.deepcopy(widget.get("rows")),
                "manager_rows": copy.deepcopy(widget.get("manager_rows")),
            })
            raw_payload = widget.get("audit_payload")
            evidence_ref = _text(widget.get("accepted_evidence_ref")).strip()
            evidence_sha = _text(widget.get("accepted_evidence_sha256")).strip()
            projection = _accepted_evidence_display_projection(widget)
            if isinstance(raw_payload, Mapping) and evidence_ref and _is_sha256(evidence_sha) and projection:
                # The ledger ref is the stable source record key.  Its file
                # hash and canonical row hash are exact source bindings, not
                # inferred integration identities.
                binding["record_id"] = evidence_ref
                binding["file_sha256"] = evidence_sha
                binding["canonical_payload_sha256"] = _sha256_bytes(_canonical_bytes(raw_payload))
                binding["display_projection"] = projection
        record_ids = binding["integration_record_ids"]
        if len(record_ids) == 1 and record_ids[0] in authoritative:
            details = authoritative[record_ids[0]]
            binding["record_id"] = record_ids[0]
            binding["file_sha256"] = details["file_sha256"]
            binding["canonical_payload_sha256"] = details["canonical_payload_sha256"]
            binding["authoritative_payload"] = copy.deepcopy(details["root"].get("payload", {}))
            binding["accepted_fields"] = copy.deepcopy(details["root"].get("accepted", {}))
            binding["display_projection"] = _candidate_display_projection(details["root"], details["record"])
        candidates.append(binding)
    # The read-only inventory carries both exact committed facts and the
    # executable recipe choices exposed by the canonical registry.  Recipes
    # are advisory; plan recording still revalidates every pointer/hash.
    design_inventory: dict[str, Any] | None = None
    chart_map_ref = _text(fixture.get("chart_map_ref")).strip()
    registry_ref = _text(fixture.get("chart_registry_ref")).strip()
    if chart_map_ref and registry_ref:
        try:
            chart_map = _read_json(context, chart_map_ref, label="presentation inventory chart map")
            registry = _read_json(context, registry_ref, label="presentation inventory chart registry")
            design_inventory = _dashboard_runtime().design_inventory(fixture, chart_map, registry)
            design_inventory["chart_map_ref"] = chart_map_ref
            design_inventory["chart_registry_ref"] = registry_ref
            recipes_by_id = {
                _text(entry.get("widget_id")): entry.get("recipes", [])
                for entry in design_inventory.get("visuals", [])
                if isinstance(entry, Mapping)
            }
            for candidate in candidates:
                candidate_chart_family = next(
                    (
                        chart.get("family")
                        for chart in _as_list(chart_map.get("charts"))
                        if isinstance(chart, Mapping)
                        and _text(chart.get("id")) == _text(candidate.get("widget_id"))
                    ),
                    "table",
                )
                candidate["chart_family"] = _text(candidate_chart_family, "table")
                candidate["recipes"] = copy.deepcopy(recipes_by_id.get(_text(candidate.get("widget_id")), []))
                # The inventory's first eligible recipe is a deterministic
                # semantic/data-shape default.  A Product Agent may replace
                # it with any other eligible ID when recording the plan.
                default_recipe = _default_recipe_id(candidate)
                if default_recipe:
                    candidate["recipe_id"] = default_recipe
                    candidate["layout"] = _dashboard_runtime().default_layout_for_recipe(default_recipe)
                    selected_recipe = next(
                        (
                            recipe for recipe in candidate.get("recipes", [])
                            if isinstance(recipe, Mapping) and _text(recipe.get("id")) == default_recipe
                        ),
                        None,
                    )
                    candidate["renderer_type"] = _text(
                        selected_recipe.get("default_renderer_type")
                        if isinstance(selected_recipe, Mapping)
                        else ""
                    ).strip() or "table"
        except (AssemblyError, OSError, ValueError, TypeError):
            # A manually authored pre-runtime fixture may not expose a chart
            # map/registry.  Keep its exact inventory usable; canonical V4
            # assembly always supplies both and therefore never takes this
            # fallback.
            design_inventory = None
    return {
        "schema_version": PRESENTATION_PLAN_V2_SCHEMA,
        "run_id": context.run_id,
        "generation_id": generation,
        "supervisor_plan_ref": supervisor_ref,
        "supervisor_plan_sha256": supervisor_hash,
        "item_order": item_order,
        "input_items": input_items,
        "parent": parent,
        "inventory_widget_count": len(candidates),
        "record_file_hashes": record_file_hashes,
        "candidates": candidates,
        "design_inventory": design_inventory,
    }


def _preflight_ref(generation_id: str, filename: str) -> str:
    """Return the deterministic non-product source-inventory reference."""

    if not re.fullmatch(r"G-[0-9]{4}", generation_id):
        raise BusinessPresentationPlanError("preflight generation_id is invalid")
    if filename not in {
        "dashboard_fixture_v4.json",
        "dashboard_chart_map_v4.json",
        "dashboard_chart_registry_v4.json",
        "preflight_manifest.json",
    }:
        raise BusinessPresentationPlanError("preflight filename is invalid")
    return f"extensions/{generation_id}/dashboard_preflight/{filename}"


def _validate_existing_preflight(
    context: RunContext,
    root: Path,
    manifest_ref: str,
    *,
    expected_input_fingerprint: str,
    expected_item_ids: Sequence[str],
    expected_rendering_identity: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Validate an existing preflight namespace before idempotent reuse.

    A malformed or tampered source inventory is never overwritten.  A valid
    inventory whose accepted/committed input fingerprint changed is returned
    as ``None`` so the caller can perform the narrowly-scoped refresh swap.
    """

    if not root.exists():
        return None
    if root.is_symlink() or not root.is_dir():
        raise BusinessPresentationPlanError("existing presentation preflight is not a regular directory")
    _assert_no_symlink_chain(context, manifest_ref, label="presentation preflight manifest")
    manifest_path = context.resolve_run_path(manifest_ref)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BusinessPresentationPlanError("existing presentation preflight manifest is missing or symlinked")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("existing presentation preflight manifest is invalid") from exc
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != "dashboard.presentation_preflight.v1":
        raise BusinessPresentationPlanError("existing presentation preflight manifest schema is invalid")
    if manifest.get("run_id") != context.run_id or manifest.get("item_ids") != list(expected_item_ids):
        # A valid old input set is replaceable only through the explicit
        # changed-fingerprint refresh path.  Keep this branch strict so a
        # caller cannot repurpose an arbitrary preflight directory.
        if manifest.get("run_id") != context.run_id:
            raise BusinessPresentationPlanError("existing presentation preflight run binding is stale")
    outputs = manifest.get("outputs")
    input_items = manifest.get("input_items")
    if not isinstance(outputs, Mapping) or not isinstance(input_items, list) or not _is_sha256(manifest.get("input_fingerprint")):
        raise BusinessPresentationPlanError("existing presentation preflight binding is invalid")
    expected_files = {
        "fixture": (outputs.get("fixture_ref"), outputs.get("fixture_sha256")),
        "chart_map": (outputs.get("chart_map_ref"), outputs.get("chart_map_sha256")),
        "chart_registry": (outputs.get("chart_registry_ref"), outputs.get("chart_registry_sha256")),
    }
    for label, (reference, expected_hash) in expected_files.items():
        if not isinstance(reference, str) or not reference or not _is_sha256(expected_hash):
            raise BusinessPresentationPlanError(f"existing presentation preflight {label} binding is invalid")
        _assert_no_symlink_chain(context, reference, label=f"presentation preflight {label}")
        path = context.resolve_run_path(reference)
        if path.is_symlink() or not path.is_file() or _sha256_bytes(path.read_bytes()) != expected_hash:
            raise BusinessPresentationPlanError(f"existing presentation preflight {label} hash mismatch")
    if (
        manifest.get("input_fingerprint") == expected_input_fingerprint
        and manifest.get("item_ids") == list(expected_item_ids)
        and manifest.get("rendering_identity") == dict(expected_rendering_identity)
    ):
        fixture_ref = expected_files["fixture"][0]
        chart_map_ref = expected_files["chart_map"][0]
        inventory = business_presentation_inventory(
            context,
            fixture_ref=fixture_ref,
            generation_id=_text(manifest.get("generation_id")),
            item_ids=expected_item_ids,
        )
        return {"manifest": dict(manifest), "inventory": inventory}
    return None


def business_presentation_preflight(
    context: RunContext,
    *,
    item_ids: Sequence[str],
    generation_id: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic read-only source inventory before final assembly.

    The helper stages the exact accepted/committed fixture, chart map, and
    registry without rendering a site or publishing a product namespace.  It
    atomically refreshes only ``extensions/<G>/dashboard_preflight`` when the
    input bindings change, and returns the inventory that a Product Agent may
    use to author explicit V2 recipe/layout/renderer choices.
    """

    if not isinstance(context, RunContext):
        raise TypeError("business_presentation_preflight requires one RunContext")
    generation, _metadata = _presentation_generation_metadata(context, generation_id)
    # Validate IDs up front using the same boundary as canonical assembly.
    plan = _load_plan(context, None)
    # Preflight is a source-inventory cache, not a caller-order presentation
    # request.  Canonicalize the validated set before assembling its fixture,
    # input bindings, and fingerprint so probes with a different item order
    # are true idempotent cache hits rather than semantic rewrites.
    selected_ids = sorted(_discover_item_ids(context, item_ids, plan))
    # The private assembler branch builds source bytes only.  Its temporary
    # product staging directory is deleted before it returns.
    preflight_output = Path("generations") / generation / ".dashboard-preflight"
    result = assemble_dashboard(
        context,
        output_dir=preflight_output,
        item_ids=selected_ids,
        _preflight_only=True,
    )
    fixture = copy.deepcopy(result.get("fixture"))
    chart_map = copy.deepcopy(result.get("chart_map"))
    registry = copy.deepcopy(result.get("registry"))
    input_items = copy.deepcopy(result.get("input_items"))
    rendering_identity = copy.deepcopy(result.get("rendering_identity"))
    input_fingerprint = _text(result.get("input_fingerprint")).strip()
    if not isinstance(fixture, Mapping) or not isinstance(chart_map, Mapping) or not isinstance(registry, Mapping):
        raise BusinessPresentationPlanError("preflight source builder returned invalid inputs")
    if not isinstance(input_items, list) or not isinstance(rendering_identity, Mapping) or not _is_sha256(input_fingerprint):
        raise BusinessPresentationPlanError("preflight input binding is invalid")

    fixture_ref = _preflight_ref(generation, "dashboard_fixture_v4.json")
    chart_map_ref = _preflight_ref(generation, "dashboard_chart_map_v4.json")
    registry_ref = _preflight_ref(generation, "dashboard_chart_registry_v4.json")
    manifest_ref = _preflight_ref(generation, "preflight_manifest.json")
    root = context.resolve_run_path(Path(manifest_ref).parent)
    for reference, label in (
        (fixture_ref, "presentation preflight fixture"),
        (chart_map_ref, "presentation preflight chart map"),
        (registry_ref, "presentation preflight chart registry"),
        (manifest_ref, "presentation preflight manifest"),
    ):
        _assert_no_symlink_chain(context, reference, label=label)
    root.parent.mkdir(parents=True, exist_ok=True)
    existing = _validate_existing_preflight(
        context,
        root,
        manifest_ref,
        expected_input_fingerprint=input_fingerprint,
        expected_item_ids=selected_ids,
        expected_rendering_identity=rendering_identity,
    )
    if existing is not None:
        manifest = existing["manifest"]
        inventory = existing["inventory"]
        return {
            **dict(manifest.get("outputs", {})),
            "schema_version": PRESENTATION_PLAN_V2_SCHEMA,
            "run_id": context.run_id,
            "generation_id": generation,
            "item_ids": list(selected_ids),
            "input_items": input_items,
            "input_fingerprint": input_fingerprint,
            "rendering_identity": copy.deepcopy(manifest.get("rendering_identity")),
            "inventory": inventory,
        }

    # Bind the copied source references to their deterministic extension
    # namespace before hashing.  No site/receipt bytes are emitted here.
    fixture = dict(fixture)
    chart_map = dict(chart_map)
    fixture["chart_registry_ref"] = registry_ref
    fixture["chart_map_ref"] = chart_map_ref
    chart_map["chart_registry_ref"] = registry_ref
    chart_map["fixture_ref"] = fixture_ref
    fixture_payload = _canonical_bytes(fixture)
    chart_map_payload = _canonical_bytes(chart_map)
    registry_payload = _canonical_bytes(registry)
    outputs = {
        "fixture_ref": fixture_ref,
        "fixture_sha256": _sha256_bytes(fixture_payload),
        "chart_map_ref": chart_map_ref,
        "chart_map_sha256": _sha256_bytes(chart_map_payload),
        "chart_registry_ref": registry_ref,
        "chart_registry_sha256": _sha256_bytes(registry_payload),
    }
    manifest = {
        "schema_version": "dashboard.presentation_preflight.v1",
        "run_id": context.run_id,
        "generation_id": generation,
        "item_ids": list(selected_ids),
        "input_items": input_items,
        "input_fingerprint": input_fingerprint,
        "rendering_identity": copy.deepcopy(rendering_identity),
        "outputs": outputs,
    }
    manifest_payload = _canonical_bytes(manifest)
    staging = root.parent / f".{root.name}.staging"
    if staging.exists() or staging.is_symlink():
        raise BusinessPresentationPlanError("presentation preflight staging namespace already exists")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        (staging / "dashboard_fixture_v4.json").write_bytes(fixture_payload)
        (staging / "dashboard_chart_map_v4.json").write_bytes(chart_map_payload)
        (staging / "dashboard_chart_registry_v4.json").write_bytes(registry_payload)
        (staging / "preflight_manifest.json").write_bytes(manifest_payload)
        backup = root.parent / f".{root.name}.previous"
        if backup.exists() or backup.is_symlink():
            raise BusinessPresentationPlanError("presentation preflight backup namespace already exists")
        moved = False
        published = False
        try:
            if root.exists() or root.is_symlink():
                os.replace(root, backup)
                moved = True
            os.replace(staging, root)
            published = True
        except BaseException as original:
            # A signal can arrive after either atomic rename, including
            # between ``os.replace`` returning and the state assignment above.
            # Derive the durable boundary from the three sibling paths so the
            # original interruption is preserved while the namespace is left
            # in a deterministic recoverable state.
            moved = moved or (
                (backup.exists() or backup.is_symlink())
                and not (root.exists() or root.is_symlink())
            )
            candidate_published = (
                (root.exists() or root.is_symlink())
                and not (staging.exists() or staging.is_symlink())
            )
            if candidate_published:
                published = True
            elif moved and not (root.exists() or root.is_symlink()) and (
                backup.exists() or backup.is_symlink()
            ):
                restore_error: BaseException | None = None
                try:
                    os.replace(backup, root)
                except BaseException as exc:
                    restore_error = exc
                if restore_error is not None:
                    # Keep the original interruption visible.  Retain the
                    # backup for explicit recovery if restoration itself was
                    # interrupted; the outer cleanup still removes staging.
                    raise original from restore_error
            raise
        finally:
            if published and moved and backup.exists() and backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    inventory = business_presentation_inventory(
        context,
        fixture_ref=fixture_ref,
        generation_id=generation,
        item_ids=selected_ids,
    )
    return {
        **outputs,
        "schema_version": PRESENTATION_PLAN_V2_SCHEMA,
        "run_id": context.run_id,
        "generation_id": generation,
        "item_ids": list(selected_ids),
        "input_items": input_items,
        "input_fingerprint": input_fingerprint,
        "rendering_identity": copy.deepcopy(rendering_identity),
        "inventory": inventory,
    }


def business_presentation_visual_inventory(
    context: RunContext,
    *,
    fixture_ref: str | Path,
    chart_map_ref: str | Path | None = None,
) -> dict[str, Any]:
    """Return exact chart snapshots and map projections for V2 review.

    The inventory is read-only.  It exposes only the existing fixture/chart
    map bytes and their canonical hashes; a V2 writer revalidates these values
    immediately before recording a plan.
    """

    fixture_path = context.resolve_run_path(fixture_ref)
    if fixture_path.is_symlink() or not fixture_path.is_file():
        raise BusinessPresentationPlanError("v2 visual inventory fixture is missing or symlinked")
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("v2 visual inventory fixture is invalid") from exc
    if not isinstance(fixture, Mapping) or not isinstance(fixture.get("widgets"), list):
        raise BusinessPresentationPlanError("v2 visual inventory fixture widgets are invalid")
    resolved_chart_ref = _text(chart_map_ref or fixture.get("chart_map_ref")).strip()
    if not resolved_chart_ref:
        raise BusinessPresentationPlanError("v2 visual inventory requires chart_map_ref")
    chart_path = context.resolve_run_path(resolved_chart_ref)
    if chart_path.is_symlink() or not chart_path.is_file():
        raise BusinessPresentationPlanError("v2 visual inventory chart map is missing or symlinked")
    try:
        chart_map = json.loads(chart_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("v2 visual inventory chart map is invalid") from exc
    if not isinstance(chart_map, Mapping) or not isinstance(chart_map.get("charts"), list):
        raise BusinessPresentationPlanError("v2 visual inventory chart map charts are invalid")
    widgets_by_id = {
        _text(widget.get("id")): widget
        for widget in fixture["widgets"]
        if isinstance(widget, Mapping) and _text(widget.get("id"))
    }
    charts_by_id = {
        _text(chart.get("id")): chart
        for chart in chart_map["charts"]
        if isinstance(chart, Mapping) and _text(chart.get("id"))
    }
    if set(widgets_by_id) != set(charts_by_id):
        raise BusinessPresentationPlanError("v2 visual inventory fixture/chart map IDs drifted")
    registry_ref = _text(fixture.get("chart_registry_ref")).strip()
    registry: Mapping[str, Any] | None = None
    design: Mapping[str, Any] | None = None
    if registry_ref:
        registry = _read_json(context, registry_ref, label="v2 visual inventory chart registry")
        design = _dashboard_runtime().design_inventory(fixture, chart_map, registry)
    recipes_by_id = {
        _text(value.get("widget_id")): value.get("recipes", [])
        for value in _as_list((design or {}).get("visuals"))
        if isinstance(value, Mapping) and _text(value.get("widget_id"))
    }
    visual_ids = _true_visual_ids(widgets_by_id, charts_by_id)
    fixture_manager_visual = fixture.get("manager_visual_widget_ids")
    fixture_audit_visual = fixture.get("audit_visual_widget_ids")
    if (
        isinstance(fixture_manager_visual, list)
        and isinstance(fixture_audit_visual, list)
        and set(fixture_manager_visual).union(fixture_audit_visual) == set(visual_ids)
        and not set(fixture_manager_visual).intersection(fixture_audit_visual)
    ):
        manager_visual_ids = list(fixture_manager_visual)
        audit_visual_ids = list(fixture_audit_visual)
    else:
        # A planless candidate has no audience authority yet.  Inventory all
        # true visuals in deterministic fixture order; the successor writer
        # applies the predecessor partition and explicitly admits only the
        # reviewed additions.
        manager_visual_ids = list(visual_ids)
        audit_visual_ids = []
    entries: list[dict[str, Any]] = []
    for widget_id in manager_visual_ids + audit_visual_ids:
        widget = widgets_by_id.get(widget_id)
        chart = charts_by_id.get(widget_id)
        if widget is None or chart is None:
            raise BusinessPresentationPlanError(f"v2 visual inventory widget is missing: {widget_id}")
        kind = _text(widget.get("type") or widget.get("kind")).strip()
        reviewed_table_fact = (
            bool(
                widget.get("dashboard_fact")
                or widget.get("limited_empty_state")
                or widget.get("accepted_visual")
                or widget.get("accepted_evidence")
            )
            and kind == "table"
        )
        if (kind in {"status_table", ""} or (kind == "table" and not reviewed_table_fact) or _text(chart.get("type")) != kind):
            raise BusinessPresentationPlanError(f"v2 visual inventory widget type is invalid: {widget_id}")
        fields = chart.get("fields_or_values_used")
        if not isinstance(fields, Mapping):
            raise BusinessPresentationPlanError(f"v2 visual inventory chart fields are invalid: {widget_id}")
        record_ids = sorted({
            _text(value).strip()
            for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
            if _text(value).strip()
        })
        visual_projection: dict[str, dict[str, Any]] = {
            "type": {"pointer": "/chart_entry/type", "value": copy.deepcopy(chart.get("type"))},
            "family": {"pointer": "/chart_entry/family", "value": copy.deepcopy(chart.get("family"))},
        }
        for field, value in fields.items():
            # Presentation role/tier are plan-derived envelope metadata, not
            # visual geometry or a reviewed value.  Excluding them keeps
            # fixture rebinding stable while every chart field remains
            # pointer-bound and exact.
            if field in {"presentation_role", "presentation_tier"}:
                continue
            visual_projection[field] = {
                "pointer": f"/chart_entry/fields_or_values_used/{_presentation_pointer_escape(field)}",
                "value": copy.deepcopy(value),
            }
        audience = "business_manager" if widget_id in manager_visual_ids else "technical_audit_gallery"
        entry = {
            "widget_id": widget_id,
            "requirement_id": _text(widget.get("requirement_id")),
            "record_ids": record_ids,
            "presentation_audience": audience,
            "visual_type": kind,
            "chart_family": _text(chart.get("family")),
            "widget_snapshot_sha256": _visual_snapshot_hash(widget),
            "chart_entry_sha256": _visual_chart_hash(chart),
            "allowed_visual_fields": list(visual_projection),
            "title_projection": {
                "pointer": "/widget_snapshot/title",
                "value": copy.deepcopy(widget.get("title") or widget.get("label") or widget_id),
            },
            "visual_projection": visual_projection,
        }
        if widget.get("accepted_visual"):
            entry.update({
                "accepted_visual_pointer": _text(widget.get("accepted_visual_pointer")),
                "accepted_content_hash": _text(widget.get("accepted_content_hash")),
                "accepted_manifest_hash": _text(widget.get("accepted_manifest_hash")),
            })
            if widget.get("accepted_artifact_ref"):
                entry["accepted_artifact_ref"] = _text(widget.get("accepted_artifact_ref"))
                entry["accepted_artifact_sha256"] = _text(widget.get("accepted_artifact_sha256"))
        if registry is not None:
            candidate = {
                "widget_id": widget_id,
                "chart_family": _text(chart.get("family")),
                "recipes": recipes_by_id.get(widget_id, []),
            }
            recipe_id = _default_recipe_id(candidate)
            if recipe_id:
                entry["recipe_id"] = recipe_id
                entry["layout"] = _dashboard_runtime().default_layout_for_recipe(recipe_id)
                selected_recipe = next(
                    (
                        recipe for recipe in recipes_by_id.get(widget_id, [])
                        if isinstance(recipe, Mapping) and _text(recipe.get("id")) == recipe_id
                    ),
                    None,
                )
                entry["renderer_type"] = _text(
                    selected_recipe.get("default_renderer_type")
                    if isinstance(selected_recipe, Mapping)
                    else ""
                ).strip() or "table"
        entries.append(entry)
    try:
        fixture_run_ref = fixture_path.relative_to(context.run_root).as_posix()
        chart_run_ref = chart_path.relative_to(context.run_root).as_posix()
    except ValueError as exc:
        raise BusinessPresentationPlanError("v2 visual inventory source escaped the run root") from exc
    return {
        "schema_version": PRESENTATION_PLAN_V2_SCHEMA,
        "fixture_ref": fixture_run_ref,
        "fixture_sha256": _sha256_bytes(fixture_path.read_bytes()),
        "chart_map_ref": chart_run_ref,
        "chart_map_sha256": _sha256_bytes(chart_path.read_bytes()),
        "fixture_widget_count": len(widgets_by_id),
        "visual_widget_count": len(entries),
        "all_visual_widget_ids": list(visual_ids),
        "manager_visual_widget_ids": manager_visual_ids,
        "audit_visual_widget_ids": audit_visual_ids,
        "visual_entries": entries,
        "design_inventory": design,
    }


def write_business_presentation_plan_v2(
    context: RunContext,
    *,
    fixture_ref: str | Path,
    chart_map_ref: str | Path | None = None,
    previous_plan_ref: str | Path,
    manager_entries: Sequence[Mapping[str, Any]],
    reviewer_ref: str,
    presentation_plan_ref: str | Path | None = None,
    _lock_held: bool = False,
    presentation: Mapping[str, Any] | None = None,
    item_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a canonical V2 plan candidate without mutating a live plan.

    The returned object is suitable for :func:`revise_business_presentation_plan_v2`;
    callers may write it to a temporary path for independent review first.
    """

    old_plan, old_hash = _load_business_presentation_plan(context, previous_plan_ref)
    if old_plan.get("schema_version") != PRESENTATION_PLAN_V2_SCHEMA:
        raise BusinessPresentationPlanError("v2 successor requires a V2 predecessor plan")

    generation_id, metadata = _presentation_generation_metadata(context, _lock_held=_lock_held)
    supervisor_ref, supervisor_plan, supervisor_hash = _presentation_supervisor_binding(context, generation_id, metadata)
    current_item_order = _discover_item_ids(context, item_ids, supervisor_plan)
    current_input_items = _presentation_input_bindings(context, current_item_order)
    current_parent = _presentation_parent_binding(context, generation_id, metadata)

    # Reuse the initial V2 inventory/selection policy, but require the
    # successor caller to make every visual presentation choice explicitly.
    inventory = business_presentation_inventory(
        context,
        fixture_ref=fixture_ref,
        generation_id=generation_id,
        item_ids=item_ids,
    )
    visual_inventory = business_presentation_visual_inventory(
        context,
        fixture_ref=fixture_ref,
        chart_map_ref=chart_map_ref or _text((inventory.get("design_inventory") or {}).get("chart_map_ref")) or None,
    )
    selection = _v2_manager_selection(
        inventory,
        visual_inventory,
        manager_entries,
        require_explicit_visual_choices=True,
    )

    # Keep the predecessor's source/widget/chart/value bindings immutable while
    # allowing the new selection to change visual audience, membership/order,
    # and recipe/layout/renderer choices.
    previous_manager_visual_ids = list(old_plan.get("manager_visual_widget_ids") or [])
    previous_audit_visual_ids = list(old_plan.get("audit_visual_widget_ids") or [])
    previous_visual_entries = [copy.deepcopy(dict(entry)) for entry in old_plan.get("visual_entries", [])]
    current_visual_by_id = {
        _text(entry.get("widget_id")): entry
        for entry in selection["visual_entries"]
        if isinstance(entry, Mapping)
    }
    previous_visual_by_id = {
        _text(entry.get("widget_id")): entry
        for entry in previous_visual_entries
        if isinstance(entry, Mapping)
    }
    for widget_id, predecessor in previous_visual_by_id.items():
        current = current_visual_by_id.get(widget_id)
        if not isinstance(current, Mapping):
            raise BusinessPresentationPlanError(f"v2 successor omits predecessor visual: {widget_id}")
        for field in (
            "requirement_id", "record_ids", "visual_type", "chart_family",
            "widget_snapshot_sha256", "chart_entry_sha256", "allowed_visual_fields",
            "title_projection", "visual_projection", "accepted_visual_pointer",
            "accepted_content_hash", "accepted_manifest_hash", "accepted_artifact_ref",
            "accepted_artifact_sha256",
        ):
            if field in predecessor and current.get(field) != predecessor.get(field):
                raise BusinessPresentationPlanError(f"v2 predecessor visual drifted: {widget_id}:{field}")

    source_bindings = {
        "fixture_ref": visual_inventory["fixture_ref"],
        "fixture_sha256": visual_inventory["fixture_sha256"],
        "chart_map_ref": visual_inventory["chart_map_ref"],
        "chart_map_sha256": visual_inventory["chart_map_sha256"],
        "previous_plan_ref": Path(previous_plan_ref).as_posix(),
        "previous_plan_sha256": old_hash,
        "previous_manager_visual_widget_ids": previous_manager_visual_ids,
        "previous_audit_visual_widget_ids": previous_audit_visual_ids,
        "previous_visual_entries": copy.deepcopy(previous_visual_entries),
        "previous_plan_manager_widget_ids": [
            _text(entry.get("widget_id"))
            for entry in old_plan.get("manager_entries", [])
            if isinstance(entry, Mapping)
        ],
        "previous_plan_manager_entries": copy.deepcopy(old_plan.get("manager_entries", [])),
    }
    plan = {
        "schema_version": PRESENTATION_PLAN_V2_SCHEMA,
        "run_id": context.run_id,
        "generation_id": generation_id,
        "supervisor_plan_ref": supervisor_ref,
        "supervisor_plan_sha256": supervisor_hash,
        "item_order": current_item_order,
        "input_items": copy.deepcopy(current_input_items),
        "parent": copy.deepcopy(current_parent),
        "reviewer_ref": _text(reviewer_ref).strip(),
        "manager_widget_ids": selection["manager_widget_ids"],
        "manager_entries": selection["manager_entries"],
        "manager_visual_widget_ids": selection["manager_visual_widget_ids"],
        "audit_visual_widget_ids": selection["audit_visual_widget_ids"],
        "visual_entries": selection["visual_entries"],
        "source_bindings": source_bindings,
    }
    if presentation is not None:
        plan["presentation"] = _dashboard_runtime().validate_presentation_copy(
            presentation, widget_ids=plan["manager_widget_ids"], requirement_ids=plan["item_order"])
    elif "presentation" in old_plan:
        plan["presentation"] = copy.deepcopy(old_plan["presentation"])
    _validate_presentation_plan_v2_shape(plan)
    return plan


def _revise_business_presentation_plan_v2_unlocked(
    context: RunContext,
    *,
    successor_plan: Mapping[str, Any] | str | Path,
    expected_current_plan_sha256: str,
    expected_successor_plan_sha256: str,
    presentation_plan_ref: str | Path,
) -> dict[str, Any]:
    """CAS-replace one V2 blueprint with a validated V2 successor atomically.

    This public mutation is intentionally narrow: it accepts exactly the
    expected current V2 bytes and expected canonical successor bytes.  A
    retry against already-published successor bytes is idempotent; any other
    current/successor combination fails before touching the plan file.
    """

    if not _is_sha256(expected_current_plan_sha256) or not _is_sha256(expected_successor_plan_sha256):
        raise BusinessPresentationPlanError("v2 revision expected hashes are invalid")
    path_ref = _presentation_plan_ref(context, _text(successor_plan.get("generation_id")) if isinstance(successor_plan, Mapping) else _active_generation_id(context), presentation_plan_ref)
    path = context.resolve_run_path(path_ref)
    if path.is_symlink() or not path.is_file():
        raise BusinessPresentationPlanError("v2 revision current plan is missing or symlinked")
    if isinstance(successor_plan, (str, Path)):
        successor_path = Path(successor_plan)
        if successor_path.is_symlink() or not successor_path.is_file():
            raise BusinessPresentationPlanError("v2 successor plan is missing or symlinked")
        successor_bytes = successor_path.read_bytes()
        try:
            candidate = json.loads(successor_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessPresentationPlanError("v2 successor plan is invalid") from exc
    elif isinstance(successor_plan, Mapping):
        candidate = dict(successor_plan)
        successor_bytes = _canonical_bytes(candidate)
    else:
        raise BusinessPresentationPlanError("v2 successor plan must be an object or JSON path")
    if not isinstance(candidate, Mapping):
        raise BusinessPresentationPlanError("v2 successor plan must be an object")
    _validate_presentation_plan_v2_shape(candidate)
    canonical_successor_bytes = _canonical_bytes(candidate)
    if successor_bytes != canonical_successor_bytes:
        raise BusinessPresentationPlanError("v2 successor plan is not canonical")
    if _sha256_bytes(successor_bytes) != expected_successor_plan_sha256:
        raise BusinessPresentationPlanError("v2 successor plan hash does not match expected hash")
    current_bytes = path.read_bytes()
    current_hash = _sha256_bytes(current_bytes)
    if current_hash == expected_successor_plan_sha256:
        if current_bytes != successor_bytes:
            raise BusinessPresentationPlanError("v2 successor hash matches but bytes differ")
        return dict(candidate)
    if current_hash != expected_current_plan_sha256:
        raise BusinessPresentationPlanError("v2 revision current plan hash does not match expected V2")
    try:
        current = json.loads(current_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("v2 revision current plan is invalid") from exc
    if not isinstance(current, Mapping) or current.get("schema_version") != PRESENTATION_PLAN_V2_SCHEMA:
        raise BusinessPresentationPlanError("v2 revision current plan must be V2")
    source_bindings = candidate.get("source_bindings")
    if not isinstance(source_bindings, Mapping):
        raise BusinessPresentationPlanError("v2 successor source bindings are missing")
    predecessor_ids = source_bindings.get("previous_plan_manager_widget_ids")
    predecessor_entries = source_bindings.get("previous_plan_manager_entries")
    if not isinstance(predecessor_ids, list) or not isinstance(predecessor_entries, list):
        raise BusinessPresentationPlanError("v2 successor previous-plan manager binding is missing")
    current_ids = current.get("manager_widget_ids")
    current_entries = current.get("manager_entries")
    # The successor must bind the actual predecessor bytes, but it is allowed
    # to choose a wholly new manager membership/order.  Compare the source
    # envelope with the currently installed predecessor, not with the
    # successor's newly selected manager entries.
    if current_ids != predecessor_ids or current_entries != predecessor_entries:
        raise BusinessPresentationPlanError("v2 successor previous-plan manager binding is not the actual predecessor")
    predecessor_visual_bindings = {
        "previous_manager_visual_widget_ids": current.get("manager_visual_widget_ids"),
        "previous_audit_visual_widget_ids": current.get("audit_visual_widget_ids"),
        "previous_visual_entries": current.get("visual_entries"),
    }
    if any(source_bindings.get(key) != value for key, value in predecessor_visual_bindings.items()):
        raise BusinessPresentationPlanError("v2 successor previous-plan visual binding is not the actual predecessor")
    previous_ref = Path(_text(source_bindings.get("previous_plan_ref"))).as_posix()
    if previous_ref != path_ref or source_bindings.get("previous_plan_sha256") != expected_current_plan_sha256:
        raise BusinessPresentationPlanError("v2 successor previous-plan CAS binding is invalid")
    fixture_ref = _text(source_bindings.get("fixture_ref"))
    chart_map_ref = _text(source_bindings.get("chart_map_ref"))
    if not fixture_ref or not chart_map_ref:
        raise BusinessPresentationPlanError("v2 successor visual source bindings are missing")
    fixture_path = context.resolve_run_path(fixture_ref)
    chart_map_path = context.resolve_run_path(chart_map_ref)
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        chart_map = json.loads(chart_map_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("v2 successor visual source is invalid") from exc
    if not isinstance(fixture, Mapping) or not isinstance(chart_map, Mapping) or not isinstance(fixture.get("widgets"), list):
        raise BusinessPresentationPlanError("v2 successor visual source shape is invalid")
    _validate_business_presentation_plan_v2(
        context,
        candidate,
        fixture=fixture,
        fixture_ref=fixture_ref,
        chart_map=chart_map,
        chart_map_ref=chart_map_ref,
        widgets=[widget for widget in fixture["widgets"] if isinstance(widget, Mapping)],
        strict_source_hash=True,
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.v2.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(successor_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(candidate)


def revise_business_presentation_plan_v2(
    context: RunContext,
    *,
    successor_plan: Mapping[str, Any] | str | Path,
    expected_current_plan_sha256: str,
    expected_successor_plan_sha256: str,
    presentation_plan_ref: str | Path,
) -> dict[str, Any]:
    """CAS-replace the current V2 plan with one validated V2 successor under run lock.

    The compare/read/validate/replace sequence is serialized with dashboard
    delta and lifecycle publication through the same run lock.  This keeps a
    concurrent generation admission from racing a plan CAS while preserving
    the public function's exact-retry semantics.
    """

    try:
        from auto_foundry_core.lifecycle import RunLifecycle
    except ModuleNotFoundError as exc:  # pragma: no cover - package guard
        raise BusinessPresentationPlanError("RunLifecycle is unavailable for plan revision") from exc
    with RunLifecycle._run_lock(context):  # noqa: SLF001 - publication CAS boundary
        return _revise_business_presentation_plan_v2_unlocked(
            context,
            successor_plan=successor_plan,
            expected_current_plan_sha256=expected_current_plan_sha256,
            expected_successor_plan_sha256=expected_successor_plan_sha256,
            presentation_plan_ref=presentation_plan_ref,
        )


def _direct_v2_reference(
    context: RunContext,
    reference: str | Path,
    *,
    expected: str,
    label: str,
) -> tuple[str, Path]:
    """Validate one canonical generation-scoped input reference.

    The direct recorder intentionally accepts only the active generation's
    canonical fixture/map, its immediate predecessor plan, and its own plan
    target.  This keeps the public mutation path deterministic and prevents a
    caller from selecting an unrelated run-relative file while still using
    the normal run-root containment checks.
    """

    value = _text(reference).strip()
    if not value or "\\" in value or "\x00" in value:
        raise BusinessPresentationPlanError(f"{label} reference is invalid")
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts or raw.as_posix() != expected:
        raise BusinessPresentationPlanError(f"{label} reference is not canonical: {value}")

    # ``RunContext.resolve_run_path`` returns a resolved path.  Checking only
    # that result would therefore miss an in-root symlink (the resolved path
    # is no longer itself a symlink).  Walk the lexical path first so the
    # direct publication boundary cannot read through or replace a symlinked
    # source/target component.
    lexical = context.run_root.joinpath(*raw.parts)
    current = context.run_root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise BusinessPresentationPlanError(f"{label} cannot be symlinked: {expected}")
    path = context.resolve_run_path(expected)
    if path != lexical or not path.is_file():
        raise BusinessPresentationPlanError(f"{label} is missing or symlinked: {expected}")
    return expected, path


def _direct_v2_target_path(
    context: RunContext,
    reference: str,
    *,
    expected: str,
) -> Path:
    """Resolve the canonical V2 target while preserving lexical symlink checks."""

    raw = Path(reference)
    if raw.is_absolute() or ".." in raw.parts or raw.as_posix() != expected:
        raise BusinessPresentationPlanError(f"direct V2 target must be canonical: {reference}")
    lexical = context.run_root.joinpath(*raw.parts)
    current = context.run_root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise BusinessPresentationPlanError("direct V2 target cannot be symlinked")
    resolved = context.resolve_run_path(reference)
    if resolved != lexical:
        raise BusinessPresentationPlanError("direct V2 target escaped the run root")
    return resolved


def _atomic_replace_plan_bytes(path: Path, payload: bytes) -> None:
    """Atomically publish bytes and restore the prior target on post-swap failure."""

    previous_exists = path.exists() or path.is_symlink()
    previous_bytes = path.read_bytes() if previous_exists and path.is_file() and not path.is_symlink() else None
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.v2.record.", dir=path.parent)
    temporary = Path(temporary_name)
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        replaced = True
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        # The target write is the only mutation in this helper.  If the
        # directory durability step fails after replace, restore the exact
        # previous bytes (or remove a newly-created target) before re-raising.
        if replaced:
            try:
                if previous_bytes is None:
                    if path.is_file() and not path.is_symlink() and path.read_bytes() == payload:
                        path.unlink()
                else:
                    restore_fd, restore_name = tempfile.mkstemp(prefix=f".{path.name}.v2.restore.", dir=path.parent)
                    restore = Path(restore_name)
                    try:
                        with os.fdopen(restore_fd, "wb") as stream:
                            stream.write(previous_bytes)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(restore, path)
                    finally:
                        restore.unlink(missing_ok=True)
            except OSError:
                # Preserve the original durability error; callers must not
                # mistake a failed rollback for a successful publication.
                pass
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _record_business_presentation_plan_v2_unlocked(
    context: RunContext,
    *,
    fixture_ref: str | Path,
    chart_map_ref: str | Path,
    previous_plan_ref: str | Path,
    manager_entries: Sequence[Mapping[str, Any]],
    reviewer_ref: str,
    expected_fixture_sha256: str,
    expected_chart_map_sha256: str,
    expected_previous_plan_sha256: str,
    expected_successor_plan_sha256: str,
    presentation_plan_ref: str | Path | None,
) -> dict[str, Any]:
    """Create the active generation's absent V2 plan under the held run lock."""

    ensure_valid = getattr(context, "ensure_valid", None)
    if callable(ensure_valid):
        ensure_valid()
    try:
        from auto_foundry_core.lifecycle import RunLifecycle

        pointer = RunLifecycle._read_generation_pointer_unlocked(context)  # noqa: SLF001 - lock is held here
        if pointer is None:
            raise BusinessPresentationPlanError("direct V2 recording requires an active generation")
        metadata = RunLifecycle._load_generation_unlocked(context, pointer)  # noqa: SLF001 - lock is held here
    except BusinessPresentationPlanError:
        raise
    except Exception as exc:
        raise BusinessPresentationPlanError("active generation validation failed") from exc

    generation_id = _text(getattr(metadata, "generation_id", "")).strip()
    parent_generation_id = _text(getattr(metadata, "parent_generation_id", "")).strip()
    if not re.fullmatch(r"G-[0-9]{4}", generation_id) or not re.fullmatch(r"G-[0-9]{4}", parent_generation_id):
        raise BusinessPresentationPlanError("active generation lineage is invalid")
    ordinal = int(generation_id[2:])
    if int(parent_generation_id[2:]) != ordinal - 1:
        raise BusinessPresentationPlanError("active generation parent lineage is invalid")

    canonical_fixture_ref = f"products/generations/{generation_id}/dashboard/dashboard_fixture_v4.json"
    canonical_chart_map_ref = f"products/generations/{generation_id}/dashboard/dashboard_chart_map_v4.json"
    canonical_previous_ref = f"extensions/{parent_generation_id}/{PRESENTATION_PLAN_FILENAME}"
    canonical_target_ref = f"extensions/{generation_id}/{PRESENTATION_PLAN_FILENAME}"
    fixture_ref, fixture_path = _direct_v2_reference(
        context, fixture_ref, expected=canonical_fixture_ref, label="fixture"
    )
    chart_map_ref, chart_map_path = _direct_v2_reference(
        context, chart_map_ref, expected=canonical_chart_map_ref, label="chart map"
    )
    previous_plan_ref, previous_plan_path = _direct_v2_reference(
        context, previous_plan_ref, expected=canonical_previous_ref, label="predecessor plan"
    )
    target_ref = _presentation_plan_ref(context, generation_id, presentation_plan_ref)
    if target_ref != canonical_target_ref:
        raise BusinessPresentationPlanError("direct V2 target must be the active generation plan path")
    target_path = _direct_v2_target_path(context, target_ref, expected=canonical_target_ref)
    if target_path.exists() and not target_path.is_file():
        raise BusinessPresentationPlanError("direct V2 target is not a regular file")
    reviewer = _text(reviewer_ref).strip()
    if not reviewer or reviewer != _text(reviewer_ref) or not re.fullmatch(r"[A-Za-z0-9_./:-]+", reviewer):
        raise BusinessPresentationPlanError("direct V2 reviewer_ref is invalid")
    for label, value in (
        ("fixture", expected_fixture_sha256),
        ("chart map", expected_chart_map_sha256),
        ("predecessor plan", expected_previous_plan_sha256),
    ):
        if not _is_sha256(value):
            raise BusinessPresentationPlanError(f"expected {label} hash is invalid")
    if not _is_sha256(expected_successor_plan_sha256):
        raise BusinessPresentationPlanError("expected successor plan hash is invalid")

    def _check_source(path: Path, expected_hash: str, label: str) -> bytes:
        raw = path.read_bytes()
        if _sha256_bytes(raw) != expected_hash:
            raise BusinessPresentationPlanError(f"{label} hash drifted")
        return raw

    fixture_bytes = _check_source(fixture_path, expected_fixture_sha256, "fixture")
    chart_map_bytes = _check_source(chart_map_path, expected_chart_map_sha256, "chart map")
    previous_plan_bytes = _check_source(previous_plan_path, expected_previous_plan_sha256, "predecessor plan")

    # This direct successor path requires a V2 predecessor before invoking the
    # builder, so callers cannot bootstrap a new generation through an older
    # plan shape.
    try:
        previous_plan = json.loads(previous_plan_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("direct V2 predecessor plan is invalid") from exc
    if not isinstance(previous_plan, Mapping) or previous_plan.get("schema_version") != PRESENTATION_PLAN_V2_SCHEMA:
        raise BusinessPresentationPlanError("direct V2 predecessor must be a V2 plan")

    candidate = write_business_presentation_plan_v2(
        context,
        fixture_ref=fixture_ref,
        chart_map_ref=chart_map_ref,
        previous_plan_ref=previous_plan_ref,
        manager_entries=manager_entries,
        reviewer_ref=reviewer,
        presentation_plan_ref=target_ref,
        _lock_held=True,
    )
    _validate_presentation_plan_v2_shape(candidate)
    if candidate.get("generation_id") != generation_id or candidate.get("reviewer_ref") != reviewer:
        raise BusinessPresentationPlanError("direct V2 candidate lineage/reviewer binding is invalid")
    source = candidate.get("source_bindings")
    expected_source = {
        "fixture_ref": fixture_ref,
        "fixture_sha256": expected_fixture_sha256,
        "chart_map_ref": chart_map_ref,
        "chart_map_sha256": expected_chart_map_sha256,
        "previous_plan_ref": previous_plan_ref,
        "previous_plan_sha256": expected_previous_plan_sha256,
    }
    if not isinstance(source, Mapping) or any(source.get(key) != value for key, value in expected_source.items()):
        raise BusinessPresentationPlanError("direct V2 candidate source binding is invalid")
    # Recheck all immutable inputs after the expensive build and before any
    # target mutation, so a concurrent source change fails closed.
    fixture_bytes = _check_source(fixture_path, expected_fixture_sha256, "fixture")
    chart_map_bytes = _check_source(chart_map_path, expected_chart_map_sha256, "chart map")
    _check_source(previous_plan_path, expected_previous_plan_sha256, "predecessor plan")

    try:
        fixture = json.loads(fixture_bytes.decode("utf-8"))
        chart_map = json.loads(chart_map_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("direct V2 visual source is invalid") from exc
    if not isinstance(fixture, Mapping) or not isinstance(chart_map, Mapping):
        raise BusinessPresentationPlanError("direct V2 visual source must be JSON objects")
    widgets = fixture.get("widgets")
    if not isinstance(widgets, list):
        raise BusinessPresentationPlanError("direct V2 fixture widgets are invalid")
    _validate_business_presentation_plan_v2(
        context,
        candidate,
        fixture=fixture,
        fixture_ref=fixture_ref,
        chart_map=chart_map,
        chart_map_ref=chart_map_ref,
        widgets=[widget for widget in widgets if isinstance(widget, Mapping)],
        strict_source_hash=True,
    )
    payload = _canonical_bytes(candidate)
    candidate_hash = _sha256_bytes(payload)
    if candidate_hash != expected_successor_plan_sha256:
        raise BusinessPresentationPlanError("direct V2 candidate hash does not match expected successor")
    if target_path.is_file() and not target_path.is_symlink():
        current = target_path.read_bytes()
        if current == payload:
            return dict(candidate)
        raise BusinessPresentationPlanError("existing direct V2 target conflicts with the requested candidate")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_replace_plan_bytes(target_path, payload)
    return dict(candidate)


def record_business_presentation_plan_v2(
    context: RunContext,
    *,
    fixture_ref: str | Path,
    chart_map_ref: str | Path,
    previous_plan_ref: str | Path,
    manager_entries: Sequence[Mapping[str, Any]],
    reviewer_ref: str,
    expected_fixture_sha256: str,
    expected_chart_map_sha256: str,
    expected_previous_plan_sha256: str,
    expected_successor_plan_sha256: str,
    presentation_plan_ref: str | Path | None = None,
) -> dict[str, Any]:
    """Public absent-target V2 recorder with strict source CAS semantics."""

    try:
        from auto_foundry_core.lifecycle import RunLifecycle
    except ModuleNotFoundError as exc:  # pragma: no cover - package guard
        raise BusinessPresentationPlanError("RunLifecycle is unavailable for direct V2 recording") from exc
    with RunLifecycle._run_lock(context):  # noqa: SLF001 - publication boundary
        return _record_business_presentation_plan_v2_unlocked(
            context,
            fixture_ref=fixture_ref,
            chart_map_ref=chart_map_ref,
            previous_plan_ref=previous_plan_ref,
            manager_entries=manager_entries,
            reviewer_ref=reviewer_ref,
            expected_fixture_sha256=expected_fixture_sha256,
            expected_chart_map_sha256=expected_chart_map_sha256,
            expected_previous_plan_sha256=expected_previous_plan_sha256,
            expected_successor_plan_sha256=expected_successor_plan_sha256,
            presentation_plan_ref=presentation_plan_ref,
        )


def _v2_manager_selection(
    inventory: Mapping[str, Any],
    visual_inventory: Mapping[str, Any],
    manager_entries: Sequence[Mapping[str, Any]],
    *,
    require_explicit_visual_choices: bool = False,
) -> dict[str, Any]:
    """Validate one complete manager selection against read-only inventories.

    Initial admission and same-run successor generation share this policy.  A
    caller supplies the entire ordered manager envelope; all executable visuals
    omitted from it are retained in the technical-audit gallery rather than
    being implicitly promoted.  ``require_explicit_visual_choices`` is enabled
    only for successor candidates, where recipe/layout/renderer values are
    part of the new Product decision rather than inherited defaults.
    """

    visual_entries = [copy.deepcopy(dict(entry)) for entry in visual_inventory.get("visual_entries", [])]
    visual_by_id = {_text(entry.get("widget_id")): entry for entry in visual_entries}
    candidates_by_id = {
        _text(entry.get("widget_id")): entry
        for entry in inventory.get("candidates", [])
        if isinstance(entry, Mapping) and _text(entry.get("widget_id"))
    }
    requested = [dict(value) for value in manager_entries if isinstance(value, Mapping)]
    if len(requested) != len(manager_entries):
        raise BusinessPresentationPlanError("manager_entries must contain objects")
    requested_ids = [_text(value.get("widget_id")).strip() for value in requested]
    if len(requested_ids) != len(set(requested_ids)) or any(not value for value in requested_ids):
        raise BusinessPresentationPlanError("manager_entries widget IDs must be unique and non-empty")
    unknown = sorted(set(requested_ids) - set(candidates_by_id))
    if unknown:
        raise BusinessPresentationPlanError(f"manager_entries reference unknown widgets: {unknown[:5]}")

    # A geometry-less visual after the one requirement fallback is an
    # audit-only declaration, even if a stale/over-eager Product request names
    # its ID.  Drop that integrity duplicate before deriving manager IDs and
    # partition; no semantic field is inspected.
    effective_requested = [
        entry
        for entry in requested
        if candidates_by_id[_text(entry.get("widget_id")).strip()].get("no_geometry_fallback_duplicate") is not True
    ]
    requested = effective_requested
    requested_ids = [_text(value.get("widget_id")).strip() for value in requested]

    # Keep only source/data integrity checks here.  Product Agent membership is
    # the semantic decision; titles, content, roles, kinds, technical
    # annotations, and prior presentation metadata never veto a selected ID.

    # A V2 visual partition covers every executable chart, while the selected
    # manager IDs define the business audience.  Remaining visuals stay in the
    # technical audit gallery; they are never promoted implicitly.
    all_visual_ids = list(visual_inventory.get("all_visual_widget_ids") or [])
    manager_visual_ids = [widget_id for widget_id in requested_ids if widget_id in visual_by_id]
    audit_visual_ids = [widget_id for widget_id in all_visual_ids if widget_id not in manager_visual_ids]
    visual_by_id_final: dict[str, dict[str, Any]] = {}
    for widget_id in all_visual_ids:
        visual = copy.deepcopy(visual_by_id[widget_id])
        visual["presentation_audience"] = (
            "business_manager" if widget_id in manager_visual_ids else "technical_audit_gallery"
        )
        visual_by_id_final[widget_id] = visual
    # Persist the reviewed partition order explicitly: manager visuals first,
    # followed by the remaining technical-audit visuals.
    visual_entries = [visual_by_id_final[widget_id] for widget_id in manager_visual_ids + audit_visual_ids]

    manager_entries_v2: list[dict[str, Any]] = []
    for widget_id, requested_entry in zip(requested_ids, requested):
        candidate = candidates_by_id[widget_id]
        if candidate.get("record_id") != requested_entry.get("record_id"):
            raise BusinessPresentationPlanError(f"manager entry record does not match inventory: {widget_id}")
        if (
            candidate.get("file_sha256") != requested_entry.get("file_sha256")
            or candidate.get("canonical_payload_sha256") != requested_entry.get("canonical_payload_sha256")
        ):
            raise BusinessPresentationPlanError(f"manager entry hash does not match inventory: {widget_id}")
        if (
            candidate.get("requirement_id") != requested_entry.get("requirement_id")
            or candidate.get("presentation_role") != requested_entry.get("presentation_role")
        ):
            raise BusinessPresentationPlanError(f"manager entry identity does not match inventory: {widget_id}")
        if requested_entry.get("display_projection") != candidate.get("display_projection"):
            raise BusinessPresentationPlanError(
                f"manager entry display projection does not match inventory: {widget_id}"
            )
        if widget_id in visual_by_id_final:
            recipe_id, layout, renderer_type = _validated_plan_selection(
                candidate,
                requested_entry,
                require_explicit=require_explicit_visual_choices,
            )
            visual_by_id_final[widget_id]["recipe_id"] = recipe_id
            visual_by_id_final[widget_id]["layout"] = layout
            visual_by_id_final[widget_id]["renderer_type"] = renderer_type
        entry = copy.deepcopy(requested_entry)
        if widget_id in visual_by_id_final:
            entry.update(_v2_manager_entry_from_visual(visual_by_id_final[widget_id]))
            entry["display_projection"] = copy.deepcopy(requested_entry.get("display_projection", {}))
        manager_entries_v2.append(entry)
    return {
        "manager_widget_ids": requested_ids,
        "manager_entries": manager_entries_v2,
        "manager_visual_widget_ids": manager_visual_ids,
        "audit_visual_widget_ids": audit_visual_ids,
        "visual_entries": visual_entries,
    }


def _write_business_presentation_blueprint_v2(
    context: RunContext,
    *,
    manager_entries: Sequence[Mapping[str, Any]],
    reviewer_ref: str,
    fixture_ref: str | Path,
    chart_map_ref: str | Path | None,
    item_ids: Sequence[str] | None,
    generation_id: str | None,
    presentation_plan_ref: str | Path | None,
    presentation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one V2 blueprint from exact inventory entries.

    This is the sole public writer implementation.  ``manager_entries`` may
    still carry the familiar pointer-bound display projection, but the
    persisted contract is always the V2 visual/source blueprint.
    """

    reviewer = _text(reviewer_ref).strip()
    if not reviewer or reviewer != _text(reviewer_ref) or not re.fullmatch(r"[A-Za-z0-9_./:-]+", reviewer):
        raise BusinessPresentationPlanError("reviewer_ref is invalid")
    inventory = business_presentation_inventory(
        context,
        fixture_ref=fixture_ref,
        generation_id=generation_id,
        item_ids=item_ids,
    )
    visual_inventory = business_presentation_visual_inventory(
        context,
        fixture_ref=fixture_ref,
        chart_map_ref=chart_map_ref or _text((inventory.get("design_inventory") or {}).get("chart_map_ref")) or None,
    )
    selection = _v2_manager_selection(inventory, visual_inventory, manager_entries)

    generation, metadata = _presentation_generation_metadata(context, generation_id)
    supervisor_ref, supervisor_plan, supervisor_hash = _presentation_supervisor_binding(context, generation, metadata)
    input_items = _presentation_input_bindings(
        context,
        _discover_item_ids(context, item_ids, supervisor_plan),
    )
    parent = _presentation_parent_binding(context, generation, metadata)
    source = {
        "fixture_ref": visual_inventory["fixture_ref"],
        "fixture_sha256": visual_inventory["fixture_sha256"],
        "chart_map_ref": visual_inventory["chart_map_ref"],
        "chart_map_sha256": visual_inventory["chart_map_sha256"],
    }
    plan: dict[str, Any] = {
        "schema_version": PRESENTATION_PLAN_V2_SCHEMA,
        "run_id": context.run_id,
        "generation_id": generation,
        "supervisor_plan_ref": supervisor_ref,
        "supervisor_plan_sha256": supervisor_hash,
        "item_order": [item["item_id"] for item in input_items],
        "input_items": input_items,
        "parent": parent,
        "reviewer_ref": reviewer,
        "manager_widget_ids": selection["manager_widget_ids"],
        "manager_entries": selection["manager_entries"],
        "manager_visual_widget_ids": selection["manager_visual_widget_ids"],
        "audit_visual_widget_ids": selection["audit_visual_widget_ids"],
        "visual_entries": selection["visual_entries"],
        "source_bindings": source,
    }
    if presentation is not None:
        plan["presentation"] = _dashboard_runtime().validate_presentation_copy(
            presentation, widget_ids=plan["manager_widget_ids"], requirement_ids=plan["item_order"])
    _validate_presentation_plan_v2_shape(plan)
    reference = _presentation_plan_ref(context, generation, presentation_plan_ref)
    path = context.resolve_run_path(reference)
    payload = _canonical_bytes(plan)
    if path.is_file() or path.is_symlink():
        if path.is_symlink():
            raise BusinessPresentationPlanError("existing presentation blueprint is symlinked")
        current_bytes = path.read_bytes()
        if current_bytes == payload:
            return plan
        # Preview plans are the only mutable presentation-plan namespace.  A
        # changed preflight source fingerprint permits one explicit refresh;
        # ordinary/final plan targets remain immutable and fail closed.
        source = plan.get("source_bindings")
        try:
            current = json.loads(current_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessPresentationPlanError("existing presentation blueprint is invalid") from exc
        current_source = current.get("source_bindings") if isinstance(current, Mapping) else None
        source_ref = _text(source.get("fixture_ref") if isinstance(source, Mapping) else "").strip()
        current_ref = _text(current_source.get("fixture_ref") if isinstance(current_source, Mapping) else "").strip()
        is_preflight_target = (
            reference == f"extensions/{generation}/business_presentation_plan.json"
            and source_ref == current_ref
            and source_ref == _preflight_ref(generation, "dashboard_fixture_v4.json")
        )
        if not is_preflight_target or not isinstance(source, Mapping) or not isinstance(current_source, Mapping):
            raise BusinessPresentationPlanError("existing presentation blueprint conflicts with the requested admission")
        if source.get("fixture_sha256") == current_source.get("fixture_sha256") and source.get("chart_map_sha256") == current_source.get("chart_map_sha256"):
            raise BusinessPresentationPlanError("existing preview blueprint conflicts without a changed preflight source")
        _assert_no_symlink_chain(context, reference, label="presentation blueprint")
        _atomic_replace_plan_bytes(path, payload)
        return plan
    _assert_no_symlink_chain(context, reference, label="presentation blueprint")
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_replace_plan_bytes(path, payload)
    return plan


def write_business_presentation_plan(
    context: RunContext,
    *,
    manager_entries: Sequence[Mapping[str, Any]] | None = None,
    manager_widget_ids: Sequence[str] | None = None,
    reviewer_ref: str,
    fixture_ref: str | Path,
    chart_map_ref: str | Path | None = None,
    item_ids: Sequence[str] | None = None,
    generation_id: str | None = None,
    presentation_plan_ref: str | Path | None = None,
    presentation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically record one explicit generation-scoped V2 manager admission."""

    if manager_widget_ids not in (None, []):
        raise BusinessPresentationPlanError(
            "manager_widget_ids are derived from V2 manager_entries, not accepted as input"
        )
    if manager_entries is None:
        raise BusinessPresentationPlanError(
            "manager_entries with pointer-bound projections are required"
        )

    return _write_business_presentation_blueprint_v2(
        context,
        manager_entries=manager_entries,
        reviewer_ref=reviewer_ref,
        fixture_ref=fixture_ref,
        chart_map_ref=chart_map_ref,
        item_ids=item_ids,
        generation_id=generation_id,
        presentation_plan_ref=presentation_plan_ref,
        presentation=presentation,
    )


def _group_definitions(plan: Mapping[str, Any] | None, item_ids: Sequence[str]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    assigned: set[str] = set()
    if plan and isinstance(plan.get("groups"), list):
        for index, raw in enumerate(plan["groups"], 1):
            if not isinstance(raw, Mapping):
                raise AssemblyError("requirement supervisor plan groups must be objects")
            ids = [_text(value).strip() for value in _as_list(raw.get("requirement_ids"))]
            ids = [value for value in ids if value]
            if not ids:
                continue
            if any(value in assigned for value in ids):
                raise AssemblyError("requirement supervisor plan assigns an item more than once")
            assigned.update(ids)
            groups.append({
                "id": _slug(raw.get("id") or raw.get("group_id") or f"group-{index:02d}"),
                "title": _text(raw.get("title") or raw.get("label") or raw.get("name") or raw.get("rationale") or f"Decision group {index}"),
                "order": index,
                "requirement_ids": ids,
                "summary": _text(raw.get("summary") or raw.get("rationale")),
                # Planner decision-flow scope is kept exact.  It is used only
                # for syntactic presentation-label extraction below; rationale
                # remains the group's summary/description.
                "scope": _text(raw.get("scope") or raw.get("decision_flow_scope")),
            })
    for item_id in item_ids:
        if item_id not in assigned:
            index = len(groups) + 1
            groups.append({"id": f"group-{index:02d}", "title": item_id, "order": index, "requirement_ids": [item_id], "summary": "", "scope": ""})
            assigned.add(item_id)
    # A plan may mention an item that was not requested; do not silently read it.
    for group in groups:
        group["requirement_ids"] = [item_id for item_id in group["requirement_ids"] if item_id in item_ids]
    groups = [group for group in groups if group["requirement_ids"]]
    return groups


_PRESENTATION_ATX_HEADING_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?#{1,6}\s+(?P<label>.+?)\s*(?:#+\s*)?$"
)
_PRESENTATION_BOLD_HEADING_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?\*\*(?P<label>.+?)\*\*\s*$"
)
# Presentation ordinals are intentionally bounded to one or two digits.  A
# four-digit year at the start of a heading is content, not an ordinal.
_PRESENTATION_ORDINAL_RE = re.compile(r"^\s*(?:[1-9]|[1-9]\d)(?:[.)]\s*|\s+)")
_PRESENTATION_COMPACT_ORDINAL_RE = re.compile(r"^\s*(?:[1-9]|[1-9]\d)(?=[A-Z][a-z])")


def _presentation_heading_labels(scopes: Iterable[Any], *, limit: int = 2) -> list[str]:
    """Extract at most two exact markdown heading labels syntactically."""

    labels: list[str] = []
    for scope in scopes:
        for line in _text(scope).splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            match = _PRESENTATION_ATX_HEADING_RE.fullmatch(candidate)
            if match is None:
                match = _PRESENTATION_BOLD_HEADING_RE.fullmatch(candidate)
            if match is None:
                continue
            label = match.group("label").strip()
            label = _PRESENTATION_ORDINAL_RE.sub("", label, count=1).strip()
            label = _PRESENTATION_COMPACT_ORDINAL_RE.sub("", label, count=1).strip()
            if label and label not in labels:
                labels.append(label)
            if len(labels) >= limit:
                return labels
    return labels


def _presentation_domain_title(group: Mapping[str, Any], flow_defs: Sequence[Mapping[str, Any]]) -> str:
    """Choose a concise domain label from exact flow scope headings."""

    heading_labels = _presentation_heading_labels(
        [group.get("scope"), *(flow.get("scope") for flow in flow_defs)]
    )
    if heading_labels:
        return " · ".join(heading_labels)
    # No heading: preserve up to two already-concise, distinct flow titles
    # rather than slicing a rationale sentence.  A group title is the final
    # concise fallback.
    flow_titles: list[str] = []
    for flow in flow_defs:
        candidate = _text(flow.get("title")).strip()
        if (
            candidate
            and "\n" not in candidate
            and len(candidate) <= 80
            and not _RAW_REQUIREMENT_TITLE_RE.fullmatch(candidate)
            and candidate not in flow_titles
        ):
            flow_titles.append(candidate)
            if len(flow_titles) >= 2:
                break
    if flow_titles:
        return " · ".join(flow_titles)
    group_title = _text(group.get("title")).strip()
    if group_title and "\n" not in group_title and len(group_title) <= 80 and not _RAW_REQUIREMENT_TITLE_RE.fullmatch(group_title):
        return group_title
    return "Decision domain"


def _manager_requirement_title(
    item_id: str,
    content: Mapping[str, Any],
    original_text: str,
    records: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Choose a complete, scope-level business clause for the manager surface.

    Requirement wording is the authority for this heading.  Strip only the
    request/time boilerplate, then stop at an explicit secondary-clause cue;
    never hard-slice or ellipsize a sentence.  The complete original scope is
    carried separately in ``requirement_scope``/the technical audit.
    """

    # Real supervisor-shaped runs keep the exact markdown heading in
    # ``item_state.original_text`` while group scope is empty.  Prefer that
    # syntactic heading over the surrounding request/rationale prose.
    heading = _presentation_heading_labels([original_text], limit=1)
    if heading:
        return heading[0]

    lexical = re.sub(r"\s+", " ", _text(original_text)).strip(" \t\r\n.:;,-")
    if lexical:
        body = _SCOPE_TIME_PREFIX_RE.sub("", lexical, count=1)
        body = _SCOPE_REQUEST_PREFIX_RE.sub("", body, count=1).strip()
        cue = _SCOPE_SECONDARY_CUE_RE.search(body)
        candidate = body[:cue.start()] if cue else body
        candidate = candidate.strip(" \t\r\n,;:-.")
        while candidate:
            token = re.sub(r"[^A-Za-z]+$", "", candidate.split()[-1]).lower()
            if token not in _DANGLING_SCOPE_TOKENS:
                break
            candidate = candidate.rsplit(" ", 1)[0].rstrip(" ,;:-")
        if (
            candidate
            and len(candidate) <= 130
            and "…" not in candidate
            and _balanced_parentheses(candidate)
        ):
            return candidate[:1].upper() + candidate[1:]

    # If the first reviewed clause is unusually long, use a supplied human
    # dashboard-fact title rather than inventing a word-boundary truncation.
    for record in records or ():
        if _text(record.get("kind")) != "dashboard_fact":
            continue
        payload = _record_payload(record)
        candidate_payload = payload.get("widget") if isinstance(payload.get("widget"), Mapping) else payload
        for key in ("title", "short_title", "label", "name"):
            candidate = _text(candidate_payload.get(key)).strip()
            if _is_manager_title_candidate(candidate, item_id):
                return candidate

    # A few legacy/manual fixtures have no original text or dashboard fact.
    # Only then may an explicit content title be used; signal labels remain out
    # of this fallback so a missing scope cannot silently become a metric
    # heading.
    for key in ("short_title", "title", "name", "label"):
        candidate = _text(content.get(key)).strip()
        if _is_manager_title_candidate(candidate, item_id):
            return candidate
    return "Reviewed decision"


_SCHEMA_TOKEN_TITLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_RAW_REQUIREMENT_TITLE_RE = re.compile(r"^REQ[-_]?\d+$", flags=re.IGNORECASE)
_SCOPE_REQUEST_PREFIX_RE = re.compile(
    r"^(?:provide\s+a\s+management\s+view\s+of|provide|show|identify|determine|evaluate)\s+",
    flags=re.IGNORECASE,
)
_SCOPE_TIME_PREFIX_RE = re.compile(
    r"^(?:as\s+of|through|at|for)\s+.*?,\s*",
    flags=re.IGNORECASE,
)
_SCOPE_SECONDARY_CUE_RE = re.compile(
    r"(?:,\s+(?:where|which|how|separating|keeping|preserving|reporting|separately)\b"
    r"|\s+and\s+(?:where|whether|which|identify)\b"
    r"|\s+to\s+identify\b"
    r"|\s+so\s+Operations\b"
    r"|\s+spanning\b)",
    flags=re.IGNORECASE,
)
_DANGLING_SCOPE_TOKENS = {
    "and", "or", "from", "through", "to", "with", "of", "for", "how",
    "where", "which", "that", "the", "a", "an", "in", "on", "by", "as",
    "into", "over", "under", "at", "vs", "versus", "whether",
}


def _balanced_parentheses(value: str) -> bool:
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _is_manager_title_candidate(candidate: str, item_id: str = "") -> bool:
    """Reject raw requirement/schema identifiers as manager titles."""

    if not candidate or len(candidate) > 130 or "\n" in candidate:
        return False
    if _RAW_REQUIREMENT_TITLE_RE.fullmatch(candidate.strip()):
        return False
    if re.fullmatch(r"[a-z][a-z0-9_]*", candidate.strip()):
        return False
    if item_id and candidate.strip().lower() == item_id.strip().lower():
        return False
    return True


def _requirement_limitations(records: Sequence[Mapping[str, Any]]) -> list[str]:
    values: list[str] = []
    for record in records:
        payload = _record_payload(record)
        kind = _text(record.get("kind"))
        candidates = []
        if kind == "limitation":
            candidates.append(payload.get("limitation"))
        candidates.extend(_as_list(payload.get("limitations")))
        for value in candidates:
            text = _text(value).strip()
            if text and text not in values:
                values.append(text)
    return values


def _manager_takeaway(content: Mapping[str, Any], scope: str) -> str:
    """Return one reviewed headline verbatim when supplied.

    Semantic selection belongs to the Product Agent.  The assembler does not
    inspect or rewrite headline text to decide whether a surface is business
    or technical.
    """

    for value in _as_list(content.get("headline_findings")):
        if isinstance(value, Mapping):
            value = next(
                (value.get(key) for key in ("finding", "claim", "text", "body", "message", "title", "label", "value") if value.get(key) not in (None, "")),
                None,
            )
        text = _text(value).strip()
        if text:
            return text
    return ""


def _manager_limitations(content: Mapping[str, Any], records: Sequence[Mapping[str, Any]], scope: str) -> list[str]:
    """Return reviewed limitations verbatim, without semantic filtering."""

    values: list[str] = []
    candidates: list[Any] = [*_as_list(content.get("limitations"))]
    for record in records:
        payload = _record_payload(record)
        candidates.extend(_as_list(payload.get("limitations")))
        if _text(record.get("kind")) == "limitation":
            candidates.append(payload.get("limitation"))
    for value in candidates:
        text = _text(value).strip()
        if text and text not in values:
            values.append(text)
    return values


def _plan_binding(plan: Mapping[str, Any] | None, groups: Sequence[Mapping[str, Any]], plan_ref: str | Path | None) -> dict[str, Any]:
    """Return canonical supervisor-plan/grouping hashes for retry binding."""

    reference = Path(plan_ref or "requirement_supervisor_plan.json").as_posix()
    canonical_plan: Any = plan if plan is not None else None
    return {
        "ref": reference,
        "present": plan is not None,
        "sha256": _json_hash(canonical_plan),
        "groups_sha256": _json_hash(list(groups)),
        "group_count": len(groups),
    }


def _root_plan_binding(supervisor_plan_ref: str, supervisor_plan_hash: str) -> dict[str, Any]:
    """Return the exact root-generation supervisor-plan admission binding.

    Root product inspection binds the receipt to the authoritative supervisor
    plan *bytes*, rather than to a re-serialized JSON value.  Keep this shape
    deliberately small and exact: downstream validators use it as the CAS
    boundary for the G-0001 product.
    """

    reference = Path(supervisor_plan_ref).as_posix()
    if reference != "requirement_supervisor_plan.json" or not _is_sha256(supervisor_plan_hash):
        raise AssemblyError("root supervisor-plan binding is not canonical")
    return {
        "ref": reference,
        "sha256": supervisor_plan_hash,
        "admission_sha256": supervisor_plan_hash,
        "generation_id": "G-0001",
    }


def _record_payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _presentation_surface_is_technical(widget: Mapping[str, Any]) -> bool:
    """Expose only an explicit origin flag; never infer from presentation text.

    The flag is carried for audit/provenance consumers.  It is deliberately
    not an admission gate: Product Agent plan membership is the sole semantic
    choice for the manager surface.
    """

    return widget.get("technical_surface") is True


def _manager_admission(
    item_id: str,
    widget: Mapping[str, Any],
    *,
    subject_context: str = "",
 ) -> dict[str, Any]:
    """Return neutral baseline metadata; Product plan decides membership.

    Before a plan is attached every reviewed widget remains a candidate.  The
    only deterministic exception is a geometry-less fallback duplicate, which
    is an integrity marker rather than a semantic judgement.  No titles,
    labels, rows, values, roles, or chart kinds are inspected here.
    """

    technical = _presentation_surface_is_technical(widget)
    reason = _text(widget.get("technical_surface_reason")).strip()
    admission = {
        "status": "audit_only" if widget.get("no_geometry_fallback_duplicate") is True else "admitted",
        "presentation_audience": "technical_audit" if widget.get("no_geometry_fallback_duplicate") is True else "business_manager",
        "role": _text(widget.get("presentation_role") or "decision_view")
        if widget.get("no_geometry_fallback_duplicate") is not True
        else "technical_audit",
        "reasons": [
            "geometry-less fallback duplicate retained in audit"
            if widget.get("no_geometry_fallback_duplicate") is True
            else "candidate retained; Product presentation plan controls manager membership",
        ],
        "technical_surface": technical,
    }
    if reason:
        admission["technical_surface_reason"] = reason
    return admission


def _apply_manager_admission(widgets: list[dict[str, Any]], *, subject_context: str = "") -> None:
    """Attach neutral candidate metadata without inspecting content."""

    for widget in widgets:
        admission = _manager_admission(_text(widget.get("requirement_id")), widget, subject_context=subject_context)
        widget["manager_admission"] = admission
        widget["presentation_audience"] = admission["presentation_audience"]
        if admission["status"] != "admitted":
            widget["presentation_tier"] = "audit"
        elif widget.get("presentation_tier") is None:
            widget["presentation_tier"] = "primary"


def _apply_explicit_manager_admission(
    widgets: list[dict[str, Any]],
    manager_widget_ids: Sequence[str],
    manager_entries: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Bind manager visibility to one persisted plan, never to lexical rules."""

    for widget in widgets:
        widget_id = _text(widget.get("id")).strip()
        requested = widget_id in manager_widget_ids
        entry = manager_entries.get(widget_id) if isinstance(manager_entries, Mapping) else None
        fallback_duplicate = widget.get("no_geometry_fallback_duplicate") is True
        technical = _presentation_surface_is_technical(widget)
        # Product Agent membership is authoritative.  The one geometry-less
        # fallback marker is an exact representation integrity rule; no prior
        # semantic metadata may veto a selected ID.
        admitted = requested and not fallback_duplicate
        reasons: list[str] = ["explicit_business_presentation_plan"]
        if fallback_duplicate:
            reasons.append("duplicate geometry-less visual retains the requirement fallback in audit")
        if technical:
            reasons.append("explicit technical_surface metadata carried from origin")
        role = _text(widget.get("presentation_role") or "decision_view")
        widget["manager_admission"] = {
            "status": "admitted" if admitted else "audit_only",
            "presentation_audience": "business_manager" if admitted else "technical_audit",
            "policy": "explicit_business_presentation_plan",
            "role": role if admitted else "technical_audit",
            "plan_membership": admitted,
            "technical_surface": technical,
            "reasons": reasons,
        }
        widget["presentation_audience"] = "business_manager" if admitted else "technical_audit"
        widget["presentation_tier"] = "primary" if admitted else "audit"
        if admitted:
            if not isinstance(manager_entries, Mapping) or widget_id not in manager_entries:
                raise BusinessPresentationPlanError(f"manager widget has no pointer-bound plan entry: {widget_id}")
            widget["manager_presentation"] = copy.deepcopy(dict(manager_entries[widget_id]))
        else:
            widget.pop("manager_presentation", None)


def _humanize_label(value: Any) -> str:
    """Turn a reviewed field label into a short presentation label.

    This is deliberately lexical only.  It never maps a field to a different
    business concept, changes a value, or interprets an identifier.  The raw
    key remains available in the audit payload/trace projection.
    """

    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", _text(value)).strip()
    text = re.sub(r"[_-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "Reviewed value"
    words: list[str] = []
    for word in text.split(" "):
        if word.isupper() or any(char.isdigit() for char in word):
            words.append(word)
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def _endpoint_label(value: Any) -> str:
    """Create a human endpoint label without exposing a source path."""

    text = _text(value).strip()
    if "::" in text:
        text = text.split("::", 1)[0]
    text = text.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    text = re.sub(r"\.(?:csv|jsonl?|xlsx?|parquet)$", "", text, flags=re.IGNORECASE)
    return _humanize_label(text)


def _manager_rows_from_sequence(values: Any, *, group_label: str = "") -> list[dict[str, Any]]:
    """Project explicitly supplied row records into business-facing columns.

    Raw field/path/value_json/evidence/path data is intentionally omitted from
    this presentation projection; the exact candidate remains in
    ``audit_payload``.  Only scalar reviewed fields are copied.
    """

    rows: list[dict[str, Any]] = []
    technical = {
        "path", "field", "source_field", "row_kind", "value_json", "evidence_ref",
        "evidence_refs", "record_id", "record_hash", "accepted_content_hash",
        "integration_record_id", "integration_record_ids", "relationship_id",
        "analysis_relationship_id", "audit_id", "join_keys", "owner_ref",
    }
    for index, raw in enumerate(_as_list(values), 1):
        if isinstance(raw, Mapping):
            row: dict[str, Any] = {}
            if group_label:
                row["view"] = _humanize_label(group_label)
            for key, value in raw.items():
                key_text = _text(key)
                if key_text in technical or isinstance(value, (Mapping, list, tuple)):
                    continue
                if value is None:
                    continue
                row[_humanize_label(key_text)] = value
            if row:
                rows.append(row)
        elif raw is not None:
            rows.append({"Label": f"{_humanize_label(group_label) if group_label else 'Reviewed value'} {index}", "Value": raw})
    return rows


def _manager_rows_from_mapping(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project a supplied mapping into compact, deterministic business rows."""

    rows: list[dict[str, Any]] = []
    for key, value in sorted(values.items(), key=lambda pair: _text(pair[0])):
        label = _humanize_label(key)
        if isinstance(value, list):
            nested = _manager_rows_from_sequence(value, group_label=label)
            rows.extend(nested)
        elif isinstance(value, Mapping):
            nested = _manager_rows_from_sequence([value], group_label=label)
            if nested:
                rows.extend(nested)
        elif value is not None:
            rows.append({"Label": label, "Value": value})
    return rows


# Dashboard facts are reviewed visual contracts, rather than generic
# ``field/value`` records.  Keep their projection deliberately separate from
# the legacy status-table path above: the latter is part of the inherited G3
# byte contract, while new facts carry explicit grouping/measure/series/point
# semantics that must not be reduced to the first scalar in a row.
_FACT_TECHNICAL_KEYS = frozenset({
    "fact_id", "visual_id", "outcome_chart_id", "source_ref", "source_refs",
    "evidence_ref", "evidence_refs", "record_id", "record_ids", "record_hash",
    "integration_record_id", "integration_record_ids", "accepted_content_hash",
    "accepted_manifest_hash", "trace_ref", "trace_refs", "path", "paths",
    "field", "fields", "row_kind", "value_json", "data_ref", "item_id",
    "requirement_id", "relationship_id", "join_keys", "owner_ref",
})
_FACT_CONTROL_KEYS = frozenset({
    "source_local", "review_status", "comparability", "facet_scale", "scale",
    "ordering", "faceting", "period_authority", "date_authority", "date_period",
    "population_period", "snapshot_range", "full_ledger_audit", "supporting_stage",
    "stage", "business_area", "chart_contract", "notes", "sample_policy",
    "sample_rows", "exact_values", "invalid_count", "status_conflict_count",
})
_FACT_AUDIT_NUMERIC_KEYS = frozenset({
    "stock_rows", "forecast_rows", "ticket_rows", "delivery_rows", "line_count",
    "order_count", "invoice_count", "po_count", "row_count", "valid_quantity_rows",
    "invalid_quantity_rows", "matched_refund_rows", "pending_refund_rows",
    "pending_refund_row_count", "refund_row_count", "invalid_dates", "invalid_week_dates",
    "invalid_opened_dates", "invalid_start_dates", "missing_order_total_lines",
    "confirmed_date_count", "requested_date_count", "in_transit_missing_actual_count",
    "missing_actual_count", "missing_planned_with_valid_actual_count",
})
_FACT_IMPORTANT_COUNT_KEYS = frozenset({
    "late_count", "on_time_count", "open_or_unresolved_rows", "high_priority_rows",
    "open_or_unresolved_count", "unresolved_ticket_count", "late_or_open_count",
})
_FACT_DIMENSION_ORDER = (
    "category", "product_category", "campaign_name", "campaign_code", "channel",
    "sales_channel", "currency", "return_reason", "reason", "issue_category",
    "priority", "carrier", "service_level", "origin_location", "destination_city",
    "destination_country", "warehouse", "vendor", "vendor_no", "customer_segment",
)
_FACT_CONTROL_VALUE_RE = re.compile(
    r"^(?:unavailable(?:[_ ]in[_ ]source)?|independent[_ ]per[_ ]currency|"
    r"native[_ ]currency[_ ]facets?|no[_ ](?:fx|pooling)|not[_ ](?:applicable|available))$",
    flags=re.IGNORECASE,
)
_FACT_IDENTIFIER_RE = re.compile(r"^(?P<prefix>LOC-WH|DP-WH|WH|VEND)-(?P<number>[A-Za-z0-9]+)$", flags=re.IGNORECASE)
_FACT_IDENTIFIER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<legacy>OLD-)?(?P<prefix>LOC-WH|DP-WH|WH|VEND)-(?P<number>[A-Za-z0-9]+)(?![A-Za-z0-9])",
    flags=re.IGNORECASE,
)
_FACT_NUMERIC_TEXT_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?%?$")


def _fact_scalar(value: Any) -> bool:
    return value is not None and not isinstance(value, (Mapping, list, tuple))


def _fact_numeric(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    return bool(_FACT_NUMERIC_TEXT_RE.fullmatch(_text(value).strip()))


def _fact_control_value(value: Any) -> bool:
    return bool(_FACT_CONTROL_VALUE_RE.fullmatch(_text(value).strip().replace("-", "_")))


def _fact_identifier_label(value: Any) -> str:
    """Humanize source identifiers without changing their bound identity."""

    text = _text(value).strip()
    def replace(match: re.Match[str]) -> str:
        prefix = match.group("prefix").upper()
        number = match.group("number")
        legacy = bool(match.group("legacy"))
        if prefix == "VEND":
            return f"{'Legacy ' if legacy else ''}Supplier {number}"
        if prefix == "DP-WH":
            return f"{'Legacy ' if legacy else ''}Planning warehouse {number}"
        return f"{'Legacy ' if legacy else ''}Warehouse {number}"

    # Apply the same deterministic mapping to a reviewed composite group
    # label (for example ``USD · OLD-VEND-000002``), not just to a scalar
    # dimension.  The exact source token remains in audit payloads; manager
    # rows retain the stable identity in a readable form.
    return _FACT_IDENTIFIER_TOKEN_RE.sub(replace, text)


def _fact_visible_value(key: Any, value: Any) -> Any:
    """Return a manager-safe copy of one fact scalar, or ``None`` to omit."""

    key_text = re.sub(r"[^a-z0-9]+", "_", _text(key).strip().lower()).strip("_")
    if key_text in _FACT_TECHNICAL_KEYS or key_text in _FACT_CONTROL_KEYS:
        return None
    if not _fact_scalar(value) or _fact_control_value(value):
        return None
    if isinstance(value, str):
        return _fact_identifier_label(value)
    return value


def _fact_key_label(key: Any) -> str:
    return _humanize_label(key)


def _fact_group_keys(candidate: Mapping[str, Any], row: Mapping[str, Any]) -> list[str]:
    """Resolve explicit grouping names to exact row keys, in reviewed order."""

    keys = [key for key in row if _fact_visible_value(key, row.get(key)) is not None]
    normalized = {re.sub(r"[^a-z0-9]+", "_", _text(key).lower()).strip("_"): key for key in keys}
    grouping = _text(candidate.get("grouping")).strip()
    requested: list[str] = []
    if grouping:
        for token in re.split(r"\s*(?:x|×|,|/|\+|;| and )\s*", grouping, flags=re.IGNORECASE):
            token_key = re.sub(r"[^a-z0-9]+", "_", token.lower()).strip("_")
            if token_key and token_key in normalized:
                requested.append(normalized[token_key])
    for key in _FACT_DIMENSION_ORDER:
        if key in normalized and normalized[key] not in requested:
            requested.append(normalized[key])
    # A supplied label/name is already a reviewed group label and wins over
    # mechanically joined dimensions.  It is not a source identifier.
    for key in ("label", "name"):
        if key in normalized and normalized[key] not in requested:
            requested.insert(0, normalized[key])
    return requested


def _fact_measure_keys(candidate: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Find explicitly named measure/series fields without choosing a first scalar."""

    available = {key for row in rows for key in row if _fact_visible_value(key, row.get(key)) is not None}
    explicit = _text(candidate.get("measure")).strip()
    if explicit and explicit in available:
        return [explicit]
    x_key = _text(candidate.get("x")).strip()
    y_key = _text(candidate.get("y")).strip()
    if y_key and y_key in available:
        return [y_key]
    named: list[str] = []
    series = candidate.get("series")
    for item in _as_list(series):
        if isinstance(item, str) and item in available and item not in named:
            named.append(item)
        elif isinstance(item, Mapping):
            field = _text(item.get("field") or item.get("name") or item.get("key")).strip()
            if field in available and field not in named:
                named.append(field)
    # REQ22's accepted chart contract names both exact quantities.  The fields
    # are copied as supplied; no delta/ratio is calculated here.
    contract = _text(candidate.get("chart_contract"))
    for field in ("ordered_quantity", "shipped_quantity"):
        if field in contract and field in available and field not in named:
            named.append(field)
    if named:
        return named
    if "value" in available:
        return ["value"]
    if "display_value" in available:
        return ["display_value"]
    # Some reviewed facts omit a top-level measure but explicitly expose
    # multiple numeric row fields.  Preserve those named fields as a series;
    # audit/count mechanics are excluded while business measures remain exact.
    numeric: list[str] = []
    for key in available:
        values = [row.get(key) for row in rows]
        if not any(_fact_numeric(value) for value in values):
            continue
        normalized = re.sub(r"[^a-z0-9]+", "_", _text(key).lower()).strip("_")
        if normalized in _FACT_AUDIT_NUMERIC_KEYS or normalized.endswith("_count") or normalized.endswith("_rows"):
            if normalized not in _FACT_IMPORTANT_COUNT_KEYS:
                continue
        numeric.append(key)
    return sorted(numeric)


def _fact_group_label(candidate: Mapping[str, Any], row: Mapping[str, Any], index: int) -> str:
    values: list[str] = []
    for key in _fact_group_keys(candidate, row):
        value = _fact_visible_value(key, row.get(key))
        if value is None:
            continue
        text = _text(value).strip()
        if text and text not in values:
            values.append(text)
    if values:
        return " · ".join(values)
    return f"Reviewed row {index}"


def _fact_chart_rows(candidate: Mapping[str, Any], raw_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    measures = _fact_measure_keys(candidate, raw_rows)
    descriptors = {
        measure: _fact_scale_descriptor(candidate, measure, raw_rows)
        for measure in measures
    }
    if len(measures) > 1 and any(descriptor.get("scale_unknown") for descriptor in descriptors.values()):
        unknown = [measure for measure, descriptor in descriptors.items() if descriptor.get("scale_unknown")]
        raise AssemblyError(
            "dashboard fact mixes numeric series without an accepted scale classification: "
            + ", ".join(unknown)
        )
    geometry_facet = _fact_geometry_facet_dimension(candidate)
    if not geometry_facet and any(
        _fact_currency_value(raw.get("currency")) is not None
        for raw in raw_rows
        if isinstance(raw, Mapping)
    ):
        geometry_facet = "currency"
    output: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows, 1):
        row: dict[str, Any] = {}
        supplied_label = _fact_visible_value("label", raw.get("label")) or _fact_visible_value("name", raw.get("name"))
        row["label"] = _text(supplied_label).strip() if supplied_label is not None else _fact_group_label(candidate, raw, index)
        series: list[dict[str, Any]] = []
        for measure in measures:
            value = _fact_visible_value(measure, raw.get(measure))
            if value is None:
                continue
            series.append({
                "label": _fact_key_label(measure),
                "value": value,
                **descriptors[measure],
            })
        if len(series) == 1:
            row["value"] = series[0]["value"]
            for key in (
                "scale_group", "scale_domain", "scale_basis",
                "scale_facet_dimension", "scale_fixed_max", "scale_unknown",
            ):
                if key in series[0]:
                    row[key] = series[0][key]
            series = []
        elif len(series) > 1:
            row["series"] = series
        # Explicit bars may already carry a display value but no named source
        # measure.  Preserve that exact accepted value rather than replacing it.
        if not series:
            value = _fact_visible_value("value", raw.get("value"))
            if value is None:
                value = _fact_visible_value("display_value", raw.get("display_value"))
            if value is not None:
                row["value"] = value
        # Geometry is presentation metadata, not a visible business field.
        # Keep the exact accepted facet value alongside the projected row so
        # independent-per-currency scales can be applied without parsing a
        # human label or re-reading unbound audit payload fields later.
        if geometry_facet:
            facet_value = _fact_visible_value(geometry_facet, raw.get(geometry_facet))
            if facet_value is not None:
                row["_geometry_facet"] = _text(facet_value)
        if len(row) > 1:
            output.append(row)
    return output


def _fact_geometry_facet_dimension(candidate: Mapping[str, Any]) -> str:
    """Return an explicitly reviewed facet field used for chart scaling.

    A facet is used only when the accepted fact names an independent scale
    policy (or an explicit facet dimension).  We never infer a currency
    partition merely because a row happens to contain a currency-looking
    value; absent policy means one scale for the whole chart.
    """

    policy_values: list[Any] = [candidate.get("facet_scale"), candidate.get("scale")]
    faceting = candidate.get("faceting")
    if isinstance(faceting, Mapping):
        policy_values.extend([faceting.get("scale"), faceting.get("dimension")])
    elif faceting is not None:
        policy_values.append(faceting)
    independent = any(
        "independent_per_currency" in _text(value).strip().lower().replace(" ", "_")
        for value in policy_values
        if value is not None
    )
    explicit_dimension = _text(candidate.get("facet_by")).strip()
    if isinstance(faceting, Mapping):
        explicit_dimension = _text(faceting.get("dimension") or explicit_dimension).strip()
    if independent:
        return explicit_dimension or "currency"
    # A named facet without an independent scale still scopes the chart's
    # geometry to that accepted facet, rather than inventing a new grouping.
    return explicit_dimension if explicit_dimension else ""


def _fact_geometry_number(value: Any) -> float | None:
    """Parse one supplied numeric measure for presentation-only geometry."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        text = _text(value).strip()
        if text.endswith("%"):
            text = text[:-1].strip()
        if not _FACT_NUMERIC_TEXT_RE.fullmatch(text):
            return None
        try:
            parsed = float(text)
        except (TypeError, ValueError):
            return None
    return parsed if math.isfinite(parsed) else None


def _fact_currency_value(value: Any) -> str | None:
    """Return one usable source currency facet, excluding control values."""

    text = _text(value).strip()
    if not text or _fact_control_value(text):
        return None
    return text


def _fact_scale_descriptor(
    candidate: Mapping[str, Any],
    measure: str,
    raw_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify one reviewed measure for presentation-only scaling.

    The classifier is intentionally lexical only over the accepted field name
    and payload metadata.  It never derives a business metric or combines
    unlike units.  A lone unknown numeric field may use a chart-local scale;
    an unknown field in a mixed series fails closed because comparability is
    not reviewable from the payload.
    """

    normalized = re.sub(r"[^a-z0-9]+", "_", _text(measure).strip().lower()).strip("_")
    tokens = set(filter(None, normalized.split("_")))
    values = [
        _fact_geometry_number(row.get(measure))
        for row in raw_rows
        if isinstance(row, Mapping) and row.get(measure) is not None
    ]
    numeric_values = [value for value in values if value is not None]
    facet_dimension = _fact_geometry_facet_dimension(candidate)
    if not facet_dimension and any(
        _fact_currency_value(row.get("currency")) is not None
        for row in raw_rows
        if isinstance(row, Mapping)
    ):
        # Monetary source-local fields remain currency-local even when an
        # older accepted fact omitted an explicit facet_scale declaration.
        facet_dimension = "currency"

    is_count_name = bool(
        "count" in tokens
        or "rows" in tokens
        or "population" in tokens
        or "denominator" in tokens
        or normalized.endswith("_records")
        or normalized.endswith("_tickets")
        or normalized.endswith("_shipments")
    )
    is_rate = bool(
        {"rate", "ratio", "share", "fraction", "percent", "percentage"}.intersection(tokens)
        and not is_count_name
    )
    if is_rate:
        # Only an accepted fractional [0, 1] rate is safe to normalize to a
        # fixed domain.  Percent-like values such as ``95%`` are not silently
        # interpreted as fractions and therefore fail closed.
        if not numeric_values or any(value < 0 or value > 1 for value in numeric_values):
            raise AssemblyError(f"dashboard fact rate has no accepted fractional 0..1 domain: {measure}")
        return {
            "scale_group": "rate",
            "scale_domain": "0..1",
            "scale_basis": "accepted fractional rate domain",
            "scale_facet_dimension": facet_dimension,
            "scale_fixed_max": 1.0,
        }

    is_amount = bool(
        normalized.endswith("_source_local")
        and {"amount", "value", "cost", "price", "total", "paid", "outstanding", "line", "freight", "po"}.intersection(tokens)
    ) or bool({"amount", "cost", "price", "value"}.intersection(tokens) and "currency" in normalized)
    if is_amount:
        return {
            "scale_group": "amount",
            "scale_domain": "currency-local" if facet_dimension == "currency" else "amount-local",
            "scale_basis": "accepted source-local monetary measure"
            + ("; currency-local facet" if facet_dimension == "currency" else ""),
            "scale_facet_dimension": facet_dimension if facet_dimension == "currency" else "",
            "scale_fixed_max": None,
        }

    is_quantity = bool(
        {"qty", "quantity", "ordered", "shipped", "received", "reserved", "available", "damaged", "inbound", "on_hand", "stock"}.intersection(tokens)
        or any(
            marker in normalized
            for marker in ("on_hand", "in_transit", "qty", "quantity", "ordered", "shipped", "received", "reserved", "available", "damaged", "inbound", "stock")
        )
    )
    if is_quantity:
        return {
            "scale_group": "quantity",
            "scale_domain": "quantity-local",
            "scale_basis": "accepted quantity measure",
            "scale_facet_dimension": facet_dimension,
            "scale_fixed_max": None,
        }

    if is_count_name:
        return {
            "scale_group": "count",
            "scale_domain": "count-local",
            "scale_basis": "accepted count measure",
            "scale_facet_dimension": facet_dimension,
            "scale_fixed_max": None,
        }

    return {
        "scale_group": "chart",
        "scale_domain": "chart-local",
        "scale_basis": "single supplied numeric field; chart-local scale",
        "scale_facet_dimension": facet_dimension,
        "scale_fixed_max": None,
        "scale_unknown": True,
    }


def _fact_geometry_rows(candidate: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Attach deterministic geometry to explicit bar/column fact rows.

    This computes no business metric: every ratio below is a CSS presentation
    size derived from a supplied numeric value.  A chart uses one shared max;
    an explicitly independent-per-currency contract uses one max per bound
    currency facet.  Multi-series rows share that same applicable max so the
    series remain comparable.  The original scalar values are untouched.
    """

    facet_dimension = _fact_geometry_facet_dimension(candidate)
    entries: list[tuple[dict[str, Any], list[tuple[dict[str, Any], float, dict[str, Any]]], str]] = []
    for raw in rows:
        row = dict(raw)
        numeric_entries: list[tuple[dict[str, Any], float, dict[str, Any]]] = []
        series = row.get("series")
        if isinstance(series, list):
            for item in series:
                if not isinstance(item, Mapping):
                    continue
                parsed = _fact_geometry_number(item.get("value"))
                if parsed is not None:
                    numeric_entries.append((dict(item), parsed, dict(item)))
        else:
            parsed = _fact_geometry_number(row.get("value"))
            if parsed is not None:
                numeric_entries.append((row, parsed, dict(row)))
        facet = _text(row.get("_geometry_facet")) or "__chart__"
        entries.append((row, numeric_entries, facet or "__chart__"))

    maxima: dict[tuple[str, str], float] = {}
    for _row, numeric_entries, facet in entries:
        for _item, value, descriptor in numeric_entries:
            group = _text(descriptor.get("scale_group") or "chart")
            descriptor_facet = (
                facet
                if _text(descriptor.get("scale_facet_dimension")) and facet != "__chart__"
                else "__chart__"
            )
            key = (group, descriptor_facet)
            maximum = maxima.get(key, 0.0)
            fixed_max = descriptor.get("scale_fixed_max")
            if isinstance(fixed_max, (int, float)):
                maximum = max(maximum, float(fixed_max))
            maximum = max(maximum, abs(value))
            maxima[key] = maximum

    geometry_basis = "independent facet max normalization" if facet_dimension else "chart max normalization"
    for row, numeric_entries, facet in entries:
        if isinstance(row.get("series"), list):
            projected_series: list[dict[str, Any]] = []
            # Preserve every projected series item, including a non-numeric
            # reviewed value; only numeric items receive a drawable size.
            by_identity = {id(item): item for item in row["series"] if isinstance(item, Mapping)}
            for original in row["series"]:
                item = dict(original) if isinstance(original, Mapping) else {"value": original}
                parsed = _fact_geometry_number(item.get("value"))
                if parsed is not None:
                    descriptor = item
                    group = _text(descriptor.get("scale_group") or "chart")
                    descriptor_facet = (
                        facet
                        if _text(descriptor.get("scale_facet_dimension")) and facet != "__chart__"
                        else "__chart__"
                    )
                    maximum = maxima.get((group, descriptor_facet), 0.0)
                    ratio = abs(parsed) / maximum * 100.0 if maximum else 0.0
                    item["size"] = _format_geometry(ratio)
                    item["scale_facet"] = facet if descriptor_facet != "__chart__" else None
                    item.pop("scale_facet_dimension", None)
                    item.pop("scale_fixed_max", None)
                    item.pop("scale_unknown", None)
                    if parsed < 0:
                        item["signed_size"] = _format_geometry(-ratio)
                    elif "signed_size" in item:
                        item["signed_size"] = _format_geometry(ratio)
                projected_series.append(item)
            row["series"] = projected_series
            # The row itself is geometric when at least one explicit series
            # is geometric; the renderer will draw each supplied series track.
            row["geometry_basis"] = geometry_basis
        else:
            parsed = _fact_geometry_number(row.get("value"))
            if parsed is not None:
                descriptor = row
                group = _text(descriptor.get("scale_group") or "chart")
                descriptor_facet = (
                    facet
                    if _text(descriptor.get("scale_facet_dimension")) and facet != "__chart__"
                    else "__chart__"
                )
                maximum = maxima.get((group, descriptor_facet), 0.0)
                ratio = abs(parsed) / maximum * 100.0 if maximum else 0.0
                row["size"] = _format_geometry(ratio)
                row["scale_facet"] = facet if descriptor_facet != "__chart__" else None
                row.pop("scale_facet_dimension", None)
                row.pop("scale_fixed_max", None)
                row.pop("scale_unknown", None)
                if parsed < 0:
                    row["signed_size"] = _format_geometry(-ratio)
                elif "signed_size" in row:
                    row["signed_size"] = _format_geometry(ratio)
                row["geometry_basis"] = geometry_basis
    return [dict(row) for row, _numeric_entries, _facet in entries]


def _fact_table_rows(raw_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in raw_rows:
        row: dict[str, Any] = {}
        for key, value in raw.items():
            visible = _fact_visible_value(key, value)
            if visible is None:
                continue
            row[_fact_key_label(key)] = visible
        if row:
            output.append(row)
    return output


def _fact_scatter_rows(candidate: Mapping[str, Any], raw_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    x_key = _text(candidate.get("x")).strip()
    y_key = _text(candidate.get("y")).strip()
    if not x_key or not y_key:
        return []
    output: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows, 1):
        x_value = _fact_visible_value(x_key, raw.get(x_key))
        y_value = _fact_visible_value(y_key, raw.get(y_key))
        if x_value is None or y_value is None:
            continue
        output.append({"label": _fact_group_label(candidate, raw, index), "x": x_value, "y": y_value})
    return output


def _structured_fact_projection(candidate: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    raw_type = _text(candidate.get("type")).strip().lower()
    output_type = raw_type if raw_type else "table"
    raw_rows_value = None
    for key in ("rows", "data", "bars", "points", "cells"):
        if isinstance(candidate.get(key), list) and candidate.get(key):
            raw_rows_value = candidate.get(key)
            break
    raw_rows = [row for row in _as_list(raw_rows_value) if isinstance(row, Mapping)]
    if output_type == "scatter":
        return output_type, _fact_scatter_rows(candidate, raw_rows)
    if output_type in {"table", "status_table"}:
        return output_type, _fact_table_rows(raw_rows)
    chart_rows = _fact_chart_rows(candidate, raw_rows)
    if output_type in {"bar", "column"}:
        chart_rows = _fact_geometry_rows(candidate, chart_rows)
    return output_type, chart_rows


def _manager_fact_projection(candidate: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]] | list[dict[str, Any]]]:
    """Select a manager-safe view from explicit dashboard-fact fields only."""

    # A reviewed dashboard fact already carries its visual contract.  Keep
    # that type when projecting the supplied rows; the previous implementation
    # routed every non-KPI fact through ``status_table`` and thereby discarded
    # accepted bar/column/scatter geometry.  The projection below remains
    # presentation-only (labels are humanized, values are copied), while the
    # untouched candidate is retained in ``audit_payload`` by ``_fact_widget``.
    raw_type = _text(candidate.get("type")).strip().lower()
    supported_type = {
        "bar", "column", "lollipop", "donut", "waffle", "progress",
        "scatter", "leaderboard", "diverging_bar", "stacked_composition",
        "line", "heatmap", "table", "status_table", "metric_grid", "kpi_grid",
    }
    output_type = raw_type if raw_type in supported_type else "status_table"

    # New reviewed dashboard facts carry a real visual contract.  Resolve the
    # contract before any generic status-table fallback so grouping, measure,
    # multi-series, scatter points, and table rows survive intact.  Legacy G3
    # status_table/kpi_grid facts intentionally remain on the older branch to
    # preserve their inherited fixture/hash contract byte-for-byte.
    if raw_type in {"bar", "column", "scatter", "table"}:
        return _structured_fact_projection(candidate)

    metrics = candidate.get("metrics")
    if isinstance(metrics, Mapping):
        metrics = [{"label": key, "value": value} for key, value in metrics.items()]
    if isinstance(metrics, list) and metrics:
        tiles: list[dict[str, Any]] = []
        for index, metric in enumerate(metrics, 1):
            if not isinstance(metric, Mapping) or metric.get("value") is None:
                continue
            label = _humanize_label(metric.get("label") or metric.get("name") or f"Metric {index}")
            denominator = metric.get("denominator")
            display = metric.get("value")
            if denominator is not None and metric.get("value") is not None:
                display = f"{_text(metric.get('value'))} of {_text(denominator)}"
            tile = {"label": label, "value": display, "raw_value": metric.get("value")}
            if denominator is not None:
                tile["denominator"] = denominator
            if metric.get("unit") not in (None, ""):
                tile["unit"] = metric.get("unit")
            tiles.append(tile)
        if tiles:
            return ("kpi_grid" if output_type in {"status_table", "kpi", "kpi_grid", "metric_grid"} else output_type), tiles

    for key in ("rows", "data", "series", "bars", "categories", "segments", "points", "cells", "tiles"):
        rows = _manager_rows_from_sequence(candidate.get(key))
        if rows:
            return output_type, rows

    values = candidate.get("values")
    if isinstance(values, Mapping):
        rows = _manager_rows_from_mapping(values)
        if rows:
            return output_type, rows

    for key in ("targets", "edges", "nodes"):
        rows = _manager_rows_from_sequence(candidate.get(key), group_label=key)
        if rows:
            return output_type, rows

    scalar_rows: list[dict[str, Any]] = []
    for key in ("count", "returned", "limit", "queue_count", "status"):
        if candidate.get(key) is not None:
            scalar_rows.append({"Label": _humanize_label(key), "Value": candidate.get(key)})
    return output_type, scalar_rows or [{"Label": "Reviewed detail", "Value": "See technical audit"}]


def _decorate_manager_fact_rows(
    candidate: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Add only mechanical chart labels/value summaries to fact projections.

    Reviewed dashboard facts use domain-specific row keys (for example
    ``carrier`` + ``late_rate`` or ``category`` + ``ordered_quantity``), while
    the offline chart renderer accepts a compact label/value envelope.  Keep
    every supplied scalar row field and add deterministic presentation aliases;
    no totals, rates, or geometry are calculated here.  The untouched fact is
    retained in ``audit_payload``.
    """

    decorated: list[dict[str, Any]] = []
    measure = _text(candidate.get("measure")).strip()
    measure_label = _humanize_label(measure) if measure else ""
    for index, raw in enumerate(rows, 1):
        row = dict(raw)
        if not _text(row.get("label") or row.get("name")).strip():
            label_parts = [
                _text(value).strip()
                for key, value in row.items()
                if isinstance(value, str)
                and _text(key).lower() not in {"unit", "period", "status", "measure"}
                and _text(value).strip()
            ]
            row["label"] = " · ".join(label_parts) if label_parts else f"Reviewed row {index}"
        # Do not infer a display value from row insertion order.  Structured
        # facts have already selected their explicit measure/series above;
        # rows without one remain table-like and are rendered as supplied.
        decorated.append(row)
    return decorated


def _manager_metric_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build readable rows for a structured metric when no fact exists."""

    rows: list[dict[str, Any]] = []
    for key in ("label", "name", "metric", "source", "as_of", "date_authority", "unit", "units", "distinct_unit", "value"):
        value = payload.get(key)
        if value is not None and not isinstance(value, (Mapping, list, tuple)):
            rows.append({"Label": _humanize_label(key), "Value": value})
    for key in ("components", "breakdown", "rows", "series", "items", "records"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            rows.extend(_manager_rows_from_mapping(value))
        elif isinstance(value, list):
            rows.extend(_manager_rows_from_sequence(value, group_label=key))
    if not rows:
        rows = _manager_rows_from_mapping(payload)
    return rows


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        return None
    return float(value)


def _numeric_map(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping) or not value:
        return None
    result: dict[str, float] = {}
    for key, item in value.items():
        parsed = _numeric(item)
        if parsed is None:
            return None
        result[_text(key)] = parsed
    return result


def _format_geometry(value: float) -> str:
    rendered = f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    return rendered + "%"


def _explicit_coverage(payload: Mapping[str, Any]) -> tuple[float, Any] | None:
    coverage = _numeric(payload.get("coverage"))
    if coverage is None or coverage < 0 or coverage > 1:
        return None
    denominator = next((payload[key] for key in ("population", "denominator", "worklist_population", "source_population", "target_population") if key in payload), None)
    if denominator is None:
        return None
    return coverage, denominator


def _currency_partition(payload: Mapping[str, Any], mapping: Mapping[str, float]) -> bool:
    def _currency_key(value: str) -> bool:
        normalized = value.strip().upper()
        return normalized in {"", "UNKNOWN"} or (len(normalized) == 3 and normalized.isalpha())

    units = _text(payload.get("units") or payload.get("unit")).lower()
    currencies = payload.get("currencies")
    if isinstance(currencies, list) and currencies and all(isinstance(value, str) and len(value) == 3 for value in currencies):
        return True
    if any(token in units for token in ("currency", "no fx", "source-currency", "source currency")):
        return all(_currency_key(key) for key in mapping)
    return all(_currency_key(key) for key in mapping) and len(mapping) >= 2


def _scalar_currency_partition(payload: Mapping[str, Any]) -> bool:
    """Identify a scalar that is explicitly a source-currency partition.

    A scalar with a currency partition/no-FX contract is still not a KPI total:
    it is retained as one metric-grid tile so the presentation cannot imply a
    cross-currency aggregate.  Ordinary scalar USD/EUR metrics remain KPIs.
    """

    units = _text(payload.get("units") or payload.get("unit")).lower()
    if isinstance(payload.get("currency_partitions"), Mapping):
        return True
    if isinstance(payload.get("currencies"), (list, tuple, set, frozenset)) and payload.get("currencies"):
        return True
    return "currency" in units and ("partition" in units or "no fx" in units or "source-currency" in units or "source currency" in units)


def _signed_shape(payload: Mapping[str, Any], mapping: Mapping[str, float]) -> bool:
    shape = _text(payload.get("shape") or payload.get("value_shape")).lower()
    comparability = _text(payload.get("comparability") or payload.get("comparison")).lower()
    return bool(payload.get("signed") is True or payload.get("signed_comparable") is True or shape in {"signed", "signed_categories", "diverging"} or comparability in {"signed", "signed_comparable", "comparable_signed"}) and any(value < 0 for value in mapping.values())


def _flat_map_comparable(payload: Mapping[str, Any]) -> bool:
    """Reject explicitly mixed category units/grains before drawing geometry."""

    if payload.get("comparable") is False or payload.get("category_comparable") is False:
        return False
    for key in ("units", "unit", "grain"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            values = {_text(item).strip() for item in value.values() if _text(item).strip()}
            if len(values) > 1:
                return False
        elif isinstance(value, (list, tuple, set, frozenset)):
            values = {_text(item).strip() for item in value if _text(item).strip()}
            if len(values) > 1:
                return False
    return True


def _record_refs(item_id: str, record: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    evidence = [_safe_record_ref(value) for value in _as_list(record.get("evidence_refs")) if _text(value).strip()]
    record_id = _safe_record_ref(record.get("record_id"))
    # Trace refs must remain clickable run-local artifacts.  The typed record
    # id is retained separately for machine provenance; it is not itself a
    # filesystem reference.
    trace = [
        f"requirements/{item_id}/accepted/manifest.json",
        f"requirements/{item_id}/accepted/answer_content.json",
        f"requirements/{item_id}/integration/committed/manifest.json",
        f"requirements/{item_id}/integration/committed/records.jsonl",
    ]
    payload = _record_payload(record)
    artifact_ref = payload.get("artifact_ref") if _text(record.get("kind")) == "analytical_artifact" else None
    if isinstance(artifact_ref, str) and artifact_ref.strip():
        trace.append(f"requirements/{item_id}/{artifact_ref.strip()}")
    return evidence, trace


def _audit_record_entries(
    records_by_item: Mapping[str, Sequence[Mapping[str, Any]]],
    widgets: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return one lossless audit entry for every committed integration record.

    This record-level projection is authoritative for technical audit: widget
    projections can be aggregated or demoted, but the exact reviewed payload
    and the union of evidence/trace references remain available and clickable.
    No values are calculated or inferred here.
    """

    widget_record_ids: dict[str, set[str]] = {}
    for widget in widgets:
        widget_id = _text(widget.get("id"))
        if not widget_id:
            continue
        ids = {
            _text(value).strip()
            for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
            if _text(value).strip()
        }
        for record_id in ids:
            widget_record_ids.setdefault(record_id, set()).add(widget_id)
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item_id, records in records_by_item.items():
        for record in records:
            if not isinstance(record, Mapping):
                continue
            record_id = _text(record.get("record_id")).strip()
            if not record_id:
                continue
            # Only records represented by at least one typed dashboard widget
            # belong to the dashboard-relevant audit projection.  The
            # committed integration ledger can contain supporting records
            # that are intentionally not dashboard outputs; retaining those
            # here would make the audit count diverge from the widget
            # provenance set (291 records for the current G3 product).
            if record_id not in widget_record_ids:
                continue
            key = (item_id, record_id)
            if key in seen:
                raise AssemblyError(f"duplicate committed integration record ID: {item_id}/{record_id}")
            seen.add(key)
            evidence_refs, trace_refs = _record_refs(item_id, record)
            references = sorted(set(evidence_refs) | set(trace_refs))
            payload = copy.deepcopy(dict(_record_payload(record)))
            entry = {
                "item_id": item_id,
                "record_id": record_id,
                "kind": _text(record.get("kind") or record.get("record_type") or payload.get("kind") or "reviewed_output"),
                "payload": payload,
                "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "committed_record_payload": copy.deepcopy(payload),
                "evidence_refs": sorted(set(evidence_refs)),
                "trace_refs": sorted(set(trace_refs)),
                "reference_union": references,
                "widget_ids": sorted(widget_record_ids.get(record_id, set())),
            }
            if entry["kind"] == "analytical_artifact":
                entry["artifact_provenance"] = {
                    key: payload.get(key)
                    for key in (
                        "artifact_id", "artifact_type", "schema_version", "requirement_id",
                        "content_hash", "envelope_hash", "canonical_bytes_sha256", "artifact_ref",
                    )
                    if payload.get(key) is not None
                }
            entries.append(entry)
    return entries


def _analytical_artifact_input_entries(
    records_by_item: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Return deterministic per-item artifact bindings for fixture/receipt."""

    entries: list[dict[str, Any]] = []
    for item_id, records in records_by_item.items():
        for record in records:
            if _text(record.get("kind")) != "analytical_artifact":
                continue
            provenance = _analytical_artifact_provenance(record)
            entries.append({
                "item_id": item_id,
                **provenance,
            })
    return sorted(entries, key=lambda value: (_text(value.get("item_id")), _text(value.get("artifact_id"))))


def _audit_widget_entries(widgets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return one exact audit snapshot for every typed dashboard widget.

    Record-level audit entries link to widgets by ID, while this separate
    inventory owns each raw widget snapshot exactly once.  Keeping these two
    authorities separate prevents relationship widgets bound to several
    records from multiplying full bars/categories/geometry in the record
    audit while preserving every byte for technical review.
    """

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for widget in widgets:
        if not isinstance(widget, Mapping):
            continue
        widget_id = _text(widget.get("id")).strip()
        if not widget_id or widget_id in seen:
            raise AssemblyError(f"dashboard widget audit requires unique ID: {widget_id or '<missing>'}")
        seen.add(widget_id)
        record_ids = sorted({
            _text(value).strip()
            for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
            if _text(value).strip()
        })
        def _refs(value: Any) -> list[str]:
            return sorted({
                _text(item.get("id") or item.get("ref") or item.get("path") if isinstance(item, Mapping) else item).strip()
                for item in _as_list(value)
                if _text(item.get("id") or item.get("ref") or item.get("path") if isinstance(item, Mapping) else item).strip()
            })

        evidence_refs = _refs(widget.get("evidence_refs"))
        trace_refs = _refs(widget.get("trace_refs"))
        entries.append({
            "widget_id": widget_id,
            "requirement_id": _text(widget.get("requirement_id")),
            "record_ids": record_ids,
            "widget_snapshot": copy.deepcopy(dict(widget)),
            "evidence_refs": evidence_refs,
            "trace_refs": trace_refs,
            "reference_union": sorted(set(evidence_refs) | set(trace_refs)),
        })
    return entries


def _widget_base(item_id: str, content: Mapping[str, Any], record: Mapping[str, Any], *, title: str) -> dict[str, Any]:
    payload = _record_payload(record)
    evidence_refs, trace_refs = _record_refs(item_id, record)
    record_id = _safe_record_ref(record.get("record_id"))
    limitations: list[str] = []
    for source in (_as_list(content.get("limitations")), _as_list(payload.get("limitations"))):
        for value in source:
            if _text(value).strip() and _text(value) not in limitations:
                limitations.append(_text(value))
    period = payload.get("period")
    if period is None and (payload.get("period_from") is not None or payload.get("period_to") is not None):
        period = f"{_text(payload.get('period_from'))}..{_text(payload.get('period_to'))}".strip(".")
    population = payload.get("population")
    if population is None:
        population = payload.get("denominator")
    base: dict[str, Any] = {
        "id": f"{_slug(item_id)}-{_slug(record_id)}",
        "title": title or record_id,
        # The exact reviewed payload is retained for the collapsed technical
        # audit.  Manager-facing projections below never render this field by
        # default and never use it to calculate a value.
        "audit_payload": dict(payload),
        "reviewed_item_ref": f"requirements/{item_id}/accepted/manifest.json",
        "reviewed_output_ref": f"requirements/{item_id}/accepted/answer_content.json",
        "evidence_refs": evidence_refs,
        "trace_refs": trace_refs,
        "review_status": "accepted_and_integrated",
        "period": period,
        "population": population,
        # Keep the supplied denominator independent of population.  Aggregate
        # KPI tiles use this exact field for a direct ``value of denominator``
        # display; no denominator is inferred when the numerator is absent.
        "denominator": payload.get("denominator"),
        "unit": payload.get("units") or payload.get("unit"),
        "limitations": limitations,
        "grain": payload.get("grain"),
        "integration_record_hash": record.get("record_hash"),
        "integration_record_id": record_id,
        "integration_record_ref": f"requirements/{item_id}/integration/committed/records.jsonl",
        "accepted_content_hash": record.get("accepted_content_hash"),
    }
    # Scalar KPIs may retain their compact presentation when the reviewed
    # value is genuinely scalar, but provenance/authority fields must remain
    # visible on that card.  Structured and non-scalar values use the
    # lossless table projection below; these explicit scalar fields keep the
    # KPI path faithful without coercing or hiding source context.
    for key in ("source", "as_of", "date_authority", "distinct_unit"):
        if key in payload and payload[key] not in (None, ""):
            base[key] = payload[key]
    for key in (
        "technical_surface", "technical_surface_reason", "presentation_audience",
        "presentation_tier", "presentation_role", "kind", "manager_admission",
        "no_geometry_fallback_duplicate", "presentation_deduplication",
    ):
        if key in payload:
            base[key] = copy.deepcopy(payload[key])
    return {key: value for key, value in base.items() if value not in (None, "", [])}


def _table_widget(item_id: str, content: Mapping[str, Any], record: Mapping[str, Any], *, title: str, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    widget = _widget_base(item_id, content, record, title=title)
    widget.update({"type": "table", "rows": rows, "presentation_tier": "primary", "presentation_role": "decision_view"})
    return widget


# Accepted answer contracts use all three labels for the same reviewed local
# evidence boundary.  ``evidence_ref`` is included only as this semantic alias;
# every reference still must resolve beneath the requirement and be hash-bound
# by that requirement's accepted manifest before any bytes are read.
_ACCEPTED_VISUAL_REF_KEYS = ("source_ref", "artifact_ref", "evidence_ref")
_ACCEPTED_VISUAL_TYPE_ALIASES = {
    **{name: name for name in ("kpi", "area", "pie", "donut", "scatter", "histogram", "box_plot", "waterfall", "column", "stacked_bar", "lollipop")},
    "line": "line",
    "line_chart": "line",
    "dual_line": "line",
    "dual_line_chart": "line",
    "small_multiple_line_charts": "line",
    "small_multiples": "line",
    "diverging_bar": "diverging_bar",
    "diverging_bar_table": "diverging_bar",
    "bar": "bar",
    "horizontal_bar": "bar",
    "paired_bar": "grouped_bar",
    "grouped_bar": "grouped_bar",
    "funnel": "funnel",
    "funnel_or_kpi_strip": "funnel",
    "pareto": "pareto",
    "heatmap": "heatmap",
    "relationship_matrix": "table",
    "ranked_table": "table",
    "table": "table",
    "callout": "table",
    "evidence_callout": "table",
}
_ACCEPTED_VISUAL_COLLECTION_KEYS = (
    "rows", "values", "data", "points", "series", "cells", "stages", "bars", "panels", "categories", "bins", "boxes", "steps",
)


def _accepted_visual_ref(item_id: str, reference: Any) -> tuple[str, str]:
    """Validate one accepted visual artifact ref within its requirement.

    Accepted visual references are deliberately less restrictive than product
    asset references: accepted answer contracts commonly bind reviewed files
    below ``work/``.  They still may not escape the requirement namespace or
    use an absolute/symlinked path.
    """

    value = _text(reference).strip()
    if not value or "\x00" in value or "\\" in value:
        raise AssemblyError(f"{item_id} accepted visual reference is invalid")
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise AssemblyError(f"{item_id} accepted visual reference is invalid")
    # A visual ref is resolved below requirements/<item_id>; callers must not
    # provide a second requirement prefix or a product/external path.
    if path.parts[0] in {"requirements", "products"}:
        raise AssemblyError(f"{item_id} accepted visual reference is invalid")
    return value, f"requirements/{item_id}/{path.as_posix()}"


def _accepted_visual_artifact(
    context: RunContext,
    item_id: str,
    accepted_manifest: Mapping[str, Any],
    reference: Any,
) -> tuple[Any, str]:
    """Load one hash-bound accepted visual artifact without running analytics."""

    ref, run_ref = _accepted_visual_ref(item_id, reference)
    progress = accepted_manifest.get("artifact_progress")
    hashes = progress.get("hashes") if isinstance(progress, Mapping) else None
    expected = hashes.get(ref) if isinstance(hashes, Mapping) else None
    if not _is_sha256(expected):
        raise AssemblyError(f"{item_id} accepted visual reference is not hash-bound: {ref}")
    _assert_no_symlink_chain(context, run_ref, label=f"{item_id} accepted visual artifact")
    _path, payload = _read_bytes(context, run_ref, label=f"{item_id} accepted visual artifact")
    actual = _sha256_bytes(payload)
    if actual != expected:
        raise AssemblyError(f"{item_id} accepted visual artifact hash mismatch: {ref}")
    suffix = Path(ref).suffix.lower()
    if suffix not in {".csv", ".json", ".jsonl"}:
        raise AssemblyError(f"{item_id} accepted visual artifact format is unsupported: {ref}")
    try:
        if suffix == ".csv":
            text = payload.decode("utf-8")
            rows = list(csv.DictReader(io.StringIO(text, newline="")))
            if rows and any(not isinstance(row, Mapping) for row in rows):
                raise ValueError("CSV rows are not objects")
            value: Any = [dict(row) for row in rows]
        elif suffix in {".json", ".jsonl"}:
            text = payload.decode("utf-8")
            if suffix == ".jsonl":
                value = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AssemblyError(f"{item_id} accepted visual artifact is invalid: {ref}") from exc
    return value, expected


def _accepted_visual_source_rows(
    visual: Mapping[str, Any],
    artifact_value: Any = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Copy inline/source rows and a bounded source-shape reason.

    This helper intentionally performs only structural projection.  It never
    aggregates values, computes rates, or invents geometry.
    """

    fields: list[str] = []
    for key in ("fields", "columns", "dimensions", "measures", "labels"):
        for value in _as_list(visual.get(key)):
            if isinstance(value, str) and value.strip() and value not in fields:
                fields.append(value)
    for key in ("measure", "x"):
        value = visual.get(key)
        if isinstance(value, str) and value.strip() and value not in fields:
            fields.append(value)
    for value in _as_list(visual.get("secondary_measures")):
        if isinstance(value, str) and value.strip() and value not in fields:
            fields.append(value)
    for value in _as_list(visual.get("series")):
        if isinstance(value, str) and value.strip() and value not in fields:
            fields.append(value)
    raw: Any = None
    # A hash-bound artifact is authoritative for source-bound visuals.  Do not
    # mistake declarative metadata such as ``series: ["orders", "lines"]``
    # or ``facets`` for the reviewed data rows when that artifact is present.
    # Inline rows/values are considered only when no external source was
    # declared.
    if artifact_value is None:
        for key in _ACCEPTED_VISUAL_COLLECTION_KEYS:
            candidate = visual.get(key)
            if isinstance(candidate, list):
                raw = candidate
                break
            if isinstance(candidate, Mapping) and key in {"rows", "data", "values", "cells", "stages", "bars"}:
                raw = [candidate]
                break
    if raw is None and artifact_value is not None:
        # Recursively discover list-of-object tables.  When a visual declares
        # fields, prefer the candidate exposing the greatest exact field
        # coverage; ties are ambiguous and become an explicit limitation.
        candidates: list[tuple[str, list[Any]]] = []

        def column_names(value: Any) -> list[str]:
            """Return an explicit column declaration without coercing values."""

            names: list[str] = []
            for item in _as_list(value):
                if isinstance(item, str) and item.strip() and item not in names:
                    names.append(item)
                elif isinstance(item, Mapping):
                    name = item.get("name") or item.get("field") or item.get("id")
                    if isinstance(name, str) and name.strip() and name not in names:
                        names.append(name)
            return names

        def materialize_matrix(columns: Sequence[str], matrix: Any) -> list[dict[str, Any]] | None:
            """Materialize a reviewed column matrix, preserving every cell exactly."""

            if not columns or not isinstance(matrix, list) or not matrix:
                return None
            if not all(isinstance(row, (list, tuple)) for row in matrix):
                return None
            width = len(columns)
            if any(len(row) != width for row in matrix):
                return None
            return [
                {column: _artifact_json_value(cell) for column, cell in zip(columns, row)}
                for row in matrix
            ]

        matrix_candidates: list[tuple[str, list[dict[str, Any]] | None]] = []

        def discover_matrix(value: Any, path: str = "") -> None:
            if isinstance(value, Mapping):
                columns = column_names(value.get("columns"))
                if columns:
                    for key in ("rows", "data", "values", "records", "items"):
                        child = value.get(key)
                        if isinstance(child, list) and child and all(isinstance(row, (list, tuple)) for row in child):
                            matrix_candidates.append(
                                (
                                    f"{path}.{key}" if path else key,
                                    materialize_matrix(columns, child),
                                )
                            )
                for key, child in value.items():
                    discover_matrix(child, f"{path}.{key}" if path else str(key))

        # A top-level matrix uses the visual declaration as its source schema;
        # nested matrices carry their own reviewed ``columns`` declaration.
        if isinstance(artifact_value, list) and artifact_value and all(
            isinstance(row, (list, tuple)) for row in artifact_value
        ):
            visual_columns = column_names(visual.get("columns") or visual.get("fields"))
            matrix_candidates.append(("<root>", materialize_matrix(visual_columns, artifact_value)))
        elif isinstance(artifact_value, Mapping):
            discover_matrix(artifact_value)

        def discover(value: Any, path: str = "") -> None:
            if isinstance(value, list) and value and all(isinstance(row, Mapping) for row in value):
                candidates.append((path, value))
                return
            if isinstance(value, Mapping):
                for key, child in value.items():
                    discover(child, f"{path}.{key}" if path else str(key))

        discover(artifact_value)
        if candidates:
            if fields:
                scored = [
                    (sum(1 for row in rows if set(fields).issubset(set(row))), path, rows)
                    for path, rows in candidates
                ]
                best_score = max(score for score, _path, _rows in scored)
                best = [(path, rows) for score, path, rows in scored if score == best_score]
                if best_score == 0:
                    # Typed artifacts commonly carry one semantic ``rows``
                    # table alongside metric-definition/metadata arrays.  A
                    # single conventional rows/data/results path is a safe
                    # source-bound choice even when the visual declaration
                    # uses aliases absent from that artifact; the missing
                    # fields remain an explicit limitation below.  Multiple
                    # equally plausible tables stay fail-closed.
                    preferred = [
                        item for item in candidates
                        if item[0].split(".")[-1]
                        in {"rows", "data", "values", "results", "records", "items"}
                    ]
                    if len(preferred) == 1:
                        _path, raw = preferred[0]
                    elif len(candidates) > 1:
                        return [], "accepted visual source table selection is ambiguous"
                    else:
                        _path, raw = candidates[0]
                elif len(best) == 1:
                    _path, raw = best[0]
                else:
                    return [], "accepted visual source table selection is ambiguous"
            else:
                preferred = [item for item in candidates if item[0].split(".")[-1] in {"rows", "data", "values", "results", "records", "items"}]
                if len(preferred) == 1:
                    _path, raw = preferred[0]
                elif len(candidates) == 1:
                    _path, raw = candidates[0]
                else:
                    return [], "accepted visual source contains multiple structured tables without a field binding"
        elif matrix_candidates:
            valid_matrices = [item for item in matrix_candidates if item[1] is not None]
            if not valid_matrices:
                return [], "accepted visual source rows do not match declared columns"
            if len(valid_matrices) != 1:
                return [], "accepted visual source table selection is ambiguous"
            _path, raw = valid_matrices[0]
        elif isinstance(artifact_value, Mapping):
            scalar = {
                str(key): _artifact_json_value(value)
                for key, value in artifact_value.items()
                if not isinstance(value, (Mapping, list, tuple))
            }
            if scalar:
                raw = [scalar]
    if raw is None:
        return [], "accepted visual specification has no inline rows or hash-bound structured values"
    rows: list[dict[str, Any]] = []
    for index, value in enumerate(raw, 1):
        if isinstance(value, Mapping):
            copied = _artifact_json_value(value)
            if not isinstance(copied, Mapping):
                return [], f"accepted visual row {index} is not a structured object"
            # Explicit visual fields narrow the copied table to reviewed
            # columns.  Missing requested columns remain a limitation below;
            # no alternate field is silently substituted.
            if fields:
                # Retain only the small set of source-local identity/axis
                # columns needed to label a generic chart.  These are not
                # derived values; they are exact reviewed fields that let a
                # declaration name ``measure``/``series`` while the artifact
                # supplies its segment, period, or row/column key.
                geometry_labels = (
                    "label", "category", "stage", "exception", "segment", "name",
                    "customer_id", "customer_name", "role", "period", "time", "month",
                    "date", "year", "row", "row_label", "column", "column_label", "x", "y",
                )
                projection_fields = list(fields)
                projection_fields.extend(
                    field for field in geometry_labels if field not in projection_fields and field in copied
                )
                copied = {field: copied[field] for field in projection_fields if field in copied}
            rows.append(dict(copied))
        else:
            rows.append({"value": _artifact_json_value(value)})
    if not rows:
        return [], "accepted visual source supplied no rows"
    if fields and not any(set(fields) <= set(row) for row in rows):
        return rows, "accepted visual source does not expose all requested fields"
    return rows, None


_ACCEPTED_EVIDENCE_REF = "work/evidence.jsonl"


def _accepted_evidence_scope(
    content: Mapping[str, Any],
    accepted_manifest: Mapping[str, Any],
) -> tuple[str, set[str]] | None:
    """Return the hash-bound evidence ledger and optional record IDs.

    Accepted answer bundles sometimes expose an evidence *ID* rather than a
    filesystem path in ``evidence_refs``.  The accepted manifest remains the
    authority for resolving that ID: only its standard ``work/evidence.jsonl``
    entry is eligible, and a fragment/ID narrows the record set without
    selecting any unrelated artifact.  No visual-to-record relationship is
    inferred here.
    """

    requested = False
    record_ids: set[str] = set()

    def collect(values: Any) -> None:
        nonlocal requested
        for raw in _as_list(values):
            text = _text(raw).strip()
            if not text:
                continue
            path, separator, fragment = text.partition("#")
            path = path.strip()
            fragment = fragment.strip()
            if path == _ACCEPTED_EVIDENCE_REF:
                requested = True
                if fragment:
                    record_ids.add(fragment)
                continue
            # Evidence contracts may carry stable IDs instead of paths.  Keep
            # the ID opaque and match it only against the hash-bound ledger's
            # explicit ``evidence_id`` field below.
            if not separator and "/" not in text and "\\" not in text:
                record_ids.add(text)

    collect(content.get("evidence_refs"))
    for visual in _as_list(content.get("visuals")):
        if not isinstance(visual, Mapping):
            continue
        collect(visual.get("evidence_refs"))
        collect(visual.get("evidence_ref"))
    if not requested and not record_ids:
        return None
    progress = accepted_manifest.get("artifact_progress")
    hashes = progress.get("hashes") if isinstance(progress, Mapping) else None
    if not isinstance(hashes, Mapping) or not _is_sha256(hashes.get(_ACCEPTED_EVIDENCE_REF)):
        return None
    return _ACCEPTED_EVIDENCE_REF, record_ids


def _accepted_evidence_rows(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return only a lossless fact-sheet projection, never a chosen table.

    The assembler uses :func:`_accepted_evidence_candidates` directly.  This
    narrow helper remains for older callers but deliberately returns no rows
    when a record contains table candidates, avoiding any position/title
    based table selection.
    """

    candidates = _accepted_evidence_candidates(record)
    if any(candidate["kind"] == "table" for candidate in candidates):
        return []
    fact_sheet = next((candidate for candidate in candidates if candidate["kind"] == "fact_sheet"), None)
    return copy.deepcopy(fact_sheet["rows"]) if fact_sheet is not None else []


def _json_pointer_escape(value: Any) -> str:
    """Escape one JSON-pointer path component without interpreting its value."""

    return str(value).replace("~", "~0").replace("/", "~1")


def _json_pointer_unescape(value: Any) -> str:
    """Decode one JSON-pointer path component for presentation labels only."""

    return _text(value).replace("~1", "/").replace("~0", "~")


def _accepted_evidence_pointer_segments(pointer: Any) -> list[str]:
    """Return non-numeric JSON-pointer segments without exposing the pointer."""

    segments = [_json_pointer_unescape(part) for part in _text(pointer).split("/") if part]
    # ``facts`` is the structural root, not a business label.  Array indexes
    # are likewise identity mechanics rather than useful manager copy.
    return [
        segment
        for index, segment in enumerate(segments)
        if not (index == 0 and segment == "facts")
        and not re.fullmatch(r"\d+", segment)
    ]


def _accepted_evidence_pointer_label(pointer: Any) -> str:
    """Humanize a source pointer without interpreting its business meaning."""

    segments = _accepted_evidence_pointer_segments(pointer)
    if not segments:
        return "Business metric"
    return " · ".join(_humanize_label(segment) for segment in segments)


def _accepted_evidence_manager_rows(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """Build a presentation-only fact-sheet projection from exact path/value rows.

    The source-bound ``rows`` list remains untouched for audit/hash binding.
    Mapping/list values are represented by canonical JSON text so the generic
    table renderer can display them without dropping a container.  Scalars are
    copied without coercion or semantic normalization.
    """

    projected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or "path" not in row or "value" not in row:
            continue
        value = _artifact_json_value(row.get("value"))
        if isinstance(value, (Mapping, list, tuple)):
            value = _canonical_bytes(value).decode("utf-8").rstrip("\n")
        projected.append({
            "label": _accepted_evidence_pointer_label(row.get("path")),
            "value": value,
        })
    return projected


def _accepted_evidence_title(pointer: Any, candidate_kind: str) -> str:
    """Return a neutral pointer-derived title for one accepted candidate."""

    if candidate_kind == "fact_sheet" and _text(pointer).strip() == "/facts":
        return "Business metrics"
    segments = _accepted_evidence_pointer_segments(pointer)
    return _humanize_label(segments[-1]) if segments else "Business metrics"


def _accepted_evidence_candidates(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Enumerate every structural evidence surface without semantic guesses.

    A reviewed ``facts`` object can contain several independent tables next to
    scalar/context facts.  Earlier code selected one table and dropped the
    siblings.  This projection keeps each non-empty list-of-objects at its
    exact JSON pointer and places every sibling scalar/container in a separate
    fact-sheet candidate.  Values are copied only; no narrative parsing or
    calculation occurs.
    """

    facts = record.get("facts")
    if facts is None:
        return []

    table_candidates: list[tuple[str, list[dict[str, Any]]]] = []

    def discover_tables(value: Any, pointer: str) -> None:
        if isinstance(value, list):
            if value and all(isinstance(row, Mapping) for row in value):
                copied = _artifact_json_value(value)
                table_candidates.append(
                    (
                        pointer,
                        [dict(row) for row in copied if isinstance(row, Mapping)],
                    )
                )
            # Do not stop at a table: a reviewed row may itself carry a
            # nested list-of-objects table, which also has an exact pointer.
            for index, child in enumerate(value):
                discover_tables(child, f"{pointer}/{index}")
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                discover_tables(child, f"{pointer}/{_json_pointer_escape(key)}")

    discover_tables(facts, "/facts")
    table_roots = tuple(pointer for pointer, _rows in table_candidates)

    def under_table(pointer: str) -> bool:
        return any(pointer == root or pointer.startswith(root + "/") for root in table_roots)

    fact_rows: list[dict[str, Any]] = []

    def add_fact(pointer: str, value: Any) -> None:
        # ``path`` is an exact source pointer.  ``value`` is copied as-is,
        # including empty containers; neither field carries inferred meaning.
        fact_rows.append({"path": pointer, "value": _artifact_json_value(value)})

    def discover_facts(value: Any, pointer: str) -> None:
        if under_table(pointer):
            return
        if isinstance(value, Mapping):
            if not value:
                add_fact(pointer, value)
                return
            # Recurse through every non-empty mapping so scalar siblings keep
            # their own exact JSON pointers (for example denominator or
            # observed_count). Empty mappings remain explicit containers.
            for key, child in value.items():
                discover_facts(child, f"{pointer}/{_json_pointer_escape(key)}")
            return
        if isinstance(value, list):
            if not value:
                add_fact(pointer, value)
                return
            # Non-table arrays (including mixed/scalar arrays) are preserved as
            # one exact container.  Non-empty object arrays are table roots and
            # are already represented above.
            if not any(root == pointer for root in table_roots):
                add_fact(pointer, value)
            return
        add_fact(pointer, value)

    discover_facts(facts, "/facts")

    candidates: list[dict[str, Any]] = [
        {"kind": "table", "pointer": pointer, "rows": rows}
        for pointer, rows in table_candidates
    ]
    if fact_rows:
        candidates.append({"kind": "fact_sheet", "pointer": "/facts", "rows": fact_rows})
    # Pointer order is the source's structural order.  Keep it stable and
    # independent of any evidence-ledger/title/position preference.
    return candidates


def _accepted_evidence_widgets(
    context: RunContext,
    item_id: str,
    content: Mapping[str, Any],
    accepted_manifest: Mapping[str, Any],
    accepted_content_hash: str | None,
    accepted_manifest_hash: str | None,
) -> list[dict[str, Any]]:
    """Build generic source-bound candidates from accepted evidence records.

    These candidates are deliberately independent of accepted visual
    declarations: a visual without an explicit source binding remains a
    limitation/placeholder, while the evidence ledger can still expose its
    exact reviewed rows for Product selection.  Integration records are never
    consulted, so a technical integration failure cannot erase accepted
    business evidence.
    """

    scope = _accepted_evidence_scope(content, accepted_manifest)
    if scope is None:
        return []
    reference, selected_ids = scope
    artifact_value, artifact_sha = _accepted_visual_artifact(
        context,
        item_id,
        accepted_manifest,
        reference,
    )
    if not isinstance(artifact_value, list):
        return []
    source_ref = f"requirements/{item_id}/{reference}"
    accepted_ref = f"{source_ref}"
    widgets: list[dict[str, Any]] = []
    record_ids: dict[str, int] = {}
    eligible_records: list[tuple[Mapping[str, Any], str]] = []
    for raw_record in artifact_value:
        if not isinstance(raw_record, Mapping):
            continue
        evidence_id = _text(raw_record.get("evidence_id")).strip()
        if not evidence_id or (selected_ids and evidence_id not in selected_ids):
            continue
        record_ids[evidence_id] = record_ids.get(evidence_id, 0) + 1
        eligible_records.append((raw_record, evidence_id))
    for raw_record, evidence_id in eligible_records:
        # Duplicate IDs cannot be bound to one exact ledger record.  Omit all
        # candidates for that ID while retaining every other unique record.
        if record_ids.get(evidence_id, 0) != 1:
            continue
        candidates = _accepted_evidence_candidates(raw_record)
        if not candidates:
            # Empty/unsupported facts remain represented by the original
            # accepted visual limitation and are not turned into guesses.
            continue
        record_evidence_refs = [
            _text(value).strip()
            for value in _as_list(raw_record.get("evidence_refs"))
            if _text(value).strip()
        ]
        limitations = [
            _text(value).strip()
            for value in [*_as_list(content.get("limitations")), *_as_list(raw_record.get("limitations"))]
            if _text(value).strip()
        ]
        record_ref = f"{accepted_ref}#{evidence_id}"
        for candidate in candidates:
            pointer = _text(candidate.get("pointer")).strip()
            rows = candidate.get("rows")
            if not pointer or not isinstance(rows, list):
                continue
            candidate_kind = _text(candidate.get("kind")).strip() or "fact_sheet"
            # The pointer, rather than ledger order or a title, determines the
            # candidate identity.  A short digest prevents collisions from
            # unusual JSON keys while the exact pointer remains inspectable.
            pointer_digest = _sha256_bytes(pointer.encode("utf-8"))[:12]
            candidate_id = (
                f"{_slug(item_id)}-accepted-evidence-{_slug(evidence_id)}-"
                f"{_slug(candidate_kind)}-{pointer_digest}"
            )
            widget = {
                "id": candidate_id,
                "type": "table",
                "title": _accepted_evidence_title(pointer, candidate_kind),
                "accepted_evidence": True,
                "accepted_evidence_id": evidence_id,
                "accepted_evidence_candidate_kind": candidate_kind,
                "accepted_evidence_pointer": pointer,
                "accepted_evidence_source_pointer": pointer,
                "accepted_evidence_ref": record_ref,
                "accepted_evidence_sha256": artifact_sha,
                "accepted_content_hash": accepted_content_hash,
                "accepted_manifest_hash": accepted_manifest_hash,
                "reviewed_item_ref": f"requirements/{item_id}/accepted/manifest.json",
                "reviewed_output_ref": f"requirements/{item_id}/accepted/answer_content.json",
                "evidence_refs": [record_ref, *record_evidence_refs],
                "trace_refs": [
                    f"requirements/{item_id}/accepted/manifest.json",
                    f"requirements/{item_id}/accepted/answer_content.json",
                    source_ref,
                    record_ref,
                ],
                "review_status": "accepted",
                "presentation_role": "decision_view",
                "presentation_tier": "primary",
                "limitations": list(dict.fromkeys(limitations)),
                "audit_payload": copy.deepcopy(dict(raw_record)),
                "accepted_evidence_conclusion": copy.deepcopy(raw_record.get("conclusion")),
                "source_bound": True,
                "rows": copy.deepcopy(rows),
                "manager_rows": (
                    _accepted_evidence_manager_rows(rows)
                    if candidate_kind == "fact_sheet"
                    else copy.deepcopy(rows)
                ),
            }
            # Carry explicit origin metadata when the accepted evidence
            # record provides it.  The renderer and Product plan may inspect
            # these fields for provenance, but they never override plan
            # membership or rewrite the selected projection.
            for key in (
                "technical_surface", "technical_surface_reason",
                "presentation_audience", "presentation_tier", "presentation_role",
                "kind", "manager_admission", "no_geometry_fallback_duplicate",
                "presentation_deduplication",
            ):
                if key in raw_record:
                    widget[key] = copy.deepcopy(raw_record[key])
            if "scope" in content:
                # Keep answer-level scope exact for accepted-evidence
                # projections as well; it is presentation context, not a
                # value inferred from the ledger row.
                widget["answer_scope"] = copy.deepcopy(content.get("scope"))
            if candidate_kind == "fact_sheet":
                widget["accepted_evidence_fact_sheet"] = True
            else:
                widget["accepted_evidence_table_pointer"] = pointer
            widgets.append(widget)
    return widgets


def _accepted_visual_percent(value: Any) -> Any:
    """Return reviewed share geometry in renderer format when unambiguous."""

    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.endswith("%"):
            return stripped
        try:
            number = float(stripped)
        except ValueError:
            return None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    # Accepted answer contracts commonly encode shares as fractions.  The
    # conversion is CSS geometry only; the original share remains in rows.
    if number <= 1:
        number *= 100
    if number > 100:
        return None
    return f"{number:.6f}".rstrip("0").rstrip(".") + "%"


def _accepted_visual_numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _accepted_visual_derived_size(value: Any, values: Sequence[Any]) -> str | None:
    """Scale a supplied numeric value for CSS geometry without changing it."""

    numeric = _accepted_visual_numeric(value)
    magnitudes = [abs(number) for number in (_accepted_visual_numeric(item) for item in values) if number is not None]
    maximum = max(magnitudes, default=0.0)
    if numeric is None or maximum <= 0:
        return None
    return f"{max(0.0, min(100.0, abs(numeric) / maximum * 100.0)):.6f}".rstrip("0").rstrip(".") + "%"


def _accepted_visual_scale_group_specs(
    scale_groups: Any,
) -> list[tuple[str, tuple[str, ...], dict[str, Any]]]:
    """Read explicit accepted-visual scale-group declarations syntactically.

    A declaration maps a group label to the exact series/measure labels that
    belong to it.  Optional ``domain``/``basis`` fields are copied as context;
    neither labels nor metadata are interpreted as business semantics.
    """

    specs: list[tuple[str, tuple[str, ...], dict[str, Any]]] = []

    def member_values(raw: Any) -> tuple[list[str], dict[str, Any]]:
        if isinstance(raw, Mapping):
            metadata = copy.deepcopy(dict(raw))
            for key in ("series", "labels", "measures", "fields", "members"):
                declared = raw.get(key)
                if declared is None:
                    continue
                values = [
                    _text(value).strip()
                    for value in _as_list(declared)
                    if not isinstance(value, (Mapping, list, tuple, set, frozenset))
                    and _text(value).strip()
                ]
                return values, metadata
            return [], metadata
        values = [
            _text(value).strip()
            for value in _as_list(raw)
            if not isinstance(value, (Mapping, list, tuple, set, frozenset))
            and _text(value).strip()
        ]
        return values, {}

    if isinstance(scale_groups, Mapping):
        nested_groups = scale_groups.get("groups")
        if isinstance(nested_groups, (Mapping, list, tuple)):
            scale_groups = nested_groups
        else:
            for raw_group, raw_members in scale_groups.items():
                group = _text(raw_group).strip()
                if not group:
                    continue
                members, metadata = member_values(raw_members)
                specs.append((group, tuple(members), metadata))
            return specs
    if isinstance(scale_groups, (list, tuple)):
        for raw_group in scale_groups:
            if not isinstance(raw_group, Mapping):
                continue
            group = next(
                (
                    _text(raw_group.get(key)).strip()
                    for key in ("group", "id", "name", "label", "key")
                    if _text(raw_group.get(key)).strip()
                ),
                "",
            )
            if not group:
                continue
            members, metadata = member_values(raw_group)
            specs.append((group, tuple(members), metadata))
    return specs


def _accepted_visual_scale_group_for_label(
    label: Any,
    scale_groups: Any,
) -> tuple[str, dict[str, Any]] | None:
    """Return the explicitly declared group for one exact series label."""

    text = _text(label).strip()
    if not text:
        return None
    folded = text.casefold()
    for group, members, metadata in _accepted_visual_scale_group_specs(scale_groups):
        if any(text == member or folded == member.casefold() for member in members):
            return group, copy.deepcopy(metadata)
    return None


def _accepted_visual_scale_group_context(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Copy optional scale-domain/basis context under renderer field names."""

    context: dict[str, Any] = {}
    for output_key, aliases in (
        ("scale_domain", ("scale_domain", "domain")),
        ("scale_basis", ("scale_basis", "basis")),
        ("scale_facet", ("scale_facet", "facet")),
    ):
        for key in aliases:
            if key in metadata and metadata.get(key) not in (None, ""):
                context[output_key] = copy.deepcopy(metadata.get(key))
                break
    return context


_ACCEPTED_TABLE_DIMENSION_ALIASES = frozenset({
    "label", "category", "stage", "exception", "segment", "name", "key", "id",
    "customer_id", "customer_name", "derived_plant", "plant", "carrier", "role", "period",
    "time", "month", "date", "year",
})
_ACCEPTED_TABLE_MEASURE_ALIASES = frozenset({
    "value", "count", "amount", "measure", "n", "total", "quantity", "rate", "share",
    "percent", "percentage", "observations", "activity_count",
})


def _accepted_visual_table_chart_projection(
    visual: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]]] | None:
    """Expose a lossless quantitative table as optional chart geometry.

    Tables remain the exact detail view in ``rows``.  When the reviewed table
    contains one unambiguous dimension and one measure, or explicitly names
    several same-unit measures, this helper adds a chart projection over the
    same supplied values.  It only renames fields and derives CSS width
    percentages; it never aggregates, sorts, or changes a value.  Ambiguous
    tables intentionally remain table-only so a Product Agent cannot be led to
    a silently selected metric.
    """

    source_rows = [row for row in rows if isinstance(row, Mapping)]
    if not source_rows:
        return None
    keys: list[str] = []
    for row in source_rows:
        for key in row:
            key_text = _text(key).strip()
            if key_text and key_text not in keys:
                keys.append(key_text)

    def declared_names(*names: str) -> list[str]:
        result: list[str] = []
        for name in names:
            value = visual.get(name)
            for item in _as_list(value):
                if isinstance(item, str) and item.strip() and item not in result:
                    result.append(item)
        return [name for name in result if name in keys]

    dimensions = declared_names("dimension", "dimensions", "label")
    declared_fields = declared_names("fields", "columns")
    measures = declared_names("measure", "measures")

    def all_present(name: str) -> bool:
        return all(name in row and row.get(name) not in (None, "") for row in source_rows)

    def all_numeric(name: str) -> bool:
        return all(_accepted_visual_numeric(row.get(name)) is not None for row in source_rows)

    if dimensions and not all_present(dimensions[0]):
        dimensions = []
    if measures and not all(all_present(name) and all_numeric(name) for name in measures):
        measures = []
    if not dimensions:
        candidates = [
            key for key in keys
            if key.lower() in _ACCEPTED_TABLE_DIMENSION_ALIASES and all_present(key)
        ]
        # A declared field name is semantic dimension metadata even when its
        # values are numeric-looking business codes (for example plant IDs).
        # Keep such codes exact rather than treating them as measures.
        declared_candidates = [
            key for key in declared_fields
            if key in candidates or key.lower().endswith(("_id", "_name")) and key in keys and all_present(key)
        ]
        if declared_candidates:
            candidates = declared_candidates
        else:
            candidates = [key for key in candidates if not all_numeric(key)]
        if len(candidates) == 1:
            dimensions = candidates
        elif len(candidates) > 1:
            # Keep a human-readable name as the chart label when an accepted
            # table also carries a stable ID.  The ID remains in each exact
            # detail row and is never discarded from the source projection.
            name_candidates = [
                key for key in candidates
                if key.lower() == "name" or key.lower().endswith("_name")
            ]
            if len(name_candidates) == 1:
                dimensions = name_candidates
        # A source-bound table may declare arbitrary business columns rather
        # than one of the conventional aliases.  When that declaration names
        # exactly one non-numeric field, it is an unambiguous chart dimension;
        # preserve all other source columns in the detail rows.
        if not dimensions and declared_fields:
            declared_dimensions = [
                key for key in declared_fields
                if key in keys and all_present(key) and not all_numeric(key)
            ]
            if len(declared_dimensions) == 1:
                dimensions = declared_dimensions
    if not measures:
        candidates = [
            key for key in keys
            if key.lower() in _ACCEPTED_TABLE_MEASURE_ALIASES and all_present(key) and all_numeric(key)
        ]
        # A declaration-free table is chartable only when exactly one
        # semantic numeric field is present.  This prevents rank, IDs, and
        # multiple mixed-unit measures from becoming an arbitrary bar.
        if len(candidates) == 1:
            measures = candidates
        else:
            numeric_fields = [
                key for key in keys
                if all_present(key)
                and all_numeric(key)
                and key not in dimensions
                and key.lower() not in _ACCEPTED_TABLE_DIMENSION_ALIASES
                and not key.lower().endswith(("_id", "_name"))
            ]
            if len(numeric_fields) == 1:
                measures = numeric_fields
        if not measures and declared_fields:
            declared_measures = [
                key for key in declared_fields
                if key in keys
                and key not in dimensions
                and all_present(key)
                and all_numeric(key)
            ]
            if len(declared_measures) == 1:
                measures = declared_measures

    if len(dimensions) != 1 or not measures:
        return None
    dimension = dimensions[0]
    if len(measures) == 1:
        measure = measures[0]
        values = [row.get(measure) for row in source_rows]
        chart_rows: list[dict[str, Any]] = []
        for source_row in source_rows:
            row = dict(source_row)
            row["label"] = source_row.get(dimension)
            row["value"] = source_row.get(measure)
            row["size"] = _accepted_visual_derived_size(row["value"], values)
            if row["label"] in (None, "") or row["size"] in (None, ""):
                return None
            chart_rows.append(row)
        return "bar", chart_rows

    # Grouped bars are offered only when the accepted declaration supplies a
    # same-unit label.  Without that reviewed semantic boundary, several
    # numeric columns are an ambiguous table rather than a truthful chart.
    if not _text(visual.get("unit")).strip():
        return None
    values = [row.get(measure) for row in source_rows for measure in measures]
    group_values: dict[str, list[Any]] = {}
    for measure in measures:
        found = _accepted_visual_scale_group_for_label(measure, visual.get("scale_groups"))
        if found is None:
            continue
        group_values.setdefault(found[0], []).extend(
            row.get(measure)
            for row in source_rows
            if row.get(measure) not in (None, "")
        )
    chart_rows = []
    for source_row in source_rows:
        row = dict(source_row)
        row["label"] = source_row.get(dimension)
        series = []
        for measure in measures:
            value = source_row.get(measure)
            found = _accepted_visual_scale_group_for_label(measure, visual.get("scale_groups"))
            group = found[0] if found is not None else ""
            size_values = group_values.get(group) if len(group_values) > 1 and group else values
            size = _accepted_visual_derived_size(value, size_values or values)
            if size in (None, ""):
                return None
            item = {"label": measure, "value": value, "size": size}
            if found is not None:
                item["scale_group"] = group
                item.update(_accepted_visual_scale_group_context(found[1]))
            series.append(item)
        if row["label"] in (None, "") or not series:
            return None
        row["series"] = series
        chart_rows.append(row)
    return "grouped_bar", chart_rows


def _accepted_visual_limitations(
    visual: Mapping[str, Any],
    source_reason: str | None,
) -> list[str]:
    values: list[str] = []
    for value in _as_list(visual.get("limitations")):
        text = _text(value).strip()
        if text and text not in values:
            values.append(text)
    for key in ("limitation", "note"):
        text = _text(visual.get(key)).strip()
        if text and text not in values:
            values.append(text)
    if source_reason and source_reason not in values:
        values.append(source_reason)
    return values


def _accepted_visual_widgets(
    context: RunContext,
    item_id: str,
    content: Mapping[str, Any],
    accepted_manifest: Mapping[str, Any],
    accepted_content_hash: str | None,
    accepted_manifest_hash: str | None,
) -> list[dict[str, Any]]:
    """Build source-bound presentation candidates from accepted answer visuals."""

    raw_visuals = content.get("visuals")
    # Older/compact accepted answers may omit a visual declaration while
    # still carrying committed typed records; those records already provide
    # the normal decision surface.  Do not manufacture a duplicate visual for
    # that shape.  An explicitly empty ``visuals`` list, in contrast, is a
    # reviewed declaration with no geometry and receives a bounded limitation
    # candidate below.
    if raw_visuals is None:
        return []
    visuals = [value for value in _as_list(raw_visuals) if isinstance(value, Mapping)]
    if raw_visuals is not None and len(visuals) != len(_as_list(raw_visuals)):
        raise AssemblyError(f"{item_id} accepted visuals must be objects")
    if not visuals:
        visuals = [{"title": "Accepted business visual", "type": "table"}]
    # A requirement-level answer may carry one reviewed headline list while
    # declaring several visuals whose optional geometry is unavailable.  Keep
    # that list for one deterministic fallback surface only; later empty
    # declarations remain audit records instead of repeating the same claims.
    requirement_headline_rows: list[dict[str, Any]] = []
    for finding in _as_list(content.get("headline_findings")):
        if isinstance(finding, Mapping):
            value = next(
                (
                    finding.get(key)
                    for key in ("finding", "claim", "text", "body", "message", "title", "label", "value")
                    if finding.get(key) not in (None, "")
                ),
                None,
            )
        else:
            value = finding
        if value not in (None, ""):
            requirement_headline_rows.append({"claim": copy.deepcopy(value)})
    # Keep answer-level limitations separate from each visual's own notes so
    # they can be shown once on the single geometry-less fallback surface.
    answer_limitations: list[Any] = []
    for value in _as_list(content.get("limitations")):
        if _text(value).strip() and value not in answer_limitations:
            answer_limitations.append(copy.deepcopy(value))
    fallback_emitted = False
    widgets: list[dict[str, Any]] = []
    for index, visual in enumerate(visuals):
        declared_context: dict[str, Any] = {}
        raw_type = _text(visual.get("type") or visual.get("kind") or visual.get("chart")).strip().lower()
        normalized_type = _ACCEPTED_VISUAL_TYPE_ALIASES.get(raw_type, "table")
        title = _text(visual.get("title") or visual.get("label") or f"Accepted visual {index + 1}").strip()
        if not title:
            title = f"Accepted visual {index + 1}"
        refs = [visual.get(key) for key in _ACCEPTED_VISUAL_REF_KEYS if visual.get(key) not in (None, "")]
        artifact_value: Any = None
        artifact_ref: str | None = None
        artifact_sha: str | None = None
        if refs:
            # Only one source/artifact ref is meaningful for one accepted
            # visual.  Multiple refs are retained in audit payload but are
            # rejected at the source boundary instead of choosing silently.
            if len(refs) > 1:
                raise AssemblyError(f"{item_id} accepted visual has multiple source references")
            artifact_ref = _text(refs[0]).strip()
            artifact_value, artifact_sha = _accepted_visual_artifact(context, item_id, accepted_manifest, artifact_ref)
        rows, source_reason = _accepted_visual_source_rows(visual, artifact_value)
        # Keep reviewer-declared limitations available to a manager projection;
        # source/shape diagnostics are audit context and must not become
        # visible business claims when a visual has no geometry.
        limitations = _accepted_visual_limitations(visual, None)
        for limitation in answer_limitations:
            if limitation not in limitations:
                limitations.append(limitation)
        audit_limitations = []
        if source_reason:
            audit_limitations.append(source_reason)
        # Preserve explicitly declared business context verbatim.  These
        # fields are presentation metadata, not inferred values; copying them
        # into the candidate lets a V2 plan bind the same context beside its
        # selected chart while the immutable declaration remains in audit.
        for key in (
            "period", "population", "denominator", "unit", "units", "grain",
            "proxy", "proxy_or_limit", "limit", "descriptive", "descriptive_only",
            "causal_status", "as_of", "date_authority", "coverage", "coverage_note",
            "scope", "scope_note", "assumptions", "annotation", "scale_groups",
            "denominator_value", "denominator_label", "x_label", "y_label", "columns",
            # Structural provenance is copied from the accepted origin when
            # present.  It is descriptive metadata only; Product plan
            # membership remains authoritative for manager visibility.
            "technical_surface", "technical_surface_reason",
            "presentation_audience", "presentation_tier", "presentation_role",
            "kind", "manager_admission", "no_geometry_fallback_duplicate",
            "presentation_deduplication",
        ):
            if key in visual:
                # ``base`` is initialized immediately below; stash the exact
                # value until then without normalizing its representation.
                declared_context[key] = copy.deepcopy(visual.get(key))
        # Preserve the answer-level scope independently of a more specific
        # visual scope.  The renderer prefers typed visual scope when present,
        # while this exact prose remains available to a selected fallback or
        # visual without any semantic parsing.
        if "scope" in content:
            declared_context["answer_scope"] = copy.deepcopy(content.get("scope"))
        base: dict[str, Any] = {
            "id": f"{_slug(item_id)}-accepted-visual-{index + 1:02d}",
            "type": normalized_type,
            "title": title,
            "accepted_visual": True,
            "accepted_visual_index": index,
            "accepted_visual_pointer": f"/accepted/visuals/{index}",
            "accepted_visual_type": raw_type or "table",
            "accepted_content_hash": accepted_content_hash,
            "accepted_manifest_hash": accepted_manifest_hash,
            "reviewed_item_ref": f"requirements/{item_id}/accepted/manifest.json",
            "reviewed_output_ref": f"requirements/{item_id}/accepted/answer_content.json",
            "evidence_refs": [
                _text(value).strip()
                for value in _as_list(visual.get("evidence_refs") or visual.get("evidence_ref"))
                if _text(value).strip()
            ],
            "trace_refs": [
                f"requirements/{item_id}/accepted/manifest.json",
                f"requirements/{item_id}/accepted/answer_content.json",
            ],
            "review_status": "accepted",
            "presentation_role": "decision_view",
            "presentation_tier": "primary",
            "limitations": limitations,
            "audit_payload": copy.deepcopy(dict(visual)),
            "source_bound": True,
        }
        base.update(declared_context)
        if artifact_ref is not None:
            base["accepted_artifact_ref"] = artifact_ref
            base["accepted_artifact_sha256"] = artifact_sha
            base["accepted_source_ref"] = artifact_ref
            base["trace_refs"].append(f"requirements/{item_id}/{artifact_ref}")
        # Optional integration hints are useful only for audit association.
        # Keep them source-local and let the accepted-only quarantine below
        # remove malformed values before any strict identity path sees them.
        for key in (
            "integration_record_id",
            "integration_record_ids",
            "integration_record_ref",
            "integration_record_refs",
        ):
            if key in visual:
                base[key] = copy.deepcopy(visual[key])
        # Copy only declared inline/source values.  Semantic normalization
        # below changes field names for the renderer but never changes the
        # supplied business values kept in ``audit_payload``.
        if normalized_type == "kpi":
            if "value" in visual and visual["value"] is not None:
                base["value"] = copy.deepcopy(visual["value"])
            elif rows and len(rows) == 1 and "value" in rows[0]:
                base["value"] = copy.deepcopy(rows[0]["value"])
            else:
                base["type"] = "table"
                base["rows"] = rows
        elif normalized_type in {"donut", "pie", "histogram", "box_plot", "waterfall", "scatter", "stacked_bar"}:
            collection = {"donut": "categories", "pie": "categories", "histogram": "bins",
                          "box_plot": "boxes", "waterfall": "steps", "scatter": "points", "stacked_bar": "segments"}[normalized_type]
            base[collection] = copy.deepcopy(rows)
            # Geometry is derived only from declared typed coordinates; raw
            # values and denominators are never recalculated or rewritten.
            if normalized_type == "histogram":
                values = [row.get("count", row.get("value")) for row in rows]
                for row in base[collection]:
                    if "size" not in row:
                        row["size"] = _accepted_visual_derived_size(row.get("count", row.get("value")), values)
            if not rows:
                base["type"] = "table"
        elif normalized_type == "funnel":
            stages: list[dict[str, Any]] = []
            supplied_values = [row.get("count", row.get("population", row.get("value"))) for row in rows]
            denominator_number = _accepted_visual_numeric(visual.get("denominator"))
            for row in rows:
                copied = dict(row)
                if "label" not in copied:
                    copied["label"] = copied.get("stage") or copied.get("name")
                if "value" not in copied:
                    if "count" in copied:
                        copied["value"] = copied.get("count")
                    elif "population" in copied:
                        copied["value"] = copied.get("population")
                if "size" not in copied:
                    share_value = copied.get("share") if copied.get("share") is not None else copied.get("percent")
                    copied["size"] = _accepted_visual_percent(share_value)
                if copied.get("size") in (None, ""):
                    value_number = _accepted_visual_numeric(copied.get("value"))
                    if denominator_number and value_number is not None:
                        copied["size"] = _accepted_visual_derived_size(value_number, [denominator_number])
                    else:
                        copied["size"] = _accepted_visual_derived_size(copied.get("value"), supplied_values)
                if copied.get("label") not in (None, "") and copied.get("value") not in (None, "") and copied.get("size") not in (None, ""):
                    stages.append(copied)
            if stages:
                base["stages"] = stages
                base["denominator"] = visual.get("denominator")
            else:
                base["type"] = "table"
                if rows:
                    base["rows"] = rows
                audit_limitations.append("Accepted funnel specification lacks explicit stage geometry; reviewed values remain in a table.")
        elif normalized_type in {"line", "area"}:
            points: list[dict[str, Any]] = []
            series_names = [value for value in _as_list(visual.get("series")) if isinstance(value, str) and value.strip()]
            if not series_names:
                series_names = [value for value in _as_list(visual.get("measures")) if isinstance(value, str) and value.strip()]
            if not series_names:
                measure = visual.get("measure")
                if isinstance(measure, str) and measure.strip():
                    series_names = [measure]
            if not series_names:
                series_names = ["value"]
            for row in rows:
                period = row.get("period") or row.get("time") or row.get("month") or row.get("date") or row.get("year") or row.get("x")
                for name in series_names:
                    if name not in row or period in (None, ""):
                        continue
                    point = dict(row)
                    point.update({"label": period, "period": period, "x": period, "value": row[name], "series": row.get("series") if isinstance(row.get("series"), str) else name})
                    points.append(point)
            if len(points) >= 2:
                base["points"] = points
                base["time"] = visual.get("x") or "period"
            else:
                base["type"] = "table"
                if rows:
                    base["rows"] = rows
                audit_limitations.append("Accepted line specification lacks at least two supplied points; reviewed values remain in a table.")
        elif normalized_type in {"bar", "column", "lollipop", "grouped_bar", "diverging_bar", "pareto", "heatmap"}:
            chart_rows: list[dict[str, Any]] = []
            source_values: list[Any] = []
            for source_row in rows:
                source_value = source_row.get("value")
                if source_value in (None, ""):
                    source_value = next(
                        (source_row.get(key) for key in ("count", "amount", "measure", "activity_count", "observations") if key in source_row),
                        None,
                    )
                source_values.append(source_value)
            grouped_values: list[Any] = []
            grouped_values_by_group: dict[str, list[Any]] = {}
            ungrouped_values: list[Any] = []
            scale_group_metadata = {
                group: metadata
                for group, _members, metadata in _accepted_visual_scale_group_specs(visual.get("scale_groups"))
            }

            def series_group(item: Mapping[str, Any] | None, label: Any = None) -> tuple[str, dict[str, Any]]:
                explicit = _text(item.get("scale_group") if isinstance(item, Mapping) else "").strip()
                if explicit:
                    metadata = scale_group_metadata.get(explicit, {})
                    if not metadata:
                        metadata = next(
                            (value for key, value in scale_group_metadata.items() if key.casefold() == explicit.casefold()),
                            {},
                        )
                    return explicit, copy.deepcopy(metadata)
                found = _accepted_visual_scale_group_for_label(
                    label if label is not None else (item.get("label") if isinstance(item, Mapping) else None),
                    visual.get("scale_groups"),
                )
                if found is None:
                    return "", {}
                return found

            def grouped_size(value: Any, group: str = "") -> str | None:
                if len(grouped_values_by_group) > 1 and group:
                    values = grouped_values_by_group.get(group)
                    if values:
                        return _accepted_visual_derived_size(value, values)
                if len(grouped_values_by_group) > 1 and ungrouped_values:
                    return _accepted_visual_derived_size(value, ungrouped_values)
                return _accepted_visual_derived_size(value, grouped_values)

            if normalized_type == "grouped_bar":
                declared_series = [
                    value for value in _as_list(visual.get("series"))
                    if isinstance(value, str) and value.strip()
                ]
                declared_series.extend(
                    value for value in _as_list(visual.get("measures"))
                    if isinstance(value, str) and value.strip() and value not in declared_series
                )
                for source_row in rows:
                    nested_series = source_row.get("series")
                    if isinstance(nested_series, list):
                        for item in nested_series:
                            if not isinstance(item, Mapping) or item.get("value") in (None, ""):
                                continue
                            group, _metadata = series_group(
                                item,
                                item.get("label") or item.get("field") or item.get("name"),
                            )
                            value = item.get("value")
                            grouped_values.append(value)
                            if group:
                                grouped_values_by_group.setdefault(group, []).append(value)
                            else:
                                ungrouped_values.append(value)
                    for name in declared_series:
                        if name not in source_row or source_row.get(name) in (None, ""):
                            continue
                        group, _metadata = series_group(None, name)
                        value = source_row.get(name)
                        grouped_values.append(value)
                        if group:
                            grouped_values_by_group.setdefault(group, []).append(value)
                        else:
                            ungrouped_values.append(value)
                    if source_row.get("value") not in (None, ""):
                        value = source_row.get("value")
                        grouped_values.append(value)
                        ungrouped_values.append(value)
                if not grouped_values:
                    grouped_values = list(source_values)
            for row in rows:
                copied = dict(row)
                if "label" not in copied:
                    label_keys = ["category", "stage", "exception", "segment", "name"]
                    dimension = visual.get("dimension")
                    if isinstance(dimension, str) and dimension.strip():
                        label_keys.append(dimension)
                    for dimension in _as_list(visual.get("dimensions")):
                        if isinstance(dimension, str) and dimension.strip():
                            label_keys.append(dimension)
                    copied["label"] = next(
                        (copied.get(key) for key in label_keys if copied.get(key) not in (None, "")),
                        None,
                    )
                if "value" not in copied:
                    for key in ("count", "amount", "measure", "activity_count", "observations"):
                        if key in copied:
                            copied["value"] = copied[key]
                            break
                if "value" not in copied:
                    measure = visual.get("measure")
                    if isinstance(measure, str) and measure in copied:
                        copied["value"] = copied[measure]
                if "value" not in copied:
                    measures = [
                        value for value in _as_list(visual.get("measures"))
                        if isinstance(value, str) and value in copied
                    ]
                    if measures:
                        copied["value"] = copied[measures[0]]
                if "size" not in copied:
                    share_value = copied.get("share") if copied.get("share") is not None else copied.get("percent")
                    copied["size"] = _accepted_visual_percent(share_value)
                if copied.get("size") in (None, "") and normalized_type in {"bar", "column", "lollipop", "grouped_bar", "diverging_bar", "pareto"}:
                    copied["size"] = _accepted_visual_derived_size(copied.get("value"), source_values)
                if normalized_type == "diverging_bar" and "signed_size" not in copied:
                    raw_signed = copied.get("signed_size")
                    if raw_signed is None:
                        raw_signed = copied.get("value")
                    try:
                        numeric_signed = float(raw_signed)
                    except (TypeError, ValueError):
                        numeric_signed = None
                    if numeric_signed is not None and math.isfinite(numeric_signed):
                        signed_percent = numeric_signed * 100 if abs(numeric_signed) <= 1 else numeric_signed
                        if not -100 <= signed_percent <= 100:
                            magnitudes = [abs(number) for number in (_accepted_visual_numeric(item) for item in source_values) if number is not None]
                            maximum = max(magnitudes, default=0.0)
                            if maximum > 0:
                                signed_percent = numeric_signed / maximum * 100
                        if -100 <= signed_percent <= 100:
                            copied["signed_size"] = f"{signed_percent:.6f}".rstrip("0").rstrip(".") + "%"
                if normalized_type == "grouped_bar" and not isinstance(copied.get("series"), list):
                    series_names = [
                        value for value in declared_series
                        if isinstance(value, str) and value.strip() and value in copied
                    ]
                    if series_names:
                        projected_series: list[dict[str, Any]] = []
                        for name in series_names:
                            group, metadata = series_group(None, name)
                            series_item = {
                                "label": name,
                                "value": copied.get(name),
                                # The source values remain exact; only this
                                # CSS geometry is normalized.  Explicit scale
                                # groups use their own reviewed maximum.
                                "size": grouped_size(copied.get(name), group),
                            }
                            if group:
                                series_item["scale_group"] = group
                                series_item.update(_accepted_visual_scale_group_context(metadata))
                            projected_series.append(series_item)
                        copied["series"] = projected_series
                elif normalized_type == "grouped_bar" and isinstance(copied.get("series"), list):
                    normalized_series: list[dict[str, Any]] = []
                    for item in copied["series"]:
                        if not isinstance(item, Mapping):
                            continue
                        series_item = dict(item)
                        group, metadata = series_group(
                            series_item,
                            series_item.get("label") or series_item.get("field") or series_item.get("name"),
                        )
                        if group:
                            series_item.setdefault("scale_group", group)
                            for key, value in _accepted_visual_scale_group_context(metadata).items():
                                series_item.setdefault(key, value)
                        if series_item.get("size") in (None, ""):
                            series_item["size"] = grouped_size(series_item.get("value"), group)
                        normalized_series.append(series_item)
                    copied["series"] = normalized_series
                chart_rows.append(copied)
            required_ok = bool(chart_rows) and all(row.get("label") not in (None, "") and row.get("value") not in (None, "") for row in chart_rows)
            if normalized_type == "bar":
                required_ok = required_ok and all(row.get("size") not in (None, "") for row in chart_rows)
                if required_ok:
                    base["bars"] = chart_rows
                else:
                    base["type"] = "table"
                    if rows:
                        base["rows"] = rows
                    audit_limitations.append("Accepted bar specification lacks supplied bounded geometry; reviewed values remain in a table.")
            elif normalized_type == "grouped_bar":
                required_ok = bool(chart_rows) and all(
                    row.get("label") not in (None, "")
                    and isinstance(row.get("series"), list)
                    and bool(row.get("series"))
                    and all(
                        isinstance(series_item, Mapping)
                        and series_item.get("value") not in (None, "")
                        and series_item.get("size") not in (None, "")
                        for series_item in row.get("series", [])
                    )
                    for row in chart_rows
                )
                if required_ok:
                    base["bars"] = chart_rows
                else:
                    base["type"] = "table"
                    if rows:
                        base["rows"] = rows
                    audit_limitations.append("Accepted paired-bar specification lacks explicit grouped geometry; reviewed values remain in a table.")
            elif normalized_type == "diverging_bar":
                required_ok = required_ok and all(row.get("signed_size") not in (None, "") for row in chart_rows)
                if required_ok:
                    base["bars"] = chart_rows
                else:
                    base["type"] = "table"
                    if rows:
                        base["rows"] = rows
                    audit_limitations.append("Accepted diverging-bar specification lacks supplied signed geometry; reviewed values remain in a table.")
            elif normalized_type == "pareto":
                required_ok = required_ok and all(
                    row.get("size") not in (None, "")
                    and any(row.get(key) not in (None, "") for key in ("cumulative_size", "cumulative_percent", "cumulative_share"))
                    for row in chart_rows
                )
                if required_ok:
                    base["rows"] = chart_rows
                else:
                    base["type"] = "table"
                    if rows:
                        base["rows"] = rows
                    audit_limitations.append("Accepted Pareto specification lacks supplied cumulative geometry; reviewed values remain in a table.")
            else:  # heatmap
                cells = []
                for row in chart_rows:
                    if any(row.get(key) not in (None, "") for key in ("row", "row_label", "y")) and any(row.get(key) not in (None, "") for key in ("column", "column_label", "x")):
                        cells.append(row)
                if cells:
                    base["cells"] = cells
                else:
                    base["type"] = "table"
                    if rows:
                        base["rows"] = rows
                    audit_limitations.append("Accepted heatmap specification lacks explicit row/column cells; reviewed values remain in a table.")
        else:
            if rows:
                base["rows"] = rows
            if normalized_type != "table":
                base["type"] = "table"
            else:
                # Keep the accepted table as the exact detail surface while
                # exposing an optional chart projection when the reviewed
                # rows have one unambiguous dimension/measure shape.  The
                # Product Agent may choose that recipe; no metric is selected
                # or recomputed here.
                if raw_type not in {"relationship_matrix", "callout", "evidence_callout"}:
                    projection = _accepted_visual_table_chart_projection(visual, rows)
                    if projection is not None:
                        projection_type, chart_rows = projection
                        base["bars"] = chart_rows
                        if projection_type == "grouped_bar" and _text(visual.get("unit")).strip():
                            base["unit"] = copy.deepcopy(visual.get("unit"))
            audit_limitations.append("Accepted visual specification is retained as a source-bound table/callout; no additional analytics were computed.")
        # Preserve the accepted content limitation alongside shape-specific
        # caveats; deduplicate while retaining reviewed order.
        base["limitations"] = list(dict.fromkeys(value for value in limitations if _text(value).strip()))
        if audit_limitations:
            base["audit_limitations"] = list(dict.fromkeys(value for value in audit_limitations if _text(value).strip()))
        if base.get("value") is None and not base.get("rows") and not any(base.get(key) for key in ("bars", "points", "stages", "cells", "categories", "bins", "boxes", "steps", "segments")):
            # A visual declaration may be accepted even when its optional
            # geometry source is unavailable/ambiguous.  Expose the exact
            # reviewed headline findings (or stated limits) once as a
            # source-bound decision card rather than a renderer placeholder.
            # No narrative numbers are mined and no metric is synthesized.
            if not fallback_emitted:
                fallback_rows = copy.deepcopy(requirement_headline_rows)
                if not fallback_rows:
                    fallback_rows = [
                        {"claim": copy.deepcopy(value)}
                        for value in answer_limitations
                        if _text(value).strip()
                    ]
                if not fallback_rows:
                    fallback_rows = [
                        {"claim": copy.deepcopy(value)}
                        for value in limitations
                        if _text(value).strip()
                    ]
                if not fallback_rows:
                    fallback_rows = [{"claim": "No reviewed values were supplied for this view."}]
                base["presentation_role"] = "finding_list"
                base["rows"] = fallback_rows
                # Requirement-level limitations belong to this one fallback
                # card.  Subsequent geometry-less declarations retain only
                # their own visual-specific limitations in audit.
                base["limitations"] = list(
                    dict.fromkeys(
                        value
                        for value in [*answer_limitations, *limitations]
                        if _text(value).strip()
                    )
                )
                fallback_emitted = True
            else:
                # Preserve the declaration and its exact audit payload, but
                # keep additional geometry-less visuals out of manager cards.
                base["presentation_role"] = "finding_record"
                base["presentation_tier"] = "audit"
                base["no_geometry_fallback_duplicate"] = True
        _quarantine_accepted_visual_integration_hints(base)
        widgets.append(base)
    return widgets


def _analytical_artifact_provenance(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable artifact lineage envelope carried by a record."""

    payload = _record_payload(record)
    if _text(record.get("kind")) != "analytical_artifact" or not isinstance(payload.get("artifact"), Mapping):
        return {}
    return {
        "artifact_id": payload.get("artifact_id"),
        "artifact_type": payload.get("artifact_type"),
        "schema_version": payload.get("schema_version"),
        "requirement_id": payload.get("requirement_id"),
        "content_hash": payload.get("content_hash"),
        "envelope_hash": payload.get("envelope_hash"),
        "canonical_bytes_sha256": payload.get("canonical_bytes_sha256"),
        "artifact_ref": payload.get("artifact_ref"),
        "integration_record_id": record.get("record_id"),
        "integration_record_hash": record.get("record_hash"),
    }


def _artifact_json_value(value: Any) -> Any:
    """Copy immutable artifact values into ordinary JSON containers."""

    if isinstance(value, Mapping):
        return {str(key): _artifact_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_artifact_json_value(item) for item in value]
    return value


def _artifact_rows(value: Any) -> list[Mapping[str, Any]]:
    """Project supplied artifact values into lossless table rows.

    The assembler only changes presentation shape: it never sums, divides,
    normalizes, or otherwise calculates from the typed artifact payload.
    """

    if isinstance(value, Mapping):
        return [{"field": str(key), "value": _artifact_json_value(item)} for key, item in value.items()]
    if isinstance(value, (list, tuple)):
        rows: list[Mapping[str, Any]] = []
        for index, item in enumerate(value):
            if isinstance(item, Mapping):
                rows.append(_artifact_json_value(item))
            else:
                rows.append({"index": index, "value": _artifact_json_value(item)})
        return rows
    if value is None:
        return []
    return [{"value": _artifact_json_value(value)}]


def _artifact_widget_base(
    item_id: str,
    content: Mapping[str, Any],
    record: Mapping[str, Any],
    artifact: Any,
    *,
    title: str,
    suffix: str | None = None,
) -> dict[str, Any]:
    widget = _widget_base(item_id, content, record, title=title)
    if suffix:
        widget["id"] = f"{widget['id']}-{_slug(suffix)}"
    provenance = _analytical_artifact_provenance(record)
    widget["artifact_provenance"] = provenance
    # Flat aliases keep provenance easy to inspect in fixture/chart-map output
    # while the mapping above remains the canonical grouped envelope.
    widget["analytical_artifact_id"] = artifact.artifact_id
    widget["analytical_artifact_type"] = artifact.artifact_type
    widget["analytical_artifact_ref"] = provenance.get("artifact_ref")
    widget["analytical_artifact_content_hash"] = artifact.content_hash
    widget["analytical_artifact_envelope_hash"] = artifact.envelope_hash
    widget["analytical_artifact_canonical_bytes_sha256"] = provenance.get("canonical_bytes_sha256")
    widget["artifact_type"] = artifact.artifact_type
    widget["artifact_ref"] = provenance.get("artifact_ref")
    return widget


def _analytical_artifact_widgets(
    item_id: str,
    content: Mapping[str, Any],
    record: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build presentation-only widgets from one committed typed artifact."""

    payload = _record_payload(record)
    raw_artifact = payload.get("artifact")
    try:
        from auto_foundry_core.analytical_artifacts import AnalyticalArtifact

        artifact = AnalyticalArtifact.from_dict(raw_artifact)
    except Exception as exc:  # pragma: no cover - committed loader catches first
        raise AssemblyError(f"analytical artifact record is invalid: {_text(record.get('record_id'))}") from exc
    artifact_type = artifact.artifact_type
    title = _text(artifact.metadata.get("title") if isinstance(artifact.metadata, Mapping) else "") or artifact_type.replace("_", " ").title()
    provenance = _analytical_artifact_provenance(record)
    widgets: list[dict[str, Any]] = []

    def table(value: Any, label: str, suffix: str) -> None:
        rows = _artifact_rows(value)
        widget = _artifact_widget_base(item_id, content, record, artifact, title=label, suffix=suffix)
        widget.update({"type": "table", "rows": rows, "presentation_role": "decision_view", "presentation_tier": "primary"})
        widgets.append(widget)

    if artifact_type == "data_profile":
        table(artifact.payload.get("profile"), title, "profile")
        return widgets
    if artifact_type == "kpi_table":
        table(artifact.payload.get("rows"), title, "kpi-table")
        return widgets
    if artifact_type == "segment_profiles":
        table(artifact.payload.get("profiles"), title, "segment-profiles")
        return widgets
    if artifact_type != "segmentation_model":
        # Future typed artifact kinds remain visible as a lossless table; the
        # strict integration contract still owns their schema/hash checks.
        table(artifact.payload, title, "artifact")
        return widgets

    model = artifact.payload.get("model")
    profile_payload = artifact.payload.get("segment_profiles")
    segment_sizes = profile_payload.get("segment_sizes") if isinstance(profile_payload, Mapping) else None
    explicit_sizes = isinstance(segment_sizes, Mapping) and bool(segment_sizes)
    explicit_numeric_sizes = explicit_sizes and all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0 <= float(value)
        for value in segment_sizes.values()
    )
    # Segment sizes are population counts, never percentages.  The horizontal
    # bar family interprets ``size`` as a bounded percentage, so it cannot
    # represent these values faithfully even when every count happens to be
    # <=100.  Always use the existing column family's raw-count mode and copy
    # exact supplied values without normalization, shares, or aggregation.
    if explicit_numeric_sizes:
        column = _artifact_widget_base(item_id, content, record, artifact, title=f"{title} · segment counts", suffix="segment-counts")
        column.update({
            "type": "column",
            "categories": [
                {
                    "label": _text(key),
                    "series": [{"label": "Count", "value": copy.deepcopy(segment_sizes[key])}],
                }
                for key in sorted(segment_sizes, key=_text)
            ],
            "presentation_geometry_only": True,
            "geometry_basis": "explicit supplied segment counts",
            "geometry_mode": "raw_counts",
        })
        widgets.append(column)
    else:
        # Never invent a share/ratio when the artifact did not provide segment
        # counts.  Keep the model payload visible and state why no geometry is
        # shown.
        table(model, f"{title} · model", "model")
        widgets[-1]["limitations"] = list(widgets[-1].get("limitations", [])) + [
            "No explicit segment counts were supplied; no share or ratio was calculated."
        ]

    if isinstance(model, Mapping):
        assignments = model.get("assignments")
        if isinstance(assignments, (list, tuple)) and assignments:
            table(assignments, f"{title} · assignments", "assignments")
        candidate_validation = model.get("candidate_k_validation")
        if isinstance(candidate_validation, (list, tuple)) and candidate_validation:
            table(candidate_validation, f"{title} · validation", "validation")
    if isinstance(profile_payload, Mapping):
        profiles = profile_payload.get("segment_profiles")
        if isinstance(profiles, (list, tuple)) and profiles:
            table(profiles, f"{title} · profiles", "profiles")
    if not widgets:
        table(model or profile_payload or artifact.payload, title, "artifact")
    return widgets


def _metric_widget(item_id: str, content: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    payload = _record_payload(record)
    value = payload.get("value")
    title = _text(payload.get("label") or payload.get("name") or record.get("record_id"))
    # Structured metrics (components, source populations, breakdowns, etc.)
    # intentionally have no scalar ``value``.  They are still reviewed
    # presentation data and must remain visible as a faithful table instead
    # of becoming a misleading KPI card with an em-dash.  A non-flat mapping
    # or list is treated the same way: the lossless table below preserves the
    # complete reviewed payload before any chart-family classification can
    # discard context.
    if _requires_lossless_metric_table(payload):
        rows = _structured_metric_rows(payload)
        if rows:
            widget = _table_widget(item_id, content, record, title=title, rows=rows)
            widget["presentation_role"] = "support_metric"
            widget["manager_rows"] = _manager_metric_rows(payload)
            return widget
    widget = _widget_base(item_id, content, record, title=title)
    widget["type"] = "kpi"
    widget["value"] = value
    numerator = payload.get("numerator")
    denominator_value = payload.get("denominator")
    if numerator is not None and denominator_value is not None:
        # This is an explicit reviewed numerator/denominator display, not an
        # inferred rate or percentage.  The exact scalar ratio remains in
        # ``value`` and the denominator is carried into audit/context.
        widget["manager_display_value"] = f"{_text(numerator)} of {_text(denominator_value)}"
        widget["denominator"] = denominator_value
    if isinstance(value, Mapping):
        numeric_map = _numeric_map(value)
        if numeric_map is None:
            rows = [{"field": key, "value": item} for key, item in sorted(value.items(), key=lambda pair: _text(pair[0]))]
            widget = _table_widget(item_id, content, record, title=title, rows=rows)
            widget["presentation_role"] = "support_metric"
            widget["manager_rows"] = _manager_metric_rows(payload)
            return widget
        if any(not key.strip() for key in numeric_map) and not _currency_partition(payload, numeric_map):
            # Empty category labels are not safe chart dimensions (and the
            # renderer rejects them).  Keep the exact source value visible in
            # a table rather than inventing an ``unknown`` bucket or zero.
            rows = [{"field": key, "value": item} for key, item in sorted(value.items(), key=lambda pair: _text(pair[0]))]
            widget = _table_widget(item_id, content, record, title=title, rows=rows)
            widget["presentation_role"] = "support_metric"
            widget["manager_rows"] = _manager_metric_rows(payload)
            return widget
        if not _flat_map_comparable(payload):
            rows = [{"field": key, "value": item} for key, item in sorted(value.items(), key=lambda pair: _text(pair[0]))]
            widget = _table_widget(item_id, content, record, title=title, rows=rows)
            widget["presentation_role"] = "support_metric"
            widget["manager_rows"] = _manager_metric_rows(payload)
            return widget
        if _currency_partition(payload, numeric_map):
            widget["type"] = "metric_grid"
            widget.pop("value", None)
            widget["tiles"] = [
                {
                    "label": key if key.strip() else "(blank source currency)",
                    "source_key": key,
                    "value": value[key],
                }
                for key in sorted(value)
            ]
            widget["limitations"] = list(widget.get("limitations", [])) + ["Currency partitions remain separate; no FX conversion or cross-currency total is shown."]
            return widget
        if _signed_shape(payload, numeric_map):
            maximum = max(abs(item) for item in numeric_map.values())
            rows = [{"label": key, "value": value[key], "signed_size": _format_geometry((item / maximum) * 100 if maximum else 0)} for key, item in sorted(numeric_map.items())]
            widget.update({"type": "diverging_bar", "bars": rows, "presentation_geometry_only": True, "geometry_basis": "stable max-absolute normalization"})
            return widget
        denominator = _numeric(payload.get("denominator") or payload.get("population"))
        total = sum(numeric_map.values())
        # A flat, non-negative scalar map may be shown as part-to-whole when
        # its explicit population/denominator reconciles exactly.  This is a
        # presentation classification, not a new analytical calculation; all
        # source values and the denominator remain in the widget.
        explicit_part_to_whole = (
            denominator is not None
            and denominator > 0
            and all(item >= 0 for item in numeric_map.values())
            and 2 <= len(numeric_map) <= 5
            and abs(total - denominator) <= max(0.000001, abs(denominator) * 0.000001)
        )
        if explicit_part_to_whole and 2 <= len(numeric_map) <= 5:
            categories = [{"label": key, "value": value[key], "size": _format_geometry((numeric_map[key] / denominator) * 100 if denominator else 0)} for key in sorted(numeric_map)]
            # Semantic/data-shape default for an initial plan.  Product Agent
            # may select another eligible composition recipe later; record IDs
            # never influence chart family selection.
            family = "donut"
            widget.update({"type": family, "denominator_value": payload.get("denominator") or payload.get("population"), "denominator_label": _text(payload.get("denominator_label") or payload.get("units") or "rows"), "presentation_geometry_only": True, "geometry_basis": "explicit denominator composition"})
            if family == "stacked_composition":
                widget["segments"] = categories
            else:
                widget["categories"] = categories
            return widget
        maximum = max(abs(item) for item in numeric_map.values())
        # A flat comparable map has no reviewed ranking or time semantics, so
        # use the neutral vertical category comparison as its deterministic
        # initial chart default.  Alternate eligible recipes remain a plan
        # choice, never a record-ID hash.
        kind = "column"
        rows = [{"label": key, "value": value[key], "size": _format_geometry((abs(item) / maximum) * 100 if maximum else 0)} for key, item in sorted(numeric_map.items())]
        widget.update({"type": kind, "bars": rows, "presentation_geometry_only": True, "geometry_basis": "stable max normalization; values remain exact"})
        return widget
    if _scalar_currency_partition(payload):
        # Keep the exact source value but make the absence of FX/aggregation
        # explicit.  A one-tile metric grid is intentionally not a KPI total.
        widget["type"] = "metric_grid"
        widget.pop("value", None)
        widget["tiles"] = [{"label": title, "value": value}]
        widget["limitations"] = list(widget.get("limitations", [])) + ["Currency partitions remain source-local; no FX conversion or cross-currency total is shown."]
        return widget
    coverage = _explicit_coverage(payload)
    if coverage is not None:
        ratio, denominator = coverage
        widget.update({"type": "progress", "bars": [{"label": title, "value": value, "size": _format_geometry(ratio * 100)}], "population": denominator, "presentation_geometry_only": True, "geometry_basis": "explicit reviewed coverage"})
        return widget
    if isinstance(value, list):
        rows = [{"row": index + 1, "value": item} for index, item in enumerate(value)]
        return _table_widget(item_id, content, record, title=title, rows=rows)
    return widget


def _requires_lossless_metric_table(payload: Mapping[str, Any]) -> bool:
    """Return whether a metric payload needs a lossless table projection.

    Flat numeric ``value`` maps remain eligible for the existing reviewed
    chart families (currency partitions, composition, and bars).  Any null,
    list, nested mapping, or structured companion field instead gets the
    deterministic recursive projection so no source field is silently
    dropped.  This boundary is deliberately shape-based and performs no
    numeric coercion.
    """

    value = payload.get("value")
    if value is None or isinstance(value, list):
        return True
    # An empty/blank scalar is not a usable KPI.  Route it to the recursive
    # table even when the record carries no other structured field, and keep
    # it out of overview selection by construction (tables are not KPIs).
    if isinstance(value, str) and not value.strip():
        return True
    structured_keys = {"components", "sources", "rows", "items", "records", "series", "breakdown"}
    if structured_keys.intersection(payload):
        return True
    if any(isinstance(item, (Mapping, list)) for key, item in payload.items() if key != "value"):
        return True
    if isinstance(value, Mapping):
        # Flat numeric maps are still useful reviewed chart inputs when their
        # only siblings are presentation controls (label/unit/population).
        # Provenance/authority companions, however, make chart-only paths
        # lossy: the complete payload must be rendered as recursive rows.
        chart_control_keys = {
            "label", "name", "units", "unit", "population", "denominator",
            "denominator_label", "limitations", "grain", "period", "period_from",
            "period_to", "shape", "value_shape", "signed", "signed_comparable",
            "comparability", "comparison", "comparable", "category_comparable",
            "currency_partitions", "currencies", "denominators", "status_rows",
            "watchlist_rows", "source_population", "target_population",
        }
        if any(key not in chart_control_keys and key != "value" for key in payload):
            return True
        # Keep the established flat numeric-map visualizations intact.  A map
        # containing nested values is not chart-safe and must be fully
        # represented in the table instead.
        return _numeric_map(value) is None
    return False


def _structured_metric_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten one reviewed structured metric without dropping context.

    A structured metric is not a scalar KPI.  Earlier normalization returned
    as soon as it found ``components``/``series``/``breakdown`` and silently
    discarded top-level review fields (metric name, source, date authority,
    units, totals, and conflict flags).  The dashboard is a faithful view of
    accepted records, so context rows are emitted first in deterministic key
    order, followed by the nested values.  No value is calculated, coerced, or
    replaced with zero.
    """

    rows: list[dict[str, Any]] = []

    def value_json(value: Any) -> str:
        # JSON is the faithful display representation for null, empty strings,
        # mappings, and lists.  It is intentionally not parsed or normalized
        # into a number/string surrogate.
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def append_detail(path: str, value: Any, *, row_kind: str = "detail") -> None:
        field = path.rsplit(".", 1)[-1]
        rows.append(
            {
                "row_kind": row_kind,
                "path": path,
                "field": field,
                "value": value,
                "value_json": value_json(value),
            }
        )
        if isinstance(value, Mapping):
            for child_key in sorted(value, key=lambda item: _text(item)):
                child_path = f"{path}.{_text(child_key)}"
                append_detail(child_path, value[child_key])
        elif isinstance(value, list):
            for index, child in enumerate(value, 1):
                append_detail(f"{path}[{index}]", child)

    # Emit one context row for every top-level key, including ``value`` when it
    # is null/empty and structured payloads in their entirety.  This preserves
    # the compatibility fields consumed by existing fixtures while the detail
    # rows below provide a complete path-addressable recursive projection.
    for key, value in sorted(payload.items(), key=lambda pair: _text(pair[0])):
        field = _text(key)
        rows.append(
            {
                "row_kind": "context",
                "path": field,
                "field": field,
                "value": value,
                "value_json": value_json(value),
            }
        )
        if isinstance(value, (Mapping, list)):
            append_detail(field, value)

    # Preserve the established first-level nested rows used by dashboard
    # review contracts.  Recursive ``detail`` rows carry the complete child
    # payload without changing the meaning/order of these compatibility rows.
    for key in ("components", "sources", "rows", "items", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            for index, item in enumerate(value, 1):
                if isinstance(item, Mapping):
                    row = {"row_kind": "nested", "source_field": key, "source_index": index, **dict(item)}
                else:
                    row = {"row_kind": "nested", "source_field": key, "source_index": index, "value": item}
                row["path"] = f"{key}[{index}]"
                row["value_json"] = value_json(item)
                rows.append(row)
    series = payload.get("series")
    if isinstance(series, list):
        for index, item in enumerate(series, 1):
            rows.append(
                {
                    "row_kind": "nested",
                    "source_field": "series",
                    "source_index": index,
                    "value": item,
                    "path": f"series[{index}]",
                    "value_json": value_json(item),
                }
            )
    breakdown = payload.get("breakdown")
    if isinstance(breakdown, Mapping):
        for key, value in sorted(breakdown.items(), key=lambda pair: _text(pair[0])):
            if isinstance(value, Mapping):
                row = {"row_kind": "nested", "source_field": "breakdown", "category": _text(key), **dict(value)}
            else:
                row = {"row_kind": "nested", "source_field": "breakdown", "category": _text(key), "value": value}
            row["path"] = f"breakdown.{_text(key)}"
            row["value_json"] = value_json(value)
            rows.append(row)
    return rows


def _nested_numeric_maps(payload: Mapping[str, Any]) -> dict[str, dict[str, float]] | None:
    """Return independently chartable numeric children from a mixed mapping.

    Accepted records often carry scalar summaries, previews, and a handful of
    flat maps together.  Requiring every top-level value to be a mapping made
    the useful maps disappear into one large table.  We therefore extract only
    children whose values are all numeric; complex siblings remain available to
    the conservative table fallback in ``_metric_widgets``.
    """

    value = payload.get("value")
    if not isinstance(value, Mapping) or not value:
        return None
    nested: dict[str, dict[str, float]] = {}
    for key, item in value.items():
        parsed = _numeric_map(item)
        if parsed is not None:
            nested[_text(key)] = parsed
    return nested or None


def _nested_denominator(payload: Mapping[str, Any], subkey: str, submap: Mapping[str, float]) -> tuple[Any, str] | None:
    """Find an explicit sibling denominator that reconciles exactly.

    A denominator is used for composition geometry only when its supplied
    value equals the exact sum of the child map.  This prevents a broad
    population count from being silently treated as the denominator for an
    unrelated category map.
    """

    total = sum(submap.values())
    if total < 0:
        return None
    stem = re.sub(r"(?:_counts?|_types?|_map)$", "", subkey)
    stems = [stem]
    # ``watchlist_action_counts`` is normally reconciled to the containing
    # metric's ``watchlist_rows``; similarly status/type/category maps may use
    # the broader population stem.  Keep this expansion lexical and bounded.
    for suffix in ("_action", "_status", "_type", "_category"):
        if stem.endswith(suffix):
            stems.append(stem[: -len(suffix)])
    candidate_keys = [
        *(f"{candidate}_rows" for candidate in stems),
        "rows",
        *(f"{candidate}_count" for candidate in stems),
        "population",
        "denominator",
        "source_population",
        "target_population",
    ]
    sibling_denominators = payload.get("denominators")
    candidates: list[tuple[str, Any]] = []
    if isinstance(sibling_denominators, Mapping) and subkey in sibling_denominators:
        candidates.append((f"denominators.{subkey}", sibling_denominators[subkey]))
    # Integrated metric records commonly place scalar siblings (for example
    # ``watchlist_rows`` or ``rows``) beside the child map inside ``value``.
    # Check that containing mapping first, then explicit payload-level fields.
    containing_value = payload.get("value")
    for key in candidate_keys:
        if isinstance(containing_value, Mapping) and key in containing_value:
            candidates.append((f"value.{key}", containing_value[key]))
        if key in payload:
            candidates.append((key, payload[key]))
    for key, raw in candidates:
        numeric = _numeric(raw)
        if numeric is None or numeric <= 0:
            continue
        if abs(total - numeric) <= max(0.000001, abs(numeric) * 0.000001):
            return raw, key
    return None


def _entity_leaderboard(value: Mapping[str, Any]) -> tuple[str, dict[str, float]] | None:
    """Collapse an entity -> metric map into one deterministic leaderboard.

    Supplier/entity maps can contain several fields per entity.  Rendering one
    widget per entity is not decision-sized, so when a stable exception field
    is shared by every entity we draw one chart across entities.  If no common
    exception field exists, the caller falls back to one honest table.
    """

    if not value or not all(isinstance(item, Mapping) for item in value.values()):
        return None
    keys = [_text(key) for key in value]
    if not any(":" in key or re.match(r"(?i)^(supplier|vendor|customer|sku|warehouse|wh)[_:-]", key) for key in keys):
        return None
    parsed_children: dict[str, dict[str, float]] = {}
    for key, item in value.items():
        parsed = _numeric_map(item)
        if parsed is None:
            return None
        parsed_children[_text(key)] = parsed
    common = set.intersection(*(set(item) for item in parsed_children.values()))
    priorities = ("late", "overdue", "exception", "mismatch", "unmatched", "open", "pending", "error", "count")
    for token in priorities:
        candidates = sorted(field for field in common if token in field.lower())
        if candidates:
            field = candidates[0]
            return field, {key: children[field] for key, children in sorted(parsed_children.items())}
    return None


def _metric_widgets(item_id: str, content: Mapping[str, Any], record: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = _record_payload(record)
    nested = _nested_numeric_maps(payload)
    if nested is None:
        return [_metric_widget(item_id, content, record)]
    value = payload.get("value")
    if isinstance(value, Mapping):
        leaderboard = _entity_leaderboard(value)
        if leaderboard is not None:
            field, aggregate = leaderboard
            subpayload = dict(payload)
            subpayload["value"] = aggregate
            subpayload["label"] = f"{_text(payload.get('label') or payload.get('name') or record.get('record_id'))} · {field} by entity"
            subrecord = dict(record)
            subrecord["payload"] = subpayload
            widget = _metric_widget(item_id, content, subrecord)
            widget["source_metric_key"] = field
            widget["entity_projection"] = "deterministic common exception field"
            return [widget]
    base_title = _text(payload.get("label") or payload.get("name") or record.get("record_id"))
    widgets: list[dict[str, Any]] = []
    for subkey, submap in sorted(nested.items()):
        subpayload = dict(payload)
        subpayload["value"] = submap
        subpayload["label"] = f"{base_title} · {subkey}"
        denominator = _nested_denominator(payload, subkey, submap)
        for denominator_key in ("denominator", "population", "source_population", "target_population"):
            subpayload.pop(denominator_key, None)
        if denominator is not None:
            raw_denominator, denominator_key = denominator
            # Preserve the key's semantic label while allowing the generic
            # metric classifier to apply the reconciled composition rule.
            subpayload["denominator"] = raw_denominator
            subpayload["denominator_label"] = denominator_key
        subrecord = dict(record)
        subrecord["payload"] = subpayload
        widget = _metric_widget(item_id, content, subrecord)
        widget["id"] = f"{_slug(item_id)}-{_slug(record.get('record_id'))}-{_slug(subkey)}"
        widget["source_metric_key"] = subkey
        widgets.append(widget)
        if len(widgets) >= 6:
            break
    # If there are no (or only one) chartable child maps, retain complex
    # siblings as one collapsed table instead of silently dropping them.  A
    # rich set of deterministic charts remains decision-sized without a second
    # prose/preview table per metric.
    complex_keys = [key for key in value if _text(key) not in nested]
    if complex_keys and len(widgets) < 2:
        rows = [{"field": _text(key), "value": value[key]} for key in sorted(complex_keys, key=_text)]
        table = _table_widget(item_id, content, record, title=base_title, rows=rows)
        widgets.append(table)
    return widgets


def _relationship_widget(item_id: str, content: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(records, key=lambda value: _text(value.get("record_id")))
    first = ordered[0]
    payload = _record_payload(first)
    title = "Relationship coverage"
    widget = _widget_base(item_id, content, first, title=title)
    widget["id"] = f"{_slug(item_id)}-relationship-coverage"
    widget["presentation_role"] = "relationship_matrix"
    widget["presentation_tier"] = "primary"
    widget["integration_record_ids"] = [_safe_record_ref(record.get("record_id")) for record in ordered]
    widget["integration_record_refs"] = [f"requirements/{item_id}/integration/committed/records.jsonl" for _ in ordered]
    widget["trace_refs"] = sorted({ref for record in ordered for ref in _record_refs(item_id, record)[1]})
    widget["evidence_refs"] = sorted({ref for record in ordered for ref in _record_refs(item_id, record)[0]})
    rows: list[dict[str, Any]] = []
    audit_payloads: list[Mapping[str, Any]] = []
    for record in ordered:
        relationship_payload = _record_payload(record)
        audit_payloads.append(dict(relationship_payload))
        source = relationship_payload.get("source_id")
        target = relationship_payload.get("target_id")
        row: dict[str, Any] = {
            "Relationship": f"{_endpoint_label(source)} → {_endpoint_label(target)}",
            "Source": _endpoint_label(source),
            "Target": _endpoint_label(target),
        }
        for side, label in (("source", "Source coverage"), ("target", "Target coverage")):
            population = relationship_payload.get(f"{side}_population")
            matched = relationship_payload.get(f"matched_{side}_count")
            coverage = relationship_payload.get(f"{side}_coverage")
            if matched is not None and population is not None:
                row[label] = f"{_text(matched)}/{_text(population)}"
            elif coverage is not None:
                row[label] = coverage
        if relationship_payload.get("cardinality") not in (None, ""):
            row["Cardinality"] = relationship_payload.get("cardinality")
        rows.append(row)
    if rows:
        widget.update({"type": "table", "rows": rows, "manager_rows": rows, "audit_payload": audit_payloads})
        return widget
    table_rows = []
    for record in ordered:
        relationship_payload = _record_payload(record)
        row = {key: value for key, value in relationship_payload.items() if key in {"source_id", "target_id", "relationship_id", "cardinality", "matched_pairs", "source_population", "target_population", "limitations"}}
        table_rows.append(row or {"record_id": record.get("record_id")})
    widget.update({"type": "table", "rows": table_rows, "manager_rows": table_rows, "audit_payload": audit_payloads})
    return widget


def _claim_widget(item_id: str, content: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    payload = _record_payload(record)
    title = _text(payload.get("claim_id") or record.get("record_id"))
    widget = _table_widget(item_id, content, record, title=title, rows=[{
        "claim": payload.get("claim"),
        "status": payload.get("status"),
        "period": payload.get("period"),
    }])
    widget["presentation_role"] = "finding_record"
    widget["presentation_tier"] = "audit"
    return widget


def _fact_widget(item_id: str, content: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = _record_payload(record)
    candidate = payload.get("widget")
    if not isinstance(candidate, Mapping):
        # Result Integration's public dashboard_fact API stores the reviewed
        # presentation fields directly in the payload.  Treat that payload as
        # the candidate rather than dropping the fact when no nested widget
        # envelope was supplied.
        candidate = payload
    if not isinstance(candidate, Mapping) or not any(
        key in candidate for key in ("title", "label", "type", "rows", "series", "value", "display_value", "visual_id")
    ):
        return None
    source_widget_id = _text(candidate.get("id"))
    title = _text(candidate.get("title") or candidate.get("label") or source_widget_id or _text(record.get("record_id")))
    base = _widget_base(item_id, content, record, title=title)
    authoritative_id = _text(base["id"])
    # A dashboard fact is a presentation hint only.  Merge the small
    # presentation vocabulary and retain every item/provenance/evidence/hash,
    # status, and limitation field owned by the assembler.
    presentation_fields = {
        "type", "title", "value", "display_value", "bars", "tiles", "categories", "segments", "rows",
        "points", "data",
        "presentation_geometry_only", "geometry_basis",
        "denominator_value", "denominator_label", "chart_notes", "notes", "layout", "span", "overview",
        "small_multiple_group", "small_multiple_label", "series", "sample_policy", "sample_rows", "visual_id",
        "columns", "scale_groups",
        # Preserve any explicit origin classification for inventory/audit.
        # These fields do not veto a Product plan selection.
        "technical_surface", "technical_surface_reason", "presentation_audience",
        "presentation_tier", "presentation_role", "kind", "manager_admission",
        "no_geometry_fallback_duplicate", "presentation_deduplication",
    }
    base.update({key: candidate[key] for key in sorted(presentation_fields) if key in candidate})
    base.setdefault("presentation_role", "decision_view")
    base.setdefault("presentation_tier", "primary")
    base["audit_payload"] = dict(candidate)
    raw_type = _text(candidate.get("type")).lower()
    if raw_type in {"kpi", "kpi_grid", "metric_grid"}:
        base["type"] = "kpi_grid"
        if "tiles" not in base:
            tiles = candidate.get("tiles") or candidate.get("rows") or candidate.get("series")
            if isinstance(tiles, list):
                base["tiles"] = [dict(item) if isinstance(item, Mapping) else {"label": item, "value": item} for item in tiles]
            elif candidate.get("value") is not None:
                base["tiles"] = [{"label": title, "value": candidate.get("value")}]
    else:
        # Local facts are intentionally conservative: semantic fields are
        # projected into a compact decision view; the exact candidate remains
        # in audit_payload for the collapsed technical audit.
        manager_type, manager_values = _manager_fact_projection(candidate)
        base["type"] = manager_type
        if manager_type in {"kpi_grid", "metric_grid"}:
            base["tiles"] = manager_values
            base.pop("rows", None)
        else:
            manager_rows = _decorate_manager_fact_rows(candidate, manager_values)
            # Keep the manager projection in the renderer's explicit chart
            # channel.  The original candidate remains byte-exact in
            # ``audit_payload``; unbound raw ``bars``/``categories`` fields
            # must not win over the projection when a manager card renders.
            # Inherited status/table facts are not newly admitted chart
            # contracts. Preserve their exact row shape across cumulative
            # rebuilds; only non-table visual facts receive the compact
            # label/value aliases needed by chart renderers.
            if manager_type in {"table", "status_table"}:
                manager_rows = [dict(row) for row in manager_values if isinstance(row, Mapping)]
            if manager_type in {"bar", "column", "lollipop", "progress", "leaderboard", "diverging_bar", "stacked_composition"}:
                base["bars"] = manager_rows
                base.pop("categories", None)
                base.pop("segments", None)
                base.pop("points", None)
                # ``rows``/``data``/``series`` from the raw accepted payload
                # are audit-only once the explicit chart projection exists.
                base.pop("rows", None)
                base.pop("data", None)
                base.pop("series", None)
            elif manager_type == "scatter":
                # ``_structured_fact_projection`` already binds exact x/y
                # values and a human group label; never re-read a first scalar
                # from the raw row here.
                base["points"] = [dict(row) for row in manager_rows]
                base.pop("bars", None)
                base.pop("categories", None)
                base.pop("rows", None)
                base.pop("data", None)
                base.pop("series", None)
            else:
                base["rows"] = manager_rows
                base.pop("bars", None)
                base.pop("categories", None)
                base.pop("segments", None)
                base.pop("points", None)
                base.pop("data", None)
                # Legacy G3 dashboard facts (status/exception/coverage
                # tables) carry an accepted ``series`` snapshot even when
                # their manager rows are audit-only.  Preserve that raw
                # envelope exactly for inherited widgets; only the new
                # structured chart contract consumes/rewrites series above.
                if raw_type in {"bar", "column", "scatter", "table"}:
                    base.pop("series", None)
            base["manager_rows"] = manager_rows
    base["title"] = title
    base["dashboard_fact"] = True
    # The record-bound id is authoritative and globally scoped by item.  Keep
    # a caller-provided fact id as descriptive metadata only.
    base["id"] = authoritative_id
    if source_widget_id and source_widget_id != authoritative_id:
        base["source_widget_id"] = source_widget_id
    return base


def _widget_identity(item_id: str, group_id: str, widget: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build exact raw identity material used only for collision disambiguation."""

    record_ids = [_safe_record_ref(value) for value in _as_list(widget.get("integration_record_ids")) if _text(value).strip()]
    if not record_ids and _text(widget.get("integration_record_id")).strip():
        record_ids = [_safe_record_ref(widget.get("integration_record_id"))]
    selected: list[Mapping[str, Any]] = []
    if record_ids:
        wanted = set(record_ids)
        selected = [record for record in records if _safe_record_ref(record.get("record_id")) in wanted]
    identity: dict[str, Any] = {
        "item_id": item_id,
        "group_id": group_id,
        "record_ids": record_ids,
        "records": selected,
        "source_metric_key": _text(widget.get("source_metric_key")),
        "source_widget_id": _text(widget.get("source_widget_id")),
    }
    if not selected:
        identity["widget"] = {key: value for key, value in widget.items() if key != "id"}
    return identity


def _collision_safe_widget_ids(
    item_id: str,
    group_id: str,
    widgets: list[dict[str, Any]],
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Append deterministic raw-identity digests only when slugs collide."""

    by_base: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for widget in widgets:
        base_id = _text(widget.get("id"))
        by_base.setdefault(base_id, []).append((widget, _widget_identity(item_id, group_id, widget, records)))
    for base_id, entries in by_base.items():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda pair: _canonical_bytes(pair[1]))
        for collision_index, (widget, identity) in enumerate(entries):
            identity = dict(identity)
            identity["collision_index"] = collision_index
            widget["id"] = f"{base_id}--{_json_hash(identity)}"
    return widgets


_LEGACY_CHART_TYPES = frozenset({
    "kpi", "bar", "column", "lollipop", "donut", "metric_grid", "kpi_grid",
    "waffle", "diverging_bar", "stacked_composition", "line", "heatmap",
    "scatter", "leaderboard", "progress", "area", "stacked_area", "grouped_bar",
    "stacked_bar", "normalized_stacked_bar", "funnel", "histogram", "box_plot",
    "pareto", "waterfall", "pie",
})
_LEGACY_CHART_FIELDS = frozenset({
    "type", "title", "value", "manager_display_value", "bars", "categories", "tiles",
    "segments", "points", "cells", "data", "presentation_geometry_only",
    "geometry_basis", "denominator_value", "denominator_label", "chart_notes",
    "small_multiple_group", "small_multiple_label", "scale_policy",
    "scale_groups",
    # These fields are still presentation-envelope material (not a source or
    # identity decision) and are required to reproduce an inherited aggregate
    # metric grid exactly, e.g. REQ12's closed/open signal strip.
    "aggregated_metric_ids", "integration_record_ids", "integration_record_refs",
    "integration_record_hashes", "audit_payload",
})


def _true_visual_ids(
    widgets_by_id: Mapping[str, Mapping[str, Any]],
    charts_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Return the current generation's exact visual universe.

    V2 used to hard-code the G3 75-ID partition.  The visual universe is a
    property of the candidate fixture/chart map instead: every supported
    chart type is eligible for exactly one manager/audit partition entry.
    Ordinary table/status-table projections remain record/widget audit only;
    a reviewed ``dashboard_fact`` whose accepted chart type is ``table`` is
    an explicit visual contract and is included as-is.  The one
    ``limited_empty_state`` status table is also explicit: it is the
    source-bound terminal outcome visual selected by an all-failed V2 plan,
    not an analytical table.  This keeps inherited G3 status tables out of
    the chart partition while preserving new fact types without lexical
    admission.
    """

    result: list[str] = []
    for widget_id, widget in widgets_by_id.items():
        chart = charts_by_id.get(widget_id)
        if not isinstance(chart, Mapping):
            continue
        if _dashboard_runtime().is_partition_visual(widget, chart):
            result.append(widget_id)
    return result


def _apply_legacy_chart_hint(widget: dict[str, Any], hint: Mapping[str, Any] | None) -> None:
    """Preserve a reviewed chart projection while rebuilding its envelope."""

    if not isinstance(hint, Mapping):
        return
    hint_type = _text(hint.get("type")).strip().lower()
    if hint_type not in _LEGACY_CHART_TYPES:
        return
    if _text(hint.get("presentation_role")).lower() == "relationship_matrix":
        return
    # Never copy identity/provenance or requirement metadata from an old
    # fixture.  Those bindings belong to the current accepted/integrated
    # record; only the already-reviewed visual shape and supplied values are
    # reusable.
    for key in _LEGACY_CHART_FIELDS:
        if key in hint:
            widget[key] = copy.deepcopy(hint[key])
        elif hint_type in {"metric_grid", "kpi_grid"} and key in {"value", "manager_display_value"}:
            # Preserve absence as well as value: an inherited aggregate grid
            # has tiles, not a duplicate scalar KPI field.
            widget.pop(key, None)
    inherited_role = _text(hint.get("presentation_role")).strip()
    if inherited_role:
        widget["presentation_role"] = inherited_role
    elif hint_type != "table":
        widget["presentation_role"] = "decision_view"
    if inherited_role:
        # Carry the inherited role through the neutral candidate pass without
        # persisting an internal marker into the visual snapshot.
        widget["_legacy_presentation_role"] = inherited_role


_OPTIONAL_INTEGRATION_PROJECTION_LIMITATION = (
    "Optional integration projection is unavailable for this accepted visual; "
    "the accepted business values remain source-bound."
)


def _quarantine_accepted_visual_integration_hints(widget: dict[str, Any]) -> None:
    """Scrub malformed optional integration hints before identity/projection.

    Accepted visual content is authoritative for presentation.  Its optional
    integration IDs/refs may enrich the audit, but malformed values must not
    reach strict identity, collision, or metric-consolidation paths.  Keep
    valid hints in deterministic order and retain one source-bound audit
    limitation for every rejected value.  Committed record bindings are not
    passed through this helper and remain strict elsewhere.
    """

    if not widget.get("accepted_visual"):
        return
    def sanitize_container(container: dict[str, Any]) -> bool:
        rejected = False
        for plural_key, singular_key in (
            ("integration_record_ids", "integration_record_id"),
            ("integration_record_refs", "integration_record_ref"),
        ):
            plural_present = plural_key in container
            singular_present = singular_key in container
            if not plural_present and not singular_present:
                continue
            values: list[Any] = []
            if plural_present:
                values.extend(_as_list(container.get(plural_key)))
            if singular_present:
                values.append(container.get(singular_key))
            valid: list[str] = []
            for value in values:
                if not _text(value).strip():
                    continue
                try:
                    normalized = _safe_record_ref(value)
                except AssemblyError:
                    rejected = True
                    continue
                if normalized not in valid:
                    valid.append(normalized)
            # Canonicalize to one plural list so every downstream path
            # observes exactly the same validated values and cannot fall back
            # to a rejected singular hint.
            container.pop(singular_key, None)
            if valid:
                container[plural_key] = valid
            else:
                container.pop(plural_key, None)
        return rejected

    rejected = sanitize_container(widget)
    # The raw visual declaration is retained for audit context, but optional
    # integration hints are not business evidence.  Apply the same scrub to
    # that nested snapshot so a rejected absolute/path-like value cannot leak
    # through later audit serialization or HTML rendering.
    audit_payload = widget.get("audit_payload")
    if isinstance(audit_payload, Mapping):
        audit_payload_copy = copy.deepcopy(dict(audit_payload))
        rejected = sanitize_container(audit_payload_copy) or rejected
        widget["audit_payload"] = audit_payload_copy
    if rejected:
        limitations = [
            _text(value).strip()
            for value in _as_list(widget.get("audit_limitations"))
            if _text(value).strip()
        ]
        if _OPTIONAL_INTEGRATION_PROJECTION_LIMITATION not in limitations:
            limitations.append(_OPTIONAL_INTEGRATION_PROJECTION_LIMITATION)
        widget["audit_limitations"] = limitations


def _build_widgets(
    item_id: str,
    content: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    accepted_visuals: Sequence[Mapping[str, Any]] = (),
    legacy_hints: Mapping[str, Mapping[str, Any]] | None = None,
    manager_widget_ids: Sequence[str] | None = None,
    manager_entries: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    widgets: list[dict[str, Any]] = [copy.deepcopy(dict(widget)) for widget in accepted_visuals if isinstance(widget, Mapping)]
    relationship_records: list[Mapping[str, Any]] = []
    for record in sorted(records, key=lambda value: (_text(value.get("kind")), _text(value.get("record_id")))):
        kind = _text(record.get("kind"))
        if kind == "metric":
            widgets.extend(_metric_widgets(item_id, content, record))
        elif kind == "analytical_artifact":
            widgets.extend(_analytical_artifact_widgets(item_id, content, record))
        elif kind == "relationship":
            relationship_records.append(record)
        elif kind == "dashboard_fact":
            fact = _fact_widget(item_id, content, record)
            if fact is not None:
                widgets.append(fact)
        elif kind == "claim":
            # Accepted business conclusions are reviewed outputs too.  Keep
            # the exact claim text in a local table with the same evidence and
            # provenance envelope as every other widget; no value is derived.
            widget = _claim_widget(item_id, content, record)
            widgets.append(widget)
    if relationship_records:
        widgets.append(_relationship_widget(item_id, content, relationship_records))
    if not widgets:
        # A typed limitation-only result remains visible without manufacturing
        # a zero-valued KPI.
        for record in records:
            if _text(record.get("kind")) == "limitation":
                widgets.append(_claim_widget(item_id, content, record))
    for widget in widgets:
        hint = legacy_hints.get(_text(widget.get("id"))) if legacy_hints else None
        _apply_legacy_chart_hint(widget, hint)
        # Legacy chart hints may carry optional integration metadata.  Run the
        # accepted-only quarantine after that merge as well as on the original
        # visual so malformed values cannot reach collision/metric identity.
        _quarantine_accepted_visual_integration_hints(widget)

    # Bind every constructor-produced widget to the requirement before the
    # semantic echo pass.  The outer fixture assembly repeats these immutable
    # fields for its final envelope, but this early source-bound binding keeps
    # dedupe reachable for normal accepted-visual/fact construction without
    # inferring any missing metric, scope, grain, entity, or dimension.
    bound_scope = _text(
        content.get("__manager_requirement_scope")
        or content.get("requirement_scope")
        or content.get("scope")
        or content.get("method")
    ).strip()
    for widget in widgets:
        widget["requirement_id"] = item_id
        if bound_scope:
            widget["requirement_scope"] = bound_scope

    # Attach neutral candidate metadata before applying any explicit Product
    # plan.  This pass does not classify titles, fields, values, or chart
    # kinds; every original candidate remains available for plan selection.
    _apply_manager_admission(
        widgets,
        subject_context=" ".join(
            _text(content.get(key))
            for key in ("__manager_requirement_scope", "scope", "method")
            if _text(content.get(key)).strip()
        ),
    )

    for widget in widgets:
        widget.setdefault("presentation_tier", "primary")
        widget.setdefault("presentation_role", "decision_view")
    # Public assembly supplies an explicit plan set (possibly empty).  The
    # Product Agent's persisted membership is the only semantic manager
    # admission decision.
    if manager_widget_ids is not None:
        _apply_explicit_manager_admission(widgets, manager_widget_ids, manager_entries)
    for widget in widgets:
        widget.pop("_legacy_presentation_role", None)
    # Every accepted/integrated record is a reviewed output.  The renderer
    # supports arbitrary ordered widget lists; no records are truncated.
    return widgets


def _apply_overview_selection(
    widgets: list[dict[str, Any]],
    manager_widget_ids: Sequence[str] | None = None,
) -> list[str]:
    """Project Product-selected manager IDs into the overview in plan order.

    Overview membership is declarative: this helper only follows the ordered
    manager IDs supplied by the Product plan (or existing admission metadata
    for direct fixture calls).  It never examines widget kind, content,
    titles, semantic keys, duplicates, or a fixed count.
    """

    by_id = {_text(widget.get("id")).strip(): widget for widget in widgets if _text(widget.get("id")).strip()}
    if manager_widget_ids is None:
        selected = [
            widget
            for widget in widgets
            if isinstance(widget.get("manager_admission"), Mapping)
            and _text(widget.get("manager_admission", {}).get("status")) == "admitted"
            and _text(widget.get("presentation_audience")) == "business_manager"
        ]
        selected_ids = [_text(widget.get("id")).strip() for widget in selected]
    else:
        selected_ids = [_text(value).strip() for value in manager_widget_ids]
        if len(selected_ids) != len(set(selected_ids)) or any(not value for value in selected_ids):
            raise AssemblyError("manager widget IDs must be unique and non-empty")
        unknown = [value for value in selected_ids if value not in by_id]
        if unknown:
            raise AssemblyError(f"manager widget IDs reference unknown widgets: {unknown[:5]}")
        selected = [by_id[value] for value in selected_ids]

    for widget in widgets:
        widget.pop("overview", None)
    for widget in selected:
        widget["overview"] = True
    return selected_ids


def _ontology_projection(all_records: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    relationships: list[dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str]] = set()
    group_members: dict[str, list[str]] = {}
    group_meta: dict[str, dict[str, Any]] = {}
    for record in all_records:
        payload = _record_payload(record)
        kind = _text(record.get("kind"))
        if kind == "ontology_item" and (payload.get("ontology_projection") is True or (payload.get("ontology_id") and payload.get("kind"))):
            node_id = _text(payload.get("ontology_id") or payload.get("id"))
            label = _text(payload.get("label") or node_id)
            node_kind = _text(payload.get("kind") or payload.get("object_type"))
            if not node_id or not label or not node_kind:
                raise AssemblyError("ontology nodes require stable id, label, and kind")
            if node_id in node_ids:
                raise AssemblyError(f"duplicate ontology node: {node_id}")
            node_ids.add(node_id)
            node = {"id": node_id, "label": label, "kind": node_kind}
            group_id = _text(payload.get("group_id") or payload.get("group"))
            if group_id:
                node["group_id"] = group_id
                group_members.setdefault(group_id, []).append(node_id)
                meta = group_meta.setdefault(group_id, {"id": group_id, "label": group_id})
                if payload.get("group_label"):
                    meta["label"] = _text(payload.get("group_label"))
                if payload.get("group_order") is not None:
                    meta["order"] = payload.get("group_order")
            nodes.append(node)
        elif kind == "relationship" and payload.get("ontology_projection") is True:
            source = _text(payload.get("source_id") or payload.get("source"))
            target = _text(payload.get("target_id") or payload.get("target"))
            label = _text(payload.get("label") or payload.get("relationship"))
            if not source or not target or not label:
                continue
            edge = (source, target, label)
            if edge in edge_keys:
                raise AssemblyError(f"duplicate ontology relationship: {source}->{target}/{label}")
            edge_keys.add(edge)
            relationships.append({"source": source, "target": target, "label": label})
    if relationships and any(edge["source"] not in node_ids or edge["target"] not in node_ids for edge in relationships):
        raise AssemblyError("ontology relationship references an unknown endpoint")
    groups: list[dict[str, Any]] = []
    if group_members:
        covered: set[str] = set()
        for group_id, node_list in sorted(group_members.items(), key=lambda pair: (group_meta[pair[0]].get("order", 10**9), pair[0])):
            if any(node_id in covered for node_id in node_list):
                raise AssemblyError("ontology groups contain duplicate nodes")
            covered.update(node_list)
            meta = dict(group_meta[group_id])
            meta["node_ids"] = node_list
            groups.append(meta)
        if covered != node_ids:
            raise AssemblyError("ontology groups must cover every projected node")
    nodes.sort(key=lambda value: value["id"])
    relationships.sort(key=lambda value: (value["source"], value["target"], value["label"]))
    return nodes, relationships, groups


def _load_projection_metadata(context: RunContext, item_ids: Sequence[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind the presentation build to the public committed LEM projection.

    The product assembler does not trust a stale product snapshot as its
    semantic authority.  It rebuilds the read-only projection from the
    lifecycle's accepted/committed records, then records hashes for the
    projection, prepared registry, telemetry metadata, and (when present) the
    existing terminal product manifest.
    """

    try:
        from auto_foundry_core.lem_projection import LivingEnterpriseModelProjector
        from auto_foundry_core.prepared import PreparedAssetRegistry
    except ModuleNotFoundError as exc:  # pragma: no cover - package install guard
        raise AssemblyError("public LEM/prepared registry types are unavailable") from exc
    # The LEM projector is authoritative for accepted/committed records, but
    # terminal technical-failure items deliberately have no accepted bundle
    # or committed records.  Passing those IDs through would make a bounded
    # limited dashboard impossible even though their validated terminal
    # manifests are a legitimate source-bound lifecycle input.  The assembler
    # has already validated those manifests in its item loop; project only the
    # accepted/integrated subset and retain the lifecycle order in the
    # projector's result.  This is selection, not synthetic analytics.
    projection_item_ids: list[str] = []
    for item_id in item_ids:
        state = _read_json(context, f"requirements/{item_id}/item_state.json", label=f"{item_id} item state")
        if state.get("lifecycle_state") == "technical_failure" or state.get("integration_state") == "technical_failure":
            continue
        projection_item_ids.append(item_id)
    try:
        projection = LivingEnterpriseModelProjector.project(context, item_ids=projection_item_ids)
    except Exception as exc:
        raise AssemblyError("accepted/committed LEM projection validation failed") from exc
    exported = projection.model.export()
    count_map = {
        "ontology_items": len(exported.get("ontology", [])) if isinstance(exported.get("ontology"), list) else None,
        "relationships": len(exported.get("relationships", {})) if isinstance(exported.get("relationships"), Mapping) else None,
        "canonical_mappings": len(exported.get("canonical_mappings", [])) if isinstance(exported.get("canonical_mappings"), list) else None,
        "identity_decisions": len(exported.get("identity_decisions", [])) if isinstance(exported.get("identity_decisions"), list) else None,
        "prepared_assets": len(exported.get("prepared_assets", [])) if isinstance(exported.get("prepared_assets"), list) else None,
        "knowledge": len(exported.get("knowledge", {})) if isinstance(exported.get("knowledge"), Mapping) else None,
        "resolution_bindings": len(projection.resolution_bindings),
        "item_bindings": len(projection.bindings),
    }
    summary = {key: value for key, value in count_map.items() if value is not None}

    registry = PreparedAssetRegistry(context)
    descriptors = registry.search(include_superseded=True)
    registry_path = registry.registry_path
    registry_present = registry_path.is_file() and not registry_path.is_symlink()
    if registry_present:
        registry_bytes = registry_path.read_bytes()
        registry_hash = _sha256_bytes(registry_bytes)
    else:
        # The public registry reports an explicit empty set when no prepared
        # assets were committed.  Bind that empty semantic state without
        # manufacturing a run-local file or reading candidate work payloads.
        registry_hash = _json_hash([descriptor.to_dict() for descriptor in descriptors])
    index_path = registry.index_path
    index_present = index_path.is_file() and not index_path.is_symlink()
    index_hash = _sha256_bytes(index_path.read_bytes()) if index_present else _json_hash({"entries": [descriptor.to_dict() for descriptor in descriptors]})

    telemetry_hashes: dict[str, str] = {}
    for reference in ("telemetry/events.jsonl", "telemetry/inventory_counters.json"):
        path = context.resolve_run_path(reference)
        if path.is_file() and not path.is_symlink():
            telemetry_hashes[reference] = _sha256_bytes(path.read_bytes())
    telemetry_hash = _json_hash(telemetry_hashes)

    product_manifest_ref = "products/product_manifest.json"
    product_manifest_hash: str | None = None
    product_manifest_markers: Mapping[str, Any] = {}
    product_manifest: Mapping[str, Any] | None = None
    product_manifest_path = context.resolve_run_path(product_manifest_ref)
    if product_manifest_path.is_file() and not product_manifest_path.is_symlink():
        product_manifest = _read_json(context, product_manifest_ref, label="frozen product manifest")
        if product_manifest.get("run_id") not in {None, context.run_id}:
            raise AssemblyError("frozen product manifest run identity mismatch")
        product_manifest_hash = _sha256_bytes(product_manifest_path.read_bytes())
        raw_markers = product_manifest.get("freeze_markers")
        if isinstance(raw_markers, Mapping):
            for key, value in raw_markers.items():
                if type(value) is not bool:
                    raise AssemblyError(f"frozen product marker is not boolean: {key}")
            product_manifest_markers = dict(raw_markers)
        raw_assets = product_manifest.get("assets")
        if isinstance(raw_assets, list):
            for asset in raw_assets:
                if not isinstance(asset, Mapping):
                    raise AssemblyError("frozen product assets must be objects")
                reference = _text(asset.get("ref")).strip()
                expected_hash = asset.get("sha256")
                if not reference or not _is_sha256(expected_hash):
                    raise AssemblyError("frozen product asset reference/hash is invalid")
                # Validate before any open.  Absolute/external archives and
                # source/work/calculation refs are outside the presentation
                # boundary and fail closed rather than being silently skipped.
                safe_reference = _validate_product_asset_reference(reference)
                _path, data = _read_bytes(context, safe_reference, label="frozen product asset")
                if _sha256_bytes(data) != expected_hash:
                    raise AssemblyError(f"frozen product asset hash mismatch: {safe_reference}")
        lem_meta = product_manifest.get("lem")
        if isinstance(lem_meta, Mapping) and lem_meta.get("projection_hash") not in {None, projection.projection_hash}:
            raise AssemblyError("frozen product LEM projection hash mismatch")
        if product_manifest_markers.get("telemetry_frozen") is True and not telemetry_hashes:
            raise AssemblyError("frozen product claims telemetry is frozen but telemetry metadata is absent")
        if product_manifest_markers.get("prepared_data_registry_frozen") is True and not registry_present:
            prepared_count = lem_meta.get("prepared_asset_count") if isinstance(lem_meta, Mapping) else None
            if prepared_count not in {None, 0}:
                raise AssemblyError("frozen product claims prepared registry is frozen but registry is absent")

    freeze_markers = {
        "answers_frozen": True,
        "living_enterprise_model_frozen": True,
        "prepared_data_registry_frozen": bool(registry_present or not descriptors),
        "dashboard_frozen": True,
        "telemetry_frozen": bool(telemetry_hashes),
    }
    projection_metadata = {
        "projection_hash": projection.projection_hash,
        "export_sha256": _json_hash(exported),
        "item_order": list(projection.item_order),
        "bindings": [binding.to_dict() for binding in projection.bindings],
        "summary": summary,
        "prepared_registry": {
            "ref": "lem/prepared_data_registry.jsonl",
            "present": registry_present,
            "descriptor_count": len(descriptors),
            "sha256": registry_hash,
        },
        "prepared_index": {
            "ref": "indexes/prepared_index.json",
            "present": index_present,
            "sha256": index_hash,
        },
        "telemetry": {"sha256": telemetry_hash, "assets": telemetry_hashes},
        "product_manifest_ref": product_manifest_ref if product_manifest is not None else None,
        "product_manifest_sha256": product_manifest_hash,
        "freeze_markers": freeze_markers,
    }
    return summary, projection_metadata


def _build_registry(source: Path, destination: Path) -> dict[str, Any]:
    try:
        registry = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssemblyError("committed dashboard chart registry asset is invalid") from exc
    if not isinstance(registry, Mapping) or registry.get("schema_version") != REGISTRY_SCHEMA or not isinstance(registry.get("families"), list):
        raise AssemblyError("dashboard chart registry schema is invalid")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    return {"schema_version": registry["schema_version"], "sha256": _sha256_bytes(destination.read_bytes())}


def _chart_map_entry(widget: Mapping[str, Any], requirement_id: str, item: Mapping[str, Any]) -> dict[str, Any]:
    kind = _text(widget.get("type") or "table")
    family = {
        "kpi": "kpi_card", "bar": "horizontal_bar", "column": "column", "lollipop": "lollipop",
        "diverging_bar": "diverging_bar", "waffle": "waffle", "donut": "donut_pie", "stacked_composition": "stacked_bar", "progress": "horizontal_bar",
        "metric_grid": "metric_grid", "kpi_grid": "metric_grid", "table": "table", "status_table": "table", "line": "line_area_slope", "scatter": "scatter_bubble",
        "area": "line_area_slope", "stacked_area": "line_area_slope", "grouped_bar": "grouped_bar",
        "stacked_bar": "stacked_bar", "normalized_stacked_bar": "stacked_bar", "funnel": "funnel",
        "histogram": "histogram_box", "box_plot": "histogram_box", "pareto": "pareto",
        "waterfall": "waterfall", "pie": "donut_pie",
    }.get(kind, "table")
    fields: dict[str, Any] = {key: value for key, value in widget.items() if key in {"value", "manager_display_value", "bars", "tiles", "categories", "segments", "points", "series", "rows", "manager_rows", "stages", "bins", "boxes", "steps", "data", "values", "population", "denominator", "unit", "units", "period", "grain", "data_grain", "row_grain", "proxy", "proxy_or_limit", "limit", "descriptive", "descriptive_only", "causal_status", "as_of", "date_authority", "assumptions", "annotation", "dimensions", "measures", "time", "coverage", "coverage_note", "scope", "answer_scope", "scope_note", "limitations", "filters", "drilldown", "empty_state", "presentation_geometry_only", "geometry_basis", "denominator_value", "denominator_label", "grain", "integration_record_id", "integration_record_ids", "integration_record_ref", "integration_record_refs", "presentation_role", "presentation_tier", "technical_surface", "technical_surface_reason", "artifact_type", "artifact_ref", "analytical_artifact_id", "analytical_artifact_ref", "analytical_artifact_content_hash", "analytical_artifact_envelope_hash", "analytical_artifact_canonical_bytes_sha256", "artifact_provenance", "accepted_visual", "accepted_visual_index", "accepted_visual_pointer", "accepted_visual_type", "accepted_evidence", "accepted_evidence_id", "accepted_evidence_candidate_kind", "accepted_evidence_pointer", "accepted_evidence_source_pointer", "accepted_evidence_table_pointer", "accepted_evidence_ref", "accepted_evidence_sha256", "accepted_evidence_conclusion", "accepted_content_hash", "accepted_manifest_hash", "accepted_source_ref", "accepted_artifact_ref", "accepted_artifact_sha256", "source_bound", "scale_groups", "x_label", "y_label", "columns"}}
    return {
        "id": _text(widget.get("id")),
        "type": kind,
        "family": family,
        "requirement_id": requirement_id,
        "fields_or_values_used": fields,
        "provenance": {
            "item_id": _text(item.get("item_id")),
            "accepted_manifest_hash": _text(item.get("accepted_manifest_hash")),
            "accepted_content_hash": _text(item.get("accepted_content_hash")),
            "integration_manifest_hash": _text(item.get("integration_manifest_hash")),
            "integration_record_hash": _text(widget.get("integration_record_hash")),
            "integration_record_id": _text(widget.get("integration_record_id")),
            "integration_record_ids": list(widget.get("integration_record_ids", [])),
            "integration_record_ref": _text(widget.get("integration_record_ref")),
            "accepted_visual": bool(widget.get("accepted_visual")),
            "accepted_visual_index": widget.get("accepted_visual_index"),
            "accepted_visual_pointer": _text(widget.get("accepted_visual_pointer")),
            "accepted_evidence": bool(widget.get("accepted_evidence")),
            "accepted_evidence_id": _text(widget.get("accepted_evidence_id")),
            "accepted_evidence_candidate_kind": _text(widget.get("accepted_evidence_candidate_kind")),
            "accepted_evidence_pointer": _text(widget.get("accepted_evidence_pointer")),
            "accepted_evidence_source_pointer": _text(widget.get("accepted_evidence_source_pointer")),
            "accepted_evidence_table_pointer": _text(widget.get("accepted_evidence_table_pointer")),
            "accepted_evidence_ref": _text(widget.get("accepted_evidence_ref")),
            "accepted_evidence_sha256": _text(widget.get("accepted_evidence_sha256")),
            "accepted_content_hash": _text(widget.get("accepted_content_hash")),
            "accepted_manifest_hash": _text(widget.get("accepted_manifest_hash")),
            "accepted_source_ref": _text(widget.get("accepted_source_ref")),
            "accepted_artifact_ref": _text(widget.get("accepted_artifact_ref")),
            "accepted_artifact_sha256": _text(widget.get("accepted_artifact_sha256")),
            "evidence_refs": list(widget.get("evidence_refs", [])),
            "trace_refs": list(widget.get("trace_refs", [])),
            "artifact_provenance": copy.deepcopy(widget.get("artifact_provenance", {})),
        },
    }


def _limited_availability_widget(
    context: RunContext,
    failed_items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build one explicit non-analytical empty state for all-terminal runs.

    The card is a status table, not a fabricated KPI or success signal.  Its
    visible row states only that no accepted business visual is available;
    terminal manifest/reason hashes remain in the exact audit payload and
    trace references for technical review.
    """

    refs = sorted({
        _text(item.get("manifest_ref")).strip()
        for item in failed_items
        if isinstance(item, Mapping) and _text(item.get("manifest_ref")).strip()
    })
    if not refs:
        raise AssemblyError("all-terminal empty-state requires validated terminal manifest references")
    widget_id = "business-availability-empty-state"
    return {
        "id": widget_id,
        "type": "table",
        "title": "Business visual availability",
        "label": "Business visual availability",
        "requirement_id": "business-availability",
        "requirement_title": "Business visual availability",
        "requirement_order": 1,
        "domain_id": "business-availability",
        "manager_anchor": "business-availability-empty-state",
        "presentation_role": "decision_view",
        "presentation_tier": "primary",
        "presentation_audience": "business_manager",
        "manager_admission": {
            "status": "admitted",
            "presentation_audience": "business_manager",
            "policy": "terminal_limited_dashboard",
            "role": "business_outcome",
            "plan_membership": True,
        },
        "limited_empty_state": True,
        "empty_state": "No accepted business visual is available for this run.",
        # Keep this an explicit lifecycle status row.  A count of failed
        # requirements would be a derived metric, and this limited widget is
        # intentionally not an analytical result.  Per-item terminal reasons
        # remain available in ``audit_payload``/``evidence_refs``.
        "rows": [{
            "status": "No accepted business visual is available for this run.",
        }],
        "manager_rows": [{
            "status": "No accepted business visual is available for this run.",
        }],
        "audit_payload": copy.deepcopy(list(failed_items)),
        "reviewed_item_ref": refs[0],
        "reviewed_output_ref": refs[0],
        "evidence_refs": refs,
        "trace_refs": refs,
        "integration_record_ids": [],
        "integration_record_refs": [],
        "limitations": [
            "No accepted business visual is available because every selected requirement reached a terminal failure or limitation state.",
            "No KPI, metric, row, or analytical success has been fabricated.",
        ],
    }


def _product_ref(context: RunContext, value: str | Path) -> tuple[Path, str]:
    raw = Path(value)
    # ``RunContext.resolve_product_path`` already anchors relative paths under
    # ``run_root/products``.  Accepting a leading ``products/`` convenience
    # prefix without stripping it would create ``products/products/...``;
    # normalize that prefix while retaining all containment checks.
    if not raw.is_absolute() and raw.parts and raw.parts[0] == "products":
        raw = Path(*raw.parts[1:]) if len(raw.parts) > 1 else Path(".")
    path = context.resolve_product_path(raw)
    relative = path.relative_to(context.run_root).as_posix()
    return path, relative


def _revision_output_root(
    context: RunContext,
    revision_id: str | None,
    output_root_ref: str | Path | None,
) -> tuple[Path, str] | None:
    """Resolve the store-authoritative Product revision output namespace."""

    if revision_id is None and output_root_ref is None:
        return None
    if revision_id is None or output_root_ref is None:
        raise AssemblyError("product revision output binding is incomplete")
    if not re.fullmatch(r"rev-[0-9]{4,}", str(revision_id).strip()):
        raise AssemblyError("product revision_id is invalid")
    # Resolve the store from the canonical run-relative namespace itself.  A
    # freshly seeded/replayed context may not yet expose ``active_generation``
    # even though the durable Product revision does; relying on that optional
    # projection would make an otherwise valid bound action fail before the
    # store can validate its lineage.  The exact shape is checked before any
    # filesystem resolution so caller-controlled paths cannot select another
    # generation or revision.
    output_ref = Path(str(output_root_ref))
    if output_ref.is_absolute() or len(output_ref.parts) != 6:
        raise AssemblyError("product revision output root binding is invalid")
    if output_ref.parts[0] != "products" or output_ref.parts[1] != "generations":
        raise AssemblyError("product revision output root binding is invalid")
    generation_id = output_ref.parts[2]
    if not re.fullmatch(r"G-[0-9]{4,}", generation_id):
        raise AssemblyError("product revision output root binding is invalid")
    if output_ref.parts[3] != "product_revisions" or output_ref.parts[4] != str(revision_id).strip() or output_ref.parts[5] != "artifacts":
        raise AssemblyError("product revision output root binding is invalid")
    active_generation = _active_generation_id(context)
    if active_generation and active_generation != generation_id:
        raise AssemblyError("product revision output generation does not match active generation")
    try:
        from auto_foundry_core.product_review import ProductReviewStore

        store = ProductReviewStore(context, generation_id)
        revision = store.load_revision(str(revision_id).strip())
        expected_ref = store.revision_artifacts_ref(revision.revision_id)
        root = store.revision_artifacts_root(revision.revision_id)
    except Exception as exc:
        raise AssemblyError("product revision output namespace is unavailable") from exc
    if Path(str(output_root_ref)).as_posix() != Path(expected_ref).as_posix():
        raise AssemblyError("product revision output root binding is invalid")
    if revision.status not in {"pending", "candidate", "reviewed"}:
        raise AssemblyError("product revision is not accepting product output")
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise AssemblyError("product revision output namespace is invalid")
    return root, expected_ref


def _is_canonical_preview_namespace(
    context: RunContext,
    output_root: Path,
    output_run_ref: str,
    generation_id: str,
    output_refs: Mapping[str, str],
) -> bool:
    """Return whether *output_root* is the lifecycle-owned preview namespace.

    Preview refresh is deliberately an implicit capability of the canonical
    ``generations/<G>/preview`` output only.  Reusing the ordinary assembler
    API for any other namespace keeps its existing fail-closed drift checks.
    All child output references must also be the canonical names so a caller
    cannot opt an arbitrary product tree into refresh semantics.
    """

    expected_prefix = f"products/generations/{generation_id}/preview"
    try:
        expected_root = context.resolve_product_path(Path("generations") / generation_id / "preview")
    except (AllowedRootError, OSError, ValueError):
        return False
    expected_refs = {
        "fixture_ref": f"{expected_prefix}/dashboard_fixture_v4.json",
        "chart_map_ref": f"{expected_prefix}/dashboard_chart_map_v4.json",
        "chart_registry_ref": f"{expected_prefix}/dashboard_chart_registry_v4.json",
        "blueprint_ref": f"{expected_prefix}/{BLUEPRINT_FILENAME}",
        "site_ref": f"{expected_prefix}/site",
        "receipt_ref": f"{expected_prefix}/build_receipt.json",
    }
    return output_root == expected_root and output_run_ref == expected_prefix and all(
        output_refs.get(key) == value for key, value in expected_refs.items()
    )


def _canonical_preview_request_has_symlink(context: RunContext, output_dir: str | Path, generation_id: str) -> bool:
    """Detect aliases in the lifecycle-owned preview path before resolution.

    ``RunContext.resolve_product_path`` intentionally resolves symlinks before
    checking containment.  That is the right boundary for ordinary product
    paths, but it would make a symlink at ``generations/<G>/preview`` appear as
    its target and could let a refresh replace the target behind a tampered
    canonical path.  Preview refresh therefore rejects symlink components in
    the exact lexical namespace requested by the lifecycle.
    """

    raw = Path(output_dir).expanduser()
    if raw.is_absolute():
        requested = raw
    else:
        if raw.parts and raw.parts[0] == "products":
            raw = Path(*raw.parts[1:]) if len(raw.parts) > 1 else Path(".")
        requested = context.run_root / "products" / raw
    canonical = context.run_root / "products" / "generations" / generation_id / "preview"
    if requested != canonical:
        return False
    current = context.run_root
    for component in canonical.relative_to(context.run_root).parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def _replace_prefix(root: Path, old: str, new: str) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise AssemblyError(f"cannot read staged presentation asset {path}") from exc
        if old.encode("utf-8") in data:
            path.write_bytes(data.replace(old.encode("utf-8"), new.encode("utf-8")))


def _publish_staged_output(staging_root: Path, output_root: Path, *, retain_backup: bool = False) -> None:
    """Replace one product namespace with a validated staged directory.

    The previous namespace is moved to a sibling backup before the staged
    directory is published.  If publication fails, the old directory is
    restored, leaving the prior product bytes and receipt intact.
    """

    backup_root = output_root.parent / f".{output_root.name}.previous"
    if backup_root.exists() or backup_root.is_symlink():
        raise AssemblyError(f"previous product backup already exists: {backup_root.name}")
    moved_previous = False
    published = False
    try:
        if output_root.exists() or output_root.is_symlink():
            os.replace(output_root, backup_root)
            moved_previous = True
        os.replace(staging_root, output_root)
        published = True
    except BaseException as original:
        # ``KeyboardInterrupt`` (and a hard-to-schedule signal between the
        # second rename and ``published = True``) is a real publication
        # boundary, not an ordinary ``Exception``.  Inspect both lexical
        # paths after the interruption so we can distinguish a failed second
        # rename from a completed one and leave exactly one authoritative
        # namespace.
        # A signal can arrive after ``os.replace(output, backup)`` returns but
        # before the following ``moved_previous = True`` assignment.  Derive
        # ownership from the durable paths as well as the in-memory flag.
        moved_previous = moved_previous or (
            (backup_root.exists() or backup_root.is_symlink())
            and not (output_root.exists() or output_root.is_symlink())
        )
        candidate_published = (
            (output_root.exists() or output_root.is_symlink())
            and not (staging_root.exists() or staging_root.is_symlink())
        )
        if candidate_published:
            published = True
        elif moved_previous and not (output_root.exists() or output_root.is_symlink()) and (
            backup_root.exists() or backup_root.is_symlink()
        ):
            restore_error: BaseException | None = None
            try:
                os.replace(backup_root, output_root)
            except BaseException as exc:
                restore_error = exc
            if restore_error is not None:
                # The interruption that caused publication to fail remains
                # the caller-visible error; the restore failure is attached as
                # its cause for diagnosis and the durable backup is retained
                # for a subsequent, explicit recovery attempt.
                raise original from restore_error
        raise
    finally:
        if published and moved_previous and not retain_backup and (backup_root.exists() or backup_root.is_symlink()):
            if backup_root.is_dir() and not backup_root.is_symlink():
                shutil.rmtree(backup_root, ignore_errors=True)
            else:
                backup_root.unlink(missing_ok=True)


@contextmanager
def _assembly_lock(lock_path: Path) -> Iterable[None]:
    """Serialize one output namespace across processes and threads.

    The full/root assembler predates the generation transaction lock used by
    the delta assembler.  Its deterministic staging name therefore doubles
    as a collision guard, but a process death can leave that name behind and
    make an otherwise safe retry fail before it can rebuild.  A stable
    advisory lock gives the staging namespace one owner at a time; once the
    lock is held, a leftover staging directory is known to be an orphan from
    an earlier attempt and can be cleaned before rebuilding.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise AssemblyError(f"assembly lock cannot be a symlink: {lock_path}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise AssemblyError(f"cannot open assembly lock: {lock_path}") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _remove_orphan_staging(staging_root: Path) -> None:
    """Remove one owned staging namespace after the assembly lock is held."""

    if staging_root.is_symlink():
        raise AssemblyError(f"staging namespace is symlinked: {staging_root.name}")
    if not staging_root.exists():
        return
    if not staging_root.is_dir():
        raise AssemblyError(f"staging namespace is not a directory: {staging_root.name}")
    # Never recurse through an alias while cleaning a crashed candidate.  A
    # malformed/tampered staging tree remains fail-closed instead of turning
    # cleanup into an arbitrary filesystem delete.
    for entry in staging_root.rglob("*"):
        if entry.is_symlink():
            raise AssemblyError(f"staging namespace contains a symlink: {entry.relative_to(staging_root)}")
        if not entry.is_file() and not entry.is_dir():
            raise AssemblyError(f"staging namespace contains an unsupported entry: {entry.relative_to(staging_root)}")
    shutil.rmtree(staging_root)


def _site_tree_binding(site_root: Path, *, exclude: set[str] | None = None) -> dict[str, Any]:
    """Return canonical per-file hashes and a complete deterministic tree hash."""

    if not site_root.is_dir() or site_root.is_symlink():
        raise AssemblyError(f"site output directory is missing or symlinked: {site_root}")
    excluded = exclude or set()
    files: dict[str, str] = {}
    for path in sorted(site_root.rglob("*")):
        relative = path.relative_to(site_root).as_posix()
        if relative in excluded:
            continue
        if path.is_symlink():
            raise AssemblyError(f"site output contains symlink: {relative}")
        if not path.is_file():
            continue
        try:
            files[relative] = _sha256_bytes(path.read_bytes())
        except OSError as exc:
            raise AssemblyError(f"site output file cannot be read: {relative}") from exc
    if not files:
        raise AssemblyError("site output contains no files")
    return {"files": files, "tree_sha256": _json_hash(files), "file_count": len(files)}


def _validate_site_manifest_binding(
    site_root: Path,
    *,
    expected_blueprint_ref: str | None = None,
    expected_blueprint_sha256: str | None = None,
    expected_chart_map_ref: str | None = None,
) -> dict[str, Any]:
    """Validate the renderer's self-excluding site-manifest binding.

    The renderer writes a manifest *inside* the site tree.  Its
    ``site_file_hashes``/``site_tree_sha256`` therefore intentionally exclude
    that manifest so the manifest can describe the tree without a
    self-reference.  The assembler receipt has a separate complete
    ``site_binding`` that includes ``site_manifest.json``.  Keep this
    distinction in one program-owned validator so callers never need to
    compare the two domains themselves.
    """

    if not site_root.is_dir() or site_root.is_symlink():
        raise AssemblyError("site output directory is missing or symlinked")
    manifest_path = site_root / "site_manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise AssemblyError("site manifest is missing or symlinked")
    try:
        raw = manifest_path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssemblyError("site manifest is invalid") from exc
    if not isinstance(value, Mapping) or raw != _canonical_bytes(value):
        raise AssemblyError("site manifest is not canonical")
    non_manifest = _site_tree_binding(site_root, exclude={"site_manifest.json"})
    if value.get("site_file_hashes") != non_manifest["files"]:
        raise AssemblyError("site manifest file binding does not match site")
    if value.get("site_tree_sha256") != non_manifest["tree_sha256"]:
        raise AssemblyError("site manifest tree binding does not match site")
    if value.get("site_tree_file_count") != non_manifest["file_count"]:
        raise AssemblyError("site manifest file count does not match site")
    if expected_blueprint_ref is not None and value.get("blueprint_ref") != expected_blueprint_ref:
        raise AssemblyError("site manifest blueprint reference is stale")
    if expected_blueprint_sha256 is not None and value.get("blueprint_sha256") != expected_blueprint_sha256:
        raise AssemblyError("site manifest blueprint hash is stale")
    if expected_chart_map_ref is not None and value.get("chart_map_ref") != expected_chart_map_ref:
        raise AssemblyError("site manifest chart map reference is stale")
    return {
        "ref": "site_manifest.json",
        "sha256": _sha256_bytes(raw),
        "files": dict(non_manifest["files"]),
        "tree_sha256": non_manifest["tree_sha256"],
        "file_count": non_manifest["file_count"],
    }


def _validate_site_links_in_tree(site_root: Path) -> None:
    """Run the canonical offline renderer link validator over one site tree."""

    renderer_path = Path(__file__).resolve().with_name("dashboard_renderer.py")
    spec = importlib.util.spec_from_file_location("dashboard_renderer_for_receipt_validation", renderer_path)
    if spec is None or spec.loader is None:
        raise AssemblyError("dashboard renderer cannot be loaded for receipt validation")
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    pages: dict[str, bytes] = {}
    for path in sorted(site_root.rglob("*")):
        if path.is_symlink():
            raise AssemblyError(f"assembled receipt site contains symlink: {path.relative_to(site_root)}")
        if path.is_file():
            pages[path.relative_to(site_root).as_posix()] = path.read_bytes()
    try:
        renderer._validate_site_links(pages)
    except Exception as exc:
        raise AssemblyError("assembled receipt site links are invalid") from exc


def _validate_assembled_receipt(context: RunContext, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one completed root assembler receipt and both site trees.

    This is intentionally an internal helper for the generation-aware public
    validator.  It performs no writes and returns only canonical, validated
    bindings that the Product Agent can hand to the existing candidate store.
    """

    if not isinstance(context, RunContext):
        raise TypeError("receipt validation requires a RunContext")
    if not isinstance(receipt, Mapping):
        raise AssemblyError("assembled receipt must be an object")
    value = dict(receipt)
    if value.get("schema_version") != ASSEMBLER_SCHEMA or value.get("status") != "complete":
        raise AssemblyError("assembled receipt is incomplete or has an unsupported schema")
    if value.get("run_id") != context.run_id or value.get("new_analytics") is not False:
        raise AssemblyError("assembled receipt run/new_analytics binding is invalid")
    outputs = value.get("outputs")
    output_hashes = value.get("output_hashes")
    expected_outputs = {
        "fixture_ref",
        "chart_map_ref",
        "chart_registry_ref",
        "blueprint_ref",
        "site_ref",
        "receipt_ref",
    }
    expected_hashes = {
        "fixture_sha256",
        "chart_map_sha256",
        "chart_registry_sha256",
        "blueprint_sha256",
        "site_manifest_sha256",
    }
    if not isinstance(outputs, Mapping) or set(outputs) != expected_outputs:
        raise AssemblyError("assembled receipt output bindings are not exact")
    if not isinstance(output_hashes, Mapping) or set(output_hashes) != expected_hashes:
        raise AssemblyError("assembled receipt output hashes are not exact")

    receipt_ref = outputs.get("receipt_ref")
    if not isinstance(receipt_ref, str) or not receipt_ref.strip():
        raise AssemblyError("assembled receipt reference is missing")
    _assert_no_symlink_chain(context, receipt_ref, label="assembled receipt reference")
    receipt_path = context.resolve_run_path(receipt_ref)
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise AssemblyError("assembled receipt reference is missing or symlinked")
    try:
        receipt_bytes = receipt_path.read_bytes()
    except OSError as exc:
        raise AssemblyError("assembled receipt cannot be read") from exc
    if receipt_bytes != _canonical_bytes(value):
        raise AssemblyError("assembled receipt is not canonical or differs from its output")

    file_outputs = (
        ("fixture_ref", "fixture_sha256"),
        ("chart_map_ref", "chart_map_sha256"),
        ("chart_registry_ref", "chart_registry_sha256"),
        ("blueprint_ref", "blueprint_sha256"),
    )
    resolved_outputs: dict[str, Path] = {}
    for ref_key, hash_key in file_outputs:
        reference = outputs.get(ref_key)
        digest = output_hashes.get(hash_key)
        if not isinstance(reference, str) or not reference.strip() or not _is_sha256(digest):
            raise AssemblyError(f"assembled receipt output binding is invalid: {ref_key}")
        _assert_no_symlink_chain(context, reference, label=f"assembled {ref_key}")
        target = context.resolve_run_path(reference)
        if target.is_symlink() or not target.is_file() or _sha256_bytes(target.read_bytes()) != digest:
            raise AssemblyError(f"assembled receipt output hash mismatch: {ref_key}")
        resolved_outputs[ref_key] = target

    site_ref = outputs.get("site_ref")
    if not isinstance(site_ref, str) or not site_ref.strip():
        raise AssemblyError("assembled receipt site reference is missing")
    _assert_no_symlink_chain(context, site_ref, label="assembled site reference")
    site_root = context.resolve_run_path(site_ref)
    if site_root.is_symlink() or not site_root.is_dir():
        raise AssemblyError("assembled receipt site is missing or symlinked")
    actual_site_binding = _site_tree_binding(site_root)
    supplied_site_binding = value.get("site_binding")
    if not isinstance(supplied_site_binding, Mapping) or dict(supplied_site_binding) != actual_site_binding:
        raise AssemblyError("assembled receipt complete site binding does not match site")
    blueprint_ref = outputs["blueprint_ref"]
    blueprint_hash = output_hashes["blueprint_sha256"]
    blueprint_binding = value.get("blueprint_binding")
    if (
        not isinstance(blueprint_binding, Mapping)
        or blueprint_binding.get("ref") != blueprint_ref
        or blueprint_binding.get("sha256") != blueprint_hash
        or blueprint_binding.get("schema_version") != "dashboard.business_presentation_plan.v2"
        or blueprint_binding.get("status") not in {"Preview", "Reviewed"}
    ):
        raise AssemblyError("assembled receipt blueprint binding is invalid")
    site_manifest_binding = _validate_site_manifest_binding(
        site_root,
        expected_blueprint_ref=blueprint_ref,
        expected_blueprint_sha256=blueprint_hash,
        expected_chart_map_ref=outputs["chart_map_ref"],
    )
    if _sha256_bytes((site_root / "site_manifest.json").read_bytes()) != output_hashes["site_manifest_sha256"]:
        raise AssemblyError("assembled receipt site manifest hash mismatch")

    # The renderer's link validator is deliberately reused after the
    # receipt/site binding checks.  Any tampered page or fragment therefore
    # fails the same program-owned gate as a tree/hash mismatch.
    _validate_site_links_in_tree(site_root)

    return {
        "valid": True,
        "stage": "assembled",
        "run_id": context.run_id,
        "generation_id": value.get("generation_id"),
        "receipt_ref": receipt_ref,
        "receipt_sha256": _sha256_bytes(receipt_bytes),
        "outputs": copy.deepcopy(dict(outputs)),
        "output_hashes": copy.deepcopy(dict(output_hashes)),
        "site_binding": copy.deepcopy(dict(actual_site_binding)),
        "site_manifest_binding": site_manifest_binding,
    }


def _assemble_dashboard_locked(
    context: RunContext,
    *,
    output_dir: str | Path = "repro_dashboard_v4",
    revision_id: str | None = None,
    item_ids: Sequence[str] | None = None,
    plan_ref: str | Path | None = None,
    fixture_ref: str | Path | None = None,
    chart_map_ref: str | Path | None = None,
    chart_registry_ref: str | Path | None = None,
    blueprint_ref: str | Path | None = None,
    site_ref: str | Path | None = None,
    receipt_ref: str | Path | None = None,
    presentation_plan_ref: str | Path | None = None,
    _preflight_only: bool = False,
) -> dict[str, Any]:
    """Assemble a deterministic run-local V4 fixture, map, registry, and site.

    ``_preflight_only`` is an internal source-inventory seam used by
    :func:`business_presentation_preflight`.  It stops after the exact
    fixture/chart-map/registry bytes are staged, before any site rendering or
    product publication; callers should use the public preflight helper rather
    than passing this private flag directly.
    """

    if not isinstance(context, RunContext):
        raise TypeError("assemble_dashboard requires one RunContext")
    rendering_identity = _rendering_identity(context)
    output_root, output_run_ref = _product_ref(context, output_dir)
    if output_root == context.run_root / "products":
        raise AssemblyError("output_dir must be a dedicated reproducibility namespace")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = output_root.parent / f".{output_root.name}.staging"
    plan = _load_plan(context, plan_ref)
    selected_ids = _discover_item_ids(context, item_ids, plan)
    groups = _group_definitions(plan, selected_ids)
    plan_binding = _plan_binding(plan, groups, plan_ref)
    generation_id, generation_metadata = _presentation_generation_metadata(context)
    if generation_metadata is not None:
        supervisor_plan_ref, supervisor_plan, supervisor_plan_hash = _presentation_supervisor_binding(
            context,
            generation_id,
            generation_metadata,
        )
    else:
        supervisor_plan_ref = Path(plan_ref or "requirement_supervisor_plan.json").as_posix()
        supervisor_plan = plan or _read_json(context, supervisor_plan_ref, label="presentation supervisor plan")
        supervisor_plan_hash = _sha256_bytes(context.resolve_run_path(supervisor_plan_ref).read_bytes())
    if generation_id == "G-0001":
        # Root products must bind to the lifecycle's canonical supervisor plan
        # bytes.  Reject an alias before any product staging or publication.
        if plan_ref is not None and Path(plan_ref).as_posix() != supervisor_plan_ref:
            raise AssemblyError("root plan_ref must be requirement_supervisor_plan.json")
        plan_binding = _root_plan_binding(supervisor_plan_ref, supervisor_plan_hash)
    presentation_plan: Mapping[str, Any] | None = None
    presentation_plan_sha256: str | None = None
    manager_widget_ids: list[str] = []
    manager_entries: dict[str, Mapping[str, Any]] = {}
    resolved_presentation_plan_ref: str | None = None
    if presentation_plan_ref is not None:
        resolved_presentation_plan_ref = _presentation_plan_ref(context, generation_id, presentation_plan_ref)
        presentation_plan, presentation_plan_sha256 = _load_business_presentation_plan(
            context,
            resolved_presentation_plan_ref,
        )
        # Input/parent bindings are checked again after all accepted and
        # committed bundles have been loaded below.
    loaded: dict[str, dict[str, Any]] = {}
    all_records: list[Mapping[str, Any]] = []
    failed_items: list[dict[str, Any]] = []
    for item_id in selected_ids:
        state_probe = _read_json(context, f"requirements/{item_id}/item_state.json", label=f"{item_id} item state")
        integration_failure = None
        if state_probe.get("lifecycle_state") == "technical_failure":
            terminal_manifest = _load_item_technical_failure_manifest(context, item_id, state_probe)
            content = {}
            accepted_manifest = terminal_manifest
            accepted_meta = {
                "state": state_probe,
                "envelope": {},
                "bundle": None,
            }
            integration_manifest = {"manifest_hash": terminal_manifest["manifest_hash"]}
            records = []
            integration_failure = {
                "status": "technical_failure",
                "recovery_exhausted": True,
                "manifest_hash": terminal_manifest["manifest_hash"],
                "manifest_ref": f"requirements/{item_id}/accepted/manifest.json",
                "reason_hash": _json_hash({"reason": terminal_manifest.get("reason")}),
            }
            failed_items.append({"item_id": item_id, **integration_failure})
        else:
            content, accepted_manifest, accepted_meta = _load_public_accepted_bundle(context, item_id)
        if state_probe.get("integration_state") == "technical_failure":
            integration_manifest = _load_technical_failure_manifest(context, item_id, accepted_meta)
            records = []
            integration_failure = {
                "status": "technical_failure",
                "recovery_exhausted": True,
                "manifest_hash": integration_manifest["manifest_hash"],
                "manifest_ref": f"requirements/{item_id}/integration/technical_failure/manifest.json",
                # Keep the failure visible without copying potentially
                # sensitive role/error text into product metadata.
                "reason_hash": _json_hash({"reason": integration_manifest.get("reason")}),
            }
            failed_items.append({"item_id": item_id, **integration_failure})
        elif state_probe.get("lifecycle_state") != "technical_failure":
            integration_manifest, records = _load_committed_records(context, item_id, accepted_manifest, accepted_meta["bundle"])
            if not records:
                # A settled requirement may carry only an explicit limitation
                # answer and no committed analytical records.  Preserve that
                # terminal state as a bounded failure/availability note so an
                # all-limited run still produces a non-blank product without
                # reclassifying it as analytical success.
                failed_items.append({
                    "item_id": item_id,
                    "status": "limited_no_committed_records",
                    "recovery_exhausted": False,
                    "manifest_hash": _text(integration_manifest.get("manifest_hash")),
                    "manifest_ref": f"requirements/{item_id}/integration/committed/manifest.json",
                    "reason_hash": _json_hash({"reason": "no committed analytical records"}),
                })
        accepted_bundle = accepted_meta.get("bundle")
        accepted_manifest_hash = (
            accepted_bundle.manifest_hash
            if accepted_bundle is not None
            else accepted_manifest.get("manifest_hash")
        )
        accepted_content_hash = (
            accepted_bundle.content_hash
            if accepted_bundle is not None
            else accepted_manifest.get("content_hash")
        )
        accepted_visual_widgets = (
            _accepted_visual_widgets(
                context,
                item_id,
                content,
                accepted_manifest,
                accepted_content_hash,
                accepted_manifest_hash,
            )
            if accepted_bundle is not None
            else []
        )
        if accepted_bundle is not None:
            # Accepted evidence rows are an independent source-bound
            # candidate universe.  They remain available even when the
            # integration boundary is terminally technical-failed; no
            # committed record is required to preserve the reviewed answer.
            accepted_visual_widgets.extend(
                _accepted_evidence_widgets(
                    context,
                    item_id,
                    content,
                    accepted_manifest,
                    accepted_content_hash,
                    accepted_manifest_hash,
                )
            )
        raw_scope = _text(content.get("scope") or content.get("method"))
        # The full reviewed scope is retained in ``requirement_scope`` and the
        # collapsed audit.  It is not a manager subtitle because method/source
        # mechanics in that text would bypass admission.
        compact_scope = ""
        loaded[item_id] = {
            "item_id": item_id,
            "content": content,
            "requirement_scope": _text(accepted_meta["state"].get("original_text") or content.get("scope") or item_id),
            "requirement_title": _manager_requirement_title(
                item_id,
                content,
                _text(accepted_meta["state"].get("original_text") or content.get("scope") or item_id),
                records,
            ),
            "requirement_subtitle": compact_scope,
            "takeaway": _manager_takeaway(
                content,
                _text(accepted_meta["state"].get("original_text") or content.get("scope") or item_id),
            ),
            "limitations": _manager_limitations(
                content,
                records,
                _text(accepted_meta["state"].get("original_text") or content.get("scope") or item_id),
            ),
            "accepted_manifest_hash": accepted_manifest_hash,
            "accepted_content_hash": accepted_content_hash,
            "integration_manifest_hash": integration_manifest["manifest_hash"],
            "records": records,
            "accepted_visual_widgets": accepted_visual_widgets,
        }
        if integration_failure is not None:
            loaded[item_id]["integration_failure"] = integration_failure
            loaded[item_id]["limitations"] = tuple(
                [*loaded[item_id]["limitations"], "Integration failed after recovery exhaustion; accepted business output is retained but no committed records feed analytics."]
            )
        all_records.extend(records)
    lem_summary, projection_metadata = _load_projection_metadata(context, selected_ids)
    legacy_hints = _load_legacy_chart_hints(context)
    legacy_chart_map_hints = _load_legacy_chart_map_hints(context)
    nodes, relationships, ontology_groups = _ontology_projection(all_records)
    fixture_path, fixture_run_ref = _product_ref(context, fixture_ref or (Path(output_dir) / "dashboard_fixture_v4.json"))
    chart_map_path, chart_map_run_ref = _product_ref(context, chart_map_ref or (Path(output_dir) / "dashboard_chart_map_v4.json"))
    registry_path, registry_run_ref = _product_ref(context, chart_registry_ref or (Path(output_dir) / "dashboard_chart_registry_v4.json"))
    blueprint_path, blueprint_run_ref = _product_ref(context, blueprint_ref or (Path(output_dir) / BLUEPRINT_FILENAME))
    site_path, site_run_ref = _product_ref(context, site_ref or (Path(output_dir) / "site"))
    receipt_path, receipt_run_ref = _product_ref(context, receipt_ref or (Path(output_dir) / "build_receipt.json"))
    output_refs = {
        "fixture_ref": fixture_run_ref,
        "chart_map_ref": chart_map_run_ref,
        "chart_registry_ref": registry_run_ref,
        "blueprint_ref": blueprint_run_ref,
        "site_ref": site_run_ref,
        "receipt_ref": receipt_run_ref,
    }
    if _canonical_preview_request_has_symlink(context, output_dir, generation_id):
        raise AssemblyError("canonical preview namespace contains a symlink component")
    canonical_preview = _is_canonical_preview_namespace(
        context,
        output_root,
        output_run_ref,
        generation_id,
        output_refs,
    )
    for path, label in ((fixture_path, "fixture_ref"), (chart_map_path, "chart_map_ref"), (registry_path, "chart_registry_ref"), (blueprint_path, "blueprint_ref"), (site_path, "site_ref"), (receipt_path, "receipt_ref")):
        try:
            path.relative_to(output_root)
        except ValueError as exc:
            raise AssemblyError(f"{label} must remain inside output_dir") from exc
    current_artifact_inputs = _analytical_artifact_input_entries(
        {item_id: loaded[item_id]["records"] for item_id in selected_ids}
    )
    current_artifacts_by_item: dict[str, list[dict[str, Any]]] = {item_id: [] for item_id in selected_ids}
    for binding in current_artifact_inputs:
        current_artifacts_by_item.setdefault(_text(binding.get("item_id")), []).append(copy.deepcopy(binding))
    current_input_items = [
        {
            "item_id": item_id,
            "accepted_content_hash": loaded[item_id]["accepted_content_hash"],
            "accepted_manifest_hash": loaded[item_id]["accepted_manifest_hash"],
            "integration_manifest_hash": loaded[item_id]["integration_manifest_hash"],
            "record_count": len(loaded[item_id]["records"]),
            # Keep an explicit empty list for requirements without typed
            # artifacts: omission would create a second, ambiguous binding
            # shape and make an accepted artifact appear only at receipt time.
            "analytical_artifacts": copy.deepcopy(current_artifacts_by_item[item_id]),
        }
        for item_id in selected_ids
    ]
    presentation_parent = _presentation_parent_binding(context, generation_id, generation_metadata)
    if presentation_plan is not None:
        # V2 visual/source validation is completed after the candidate
        # widgets and chart map have been rebuilt below.  Input/parent
        # lineage is still checked before any staging namespace is made.
        _validate_v2_plan_lineage(
            context,
            presentation_plan,
            generation_id=generation_id,
            supervisor_ref=supervisor_plan_ref,
            input_items=current_input_items,
            parent=presentation_parent,
        )
        manager_widget_ids = list(presentation_plan["manager_widget_ids"])
        manager_entries = {entry["widget_id"]: entry for entry in presentation_plan["manager_entries"]}
    existing_receipt: dict[str, Any] | None = None
    # ``ProductReviewStore.begin_revision`` creates an empty, reserved
    # artifact namespace before dispatch.  That directory is the only
    # existing output root that may be treated as a fresh assembly target;
    # every other pre-existing namespace still requires a complete receipt.
    fresh_revision_namespace = (
        revision_id is not None
        and output_root.exists()
        and output_root.is_dir()
        and not any(output_root.iterdir())
    )
    if output_root.exists() and not fresh_revision_namespace:
        if canonical_preview and (output_root.is_symlink() or not output_root.is_dir()):
            raise AssemblyError("canonical preview namespace is not a regular directory")
        if not receipt_path.is_file() or receipt_path.is_symlink():
            raise AssemblyError(f"output namespace already exists without a valid receipt: {output_dir}")
        try:
            existing_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AssemblyError(f"existing output receipt is invalid: {output_dir}") from exc
        if not isinstance(existing_receipt, Mapping) or existing_receipt.get("schema_version") != ASSEMBLER_SCHEMA or existing_receipt.get("status") != "complete":
            raise AssemblyError(f"existing output receipt is invalid: {output_dir}")
        hashes = existing_receipt.get("output_hashes")
        outputs = existing_receipt.get("outputs")
        if not isinstance(hashes, Mapping) or not isinstance(outputs, Mapping):
            raise AssemblyError("existing output receipt lacks output bindings")
        check_refs = {
            "fixture_sha256": outputs.get("fixture_ref"),
            "chart_map_sha256": outputs.get("chart_map_ref"),
            "chart_registry_sha256": outputs.get("chart_registry_ref"),
        }
        for key, reference in check_refs.items():
            if not isinstance(reference, str) or not reference:
                raise AssemblyError(f"existing output receipt reference is missing: {key}")
            path = context.resolve_run_path(reference)
            if not path.is_file() or path.is_symlink() or _sha256_bytes(path.read_bytes()) != hashes.get(key):
                raise AssemblyError(f"existing output hash mismatch: {key}")
        existing_blueprint = existing_receipt.get("blueprint_binding")
        existing_blueprint_ref = outputs.get("blueprint_ref")
        if canonical_preview and not isinstance(existing_blueprint, Mapping):
            raise AssemblyError("existing canonical preview blueprint binding is missing")
        if isinstance(existing_blueprint, Mapping):
            if existing_blueprint_ref != existing_blueprint.get("ref") or not isinstance(existing_blueprint.get("sha256"), str):
                raise AssemblyError("existing output blueprint binding is invalid")
            blueprint_target = context.resolve_run_path(existing_blueprint_ref)
            if (
                not blueprint_target.is_file()
                or blueprint_target.is_symlink()
                or _sha256_bytes(blueprint_target.read_bytes()) != existing_blueprint.get("sha256")
            ):
                raise AssemblyError("existing output blueprint hash mismatch")
        site_ref_value = outputs.get("site_ref")
        if not isinstance(site_ref_value, str) or not site_ref_value:
            raise AssemblyError("existing output receipt reference is missing: site_ref")
        if canonical_preview:
            if existing_receipt.get("run_id") != context.run_id or existing_receipt.get("generation_id") != generation_id or existing_receipt.get("new_analytics") is not False:
                raise AssemblyError("existing canonical preview receipt lineage is invalid")
            for key, expected in output_refs.items():
                if outputs.get(key) != expected:
                    raise AssemblyError(f"existing canonical preview output binding is invalid: {key}")
            if any(not _is_sha256(hashes.get(key)) for key in ("fixture_sha256", "chart_map_sha256", "chart_registry_sha256", "site_manifest_sha256")):
                raise AssemblyError("existing canonical preview output hashes are invalid")
        actual_site_binding = _site_tree_binding(context.resolve_run_path(site_ref_value))
        if actual_site_binding != existing_receipt.get("site_binding"):
            raise AssemblyError("existing output site file hash mismatch")
        site_manifest_path = context.resolve_run_path(f"{site_ref_value.rstrip('/')}/site_manifest.json")
        if not site_manifest_path.is_file() or site_manifest_path.is_symlink():
            raise AssemblyError("existing output site manifest is missing or symlinked")
        if _sha256_bytes(site_manifest_path.read_bytes()) != hashes.get("site_manifest_sha256"):
            raise AssemblyError("existing output hash mismatch: site_manifest_sha256")
        if not canonical_preview:
            if existing_receipt.get("input_items") != current_input_items:
                raise AssemblyError("existing output namespace input hashes do not match current accepted/committed inputs")
            if existing_receipt.get("plan_binding") != plan_binding:
                raise AssemblyError("existing output namespace supervisor plan/grouping hash does not match")
            existing_presentation_ref = existing_receipt.get("presentation_plan_ref")
            existing_presentation_hash = existing_receipt.get("presentation_plan_sha256")
            existing_manager_ids = existing_receipt.get("manager_widget_ids")
            if presentation_plan is None:
                if existing_presentation_ref is not None or existing_presentation_hash is not None or existing_manager_ids not in (None, []):
                    raise AssemblyError("existing output namespace requires its explicit presentation plan")
            elif (
                existing_presentation_ref is not None
                and (existing_presentation_ref != resolved_presentation_plan_ref or existing_presentation_hash != presentation_plan_sha256 or existing_manager_ids != list(manager_widget_ids))
            ):
                raise AssemblyError("existing output namespace presentation plan binding does not match")
            existing_freeze = existing_receipt.get("freeze_inputs")

            def _bound_hash(value: Any) -> Any:
                return value.get("sha256") if isinstance(value, Mapping) else None

            if (
                not isinstance(existing_freeze, Mapping)
                or existing_freeze.get("projection_hash") != projection_metadata["projection_hash"]
                or _bound_hash(existing_freeze.get("prepared_registry")) != projection_metadata["prepared_registry"]["sha256"]
                or _bound_hash(existing_freeze.get("prepared_index")) != projection_metadata["prepared_index"]["sha256"]
            ):
                raise AssemblyError("existing output namespace frozen projection/metadata hashes do not match")
        existing_receipt = dict(existing_receipt)
    # The public wrapper holds the namespace lock before entering this
    # function.  A prior process may have died after creating the deterministic
    # staging directory (for example, a user KeyboardInterrupt during site
    # rendering).  That directory is never authoritative until the final
    # atomic rename, so remove only this exact, lock-owned namespace and rebuild
    # from the immutable accepted/committed inputs below.
    _remove_orphan_staging(staging_root)
    staging_root.mkdir(parents=True)
    staging_prefix = staging_root.relative_to(context.run_root).as_posix()
    final_prefix = output_root.relative_to(context.run_root).as_posix()
    fixture_rel = fixture_path.relative_to(output_root)
    chart_map_rel = chart_map_path.relative_to(output_root)
    registry_rel = registry_path.relative_to(output_root)
    blueprint_rel = blueprint_path.relative_to(output_root)
    site_rel = site_path.relative_to(output_root)
    receipt_rel = receipt_path.relative_to(output_root)
    staged_fixture_path = staging_root / fixture_rel
    staged_map_path = staging_root / chart_map_rel
    staged_registry_path = staging_root / registry_rel
    staged_blueprint_path = staging_root / blueprint_rel
    staged_site_path = staging_root / site_rel
    staged_receipt_path = staging_root / receipt_rel
    staged_fixture_ref = staged_fixture_path.relative_to(context.run_root).as_posix()
    staged_map_ref = staged_map_path.relative_to(context.run_root).as_posix()
    staged_registry_ref = staged_registry_path.relative_to(context.run_root).as_posix()
    staged_blueprint_ref = staged_blueprint_path.relative_to(context.run_root).as_posix()
    staged_site_ref = staged_site_path.relative_to(context.run_root / "products").as_posix()
    try:
        registry_source = Path(__file__).resolve().parent.parent / "assets" / "dashboard_chart_registry.json"
        registry_info = _build_registry(registry_source, staged_registry_path)
        staged_fixture_path.parent.mkdir(parents=True, exist_ok=True)
        staged_map_path.parent.mkdir(parents=True, exist_ok=True)
        widgets: list[dict[str, Any]] = []
        domains: list[dict[str, Any]] = []
        chart_items: list[dict[str, Any]] = []
        for group in groups:
            flow_defs: list[dict[str, Any]] = []
            for flow_order, item_id in enumerate(group["requirement_ids"], 1):
                item = loaded[item_id]
                # Failed items are represented in ``failed_items`` rather
                # than as empty decision flows; keep surviving flow order
                # contiguous for the renderer's schema.
                flow_order = len(flow_defs) + 1
                widget_content = dict(item["content"])
                widget_content["__manager_requirement_scope"] = item["requirement_scope"]
                item_widgets = _build_widgets(
                    item_id,
                    widget_content,
                    item["records"],
                    accepted_visuals=item.get("accepted_visual_widgets", ()),
                    legacy_hints=legacy_hints,
                    manager_widget_ids=manager_widget_ids,
                    manager_entries=manager_entries,
                )
                _collision_safe_widget_ids(item_id, group["id"], item_widgets, item["records"])
                admitted_widget_ids = {
                    _text(widget.get("id"))
                    for widget in item_widgets
                    if isinstance(widget.get("manager_admission"), Mapping)
                    and widget["manager_admission"].get("status") == "admitted"
                }
                item["manager_admission"] = {
                    "status": "admitted" if admitted_widget_ids else "audit_only",
                    "presentation_audience": "business_manager" if admitted_widget_ids else "technical_audit",
                    "policy": "explicit_business_presentation_plan",
                "admitted_widget_ids": [widget_id for widget_id in manager_widget_ids if widget_id in admitted_widget_ids],
                    "presentation_plan_ref": resolved_presentation_plan_ref,
                    "presentation_plan_sha256": presentation_plan_sha256,
                }
                for widget_index, widget in enumerate(item_widgets, 1):
                    widget["requirement_id"] = item_id
                    widget["requirement_title"] = item["requirement_title"]
                    if item["requirement_subtitle"]:
                        widget["requirement_subtitle"] = item["requirement_subtitle"]
                    if item["takeaway"]:
                        widget["takeaway"] = item["takeaway"]
                    if item["requirement_scope"]:
                        widget["requirement_scope"] = item["requirement_scope"]
                    if item["limitations"]:
                        widget["requirement_limitations"] = list(item["limitations"])
                    widget["requirement_order"] = flow_order
                    widget["domain_id"] = group["id"]
                    widget["manager_admission"] = dict(widget.get("manager_admission") or {})
                    widget["manager_admission"]["requirement_status"] = item["manager_admission"]["status"]
                    widget["manager_anchor"] = f"{_slug(item_id)}-{_slug(widget.get('presentation_role') or 'decision-view')}-{widget_index}"
                    widgets.append(widget)
                    chart_entry = _chart_map_entry(widget, item_id, item)
                    inherited_chart_entry = legacy_chart_map_hints.get(_text(widget.get("id")).strip())
                    chart_items.append(
                        copy.deepcopy(inherited_chart_entry)
                        if inherited_chart_entry is not None
                        and _text(widget.get("integration_record_id"))
                        and _text(widget.get("artifact_type")) == ""
                        else chart_entry
                    )
                flow = {
                    "id": f"{group['id']}-{_slug(item_id)}",
                    "title": item["requirement_title"],
                    "order": flow_order,
                    "widget_ids": [widget["id"] for widget in item_widgets],
                    "manager_admission": dict(item["manager_admission"]),
                    "presentation_audience": item["manager_admission"]["presentation_audience"],
                }
                # Section headers carry the original requirement text and the
                # accepted headline/takeaway.  The latter is context only;
                # no number mining or re-analysis occurs in the assembler.
                if item["requirement_subtitle"]:
                    flow["subtitle"] = item["requirement_subtitle"]
                if item["takeaway"]:
                    flow["takeaway"] = item["takeaway"]
                flow_scope = _text(group.get("scope")).strip() or item["requirement_scope"]
                if flow_scope:
                    flow["scope"] = flow_scope
                if item["limitations"]:
                    flow["limitations"] = list(item["limitations"])
                if item.get("integration_failure"):
                    flow["failure"] = copy.deepcopy(item["integration_failure"])
                # The renderer's decision-flow contract requires every flow
                # to carry at least one typed widget.  A requirement that
                # failed before acceptance or at the accepted integration
                # boundary has no business/integration records to render;
                # keep it visible through the fixture-level ``failed_items``
                # and limitations projections instead of fabricating a
                # placeholder widget or emitting an invalid empty flow.
                if item_widgets:
                    flow_defs.append(flow)
            if flow_defs:
                domain_title = _presentation_domain_title(group, flow_defs)
                domains.append({"id": group["id"], "title": domain_title, "summary": group.get("summary"), "order": group["order"], "decision_flow": flow_defs})
        limited_dashboard = False
        if not widgets:
            if not failed_items:
                raise AssemblyError("accepted/integrated inputs produced no typed presentation widgets")
            # All selected requirements are terminal failures.  Keep the
            # product non-blank with one explicit source-bound status table;
            # this is not an analytical result and carries no invented value.
            empty_widget = _limited_availability_widget(context, failed_items)
            # A V2 plan may explicitly select the limited availability visual
            # from the preflight inventory.  Preserve that exact plan entry so
            # the manager surface, Blueprint, site, and receipt all bind the
            # same source-bound empty-state choice.  A planless legacy/manual
            # invocation retains the deterministic table-only fallback below.
            if presentation_plan is not None:
                limited_id = _text(empty_widget.get("id")).strip()
                selected_entry = manager_entries.get(limited_id)
                if (
                    list(manager_widget_ids) != [limited_id]
                    or not isinstance(selected_entry, Mapping)
                    or presentation_plan.get("manager_visual_widget_ids") != [limited_id]
                    or presentation_plan.get("audit_visual_widget_ids") != []
                ):
                    raise BusinessPresentationPlanError(
                        "v2 limited dashboard plan must select its source-bound empty-state visual"
                    )
                empty_widget["manager_presentation"] = copy.deepcopy(dict(selected_entry))
                empty_widget["manager_admission"]["presentation_plan_ref"] = resolved_presentation_plan_ref
                empty_widget["manager_admission"]["presentation_plan_sha256"] = presentation_plan_sha256
            else:
                manager_widget_ids = [empty_widget["id"]]
                manager_entries = {}
            widgets.append(empty_widget)
            chart_items.append(_chart_map_entry(empty_widget, "business-availability", {"item_id": "business-availability"}))
            domains = [{
                "id": "business-availability",
                "title": "Business availability",
                "summary": "Terminal outcome only; no accepted business visual is available.",
                "order": 1,
                "decision_flow": [{
                    "id": "business-availability-flow",
                    "title": "Business visual availability",
                    "order": 1,
                    "widget_ids": [empty_widget["id"]],
                    "manager_admission": copy.deepcopy(empty_widget["manager_admission"]),
                    "presentation_audience": "business_manager",
                }],
            }]
            if presentation_plan is None:
                manager_widget_ids = [empty_widget["id"]]
                manager_entries = {}
            limited_dashboard = True
        # Product plan order is authoritative for the manager overview; the
        # helper only projects those selected IDs and never chooses by shape.
        presentation_copy = copy.deepcopy((presentation_plan or {}).get("presentation", {}))
        overview_widget_ids = _apply_overview_selection(
            widgets, presentation_copy.get("overview_widget_ids", manager_widget_ids))
        audit_records = _audit_record_entries(
            {item_id: loaded[item_id]["records"] for item_id in selected_ids},
            widgets,
        )
        artifact_inputs = current_artifact_inputs
        artifact_inputs_by_item = current_artifacts_by_item
        audit_widgets = _audit_widget_entries(widgets)
        fixture: dict[str, Any] = {
            "schema_version": FIXTURE_SCHEMA,
            "dashboard_version": 4,
            "site_version": 4,
            "title": presentation_copy.get("title", "Business dashboard"),
            "presentation": presentation_copy,
            "subtitle": presentation_copy.get("subtitle", "Reviewed business results with source context and limitations."),
            "run_id": context.run_id,
            "generation_id": generation_id,
            "skill_name": SKILL_NAME,
            "skill_version": context.skill_version or "0.8.0",
            "core_name": CORE_NAME,
            "core_version": context.core_version,
            "freeze_markers": dict(projection_metadata["freeze_markers"]),
            "chart_registry_ref": staged_registry_ref,
            "chart_map_ref": staged_map_ref,
            "domains": domains,
            "widgets": widgets,
            "audit_records": audit_records,
            "audit_widgets": audit_widgets,
            "audit_widget_entry_count": len(audit_widgets),
            "analytical_artifacts": artifact_inputs,
            "ontology_summary": lem_summary,
            "overview_widget_ids": overview_widget_ids,
            "presentation_plan_ref": resolved_presentation_plan_ref,
            "presentation_plan_sha256": presentation_plan_sha256,
            "manager_widget_ids": list(manager_widget_ids),
            "manager_entries": [
                copy.deepcopy(manager_entries[key])
                for key in manager_widget_ids
                if key in manager_entries
            ],
            "manager_admission": {
                "policy": "terminal_limited_dashboard" if limited_dashboard else "explicit_business_presentation_plan",
                "presentation_plan_ref": resolved_presentation_plan_ref,
                "presentation_plan_sha256": presentation_plan_sha256,
                "business_requirements": sorted(
                    item_id for item_id, item in loaded.items()
                    if item.get("manager_admission", {}).get("status") == "admitted"
                ),
                "technical_requirements": sorted(
                    item_id for item_id, item in loaded.items()
                    if item.get("manager_admission", {}).get("status") != "admitted"
                ),
            },
            "lem_projection_hash": projection_metadata["projection_hash"],
            "lem_export_sha256": projection_metadata["export_sha256"],
            "prepared_registry_hash": projection_metadata["prepared_registry"]["sha256"],
            "telemetry_metadata_hash": projection_metadata["telemetry"]["sha256"],
            "ontology_objects": nodes,
            "ontology_relationships": relationships,
            "ontology_groups": ontology_groups,
            "limitations": ["Presentation values are copied from accepted answer/integration records only.", "Unknown, malformed, mixed-unit, and no-FX values remain visible as limitations or tables; no missing value is rendered as zero."],
            "failed_items": copy.deepcopy(failed_items),
        }
        if limited_dashboard:
            fixture["limited_dashboard"] = True
            fixture["limited_dashboard_reason"] = "all_selected_requirements_terminal"
        if failed_items:
            fixture["limitations"].append(
                "Failed requirements are listed explicitly; their accepted outputs remain immutable and do not contribute fabricated analytics or ontology records."
            )
        if presentation_plan is not None:
            fixture["manager_visual_widget_ids"] = list(presentation_plan["manager_visual_widget_ids"])
            fixture["audit_visual_widget_ids"] = list(presentation_plan["audit_visual_widget_ids"])
            fixture["visual_entries"] = copy.deepcopy(presentation_plan["visual_entries"])
            fixture["presentation_plan_schema"] = PRESENTATION_PLAN_V2_SCHEMA
        chart_map = {"schema_version": CHART_MAP_SCHEMA, "chart_registry_ref": staged_registry_ref, "fixture_ref": staged_fixture_ref, "charts": chart_items}
        if presentation_plan is not None:
            _validate_business_presentation_plan_v2(
                context,
                presentation_plan,
                fixture={"widgets": widgets},
                fixture_ref=staged_fixture_ref,
                chart_map=chart_map,
                chart_map_ref=staged_map_ref,
                widgets=widgets,
                strict_source_hash=False,
            )
        _write_json(staged_fixture_path, fixture)
        _write_json(staged_map_path, chart_map)
        if _preflight_only:
            # Return only the source-bound in-memory inputs.  The temporary
            # staging namespace is removed before returning, so a preflight
            # cannot accidentally publish or leave behind an immutable
            # dashboard/site candidate.
            try:
                registry_payload = json.loads(staged_registry_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AssemblyError("preflight chart registry is invalid") from exc
            if not isinstance(registry_payload, Mapping):
                raise AssemblyError("preflight chart registry is invalid")
            result = {
                "schema_version": PRESENTATION_PLAN_V2_SCHEMA,
                "run_id": context.run_id,
                "generation_id": generation_id,
                "item_ids": list(selected_ids),
                "input_items": copy.deepcopy(current_input_items),
                "input_fingerprint": _preflight_input_fingerprint(current_input_items, rendering_identity),
                "rendering_identity": copy.deepcopy(rendering_identity),
                "fixture": copy.deepcopy(fixture),
                "chart_map": copy.deepcopy(chart_map),
                "registry": copy.deepcopy(registry_payload),
            }
            shutil.rmtree(staging_root, ignore_errors=True)
            return result
        # Build the exact source-bound Blueprint before rendering.  The
        # renderer validates this staged artifact and therefore consumes the
        # Product Agent's selected recipe/layout/renderer_type rather than
        # deriving a post-hoc presentation from fixture metadata.
        blueprint_registry = _read_json(
            context,
            staged_registry_path.relative_to(context.run_root),
            label="staged chart registry",
        )
        provisional_blueprint = _dashboard_runtime().build_blueprint(
            fixture=fixture,
            chart_map=chart_map,
            registry=blueprint_registry,
            fixture_ref=staged_fixture_ref,
            chart_map_ref=staged_map_ref,
            registry_ref=staged_registry_ref,
            fixture_sha256=_sha256_bytes(staged_fixture_path.read_bytes()),
            chart_map_sha256=_sha256_bytes(staged_map_path.read_bytes()),
            registry_sha256=registry_info["sha256"],
            blueprint_ref=staged_blueprint_ref,
            presentation_plan_ref=resolved_presentation_plan_ref,
            presentation_plan_sha256=presentation_plan_sha256,
            review_status="Preview",
        )
        _write_json(staged_blueprint_path, provisional_blueprint)
        # render_site_fixture validates the fixture, chart map, registry, links,
        # and the offline stylesheet before the staging namespace is published.
        renderer_path = Path(__file__).resolve().with_name("dashboard_renderer.py")
        import importlib.util
        spec = importlib.util.spec_from_file_location("dashboard_renderer_for_assembler", renderer_path)
        if spec is None or spec.loader is None:
            raise AssemblyError("dashboard renderer cannot be loaded")
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        renderer.render_site_fixture(
            context,
            staged_fixture_ref,
            staged_site_ref,
            f"{staged_site_ref}/site_manifest.json",
            blueprint_ref=staged_blueprint_ref,
        )
        _replace_prefix(staging_root, staging_prefix, final_prefix)
        fixture["chart_registry_ref"] = registry_run_ref
        fixture["chart_map_ref"] = chart_map_run_ref
        chart_map["chart_registry_ref"] = registry_run_ref
        chart_map["fixture_ref"] = fixture_run_ref
        _write_json(staged_fixture_path, fixture)
        _write_json(staged_map_path, chart_map)
        # Persist the canonical source-bound V2 blueprint beside the rendered
        # site.  It is built from the exact final fixture/map/registry bytes;
        # the runtime never aggregates or invents rows.
        blueprint_registry = _read_json(
            context,
            staged_registry_path.relative_to(context.run_root),
            label="staged chart registry",
        )
        blueprint = _dashboard_runtime().build_blueprint(
            fixture=fixture,
            chart_map=chart_map,
            registry=blueprint_registry,
            fixture_ref=fixture_run_ref,
            chart_map_ref=chart_map_run_ref,
            registry_ref=registry_run_ref,
            fixture_sha256=_sha256_bytes(staged_fixture_path.read_bytes()),
            chart_map_sha256=_sha256_bytes(staged_map_path.read_bytes()),
            registry_sha256=registry_info["sha256"],
            blueprint_ref=blueprint_run_ref,
            presentation_plan_ref=resolved_presentation_plan_ref,
            presentation_plan_sha256=presentation_plan_sha256,
            review_status="Preview",
        )
        _write_json(staged_blueprint_path, blueprint)
        blueprint_sha256 = _sha256_bytes(staged_blueprint_path.read_bytes())
        staged_site_manifest_path = staged_site_path / "site_manifest.json"
        if not staged_site_manifest_path.is_file():
            raise AssemblyError("renderer did not produce site_manifest.json")
        site_manifest = _read_json(context, staged_site_manifest_path.relative_to(context.run_root), label="staged site manifest")
        site_manifest = dict(site_manifest)
        site_manifest["blueprint_ref"] = blueprint_run_ref
        site_manifest["blueprint_sha256"] = blueprint_sha256
        site_manifest["blueprint_schema"] = blueprint.get("schema_version")
        site_manifest["status"] = "Preview"
        site_manifest["chart_map_sha256"] = _sha256_bytes(staged_map_path.read_bytes())
        non_manifest_site_binding = _site_tree_binding(staged_site_path, exclude={"site_manifest.json"})
        site_manifest["site_file_hashes"] = non_manifest_site_binding["files"]
        site_manifest["site_tree_sha256"] = non_manifest_site_binding["tree_sha256"]
        site_manifest["site_tree_file_count"] = non_manifest_site_binding["file_count"]
        _write_json(staged_site_manifest_path, site_manifest)
        site_binding = _site_tree_binding(staged_site_path)
        # Keep the receipt/freeze schema stable for every run.  The artifact
        # binding is an exact list even when no typed artifacts were supplied;
        # consumers must not infer schema from whether a run happened to carry
        # artifacts.
        freeze_inputs = copy.deepcopy(projection_metadata)
        freeze_inputs["analytical_artifacts"] = copy.deepcopy(artifact_inputs)
        receipt = {
            "schema_version": ASSEMBLER_SCHEMA,
            "status": "complete",
            "run_id": context.run_id,
            "generation_id": generation_id,
            "source_policy": "accepted_and_committed_only",
            "new_analytics": False,
            "input_items": copy.deepcopy(current_input_items),
            "analytical_artifacts": artifact_inputs,
            "plan_binding": plan_binding,
            "outputs": {"fixture_ref": fixture_run_ref, "chart_map_ref": chart_map_run_ref, "chart_registry_ref": registry_run_ref, "blueprint_ref": blueprint_run_ref, "site_ref": site_run_ref, "receipt_ref": receipt_run_ref},
            "output_hashes": {
                "fixture_sha256": _sha256_bytes(staged_fixture_path.read_bytes()),
                "chart_map_sha256": _sha256_bytes(staged_map_path.read_bytes()),
                "chart_registry_sha256": registry_info["sha256"],
                "blueprint_sha256": blueprint_sha256,
                "site_manifest_sha256": _sha256_bytes(staged_site_manifest_path.read_bytes()),
            },
            "blueprint_binding": {"ref": blueprint_run_ref, "sha256": blueprint_sha256, "schema_version": blueprint.get("schema_version"), "status": "Preview"},
            "site_binding": site_binding,
            "rendering_identity": copy.deepcopy(rendering_identity),
            "freeze_inputs": freeze_inputs,
            "widget_count": len(widgets),
            "ontology_counts": {"objects": len(nodes), "relationships": len(relationships), "groups": len(ontology_groups), "summary": lem_summary},
            "retry": "idempotent only when the existing receipt/input hashes match; conflicting output namespaces fail closed",
        }
        if generation_id == "G-0001":
            receipt["parent"] = {
                "root_generation": True,
                "parent_generation_id": None,
                "parent_manifest_ref": None,
                "parent_manifest_hash": None,
            }
        # Keep plan metadata on receipts that actually bind a manager plan.
        # Historical no-plan receipts remain consumable by the legacy parent
        # bridge; their fixture is explicitly all-audit and carries no
        # admitted IDs, so omitting absent plan fields is semantically exact.
        if presentation_plan is not None:
            receipt.update({
                "presentation_plan_ref": resolved_presentation_plan_ref,
                "presentation_plan_sha256": presentation_plan_sha256,
                "manager_widget_ids": list(manager_widget_ids),
            })
        _write_json(staged_receipt_path, receipt)
        # Receipt hash is intentionally not self-referential; every file is
        # complete before publication.  An unchanged candidate is a true
        # idempotent retry.  A changed candidate is allowed to replace only
        # this product namespace; accepted answers, integration, LEM, and
        # source artifacts are never touched by the swap.
        if existing_receipt is not None and existing_receipt == receipt:
            shutil.rmtree(staging_root, ignore_errors=True)
            return existing_receipt
        _publish_staged_output(staging_root, output_root)
        return receipt
    except BaseException:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


def assemble_dashboard(
    context: RunContext,
    *,
    output_dir: str | Path = "repro_dashboard_v4",
    item_ids: Sequence[str] | None = None,
    plan_ref: str | Path | None = None,
    fixture_ref: str | Path | None = None,
    chart_map_ref: str | Path | None = None,
    chart_registry_ref: str | Path | None = None,
    blueprint_ref: str | Path | None = None,
    site_ref: str | Path | None = None,
    receipt_ref: str | Path | None = None,
    presentation_plan_ref: str | Path | None = None,
    _preflight_only: bool = False,
    revision_id: str | None = None,
    output_root_ref: str | Path | None = None,
) -> dict[str, Any]:
    """Serialize full/root assembly before reconciling its staging namespace."""

    if not isinstance(context, RunContext):
        raise TypeError("assemble_dashboard requires one RunContext")
    revision_root = _revision_output_root(context, revision_id, output_root_ref)
    if revision_root is not None:
        expected_root, expected_ref = revision_root
        if output_dir is None or str(output_dir) == "repro_dashboard_v4":
            output_dir = expected_ref
        else:
            requested_root, _requested_ref = _product_ref(context, output_dir)
            if requested_root != expected_root:
                raise AssemblyError("product revision output_dir disagrees with output_root_ref")
    output_root, _output_run_ref = _product_ref(context, output_dir)
    if output_root == context.run_root / "products":
        raise AssemblyError("output_dir must be a dedicated reproducibility namespace")
    lock_path = output_root.parent / f".{output_root.name}.assembly.lock"
    with _assembly_lock(lock_path):
        return _assemble_dashboard_locked(
            context,
            output_dir=output_dir,
            revision_id=revision_id,
            item_ids=item_ids,
            plan_ref=plan_ref,
            fixture_ref=fixture_ref,
            chart_map_ref=chart_map_ref,
            chart_registry_ref=chart_registry_ref,
            blueprint_ref=blueprint_ref,
            site_ref=site_ref,
            receipt_ref=receipt_ref,
            presentation_plan_ref=presentation_plan_ref,
            _preflight_only=_preflight_only,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=False, help="products-relative reproducibility output directory")
    parser.add_argument("--presentation-plan-ref", required=False, help="run-relative business presentation plan")
    parser.add_argument("--revision-id", required=False, help="bound Product revision identifier")
    parser.add_argument("--output-root-ref", required=False, help="run-relative immutable Product revision output root")
    parser.add_argument("--write-presentation-plan", action="store_true", help="write an explicit manager presentation plan")
    parser.add_argument("--presentation-inventory-fixture-ref", required=False, help="export a read-only candidate inventory")
    parser.add_argument("--presentation-visual-inventory", action="store_true", help="export the read-only V2 visual inventory")
    parser.add_argument("--presentation-fixture-ref", required=False, help="run-relative candidate fixture for plan selection")
    parser.add_argument("--reviewer-ref", required=False, help="reviewer reference for an explicit presentation plan")
    parser.add_argument("--manager-entry-json", action="append", default=[], help="JSON file containing one pointer-bound manager entry (repeatable)")
    parser.add_argument("--revise-presentation-plan-v2", action="store_true", help="CAS-replace the current V2 plan with a canonical V2 successor")
    parser.add_argument("--record-presentation-plan-v2", action="store_true", help="create the active generation's absent canonical V2 plan")
    parser.add_argument("--successor-plan-json", required=False, help="JSON path for the V2 successor plan")
    parser.add_argument("--expected-current-plan-sha256", required=False)
    parser.add_argument("--expected-successor-plan-sha256", required=False)
    parser.add_argument("--presentation-chart-map-ref", required=False, help="run-relative active-generation V2 chart map")
    parser.add_argument("--presentation-previous-plan-ref", required=False, help="run-relative immediate predecessor V2 plan")
    parser.add_argument("--expected-fixture-sha256", required=False)
    parser.add_argument("--expected-chart-map-sha256", required=False)
    parser.add_argument("--expected-previous-plan-sha256", required=False)
    args = parser.parse_args(argv)
    try:
        context = RunContext(run_id=args.run_id, run_root=args.run_root)
        if args.revise_presentation_plan_v2:
            if not args.presentation_plan_ref or not args.successor_plan_json or not args.expected_current_plan_sha256 or not args.expected_successor_plan_sha256:
                raise BusinessPresentationPlanError("v2 revision requires --presentation-plan-ref, --successor-plan-json, and both expected hashes")
            result = revise_business_presentation_plan_v2(
                context,
                successor_plan=args.successor_plan_json,
                expected_current_plan_sha256=args.expected_current_plan_sha256,
                expected_successor_plan_sha256=args.expected_successor_plan_sha256,
                presentation_plan_ref=args.presentation_plan_ref,
            )
        elif args.record_presentation_plan_v2:
            required = (
                args.presentation_fixture_ref,
                args.presentation_chart_map_ref,
                args.presentation_previous_plan_ref,
                args.reviewer_ref,
                args.expected_fixture_sha256,
                args.expected_chart_map_sha256,
                args.expected_previous_plan_sha256,
                args.expected_successor_plan_sha256,
            )
            if any(value is None for value in required):
                raise BusinessPresentationPlanError(
                    "direct V2 recording requires fixture/map/previous refs, reviewer, and all expected source/successor hashes"
                )
            if not args.manager_entry_json:
                raise BusinessPresentationPlanError(
                    "direct V2 recording requires the complete ordered manager selection via --manager-entry-json"
                )
            manager_entries = []
            for entry_ref in args.manager_entry_json:
                entry_path = Path(entry_ref)
                if entry_path.is_symlink() or not entry_path.is_file():
                    raise BusinessPresentationPlanError(f"manager entry JSON is missing or symlinked: {entry_ref}")
                raw_entry = json.loads(entry_path.read_text(encoding="utf-8"))
                if not isinstance(raw_entry, Mapping):
                    raise BusinessPresentationPlanError(f"manager entry JSON must be an object: {entry_ref}")
                manager_entries.append(dict(raw_entry))
            result = record_business_presentation_plan_v2(
                context,
                fixture_ref=args.presentation_fixture_ref,
                chart_map_ref=args.presentation_chart_map_ref,
                previous_plan_ref=args.presentation_previous_plan_ref,
                manager_entries=manager_entries,
                reviewer_ref=args.reviewer_ref,
                expected_fixture_sha256=args.expected_fixture_sha256,
                expected_chart_map_sha256=args.expected_chart_map_sha256,
                expected_previous_plan_sha256=args.expected_previous_plan_sha256,
                expected_successor_plan_sha256=args.expected_successor_plan_sha256,
                presentation_plan_ref=args.presentation_plan_ref,
            )
        elif args.presentation_visual_inventory:
            if not args.presentation_inventory_fixture_ref:
                raise BusinessPresentationPlanError("v2 visual inventory requires --presentation-inventory-fixture-ref")
            result = business_presentation_visual_inventory(
                context,
                fixture_ref=args.presentation_inventory_fixture_ref,
            )
        elif args.presentation_inventory_fixture_ref:
            result = business_presentation_inventory(
                context,
                fixture_ref=args.presentation_inventory_fixture_ref,
            )
        elif args.write_presentation_plan:
            if not args.presentation_fixture_ref or not args.reviewer_ref or not args.manager_entry_json:
                raise BusinessPresentationPlanError("plan writing requires --presentation-fixture-ref, --reviewer-ref, and --manager-entry-json")
            manager_entries = []
            for entry_ref in args.manager_entry_json:
                entry_path = Path(entry_ref)
                if entry_path.is_symlink() or not entry_path.is_file():
                    raise BusinessPresentationPlanError(f"manager entry JSON is missing or symlinked: {entry_ref}")
                raw_entry = json.loads(entry_path.read_text(encoding="utf-8"))
                if not isinstance(raw_entry, Mapping):
                    raise BusinessPresentationPlanError(f"manager entry JSON must be an object: {entry_ref}")
                manager_entries.append(dict(raw_entry))
            result = write_business_presentation_plan(
                context,
                manager_entries=manager_entries,
                reviewer_ref=args.reviewer_ref,
                fixture_ref=args.presentation_fixture_ref,
                presentation_plan_ref=args.presentation_plan_ref,
            )
        else:
            if not args.output_dir and not args.output_root_ref:
                raise AssemblyError("--output-dir is required for dashboard assembly")
            result = assemble_dashboard(
                context,
                output_dir=args.output_dir or "repro_dashboard_v4",
                presentation_plan_ref=args.presentation_plan_ref,
                revision_id=args.revision_id,
                output_root_ref=args.output_root_ref,
            )
    except (OSError, ValueError, AssemblyError, BusinessPresentationPlanError, AllowedRootError) as exc:
        print(f"dashboard assembler: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
