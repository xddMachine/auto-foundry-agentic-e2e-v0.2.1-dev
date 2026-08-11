#!/usr/bin/env python3
"""Build deterministic local skill ZIP and offline core wheel artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ZIP_NAME = "auto-foundry-agentic-e2e-v0.2.7.zip"
REQUIRED_SKILL_FILES = (
    "SKILL.md",
    "README.md",
    "scripts/dashboard_renderer.py",
    "scripts/optimizer_evidence_collector.py",
    "references/FINAL_PRODUCT_AND_AUTOMATION.md",
    "assets/REQUIREMENT_RECORD_TEMPLATE.json",
    "assets/ITEM_STATE_TEMPLATE.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skill_files(skill_root: Path) -> list[Path]:
    files = []
    for path in skill_root.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.name in {".DS_Store"}:
            continue
        files.append(path)
    return sorted(files, key=lambda path: path.relative_to(skill_root).as_posix())


def _write_deterministic_zip(skill_root: Path, destination: Path) -> dict[str, object]:
    files = _skill_files(skill_root)
    relative_files = {path.relative_to(skill_root).as_posix() for path in files}
    missing = sorted(set(REQUIRED_SKILL_FILES) - relative_files)
    if missing:
        raise RuntimeError(f"required skill files missing: {missing}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(skill_root).as_posix()
            name = f"auto-foundry-agentic-e2e/{relative}"
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return {"path": str(destination), "sha256": _sha256(destination), "file_count": len(files)}


def _cleanup_build_outputs(root: Path) -> None:
    # These are setuptools-generated and ignored; authored files are never
    # removed.  Keep the worktree clean after a local packaging check.
    for path in (root / "build", root / "src" / "auto_foundry_core.egg-info", root / "auto_foundry_core.egg-info"):
        if path.is_dir():
            shutil.rmtree(path)


def _build_wheel(root: Path, dist: Path) -> Path:
    before = set(dist.glob("auto_foundry_core-0.3.4-*.whl"))
    command = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        ".",
        "--no-deps",
        "--no-build-isolation",
        "--no-index",
        "--wheel-dir",
        str(dist),
    ]
    env = dict(__import__("os").environ)
    env["PIP_NO_INDEX"] = "1"
    try:
        subprocess.run(command, cwd=root, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"offline wheel build failed: {detail}") from exc
    finally:
        _cleanup_build_outputs(root)
    after = sorted(set(dist.glob("auto_foundry_core-0.3.4-*.whl")) - before)
    if len(after) != 1:
        # A clean build should produce one wheel.  Reusing an existing exact
        # artifact is not deterministic, so fail rather than guessing.
        raise RuntimeError(f"expected exactly one newly built core wheel, found {after}")
    wheel = after[0]
    # Wheel builders may stamp ZIP members with the current clock.  Rewrite
    # the already-built wheel with fixed member metadata so identical source
    # trees produce identical bytes without changing wheel payloads.
    temporary = wheel.with_suffix(".normalized.whl")
    with zipfile.ZipFile(wheel, "r") as source, zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for name in sorted(source.namelist()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            target.writestr(info, source.read(name))
    wheel.unlink()
    temporary.rename(wheel)
    return wheel


def package_release(root: Path, dist: Path) -> dict[str, object]:
    skill_root = root / "skills" / "auto-foundry-agentic-e2e"
    if not skill_root.is_dir():
        raise FileNotFoundError(skill_root)
    dist.mkdir(parents=True, exist_ok=True)
    zip_path = dist / ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()
    for wheel in dist.glob("auto_foundry_core-0.3.4-*.whl"):
        wheel.unlink()
    wheel_path = _build_wheel(root, dist)
    zip_info = _write_deterministic_zip(skill_root, zip_path)
    return {
        "zip": zip_info,
        "wheel": {"path": str(wheel_path), "sha256": _sha256(wheel_path), "bytes": wheel_path.stat().st_size},
        "offline": True,
        "network": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    dist = (args.dist or root / "dist").resolve()
    try:
        result = package_release(root, dist)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"package release: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
