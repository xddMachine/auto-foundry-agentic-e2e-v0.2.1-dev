#!/usr/bin/env python3
"""Collect deterministic, run-local evidence for a later optimizer agent.

This development-only helper is deliberately a collector, not a reasoning
agent.  It reads one frozen run, hashes the evidence before and after the
scan, records exact duplicate files, and summarizes passive cache/read/review
and capability facts.  It writes only the bounded evidence bundle and hash
appendix below ``run_root/optimizer``.  A separate fresh Optimization Agent
may later use that bundle to write a free-form report; no model call occurs
here.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping

try:
    from auto_foundry_core.workspace import AllowedRootError, RunContext
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    _SRC = Path(__file__).resolve().parents[3] / "src"
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    from auto_foundry_core.workspace import AllowedRootError, RunContext


OUTPUT_NAMES = (
    "optimizer_evidence_bundle.md",
    "optimizer_evidence_appendix.md",
)
CATEGORIES = (
    "repeated_code",
    "repeated_reads_context",
    "cache_misses",
    "reviewer_bottleneck",
    "capability_gaps",
)


class EvidenceCollectorPreconditionError(ValueError):
    """Raised when the run-local frozen evidence contract is absent."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_path(context: RunContext, value: str | Path) -> Path:
    """Resolve one evidence input under the current run before probing it."""

    return context.resolve_run_path(value)


def _files(context: RunContext, paths: Iterable[str | Path]) -> list[Path]:
    """Expand run-relative files/directories after validating each path.

    Resolution happens before ``exists``/``is_file`` and before recursive
    traversal.  This means a symlink to a sibling run or host path is rejected
    at the context boundary rather than being silently scanned.
    """

    result: set[Path] = set()
    for raw in paths:
        path = _run_path(context, raw)
        if path.is_file():
            result.add(path)
            continue
        if path.is_dir():
            for candidate in path.rglob("*"):
                resolved = _run_path(context, candidate)
                if resolved.is_file():
                    result.add(resolved)
            continue
        raise FileNotFoundError(path)
    return sorted(result, key=lambda candidate: candidate.as_posix())


def _relative(context: RunContext, path: Path) -> str:
    try:
        return path.relative_to(context.run_root).as_posix()
    except ValueError:
        # All collector inputs are run-bound.  Keep this defensive branch
        # explicit so an accidental future caller cannot leak an arbitrary
        # host path into a report.
        return "<outside-current-run>"


