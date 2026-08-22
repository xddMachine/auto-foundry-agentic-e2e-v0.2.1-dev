"""A small durable Planner-to-role coordinator.

The coordinator is deliberately boring. The requirement Planner is the only
source of work, one role process performs one public phase action, and the
Planner is read again after that process exits. Process output is diagnostic
transport; persisted Auto Foundry state is the authority.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import tempfile
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from threading import Condition, Thread
import time
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
import uuid

try:  # pragma: no cover - POSIX hosts provide fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .requirement_planning import PlannerAction
from .workspace import RunContext
from .agent_lifecycle import (
    LifecycleEventWriter,
    MAX_CODEX_EVENT_BYTES,
    normalize_codex_json_line,
)


CONTROL_PLANE_DIRNAME = "control_plane"
COORDINATOR_STATE_FILENAME = "coordinator_state.json"
COORDINATOR_SPEC_FILENAME = "coordinator_spec.json"
COORDINATOR_EVENTS_FILENAME = "coordinator_events.jsonl"
COORDINATOR_LOCK_FILENAME = ".coordinator.lock"
COORDINATOR_LEGACY_ARCHIVE_DIRNAME = "legacy_import"
COORDINATOR_LEGACY_INTENT_FILENAME = ".legacy_import.intent.json"
COORDINATOR_LEGACY_STAGING_DIRNAME = ".legacy_import.staging"
COORDINATOR_SCHEMA_VERSION = 1
DEFAULT_LEASE_TTL_SECONDS = 30.0
MAX_ROLE_DIAGNOSTIC_BYTES = 32_768
MAX_RUN_RETRIES_PER_ACTION = 2
TERMINAL_STATUSES = frozenset({"complete", "complete_with_limits"})

# Requirement work has two independent admission dimensions.  Entity
# resolution remains domain-parallel, while one Analytical Owner is allowed
# for the whole run and a requirement may have only one mutating workflow
# action at a time.  Keep these contracts here (rather than in adapters) so
# persisted/reopened dispatches and every role transport share one gate.
_ANALYTICAL_OWNER_ROLE = "analytical_owner"
_REQUIREMENT_MUTATING_ROLES = frozenset(
    {
        "analytical_owner",
        "business_reviewer",
        "integration_agent",
        "integration_fidelity_reviewer",
        "fidelity_reviewer",
    }
)
_ANALYTICAL_OWNER_ACTIONS = frozenset(
    {"analyze_requirement", "resume_requirement_analysis", "repair_requirement"}
)
_REQUIREMENT_MUTATING_ACTIONS = frozenset(
    {
        *_ANALYTICAL_OWNER_ACTIONS,
        "review_requirement",
        "finalize_requirement_review",
        "integrate_requirement",
        "repair_integration_fidelity",
        "review_integration_fidelity",
        "commit_integration_requirement",
    }
)
_NON_ANALYTICAL_OWNER_ACTIONS = frozenset(
    {
        "resolve_identity",
        "repair_identity_result",
        "resume_identity_resolution",
        "review_identity_result",
        "commit_identity_result",
        "escalate_identity_failure",
    }
)

# The Control Center binds every production Codex role to this release.  The
# digest is the deterministic extracted-tree identity emitted by the reviewed
# release packager.  These small tracked constants are the runtime manifest;
# provisioning the ZIP is deliberately outside the run path.
PRODUCTION_SKILL_VERSION = "0.7.1"
PRODUCTION_CORE_VERSION = "0.8.0"
# Updated to the deterministic v0.7.1 package after the release artifact is
# built.  Keeping this tracked manifest independent of ``dist/`` lets a fresh
# checkout validate an already-installed skill without requiring ignored
# package output.
PRODUCTION_SKILL_SHA256 = "01b0bd8b8fe1edbef8bf5b86c4315526bcb8993e5e044428269efd71d16b0b19"
PRODUCTION_SKILL_FILE_COUNT = 27
PRODUCTION_SKILL_NAME = "auto-foundry-agentic-e2e"
PRODUCTION_RELEASE = "entity-resolution-and-analytical-relationships"

# The only legacy control-plane shape accepted here is the G5 wrapper that
# shipped before the deletion-first coordinator.  Its event hashes covered a
# different canonical payload, so importing means archiving bytes and
# starting a fresh local chain; it is never replayed as current state.
_LEGACY_SPEC_KEYS = frozenset({"schema_version", "kind", "run_spec", "spec_hash", "lineage_binding"})
_LEGACY_RUN_SPEC_KEYS = frozenset(
    {
        "actions",
        "adapter_capabilities",
        "codex_exec",
        "coordinator_agent_command",
        "generation_id",
        "offline_test_mode",
        "parent_lineage",
        "phase_validator_command",
        "planner_hash",
        "planner_ref",
        "policy",
        "publication_policy",
        "role_dispatch_command",
        "run_id",
    }
)
_LEGACY_LINEAGE_KEYS = frozenset(
    {
        "active_generation_pointer_hash",
        "generation_id",
        "manifest_hash",
        "plan_hash",
        "planner_hash",
        "planner_ref",
    }
)
_LEGACY_STATE_REQUIRED_KEYS = frozenset(
    {"run_id", "generation_id", "planner_ref", "planner_hash", "spec_hash", "spec_ref", "schema_version", "kind", "last_event_seq", "last_event_hash"}
)
_LEGACY_EVENT_REQUIRED_KEYS = frozenset(
    {"seq", "event", "event_hash", "state_hash", "after_state", "run_id", "generation_id", "planner_ref", "planner_hash", "schema_version", "kind"}
)
_LEGACY_CONTROL_FILES = frozenset(
    {
        COORDINATOR_LOCK_FILENAME,
        COORDINATOR_EVENTS_FILENAME,
        COORDINATOR_SPEC_FILENAME,
        COORDINATOR_STATE_FILENAME,
    }
)


class CoordinatorError(RuntimeError):
    """Base error for coordinator admission, integrity, and scheduling."""


class CoordinatorIntegrityError(CoordinatorError):
    """Raised when persisted state or the event chain is not trustworthy."""


class CoordinatorConflictError(CoordinatorError):
    """Raised when a lease, lineage, or compare-and-swap check is stale."""


class CoordinatorPublicationError(CoordinatorError):
    """Raised when a local publication policy denies an action."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _canonical(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, Path):
        return str(value)
    return value


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(_canonical(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON value is not permitted: {value}")


def _strict_json_loads(raw: str | bytes) -> Any:
    return json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_nonfinite_json_constant,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value).rstrip(b"\n"))


