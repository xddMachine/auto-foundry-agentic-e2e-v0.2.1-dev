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


ZIP_NAME = "auto-foundry-agentic-e2e-v0.2.1.zip"


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
        mismatches = [relative for relative in expected if _sha256_bytes(actual[relative]) != _sha256_bytes(expected[relative])]
        if mismatches:
            raise ValueError(f"ZIP per-file SHA mismatch: {mismatches}")
        skill_text = actual["SKILL.md"].decode("utf-8")
        if not skill_text.startswith("---\n"):
            raise ValueError("SKILL.md frontmatter missing")
        frontmatter = skill_text.split("---\n", 2)[1]
        required = ('name: auto-foundry-agentic-e2e', 'version: "0.2.1"', 'core_name: auto_foundry_core', 'core_version: "0.1.0"')
        if any(marker not in frontmatter for marker in required):
            raise ValueError("SKILL.md frontmatter/version markers invalid")
        for marker in ("skill_version: 0.2.1", "core_version: 0.1.0"):
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


def _validate_wheel(wheel_path: Path) -> dict[str, object]:
    with zipfile.ZipFile(wheel_path) as archive:
        if archive.testzip() is not None:
            raise ValueError("wheel CRC failure")
        names = archive.namelist()
        if any(not _safe_member(name) for name in names):
            raise ValueError("wheel contains unsafe path")
        metadata_name = next((name for name in names if name.endswith(".dist-info/METADATA")), None)
        if metadata_name is None:
            raise ValueError("wheel METADATA missing")
        metadata = _metadata(archive.read(metadata_name).decode("utf-8"))
        if metadata.get("Name") != "auto_foundry_core" or metadata.get("Version") != "0.1.0":
            raise ValueError(f"wheel metadata mismatch: {metadata.get('Name')} {metadata.get('Version')}")
        package_files = {"auto_foundry_core/__init__.py", "auto_foundry_core/cli.py", "auto_foundry_core/catalog.py"}
        missing = sorted(package_files - set(names))
        if missing:
            raise ValueError(f"wheel package files missing: {missing}")
        return {
            "crc": "PASS",
            "metadata_name": metadata["Name"],
            "metadata_version": metadata["Version"],
            "package_files": "PASS",
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
        import_result = subprocess.run([sys.executable, "-c", "import auto_foundry_core; print(auto_foundry_core.__version__)"], check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        cli_result = subprocess.run([sys.executable, "-m", "auto_foundry_core", "catalog", "list"], check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if import_result.stdout.strip() != "0.1.0":
            raise ValueError(f"installed import version mismatch: {import_result.stdout!r}")
        if not cli_result.stdout.strip().startswith("["):
            raise ValueError("installed catalog CLI did not return JSON list")
    return {"offline_install": "PASS", "import": "PASS", "cli_catalog": "PASS", "target": "temporary"}


def validate_release(root: Path, dist: Path, zip_path: Path | None = None, wheel_path: Path | None = None) -> dict[str, object]:
    zip_path = zip_path or dist / ZIP_NAME
    if wheel_path is None:
        wheels = sorted(dist.glob("auto_foundry_core-0.1.0-*.whl"))
        if len(wheels) != 1:
            raise ValueError(f"expected one core wheel in {dist}, found {wheels}")
        wheel_path = wheels[0]
    zip_result = _validate_zip(zip_path, root / "skills" / "auto-foundry-agentic-e2e")
    wheel_result = _validate_wheel(wheel_path)
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