def _read_text(paths: Iterable[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, UnicodeError):
            continue
    return "\n".join(chunks)


def _truthy_frozen(manifest: Mapping[str, Any]) -> bool:
    """Require each separately recorded freeze marker."""

    mappings: list[Mapping[str, Any]] = [manifest]
    for key in ("freeze", "preconditions", "product_freeze", "freeze_manifest", "frozen_products"):
        value = manifest.get(key)
        if isinstance(value, Mapping):
            mappings.append(value)
    aliases = (
        ("answers_frozen",),
        ("living_enterprise_model_frozen", "lem_frozen"),
        ("prepared_assets_frozen", "prepared_data_registry_frozen"),
        ("dashboard_frozen",),
        ("telemetry_frozen",),
    )
    return all(
        any(any(mapping.get(name) is True for name in names) for mapping in mappings)
        for names in aliases
    )


def _forbidden_classification(value: Any, key: str = "") -> str | None:
    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            hit = _forbidden_classification(child_value, str(child_key))
            if hit:
                return hit
        return None
    if isinstance(value, list):
        for child in value:
            hit = _forbidden_classification(child, key)
            if hit:
                return hit
        return None
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    key_normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if any(token in key_normalized for token in ("classification", "automation", "candidate_class", "scope_class")) or key_normalized in {"scope", "automation_scope", "candidate_type"}:
        if normalized in {
            "client_business_automation",
            "client_business_process_automation",
            "client_automation",
            "business_process_automation",
        }:
            return value
    return None


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceCollectorPreconditionError(f"invalid JSON input {_relative_for_error(path)}: {exc}") from exc


def _relative_for_error(path: Path) -> str:
    # Error strings are intentionally path-neutral; the caller gets the
    # technical exception while reports never expose arbitrary host paths.
    return path.name


_TEXT_CLASSIFICATION_RE = re.compile(
    r"(?is)(?:classification|candidate[_ -]?class|automation[_ -]?classification|automation[_ -]?scope|scope)"
    r"\s*[:=]\s*[`\"']?"
    r"(?:client[_ -]?business[_ -]?automation|client[_ -]?business[_ -]?process[_ -]?automation|client[_ -]?automation)\b"
)


def _telemetry_records(paths: Iterable[Path]) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for path in paths:
        if path.suffix.lower() != ".jsonl":
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(value if isinstance(value, Mapping) else {"line": line_number, "value": value})
    return records


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        result: list[str] = []
        for key, child in value.items():
            result.append(str(key))
            result.extend(_flatten_strings(child))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for child in value:
            result.extend(_flatten_strings(child))
        return result
    return [str(value)] if isinstance(value, str) else []


@dataclass(frozen=True)
class EvidenceFact:
    category: str
    observed: str
    references: tuple[str, ...] = ()


def _detect(
    context: RunContext,
    category: str,
    scripts: list[Path],
    traces: list[Path],
    telemetry: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> EvidenceFact:
    trace_text = _read_text(traces).lower()
    script_hashes: dict[str, list[Path]] = {}
    for path in scripts:
        script_hashes.setdefault(_sha256(path), []).append(path)
    duplicate_groups = [group for group in script_hashes.values() if len(group) > 1]
    event_text = " ".join(" ".join(_flatten_strings(record)) for record in telemetry).lower()
    refs: list[str] = []
    if category == "repeated_code":
        if duplicate_groups:
            observed = f"{len(duplicate_groups)} exact duplicate script group(s) across {len(scripts)} script file(s)."
            refs = [_relative(context, path) for group in duplicate_groups for path in group]
        else:
            observed = f"No exact duplicate script files across {len(scripts)} supplied script file(s)."
    elif category == "repeated_reads_context":
        hits = [path for path in traces if re.search(r"repeated|re-read|re_read|context|bundle|read", path.read_text(encoding="utf-8", errors="replace"), re.I)]
        if hits or re.search(r"repeated|re-read|re_read|context", event_text):
            observed = f"Read/context repetition terms appear in {len(hits)} trace file(s) or telemetry record(s)."
            refs = [_relative(context, path) for path in hits]
        else:
            observed = "No repeated-read or repeated-context evidence was found in supplied traces or telemetry."
    elif category == "cache_misses":
        hits = [record for record in telemetry if any(re.search(r"cache.?miss|miss", token, re.I) for token in _flatten_strings(record))]
        if hits or re.search(r"cache.?miss", trace_text):
            observed = f"{len(hits)} telemetry record(s) or trace mention(s) indicate cache misses."
            refs = [f"telemetry:{index}" for index, _ in enumerate(hits)]
        else:
            observed = "No cache-miss evidence was supplied."
    elif category == "reviewer_bottleneck":
        unavailable = [record for record in telemetry if any(re.search(r"review.*unavailable|reviewer.*unavailable|host thread|bottleneck", token, re.I) for token in _flatten_strings(record))]
        routing = manifest.get("review_routing")
        routing_unavailable = isinstance(routing, Mapping) and routing.get("fresh_sol_review_available") is False
        if unavailable or routing_unavailable or re.search(r"reviewer.*unavailable|host thread", trace_text):
            observed = f"Reviewer availability limitation observed ({len(unavailable)} matching telemetry record(s)); routing unavailable={routing_unavailable}."
            refs = [f"telemetry:{index}" for index, _ in enumerate(unavailable)] + (["manifest:review_routing"] if routing_unavailable else [])
        else:
            observed = "No reviewer bottleneck evidence was supplied."
    elif category == "capability_gaps":
        hits = [record for record in telemetry if any(re.search(r"capability.?gap|unsupported capability|missing capability", token, re.I) for token in _flatten_strings(record))]
        if hits or re.search(r"capability.?gap|unsupported capability|missing capability", trace_text):
            observed = f"{len(hits)} telemetry record(s) or trace mention(s) indicate a capability gap."
            refs = [f"telemetry:{index}" for index, _ in enumerate(hits)]
        else:
            observed = "No capability-gap evidence was supplied."
    else:  # pragma: no cover - guarded by CATEGORIES
        raise AssertionError(category)
    return EvidenceFact(category, observed, tuple(sorted(set(refs))))


def _input_hashes(context: RunContext, paths: Iterable[Path]) -> dict[str, str]:
    return {_relative(context, path): _sha256(path) for path in _files(context, paths)}


def _duplicate_groups(context: RunContext, paths: Iterable[Path]) -> list[list[str]]:
    by_hash: dict[str, list[Path]] = {}
    for path in _files(context, paths):
        by_hash.setdefault(_sha256(path), []).append(path)
    return [sorted((_relative(context, path) for path in group)) for group in by_hash.values() if len(group) > 1]


def _markdown(
    facts: list[EvidenceFact],
    hashes: Mapping[str, str],
    manifest_path: str,
    duplicate_groups: list[list[str]],
) -> str:
    lines = [
        "# Optimizer evidence bundle",
        "",
        "Development-only deterministic observation of one frozen Auto Foundry run.",
        "",
        "## Boundary",
        "",
        "- Frozen run precondition: PASS (all five product/telemetry markers were true).",
        "- Analytical/model calls: none; no network access; no source or product mutation.",
        "- This file is an evidence bundle, not a free-form optimization report and not a client-business automation proposal.",
        "- A separate fresh Optimization Agent may reason from this bundle after collection.",
        f"- Frozen manifest: `{manifest_path}`.",
        f"- Analytical files hashed: {len(hashes)}; exact duplicate groups: {len(duplicate_groups)}.",
        "",
        "## Observed evidence",
        "",
    ]
    for fact in facts:
        refs = ", ".join(f"`{ref}`" for ref in fact.references) if fact.references else "none supplied"
        lines.extend([
            f"### {fact.category}",
            "",
            f"- **Observed evidence:** {fact.observed}",
            f"- **Evidence refs:** {refs}",
            "",
        ])
    lines.extend([
        "## Collection result",
        "",
        "- Input hashes are recorded in the appendix and were unchanged after collection.",
        "- No recommendation, code change, promotion, or route decision is produced by this deterministic collector.",
        "",
    ])
    return "\n".join(lines)


def _appendix(
    before: Mapping[str, str],
    after: Mapping[str, str],
    duplicate_groups: list[list[str]],
) -> str:
    lines = [
        "# Optimizer evidence appendix",
        "",
        "## Analytical input hashes",
        "",
        "| Run-relative path | Before SHA-256 | After SHA-256 | Unchanged |",
        "|---|---|---|---|",
    ]
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path, "missing"), after.get(path, "missing")
        lines.append(f"| `{path}` | `{old}` | `{new}` | {'yes' if old == new else 'NO'} |")
    lines.extend(["", "## Exact duplicate files", ""])
    if duplicate_groups:
        lines.extend(f"- {', '.join(f'`{path}`' for path in group)}" for group in duplicate_groups)
    else:
        lines.append("- none observed")
    lines.extend([
        "",
        "## Read-only collection facts",
        "",
        f"- Files hashed before collection: {len(before)}.",
        f"- Files hashed after collection: {len(after)}.",
        f"- All analytical inputs unchanged: {'yes' if before == after else 'NO'}.",
        "- Outputs are limited to `optimizer_evidence_bundle.md` and `optimizer_evidence_appendix.md`.",
        "",
    ])
    return "\n".join(lines)


def collect_evidence(
    context: RunContext,
    *,
    products_manifest: str | Path,
    telemetry: Iterable[str | Path] = (),
    traces: Iterable[str | Path] = (),
    scripts: Iterable[str | Path] = (),
    analytical_inputs: Iterable[str | Path] = (),
    analytical_complete: bool = True,
) -> dict[str, Any]:
    """Collect one bounded deterministic evidence bundle.

    All inputs are run-relative and all outputs are fixed under the current
    context's optimizer root.  Precondition/path/hash failures are surfaced to
    the non-blocking wrapper below rather than being presented as data claims.
    """

    if not isinstance(context, RunContext):
        raise TypeError("collect_evidence requires one RunContext")
    # Resolve every boundary before any file read or output directory creation.
    manifest_path = _run_path(context, products_manifest)
    optimizer_root = context.resolve_optimizer_path("")
    if optimizer_root != context.run_root / "optimizer":
        raise AllowedRootError("optimizer root must be the current run's optimizer directory")
    telemetry_paths = _files(context, telemetry)
    trace_paths = _files(context, traces)
    script_paths = _files(context, scripts)
    additional_paths = _files(context, analytical_inputs)
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, Mapping):
        raise EvidenceCollectorPreconditionError("products manifest must be a JSON object")
    if not _truthy_frozen(manifest):
        raise EvidenceCollectorPreconditionError("all five frozen-run markers are required")
    forbidden = _forbidden_classification(manifest)
    if forbidden:
        raise EvidenceCollectorPreconditionError(f"client-business-automation classification rejected: {forbidden}")

    analytical_paths = _files(context, [manifest_path, *additional_paths, *telemetry_paths, *trace_paths, *script_paths])
    before = _input_hashes(context, analytical_paths)
    telemetry_records = _telemetry_records(telemetry_paths)
    for path in [*trace_paths, *script_paths, *telemetry_paths]:
        text = path.read_text(encoding="utf-8", errors="replace")
        if _TEXT_CLASSIFICATION_RE.search(text):
            raise EvidenceCollectorPreconditionError(f"client-business-automation classification rejected in {path.name}")
        if path.suffix.lower() == ".json":
            parsed = _load_json(path)
            forbidden = _forbidden_classification(parsed)
            if forbidden:
                raise EvidenceCollectorPreconditionError(f"client-business-automation classification rejected: {forbidden}")
    facts = [_detect(context, category, script_paths, trace_paths, telemetry_records, manifest) for category in CATEGORIES]
    duplicate_groups = _duplicate_groups(context, script_paths)
    after = _input_hashes(context, analytical_paths)
    if before != after:
        raise EvidenceCollectorPreconditionError("analytical input changed during read-only collection")

    # Only now create/write the fixed optimizer outputs; both paths were
    # resolved and validated before any filesystem mutation.
    optimizer_root.mkdir(parents=True, exist_ok=True)
    bundle_path = context.resolve_optimizer_path(OUTPUT_NAMES[0])
    appendix_path = context.resolve_optimizer_path(OUTPUT_NAMES[1])
    bundle_path.write_text(
        _markdown(facts, before, _relative(context, manifest_path), duplicate_groups).rstrip() + "\n",
        encoding="utf-8",
    )
    appendix_path.write_text(_appendix(before, after, duplicate_groups).rstrip() + "\n", encoding="utf-8")
    return {
        "optimizer_status": "complete",
        "analytical_complete": bool(analytical_complete),
        "evidence_bundle": _relative(context, bundle_path),
        "evidence_appendix": _relative(context, appendix_path),
        "output_names": list(OUTPUT_NAMES),
        "input_hashes_unchanged": True,
        "categories": list(CATEGORIES),
        "exact_duplicate_groups": duplicate_groups,
        "input_hashes": dict(before),
    }


