#!/usr/bin/env python3
"""Validate local release ZIP and wheel without network or runtime installs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable


ZIP_NAME = "auto-foundry-agentic-e2e-v0.8.0.zip"
RELEASE_SLUG = "reliable-analytics-dashboard"
STALE_RELEASE_SLUG = "cognitive-requirement-supervisor-and-simple-item-flow"

# The release deliberately replaces the old deterministic optimizer report
# helper with the evidence collector.  Keep this check explicit so a stale
# build directory cannot make an obsolete optimizer entry look installable.
REQUIRED_SKILL_MEMBERS = {
    "scripts/product_workspace.py",
    "scripts/dashboard_assembler.py",
    "scripts/dashboard_delta_assembler.py",
    "scripts/dashboard_renderer.py",
    "scripts/optimizer_evidence_collector.py",
    "references/FINAL_PRODUCT_AND_AUTOMATION.md",
    "references/ANALYTICAL_COLLABORATION.md",
    "references/ANALYTICS_TOOLKIT.md",
    "assets/REQUIREMENT_RECORD_TEMPLATE.json",
    "assets/ITEM_STATE_TEMPLATE.json",
}
DELETED_OPTIMIZER_MEMBERS = {
    "scripts/experimental_optimizer.py",
    "assets/EXPERIMENTAL_OPTIMIZER_REPORT_TEMPLATE.md",
}
REQUIRED_CORE_MODULES = {
    "auto_foundry_core/analysis.py",
    "auto_foundry_core/analyst_workspace.py",
    "auto_foundry_core/__init__.py",
    "auto_foundry_core/__main__.py",
    "auto_foundry_core/aggregation.py",
    "auto_foundry_core/artifacts.py",
    "auto_foundry_core/cache.py",
    "auto_foundry_core/capabilities.py",
    "auto_foundry_core/catalog.py",
    "auto_foundry_core/cli.py",
    "auto_foundry_core/contracts.py",
    "auto_foundry_core/durable.py",
    "auto_foundry_core/enterprise_model.py",
    "auto_foundry_core/identity.py",
    "auto_foundry_core/integration.py",
    "auto_foundry_core/integration_review.py",
    "auto_foundry_core/lifecycle.py",
    "auto_foundry_core/lem_projection.py",
    "auto_foundry_core/normalization.py",
    "auto_foundry_core/populations.py",
    "auto_foundry_core/profiling.py",
    "auto_foundry_core/prepared.py",
    "auto_foundry_core/product_contracts.py",
    "auto_foundry_core/reporting.py",
    "auto_foundry_core/relationships.py",
    "auto_foundry_core/reproduction.py",
    "auto_foundry_core/references.py",
    "auto_foundry_core/requirement_planning.py",
    "auto_foundry_core/runtime.py",
    "auto_foundry_core/sources.py",
    "auto_foundry_core/telemetry.py",
    "auto_foundry_core/workbench.py",
    "auto_foundry_core/workspace.py",
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str) -> bool:
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in name


def _skill_files(skill_root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in skill_root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"} or path.name == ".DS_Store":
            continue
        result[path.relative_to(skill_root).as_posix()] = path.read_bytes()
    return result


def _validate_zip(zip_path: Path, skill_root: Path) -> dict[str, object]:
    expected = _skill_files(skill_root)
    with zipfile.ZipFile(zip_path) as archive:
        bad_crc = archive.testzip()
        if bad_crc is not None:
            raise ValueError(f"ZIP CRC failure: {bad_crc}")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("ZIP contains duplicate paths")
        for name in names:
            if not _safe_member(name) or not name.startswith("auto-foundry-agentic-e2e/"):
                raise ValueError(f"unsafe or unexpected ZIP path: {name}")
            if name.endswith("/"):
                raise ValueError(f"ZIP contains directory entry: {name}")
            if any(token in name for token in ("__pycache__", ".pyc", ".pyo", ".DS_Store", ".git/")):
                raise ValueError(f"ZIP contains cache/metadata path: {name}")
        actual = {name.removeprefix("auto-foundry-agentic-e2e/"): archive.read(name) for name in names}
        if set(actual) != set(expected):
            raise ValueError(f"ZIP skill file set differs: missing={sorted(set(expected)-set(actual))}, extra={sorted(set(actual)-set(expected))}")
        missing_required = sorted(REQUIRED_SKILL_MEMBERS - set(actual))
        if missing_required:
            raise ValueError(f"ZIP required skill files missing: {missing_required}")
        stale_optimizer = sorted(DELETED_OPTIMIZER_MEMBERS & set(actual))
        if stale_optimizer:
            raise ValueError(f"ZIP contains deleted optimizer members: {stale_optimizer}")
        mismatches = [relative for relative in expected if _sha256_bytes(actual[relative]) != _sha256_bytes(expected[relative])]
        if mismatches:
            raise ValueError(f"ZIP per-file SHA mismatch: {mismatches}")
        skill_text = actual["SKILL.md"].decode("utf-8")
        if not skill_text.startswith("---\n"):
            raise ValueError("SKILL.md frontmatter missing")
        frontmatter = skill_text.split("---\n", 2)[1]
        required = ('name: auto-foundry-agentic-e2e', 'version: "0.8.0"', 'core_name: auto_foundry_core', 'core_version: "0.9.0"', f"release: {RELEASE_SLUG}")
        if any(marker not in frontmatter for marker in required):
            raise ValueError("SKILL.md frontmatter/version markers invalid")
        if f"release: {STALE_RELEASE_SLUG}" in frontmatter:
            raise ValueError("SKILL.md contains stale release slug")
        for marker in ("skill_version: 0.8.0", "core_version: 0.9.0"):
            if marker not in skill_text:
                raise ValueError(f"SKILL.md run marker missing: {marker}")
        return {
            "crc": "PASS",
            "one_top_level_root": True,
            "file_count": len(actual),
            "per_file_sha": "PASS",
            "frontmatter": "PASS",
            "zip_sha256": _sha256(zip_path),
        }


def _metadata(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            values.setdefault(key, value)
    return values


def _core_source_files(source_root: Path) -> dict[str, bytes]:
    """Return the complete source-file map expected in the core wheel."""

    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise ValueError(f"core source root is missing: {source_root}")
    result: dict[str, bytes] = {}
    for path in source_root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        relative = path.relative_to(source_root).as_posix()
        result[f"auto_foundry_core/{relative}"] = path.read_bytes()
    if not result:
        raise ValueError(f"core source root has no package files: {source_root}")
    return result


def _validate_wheel(wheel_path: Path, source_root: Path) -> dict[str, object]:
    expected_source = _core_source_files(source_root)
    with zipfile.ZipFile(wheel_path) as archive:
        if archive.testzip() is not None:
            raise ValueError("wheel CRC failure")
        names = archive.namelist()
        if any(not _safe_member(name) for name in names):
            raise ValueError("wheel contains unsafe path")
        actual_source = {
            name: archive.read(name)
            for name in names
            if name.startswith("auto_foundry_core/")
        }
        missing_source = sorted(set(expected_source) - set(actual_source))
        extra_source = sorted(set(actual_source) - set(expected_source))
        if missing_source or extra_source:
            raise ValueError(
                "wheel source mapping mismatch: "
                f"missing={missing_source}, extra={extra_source}"
            )
        mismatched_source = sorted(
            name for name, expected in expected_source.items() if actual_source[name] != expected
        )
        if mismatched_source:
            raise ValueError(f"wheel source byte mismatch: {mismatched_source}")
        metadata_name = next((name for name in names if name.endswith(".dist-info/METADATA")), None)
        if metadata_name is None:
            raise ValueError("wheel METADATA missing")
        metadata = _metadata(archive.read(metadata_name).decode("utf-8"))
        # PyPA distribution-name normalization: hyphens, underscores and periods
        # are equivalent. Keep the original name in the verification receipt.
        normalized_name = re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower()
        if normalized_name != "auto-foundry-core" or metadata.get("Version") != "0.9.0":
            raise ValueError(f"wheel metadata mismatch: {metadata.get('Name')} {metadata.get('Version')}")
        missing = sorted(REQUIRED_CORE_MODULES - set(names))
        if missing:
            raise ValueError(f"wheel package files missing: {missing}")
        return {
            "crc": "PASS",
            "metadata_name": metadata["Name"],
            "metadata_version": metadata["Version"],
            "package_files": "PASS",
            "source_mapping": "PASS",
            "source_file_count": len(expected_source),
            "wheel_sha256": _sha256(wheel_path),
        }


def _offline_install_smoke(wheel_path: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="auto-foundry-release-") as target:
        install_target = Path(target) / "site"
        install_target.mkdir()
        env = dict(os.environ)
        env["PIP_NO_INDEX"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(install_target)
        command = [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--target", str(install_target), str(wheel_path)]
        subprocess.run(command, check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        import_script = (
            "import auto_foundry_core as core; "
            "import auto_foundry_core.requirement_planning as requirement_planning; "
            "from auto_foundry_core import (AcceptedAnalysisBundle, AcceptedSnapshot, AgentInvocationReceipt, "
            "AnalystAnswer, AnalystBrief, AnalystSource, AnalystWorkspace, BusinessReviewAdapter, "
            "ArtifactProgress, BoundAnalysisContext, CatalogCounts, CatalogSnapshot, CoreExecutionResult, CoreRuntime, "
            "ControlledScriptRunner, DataRoom, DataRoomCatalogEntry, DataRoomMember, DataRoomWorkbench, "
            "DataInsufficiencyConclusion, EvidenceNote, ExecutionAttempt, FreezeMarkers, IntegrationFidelityPacket, "
            "IntegrationRecord, IntegrationSession, IntegrationValidation, "
            "InvocationReceiptLedger, ITEM_STATE_FIELDS, ITEM_STATE_SCHEMA, ItemWorkspace, LEMRef, "
            "LEMProjection, LivingEnterpriseModelProjector, PreparedAsset, PreparedAssetRegistry, ProductContractError, "
            "ProgressDecision, RequirementAnalysisPlan, RequirementAnalysisTask, ReviewFinding, RunContext, "
            "RunLifecycle, RunLifecycleSnapshot, RequirementExecutionGroup, RequirementExecutionPlan, RequirementSupervisorWorkspace, "
            "compact_catalog_payload, "
            "ScriptExecutionReceipt, ScriptRunReport, decode_freeze_markers, "
            "SpecialistMemo, SpecialistTask, load_bound_analysis_context); "
            "assert core.RequirementExecutionPlan is RequirementExecutionPlan; "
            "assert requirement_planning.RequirementExecutionPlan is RequirementExecutionPlan; "
            "assert callable(compact_catalog_payload); "
            "assert all(item is not None for item in (AcceptedAnalysisBundle, AcceptedSnapshot, AgentInvocationReceipt, "
            "AnalystAnswer, AnalystBrief, AnalystSource, AnalystWorkspace, BusinessReviewAdapter, ArtifactProgress, "
            "BoundAnalysisContext, CatalogCounts, CatalogSnapshot, CoreExecutionResult, CoreRuntime, "
            "ControlledScriptRunner, DataRoom, DataRoomCatalogEntry, DataRoomMember, DataRoomWorkbench, ExecutionAttempt, "
            "DataInsufficiencyConclusion, EvidenceNote, FreezeMarkers, IntegrationFidelityPacket, IntegrationRecord, "
            "IntegrationSession, IntegrationValidation, InvocationReceiptLedger, "
            "ITEM_STATE_FIELDS, ITEM_STATE_SCHEMA, ItemWorkspace, LEMRef, PreparedAsset, PreparedAssetRegistry, "
            "LEMProjection, LivingEnterpriseModelProjector, ProductContractError, ProgressDecision, RequirementAnalysisPlan, "
            "RequirementAnalysisTask, RequirementExecutionGroup, RequirementExecutionPlan, RequirementSupervisorWorkspace, ReviewFinding, RunContext, RunLifecycle, RunLifecycleSnapshot, SpecialistMemo, SpecialistTask, "
            "ScriptExecutionReceipt, ScriptRunReport, decode_freeze_markers, load_bound_analysis_context)); "
            "assert all(callable(getattr(AnalystWorkspace, name, None)) for name in ("
            "'brief', 'search_ontology', 'select_ontology', 'search_prepared_assets', "
            "'select_prepared_assets', 'load_prepared_asset')); "
            "assert set(ITEM_STATE_FIELDS[:8]) == {'item_id', 'mode', 'original_text', 'lifecycle_state', "
            "'execution_recovery_count', 'business_repair_count', 'created_at', 'updated_at'}; "
            "assert core.capability_catalog(); "
            "print(core.__version__)"
        )
        import_result = subprocess.run([sys.executable, "-c", import_script], check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        cli_result = subprocess.run([sys.executable, "-m", "auto_foundry_core", "catalog", "list"], check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if import_result.stdout.strip() != "0.9.0":
            raise ValueError(f"installed import version mismatch: {import_result.stdout!r}")
        if not cli_result.stdout.strip().startswith("["):
            raise ValueError("installed catalog CLI did not return JSON list")
    return {"offline_install": "PASS", "import": "PASS", "cli_catalog": "PASS", "target": "temporary"}


def validate_release(root: Path, dist: Path, zip_path: Path | None = None, wheel_path: Path | None = None) -> dict[str, object]:
    zip_path = zip_path or dist / ZIP_NAME
    if wheel_path is None:
        wheels = sorted(dist.glob("auto_foundry_core-0.9.0-*.whl"))
        if len(wheels) != 1:
            raise ValueError(f"expected one core wheel in {dist}, found {wheels}")
        wheel_path = wheels[0]
    zip_result = _validate_zip(zip_path, root / "skills" / "auto-foundry-agentic-e2e")
    wheel_result = _validate_wheel(wheel_path, root / "src" / "auto_foundry_core")
    install_result = _offline_install_smoke(wheel_path)
    return {"zip": zip_result, "wheel": wheel_result, "install": install_result, "network": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--zip", dest="zip_path", type=Path)
    parser.add_argument("--wheel", dest="wheel_path", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    dist = (args.dist or root / "dist").resolve()
    try:
        result = validate_release(root, dist, args.zip_path.resolve() if args.zip_path else None, args.wheel_path.resolve() if args.wheel_path else None)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"release validation: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
