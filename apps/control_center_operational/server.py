"""Loopback operational Control Center server.

The original dark Control Center and the light dashboard prototype remain
unchanged.  This server composes their read-only repository/assets and adds a
small, token-protected operational API for staged new-run launches.
"""

from __future__ import annotations

import argparse
import json
import ipaddress
import mimetypes
import sys
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from apps.control_center.server import (
    DEFAULT_FIXTURE,
    ControlCenterHandler,
    ReadOnlyRepository,
    _safe_text,
)
from apps.control_center_dashboard_prototype.server import render_index as render_dashboard_index

from .launch import (
    MAX_REQUEST_BYTES,
    LaunchError,
    LaunchManager,
    LaunchSettings,
    LaunchValidationError,
    default_codex_binary,
)
from .projection import OperationalRepository
from .run_control import RunControlManager


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
REPO_ROOT = APP_DIR.parents[1]
OPERATIONAL_ASSETS = {"/operational.css": STATIC_DIR / "operational.css", "/operational.js": STATIC_DIR / "operational.js"}


def render_index() -> bytes:
    """Layer operational assets/text on the dashboard prototype shell only."""

    html = render_dashboard_index().decode("utf-8")
    replacements = {
        "<em>deferred</em>": "<em>fetched on execute</em>",
        "Compose and validate a launch draft. Runtime commands remain disabled in the safe Layer 1 build.": "Prepare an immutable launch package, then explicitly confirm before starting a local Planner session.",
        "DRAFT VALIDATION ONLY": "TWO-STEP LAUNCH",
        "Validate draft": "Prepare launch",
        "This action validates only. It cannot start, stop, or change a run.": "Preparation is non-mutating; Start run requires a second fingerprint-bound confirmation.",
        'type="range" min="1" max="8" value="8"': 'type="range" min="1" max="64" value="64"',
        '<script src="/app.js" defer></script>': '<script src="/app.js" defer></script>\n    <script src="/operational.js" defer></script>',
    }
    for old, new in replacements.items():
        html = html.replace(old, new)
    # The base prototype adds base.css and theme.css.  Keep both links and
    # append the operational layer after them so cascade order is explicit.
    html = html.replace(
        '<link rel="stylesheet" href="/theme.css" />',
        '<link rel="stylesheet" href="/theme.css" />\n    <link rel="stylesheet" href="/operational.css" />',
    )
    return html.encode("utf-8")