def _skill_release_bytes(skill_root: Path) -> bytes:
    """Serialize one skill tree with the release packager's byte contract.

    The release ZIP is the production identity.  Recreating its deterministic
    member metadata here lets a persisted run prove that its bound directory
    still contains exactly those bytes without consulting ``CODEX_HOME`` or a
    mutable checkout.  Symlinks and generated caches are never part of a
    production skill tree.
    """

    if skill_root.is_symlink() or not skill_root.is_dir():
        raise CoordinatorIntegrityError("bound skill path is missing or not a regular directory")
    files: list[Path] = []
    for path in skill_root.rglob("*"):
        if path.is_symlink():
            raise CoordinatorIntegrityError("bound skill tree contains a symlink or non-regular entry")
        if path.is_dir():
            continue
        if not path.is_file():
            raise CoordinatorIntegrityError("bound skill tree contains a non-regular entry")
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"} or path.name == ".DS_Store":
            continue
        files.append(path)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files, key=lambda item: item.relative_to(skill_root).as_posix()):
            relative = path.relative_to(skill_root).as_posix()
            info = zipfile.ZipInfo(f"{PRODUCTION_SKILL_NAME}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return payload.getvalue()


def _validate_skill_frontmatter(skill_root: Path, *, skill_version: str, core_version: str) -> None:
    skill_file = skill_root / "SKILL.md"
    if skill_file.is_symlink() or not skill_file.is_file():
        raise CoordinatorIntegrityError("bound skill SKILL.md is missing or not regular")
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CoordinatorIntegrityError("bound skill SKILL.md cannot be read") from exc
    if not text.startswith("---\n"):
        raise CoordinatorIntegrityError("bound skill frontmatter is missing")
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise CoordinatorIntegrityError("bound skill frontmatter is incomplete")
    frontmatter = parts[1]
    frontmatter_lines = {line.strip() for line in frontmatter.splitlines() if line.strip()}
    required = {
        'name: auto-foundry-agentic-e2e',
        f'version: "{skill_version}"',
        'core_name: auto_foundry_core',
        f'core_version: "{core_version}"',
        f"release: {PRODUCTION_RELEASE}",
    }
    if not required.issubset(frontmatter_lines):
        raise CoordinatorIntegrityError("bound skill frontmatter/version markers do not match")
    if f"skill_version: {skill_version}" not in text or f"core_version: {core_version}" not in text:
        raise CoordinatorIntegrityError("bound skill runtime version markers do not match")


def _skill_frontmatter_name(skill_file: Path) -> str:
    """Read one top-level ``name`` from a SKILL.md YAML frontmatter block.

    Skill discovery only needs the top-level name.  A full YAML dependency is
    intentionally not pulled into the core package: nested ``metadata.name``
    values must not masquerade as the declaration, while quoted values and
    trailing comments remain valid YAML scalars.  The bounded parser below
    handles exactly that frontmatter scalar contract and fails closed on
    ambiguous top-level declarations.
    """

    if skill_file.is_symlink() or not skill_file.is_file():
        raise CoordinatorIntegrityError("discoverable skill SKILL.md is missing or not regular")
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CoordinatorIntegrityError("discoverable skill SKILL.md cannot be read") from exc
    if not text.startswith("---\n"):
        raise CoordinatorIntegrityError("discoverable skill frontmatter is missing")
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise CoordinatorIntegrityError("discoverable skill frontmatter is incomplete")
    names: list[str] = []
    for line in parts[1].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # YAML indentation is significant.  Ignore nested ``metadata.name``
        # and only inspect an exact top-level key spelling.
        if line[:1].isspace() or not line.startswith("name"):
            continue
        key, separator, raw_value = line.partition(":")
        if separator != ":" or key != "name":
            continue
        value_chars: list[str] = []
        quote: str | None = None
        escaped = False
        for char in raw_value.strip():
            if quote == '"' and escaped:
                value_chars.append(char)
                escaped = False
                continue
            if quote == '"' and char == "\\":
                value_chars.append(char)
                escaped = True
                continue
            if quote is None and char in {"'", '"'}:
                quote = char
                value_chars.append(char)
                continue
            if quote is not None and char == quote:
                quote = None
                value_chars.append(char)
                continue
            if quote is None and char == "#":
                break
            value_chars.append(char)
        if quote is not None:
            raise CoordinatorIntegrityError("discoverable skill frontmatter name is malformed")
        value = "".join(value_chars).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            if value[0] == '"':
                try:
                    decoded = json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise CoordinatorIntegrityError("discoverable skill frontmatter name is malformed") from exc
                if not isinstance(decoded, str):
                    raise CoordinatorIntegrityError("discoverable skill frontmatter name is malformed")
                value = decoded
            else:
                value = value[1:-1].replace("''", "'")
        if not value:
            raise CoordinatorIntegrityError("discoverable skill frontmatter name is empty")
        names.append(value)
    if len(names) != 1:
        raise CoordinatorIntegrityError("discoverable skill frontmatter name is malformed")
    return names[0]


def _skill_scope_candidates(scope: Path) -> tuple[Path, ...]:
    """Find declared production names among direct skill-root children."""

    if scope.is_symlink():
        raise CoordinatorIntegrityError(f"skill discovery scope cannot be a symlink: {scope}")
    if not scope.exists():
        return ()
    if not scope.is_dir():
        raise CoordinatorIntegrityError(f"skill discovery scope is not a directory: {scope}")
    try:
        matches: list[Path] = []
        for candidate in sorted(scope.iterdir(), key=lambda path: str(path)):
            if candidate.is_symlink():
                try:
                    target = candidate.resolve(strict=True)
                except OSError as exc:
                    # An unrelated broken alias is outside this production
                    # skill's authority.  An alias named as the production
                    # entrypoint is still a fail-closed discovery defect.
                    if candidate.name == PRODUCTION_SKILL_NAME:
                        raise CoordinatorIntegrityError("discoverable skill symlink is unavailable") from exc
                    continue
                if target.is_dir() and (target / "SKILL.md").exists():
                    try:
                        declared = _skill_frontmatter_name(target / "SKILL.md")
                    except CoordinatorIntegrityError:
                        # A malformed symlink target cannot be safely
                        # identified as this production skill unless its
                        # lexical alias says so.  Ignore unrelated aliases;
                        # the active path itself is validated separately.
                        if candidate.name == PRODUCTION_SKILL_NAME or target.name == PRODUCTION_SKILL_NAME:
                            raise
                        continue
                    if declared == PRODUCTION_SKILL_NAME:
                        raise CoordinatorIntegrityError("discoverable production skill directory cannot be a symlink")
                continue
            if not candidate.is_dir():
                continue
            skill_file = candidate / "SKILL.md"
            if not skill_file.exists():
                continue
            if _skill_frontmatter_name(skill_file) == PRODUCTION_SKILL_NAME:
                matches.append(candidate)
    except OSError as exc:
        raise CoordinatorIntegrityError(f"skill discovery scope cannot be scanned: {scope}") from exc
    return tuple(sorted(set(matches), key=lambda path: str(path)))


def _canonical_skill_path(path: Path, *, description: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise CoordinatorIntegrityError(f"{description} must be an absolute non-symlink directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CoordinatorIntegrityError(f"{description} is unavailable") from exc
    # ``/var`` -> ``/private/var`` is an administrator-provided macOS alias,
    # not a skill-directory symlink.  Persist and compare the resolved path;
    # reject the target directory itself when it is a symlink.
    if resolved.is_symlink() or not resolved.is_dir():
        raise CoordinatorIntegrityError(f"{description} is not canonical or regular")
    return resolved


def _ancestor_skill_scopes(anchor: Path) -> tuple[Path, ...]:
    """Return project ``.agents/skills`` scopes along one bounded ancestor chain."""

    try:
        current = anchor.expanduser().resolve(strict=True)
    except OSError as exc:
        raise CoordinatorIntegrityError("skill role-cwd scope is unavailable") from exc
    values: list[Path] = []
    while True:
        values.append(current / ".agents" / "skills")
        if current.parent == current:
            break
        current = current.parent
    return tuple(values)


def resolve_production_skill_binding(
    *,
    repo_root: Path | None = None,
    role_cwd: Path | None = None,
) -> dict[str, str]:
    """Validate and return the single active installed production skill.

    Codex discovers skills from the configured user skills root, the user's
    ``$HOME/.agents/skills`` root, and project-local ``.agents/skills`` roots
    along the role-cwd and runtime/repository ancestor chains.  The
    repository's packaged source tree is intentionally not a discovery scope.
    Exactly one same-name entrypoint must exist, and it must be the active
    installed path.
    """

    raw_code_home = os.environ.get("CODEX_HOME")
    code_home = Path(raw_code_home).expanduser() if raw_code_home else Path.home() / ".codex"
    code_home = _canonical_skill_path(code_home, description="CODEX_HOME") if code_home.exists() else code_home
    if not code_home.is_absolute() or code_home.is_symlink():
        raise CoordinatorIntegrityError("CODEX_HOME must be an absolute non-symlink directory")
    skills_root = code_home / "skills"
    active_raw = skills_root / PRODUCTION_SKILL_NAME
    active = _canonical_skill_path(active_raw, description="active installed skill")

    project_root = (repo_root or Path.cwd()).expanduser()
    if not project_root.is_absolute():
        raise CoordinatorIntegrityError("repository skill scope must be absolute")
    try:
        project_root = project_root.resolve(strict=True)
    except OSError as exc:
        raise CoordinatorIntegrityError("repository skill scope is unavailable") from exc
    cwd_root = (role_cwd or Path.cwd()).expanduser()
    if not cwd_root.is_absolute():
        raise CoordinatorIntegrityError("role-cwd skill scope must be absolute")
    scopes = [skills_root, Path.home() / ".agents" / "skills"]
    scopes.extend(_ancestor_skill_scopes(cwd_root))
    scopes.extend(_ancestor_skill_scopes(project_root))
    # Validate the active entrypoint before discovery enumeration so malformed
    # active frontmatter is diagnosed directly rather than reported as a
    # generic missing candidate.
    _validate_skill_frontmatter(active, skill_version=PRODUCTION_SKILL_VERSION, core_version=PRODUCTION_CORE_VERSION)
    # Preserve ordering for diagnostics while avoiding duplicate scans.
    scopes = list(dict.fromkeys(scopes))
    candidates: set[Path] = set()
    for scope in scopes:
        candidates.update(_skill_scope_candidates(scope))
    canonical_candidates: set[Path] = set()
    for candidate in candidates:
        canonical_candidates.add(_canonical_skill_path(candidate, description="discoverable skill"))
    if canonical_candidates != {active}:
        others = sorted(str(path) for path in canonical_candidates if path != active)
        raise CoordinatorIntegrityError(
            "exactly one active production skill is required; discoverable duplicates: "
            + (", ".join(others) if others else "missing active entrypoint")
        )
    actual = _sha256_bytes(_skill_release_bytes(active))
    if actual != PRODUCTION_SKILL_SHA256:
        raise CoordinatorIntegrityError("active installed skill bytes do not match the production release hash")
    return {
        "skill_path": str(active),
        "skill_version": PRODUCTION_SKILL_VERSION,
        "core_version": PRODUCTION_CORE_VERSION,
        "skill_sha256": PRODUCTION_SKILL_SHA256,
    }


def _legacy_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(_canonical(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _legacy_hash(value: Any) -> str:
    """Hash one legacy event value with its newline-inclusive JSON rule."""

    return _sha256_bytes(_legacy_json_bytes(value))


def _legacy_state_hash(value: Mapping[str, Any]) -> str:
    snapshot = dict(_canonical(value))
    snapshot["last_event_hash"] = ""
    return _legacy_hash(snapshot)


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _pid_alive(pid: Any) -> bool:
    """Return true unless the operating system proves a PID is gone."""

    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by another user; this is not proof
        # of death and therefore remains claimed.
        return True
    except OSError:
        return False
    return True


def _state_hash(value: Mapping[str, Any]) -> str:
    return _sha256_value(value)


def _text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    result = value.strip()
    if not allow_empty and not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _action(value: PlannerAction | Mapping[str, Any]) -> PlannerAction:
    if isinstance(value, PlannerAction):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("Planner action must be a PlannerAction or mapping")
    required = {"action", "role", "subject_id", "reason"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("Planner action missing required fields: " + ", ".join(missing))
    return PlannerAction(
        action=value["action"],
        role=value["role"],
        subject_id=value["subject_id"],
        reason=value["reason"],
        priority=value.get("priority", 100),
        metadata=value.get("metadata") or {},
    )


def _action_key(action: PlannerAction | Mapping[str, Any] | None) -> str | None:
    return None if action is None else _sha256_value(_action(action).to_dict())


def _action_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    try:
        return _action(left).to_dict() == _action(right).to_dict()
    except (TypeError, ValueError):
        return False


def _safe_text(value: Any, limit: int = MAX_ROLE_DIAGNOSTIC_BYTES) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value)
    if len(text.encode("utf-8")) <= limit:
        return text
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore") + "…"


def _atomic_bytes(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise CoordinatorIntegrityError(f"refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, _json_bytes(value))


def _append_line(path: Path, value: Any) -> None:
    if path.is_symlink():
        raise CoordinatorIntegrityError(f"coordinator event log cannot be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())


def _load_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return _strict_json_loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise CoordinatorIntegrityError(f"invalid coordinator JSON: {path}") from exc


def _load_events(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink():
        raise CoordinatorIntegrityError("coordinator event log cannot be a symlink")
    if not path.exists():
        return []
    if not path.is_file():
        raise CoordinatorIntegrityError("coordinator event log is not a regular file")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CoordinatorIntegrityError("coordinator event log cannot be read") from exc
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        value = _strict_json_loads(line)
        if not isinstance(value, Mapping):
            raise CoordinatorIntegrityError("coordinator event must be a JSON object")
        events.append(dict(value))
    return events


def _command_tuple(value: Sequence[str] | str | None, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(_text(item, name) for item in shlex.split(value))
    return tuple(_text(item, name) for item in value)


@dataclass(frozen=True)
class CoordinatorRunSpec:
    """Persisted inputs needed to reconstruct the direct coordinator loop."""

    run_id: str
    generation_id: str
    planner_ref: str
    planner_hash: str
    role_dispatch_command: tuple[str, ...] = ()
    publication_policy: Mapping[str, Any] = field(default_factory=dict)
    codex_exec: Mapping[str, Any] = field(default_factory=dict)
    lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _text(self.run_id, "run_id"))
        object.__setattr__(self, "generation_id", _text(self.generation_id, "generation_id"))
        object.__setattr__(self, "planner_ref", _text(self.planner_ref, "planner_ref"))
        if not _is_sha256(self.planner_hash):
            raise ValueError("planner_hash must be a lowercase SHA-256 hex digest")
        object.__setattr__(self, "role_dispatch_command", _command_tuple(self.role_dispatch_command, "role_dispatch_command"))
        if not isinstance(self.publication_policy, Mapping):
            raise TypeError("publication_policy must be a mapping")
        object.__setattr__(self, "publication_policy", dict(_canonical(self.publication_policy)))
        if not isinstance(self.codex_exec, Mapping):
            raise TypeError("codex_exec must be a mapping")
        object.__setattr__(self, "codex_exec", dict(_canonical(self.codex_exec)))
        if isinstance(self.lease_ttl_seconds, bool) or not isinstance(self.lease_ttl_seconds, (int, float)) or self.lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        object.__setattr__(self, "lease_ttl_seconds", float(self.lease_ttl_seconds))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generation_id": self.generation_id,
            "planner_ref": self.planner_ref,
            "planner_hash": self.planner_hash,
            "role_dispatch_command": list(self.role_dispatch_command),
            "publication_policy": dict(self.publication_policy),
            "codex_exec": dict(self.codex_exec),
            "lease_ttl_seconds": self.lease_ttl_seconds,
        }

    @property
    def skill_path(self) -> str | None:
        value = self.codex_exec.get("skill_path")
        return value if isinstance(value, str) else None

    @property
    def skill_version(self) -> str | None:
        value = self.codex_exec.get("skill_version")
        return value if isinstance(value, str) else None

    @property
    def core_version(self) -> str | None:
        value = self.codex_exec.get("core_version")
        return value if isinstance(value, str) else None

    @property
    def skill_sha256(self) -> str | None:
        value = self.codex_exec.get("skill_sha256")
        return value if isinstance(value, str) else None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CoordinatorRunSpec":
        if not isinstance(value, Mapping):
            raise TypeError("run spec must be a mapping")
        required = {"run_id", "generation_id", "planner_ref", "planner_hash"}
        missing = sorted(required - set(value))
        if missing:
            raise ValueError("run spec missing required fields: " + ", ".join(missing))
        allowed = {
            "run_id", "generation_id", "planner_ref", "planner_hash",
            "role_dispatch_command", "publication_policy", "codex_exec",
            "lease_ttl_seconds", "schema_version", "kind",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError("run spec contains unknown/deprecated fields: " + ", ".join(unknown))
        if "schema_version" in value and value["schema_version"] != COORDINATOR_SCHEMA_VERSION:
            raise ValueError("run spec schema_version is unsupported")
        if "kind" in value and value["kind"] != "run_coordinator_spec":
            raise ValueError("run spec kind is unsupported")
        publication_policy = value.get("publication_policy", {})
        if not isinstance(publication_policy, Mapping):
            raise TypeError("publication_policy must be a mapping")
        codex_exec = value.get("codex_exec", {})
        if not isinstance(codex_exec, Mapping):
            raise TypeError("codex_exec must be a mapping")
        # Validate the raw nested shape before constructing or persisting a
        # coordinator. This rejects stale/deprecated Codex keys without
        # silently dropping them during a migration.
        CodexExecConfig.from_dict(codex_exec)
        return cls(
            run_id=value["run_id"],
            generation_id=value["generation_id"],
            planner_ref=value["planner_ref"],
            planner_hash=value["planner_hash"],
            role_dispatch_command=value.get("role_dispatch_command") or (),
            publication_policy=publication_policy,
            codex_exec=codex_exec,
            lease_ttl_seconds=value.get("lease_ttl_seconds", DEFAULT_LEASE_TTL_SECONDS),
        )


@dataclass(frozen=True)
class RoleExecution:
    """Transport-only role result; never a phase-success authority."""

    exit_code: int | None = 0
    output: str = ""
    error: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "output": _safe_text(self.output),
            "error": _safe_text(self.error),
            "timed_out": bool(self.timed_out),
        }


class PlannerActionProvider(Protocol):
    def next_actions(self, context: RunContext, state: Mapping[str, Any]) -> Sequence[PlannerAction]: ...


class RequirementPlannerProvider:
    """Read the active public requirement Planner without doing work."""

    def next_actions(self, context: RunContext, state: Mapping[str, Any]) -> Sequence[PlannerAction]:
        from .requirement_planning import RequirementSupervisorWorkspace

        return RequirementSupervisorWorkspace(context).next_actions()


class RoleAdapter(Protocol):
    def __call__(self, action: PlannerAction, *, idempotency_key: str, context: RunContext) -> Any: ...


class RoleRunner(Protocol):
    def dispatch(self, action: PlannerAction, *, idempotency_key: str, context: RunContext) -> Any: ...


def _normalize_role_execution(value: Any) -> RoleExecution:
    if isinstance(value, RoleExecution):
        return value
    if isinstance(value, Mapping) and any(key in value for key in ("exit_code", "returncode", "output", "stdout", "error", "stderr", "timed_out")):
        raw_exit = value.get("exit_code", value.get("returncode", 0))
        try:
            exit_code = None if raw_exit is None else int(raw_exit)
        except (TypeError, ValueError):
            exit_code = 1
        return RoleExecution(
            exit_code=exit_code,
            output=_safe_text(value.get("output", value.get("stdout", ""))),
            error=_safe_text(value.get("error", value.get("stderr", ""))),
            timed_out=bool(value.get("timed_out", False)),
        )
    if value is None:
        return RoleExecution(exit_code=0)
    if isinstance(value, bytes):
        return RoleExecution(output=_safe_text(value))
    if isinstance(value, str):
        return RoleExecution(output=_safe_text(value))
    return RoleExecution(output=_safe_text(value))


class MappingRoleAdapter:
    """Small role/action callable registry with no result interpretation."""

    def __init__(self, adapters: Mapping[str, RoleAdapter] | None = None, default: RoleAdapter | None = None) -> None:
        self.adapters = dict(adapters or {})
        self.default = default

    def dispatch(self, action: PlannerAction, *, idempotency_key: str, context: RunContext) -> RoleExecution:
        adapter = self.adapters.get(action.action) or self.adapters.get(action.role) or self.default
        if adapter is None:
            return RoleExecution(exit_code=1, error=f"no role adapter for {action.action}/{action.role}")
        try:
            if hasattr(adapter, "dispatch") and callable(getattr(adapter, "dispatch")):
                value = adapter.dispatch(action, idempotency_key=idempotency_key, context=context)  # type: ignore[attr-defined]
            else:
                value = adapter(action, idempotency_key=idempotency_key, context=context)
            return _normalize_role_execution(value)
        except Exception as exc:
            return RoleExecution(exit_code=1, error=f"role adapter error: {exc}")


def _role_guidance(action: PlannerAction) -> str:
    role = action.role.lower()
    name = action.action
    if name == "finalize_requirement_review":
        return (
            "Finalization sequence: use ItemWorkspace.accept, or the public blocked-by-evidence "
            "finalizer when the review recorded data insufficiency. Do not edit item_state JSON."
        )
    if role == "entity_resolution_owner":
        return (
            "Entity Resolution Owner sequence: load the public current_scope for the assigned domain; "
            "whenever concrete new source hints or representation item IDs are found, call the public "
            "record_scope_discovery(domain_id, owner_ref, source_hints=..., representation_item_ids=...) "
            "operation while holding the active resolution-owner lease. Treat an already_present result "
            "as success, refresh current_scope immediately before submission, and call submit_result with "
            "expected_scope_hash=current_scope.scope_hash. If submit_result reports a stale scope, treat it "
            "as ordinary resolver continuation: refresh current_scope, incorporate the expanded scope, and "
            "retry; do not consume repair or claim independent-review failure. Use only these public APIs and "
            "do not expose internal paths or state details."
        )
    if name in {"analyze_requirement", "resume_requirement_analysis", "repair_requirement"} or role == "analytical_owner":
        return (
            "Analytical Owner sequence: bind/load ItemWorkspace and BoundAnalysisContext, "
            "construct AnalystWorkspace, perform a readiness check and call public "
            "search_ontology, search_identity_mappings, search_identity_decisions, and "
            "search_prepared_assets/select APIs before analysis, then call begin_attempt and the public requirement "
            "plan/semantic-scope/source/evidence/specialist APIs, run_analysis and "
            "prepare_data as needed, submit_answer or conclude_data_insufficiency, and "
            "finish_attempt(status='completed') only after the draft/conclusion is persisted. "
            "For source selection, use AnalystWorkspace.selected_sources() and "
            "analysis.load_selected_source_ids(); consume the returned public AnalystSource/source IDs "
            "exactly as bound. Never read bound.source_catalog.entries, use raw "
            "DataRoomCatalogEntry.source_id, or rebuild source IDs from filenames. "
            "If readiness/search identifies an unresolved identity gap, call "
            "AnalystWorkspace.propose_identity_domain for every newly discovered domain, "
            "then call AnalystWorkspace.mark_waiting_on_resolution with those domain IDs and "
            "let the Planner schedule one Entity Resolution Owner request per domain; wait "
            "for those owners and do not continue as if identity resolution were complete. "
            "submit_answer alone is not an attempt transition. For code or runtime "
            "failures, use ControlledScriptRunner.validate_script/run_analysis diagnostics, "
            "correct the script through public APIs, and retry within the same Analytical Owner "
            "action and begin_attempt; do not finish, close, or hand off a correctable attempt. "
            "If Planner metadata repair_active=true, an active business-repair authorization "
            "already exists: reuse that authorization, bind the same owner, and begin or continue "
            "the repair attempt; never call use_business_repair again for that authorization. "
            "Otherwise, for repair, use the public same-owner repair API before repeating the attempt."
        )
    if name == "review_requirement" or role == "business_reviewer":
        return (
            "Business reviewer sequence: load the item and its public AnalystWorkspace/"
            "BusinessReviewAdapter, inspect the persisted draft and evidence, and call "
            "record (or confirm_data_insufficiency) with an independent reviewer identity."
        )
    if name in {"integrate_requirement", "repair_integration_fidelity", "commit_integration_requirement"} or role == "integration_agent":
        return (
            "Integration sequence: use IntegrationSession.create/load, stage typed ontology and "
            "dashboard records, build_fidelity_packet, and after an accepted fidelity review "
            "call session.commit. Do not write integration artifacts directly."
        )
    if name == "review_integration_fidelity" or role in {"integration_fidelity_reviewer", "fidelity_reviewer"}:
        return "Use IntegrationSession.load and record_fidelity_review only; an independent reviewer must not commit."
    if name == "build_product_candidate":
        return (
            "Product candidate sequence: for G-N, when the business presentation plan is absent, "
            "call write_business_presentation_plan_v2 to create it and "
            "record_business_presentation_plan_v2 to record it; run the canonical dashboard "
            "assembler with the explicit presentation_plan_ref. Only after canonical assembly, "
            "if a stale product-candidate binding is present, call discard_stale_product_candidate; "
            "then construct a refs-only ProductCandidate and call ProductReviewStore.record_candidate."
        )
    if name in {"build_final_product", "publish_final_product"} or role == "product_agent":
        return (
            "Product sequence: use the existing dashboard/product assembler and "
            "ProductReviewStore.record_candidate; publication uses authorize_publish only "
            "with the supplied explicit policy."
        )
    if name == "review_final_product" or role == "product_reviewer":
        return "Load ProductReviewStore and call record_review as an independent reviewer; do not authorize publication."
    if name == "finalize_final_report":
        return (
            "Reporting finalization sequence: call RunReportInputGatherer.gather_from_run(context, persist=True) "
            "for authoritative preflight/recovery, load the persisted preflight, then call "
            "RunReportFinalizer.finalize. Do not author timings, incidents, reviews, or implementation hashes; "
            "these values must come from persisted public report inputs, never agent-authored values."
        )
    if role in {"reporting_agent", "report_agent"} or name in {"recover_final_report", "preflight_final_report"}:
        return (
            "Reporting preflight/recovery sequence: call "
            "RunReportInputGatherer.gather_from_run(context, persist=True), then load the persisted preflight "
            "before continuing through public report APIs. Do not author timings, incidents, reviews, or "
            "implementation hashes; these values must come from persisted public report inputs, never "
            "agent-authored values."
        )
    return "Use the public API for the named Planner action and persist its real phase transition."


def build_role_prompt(
    action: PlannerAction,
    *,
    context: RunContext,
    idempotency_key: str,
    custom: str | None = None,
    skill_binding: Mapping[str, Any] | None = None,
) -> str:
    """Build the plain-text role contract sent to a role process."""

    mandatory_guidance = _role_guidance(action)
    custom_guidance = custom.strip() if isinstance(custom, str) and custom.strip() else ""
    if custom_guidance and (
        action.role.lower() in {"reporting_agent", "report_agent"}
        or action.role.lower() in {"entity_resolution_owner", "analytical_owner"}
        or action.action in {"finalize_final_report", "recover_final_report", "preflight_final_report"}
    ):
        # Persisted role prompts provide context, but cannot replace the
        # action-specific safety contract. Keep the mandatory sequence last
        # so it has deterministic precedence in the plain-text prompt.
        guidance = f"{custom_guidance}\n\n{mandatory_guidance}"
    else:
        guidance = custom_guidance or mandatory_guidance
    payload = json.dumps(action.to_dict(), sort_keys=True, ensure_ascii=False)
    binding_text = ""
    if skill_binding:
        binding_text = (
            "\n"
            "Exact production skill binding (mandatory; do not resolve a global or named alternative):\n"
            f"Skill path: {skill_binding.get('skill_path')}\n"
            f"Skill version: {skill_binding.get('skill_version')}\n"
            f"Core version: {skill_binding.get('core_version')}\n"
            f"Skill release SHA-256: {skill_binding.get('skill_sha256')}\n"
            "Use only this exact path and release identity for the role instructions.\n"
        )
    return (
        "You are the assigned Auto Foundry role for exactly one Planner action.\n"
        "Use only existing public Auto Foundry APIs and the supplied RunContext.\n"
        "Persist the real phase state through those APIs; the Coordinator will reread Planner state.\n"
        "Your stdout/stderr or JSON is diagnostic only and is never evidence of success.\n"
        "Do not spawn or delegate subagents. Do not edit repository source, tests, or configuration files; "
        "make all run changes through public APIs.\n"
        "Do not edit item_state.json, run_state.json, planner files, coordinator files, manifests, "
        "review packets, integration records, or report internals directly. Do not emit an internal "
        "result envelope or claim completion. Exit 0 only after public API calls return; explain "
        "failures and use a nonzero exit when the action could not be performed.\n\n"
        f"Run root: {context.run_root}\n"
        f"Run id: {context.run_id}\n"
        f"Idempotency key (for public API calls): {idempotency_key}\n"
        f"Planner action: {payload}\n\n"
        f"{binding_text}"
        f"Role-specific contract: {guidance}\n"
    )


@dataclass(frozen=True)
class CodexExecConfig:
    """Configuration for one plain-text ``codex exec`` role process."""

    binary: str = "codex"
    model: str | None = None
    profile: str | None = None
    sandbox: str = "workspace-write"
    timeout_seconds: float | None = None
    ephemeral: bool = True
    role_prompts: Mapping[str, str] = field(default_factory=dict)
    role_models: Mapping[str, str] = field(default_factory=dict)
    role_profiles: Mapping[str, str] = field(default_factory=dict)
    role_sandboxes: Mapping[str, str] = field(default_factory=dict)
    role_timeouts: Mapping[str, float] = field(default_factory=dict)
    # A production Codex invocation is bound to one materialized release
    # tree.  ``None`` remains useful for direct in-process adapter tests and
    # non-Codex command adapters; RunCoordinator validates the binding before
    # configuring a persisted Codex role.
    skill_path: str | None = None
    skill_version: str | None = None
    core_version: str | None = None
    skill_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "binary", _text(self.binary, "codex binary"))
        if self.model is not None:
            object.__setattr__(self, "model", _text(self.model, "model"))
        if self.profile is not None:
            object.__setattr__(self, "profile", _text(self.profile, "profile"))
        sandbox = _text(self.sandbox, "sandbox").lower()
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("sandbox must be read-only, workspace-write, or danger-full-access")
        object.__setattr__(self, "sandbox", sandbox)
        if self.timeout_seconds is not None and (isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or self.timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(self.ephemeral, bool):
            raise TypeError("ephemeral must be a boolean")
        for name in ("role_prompts", "role_models", "role_profiles", "role_sandboxes", "role_timeouts"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping")
        object.__setattr__(self, "role_prompts", {str(k): _text(v, "role prompt") for k, v in self.role_prompts.items()})
        object.__setattr__(self, "role_models", {str(k): _text(v, "role model") for k, v in self.role_models.items()})
        object.__setattr__(self, "role_profiles", {str(k): _text(v, "role profile") for k, v in self.role_profiles.items()})
        sandboxes = {str(k): _text(v, "role sandbox").lower() for k, v in self.role_sandboxes.items()}
        if any(value not in {"read-only", "workspace-write", "danger-full-access"} for value in sandboxes.values()):
            raise ValueError("invalid role sandbox")
        object.__setattr__(self, "role_sandboxes", sandboxes)
        timeouts: dict[str, float] = {}
        for key, value in self.role_timeouts.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"invalid role timeout for {key}")
            timeouts[str(key)] = float(value)
        object.__setattr__(self, "role_timeouts", timeouts)
        for name in ("skill_path", "skill_version", "core_version", "skill_sha256"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, name))
        if self.skill_sha256 is not None and not _is_sha256(self.skill_sha256):
            raise ValueError("skill_sha256 must be a lowercase SHA-256 hex digest")
        if self.has_skill_binding:
            self.validate_skill_binding(required=True, verify_active=False)

    def _role_setting(self, mapping: Mapping[str, Any], role: str, default: Any = None) -> Any:
        return mapping.get(role, default)

    def for_role(self, role: str) -> "CodexExecConfig":
        return CodexExecConfig(
            binary=self.binary,
            model=self._role_setting(self.role_models, role, self.model),
            profile=self._role_setting(self.role_profiles, role, self.profile),
            sandbox=self._role_setting(self.role_sandboxes, role, self.sandbox),
            timeout_seconds=self._role_setting(self.role_timeouts, role, self.timeout_seconds),
            ephemeral=self.ephemeral,
            role_prompts=self.role_prompts,
            role_models=self.role_models,
            role_profiles=self.role_profiles,
            role_sandboxes=self.role_sandboxes,
            role_timeouts=self.role_timeouts,
            skill_path=self.skill_path,
            skill_version=self.skill_version,
            core_version=self.core_version,
            skill_sha256=self.skill_sha256,
        )

    @property
    def has_skill_binding(self) -> bool:
        return any(value is not None for value in (self.skill_path, self.skill_version, self.core_version, self.skill_sha256))

    def validate_skill_binding(
        self,
        *,
        required: bool = True,
        verify_active: bool = True,
        repo_root: Path | None = None,
        role_cwd: Path | None = None,
    ) -> None:
        """Fail closed when the bound production skill is absent or drifts."""

        values = (self.skill_path, self.skill_version, self.core_version, self.skill_sha256)
        if not self.has_skill_binding:
            if required:
                raise CoordinatorIntegrityError("Codex execution requires an exact skill binding")
            return
        if any(value is None for value in values):
            raise CoordinatorIntegrityError("Codex skill binding is incomplete")
        assert self.skill_path is not None
        assert self.skill_version is not None
        assert self.core_version is not None
        assert self.skill_sha256 is not None
        if self.skill_version != PRODUCTION_SKILL_VERSION:
            raise CoordinatorIntegrityError("Codex skill version is not the production release")
        if self.core_version != PRODUCTION_CORE_VERSION:
            raise CoordinatorIntegrityError("Codex core version is not the production release")
        if verify_active:
            installed = resolve_production_skill_binding(
                repo_root=repo_root or Path(__file__).resolve().parents[2],
                role_cwd=role_cwd,
            )
            if any(
                getattr(self, field_name) != installed[field_name]
                for field_name in ("skill_path", "skill_version", "core_version", "skill_sha256")
            ):
                raise CoordinatorIntegrityError("Codex skill binding does not match the single active installed skill")
        raw_path = Path(self.skill_path).expanduser()
        if not raw_path.is_absolute() or raw_path.is_symlink():
            raise CoordinatorIntegrityError("Codex skill path must be an absolute non-symlink path")
        try:
            resolved = raw_path.resolve(strict=True)
        except OSError as exc:
            raise CoordinatorIntegrityError("Codex skill path is unavailable") from exc
        if resolved != raw_path or resolved.is_symlink() or not resolved.is_dir():
            raise CoordinatorIntegrityError("Codex skill path is not canonical or regular")
        _validate_skill_frontmatter(resolved, skill_version=self.skill_version, core_version=self.core_version)
        actual = _sha256_bytes(_skill_release_bytes(resolved))
        if actual != self.skill_sha256:
            raise CoordinatorIntegrityError("Codex skill bytes do not match the persisted release hash")

    def to_dict(self) -> dict[str, Any]:
        return {
            "binary": self.binary,
            "model": self.model,
            "profile": self.profile,
            "sandbox": self.sandbox,
            "timeout_seconds": self.timeout_seconds,
            "ephemeral": self.ephemeral,
            "role_prompts": dict(self.role_prompts),
            "role_models": dict(self.role_models),
            "role_profiles": dict(self.role_profiles),
            "role_sandboxes": dict(self.role_sandboxes),
            "role_timeouts": dict(self.role_timeouts),
            "skill_path": self.skill_path,
            "skill_version": self.skill_version,
            "core_version": self.core_version,
            "skill_sha256": self.skill_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "CodexExecConfig":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise TypeError("codex_exec must be a mapping")
        allowed = {
            "binary", "model", "profile", "sandbox", "timeout_seconds", "ephemeral",
            "role_prompts", "role_models", "role_profiles", "role_sandboxes", "role_timeouts",
            "skill_path", "skill_version", "core_version", "skill_sha256",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError("codex_exec contains unknown/deprecated fields: " + ", ".join(unknown))
        return cls(**{key: value[key] for key in allowed if key in value})


class CommandRoleAdapter:
    """Run one local role command with a plain-text prompt."""

    def __init__(self, command: Sequence[str] | str, *, timeout_seconds: float | None = None) -> None:
        self.command = _command_tuple(command, "role command")
        if not self.command:
            raise ValueError("role command must not be empty")
        self.timeout_seconds = timeout_seconds

    def __call__(self, action: PlannerAction, *, idempotency_key: str, context: RunContext) -> RoleExecution:
        prompt = build_role_prompt(action, context=context, idempotency_key=idempotency_key)
        try:
            completed = subprocess.run(
                list(self.command),
                input=prompt,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
                cwd=context.run_root if context.run_root.exists() else None,
            )
        except subprocess.TimeoutExpired as exc:
            return RoleExecution(exit_code=None, output=_safe_text(exc.stdout), error=_safe_text(exc.stderr), timed_out=True)
        except Exception as exc:
            return RoleExecution(exit_code=1, error=f"role command invocation failed: {exc}")
        return RoleExecution(
            exit_code=completed.returncode,
            output=_safe_text(completed.stdout),
            error=_safe_text(completed.stderr),
        )


class CodexRoleAdapter:
    """Invoke one Codex role process and stream privacy-safe lifecycle events."""

    def __init__(
        self,
        context: RunContext,
        config: CodexExecConfig | Mapping[str, Any] | None = None,
        *,
        require_skill_binding: bool = False,
    ) -> None:
        self.context = context
        self.config = config if isinstance(config, CodexExecConfig) else CodexExecConfig.from_dict(config)
        self.require_skill_binding = bool(require_skill_binding)
        if self.require_skill_binding:
            self.config.validate_skill_binding(
                required=True,
                verify_active=True,
                repo_root=Path(__file__).resolve().parents[2],
                role_cwd=context.run_root,
            )

    def __call__(self, action: PlannerAction, *, idempotency_key: str, context: RunContext) -> RoleExecution:
        try:
            config = self.config.for_role(action.role)
            config.validate_skill_binding(
                required=self.require_skill_binding,
                verify_active=self.require_skill_binding,
                repo_root=Path(__file__).resolve().parents[2],
                role_cwd=context.run_root,
            )
        except CoordinatorIntegrityError as exc:
            # This is a pre-subprocess admission failure.  Keep it in the
            # durable role diagnostic channel when called from a coordinator,
            # while guaranteeing that no Codex process is started.
            return RoleExecution(exit_code=1, error=f"codex skill binding rejected: {exc}")
        prompt = build_role_prompt(
            action,
            context=context,
            idempotency_key=idempotency_key,
            custom=config.role_prompts.get(action.role),
            skill_binding=config.to_dict() if config.has_skill_binding else None,
        )
        with tempfile.TemporaryDirectory(prefix="auto-foundry-codex-") as temporary:
            output_path = Path(temporary) / "last-message.txt"
            # Run roots are durable analytical workspaces, not source-code
            # repositories.  Keep the normal workspace sandbox while
            # explicitly allowing this intentional non-Git working tree.
            argv = [config.binary, "exec", "--skip-git-repo-check"]
            if config.ephemeral:
                argv.append("--ephemeral")
            argv.extend(["--sandbox", config.sandbox])
            if config.model:
                argv.extend(["--model", config.model])
            if config.profile:
                argv.extend(["--profile", config.profile])
            # JSONL is transport telemetry only.  It is normalized into the
            # Control Center lifecycle allowlist and never treated as phase
            # success authority.  Deliberately no --output-schema.
            argv.extend(["--json", "--output-last-message", str(output_path), "-"])
            process: subprocess.Popen[bytes] | None = None
            stderr_bytes = bytearray()
            lifecycle_errors: list[str] = []
            writer = LifecycleEventWriter(context.run_root)
            root_thread: str | None = None

            def read_stdout() -> None:
                nonlocal root_thread
                assert process is not None and process.stdout is not None
                while True:
                    line = process.stdout.readline(MAX_CODEX_EVENT_BYTES + 1)
                    if not line:
                        return
                    if len(line) > MAX_CODEX_EVENT_BYTES or not line.endswith(b"\n"):
                        # Discard the rest of one oversized JSONL record.  A
                        # later well-formed record remains observable.
                        while line and not line.endswith(b"\n"):
                            line = process.stdout.readline(MAX_CODEX_EVENT_BYTES + 1)
                        continue
                    try:
                        root_thread, rows = normalize_codex_json_line(
                            line,
                            root_thread=root_thread,
                            root_invocation_id=idempotency_key,
                        )
                        for row in rows:
                            writer.append(row)
                    except Exception:
                        # Monitoring is best-effort and must never terminate
                        # or alter the analytical role invocation.
                        lifecycle_errors.append("lifecycle telemetry unavailable")

            def read_stderr() -> None:
                assert process is not None and process.stderr is not None
                while True:
                    chunk = process.stderr.read(8192)
                    if not chunk:
                        return
                    remaining = MAX_ROLE_DIAGNOSTIC_BYTES - len(stderr_bytes)
                    if remaining > 0:
                        stderr_bytes.extend(chunk[:remaining])

            def feed_stdin() -> None:
                assert process is not None and process.stdin is not None
                try:
                    process.stdin.write(prompt.encode("utf-8"))
                    process.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except (OSError, ValueError):
                        pass

            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=context.run_root if context.run_root.exists() else None,
                )
            except Exception as exc:
                return RoleExecution(exit_code=1, error=f"codex exec invocation failed: {exc}")

            stdout_thread = Thread(target=read_stdout, name="codex-jsonl-reader", daemon=True)
            stderr_thread = Thread(target=read_stderr, name="codex-stderr-reader", daemon=True)
            stdin_thread = Thread(target=feed_stdin, name="codex-stdin-writer", daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            stdin_thread.start()
            timed_out = False
            try:
                returncode = process.wait(timeout=config.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.terminate()
                try:
                    returncode = process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    returncode = process.wait(timeout=3)
            finally:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except (OSError, ValueError):
                        pass
                stdin_thread.join(timeout=2)
                # Once the child is reaped, stdout/stderr reach EOF.  Let the
                # readers drain before closing their handles so a fast child
                # cannot race with cleanup and produce a thread exception.
                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            pass
            output = ""
            if output_path.is_file() and not output_path.is_symlink():
                try:
                    output = _safe_text(output_path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    output = ""
            error = _safe_text(bytes(stderr_bytes))
            if lifecycle_errors and not error:
                error = lifecycle_errors[0]
            return RoleExecution(exit_code=None if timed_out else returncode, output=output, error=error, timed_out=timed_out)


@dataclass(frozen=True)
class CoordinatorStatus:
    """Read-only projection of the persisted coordinator state."""

    run_id: str
    generation_id: str
    status: str
    phase: str
    next_action: Mapping[str, Any] | None
    owner: str | None
    lease_expires_at: float | None
    diagnostics: tuple[Mapping[str, Any], ...]
    last_event_seq: int
    last_event_hash: str
    publication_ready: bool
    publication_enabled: bool
    no_progress_count: int = 0
    next_actions: tuple[Mapping[str, Any], ...] = ()
    active_dispatches: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generation_id": self.generation_id,
            "status": self.status,
            "phase": self.phase,
            "next_action": dict(self.next_action) if self.next_action else None,
            "next_actions": [dict(value) for value in self.next_actions],
            "active_dispatches": [dict(value) for value in self.active_dispatches],
            "owner": self.owner,
            "lease_expires_at": self.lease_expires_at,
            "diagnostics": [dict(item) for item in self.diagnostics],
            "last_event_seq": self.last_event_seq,
            "last_event_hash": self.last_event_hash,
            "publication_ready": self.publication_ready,
            "publication_enabled": self.publication_enabled,
            "no_progress_count": self.no_progress_count,
        }


DEFAULT_MAX_WORKERS = 8


class RunCoordinator:
    """Event-driven launcher for the Planner's complete ready set.

    Planner ordering is retained when dispatch records are created.  Role
    processes execute outside the one control-plane lock, and a fresh Planner
    read happens after every completed role.  Transport output is diagnostic;
    only the public Planner/state projection determines progress.
    """

    def __init__(
        self,
        context: RunContext,
        *,
        adapters: Mapping[str, RoleAdapter] | MappingRoleAdapter | None = None,
        role_runner: Any | None = None,
        planner_provider: PlannerActionProvider | None = None,
        planner: Callable[[Mapping[str, Any]], Sequence[PlannerAction]] | None = None,
        owner_id: str | None = None,
        clock: Callable[[], float] | None = None,
        failpoint: Callable[[str], None] | None = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> None:
        if not isinstance(context, RunContext):
            raise TypeError("RunCoordinator requires a RunContext")
        if planner_provider is not None and planner is not None:
            raise TypeError("provide planner_provider or planner, not both")
        if role_runner is not None and adapters is not None:
            raise TypeError("provide role_runner or adapters, not both")
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers <= 0:
            raise ValueError("max_workers must be a positive integer")
        self.context = context
        self.control_plane = context.resolve_run_path(CONTROL_PLANE_DIRNAME)
        self.spec_path = self.control_plane / COORDINATOR_SPEC_FILENAME
        self.state_path = self.control_plane / COORDINATOR_STATE_FILENAME
        self.events_path = self.control_plane / COORDINATOR_EVENTS_FILENAME
        self.lock_path = self.control_plane / COORDINATOR_LOCK_FILENAME
        if isinstance(adapters, MappingRoleAdapter):
            self.role_runner: Any = adapters
        elif adapters is not None:
            self.role_runner = MappingRoleAdapter(adapters)
        elif role_runner is not None:
            self.role_runner = role_runner
        else:
            self.role_runner = None
        self._custom_role_runner = self.role_runner is not None
        if planner_provider is not None:
            self.planner_provider: Any = planner_provider
        elif planner is not None:
            self.planner_provider = _CallablePlannerProvider(planner)
        else:
            self.planner_provider = RequirementPlannerProvider()
        self.owner_id = _text(owner_id or f"coordinator-{uuid.uuid4().hex}", "owner_id")
        self._clock = clock or time.time
        self._failpoint = failpoint
        self._spec: CoordinatorRunSpec | None = None
        self._legacy_pending = False
        self.max_workers = max_workers
        self._executor: ThreadPoolExecutor | None = None
        self._futures: dict[str, Future[Any]] = {}
        self._future_entries: dict[str, dict[str, Any]] = {}
        self._last_completion_same = False

    @contextmanager
    def _locked(self, *, create: bool = True) -> Iterable[None]:
        """Hold the sole coordinator lock for state/event/checkpoint writes."""

        if self.control_plane.is_symlink():
            raise CoordinatorIntegrityError("coordinator control plane cannot be a symlink")
        if create:
            self.control_plane.mkdir(parents=True, exist_ok=True)
        elif not self.control_plane.is_dir():
            raise CoordinatorIntegrityError("coordinator control plane is missing")
        if self.lock_path.is_symlink():
            raise CoordinatorIntegrityError("coordinator lock cannot be a symlink")
        with self.lock_path.open("a+b") as stream:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _spec_hash(self, spec: CoordinatorRunSpec) -> str:
        return _sha256_value(spec.to_dict())

    def _write_spec(self, spec: CoordinatorRunSpec) -> None:
        _atomic_json(
            self.spec_path,
            {
                "schema_version": COORDINATOR_SCHEMA_VERSION,
                "kind": "run_coordinator_spec",
                **spec.to_dict(),
            },
        )

    @property
    def legacy_archive_dir(self) -> Path:
        return self.control_plane / COORDINATOR_LEGACY_ARCHIVE_DIRNAME

    @property
    def legacy_intent_path(self) -> Path:
        return self.control_plane / COORDINATOR_LEGACY_INTENT_FILENAME

    @property
    def legacy_staging_dir(self) -> Path:
        return self.control_plane / COORDINATOR_LEGACY_STAGING_DIRNAME

    @staticmethod
    def _looks_like_legacy_spec(value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        return "run_spec" in value or "lineage_binding" in value

    @staticmethod
    def _is_legacy_spec_document(value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        return (
            set(value) == _LEGACY_SPEC_KEYS
            and value.get("schema_version") == 1
            and value.get("kind") == "run_coordinator_spec"
        )

    @staticmethod
    def _legacy_spec_from_document(value: Mapping[str, Any]) -> CoordinatorRunSpec:
        run_spec = value.get("run_spec")
        if not isinstance(run_spec, Mapping):
            raise CoordinatorIntegrityError("legacy coordinator run_spec must be an object")
        if set(run_spec) != _LEGACY_RUN_SPEC_KEYS:
            raise CoordinatorIntegrityError("legacy coordinator run_spec shape is not the known G5 version")
        lineage = value.get("lineage_binding")
        if not isinstance(lineage, Mapping) or set(lineage) != _LEGACY_LINEAGE_KEYS:
            raise CoordinatorIntegrityError("legacy coordinator lineage shape is not the known G5 version")
        spec_hash = value.get("spec_hash")
        if not _is_sha256(spec_hash):
            raise CoordinatorIntegrityError("legacy coordinator spec_hash is invalid")
        if _sha256_bytes(_json_bytes(run_spec)) != spec_hash:
            raise CoordinatorIntegrityError("legacy coordinator spec_hash does not match run_spec")
        for name in ("active_generation_pointer_hash", "manifest_hash", "plan_hash", "planner_hash"):
            if not _is_sha256(lineage.get(name)):
                raise CoordinatorIntegrityError(f"legacy coordinator lineage {name} is invalid")
        if lineage.get("generation_id") != run_spec.get("generation_id"):
            raise CoordinatorIntegrityError("legacy coordinator generation binding mismatch")
        if lineage.get("planner_ref") != run_spec.get("planner_ref"):
            raise CoordinatorIntegrityError("legacy coordinator planner reference mismatch")
        if lineage.get("planner_hash") != run_spec.get("planner_hash"):
            raise CoordinatorIntegrityError("legacy coordinator planner hash mismatch")
        if lineage.get("plan_hash") != run_spec.get("planner_hash"):
            raise CoordinatorIntegrityError("legacy coordinator plan hash mismatch")
        try:
            codex_exec = run_spec.get("codex_exec") or {}
            if not isinstance(codex_exec, Mapping):
                raise TypeError("codex_exec must be a mapping")
            publication_policy = run_spec.get("publication_policy")
            if publication_policy is None:
                policy = run_spec.get("policy")
                if not isinstance(policy, Mapping):
                    raise TypeError("legacy policy must be a mapping")
                publication_policy = {"enabled": bool(policy.get("publication_enabled", False))}
            if not isinstance(publication_policy, Mapping):
                raise TypeError("publication_policy must be a mapping")
            return CoordinatorRunSpec(
                run_id=run_spec["run_id"],
                generation_id=run_spec["generation_id"],
                planner_ref=run_spec["planner_ref"],
                planner_hash=run_spec["planner_hash"],
                role_dispatch_command=run_spec.get("role_dispatch_command") or (),
                publication_policy=publication_policy,
                codex_exec=codex_exec,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CoordinatorIntegrityError("legacy coordinator run_spec is invalid") from exc

    @staticmethod
    def _read_regular_bytes(path: Path, description: str) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise CoordinatorIntegrityError(f"legacy coordinator {description} is missing or not regular")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise CoordinatorIntegrityError(f"legacy coordinator {description} cannot be read") from exc

    def _validate_legacy_archive_dir(self, archive: Path, expected: Mapping[str, bytes]) -> None:
        if archive.is_symlink() or not archive.is_dir():
            raise CoordinatorIntegrityError("legacy coordinator archive is not a directory")
        expected_files = set(expected) | {"manifest.json"}
        actual = {entry.name for entry in archive.iterdir()}
        if actual != expected_files:
            raise CoordinatorIntegrityError("legacy coordinator archive layout is incomplete")
        manifest_path = archive / "manifest.json"
        manifest_raw = self._read_regular_bytes(manifest_path, "archive manifest")
        try:
            manifest = _strict_json_loads(manifest_raw)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise CoordinatorIntegrityError("legacy coordinator archive manifest is invalid") from exc
        if not isinstance(manifest, Mapping) or set(manifest) != {"schema_version", "kind", "files"}:
            raise CoordinatorIntegrityError("legacy coordinator archive manifest shape is invalid")
        if manifest.get("schema_version") != 1 or manifest.get("kind") != "legacy_coordinator_archive":
            raise CoordinatorIntegrityError("legacy coordinator archive manifest version is invalid")
        files = manifest.get("files")
        if not isinstance(files, Mapping) or set(files) != set(expected):
            raise CoordinatorIntegrityError("legacy coordinator archive manifest files are invalid")
        for name, source_bytes in expected.items():
            archived = self._read_regular_bytes(archive / name, f"archive {name}")
            if archived != source_bytes:
                raise CoordinatorIntegrityError(f"legacy coordinator archive {name} is not byte exact")
            descriptor = files.get(name)
            if (
                not isinstance(descriptor, Mapping)
                or set(descriptor) != {"sha256", "size"}
                or descriptor.get("sha256") != _sha256_bytes(source_bytes)
                or descriptor.get("size") != len(source_bytes)
            ):
                raise CoordinatorIntegrityError(f"legacy coordinator archive hash for {name} is invalid")

    def _validate_legacy_archive(self, expected: Mapping[str, bytes]) -> None:
        self._validate_legacy_archive_dir(self.legacy_archive_dir, expected)

    @staticmethod
    def _legacy_manifest(raw: Mapping[str, bytes]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "legacy_coordinator_archive",
            "files": {
                name: {"sha256": _sha256_bytes(payload), "size": len(payload)}
                for name, payload in raw.items()
            },
        }

    def _legacy_intent(self, spec_document: Mapping[str, Any], raw: Mapping[str, bytes], reason: str) -> dict[str, Any]:
        spec = self._legacy_spec_from_document(spec_document)
        return {
            "schema_version": 1,
            "kind": "legacy_import_intent",
            "run_id": spec.run_id,
            "generation_id": spec.generation_id,
            "source_spec_hash": str(spec_document.get("spec_hash")),
            "source_files": {
                name: {"sha256": _sha256_bytes(payload), "size": len(payload)}
                for name, payload in raw.items()
            },
            "reason": reason,
        }

    def _validate_legacy_intent(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version", "kind", "run_id", "generation_id", "source_spec_hash", "source_files", "reason"
        }:
            raise CoordinatorIntegrityError("legacy import intent shape is invalid")
        if value.get("schema_version") != 1 or value.get("kind") != "legacy_import_intent":
            raise CoordinatorIntegrityError("legacy import intent version is invalid")
        if not _is_sha256(value.get("source_spec_hash")):
            raise CoordinatorIntegrityError("legacy import intent source hash is invalid")
        files = value.get("source_files")
        if not isinstance(files, Mapping) or set(files) != set(_LEGACY_CONTROL_FILES - {COORDINATOR_LOCK_FILENAME}):
            raise CoordinatorIntegrityError("legacy import intent source files are invalid")
        for name, descriptor in files.items():
            if not isinstance(descriptor, Mapping) or set(descriptor) != {"sha256", "size"}:
                raise CoordinatorIntegrityError(f"legacy import intent descriptor is invalid: {name}")
            if not _is_sha256(descriptor.get("sha256")) or not isinstance(descriptor.get("size"), int) or descriptor.get("size") < 0:
                raise CoordinatorIntegrityError(f"legacy import intent hash is invalid: {name}")
        _text(value.get("run_id"), "legacy import intent run_id")
        _text(value.get("generation_id"), "legacy import intent generation_id")
        _text(value.get("reason"), "legacy import intent reason")
        return dict(value)

    def _raw_matches_intent(self, raw: Mapping[str, bytes], intent: Mapping[str, Any]) -> bool:
        files = intent.get("source_files")
        if not isinstance(files, Mapping) or set(files) != set(raw):
            return False
        return all(
            isinstance(files[name], Mapping)
            and files[name].get("sha256") == _sha256_bytes(payload)
            and files[name].get("size") == len(payload)
            for name, payload in raw.items()
        )

    def _read_legacy_archive_raw(self, archive: Path, intent: Mapping[str, Any]) -> dict[str, bytes]:
        names = tuple(sorted(_LEGACY_CONTROL_FILES - {COORDINATOR_LOCK_FILENAME}))
        raw = {name: self._read_regular_bytes(archive / name, f"archive {name}") for name in names}
        self._validate_legacy_archive_dir(archive, raw)
        if not self._raw_matches_intent(raw, intent):
            raise CoordinatorIntegrityError("legacy import archive does not match intent")
        return raw

    @staticmethod
    def _staging_is_owned_partial(path: Path) -> bool:
        allowed = set(_LEGACY_CONTROL_FILES - {COORDINATOR_LOCK_FILENAME}) | {"manifest.json"}
        try:
            entries = {entry.name for entry in path.iterdir()}
        except OSError as exc:
            raise CoordinatorIntegrityError("legacy import staging cannot be inspected") from exc
        if entries - allowed:
            raise CoordinatorIntegrityError("legacy import staging contains unrelated files")
        # A complete directory is not a partial process-death staging copy.
        # Callers must reject it rather than deleting a potentially useful
        # verified copy when the final archive already exists.
        return entries != allowed

    def _validate_legacy_snapshot(self) -> tuple[CoordinatorRunSpec, dict[str, bytes]]:
        """Validate the known G5 snapshot without changing any bytes."""

        if not self.control_plane.is_dir() or self.control_plane.is_symlink():
            raise CoordinatorIntegrityError("legacy coordinator control plane is missing")
        entries = {entry.name for entry in self.control_plane.iterdir()}
        allowed = set(_LEGACY_CONTROL_FILES) | {
            COORDINATOR_LEGACY_ARCHIVE_DIRNAME,
            COORDINATOR_LEGACY_INTENT_FILENAME,
            COORDINATOR_LEGACY_STAGING_DIRNAME,
        }
        if not set(_LEGACY_CONTROL_FILES).issubset(entries):
            raise CoordinatorIntegrityError("legacy coordinator control plane is incomplete")
        unexpected = entries - allowed
        if unexpected:
            raise CoordinatorIntegrityError("legacy coordinator control plane has unexpected files")
        raw = {
            COORDINATOR_SPEC_FILENAME: self._read_regular_bytes(self.spec_path, "specification"),
            COORDINATOR_STATE_FILENAME: self._read_regular_bytes(self.state_path, "state"),
            COORDINATOR_EVENTS_FILENAME: self._read_regular_bytes(self.events_path, "event log"),
        }
        try:
            spec_document = _strict_json_loads(raw[COORDINATOR_SPEC_FILENAME])
            state = _strict_json_loads(raw[COORDINATOR_STATE_FILENAME])
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise CoordinatorIntegrityError("legacy coordinator JSON is invalid") from exc
        if not self._is_legacy_spec_document(spec_document):
            raise CoordinatorIntegrityError("coordinator specification is not the known G5 legacy wrapper")
        spec = self._legacy_spec_from_document(spec_document)
        if not isinstance(state, Mapping) or not _LEGACY_STATE_REQUIRED_KEYS.issubset(set(state)):
            raise CoordinatorIntegrityError("legacy coordinator state shape is not the known G5 version")
        if state.get("schema_version") != 1 or state.get("kind") != "run_coordinator_state":
            raise CoordinatorIntegrityError("legacy coordinator state version is invalid")
        if (
            state.get("run_id") != spec.run_id
            or state.get("generation_id") != spec.generation_id
            or state.get("planner_ref") != spec.planner_ref
            or state.get("planner_hash") != spec.planner_hash
            or state.get("spec_hash") != spec_document.get("spec_hash")
            or state.get("spec_ref") != f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_SPEC_FILENAME}"
        ):
            raise CoordinatorIntegrityError("legacy coordinator state binding mismatch")
        lineage = state.get("lineage_binding")
        if lineage != spec_document.get("lineage_binding"):
            raise CoordinatorIntegrityError("legacy coordinator state lineage mismatch")
        if not _is_sha256(state.get("last_event_hash")):
            raise CoordinatorIntegrityError("legacy coordinator state event hash is invalid")
        events_raw = raw[COORDINATOR_EVENTS_FILENAME]
        lines = events_raw.splitlines(keepends=True)
        if not lines or any(not line.endswith(b"\n") or not line.strip() for line in lines):
            raise CoordinatorIntegrityError("legacy coordinator event log is empty or partial")
        events: list[Mapping[str, Any]] = []
        previous_hash = ""
        for index, line in enumerate(lines, start=1):
            try:
                event = _strict_json_loads(line)
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                raise CoordinatorIntegrityError("legacy coordinator event log is invalid") from exc
            if not isinstance(event, Mapping) or _legacy_json_bytes(event) != line:
                raise CoordinatorIntegrityError("legacy coordinator event log is not canonical")
            if (
                not isinstance(event, Mapping) or not _LEGACY_EVENT_REQUIRED_KEYS.issubset(set(event))
            ):
                raise CoordinatorIntegrityError("legacy coordinator event shape is not the known G5 version")
            if (
                event.get("schema_version") != 1
                or event.get("kind") != "run_coordinator_event"
                or event.get("seq") != index
                or event.get("previous_event_hash", "") != previous_hash
                or event.get("run_id") != spec.run_id
                or event.get("generation_id") != spec.generation_id
                or event.get("planner_ref") != spec.planner_ref
                or event.get("planner_hash") != spec.planner_hash
                or not isinstance(event.get("after_state"), Mapping)
                or not _is_sha256(event.get("event_hash"))
                or not _is_sha256(event.get("state_hash"))
            ):
                raise CoordinatorIntegrityError("legacy coordinator event binding is invalid")
            after_state = event["after_state"]
            if after_state.get("last_event_seq") != index or after_state.get("last_event_hash", "") != "":
                raise CoordinatorIntegrityError("legacy coordinator event checkpoint is invalid")
            if _legacy_state_hash(after_state) != event["state_hash"]:
                raise CoordinatorIntegrityError("legacy coordinator event state hash mismatch")
            unsigned = dict(event)
            event_hash = unsigned.pop("event_hash")
            if _legacy_hash(unsigned) != event_hash:
                raise CoordinatorIntegrityError("legacy coordinator event hash mismatch")
            events.append(event)
            previous_hash = event_hash
        if state.get("last_event_seq") != len(events) or state.get("last_event_hash") != events[-1].get("event_hash"):
            raise CoordinatorIntegrityError("legacy coordinator state does not match event tail")
        checkpoint = dict(_canonical(events[-1]["after_state"]))
        checkpoint["last_event_seq"] = len(events)
        checkpoint["last_event_hash"] = events[-1]["event_hash"]
        if _legacy_state_hash(state) != _legacy_state_hash(checkpoint):
            raise CoordinatorIntegrityError("legacy coordinator checkpoint state mismatch")
        if self.legacy_archive_dir.exists():
            self._validate_legacy_archive(raw)
        return spec, raw

    def _legacy_failpoint(self, name: str) -> None:
        if self._failpoint is not None:
            self._failpoint(name)

    def _legacy_source_locked(self, intent: Mapping[str, Any]) -> tuple[CoordinatorRunSpec, dict[str, bytes]]:
        """Load the verified legacy source for an interrupted import."""

        archive = self.legacy_archive_dir
        staging = self.legacy_staging_dir
        if archive.is_symlink():
            raise CoordinatorIntegrityError("legacy coordinator archive cannot be a symlink")
        raw: dict[str, bytes]
        if archive.exists():
            raw = self._read_legacy_archive_raw(archive, intent)
            if staging.exists():
                if staging.is_symlink() or not staging.is_dir():
                    raise CoordinatorIntegrityError("legacy import staging is not a regular directory")
                try:
                    self._read_legacy_archive_raw(staging, intent)
                except CoordinatorIntegrityError:
                    if not self._staging_is_owned_partial(staging):
                        raise CoordinatorIntegrityError("legacy import staging is not an owned partial")
                    shutil.rmtree(staging)
                else:
                    raise CoordinatorIntegrityError("legacy import staging is complete alongside archive")
        elif staging.exists():
            if staging.is_symlink() or not staging.is_dir():
                raise CoordinatorIntegrityError("legacy import staging is not a regular directory")
            try:
                raw = self._read_legacy_archive_raw(staging, intent)
            except CoordinatorIntegrityError:
                # The staging directory is owned by this bounded transaction;
                # a partial process-death copy can be rebuilt from the source.
                if not self._staging_is_owned_partial(staging):
                    raise CoordinatorIntegrityError("legacy import staging is not an owned partial")
                shutil.rmtree(staging)
                raw = {}
            if not raw:
                source = {
                    name: self._read_regular_bytes(self.control_plane / name, f"{name} source")
                    for name in sorted(_LEGACY_CONTROL_FILES - {COORDINATOR_LOCK_FILENAME})
                }
                if not self._raw_matches_intent(source, intent):
                    raise CoordinatorIntegrityError("legacy import source changed during recovery")
                self._validate_legacy_snapshot()
                raw = source
        else:
            source = {
                name: self._read_regular_bytes(self.control_plane / name, f"{name} source")
                for name in sorted(_LEGACY_CONTROL_FILES - {COORDINATOR_LOCK_FILENAME})
            }
            if not self._raw_matches_intent(source, intent):
                raise CoordinatorIntegrityError("legacy import source changed during recovery")
            self._validate_legacy_snapshot()
            raw = source
        try:
            document = _strict_json_loads(raw[COORDINATOR_SPEC_FILENAME])
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise CoordinatorIntegrityError("legacy import source specification is invalid") from exc
        if not self._is_legacy_spec_document(document):
            raise CoordinatorIntegrityError("legacy import source is not the known G5 wrapper")
        if document.get("spec_hash") != intent.get("source_spec_hash"):
            raise CoordinatorIntegrityError("legacy import source specification hash changed")
        spec = self._legacy_spec_from_document(document)
        if spec.run_id != intent.get("run_id") or spec.generation_id != intent.get("generation_id"):
            raise CoordinatorIntegrityError("legacy import intent lineage changed")
        return spec, raw

    def _finish_legacy_import_locked(
        self,
        intent: Mapping[str, Any],
        spec: CoordinatorRunSpec,
        raw: Mapping[str, bytes],
    ) -> CoordinatorStatus:
        """Complete an import intent. Every write is idempotent and recoverable."""

        staging = self.legacy_staging_dir
        archive = self.legacy_archive_dir
        if archive.is_symlink():
            raise CoordinatorIntegrityError("legacy coordinator archive cannot be a symlink")
        if archive.exists():
            self._read_legacy_archive_raw(archive, intent)
            if staging.exists():
                if staging.is_symlink() or not staging.is_dir():
                    raise CoordinatorIntegrityError("legacy import staging is not a directory")
                try:
                    self._read_legacy_archive_raw(staging, intent)
                except CoordinatorIntegrityError:
                    if not self._staging_is_owned_partial(staging):
                        raise CoordinatorIntegrityError("legacy import staging is not an owned partial")
                    shutil.rmtree(staging)
                else:
                    raise CoordinatorIntegrityError("legacy import staging is complete alongside archive")
        else:
            if staging.exists():
                if staging.is_symlink() or not staging.is_dir():
                    raise CoordinatorIntegrityError("legacy import staging is not a regular directory")
                try:
                    self._read_legacy_archive_raw(staging, intent)
                except CoordinatorIntegrityError:
                    if not self._staging_is_owned_partial(staging):
                        raise CoordinatorIntegrityError("legacy import staging is not an owned partial")
                    shutil.rmtree(staging)
            if not staging.exists():
                staging.mkdir()
                for name, payload in raw.items():
                    _atomic_bytes(staging / name, payload)
                    self._legacy_failpoint(f"legacy_import_after_stage_{name}")
                _atomic_json(staging / "manifest.json", self._legacy_manifest(raw))
                self._validate_legacy_archive_dir(staging, raw)
            os.replace(staging, archive)
            self._legacy_failpoint("legacy_import_after_archive_rename")
            self._validate_legacy_archive(raw)

        flat_value = _load_json(self.spec_path)
        if self._is_legacy_spec_document(flat_value):
            self._spec = spec
            self._configure_from_spec(spec)
            self._write_spec(spec)
            self._legacy_failpoint("legacy_import_after_flat_spec")
        elif isinstance(flat_value, Mapping):
            current = self._load_spec_document()
            if current.to_dict() != spec.to_dict():
                raise CoordinatorIntegrityError("legacy import current specification is unexpected")
            self._spec = current
            self._configure_from_spec(current)
        else:
            raise CoordinatorIntegrityError("legacy import current specification is missing")

        complete_state: dict[str, Any] | None = None
        try:
            candidate_state, candidate_events = self._read_replay()
            if (
                candidate_state is not None
                and candidate_events
                and candidate_events[-1].get("event") == "legacy_imported"
                and candidate_state.get("spec_hash") == self._spec_hash(spec)
            ):
                complete_state = candidate_state
        except CoordinatorError:
            complete_state = None
        if complete_state is None:
            state = self._initial_state(spec)
            state["phase"] = "ready"
            _atomic_json(self.state_path, state)
            self._legacy_failpoint("legacy_import_after_state")
            _atomic_bytes(self.events_path, b"")
            self._legacy_failpoint("legacy_import_after_events")
            archive_refs = {
                name: {
                    "ref": f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_LEGACY_ARCHIVE_DIRNAME}/{name}",
                    "sha256": _sha256_bytes(payload),
                    "size": len(payload),
                }
                for name, payload in raw.items()
            }
            manifest_hash = _sha256_bytes((archive / "manifest.json").read_bytes())
            self._append_event_locked(
                state,
                "legacy_imported",
                {
                    "reason": intent.get("reason", "legacy import recovery"),
                    "legacy_schema_version": 1,
                    "legacy_spec_hash": _strict_json_loads(raw[COORDINATOR_SPEC_FILENAME]).get("spec_hash"),
                    "archive_manifest_sha256": manifest_hash,
                    "archive": archive_refs,
                },
            )
            complete_state = self._status_state_after_import(state)
        # Verify the new chain before deleting the durable intent.
        self._load_spec_document()
        verified, events = self._read_replay()
        if verified is None or not events or events[-1].get("event") != "legacy_imported":
            raise CoordinatorIntegrityError("legacy import chain did not verify")
        self._validate_legacy_archive(raw)
        if self.legacy_intent_path.exists():
            self.legacy_intent_path.unlink()
        if staging.exists():
            if staging.is_symlink():
                raise CoordinatorIntegrityError("legacy import staging cannot be a symlink")
            if staging.is_dir():
                shutil.rmtree(staging)
        self._legacy_pending = False
        self._spec = spec
        return self._status_from_state(verified)

    @staticmethod
    def _status_state_after_import(state: dict[str, Any]) -> dict[str, Any]:
        return state

    def _recover_legacy_import_locked(self) -> CoordinatorStatus | None:
        if not self.legacy_intent_path.exists() and not self.legacy_staging_dir.exists():
            return None
        if self.legacy_intent_path.is_symlink() or not self.legacy_intent_path.is_file():
            raise CoordinatorIntegrityError("legacy import intent is not a regular file")
        intent = self._validate_legacy_intent(_load_json(self.legacy_intent_path))
        spec, raw = self._legacy_source_locked(intent)
        return self._finish_legacy_import_locked(intent, spec, raw)

    def _recover_legacy_import(self) -> None:
        if not self.legacy_intent_path.exists() and not self.legacy_staging_dir.exists():
            return
        if self.lock_path.is_symlink() or not self.lock_path.is_file():
            raise CoordinatorIntegrityError("legacy coordinator lock is missing or not regular")
        with self._locked(create=False):
            self._recover_legacy_import_locked()

    def _load_spec_document(self) -> CoordinatorRunSpec:
        value = _load_json(self.spec_path)
        if not isinstance(value, Mapping):
            raise CoordinatorIntegrityError("coordinator_spec.json is missing or invalid")
        if self._looks_like_legacy_spec(value):
            raise CoordinatorIntegrityError("legacy G5 coordinator specification requires reopen import")
        spec = CoordinatorRunSpec.from_dict(value)
        if spec.run_id != self.context.run_id:
            raise CoordinatorIntegrityError("persisted coordinator spec has a different run_id")
        return spec

    def _prepare_persisted_spec(self) -> CoordinatorRunSpec:
        value = _load_json(self.spec_path)
        if not isinstance(value, Mapping):
            raise CoordinatorIntegrityError("coordinator_spec.json is missing or invalid")
        if self._is_legacy_spec_document(value):
            spec, _ = self._validate_legacy_snapshot()
            if spec.run_id != self.context.run_id:
                raise CoordinatorIntegrityError("legacy coordinator spec has a different run_id")
            self._spec = spec
            self._legacy_pending = True
            return spec
        if self._looks_like_legacy_spec(value):
            raise CoordinatorIntegrityError("coordinator specification is a malformed legacy wrapper")
        spec = self._load_spec_document()
        self._spec = spec
        self._legacy_pending = False
        self._configure_from_spec(spec)
        return spec

    def persisted_spec(self) -> CoordinatorRunSpec:
        self._recover_legacy_import()
        if self._legacy_pending:
            assert self._spec is not None
            return self._spec
        with self._locked(create=False):
            self._recover_legacy_import_locked()
            self._recover_binding_upgrade_locked()
            return self._prepare_persisted_spec()

    def _configure_from_spec(self, spec: CoordinatorRunSpec) -> None:
        if self._custom_role_runner:
            return
        if spec.role_dispatch_command:
            self.role_runner = CommandRoleAdapter(
                spec.role_dispatch_command,
                timeout_seconds=spec.lease_ttl_seconds,
            )
        elif spec.codex_exec:
            self.role_runner = CodexRoleAdapter(
                self.context,
                CodexExecConfig.from_dict(spec.codex_exec),
                require_skill_binding=True,
            )

    def _ensure_persisted_configuration(self) -> None:
        self._recover_legacy_import()
        if self._spec is not None:
            return
        if self.spec_path.is_file() and not self.spec_path.is_symlink():
            self._prepare_persisted_spec()

    def _initial_state(self, spec: CoordinatorRunSpec) -> dict[str, Any]:
        return {
            "schema_version": COORDINATOR_SCHEMA_VERSION,
            "kind": "run_coordinator_state",
            "run_id": spec.run_id,
            "generation_id": spec.generation_id,
            "planner_ref": spec.planner_ref,
            "planner_hash": spec.planner_hash,
            "spec_hash": self._spec_hash(spec),
            "spec_ref": f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_SPEC_FILENAME}",
            "status": "ready",
            "phase": "queued",
            "active_dispatches": [],
            "last_action": None,
            "attempt": 0,
            "no_progress_count": 0,
            "last_no_progress_action": None,
            "diagnostics": [],
            "publication_policy": dict(spec.publication_policy),
            "publication_ready": False,
            "pending_plan_rebind": None,
            "pending_binding_upgrade": None,
            "last_event_seq": 0,
            "last_event_hash": "",
        }

    @staticmethod
    def _normalize_replayed_state(state: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize the persisted active-dispatch list."""

        active = state.get("active_dispatches")
        if not isinstance(active, list):
            active = []
        diagnostics = state.get("diagnostics")
        return {
            "schema_version": COORDINATOR_SCHEMA_VERSION,
            "kind": "run_coordinator_state",
            "run_id": state.get("run_id"),
            "generation_id": state.get("generation_id"),
            "planner_ref": state.get("planner_ref"),
            "planner_hash": state.get("planner_hash"),
            "spec_hash": state.get("spec_hash"),
            "spec_ref": state.get("spec_ref", f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_SPEC_FILENAME}"),
            "status": state.get("status", "ready"),
            "phase": state.get("phase", "queued"),
            "active_dispatches": [dict(_canonical(value)) for value in active if isinstance(value, Mapping)],
            "last_action": state.get("last_action") or state.get("last_completed_action"),
            "attempt": int(state.get("attempt", 0) or 0),
            "no_progress_count": int(state.get("no_progress_count", state.get("consecutive_no_progress", 0)) or 0),
            "last_no_progress_action": state.get("last_no_progress_action"),
            "diagnostics": list(diagnostics) if isinstance(diagnostics, list) else [],
            "publication_policy": dict(state.get("publication_policy") or {}) if isinstance(state.get("publication_policy") or {}, Mapping) else {},
            "publication_ready": bool(state.get("publication_ready", False)),
            "pending_plan_rebind": dict(_canonical(state.get("pending_plan_rebind"))) if isinstance(state.get("pending_plan_rebind"), Mapping) else None,
            "pending_binding_upgrade": dict(_canonical(state.get("pending_binding_upgrade"))) if isinstance(state.get("pending_binding_upgrade"), Mapping) else None,
            "last_event_seq": int(state.get("last_event_seq", 0) or 0),
            "last_event_hash": state.get("last_event_hash", ""),
        }

    def _read_replay(self, *, require_state: bool = True) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        raw_state = _load_json(self.state_path)
        events = _load_events(self.events_path)
        if raw_state is None and not events:
            if require_state:
                raise CoordinatorError("coordinator has not been started")
            return None, []
        if raw_state is not None and not isinstance(raw_state, Mapping):
            raise CoordinatorIntegrityError("coordinator state must be an object")
        for index, event in enumerate(events, start=1):
            if event.get("seq") != index:
                raise CoordinatorIntegrityError("coordinator event sequence is not contiguous")
            previous = events[index - 2].get("event_hash", "") if index > 1 else ""
            if event.get("previous_event_hash", "") != previous:
                raise CoordinatorIntegrityError("coordinator event previous hash mismatch")
            event_hash = event.get("event_hash")
            if not _is_sha256(event_hash):
                raise CoordinatorIntegrityError("coordinator event hash missing or invalid")
            unsigned = dict(event)
            unsigned.pop("event_hash", None)
            if _sha256_value(unsigned) != event_hash:
                raise CoordinatorIntegrityError("coordinator event hash mismatch")
            after_state = event.get("after_state")
            state_hash = event.get("state_hash")
            if not isinstance(after_state, Mapping) or not _is_sha256(state_hash) or _state_hash(after_state) != state_hash:
                raise CoordinatorIntegrityError("coordinator event after_state/state_hash mismatch")
            for field_name in ("run_id", "generation_id", "planner_ref", "planner_hash"):
                if field_name in event and event.get(field_name) != after_state.get(field_name):
                    raise CoordinatorIntegrityError(f"coordinator event {field_name} lineage mismatch")
        if not events:
            assert isinstance(raw_state, Mapping)
            state = self._normalize_replayed_state(raw_state)
            if not _is_sha256(state.get("planner_hash")):
                raise CoordinatorIntegrityError("coordinator planner hash is invalid")
            return state, []
        latest = dict(_canonical(events[-1]["after_state"]))
        latest["last_event_seq"] = len(events)
        latest["last_event_hash"] = events[-1]["event_hash"]
        if raw_state is not None:
            checkpoint_seq = int(raw_state.get("last_event_seq", 0) or 0)
            if checkpoint_seq > len(events):
                raise CoordinatorIntegrityError("coordinator checkpoint is ahead of event log")
            if checkpoint_seq == len(events):
                expected = dict(_canonical(events[-1]["after_state"]))
                expected["last_event_seq"] = len(events)
                expected["last_event_hash"] = events[-1]["event_hash"]
                if raw_state.get("last_event_hash", "") != events[-1]["event_hash"]:
                    raise CoordinatorIntegrityError("coordinator checkpoint hash mismatch")
                if _state_hash(raw_state) != _state_hash(expected):
                    raise CoordinatorIntegrityError("coordinator checkpoint state mismatch")
        state = self._normalize_replayed_state(latest)
        state["last_event_seq"] = len(events)
        state["last_event_hash"] = events[-1]["event_hash"]
        if not _is_sha256(state.get("planner_hash")):
            raise CoordinatorIntegrityError("coordinator planner hash is invalid")
        return state, events

    def _verify_planner_binding(self, state: Mapping[str, Any]) -> None:
        planner_ref = state.get("planner_ref")
        planner_hash = state.get("planner_hash")
        if not _is_sha256(planner_hash):
            raise CoordinatorIntegrityError("coordinator planner hash is invalid")
        if not isinstance(planner_ref, str) or "://" in planner_ref:
            return
        try:
            path = self.context.resolve_run_path(planner_ref)
        except Exception as exc:
            raise CoordinatorIntegrityError("planner reference escapes the run root") from exc
        if path.is_symlink():
            raise CoordinatorIntegrityError("planner reference cannot be a symlink")
        if path.is_file() and _sha256_bytes(path.read_bytes()) != planner_hash:
            raise CoordinatorConflictError("planner reference hash changed")

    @staticmethod
    def _slot_key(action: PlannerAction) -> str:
        return f"{action.role}\\x00{action.subject_id}\\x00{action.action}"

    @staticmethod
    def _admission_scope(action: PlannerAction) -> tuple[bool, str | None]:
        """Return the run-wide AO and per-requirement mutation scopes.

        The exact slot key remains the idempotency boundary. This separate
        projection is role/action based so an offered repair and review cannot
        be admitted together merely because their slots differ, while resolver
        and identity-review domain work stays parallel.
        """

        role = action.role.lower()
        name = action.action.lower()
        # Known workflow actions are authoritative over a synthetic/legacy
        # role label (for example tests and imported plans may label a
        # resolver/reviewer as analytical_owner). Unknown actions retain the
        # explicit role binding for forward-compatible AO adapters.
        analytical_owner = (
            name in _ANALYTICAL_OWNER_ACTIONS
            or (
                role == _ANALYTICAL_OWNER_ROLE
                and name not in (_REQUIREMENT_MUTATING_ACTIONS | _NON_ANALYTICAL_OWNER_ACTIONS)
            )
        )
        mutating = role in _REQUIREMENT_MUTATING_ROLES or name in _REQUIREMENT_MUTATING_ACTIONS
        return analytical_owner, action.subject_id if mutating else None

    @classmethod
    def _admission_counts(
        cls,
        entries: Sequence[Mapping[str, Any]],
    ) -> tuple[int, set[str]]:
        analytical_owner_count = 0
        workflow_keys: set[str] = set()
        for entry in entries:
            raw_action = entry.get("action") if isinstance(entry, Mapping) else None
            if not isinstance(raw_action, Mapping):
                continue
            action = _action(raw_action)
            analytical_owner, workflow_key = cls._admission_scope(action)
            if analytical_owner:
                analytical_owner_count += 1
            if workflow_key is not None:
                workflow_keys.add(workflow_key)
        return analytical_owner_count, workflow_keys

    @staticmethod
    def _dedupe(actions: Sequence[PlannerAction]) -> tuple[PlannerAction, ...]:
        result: list[PlannerAction] = []
        seen: set[str] = set()
        for action in actions:
            slot = RunCoordinator._slot_key(action)
            if slot in seen:
                continue
            seen.add(slot)
            result.append(action)
        return tuple(result)

    @staticmethod
    def _active_entries(state: Mapping[str, Any]) -> list[dict[str, Any]]:
        values = state.get("active_dispatches")
        if not isinstance(values, list):
            return []
        result: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, Mapping) or not isinstance(value.get("action"), Mapping):
                continue
            action = _action(value["action"])
            entry = {
                "action": action.to_dict(),
                "idempotency_key": str(value.get("idempotency_key") or ""),
                "slot_key": str(value.get("slot_key") or RunCoordinator._slot_key(action)),
            }
            if value.get("runner_id") is not None:
                entry["runner_id"] = str(value.get("runner_id"))
            if value.get("runner_pid") is not None:
                try:
                    entry["runner_pid"] = int(value.get("runner_pid"))
                except (TypeError, ValueError):
                    entry["runner_pid"] = value.get("runner_pid")
            result.append(entry)
        return result

    def _append_event_locked(
        self,
        state: dict[str, Any],
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        events = _load_events(self.events_path)
        seq = len(events) + 1
        previous_hash = events[-1].get("event_hash", "") if events else ""
        active = self._active_entries(state)
        action = active[0]["action"] if active else state.get("last_action")
        key = active[0].get("idempotency_key") if active else None
        base = {
            "schema_version": COORDINATOR_SCHEMA_VERSION,
            "kind": "run_coordinator_event",
            "seq": seq,
            "event": _text(event_type, "event_type"),
            "run_id": state.get("run_id"),
            "generation_id": state.get("generation_id"),
            "planner_ref": state.get("planner_ref"),
            "planner_hash": state.get("planner_hash"),
            "phase": state.get("phase", ""),
            "action": action.get("action") if isinstance(action, Mapping) else None,
            "subject_id": action.get("subject_id") if isinstance(action, Mapping) else None,
            "idempotency_key": key,
            "active_dispatches": active,
            "attempt": int(state.get("attempt", 0) or 0),
            "payload": dict(_canonical(payload or {})),
            "created_at": _now(),
            "previous_event_hash": previous_hash,
        }
        after_state = dict(_canonical(state))
        after_state["last_event_seq"] = seq
        after_state["last_event_hash"] = ""
        base["after_state"] = after_state
        base["state_hash"] = _state_hash(after_state)
        event = {**base, "event_hash": _sha256_value(base)}
        _append_line(self.events_path, event)
        if self._failpoint is not None:
            self._failpoint("after_event_before_checkpoint")
        state["last_event_seq"] = seq
        state["last_event_hash"] = event["event_hash"]
        self._write_state(state)
        return event

    def _write_state(self, state: Mapping[str, Any]) -> None:
        _atomic_json(self.state_path, state)

    @staticmethod
    def _diagnostic_value(value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(_canonical(value))

    def _append_diagnostic(self, state: dict[str, Any], diagnostic: Mapping[str, Any]) -> None:
        values = state.setdefault("diagnostics", [])
        if not isinstance(values, list):
            values = []
            state["diagnostics"] = values
        values.append(self._diagnostic_value(diagnostic))
        if len(values) > 32:
            del values[:-32]

    def _status_from_state(self, state: Mapping[str, Any]) -> CoordinatorStatus:
        active = tuple(self._active_entries(state))
        next_action: Mapping[str, Any] | None = None
        if active:
            next_action = active[0]["action"]
        elif state.get("status") == "waiting" and isinstance(state.get("last_action"), Mapping):
            next_action = dict(state["last_action"])
        policy = state.get("publication_policy") if isinstance(state.get("publication_policy"), Mapping) else {}
        diagnostics = tuple(
            dict(value) for value in (state.get("diagnostics") or ()) if isinstance(value, Mapping)
        )
        return CoordinatorStatus(
            run_id=str(state.get("run_id", self.context.run_id)),
            generation_id=str(state.get("generation_id", "")),
            status=str(state.get("status", "ready")),
            phase=str(state.get("phase", "queued")),
            next_action=next_action,
            owner=None,
            lease_expires_at=None,
            diagnostics=diagnostics,
            last_event_seq=int(state.get("last_event_seq", 0) or 0),
            last_event_hash=str(state.get("last_event_hash", "")),
            publication_ready=bool(state.get("publication_ready", False)),
            publication_enabled=bool(policy.get("enabled", False)),
            no_progress_count=int(state.get("no_progress_count", 0) or 0),
            next_actions=tuple(dict(value["action"]) for value in active),
            active_dispatches=active,
        )

    def _binding_upgrade_status(self, state: Mapping[str, Any]) -> CoordinatorStatus:
        """Project a durable binding-upgrade intent without configuring a role."""

        status = self._status_from_state(state)
        return CoordinatorStatus(
            run_id=status.run_id,
            generation_id=status.generation_id,
            status="waiting",
            phase="binding_upgrade_pending",
            next_action=status.next_action,
            owner=status.owner,
            lease_expires_at=status.lease_expires_at,
            diagnostics=status.diagnostics,
            last_event_seq=status.last_event_seq,
            last_event_hash=status.last_event_hash,
            publication_ready=False,
            publication_enabled=status.publication_enabled,
            no_progress_count=status.no_progress_count,
            next_actions=status.next_actions,
            active_dispatches=status.active_dispatches,
        )

    @staticmethod
    def _binding_fields() -> tuple[str, ...]:
        return ("skill_path", "skill_version", "core_version", "skill_sha256")

    def _binding_upgrade_payload(
        self,
        old: CoordinatorRunSpec,
        new: CoordinatorRunSpec,
        *,
        old_spec_hash: str,
        new_spec_hash: str,
    ) -> dict[str, Any]:
        return {
            "run_id": new.run_id,
            "old_spec_hash": old_spec_hash,
            "new_spec_hash": new_spec_hash,
            "binding_fields": list(self._binding_fields()),
            "from": {
                "generation_id": old.generation_id,
                "planner_ref": old.planner_ref,
                "planner_hash": old.planner_hash,
            },
            "to": {
                "generation_id": new.generation_id,
                "planner_ref": new.planner_ref,
                "planner_hash": new.planner_hash,
            },
            "new_spec": new.to_dict(),
        }

    def _recover_binding_upgrade_locked(self) -> CoordinatorStatus | None:
        """Finish one interrupted binding-only upgrade under the coordinator lock.

        The intent is written as part of a hash-chained ``started`` event
        before the specification bytes move.  A crash can therefore leave
        either the old or the new spec beside the intent; both cases converge
        to one canonical ``coordinator_binding_upgraded`` event on retry.
        This method never calls ``_configure_from_spec`` on the old unbound
        document.
        """

        if not self.spec_path.exists() or not self.state_path.exists():
            return None
        # G5 wrappers use a different event/hash contract and are handled by
        # the dedicated legacy import transaction below.
        raw_spec = _load_json(self.spec_path)
        if self._looks_like_legacy_spec(raw_spec):
            return None
        state, _ = self._read_replay()
        assert state is not None
        pending = state.get("pending_binding_upgrade")
        if pending is None:
            return None
        if not isinstance(pending, Mapping):
            raise CoordinatorIntegrityError("pending binding upgrade state is invalid")
        if self._active_entries(state):
            raise CoordinatorConflictError("coordinator cannot recover binding upgrade while dispatches are active")
        try:
            old_hash = str(pending["old_spec_hash"])
            new_hash = str(pending["new_spec_hash"])
            raw_new = pending["new_spec"]
            target = CoordinatorRunSpec.from_dict(raw_new)
        except (KeyError, TypeError, ValueError) as exc:
            raise CoordinatorIntegrityError("pending binding upgrade intent is malformed") from exc
        if target.run_id != self.context.run_id or self._spec_hash(target) != new_hash:
            raise CoordinatorIntegrityError("pending binding upgrade target hash is invalid")
        if tuple(pending.get("binding_fields") or ()) != self._binding_fields():
            raise CoordinatorIntegrityError("pending binding upgrade fields are invalid")
        CodexExecConfig.from_dict(target.codex_exec).validate_skill_binding(
            required=True,
            verify_active=True,
            repo_root=Path(__file__).resolve().parents[2],
            role_cwd=self.context.run_root,
        )
        persisted = self._load_spec_document()
        persisted_hash = self._spec_hash(persisted)
        if persisted_hash not in {old_hash, new_hash}:
            raise CoordinatorConflictError("pending binding upgrade source specification changed")
        if (
            persisted.run_id != target.run_id
            or persisted.generation_id != target.generation_id
            or persisted.planner_ref != target.planner_ref
            or persisted.planner_hash != target.planner_hash
        ):
            raise CoordinatorConflictError("pending binding upgrade planner lineage changed")
        if persisted_hash == old_hash:
            self._write_spec(target)
        state.update(
            {
                "spec_hash": new_hash,
                "spec_ref": f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_SPEC_FILENAME}",
                "status": str((pending.get("prior_state") or {}).get("status") or "ready"),
                "phase": str((pending.get("prior_state") or {}).get("phase") or "queued"),
                "pending_binding_upgrade": None,
                "active_dispatches": [],
                "publication_ready": bool((pending.get("prior_state") or {}).get("publication_ready", False)),
            }
        )
        payload = dict(_canonical(pending))
        payload.pop("new_spec", None)
        self._append_event_locked(state, "coordinator_binding_upgraded", payload)
        self._spec = target
        return self._status_from_state(state)

    def _pending_rebind_status(self) -> CoordinatorStatus | None:
        if self._legacy_pending or not self.spec_path.exists():
            return None
        with self._locked(create=False):
            state, _ = self._read_replay()
            assert state is not None
            if isinstance(state.get("pending_binding_upgrade"), Mapping):
                return self._binding_upgrade_status(state)
            if not isinstance(state.get("pending_plan_rebind"), Mapping):
                return None
            return self._status_from_state(state)

    def _legacy_pending_status(self) -> CoordinatorStatus:
        spec = self._spec
        if spec is None:
            raise CoordinatorIntegrityError("legacy coordinator specification is not loaded")
        return CoordinatorStatus(
            run_id=spec.run_id,
            generation_id=spec.generation_id,
            status="waiting",
            phase="legacy_import_required",
            next_action=None,
            owner=None,
            lease_expires_at=None,
            diagnostics=(
                {
                    "kind": "legacy_import_required",
                    "message": "reopen is required to archive the G5 control plane and start a fresh chain",
                },
            ),
            last_event_seq=0,
            last_event_hash="",
            publication_ready=False,
            publication_enabled=bool(spec.publication_policy.get("enabled", False)),
            no_progress_count=0,
            next_actions=(),
            active_dispatches=(),
        )

    def _import_legacy(self, reason: str) -> CoordinatorStatus:
        """Archive one exact G5 snapshot and initialize a fresh local chain."""

        reason = _text(reason, "reason")
        # A missing lock is a partial legacy layout.  Check before entering
        # _locked, whose normal create semantics would otherwise create it.
        if self.lock_path.is_symlink() or not self.lock_path.is_file():
            raise CoordinatorIntegrityError("legacy coordinator lock is missing or not regular")
        with self._locked(create=False):
            recovered = self._recover_legacy_import_locked()
            if recovered is not None:
                return recovered
            value = _load_json(self.spec_path)
            if not self._is_legacy_spec_document(value):
                # A second import/reopen is a harmless status read after the
                # canonical flat spec has replaced the wrapper.
                self._legacy_pending = False
                state, _ = self._read_replay()
                assert state is not None
                return self._status_from_state(state)
            spec, raw = self._validate_legacy_snapshot()
            intent = self._legacy_intent(_strict_json_loads(raw[COORDINATOR_SPEC_FILENAME]), raw, reason)
            _atomic_json(self.legacy_intent_path, intent)
            self._legacy_failpoint("legacy_import_after_intent")
            return self._finish_legacy_import_locked(intent, spec, raw)

    def _idempotency_key(self, state: Mapping[str, Any], action: PlannerAction) -> str:
        return _sha256_value(
            {
                "run_id": state.get("run_id"),
                "generation_id": state.get("generation_id"),
                "planner_ref": state.get("planner_ref"),
                "planner_hash": state.get("planner_hash"),
                "action": action.to_dict(),
            }
        )

    def _query_planner(self, state: Mapping[str, Any]) -> tuple[PlannerAction, ...]:
        supplied = self.planner_provider.next_actions(self.context, dict(state))
        if supplied is None:
            return ()
        return self._dedupe(tuple(_action(item) for item in supplied))

    def _phase_snapshot(self) -> Mapping[str, Any]:
        from .requirement_planning import RequirementSupervisorWorkspace

        return RequirementSupervisorWorkspace(self.context).phase_snapshot()

    def _terminal_projection(self) -> tuple[bool, str, Mapping[str, Any]]:
        try:
            snapshot = self._phase_snapshot()
        except Exception as exc:
            return False, "phase_snapshot_error", {"valid": False, "diagnostics": [str(exc)]}
        lifecycle_validation = snapshot.get("lifecycle_validation") or {}
        lifecycle_valid = bool(lifecycle_validation.get("valid"))
        lifecycle_state = str(snapshot.get("lifecycle_state") or "")
        product = snapshot.get("product") if isinstance(snapshot.get("product"), Mapping) else {}
        report = snapshot.get("report") if isinstance(snapshot.get("report"), Mapping) else {}
        product_valid = bool((product.get("validation") or {}).get("valid"))
        report_valid = bool((report.get("validation") or {}).get("valid"))
        if (
            lifecycle_valid
            and lifecycle_state in TERMINAL_STATUSES
            and snapshot.get("all_items_integrated")
            and product_valid
            and report_valid
        ):
            return True, lifecycle_state, {"snapshot": snapshot}
        return False, "planner_empty_nonterminal", {
            "lifecycle_state": lifecycle_state,
            "lifecycle_validation": lifecycle_validation,
            "all_items_integrated": snapshot.get("all_items_integrated"),
            "product_validation": product.get("validation"),
            "report_validation": report.get("validation"),
        }

    def _ensure_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="auto-foundry-role",
            )
        return self._executor

    def _dispatch_role(self, action: PlannerAction, key: str) -> RoleExecution:
        runner = self.role_runner
        if runner is None:
            return RoleExecution(exit_code=1, error="no role runner configured")
        try:
            if hasattr(runner, "dispatch") and callable(getattr(runner, "dispatch")):
                value = runner.dispatch(action, idempotency_key=key, context=self.context)
            else:
                value = runner(action, idempotency_key=key, context=self.context)
            return _normalize_role_execution(value)
        except Exception as exc:
            return RoleExecution(exit_code=1, error=f"role process exception: {exc}")

    def _submit_entries(self, entries: Sequence[Mapping[str, Any]]) -> None:
        if not entries:
            return
        executor = self._ensure_executor()
        gate = Condition()
        turn = {"value": 0}

        def invoke(entry: Mapping[str, Any], index: int) -> RoleExecution:
            with gate:
                while turn["value"] != index:
                    gate.wait()
                turn["value"] += 1
                gate.notify_all()
            action = _action(entry["action"])
            return self._dispatch_role(action, str(entry.get("idempotency_key") or ""))

        for index, entry in enumerate(entries):
            slot = str(entry["slot_key"])
            try:
                future = executor.submit(invoke, dict(entry), index)
            except Exception as exc:
                future = Future()
                future.set_result(RoleExecution(exit_code=1, error=f"role dispatch submission failed: {exc}"))
            self._futures[slot] = future
            self._future_entries[slot] = dict(entry)

    def _claim_entry(self, entry: dict[str, Any]) -> bool:
        """Claim an active slot, refusing a live process's claim."""

        prior_pid = entry.get("runner_pid")
        prior_runner = entry.get("runner_id")
        if prior_pid is not None and _pid_alive(prior_pid):
            # A claim from this exact coordinator instance is reusable.  Any
            # other live PID remains authoritative until that process exits.
            return prior_pid == os.getpid() and prior_runner == self.owner_id
        entry["runner_id"] = self.owner_id
        entry["runner_pid"] = os.getpid()
        return True

    def _resume_active_locked(
        self,
        state: dict[str, Any],
        offered: Sequence[PlannerAction],
    ) -> list[dict[str, Any]]:
        """Reconcile persisted dispatches inside the Planner snapshot lock."""

        entries = self._active_entries(state)
        offered_slots = {self._slot_key(action) for action in offered}
        removed = [
            entry
            for entry in entries
            if entry["slot_key"] not in offered_slots
            and entry["slot_key"] not in self._futures
            and not (entry.get("runner_pid") is not None and _pid_alive(entry.get("runner_pid")))
        ]
        if removed:
            entries = [entry for entry in entries if entry not in removed]
            state["active_dispatches"] = entries
            diagnostic = {
                "kind": "planner_advanced",
                "reason": "restart_active_not_offered",
                "removed_dispatches": [dict(entry) for entry in removed],
                "after_actions": [action.to_dict() for action in offered],
            }
            self._append_diagnostic(state, diagnostic)
            self._append_event_locked(state, "planner_advanced", diagnostic)
        # Treat the persisted active list as an admission queue on restart.
        # Older state may contain more than one AO or two mutating actions for
        # one requirement; submit only the first entry in each scope and leave
        # the remainder durable for the next reconciliation after completion.
        pending: list[dict[str, Any]] = []
        analytical_owner_count = 0
        workflow_keys: set[str] = set()
        for entry in entries:
            action = _action(entry["action"])
            analytical_owner, workflow_key = self._admission_scope(action)
            if analytical_owner and analytical_owner_count >= 1:
                continue
            if workflow_key is not None and workflow_key in workflow_keys:
                continue
            if analytical_owner:
                analytical_owner_count += 1
            if workflow_key is not None:
                workflow_keys.add(workflow_key)
            if entry["slot_key"] in self._futures:
                continue
            if self._claim_entry(entry):
                pending.append(entry)
        if pending:
            state["active_dispatches"] = entries
            self._append_event_locked(
                state,
                "dispatch_claimed",
                {
                    "runner_id": self.owner_id,
                    "runner_pid": os.getpid(),
                    "dispatches": [dict(entry) for entry in pending],
                },
            )
        return pending

    def _record_planner_error(self, error: Exception) -> CoordinatorStatus:
        with self._locked(create=False):
            state, _ = self._read_replay()
            assert state is not None
            self._append_diagnostic(state, {"kind": "planner_error", "error": str(error)})
            active = self._active_entries(state)
            if active:
                state["status"] = "dispatching"
                state["phase"] = "dispatching"
            else:
                state["status"] = "waiting"
                state["phase"] = "waiting"
            self._append_event_locked(state, "wait", {"reason": "planner_error", "error": str(error)})
            return self._status_from_state(state)

    def _refresh_and_launch(
        self,
        retry_blocked: set[str],
        *,
        completed: tuple[PlannerAction, RoleExecution] | None = None,
    ) -> CoordinatorStatus:
        self._last_completion_same = False
        # The completed entry is still present in the durable active map until
        # the reconciliation transaction below removes it. Never resubmit
        # that entry between ``_consume_one`` and this transaction; other
        # active entries may still be safely resumed/submitted.
        pending: list[dict[str, Any]] = []
        new_entries: list[dict[str, Any]] = []
        planner_read = False
        try:
            # Keep the Planner read and control-plane reconciliation in one
            # short critical section. Planner owns its public state and never
            # calls back into the coordinator; role execution starts only
            # after this lock is released.
            with self._locked(create=False):
                state, _ = self._read_replay()
                assert state is not None
                self._verify_planner_binding(state)
                planner_read = True
                actions = self._query_planner(state)
                planner_read = False
                if completed is None:
                    pending = self._resume_active_locked(state, actions)

                active = self._active_entries(state)
                active_slots = {str(entry["slot_key"]) for entry in active}
                analytical_owner_count, workflow_keys = self._admission_counts(active)
                deferred_actions: list[dict[str, Any]] = []
                if completed is not None:
                    completed_action, execution = completed
                    completed_slot = self._slot_key(completed_action)
                    self._last_completion_same = any(
                        self._slot_key(action) == completed_slot for action in actions
                    )
                    state["last_action"] = completed_action.to_dict()
                    state["active_dispatches"] = [
                        entry for entry in active if str(entry["slot_key"]) != completed_slot
                    ]
                    active = self._active_entries(state)
                    active_slots = {str(entry["slot_key"]) for entry in active}
                    analytical_owner_count, workflow_keys = self._admission_counts(active)
                    if self._last_completion_same:
                        previous = state.get("last_no_progress_action")
                        previous_slot = (
                            self._slot_key(_action(previous))
                            if isinstance(previous, Mapping)
                            else None
                        )
                        state["no_progress_count"] = (
                            int(state.get("no_progress_count", 0) or 0) + 1
                            if previous_slot == completed_slot
                            else 1
                        )
                        state["last_no_progress_action"] = completed_action.to_dict()
                        kind = "no_progress" if execution.ok else "role_transport_failure"
                        self._append_diagnostic(
                            state,
                            {
                                "kind": kind,
                                "action": completed_action.to_dict(),
                                "transport": execution.to_dict(),
                                "count": state["no_progress_count"],
                            },
                        )
                        self._append_event_locked(
                            state,
                            "role_exit",
                            {
                                "action": completed_action.to_dict(),
                                "idempotency_key": self._idempotency_key(state, completed_action),
                                "transport": execution.to_dict(),
                            },
                        )
                    else:
                        state["no_progress_count"] = 0
                        state["last_no_progress_action"] = None
                        if not execution.ok:
                            self._append_diagnostic(
                                state,
                                {
                                    "kind": "role_transport_failure",
                                    "action": completed_action.to_dict(),
                                    "transport": execution.to_dict(),
                                },
                            )
                        self._append_event_locked(
                            state,
                            "planner_advanced",
                            {
                                "before_action": completed_action.to_dict(),
                                "after_actions": [action.to_dict() for action in actions],
                                "transport": execution.to_dict(),
                            },
                        )
                    # A restart may have left a second action durable in the
                    # same scope but intentionally unsent by the admission
                    # queue. Once the completed entry is removed, claim the
                    # next persisted offer in this same reconciliation so a
                    # single ``step``/``resume`` cannot return dispatching
                    # with no future while still respecting the capacity gate.
                    pending.extend(self._resume_active_locked(state, actions))
                    active = self._active_entries(state)
                    active_slots = {str(entry["slot_key"]) for entry in active}
                    analytical_owner_count, workflow_keys = self._admission_counts(active)
                for action in actions:
                    slot = self._slot_key(action)
                    if slot in active_slots or slot in retry_blocked:
                        continue
                    analytical_owner, workflow_key = self._admission_scope(action)
                    if analytical_owner and analytical_owner_count >= 1:
                        deferred_actions.append(
                            {
                                "action": action.to_dict(),
                                "reason": "analytical_owner_capacity",
                            }
                        )
                        continue
                    if workflow_key is not None and workflow_key in workflow_keys:
                        deferred_actions.append(
                            {
                                "action": action.to_dict(),
                                "reason": "requirement_workflow_busy",
                            }
                        )
                        continue
                    key = self._idempotency_key(state, action)
                    entry = {
                        "action": action.to_dict(),
                        "idempotency_key": key,
                        "slot_key": slot,
                        "runner_id": self.owner_id,
                        "runner_pid": os.getpid(),
                    }
                    state.setdefault("active_dispatches", []).append(entry)
                    state["status"] = "dispatching"
                    state["phase"] = action.action
                    state["attempt"] = int(state.get("attempt", 0) or 0) + 1
                    self._append_event_locked(
                        state,
                        "dispatch_started",
                        {"action": action.to_dict(), "idempotency_key": key},
                    )
                    active_slots.add(slot)
                    if analytical_owner:
                        analytical_owner_count += 1
                    if workflow_key is not None:
                        workflow_keys.add(workflow_key)
                    new_entries.append(entry)
                if deferred_actions:
                    # Deferred Planner offers are re-evaluated after the
                    # active scope completes; they are not retry failures and
                    # do not consume the action retry budget.
                    self._append_diagnostic(
                        state,
                        {
                            "kind": "dispatch_deferred",
                            "reason": "admission_capacity",
                            "actions": deferred_actions,
                        },
                    )
                active = self._active_entries(state)
                if not active and not actions:
                    terminal, phase, diagnostic = self._terminal_projection()
                    if terminal:
                        state["status"] = phase
                        state["phase"] = phase
                        state["publication_ready"] = True
                        self._append_event_locked(state, "run_completed", diagnostic)
                    else:
                        state["status"] = "waiting"
                        state["phase"] = "waiting"
                        self._append_diagnostic(
                            state,
                            {"kind": "planner_empty", **dict(_canonical(diagnostic))},
                        )
                        self._append_event_locked(
                            state,
                            "wait",
                            {"reason": "planner_empty", "diagnostic": diagnostic},
                        )
                elif active:
                    state["status"] = "dispatching"
                    state["phase"] = str(active[0]["action"].get("action", "dispatching"))
                else:
                    state["status"] = "waiting"
                    state["phase"] = "waiting"
                    self._append_event_locked(
                        state,
                        "wait",
                        {"reason": "retry_budget", "actions": [action.to_dict() for action in actions]},
                    )
                status = self._status_from_state(state)
        except Exception as exc:
            if not planner_read:
                raise
            return self._record_planner_error(exc)
        self._submit_entries(pending)
        self._submit_entries(new_entries)
        return status

    def _consume_one(self) -> tuple[PlannerAction, RoleExecution] | None:
        if not self._futures:
            return None
        done, _ = wait(tuple(self._futures.values()), return_when=FIRST_COMPLETED)
        slot = next(
            (candidate for candidate, future in self._futures.items() if future in done),
            None,
        )
        if slot is None:
            return None
        future = self._futures.pop(slot)
        entry = self._future_entries.pop(slot)
        try:
            execution = _normalize_role_execution(future.result())
        except Exception as exc:
            execution = RoleExecution(exit_code=1, error=f"role future failed: {exc}")
        return _action(entry["action"]), execution

    def step(self) -> CoordinatorStatus:
        """Launch the ready set, wait for one completion, and reconcile."""

        self._ensure_persisted_configuration()
        if self._legacy_pending:
            self._import_legacy("step imported legacy G5 coordinator")
        pending = self._pending_rebind_status()
        if pending is not None:
            return pending
        initial = self._refresh_and_launch(set())
        if initial.status in TERMINAL_STATUSES or (initial.status == "waiting" and not self._futures):
            return initial
        completed = self._consume_one()
        if completed is None:
            return self.status()
        return self._refresh_and_launch(
            {self._slot_key(completed[0])},
            completed=completed,
        )

    def run(self, *, max_steps: int | None = None) -> CoordinatorStatus:
        """Run the event loop until terminal, waiting, or a bounded budget."""

        if max_steps is not None and (
            isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 0
        ):
            raise ValueError("max_steps must be a non-negative integer or None")
        self._ensure_persisted_configuration()
        if self._legacy_pending:
            self._import_legacy("run imported legacy G5 coordinator")
        pending = self._pending_rebind_status()
        if pending is not None:
            return pending
        if max_steps == 0:
            return self.status()
        retry_blocked: set[str] = set()
        attempts: dict[str, int] = {}
        steps = 0
        while max_steps is None or steps < max_steps:
            status = self._refresh_and_launch(retry_blocked)
            if status.status in TERMINAL_STATUSES:
                return status
            if not self._futures:
                return status
            completed = self._consume_one()
            if completed is None:
                return self.status()
            steps += 1
            action, _execution = completed
            # Keep the just-completed slot out of this reconciliation pass so
            # an unchanged Planner offer is reported as waiting/retryable
            # instead of being launched immediately a third time.
            action_slot = self._slot_key(action)
            status = self._refresh_and_launch(
                set(retry_blocked) | {action_slot},
                completed=completed,
            )
            if self._last_completion_same:
                slot = action_slot
                attempts[slot] = attempts.get(slot, 0) + 1
                if attempts[slot] < MAX_RUN_RETRIES_PER_ACTION:
                    retry_blocked.discard(slot)
                else:
                    retry_blocked.add(slot)
            else:
                attempts.pop(self._slot_key(action), None)
                retry_blocked.discard(self._slot_key(action))
            if status.status == "waiting" and not self._futures and self._slot_key(action) in retry_blocked:
                return status
        return self.status()

    def resume(self, run_id: str | None = None) -> CoordinatorStatus:
        if run_id is not None and run_id != self.context.run_id:
            raise CoordinatorConflictError("resume run_id does not match context")
        self._ensure_persisted_configuration()
        if self._legacy_pending:
            self._import_legacy("resume imported legacy G5 coordinator")
        pending = self._pending_rebind_status()
        if pending is not None:
            return pending
        return self.step()

    def renew(self, owner_id: str, token: str) -> CoordinatorStatus:
        """Return status; role capacity is owned by public APIs, not leases."""

        if owner_id != self.owner_id:
            raise CoordinatorConflictError("lease owner does not match this coordinator")
        return self.status()

    def watchdog(self, run_id: str | None = None) -> CoordinatorStatus:
        if run_id is not None and run_id != self.context.run_id:
            raise CoordinatorConflictError("watchdog run_id does not match context")
        return self.status()

    def status(self, run_id: str | None = None) -> CoordinatorStatus:
        if run_id is not None and run_id != self.context.run_id:
            raise CoordinatorConflictError("status run_id does not match context")
        self._ensure_persisted_configuration()
        if self._legacy_pending:
            return self._legacy_pending_status()
        pending = self._pending_rebind_status()
        if pending is not None:
            return pending
        with self._locked(create=False):
            state, _ = self._read_replay()
            assert state is not None
            return self._status_from_state(state)

    def reopen(self, reason: str) -> CoordinatorStatus:
        """Reset an idle waiting/terminal coordinator for a fresh Planner read."""

        reason = _text(reason, "reason")
        self._ensure_persisted_configuration()
        if self._legacy_pending:
            return self._import_legacy(reason)
        with self._locked(create=False):
            pending = self._recover_legacy_import_locked()
            if pending is not None:
                return pending
            state, _ = self._read_replay()
            assert state is not None
            if isinstance(state.get("pending_plan_rebind"), Mapping):
                return self._status_from_state(state)
            active = self._active_entries(state)
            cleared: list[dict[str, Any]] = []
            for entry in active:
                pid = entry.get("runner_pid")
                if (pid is None and entry.get("runner_id") is not None) or (pid is not None and not _pid_alive(pid)):
                    entry.pop("runner_pid", None)
                    entry.pop("runner_id", None)
                    cleared.append(entry)
            if cleared:
                state["active_dispatches"] = active
                self._append_event_locked(
                    state,
                    "dispatch_claims_cleared",
                    {"dispatches": [dict(entry) for entry in cleared], "reason": reason},
                )
            if active:
                return self._status_from_state(state)
            if state.get("status") not in {"waiting", "blocked_rethink", *TERMINAL_STATUSES}:
                return self._status_from_state(state)
            state["status"] = "ready"
            state["phase"] = "ready"
            state["no_progress_count"] = 0
            state["last_no_progress_action"] = None
            state["publication_ready"] = False
            self._append_event_locked(state, "reopen", {"reason": reason})
            return self._status_from_state(state)

    def _plan_rebound_payload(
        self,
        old: CoordinatorRunSpec,
        new: CoordinatorRunSpec,
        *,
        old_spec_hash: str | None = None,
    ) -> dict[str, Any]:
        transport_fields = ("role_dispatch_command", "codex_exec", "lease_ttl_seconds")
        publication_fields = ("publication_policy",)
        old_transport_hash = _sha256_value({name: old.to_dict().get(name) for name in transport_fields})
        new_transport_hash = _sha256_value({name: new.to_dict().get(name) for name in transport_fields})
        old_publication_hash = _sha256_value({name: old.to_dict().get(name) for name in publication_fields})
        new_publication_hash = _sha256_value({name: new.to_dict().get(name) for name in publication_fields})
        return {
            "old_spec_hash": old_spec_hash or self._spec_hash(old),
            "new_spec_hash": self._spec_hash(new),
            "from": {
                "generation_id": old.generation_id,
                "planner_ref": old.planner_ref,
                "planner_hash": old.planner_hash,
            },
            "to": {
                "generation_id": new.generation_id,
                "planner_ref": new.planner_ref,
                "planner_hash": new.planner_hash,
            },
            "transport": {
                "fields": list(transport_fields),
                "old_hash": old_transport_hash,
                "new_hash": new_transport_hash,
            },
            "publication": {
                "fields": list(publication_fields),
                "old_hash": old_publication_hash,
                "new_hash": new_publication_hash,
            },
        }

    def publish_and_rebind(
        self,
        spec: CoordinatorRunSpec | Mapping[str, Any],
        publisher: Callable[[CoordinatorRunSpec], Any],
    ) -> CoordinatorStatus:
        """Publish a new public generation and atomically bind the coordinator.

        ``publisher`` receives the canonical target ``CoordinatorRunSpec`` and
        its return value is deliberately ignored.  It runs while the sole
        coordinator lock is held; a normal exception leaves a durable pending
        transaction that a later identical call can retry.
        """

        if not callable(publisher):
            raise TypeError("publisher must be callable")
        target = spec if isinstance(spec, CoordinatorRunSpec) else CoordinatorRunSpec.from_dict(spec)
        if target.run_id != self.context.run_id:
            raise CoordinatorConflictError("run spec run_id does not match context")
        with self._locked(create=False):
            recovered = self._recover_legacy_import_locked()
            if recovered is not None:
                raise CoordinatorConflictError("cannot publish while legacy import is pending")
            persisted = self._load_spec_document()
            state, _ = self._read_replay()
            assert state is not None
            self._spec = persisted
            self._configure_from_spec(persisted)
            pending = state.get("pending_plan_rebind")
            if pending is not None and not isinstance(pending, Mapping):
                raise CoordinatorIntegrityError("pending plan rebind state is invalid")
            target_hash = self._spec_hash(target)
            if isinstance(pending, Mapping):
                if pending.get("new_spec_hash") != target_hash:
                    raise CoordinatorConflictError("a different plan rebind is already pending")
                if self._active_entries(state):
                    raise CoordinatorConflictError("coordinator cannot rebind while dispatches are active")
                old_hash = str(pending.get("old_spec_hash") or self._spec_hash(persisted))
                old = persisted if self._spec_hash(persisted) == old_hash else CoordinatorRunSpec(
                    run_id=str(pending.get("run_id") or state.get("run_id")),
                    generation_id=str(pending.get("from", {}).get("generation_id") or pending.get("old_generation_id") or state.get("generation_id")),
                    planner_ref=str(pending.get("from", {}).get("planner_ref") or pending.get("old_planner_ref") or state.get("planner_ref")),
                    planner_hash=str(pending.get("from", {}).get("planner_hash") or pending.get("old_planner_hash") or state.get("planner_hash")),
                    role_dispatch_command=(),
                    publication_policy=state.get("publication_policy") or {},
                    codex_exec={},
                    lease_ttl_seconds=DEFAULT_LEASE_TTL_SECONDS,
                )
            else:
                if persisted.to_dict() == target.to_dict():
                    return self._status_from_state(state)
                lineage = {"generation_id", "planner_ref", "planner_hash"}
                if not any(persisted.to_dict().get(name) != target.to_dict().get(name) for name in lineage):
                    raise CoordinatorConflictError("plan rebind requires a changed generation or planner lineage")
                if self._active_entries(state):
                    raise CoordinatorConflictError("coordinator cannot rebind while dispatches are active")
                old = persisted
                payload = self._plan_rebound_payload(old, target)
                pending = {
                    "run_id": target.run_id,
                    "old_spec_hash": payload["old_spec_hash"],
                    "new_spec_hash": payload["new_spec_hash"],
                    "from": payload["from"],
                    "to": payload["to"],
                }
                state["pending_plan_rebind"] = pending
                state["status"] = "waiting"
                state["phase"] = "plan_rebind_pending"
                self._append_event_locked(state, "plan_rebind_started", {
                    "old_spec_hash": payload["old_spec_hash"],
                    "new_spec_hash": payload["new_spec_hash"],
                    "from": payload["from"],
                    "to": payload["to"],
                })
                self._legacy_failpoint("plan_rebind_after_started")

            try:
                publisher(target)
            except Exception as exc:
                self._append_diagnostic(state, {"kind": "plan_rebind_pending", "error": str(exc), "target_spec_hash": target_hash})
                state["status"] = "waiting"
                state["phase"] = "plan_rebind_pending"
                self._append_event_locked(state, "wait", {"reason": "plan_rebind_pending", "error": str(exc), "target_spec_hash": target_hash})
                return self._status_from_state(state)

            payload = self._plan_rebound_payload(old, target, old_spec_hash=str(pending.get("old_spec_hash")))
            self._spec = target
            self._configure_from_spec(target)
            self._write_spec(target)
            self._legacy_failpoint("plan_rebind_after_spec")
            state.update(
                {
                    "generation_id": target.generation_id,
                    "planner_ref": target.planner_ref,
                    "planner_hash": target.planner_hash,
                    "spec_hash": target_hash,
                    "spec_ref": f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_SPEC_FILENAME}",
                    "publication_policy": dict(target.publication_policy),
                    "status": "ready",
                    "phase": "ready",
                    "pending_plan_rebind": None,
                    "active_dispatches": [],
                    "last_action": None,
                    "no_progress_count": 0,
                    "last_no_progress_action": None,
                    "publication_ready": False,
                    "attempt": 0,
                }
            )
            self._append_event_locked(state, "plan_rebound", payload)
            self._legacy_failpoint("plan_rebind_after_event")
            return self._status_from_state(state)

    def upgrade_and_rebind(
        self,
        spec: CoordinatorRunSpec | Mapping[str, Any],
    ) -> CoordinatorStatus:
        """Quiescently bind an existing unbound coordinator specification.

        This is the one migration-shaped entrypoint for current control
        planes that predate the production skill binding.  It validates the
        target and installed skill before taking the coordinator lock, then
        repeats the binding check while locked.  The persisted specification
        is never loaded through ``CodexRoleAdapter`` until the new binding is
        durably recorded, so an old unbound spec cannot configure a fallback
        adapter.  The transaction may add *only* the four binding fields;
        every other spec and Codex setting must remain byte-for-byte equal.
        Only unchanged run/planner lineage is eligible; a normal generation
        rebind remains the separate ``publish_and_rebind`` path.
        """

        target = spec if isinstance(spec, CoordinatorRunSpec) else CoordinatorRunSpec.from_dict(spec)
        if target.run_id != self.context.run_id:
            raise CoordinatorConflictError("run spec run_id does not match context")

        def validate_target() -> CodexExecConfig:
            config = CodexExecConfig.from_dict(target.codex_exec)
            config.validate_skill_binding(
                required=True,
                verify_active=True,
                repo_root=Path(__file__).resolve().parents[2],
                role_cwd=self.context.run_root,
            )
            return config

        # Validate before the lock as an inexpensive fail-closed admission;
        # the same check is repeated under the lock to bind the bytes actually
        # present at the commit point.
        validate_target()
        with self._locked(create=False):
            if self.legacy_intent_path.exists() or self.legacy_staging_dir.exists():
                raise CoordinatorConflictError("cannot upgrade while legacy import is pending")
            state, _ = self._read_replay()
            assert state is not None
            pending = state.get("pending_binding_upgrade")
            if isinstance(pending, Mapping) and pending.get("new_spec_hash") != self._spec_hash(target):
                raise CoordinatorConflictError("a different binding upgrade is already pending")
            if pending is not None and not isinstance(pending, Mapping):
                raise CoordinatorIntegrityError("pending binding upgrade state is invalid")
            if isinstance(pending, Mapping):
                self._recover_binding_upgrade_locked()
                state, _ = self._read_replay()
                assert state is not None
            persisted = self._load_spec_document()
            state, _ = self._read_replay()
            assert state is not None
            self._verify_planner_binding(state)
            pending = state.get("pending_plan_rebind")
            if pending is not None:
                raise CoordinatorConflictError("coordinator has a pending plan rebind")
            active = self._active_entries(state)
            if active:
                raise CoordinatorConflictError("coordinator cannot upgrade while dispatches are active")
            persisted_hash = self._spec_hash(persisted)
            if state.get("spec_hash") != persisted_hash:
                raise CoordinatorConflictError("coordinator specification hash is stale")
            if (
                target.generation_id != persisted.generation_id
                or target.planner_ref != persisted.planner_ref
                or target.planner_hash != persisted.planner_hash
            ):
                raise CoordinatorConflictError("binding upgrade requires unchanged planner lineage")

            binding_fields = self._binding_fields()
            old_codex = dict(persisted.codex_exec)
            target_codex = dict(target.codex_exec)
            present_old = {name for name in binding_fields if name in old_codex}
            persisted_dict = persisted.to_dict()
            target_dict = target.to_dict()
            # Exact retries of a completed migration are idempotent.  A
            # partially-bound spec is neither a valid migration source nor a
            # completed target and therefore remains fail-closed.
            if present_old:
                if present_old == set(binding_fields) and persisted_dict == target_dict:
                    self._spec = persisted
                    return self._status_from_state(state)
                raise CoordinatorConflictError("binding upgrade requires a genuinely unbound specification")
            for field_name in persisted_dict:
                if field_name == "codex_exec":
                    continue
                if persisted_dict.get(field_name) != target_dict.get(field_name):
                    raise CoordinatorConflictError("binding upgrade would change non-binding specification fields")
            expected_codex = dict(old_codex)
            for field_name in binding_fields:
                if field_name not in target_codex or target_codex[field_name] is None:
                    raise CoordinatorConflictError("binding upgrade must add all four production binding fields")
                expected_codex[field_name] = target_codex[field_name]
            if target_codex != expected_codex:
                raise CoordinatorConflictError("binding upgrade would change non-binding Codex fields")

            validate_target()
            target_hash = self._spec_hash(target)
            payload = self._binding_upgrade_payload(
                persisted,
                target,
                old_spec_hash=persisted_hash,
                new_spec_hash=target_hash,
            )
            payload["prior_state"] = {
                "status": state.get("status", "ready"),
                "phase": state.get("phase", "queued"),
                "publication_ready": bool(state.get("publication_ready", False)),
            }
            state["pending_binding_upgrade"] = payload
            state["status"] = "waiting"
            state["phase"] = "binding_upgrade_pending"
            self._append_event_locked(state, "coordinator_binding_upgrade_started", payload)
            self._legacy_failpoint("binding_upgrade_after_intent")
            self._write_spec(target)
            self._legacy_failpoint("binding_upgrade_after_spec")
            final_payload = dict(payload)
            final_payload.pop("new_spec", None)
            state.update(
                {
                    "spec_hash": target_hash,
                    "spec_ref": f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_SPEC_FILENAME}",
                    "status": str((payload.get("prior_state") or {}).get("status") or "ready"),
                    "phase": str((payload.get("prior_state") or {}).get("phase") or "queued"),
                    "pending_binding_upgrade": None,
                    "active_dispatches": [],
                    "publication_ready": bool((payload.get("prior_state") or {}).get("publication_ready", False)),
                }
            )
            self._append_event_locked(state, "coordinator_binding_upgraded", final_payload)
            self._legacy_failpoint("binding_upgrade_after_event")
            # ``_append_event_locked`` checkpoints state as part of the event
            # transaction.  The explicit rewrite provides a named final
            # failpoint for crash-recovery tests without introducing a
            # split-brain checkpoint.
            self._write_state(state)
            self._legacy_failpoint("binding_upgrade_after_state")
            self._spec = target
            return self._status_from_state(state)

    def start(self, run_spec: CoordinatorRunSpec | Mapping[str, Any]) -> CoordinatorStatus:
        spec = run_spec if isinstance(run_spec, CoordinatorRunSpec) else CoordinatorRunSpec.from_dict(run_spec)
        if spec.run_id != self.context.run_id:
            raise CoordinatorConflictError("run spec run_id does not match context")
        with self._locked(create=True):
            self._recover_legacy_import_locked()
            existing = _load_json(self.spec_path)
            if existing is not None or self.events_path.exists() or self.state_path.exists():
                if self._is_legacy_spec_document(existing):
                    self._prepare_persisted_spec()
                    return self._legacy_pending_status()
                if self._looks_like_legacy_spec(existing):
                    raise CoordinatorIntegrityError("coordinator specification is a malformed legacy wrapper")
                persisted = self._load_spec_document()
                state, _ = self._read_replay()
                assert state is not None
                pending_rebind = state.get("pending_plan_rebind")
                if isinstance(pending_rebind, Mapping):
                    if pending_rebind.get("new_spec_hash") != self._spec_hash(spec):
                        raise CoordinatorConflictError("a different plan rebind is already pending")
                    return self._status_from_state(state)
                if persisted.to_dict() == spec.to_dict():
                    self._spec = persisted
                    self._configure_from_spec(persisted)
                    return self._status_from_state(state)
                raise CoordinatorConflictError(
                    "coordinator already started with a different spec; use publish_and_rebind"
                )
            self._spec = spec
            self._configure_from_spec(spec)
            self._write_spec(spec)
            state = self._initial_state(spec)
            self._append_event_locked(state, "run_started", {"spec_hash": self._spec_hash(spec)})
            return self._status_from_state(state)

    @classmethod
    def from_spec(
        cls,
        context: RunContext,
        spec: CoordinatorRunSpec | Mapping[str, Any],
        **kwargs: Any,
    ) -> "RunCoordinator":
        coordinator = cls(context, **kwargs)
        parsed = spec if isinstance(spec, CoordinatorRunSpec) else CoordinatorRunSpec.from_dict(spec)
        if parsed.run_id != context.run_id:
            raise CoordinatorConflictError("run spec run_id does not match context")
        coordinator._spec = parsed
        coordinator._configure_from_spec(parsed)
        return coordinator

    @classmethod
    def from_persisted_spec(
        cls,
        context: RunContext,
        *,
        spec_path: str | os.PathLike[str] | None = None,
        owner_id: str | None = None,
        **kwargs: Any,
    ) -> "RunCoordinator":
        coordinator = cls(context, owner_id=owner_id, **kwargs)
        coordinator._recover_legacy_import()
        canonical = coordinator.spec_path
        if canonical.is_file() and not canonical.is_symlink():
            with coordinator._locked(create=False):
                coordinator._recover_legacy_import_locked()
                coordinator._recover_binding_upgrade_locked()
                spec = coordinator._prepare_persisted_spec()
        elif spec_path is not None:
            candidate = Path(spec_path).expanduser().resolve(strict=True)
            value = _load_json(candidate)
            if not isinstance(value, Mapping):
                raise CoordinatorIntegrityError("supplied coordinator spec is invalid")
            if coordinator._looks_like_legacy_spec(value):
                raise CoordinatorIntegrityError("legacy G5 import requires the canonical control plane")
            spec = CoordinatorRunSpec.from_dict(value)
            if spec.run_id != context.run_id:
                raise CoordinatorConflictError("run spec run_id does not match context")
        else:
            raise CoordinatorError("persisted coordinator spec is missing")
        coordinator._spec = spec
        coordinator._configure_from_spec(spec)
        return coordinator

    def close(self, *, wait_for_roles: bool = False) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=wait_for_roles, cancel_futures=False)
            self._executor = None


class _CallablePlannerProvider:
    def __init__(self, callback: Callable[[Mapping[str, Any]], Sequence[PlannerAction]]) -> None:
        self.callback = callback

    def next_actions(self, context: RunContext, state: Mapping[str, Any]) -> Sequence[PlannerAction]:
        return self.callback(dict(state))


def start_coordinator(
    context: RunContext,
    run_spec: CoordinatorRunSpec | Mapping[str, Any],
    **kwargs: Any,
) -> RunCoordinator:
    return RunCoordinator.from_spec(context, run_spec, **kwargs)


__all__ = [
    "CONTROL_PLANE_DIRNAME",
    "COORDINATOR_EVENTS_FILENAME",
    "COORDINATOR_LEGACY_ARCHIVE_DIRNAME",
    "COORDINATOR_LOCK_FILENAME",
    "COORDINATOR_SPEC_FILENAME",
    "COORDINATOR_STATE_FILENAME",
    "PRODUCTION_SKILL_VERSION",
    "PRODUCTION_CORE_VERSION",
    "PRODUCTION_SKILL_SHA256",
    "PRODUCTION_SKILL_FILE_COUNT",
    "PRODUCTION_SKILL_NAME",
    "PRODUCTION_RELEASE",
    "resolve_production_skill_binding",
    "CoordinatorConflictError",
    "CoordinatorError",
    "CoordinatorIntegrityError",
    "CoordinatorPublicationError",
    "CoordinatorRunSpec",
    "CoordinatorStatus",
    "CodexExecConfig",
    "CodexRoleAdapter",
    "CommandRoleAdapter",
    "MappingRoleAdapter",
    "PlannerActionProvider",
    "RequirementPlannerProvider",
    "RoleAdapter",
    "RoleExecution",
    "RoleRunner",
    "RunCoordinator",
    "build_role_prompt",
    "start_coordinator",
]
