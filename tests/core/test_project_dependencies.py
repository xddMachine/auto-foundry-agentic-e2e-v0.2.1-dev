from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tomllib


def test_advertised_base_format_dependencies_are_importable() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    with (repo_root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    dependencies = {str(value).split(">=", 1)[0].split("==", 1)[0] for value in project["dependencies"]}
    assert {"pyarrow", "openpyxl", "pypdf"}.issubset(dependencies)

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run(
        [sys.executable, "-c", "import auto_foundry_core, openpyxl, pypdf, pyarrow"],
        cwd=repo_root,
        env=environment,
        check=True,
    )
