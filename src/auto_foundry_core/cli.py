"""Small agent-facing CLI for catalog discovery and deterministic operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .capabilities import execute
from .catalog import capability_catalog, get_capability, search_capabilities
from .contracts import OperationSpec
from .workspace import require_allowed_roots, validate_allowed_path


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
    run.add_argument("--output", required=True, help="derived output directory")
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
    spec_path = Path(args.spec).expanduser().resolve()
    with spec_path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    payload["capability_id"] = args.capability_id
    if "allowed_roots" in payload:
        parameters = dict(payload.get("parameters") or {})
        parameters.setdefault("allowed_roots", payload.pop("allowed_roots"))
        payload["parameters"] = parameters
        payload["allowed_roots"] = parameters["allowed_roots"]
    spec = OperationSpec.from_dict(payload)
    declared_roots = require_allowed_roots(
        payload.get("allowed_roots") or payload.get("parameters", {}).get("allowed_roots"),
        context="CLI run",
    )
    raw_output = Path(args.output).expanduser().resolve()
    output_dir = validate_allowed_path(raw_output, declared_roots) if declared_roots else raw_output
    result_path = validate_allowed_path(output_dir / "result.json", declared_roots) if declared_roots else output_dir / "result.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    result = execute(spec, output_dir=str(output_dir))
    serialized = result.to_dict() if hasattr(result, "to_dict") else result
    # Every CLI run has a small stable result envelope in the requested output
    # directory; capability-specific writers may add their own derived files.
    result_path.write_text(json.dumps(serialized, sort_keys=True, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    _print(serialized)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