def collect_evidence_non_blocking(
    context: RunContext,
    *,
    products_manifest: str | Path,
    telemetry: Iterable[str | Path] = (),
    traces: Iterable[str | Path] = (),
    scripts: Iterable[str | Path] = (),
    analytical_inputs: Iterable[str | Path] = (),
    analytical_complete: bool = True,
) -> dict[str, Any]:
    """Return a technical failure result without invalidating the run."""

    try:
        return collect_evidence(
            context,
            products_manifest=products_manifest,
            telemetry=telemetry,
            traces=traces,
            scripts=scripts,
            analytical_inputs=analytical_inputs,
            analytical_complete=analytical_complete,
        )
    except Exception as exc:  # collector is development-only and non-blocking
        return {
            "optimizer_status": "technical_failure",
            "analytical_complete": bool(analytical_complete),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "output_names": list(OUTPUT_NAMES),
            "input_hashes_unchanged": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True, help="current run root")
    parser.add_argument("--run-id", required=True, help="simple current run identifier")
    parser.add_argument("--products-manifest", required=True, help="run-relative frozen products manifest")
    parser.add_argument("--telemetry", action="append", default=[], help="run-relative JSONL telemetry file or directory")
    parser.add_argument("--traces", action="append", default=[], help="run-relative trace file or directory")
    parser.add_argument("--scripts", action="append", default=[], help="run-relative script file or directory")
    parser.add_argument("--analytical-input", action="append", default=[], help="additional run-relative hashed evidence input")
    args = parser.parse_args(argv)
    try:
        context = RunContext(run_id=args.run_id, run_root=args.run_root)
    except (OSError, ValueError) as exc:
        print(json.dumps({"optimizer_status": "technical_failure", "analytical_complete": True, "error_type": type(exc).__name__, "error": str(exc)}, sort_keys=True))
        return 0
    result = collect_evidence_non_blocking(
        context,
        products_manifest=args.products_manifest,
        telemetry=args.telemetry,
        traces=args.traces,
        scripts=args.scripts,
        analytical_inputs=args.analytical_input,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
