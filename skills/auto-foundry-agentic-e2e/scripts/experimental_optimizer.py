#!/usr/bin/env python3
"""Observe run-local Auto Foundry workflow evidence without mutating it.

This is a development-only helper.  It requires an explicit structured freeze
manifest proving that answers, the Living Enterprise Model, prepared assets,
the dashboard, and telemetry are each frozen. It writes exactly two files in
the caller-provided optimizer directory: ``experimental_optimizer_report.md``
and ``experimental_optimizer_evidence_appendix.md``.
The helper has no model, network, source, or business-process automation
capability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


OUTPUT_NAMES = (
    "experimental_optimizer_report.md",
    "experimental_optimizer_evidence_appendix.md",
)
CATEGORIES = (
    "repeated_code",
    "repeated_reads_context",
    "cache_misses",
    "reviewer_bottleneck",
    "capability_gaps",
)


class OptimizerPreconditionError(ValueError):
    """Raised when the frozen-products or read-only input contract is absent."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(paths: Iterable[Path]) -> list[Path]:
    result: set[Path] = set()
    for path in paths:
        if path.is_file():
            result.add(path.resolve())
        elif path.is_dir():
            result.update(candidate.resolve() for candidate in path.rglob("*") if candidate.is_file())
        else:
            raise FileNotFoundError(path)
    return sorted(result, key=lambda candidate: candidate.as_posix())


