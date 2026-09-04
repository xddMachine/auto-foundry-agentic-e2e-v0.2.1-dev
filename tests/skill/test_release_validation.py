"""Release identity is normalized without weakening source/version checks."""
from pathlib import Path
import importlib.util
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_validation_identity", ROOT / "scripts/validate_release.py")
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def make_wheel(tmp_path: Path, name: str, version: str = "0.9.0", *, tamper: bool = False) -> tuple[Path, Path]:
    source = tmp_path / "auto_foundry_core"
    source.mkdir()
    wheel = tmp_path / "auto_foundry_core-0.9.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for member in sorted(release.REQUIRED_CORE_MODULES):
            data = b"# Synthetic package file used only to verify exact mapping.\n"
            (source / Path(member).name).write_bytes(data)
            archive.writestr(member, b"# wrong source bytes\n" if tamper else data)
        archive.writestr("auto_foundry_core-0.9.0.dist-info/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return wheel, source


@pytest.mark.parametrize("name", ["auto_foundry_core", "auto-foundry-core", "Auto.Foundry.Core", "auto--foundry__core"])
def test_equivalent_distribution_names_accept_the_same_verified_source(tmp_path: Path, name: str) -> None:
    wheel, source = make_wheel(tmp_path, name)
    result = release._validate_wheel(wheel, source)
    assert result["source_mapping"] == "PASS"
    assert result["metadata_name"] == name  # Record the actual metadata, not a rewritten value.


@pytest.mark.parametrize("name,version", [("different-package", "0.9.0"), ("auto-foundry-core", "0.8.1")])
def test_other_packages_and_versions_are_rejected(tmp_path: Path, name: str, version: str) -> None:
    wheel, source = make_wheel(tmp_path, name, version)
    with pytest.raises(ValueError, match="metadata mismatch"):
        release._validate_wheel(wheel, source)


def test_normalized_name_does_not_bypass_exact_source_mapping(tmp_path: Path) -> None:
    wheel, source = make_wheel(tmp_path, "auto-foundry-core", tamper=True)
    with pytest.raises(ValueError, match="source byte mismatch"):
        release._validate_wheel(wheel, source)
