"""Single loopback Operational Control Center runtime.

The operational package owns the HTTP surface, read model, and static shell.
It is the only supported Control Center entry point; all browser operations
remain bounded and local while launch/run-control commands require the explicit
fingerprint and token checks implemented by :mod:`launch` and
:mod:`run_control`.
"""

from __future__ import annotations

import argparse
import json
import ipaddress
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .read_model import (
    DEFAULT_FIXTURE,
    ReadOnlyRepository,
    _utc_now,
    _safe_text,
)

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
def render_index() -> bytes:
    """Return the canonical operational shell from this package's static dir."""

    path = STATIC_DIR / "index.html"
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(path)
    return path.read_bytes()


class OperationalHandler(BaseHTTPRequestHandler):
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

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"[control-center-operational] {self.address_string()} {format % args}\n")

    def _security_headers(self, *, product: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if product:
            # Generated products are untrusted reviewed artifacts.  The
            # sandboxed response has script execution but no same-origin
            # privilege, forms, navigation, workers, or network access.  The
            # canonical product manifest still governs which files can be
            # requested.
            policy = (
                "sandbox allow-scripts; default-src 'none'; script-src 'self'; "
                "connect-src 'none'; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data:; "
                "font-src 'self'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'; object-src 'none'"
            )
            # A sandboxed document has an opaque origin (no
            # ``allow-same-origin`` by design).  ``same-origin`` CORP would
            # therefore block its own hash-bound CSS/JS/SVG subresources;
            # ``cross-origin`` permits those bytes while retaining the
            # origin-isolation boundary enforced by the sandbox/CSP.
            self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        else:
            policy = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'"
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Cache-Control", "no-store")

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message, "status": int(status)}, status)

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/", "/index.html"} else unquote(request_path.lstrip("/"))
        candidate = STATIC_DIR / relative
        # Reject traversal and every symlink component before resolving.  A
        # symlink whose target happens to remain under STATIC_DIR is still not
        # a canonical operational asset.
        if not relative or Path(relative).is_absolute() or Path(relative).as_posix() != relative or ".." in Path(relative).parts:
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        current = STATIC_DIR
        try:
            for part in Path(relative).parts:
                current = current / part
                if current.is_symlink():
                    self._error(HTTPStatus.NOT_FOUND, "Not found")
                    return
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        requested = candidate.resolve(strict=False)
        try:
            requested.relative_to(STATIC_DIR.resolve())
        except (OSError, ValueError):
            requested = None
        if requested is None or not requested.is_file() or requested.is_symlink():
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._send_asset(requested.read_bytes(), requested.name)

    def _serve_product_asset(self, request_path: str, query: str = "", *, preview: bool = False) -> None:
        """Serve one validated final/preview dashboard asset from an active run."""

        prefix = "/api/product/preview/" if preview else "/api/product/dashboard/"
        if query or not request_path.startswith(prefix):
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        tail = unquote(request_path[len(prefix):])
        parts = tail.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        result = self.repository.product_asset(parts[0], parts[1], preview=preview)
        if result is None:
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        body, relative = result
        self._send_asset(body, relative, product=True)

    def _send_asset(self, body: bytes, filename: str, *, product: bool = False) -> None:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers(product=product)
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
        if parsed.path == "/api/health":
            self._json({"status": "ok", "mode": "operational", "time": _utc_now()})
            return
        if parsed.path == "/api/config":
            self._json(self.manager.settings.config_payload())
            return
        if parsed.path == "/api/runs":
            # A browser reload has no in-memory draft to poll.  Reconcile the
            # bounded launch-status store first so stale starting/running
            # placeholders consume any hash-bound Supervisor readiness/exit
            # receipt before the read-only projection is returned.
            reconcile = getattr(self.manager, "reconcile_launch_statuses", None)
            if callable(reconcile):
                reconcile()
            self._json({"runs": self.repository.list_runs(), "observedAt": _utc_now()})
            return
        if parsed.path == "/api/snapshot":
            run_id = parse_qs(parsed.query).get("run_id", [""])[0]
            try:
                reconcile = getattr(self.manager, "reconcile_launch_statuses", None)
                if callable(reconcile):
                    reconcile()
                self._json(self.repository.snapshot(run_id))
            except KeyError:
                self._error(HTTPStatus.NOT_FOUND, "Unknown run")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._error(HTTPStatus.UNPROCESSABLE_ENTITY, f"Could not project run: {_safe_text(exc)}")
            return
        if parsed.path == "/api/events":
            query = parse_qs(parsed.query)
            run_id = query.get("run_id", [""])[0]
            stream_id = _safe_text(query.get("stream", [""])[0], 120)
            try:
                cursor = max(0, int(query.get("after", ["0"])[0]))
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "Invalid event cursor")
                return
            try:
                self._json(self.repository.events_after(run_id, cursor, stream_id))
            except KeyError:
                self._error(HTTPStatus.NOT_FOUND, "Unknown run")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._error(HTTPStatus.UNPROCESSABLE_ENTITY, f"Could not read events: {_safe_text(exc)}")
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
        if parsed.path.startswith("/api/product/preview/"):
            self._serve_product_asset(parsed.path, parsed.query, preview=True)
            return
        if parsed.path.startswith("/api/product/dashboard/"):
            self._serve_product_asset(parsed.path, parsed.query, preview=False)
            return
        if parsed.path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        if not self._request_host_valid():
            return
        parsed = urlparse(self.path)
        if parsed.path not in {
            "/api/launch/upload",
            "/api/launch/prepare",
            "/api/launch/execute",
            "/api/launch/cancel",
            "/api/run/pause",
            "/api/run/resume",
            "/api/run/regenerate-product",
        }:
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Unknown operational command")
            return
        if not self._mutating_guard():
            return
        try:
            if parsed.path in {"/api/run/pause", "/api/run/resume", "/api/run/regenerate-product"}:
                payload = self._read_json()
                run_id = str(payload.get("runId") or "")
                confirmed = payload.get("confirmed") is True
                if parsed.path.endswith("/pause"):
                    result = self.run_control.pause(run_id, confirmed=confirmed)
                elif parsed.path.endswith("/resume"):
                    result = self.run_control.resume(run_id, confirmed=confirmed)
                else:
                    raw_key = payload.get("idempotencyKey", payload.get("idempotency_key"))
                    idempotency_key = raw_key if isinstance(raw_key, str) else None
                    raw_reason = payload.get("reason")
                    reason = raw_reason if isinstance(raw_reason, str) and raw_reason.strip() else "operator requested Product dashboard regeneration"
                    result = self.run_control.regenerate_product(
                        run_id,
                        confirmed=confirmed,
                        reason=reason,
                        idempotency_key=idempotency_key,
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
            if parsed.path == "/api/launch/cancel":
                payload = self._read_json()
                result = self.manager.cancel(
                    draft_id=str(payload.get("draftId") or ""),
                    fingerprint=str(payload.get("fingerprint") or ""),
                    confirmed=payload.get("confirmed") is True,
                )
                self._json(result, HTTPStatus.ACCEPTED)
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
        except RuntimeError as exc:
            # Coordinator integrity/admission and other program-owned launch
            # failures are expected command outcomes, not transport failures.
            # Always complete the HTTP response so the browser can leave its
            # disabled ``Starting…`` state and show the durable failure.
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
    # Bind the checkout-owned core before the first repository projection.
    # OperationalRepository validates durable lifecycle state lazily, so a
    # documented server invocation without ``PYTHONPATH=src`` must still
    # resolve the same checkout core before any /api/runs request.
    manager._core_imports()  # noqa: SLF001 - canonical startup bootstrap
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