def _read_text(paths: Iterable[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, UnicodeError):
            continue
    return "\n".join(chunks)


def _truthy_frozen(manifest: Mapping[str, Any]) -> bool:
    """Require all five explicit, separately recorded freeze markers."""

    mappings: list[Mapping[str, Any]] = [manifest]
    for key in ("freeze", "preconditions", "product_freeze", "freeze_manifest", "frozen_products"):
        value = manifest.get(key)
        if isinstance(value, Mapping):
            mappings.append(value)
    aliases = {
        "answers_frozen": ("answers_frozen",),
        "lem_frozen": ("living_enterprise_model_frozen", "lem_frozen"),
        "prepared_frozen": ("prepared_assets_frozen", "prepared_data_registry_frozen"),
        "dashboard_frozen": ("dashboard_frozen",),
        "telemetry_frozen": ("telemetry_frozen",),
    }
    for names in aliases.values():
        if not any(any(mapping.get(name) is True for name in names) for mapping in mappings):
            return False
    return True


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
        raise OptimizerPreconditionError(f"invalid JSON input {path}: {exc}") from exc


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
            if isinstance(value, Mapping):
                records.append(value)
            else:
                records.append({"line": line_number, "value": value})
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
class Evidence:
    category: str
    observed: str
    references: tuple[str, ...]
    hypothesis: str
    recommendation: str
    benefit: str
    risk: str
    generality: str
    classification: str


def _detect(
    category: str,
    scripts: list[Path],
    traces: list[Path],
    telemetry: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> Evidence:
    trace_text = _read_text(traces).lower()
    script_hashes: dict[str, list[Path]] = {}
    for path in scripts:
        script_hashes.setdefault(_sha256(path), []).append(path)
    duplicate_groups = [group for group in script_hashes.values() if len(group) > 1]
    event_text = " ".join(" ".join(_flatten_strings(record)) for record in telemetry).lower()
    refs: list[str] = []
    if category == "repeated_code":
        if duplicate_groups:
            names = [", ".join(path.name for path in group) for group in duplicate_groups]
            observed = f"{len(duplicate_groups)} exact duplicate script group(s): " + "; ".join(names)
            refs = [str(path) for group in duplicate_groups for path in group]
            hypothesis = "Repeated deterministic code may be a candidate for a shared reviewed helper."
            recommendation = "Compare duplicate scripts at the next design review; extract only when inputs, limits, and evidence contracts are identical."
            classification = "mechanical_now"
        else:
            observed = f"No exact duplicate script files observed across {len(scripts)} supplied script file(s)."
            hypothesis = "Repeated code is not evidenced by exact file hashes in this run."
            recommendation = "Keep code local until a repeated implementation is observed in additional runs."
            classification = "deterministic_after_more_runs"
        benefit = "Lower maintenance cost if identical evidence-bound logic is safely shared; otherwise unknown."
        risk = "Premature extraction could broaden evidence scope or hide question-specific limits."
        generality = "Requires repetition in another run with the same contract; not a general client automation recommendation."
    elif category == "repeated_reads_context":
        hits = [path for path in traces if re.search(r"repeated|re-read|re_read|context|bundle|read", path.read_text(encoding="utf-8", errors="replace"), re.I)]
        if hits or re.search(r"repeated|re-read|re_read|context", event_text):
            observed = f"Read/context repetition terms appear in {len(hits)} trace file(s) or telemetry record(s)."
            refs = [str(path) for path in hits]
            hypothesis = "Repeated reads or oversized context bundles may contribute to avoidable workflow effort."
            recommendation = "Add bounded read/bundle counters to future passive telemetry and review exact-ID bundle sizes before changing routing."
            classification = "deterministic_after_more_runs"
        else:
            observed = "No repeated-read or repeated-context evidence was found in supplied traces or telemetry."
            hypothesis = "The absence may reflect sparse telemetry rather than absence of repetition."
            recommendation = "Keep agentic routing and collect passive counters in a later run."
            classification = "keep_agentic"
        benefit = "Better evidence for context and read-budget decisions; no benefit is assumed before counters exist."
        risk = "A generic cache or bundling change could alter evidence selection and reproducibility."
        generality = "Potentially reusable for Auto Foundry runs only; never a client process automation claim."
    elif category == "cache_misses":
        hits = [record for record in telemetry if any(re.search(r"cache.?miss|miss", token, re.I) for token in _flatten_strings(record))]
        if hits or re.search(r"cache.?miss", trace_text):
            observed = f"{len(hits)} telemetry record(s) or trace mention(s) indicate cache misses."
            refs = [f"telemetry:{index}" for index, _ in enumerate(hits)]
            hypothesis = "Misses may indicate reusable run-local preparation was not available or not selected."
            recommendation = "Inspect cache keys, scope, and clean-room boundaries in a future run; do not reuse prior-run state implicitly."
            classification = "deterministic_after_more_runs"
        else:
            observed = "No cache-miss evidence was supplied."
            hypothesis = "Cache behavior is unknown under the supplied telemetry."
            recommendation = "Do not introduce a cache based on this run; retain explicit clean-room semantics."
            classification = "keep_agentic"
        benefit = "May reduce repeated preparation while preserving explicit scope and provenance."
        risk = "Cross-run reuse could contaminate a clean-room run or invalidate evidence lineage."
        generality = "Only run-local substrate behavior is in scope."
    elif category == "reviewer_bottleneck":
        unavailable = [record for record in telemetry if any(re.search(r"review.*unavailable|reviewer.*unavailable|host thread|bottleneck", token, re.I) for token in _flatten_strings(record))]
        routing = manifest.get("review_routing")
        routing_unavailable = isinstance(routing, Mapping) and routing.get("fresh_sol_review_available") is False
        if unavailable or routing_unavailable or re.search(r"reviewer.*unavailable|host thread", trace_text):
            observed = f"Reviewer availability limitation observed ({len(unavailable)} matching telemetry record(s)); manifest routing unavailable={routing_unavailable}."
            refs = [f"telemetry:{index}" for index, _ in enumerate(unavailable)] + (["manifest:review_routing"] if routing_unavailable else [])
            hypothesis = "Review capacity can become a throughput bottleneck at the product or item boundary."
            recommendation = "Instrument review queue depth and preserve explicit unavailable disclosures; add capacity only through an approved independent route."
            classification = "mechanical_now"
        else:
            observed = "No reviewer bottleneck evidence was supplied."
            hypothesis = "Reviewer throughput is unknown under this fixture."
            recommendation = "Keep the existing one-review boundary and collect passive routing outcomes."
            classification = "keep_agentic"
        benefit = "Makes review-capacity limits visible without weakening independent review."
        risk = "Automatically bypassing review would reduce assurance and is not recommended."
        generality = "Applies to Auto Foundry review routing, not client approval automation."
    elif category == "capability_gaps":
        hits = [record for record in telemetry if any(re.search(r"capability.?gap|unsupported capability|missing capability", token, re.I) for token in _flatten_strings(record))]
        if hits or re.search(r"capability.?gap|unsupported capability|missing capability", trace_text):
            observed = f"{len(hits)} telemetry record(s) or trace mention(s) indicate a capability gap."
            refs = [f"telemetry:{index}" for index, _ in enumerate(hits)]
            hypothesis = "A missing catalog capability may have forced custom work or limited the supported answer."
            recommendation = "Record the exact requested operation and evidence contract, then evaluate a bounded catalog addition after another reviewed example."
            classification = "deterministic_after_more_runs"
        else:
            observed = "No capability-gap evidence was supplied."
            hypothesis = "Catalog fit is not evidenced as a bottleneck in this fixture."
            recommendation = "Continue catalog-first selection and record a gap only when a needed operation is actually missing."
            classification = "keep_agentic"
        benefit = "May make repeated deterministic operations discoverable and reproducible."
        risk = "A broad capability could overfit one question or silently change analytical semantics."
        generality = "Requires a stable operation contract and repeated evidence across runs."
    else:  # pragma: no cover - guarded by CATEGORIES
        raise AssertionError(category)
    return Evidence(category, observed, tuple(sorted(set(refs))), hypothesis, recommendation, benefit, risk, generality, classification)


def _input_hashes(paths: Iterable[Path]) -> dict[str, str]:
    return {str(path): _sha256(path) for path in _files(paths)}


def _markdown(report: list[Evidence], hashes: Mapping[str, str], manifest_path: Path) -> str:
    lines = [
        "# Experimental optimizer report",
        "",
        "Development-only, deterministic, offline observation of Auto Foundry workflow/substrate evidence.",
        "",
        "## Preconditions and boundary",
        "",
        "- Explicit frozen-products manifest precondition: PASS.",
        "- Analytical/model calls: none; no network access; no source or product mutation.",
        "- This report does not automate a client's business process and cannot edit core, skill, scripts, answers, LEM, prepared data, dashboard, or telemetry.",
        f"- Frozen manifest: `{manifest_path}`.",
        "- Input hashes were captured before and after observation; see the evidence appendix.",
        "",
        "## Evidence categories and recommendations",
        "",
    ]
    for item in report:
        lines.extend(
            [
                f"### {item.category}",
                "",
                f"- **Observed evidence:** {item.observed}",
                f"- **Hypothesis:** {item.hypothesis}",
                f"- **Recommendation:** {item.recommendation}",
                f"- **Expected benefit:** {item.benefit}",
                f"- **Risk:** {item.risk}",
                f"- **Generality:** {item.generality}",
                f"- **Classification:** `{item.classification}`",
                f"- **Evidence refs:** {', '.join(f'`{ref}`' for ref in item.references) if item.references else 'none supplied'}",
                "",
            ]
        )
    lines.extend(
        [
            "## Decision",
            "",
            "Recommendations remain evidence-bound hypotheses or bounded mechanical follow-ups. Nothing is auto-promoted, installed, or applied to a product runtime.",
            "",
        ]
    )
    return "\n".join(lines)


def _appendix(before: Mapping[str, str], after: Mapping[str, str]) -> str:
    lines = [
        "# Optimizer evidence appendix",
        "",
        "## Analytical input hashes",
        "",
        "| Path | Before SHA-256 | After SHA-256 | Unchanged |",
        "|---|---|---|---|",
    ]
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path, "missing"), after.get(path, "missing")
        lines.append(f"| `{path}` | `{old}` | `{new}` | {'yes' if old == new else 'NO'} |")
    lines.extend(
        [
            "",
            "## Read-only evidence inventory",
            "",
            f"- Files hashed before observation: {len(before)}.",
            f"- Files hashed after observation: {len(after)}.",
            f"- All analytical inputs unchanged: {'yes' if before == after else 'NO'}.",
        "- Outputs intentionally limited to `experimental_optimizer_report.md` and `experimental_optimizer_evidence_appendix.md`.",
            "",
        ]
    )
    return "\n".join(lines)


def run_optimizer(
    *,
    products_manifest: Path,
    optimizer_dir: Path,
    telemetry: Iterable[Path] = (),
    traces: Iterable[Path] = (),
    scripts: Iterable[Path] = (),
    analytical_inputs: Iterable[Path] = (),
) -> dict[str, Any]:
    """Run a read-only observation and write exactly the two report outputs."""

    products_manifest = products_manifest.resolve()
    manifest = _load_json(products_manifest)
    if not isinstance(manifest, Mapping):
        raise OptimizerPreconditionError("products manifest must be a JSON object")
    if not _truthy_frozen(manifest):
        raise OptimizerPreconditionError("explicit frozen-products manifest/precondition is required")
    forbidden = _forbidden_classification(manifest)
    if forbidden:
        raise OptimizerPreconditionError(f"client-business-automation classification rejected: {forbidden}")

    telemetry_paths = _files(telemetry)
    trace_paths = _files(traces)
    script_paths = _files(scripts)
    analytical_paths = _files([products_manifest, *analytical_inputs, *telemetry_paths, *trace_paths, *script_paths])
    before = _input_hashes(analytical_paths)
    telemetry_records = _telemetry_records(telemetry_paths)
    for path in [*trace_paths, *script_paths, *telemetry_paths]:
        text = path.read_text(encoding="utf-8", errors="replace")
        if _TEXT_CLASSIFICATION_RE.search(text):
            raise OptimizerPreconditionError(f"client-business-automation classification rejected in {path}")
        if path.suffix.lower() in {".json", ".jsonl"}:
            try:
                parsed = _load_json(path) if path.suffix.lower() == ".json" else None
            except OptimizerPreconditionError:
                parsed = None
            if parsed is not None:
                forbidden = _forbidden_classification(parsed)
                if forbidden:
                    raise OptimizerPreconditionError(f"client-business-automation classification rejected: {forbidden}")
    observations = [_detect(category, script_paths, trace_paths, telemetry_records, manifest) for category in CATEGORIES]
    after = _input_hashes(analytical_paths)
    if before != after:
        raise OptimizerPreconditionError("analytical input changed during read-only observation")

    optimizer_dir = optimizer_dir.resolve()
    optimizer_dir.mkdir(parents=True, exist_ok=True)
    report_path = optimizer_dir / OUTPUT_NAMES[0]
    appendix_path = optimizer_dir / OUTPUT_NAMES[1]
    report_path.write_text(_markdown(observations, before, products_manifest).rstrip() + "\n", encoding="utf-8")
    appendix_path.write_text(_appendix(before, after).rstrip() + "\n", encoding="utf-8")
    # Guard the promised output boundary.  Existing unrelated files are not
    # touched, but the helper never creates any additional file.
    return {
        "report": str(report_path),
        "experimental_optimizer_evidence_appendix": str(appendix_path),
        "input_hashes_unchanged": before == after,
        "categories": list(CATEGORIES),
        "output_names": list(OUTPUT_NAMES),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products-manifest", type=Path, required=True)
    parser.add_argument("--optimizer-dir", type=Path, required=True)
    parser.add_argument("--telemetry", type=Path, action="append", default=[], help="JSONL telemetry file or directory")
    parser.add_argument("--traces", type=Path, action="append", default=[], help="run-local trace file or directory")
    parser.add_argument("--scripts", type=Path, action="append", default=[], help="run-local script file or directory")
    parser.add_argument("--analytical-input", type=Path, action="append", default=[], help="additional hashed analytical input")
    args = parser.parse_args(argv)
    try:
        result = run_optimizer(
            products_manifest=args.products_manifest,
            optimizer_dir=args.optimizer_dir,
            telemetry=args.telemetry,
            traces=args.traces,
            scripts=args.scripts,
            analytical_inputs=args.analytical_input,
        )
    except (OSError, OptimizerPreconditionError, ValueError) as exc:
        print(f"experimental optimizer: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
