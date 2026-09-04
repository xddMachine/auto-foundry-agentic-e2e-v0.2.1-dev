from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from apps.control_center_operational.launch import LaunchManager, LaunchSettings
from apps.control_center_operational.projection import OperationalRepository
from apps.control_center_operational.server import STATIC_DIR, OperationalServer, render_index


def test_operational_shell_owns_all_assets() -> None:
    html = render_index().decode("utf-8")
    assert 'content="light"' in html
    assert 'href="/styles.css"' in html
    assert 'href="/theme.css"' in html
    assert 'href="/operational.css"' in html
    assert 'src="/app.js"' in html
    assert 'src="/operational.js"' in html
    assert 'href="/' + "base.css" + '"' not in html
    assert (STATIC_DIR / "styles.css").is_file()
    assert (STATIC_DIR / "theme.css").is_file()
    assert (STATIC_DIR / "app.js").is_file()


def test_theme_has_visible_focus_proxies_for_native_controls() -> None:
    css = (STATIC_DIR / "theme.css").read_text(encoding="utf-8")
    assert ".mode-selector input:focus-visible + span" in css
    assert ".drop-zone:focus-within" in css
    assert ".folder-button:focus-within" in css


def test_static_assets_are_local_and_not_symlinked() -> None:
    for path in STATIC_DIR.iterdir():
        assert path.resolve().is_relative_to(STATIC_DIR.resolve())
        assert not path.is_symlink()


def test_operational_sources_have_no_deprecated_runtime_references() -> None:
    package_root = Path(__file__).resolve().parents[1]
    # Build deprecated package tokens without embedding them in this guard's
    # own source, otherwise the guard would detect itself.
    forbidden = (
        "apps." + "control_center.",
        "apps." + "control_center_" + "dashboard_" + "prototype",
        "control_center_" + "dashboard_" + "prototype",
    )
    for path in package_root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not any(token in text for token in forbidden), path


def test_operational_package_has_one_executable_entrypoint() -> None:
    package_root = Path(__file__).resolve().parents[1]
    entrypoints = []
    for path in package_root.glob("*.py"):
        if "if __name__ == \"__main__\"" in path.read_text(encoding="utf-8"):
            entrypoints.append(path.name)
    assert entrypoints == ["server.py"]


def test_server_rejects_non_loopback_binding() -> None:
    with pytest.raises(ValueError, match="loopback host"):
        OperationalServer(("0.0.0.0", 0), OperationalRepository(None, []), LaunchManager(LaunchSettings(runtime_root=Path(tempfile.gettempdir()), runs_root=Path(tempfile.gettempdir()) / "auto-foundry-static-test")))
