#!/usr/bin/env python3
"""Serve a separate dashboard-styled view of the read-only Control Center.

This module deliberately reuses the Control Center's read-only repository and
browser application.  It changes only the presentation layer and never edits
the original prototype or any observed run.
"""

from __future__ import annotations

import argparse
import mimetypes
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

from apps.control_center.server import (
    ControlCenterHandler,
    DEFAULT_FIXTURE,
    ReadOnlyRepository,
)


APP_DIR = Path(__file__).resolve().parent
THEME_DIR = APP_DIR / "static"
CONTROL_CENTER_DIR = APP_DIR.parent / "control_center"
CONTROL_CENTER_STATIC = CONTROL_CENTER_DIR / "static"


def render_index() -> bytes:
    """Return the original app shell with prototype-only theme resources."""
    html = (CONTROL_CENTER_STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        '<meta name="color-scheme" content="dark" />',
        '<meta name="color-scheme" content="light" />',
    )
    html = html.replace(
        "<title>Auto Foundry · Control Center</title>",
        "<title>Auto Foundry · Control Center · Dashboard Theme</title>",
    )
    html = html.replace(
        '<link rel="stylesheet" href="/styles.css" />',
        '<link rel="stylesheet" href="/base.css" />\n'
        '    <link rel="stylesheet" href="/theme.css" />',
    )
    return html.encode("utf-8")


class DashboardPrototypeHandler(ControlCenterHandler):
    """Serve the unchanged app logic with a separate dashboard visual theme."""

    def _send_asset(self, body: bytes, filename: str) -> None:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type,
        )
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, request_path: str) -> None:
        if request_path in {"", "/", "/index.html"}:
            self._send_asset(render_index(), "index.html")
            return

        assets = {
            "/base.css": CONTROL_CENTER_STATIC / "styles.css",
            "/theme.css": THEME_DIR / "theme.css",
            "/app.js": CONTROL_CENTER_STATIC / "app.js",
        }
        requested = assets.get(request_path)
        if requested is None or not requested.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._send_asset(requested.read_bytes(), requested.name)


class DashboardPrototypeServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], repository: ReadOnlyRepository) -> None:
        super().__init__(address, DashboardPrototypeHandler)
        self.repository = repository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8766, help="Local port (default: 8766)")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="Deterministic mission fixture; pass an empty string to disable",
    )
    parser.add_argument(
        "--runs-root",
        action="append",
        type=Path,
        default=[],
        help="Explicit read-only root to scan for run_state.json (repeatable)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Dashboard prototype binds to loopback only")
    fixture = args.fixture if args.fixture and args.fixture.is_file() else None
    repository = ReadOnlyRepository(fixture, args.runs_root)
    server = DashboardPrototypeServer((args.host, args.port), repository)
    print(f"Auto Foundry Control Center — Dashboard Theme: http://{args.host}:{args.port}")
    print("Mode: read-only Layer 1; original prototype and run data are unchanged")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
