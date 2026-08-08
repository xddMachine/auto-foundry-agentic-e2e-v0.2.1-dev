"""Small agent-facing CLI for catalog discovery and deterministic operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .catalog import capability_catalog, get_capability, search_capabilities
from .contracts import OperationSpec
from .runtime import CoreRuntime
from .workspace import RunContext


def _print(value: Any) -> None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto_foundry_core", description="Deterministic local analytics core")
    sub = parser.add_subparsers(dest="command", required=True)
    catalog = sub.add_parser("catalog", help="discover deterministic capabilities")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_sub.add_parser("list")
    search = catalog_sub.add_parser("search")
    search.add_argument("text")
    describe = catalog_sub.add_parser("describe")
    describe.add_argument("capability_id")
    run = sub.add_parser("run", help="execute one deterministic capability")
    run.add_argument("capability_id")
    run.add_argument("--spec", required=True, help="JSON operation specification path")
    run.add_argument("--run-root", required=True, help="current run directory")
    run.add_argument("--input-root", action="append", default=[], help="source input root (repeatable)")
    run.add_argument("--run-id", help="optional simple run identifier (defaults to the run-root name)")
    run.add_argument("--output", help="optional run-relative result directory (defaults to products)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "catalog":
        if args.catalog_command == "list":
            _print([descriptor.to_dict() for descriptor in capability_catalog()])
            return 0
        if args.catalog_command == "search":
            _print([descriptor.to_dict() for descriptor in search_capabilities(args.text)])
            return 0
        _print(get_capability(args.capability_id))
        return 0
    run_root = Path(args.run_root).expanduser().resolve(strict=False)
    run_id = args.run_id or run_root.name or "run"
    context = RunContext(run_id=run_id, run_root=run_root, input_roots=tuple(args.input_root))
    # Validate both the control-plane spec and the result destination before
    # opening, probing, or creating anything.  The JSON spec cannot broaden
    # this context.
    spec_path = context.resolve_input(args.spec)
    if args.output:
        output_dir = context.resolve_product_path(args.output)
    else:
        output_dir = context.resolve_product_path("")
    result_path = context.resolve_run_path(output_dir / "result.json")
    with spec_path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    payload["capability_id"] = args.capability_id
    parameters = dict(payload.get("parameters") or {})
    # Legacy embedded root declarations are intentionally ignored: the run
    # context is the sole normal-path boundary.
    parameters.pop("allowed_roots", None)
    payload["parameters"] = parameters
    payload.pop("allowed_roots", None)
    spec = OperationSpec.from_dict(payload)
    execution = CoreRuntime(context).execute(spec)
    value = execution.value.to_dict() if hasattr(execution.value, "to_dict") else execution.value
    serialized = {
        "value": value,
        "receipt": execution.receipt.to_dict(),
        "cache_status": execution.cache_status,
    }
    # Every CLI run has a small stable result envelope in the requested output
    # directory; capability-specific writers may add their own derived files.
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(serialized, sort_keys=True, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    _print(serialized)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
