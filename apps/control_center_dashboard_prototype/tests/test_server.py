from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import threading

import pytest

from apps.control_center_dashboard_prototype.server import (
    CONTROL_CENTER_STATIC,
    DashboardPrototypeServer,
    DEFAULT_FIXTURE,
    ReadOnlyRepository,
    THEME_DIR,
    main,
    render_index,
)


@contextmanager
def running_server():
    server = DashboardPrototypeServer(
        ("127.0.0.1", 0),
        ReadOnlyRepository(DEFAULT_FIXTURE, []),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def request(port: int, method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    response_body = response.read()
    result = response.status, dict(response.getheaders()), response_body
    connection.close()
    return result


def test_render_index_layers_theme_without_replacing_app() -> None:
    html = render_index().decode("utf-8")

    assert 'content="light"' in html
    assert 'href="/base.css"' in html
    assert 'href="/theme.css"' in html
    assert 'src="/app.js"' in html
    assert 'href="/styles.css"' not in html


def test_prototype_assets_are_separate_from_original() -> None:
    theme = (THEME_DIR / "theme.css").resolve()
    original = (CONTROL_CENTER_STATIC / "styles.css").resolve()

    assert theme.is_file()
    assert original.is_file()
    assert theme != original
    assert CONTROL_CENTER_STATIC not in theme.parents


def test_theme_has_visible_focus_proxies_for_hidden_native_controls() -> None:
    css = (THEME_DIR / "theme.css").read_text(encoding="utf-8")

    assert ".mode-selector input:focus-visible + span" in css
    assert ".drop-zone:focus-within" in css
    assert ".folder-button:focus-within" in css


def test_original_assets_remain_addressable_read_only_sources() -> None:
    expected = {"index.html", "styles.css", "app.js"}
    available = {path.name for path in Path(CONTROL_CENTER_STATIC).iterdir() if path.is_file()}

    assert expected <= available


def test_static_allowlist_rejects_traversal_and_sets_security_headers() -> None:
    with running_server() as port:
        status, headers, body = request(port, "GET", "/theme.css")
        traversal_status, _, _ = request(port, "GET", "/%2e%2e/server.py")

    assert status == 200
    assert body.startswith(b"/*")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert traversal_status == 404


def test_api_delegation_stays_read_only() -> None:
    with running_server() as port:
        config_status, _, config_body = request(port, "GET", "/api/config")
        command_status, _, _ = request(port, "POST", "/api/run/start")
        validation_status, _, validation_body = request(
            port,
            "POST",
            "/api/launch/validate",
            {
                "mode": "new",
                "projectName": "Theme review",
                "requirements": ["Compare the dashboard theme"],
                "sources": [],
                "sourceUrl": "",
                "maxAgents": 8,
                "capacity": {
                    "total": 8,
                    "entityResolution": 4,
                    "analyticalOwner": 1,
                    "specialist": 3,
                },
            },
        )

    config = json.loads(config_body)
    validation = json.loads(validation_body)
    assert config_status == 200
    assert config["commandsEnabled"] is False
    assert command_status == 405
    assert validation_status == 200
    assert validation["valid"] is True
    assert validation["mutating"] is False
    assert validation["action"] == "validate_only"


def test_non_loopback_binding_is_rejected() -> None:
    with pytest.raises(SystemExit, match="loopback only"):
        main(["--host", "0.0.0.0"])
