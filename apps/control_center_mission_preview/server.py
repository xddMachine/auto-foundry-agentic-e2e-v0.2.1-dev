"""Serve one isolated, read-only Mission redesign against an existing run.

This preview deliberately exposes no launch or run-control endpoints.  It
reuses the Control Center's allowlisted read model and serves its own static
assets on a separate loopback port.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from apps.control_center_operational.projection import OperationalRepository


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
BOARD_STATIC_DIR = APP_DIR.parent / "control_center_mission_static_concept"


def _resolve_run_id(repository: OperationalRepository, requested: str | None) -> str:
    runs = repository.list_runs()
    if requested:
        for run in runs:
            if requested in {run.get("id"), run.get("authoritativeRunId")}:
                return str(run["id"])
        raise ValueError(f"Run {requested!r} is not discoverable below the supplied run root")
    if len(runs) != 1:
        raise ValueError("--run-id is required when the supplied root contains multiple runs")
    return str(runs[0]["id"])


class MissionPreviewHandler(BaseHTTPRequestHandler):
    server_version = "AutoFoundryMissionPreview/0.1"

    @property
    def repository(self) -> OperationalRepository:
        return self.server.repository  # type: ignore[attr-defined]

    @property
    def run_id(self) -> str:
        return self.server.run_id  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"[mission-preview] {self.address_string()} {format % args}\n")

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )

    def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message, "status": int(status)}, status)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.PERMANENT_REDIRECT)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def _static(self, request_path: str, static_dir: Path = STATIC_DIR) -> None:
        relative = "index.html" if request_path in {"", "/", "/index.html"} else unquote(request_path.lstrip("/"))
        candidate_relative = Path(relative)
        if (
            not relative
            or candidate_relative.is_absolute()
            or candidate_relative.as_posix() != relative
            or ".." in candidate_relative.parts
        ):
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        candidate = static_dir / candidate_relative
        current = static_dir
        try:
            for part in candidate_relative.parts:
                current = current / part
                if current.is_symlink():
                    self._error(HTTPStatus.NOT_FOUND, "Not found")
                    return
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(static_dir.resolve(strict=True))
        except (OSError, ValueError):
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        if not resolved.is_file() or resolved.is_symlink():
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type = f"{content_type}; charset=utf-8"
        self._send(resolved.read_bytes(), content_type)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            run = next((item for item in self.repository.list_runs() if item.get("id") == self.run_id), None)
            self._json({"preview": True, "readOnly": True, "runId": self.run_id, "run": run})
            return
        if parsed.path == "/api/snapshot":
            try:
                self._json(self.repository.snapshot(self.run_id))
            except KeyError:
                self._error(HTTPStatus.NOT_FOUND, "Run not found")
            except (OSError, TypeError, ValueError):
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Snapshot temporarily unavailable")
            return
        if parsed.path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "Unknown preview endpoint")
            return
        if parsed.path == "/board":
            self._redirect("/board/")
            return
        if parsed.path.startswith("/board/"):
            self._static(parsed.path.removeprefix("/board"), BOARD_STATIC_DIR)
            return
        self._static(parsed.path)

    def _read_only(self) -> None:
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "This preview is read-only")

    do_POST = _read_only  # type: ignore[assignment]
    do_PUT = _read_only  # type: ignore[assignment]
    do_PATCH = _read_only  # type: ignore[assignment]
    do_DELETE = _read_only  # type: ignore[assignment]


class MissionPreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(run_root: Path, requested_run_id: str | None, host: str, port: int) -> MissionPreviewServer:
    raw_root = Path(run_root).expanduser()
    if raw_root.is_symlink():
        raise ValueError("Run root must not be a symlink")
    root = raw_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Run root must be a directory")
    repository = OperationalRepository(None, (root,))
    run_id = _resolve_run_id(repository, requested_run_id)
    server = MissionPreviewServer((host, port), MissionPreviewHandler)
    server.repository = repository  # type: ignore[attr-defined]
    server.run_id = run_id  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the isolated Auto Foundry Mission preview")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("The preview may bind only to loopback")
    try:
        server = build_server(args.run_root, args.run_id, args.host, args.port)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    host, port = server.server_address[:2]
    print(f"Mission preview: http://{host}:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