class OperationalHandler(ControlCenterHandler):
    server_version = "AutoFoundryControlCenterOperational/0.1"

    @property
    def repository(self) -> OperationalRepository:
        return self.server.repository  # type: ignore[attr-defined]

    @property
    def manager(self) -> LaunchManager:
        return self.server.manager  # type: ignore[attr-defined]

    @property
    def run_control(self) -> RunControlManager:
        return self.server.run_control  # type: ignore[attr-defined]

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        super()._json(payload, status)

    def _serve_static(self, request_path: str) -> None:
        if request_path in {"", "/", "/index.html"}:
            self._send_asset(render_index(), "index.html")
            return
        assets = {
            "/base.css": APP_DIR.parent / "control_center" / "static" / "styles.css",
            "/theme.css": APP_DIR.parent / "control_center_dashboard_prototype" / "static" / "theme.css",
            "/app.js": APP_DIR.parent / "control_center" / "static" / "app.js",
            **OPERATIONAL_ASSETS,
        }
        requested = assets.get(request_path)
        if requested is None or not requested.is_file() or requested.is_symlink():
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._send_asset(requested.read_bytes(), requested.name)

    def _send_asset(self, body: bytes, filename: str) -> None:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _mutating_guard(self) -> bool:
        if not self._request_host_valid():
            return False
        peer = str(self.client_address[0]).split("%", 1)[0]
        try:
            loopback_peer = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            loopback_peer = False
        if not loopback_peer:
            self._error(HTTPStatus.FORBIDDEN, "Operational commands require a loopback client")
            return False
        if not self._origin_valid():
            return False
        token = self.headers.get("X-Control-Center-Token", "")
        if token != self.manager.settings.launch_token:
            self._error(HTTPStatus.FORBIDDEN, "Invalid control-center token")
            return False
        return True

    def _configured_host(self) -> str:
        return str(getattr(self.server, "configured_host", "127.0.0.1")).strip().lower()

    def _configured_port(self) -> int:
        return int(self.server.server_address[1])

    def _endpoint_host(self) -> str:
        host = self._configured_host()
        return f"[{host}]:{self._configured_port()}" if ":" in host else f"{host}:{self._configured_port()}"

    def _request_host_valid(self) -> bool:
        raw_host = self.headers.get("Host", "")
        parsed = urlparse(f"//{raw_host}")
        configured = self._configured_host()
        hostname = (parsed.hostname or "").lower()
        try:
            port = parsed.port
        except ValueError:
            port = None
        if (
            not raw_host
            or raw_host != self._endpoint_host()
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or hostname != configured
            or port != self._configured_port()
        ):
            self._error(HTTPStatus.FORBIDDEN, "Host must exactly match the configured loopback endpoint")
            return False
        return True

    def _origin_valid(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        configured = self._configured_host()
        hostname = (parsed.hostname or "").lower()
        try:
            port = parsed.port
        except ValueError:
            port = None
        if (
            parsed.scheme != "http"
            or parsed.netloc != self._endpoint_host()
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or hostname != configured
            or port != self._configured_port()
        ):
            self._error(HTTPStatus.FORBIDDEN, "Origin must match the configured loopback endpoint")
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise LaunchValidationError({"payload": "Invalid Content-Length."}) from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise LaunchValidationError({"payload": "Request body is missing or too large."})
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LaunchValidationError({"payload": "Expected a JSON object."}) from exc
        if not isinstance(value, dict):
            raise LaunchValidationError({"payload": "Expected a JSON object."})
        return value

    def do_GET(self) -> None:  # noqa: N802
        if not self._request_host_valid() or not self._origin_valid():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            self._json(self.manager.settings.config_payload())
            return
        if parsed.path == "/api/launch/status":
            draft_id = parse_qs(parsed.query).get("draft_id", [""])[0]
            self._json(self.manager.status(draft_id))
            return
        if parsed.path == "/api/run/status":
            run_id = parse_qs(parsed.query).get("run_id", [""])[0]
            try:
                self._json(self.run_control.status(run_id))
            except LaunchError as exc:
                body = {"error": str(exc), "message": str(exc)}
                if isinstance(exc, LaunchValidationError):
                    body["errors"] = exc.errors
                self._json(body, HTTPStatus(exc.status_code))
            except (OSError, ValueError, TypeError) as exc:
                self._json(
                    {"error": _safe_text(exc), "message": "Run control status is unavailable."},
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not self._request_host_valid():
            return
        parsed = urlparse(self.path)
        if parsed.path not in {
            "/api/launch/upload",
            "/api/launch/prepare",
            "/api/launch/execute",
            "/api/run/pause",
            "/api/run/resume",
        }:
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Unknown operational command")
            return
        if not self._mutating_guard():
            return
        try:
            if parsed.path in {"/api/run/pause", "/api/run/resume"}:
                payload = self._read_json()
                run_id = str(payload.get("runId") or "")
                confirmed = payload.get("confirmed") is True
                result = (
                    self.run_control.pause(run_id, confirmed=confirmed)
                    if parsed.path.endswith("/pause")
                    else self.run_control.resume(run_id, confirmed=confirmed)
                )
                self._json(result, HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/launch/upload":
                query = parse_qs(parsed.query)
                filename = unquote(query.get("filename", [""])[0])
                relative_path = unquote(query.get("relative_path", [filename])[0])
                try:
                    content_length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    content_length = None
                record = self.manager.upload(self.rfile, filename=filename, relative_path=relative_path, content_length=content_length)
                self._json({"uploadId": record.upload_id, "relativePath": record.relative_path, "size": record.size, "sha256": record.sha256}, HTTPStatus.CREATED)
                return
            if parsed.path == "/api/launch/prepare":
                self._json(self.manager.prepare(self._read_json()))
                return
            payload = self._read_json()
            result = self.manager.execute(
                draft_id=str(payload.get("draftId") or ""),
                fingerprint=str(payload.get("fingerprint") or ""),
                confirmed=payload.get("confirmed") is True,
            )
            self._json(result, HTTPStatus.ACCEPTED)
        except LaunchError as exc:
            body = {"error": str(exc), "message": str(exc)}
            if isinstance(exc, LaunchValidationError):
                body["errors"] = exc.errors
            self._json(body, HTTPStatus(exc.status_code))
        except (OSError, ValueError, TypeError) as exc:
            self._json({"error": _safe_text(exc), "message": "Operational request failed."}, HTTPStatus.UNPROCESSABLE_ENTITY)


class OperationalServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        repository: OperationalRepository,
        manager: LaunchManager,
        run_control: RunControlManager | None = None,
    ) -> None:
        configured_host = str(address[0]).strip().lower()
        if configured_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Operational server must bind to an explicit loopback host")
        super().__init__(address, OperationalHandler)
        self.configured_host = configured_host
        self.repository = repository
        self.manager = manager
        self.run_control = run_control or RunControlManager(manager)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8768, help="Local port (default: 8768)")
    parser.add_argument("--runtime-root", type=Path, default=REPO_ROOT, help="Runtime checkout root")
    parser.add_argument("--runs-root", type=Path, default=None, help="Writable destination and observed run root")
    parser.add_argument("--source-root", action="append", type=Path, default=[], help="Allowed local source root (repeatable)")
    parser.add_argument("--state-root", type=Path, default=None, help="Operational state root")
    parser.add_argument("--max-agents", type=int, default=64, help="Maximum requested active workers")
    parser.add_argument("--enable-launch", action="store_true", help="Explicitly enable confirmed launch execution")
    parser.add_argument("--codex-bin", default=default_codex_binary(), help="Codex executable name/path")
    parser.add_argument("--protected-run-id", action="append", default=[], help="Run ID that operational continuation must never target (repeatable)")
    parser.add_argument("--protected-run-root", action="append", type=Path, default=[], help="Run root that operational continuation must never target (repeatable)")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE, help="Optional inherited fixture")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Operational Control Center binds to loopback only")
    runtime_root = args.runtime_root.resolve(strict=False)
    runs_root = (args.runs_root or (runtime_root / "runs")).resolve(strict=False)
    settings = LaunchSettings(
        runtime_root=runtime_root,
        runs_root=runs_root,
        source_roots=tuple(args.source_root),
        state_root=args.state_root,
        max_agents=args.max_agents,
        enable_launch=args.enable_launch,
        codex_bin=args.codex_bin,
        protected_run_ids=tuple(args.protected_run_id),
        protected_run_roots=tuple(args.protected_run_root),
    )
    fixture = args.fixture if args.fixture and args.fixture.is_file() else None
    repository = OperationalRepository(fixture, [runs_root], launch_state_root=settings.state_root)
    manager = LaunchManager(settings, repository=repository)
    server = OperationalServer((args.host, args.port), repository, manager)
    print(f"Auto Foundry Control Center — Operational: http://{args.host}:{args.port}")
    print(f"Launch commands enabled: {settings.commands_enabled}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["OperationalHandler", "OperationalRepository", "OperationalServer", "render_index", "main"]
