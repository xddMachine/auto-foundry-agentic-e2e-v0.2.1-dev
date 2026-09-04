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
import importlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from threading import Condition, RLock, Thread
import time
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
import uuid

try:  # pragma: no cover - POSIX hosts provide fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .requirement_planning import (
    AUTHORIZED_ACTION_ROLE_CONTRACTS,
    PlannerAction,
)
from .workspace import DEFAULT_CORE_VERSION, DEFAULT_SKILL_VERSION, RunContext
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
COORDINATOR_TRANSPORT_REBIND_INTENT_FILENAME = ".coordinator_transport_rebind.intent.json"
COORDINATOR_ROLE_SESSIONS_FILENAME = "role_sessions.json"
COORDINATOR_SCHEMA_VERSION = 1
# Role-session persistence has its own schema so continuity can evolve
# independently from the coordinator event-chain schema.
ROLE_SESSION_SCHEMA_VERSION = 2
DEFAULT_LEASE_TTL_SECONDS = 30.0
MAX_ROLE_DIAGNOSTIC_BYTES = 32_768
MAX_RUN_RETRIES_PER_ACTION = 2
TERMINAL_STATUSES = frozenset({"complete", "complete_with_limits"})
PRODUCT_REGENERATION_ORIGIN = "operator_product_regeneration"

# The coordinator is the last common boundary before a role transport is
# invoked.  PlannerAction's typed contracts are the single authority; this
# derived projection is read-only and therefore cannot drift from planning.
ROLE_ACTION_CONTRACT: Mapping[str, str] = MappingProxyType(
    {contract.action: contract.role for contract in AUTHORIZED_ACTION_ROLE_CONTRACTS}
)
# Compatibility-shaped reverse projection for callers that need to inspect
# ownership as a set; values are still derived exclusively from the typed
# Planner contract above and contain exactly one authorized role.
ACTION_ROLE_CONTRACT: Mapping[str, frozenset[str]] = MappingProxyType(
    {action: frozenset({role}) for action, role in ROLE_ACTION_CONTRACT.items()}
)

# Only dispatchable roles receive a model route.  Intake, supervisor, and
# Planner/rethink actions remain typed control records and intentionally have
# no transport route.
ROLE_MODEL_CONTRACT: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        # Intake and supervision are explicitly typed host control routes.
        # They are not Planner model actions, but launch admission invokes
        # these two boundaries directly and therefore needs the same reviewed
        # model manifest as the model-backed role set.
        "intake_planner": MappingProxyType({"model": "gpt-5.6-sol", "reasoning_effort": "high"}),
        "foundry_supervisor": MappingProxyType({"model": "gpt-5.6-sol", "reasoning_effort": "high"}),
        "analytical_owner": MappingProxyType({"model": "gpt-5.6-sol", "reasoning_effort": "high"}),
        "business_reviewer": MappingProxyType({"model": "gpt-5.6-sol", "reasoning_effort": "high"}),
        "identity_reviewer": MappingProxyType({"model": "gpt-5.6-sol", "reasoning_effort": "high"}),
        "integration_fidelity_reviewer": MappingProxyType({"model": "gpt-5.6-sol", "reasoning_effort": "high"}),
        "product_reviewer": MappingProxyType({"model": "gpt-5.6-sol", "reasoning_effort": "high"}),
        "entity_resolution_owner": MappingProxyType({"model": "gpt-5.6-luna", "reasoning_effort": "max"}),
        "integration_agent": MappingProxyType({"model": "gpt-5.6-luna", "reasoning_effort": "max"}),
        "product_agent": MappingProxyType({"model": "gpt-5.6-luna", "reasoning_effort": "max"}),
        "reporting_agent": MappingProxyType({"model": "gpt-5.6-luna", "reasoning_effort": "max"}),
        "specialist": MappingProxyType({"model": "gpt-5.6-luna", "reasoning_effort": "max"}),
    }
)


def production_role_routing() -> dict[str, dict[str, str]]:
    """Return a detached copy of the complete production role route map."""

    return {role: dict(route) for role, route in ROLE_MODEL_CONTRACT.items()}


def role_model_route(role: str) -> dict[str, str]:
    """Return the explicit model/reasoning route for one known role."""

    name = _text(role, "role").lower()
    route = ROLE_MODEL_CONTRACT.get(name)
    if route is None:
        raise CoordinatorIntegrityError(f"role has no production model route: {name}")
    return dict(route)


def validate_role_action_contract(action: PlannerAction | Mapping[str, Any]) -> PlannerAction:
    """Validate known Planner action ownership before transport admission."""

    current = _action(action)
    role = current.role.lower()
    name = current.action.lower()
    expected_role = ROLE_ACTION_CONTRACT.get(name)
    if expected_role is None:
        raise CoordinatorIntegrityError(f"Planner action has no canonical role contract: {name}")
    if role != expected_role:
        raise CoordinatorIntegrityError(
            f"Planner action {name!r} is bound to role {expected_role!r}; received {role!r}"
        )
    # Planner/intake/supervisor controls are deterministic records and never
    # enter a model transport.  Every other canonical role must have an
    # explicit production route; no ambient/default fallback is permitted.
    if name not in _CONTROL_ACTION_NAMES and role not in ROLE_MODEL_CONTRACT:
        raise CoordinatorIntegrityError(f"Planner role has no production model route: {role}")
    return current


def role_route_for_action(action: PlannerAction | Mapping[str, Any]) -> dict[str, str]:
    """Validate an action and return its stable production model route."""

    current = validate_role_action_contract(action)
    return role_model_route(current.role)


# Names retained as explicit aliases for launch integration code.  They point
# at the same immutable-by-convention manifest and are intentionally exported
# rather than reconstructed in ``launch.py``.
PRODUCTION_ROLE_MODEL_CONTRACT = ROLE_MODEL_CONTRACT
PRODUCTION_ROLE_ACTION_CONTRACT = ROLE_ACTION_CONTRACT

# A role session is a logical-owner continuity primitive, not a general
# transport cache.  Reviewers and bounded specialists intentionally remain
# fresh/independent on every dispatch.  The role/action split is kept in the
# coordinator so adapters cannot accidentally make a reviewer resumable.
_RESUMABLE_ROLE_ACTIONS = {
    "analytical_owner": frozenset(
        {"analyze_requirement", "resume_requirement_analysis", "repair_requirement"}
    ),
    "integration_agent": frozenset(
        {"integrate_requirement", "repair_integration_fidelity"}
    ),
    "entity_resolution_owner": frozenset(
        {"resolve_identity", "resume_identity_resolution", "repair_identity_result"}
    ),
    "product_agent": frozenset(
        {"refresh_product_preview", "build_product_candidate", "build_final_product", "publish_final_product"}
    ),
}
_FRESH_ROLE_NAMES = frozenset(
    {
        "business_reviewer",
        "identity_reviewer",
        "integration_fidelity_reviewer",
        "product_reviewer",
        "specialist",
    }
)
_ROLE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")

# Reservations are per logical-owner, never a process-wide execution lock.
# The in-process guard closes the small prepare/spawn race for concurrent
# callers in one interpreter; the durable registry and coordinator active
# dispatch evidence protect cross-process/restart paths.
_ROLE_RESERVATION_LOCK = RLock()
_LOCAL_ROLE_RESERVATIONS: dict[tuple[str, str], str] = {}

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

# Identity-domain actions do not carry a requirement ``item_id``.  When one
# of these executable actions exhausts the existing retry evidence, the
# failed domain's active requester requirements are the item-local boundary;
# an unbound/foreign domain remains a run-level integrity concern.
_IDENTITY_EXECUTABLE_ACTIONS = frozenset(
    {
        "resolve_identity",
        "resume_identity_resolution",
        "repair_identity_result",
        "review_identity_result",
        "commit_identity_result",
    }
)

# The Control Center binds every production Codex role to this release.  The
# digest is the deterministic extracted-tree identity emitted by the reviewed
# release packager.  These small tracked constants are the runtime manifest;
# provisioning the ZIP is deliberately outside the run path.
PRODUCTION_SKILL_VERSION = DEFAULT_SKILL_VERSION
PRODUCTION_CORE_VERSION = DEFAULT_CORE_VERSION
# Updated to the deterministic v0.8.0 package after the release artifact is
# built.  Keeping this tracked manifest independent of ``dist/`` lets a fresh
# checkout validate an already-installed skill without requiring ignored
# package output.
PRODUCTION_SKILL_SHA256 = "320c6950a2d910beb3bf7f2e2986d1fd60bbb0a2601240a08b9b03bce4b384ab"
PRODUCTION_SKILL_FILE_COUNT = 31
PRODUCTION_SKILL_NAME = "auto-foundry-agentic-e2e"
PRODUCTION_RELEASE = "reliable-analytics-dashboard"

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


class CoordinatorProductionBindingMismatch(CoordinatorIntegrityError):
    """The persisted production skill binding differs from the active release.

    This is deliberately narrower than :class:`CoordinatorIntegrityError`.
    Read-only Control Center status may project a post-rebind Product intent
    only for this exact, otherwise-valid release-rotation boundary; malformed
    state, recovery intents, or skill trees must remain ordinary integrity
    failures and never receive that fallback.
    """


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


def _load_psutil() -> Any | None:
    """Load the required process-identity provider only when needed.

    ``auto_foundry_core.analysis`` is intentionally usable inside a stripped
    child runtime that does not expose coordinator dependencies.  Keep this
    import local to process-identity checks; a missing provider is an explicit
    fail-closed result when coordinator code actually asks for ownership.
    """

    try:
        return importlib.import_module("psutil")
    except (ImportError, OSError):
        return None


def _process_start_token(pid: Any = None) -> str | None:
    """Return an exact process-start identity for one local PID.

    Linux exposes a monotonic start-time tick in ``/proc/<pid>/stat``.  On
    macOS (and on platforms without ``/proc``), use the required ``psutil``
    process creation time.  ``None`` is an explicit *unknown/dead* result:
    callers that need ownership proof must not infer liveness from a PID alone.
    """

    process_util = _load_psutil()
    if process_util is None:
        return None
    try:
        value = os.getpid() if pid is None else int(pid)
    except (TypeError, ValueError, OSError):
        return None
    if value <= 0:
        return None
    proc_stat = Path(f"/proc/{value}/stat")
    if proc_stat.is_file() and not proc_stat.is_symlink():
        try:
            raw = proc_stat.read_text(encoding="utf-8")
            # ``comm`` (field 2) may contain spaces and parentheses.  The
            # final closing parenthesis terminates that field; field 22
            # (starttime) is then index 19 in the remaining field-3 slice.
            closing = raw.rfind(")")
            fields = raw[closing + 2 :].split() if closing >= 0 else []
            if len(fields) > 19 and fields[19].isdigit():
                return f"proc:{fields[19]}"
        except (OSError, UnicodeDecodeError, IndexError):
            pass
    # ``psutil`` is a required core dependency and exposes the same
    # process-start identity portably on macOS and Linux without relying on a
    # shell or the adapter's monkeypatchable subprocess transport.
    try:
        started = process_util.Process(value).create_time()
        return f"psutil:{float(started):.6f}"
    except Exception:
        pass
    return None


def _required_process_start_token(pid: Any = None) -> str:
    """Return an exact process-start token or fail closed before dispatch."""

    token = _process_start_token(pid)
    if token is None:
        raise CoordinatorIntegrityError("exact process start identity is unavailable")
    return token


def _process_identity_matches(pid: Any, process_start: Any) -> bool | None:
    """Compare a persisted PID/start token with the current local process.

    ``True`` proves the exact process instance is still present, ``False``
    proves a live PID was reused with a different start token, and ``None``
    means the owner is gone or the platform could not provide a start token.
    The final state must never treat ``None`` as proof of liveness.
    """

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    # A failed OS liveness probe means the owner process is gone (an orphan),
    # not that a replacement process inherited its PID.  Return ``None`` so
    # callers can distinguish death from a live PID whose start token differs.
    if not _pid_alive(pid):
        return None
    if not isinstance(process_start, str) or not process_start.strip():
        return None
    current = _process_start_token(pid)
    if current is None:
        return None
    return current == process_start.strip()


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


def _role_session_identity(
    action: PlannerAction | Mapping[str, Any],
    *,
    run_id: str,
    generation_id: str,
) -> tuple[str, str] | None:
    """Return ``(logical_owner_key, role)`` for a resumable action.

    The key deliberately uses the logical subject rather than an invocation
    id.  Invocation ids and prompts are transport details; the durable
    registry binds one exact Codex root session to this owner and subject.
    Generation is recorded on the registry entry and checked before resume,
    so a successor generation cannot accidentally continue an old workspace.
    """

    current = _action(action)
    role = current.role.strip().lower()
    name = current.action.strip().lower()
    allowed = _RESUMABLE_ROLE_ACTIONS.get(role)
    if allowed is None or name not in allowed:
        return None
    if role == "product_agent":
        # Product ownership is explicitly per generation/run.  The Planner
        # subject is the run id, but keeping both values in the key avoids a
        # successor generation sharing the previous product session.
        key = f"{role}:{run_id}:{generation_id}"
    else:
        key = f"{role}:{current.subject_id}"
    return key, role


def logical_action_fingerprint(
    action: PlannerAction | Mapping[str, Any],
    *,
    run_id: str | None = None,
    generation_id: str | None = None,
    product_regeneration_request_id: str | None = None,
) -> str:
    """Return the durable retry key for one logical Planner action.

    Resumable role actions deliberately share one fingerprint across the
    initial/resume/repair transport labels.  A replacement process therefore
    continues the same logical attempt instead of resetting or consuming a
    second retry budget merely because its transport action name changed.
    Explicit Product regeneration requests add their durable request id so a
    successor revision cannot inherit retry exhaustion from its predecessor;
    the id is accepted only when the caller supplies the matching durable
    request boundary.
    Non-resumable actions retain their action name as part of the key so an
    independent review/commit phase cannot inherit another phase's retries.
    """

    current = _action(action)
    metadata = current.metadata if isinstance(current.metadata, Mapping) else {}
    authoritative_request_id = (
        product_regeneration_request_id.strip()
        if isinstance(product_regeneration_request_id, str) and product_regeneration_request_id.strip()
        else None
    )
    role_identity = _role_session_identity(
        current,
        run_id=str(run_id or ""),
        generation_id=str(generation_id or ""),
    )
    if role_identity is not None and current.action.strip().lower() == "refresh_product_preview":
        # Preview retries are isolated from the final candidate/review flow,
        # while the same run/generation Product Agent session remains
        # resumable.  The canonical input fingerprint is the only retry
        # dimension: unchanged inputs reuse their budget; newly committed
        # facts produce a fresh retry key.
        logical_name = f"{role_identity[0]}:preview:{metadata.get('input_fingerprint', '')}"
    else:
        logical_name = role_identity[0] if role_identity is not None else (
            f"{current.role.strip().lower()}:{current.subject_id}:{current.action.strip().lower()}"
        )
    regeneration_request_id = metadata.get("product_regeneration_request_id")
    if (
        metadata.get("authorization_origin") == PRODUCT_REGENERATION_ORIGIN
        and isinstance(regeneration_request_id, str)
        and regeneration_request_id.strip()
        and authoritative_request_id == regeneration_request_id.strip()
        and current.role.strip().lower() in {"product_agent", "product_reviewer"}
        and current.action.strip().lower() in {"build_product_candidate", "review_final_product"}
    ):
        logical_name = f"{logical_name}:regeneration:{authoritative_request_id}"
    return _sha256_value(
        {
            "run_id": run_id,
            "generation_id": generation_id,
            "logical_action": logical_name,
        }
    )


_CONTROL_ACTION_NAMES = frozenset(
    contract.action
    for contract in AUTHORIZED_ACTION_ROLE_CONTRACTS
    if contract.role in {"planner", "intake_planner", "foundry_supervisor", "control_plane"}
)

_CONTROL_ACTION_ROLES = frozenset(
    {"planner", "intake_planner", "foundry_supervisor", "control_plane"}
)


def _is_control_action(action: PlannerAction | Mapping[str, Any]) -> bool:
    """Return whether an offer is a deterministic coordinator control step."""

    current = _action(action)
    role = current.role.strip().lower()
    name = current.action.strip().lower()
    # The typed Planner action/role contract is the only control boundary.
    # Metadata is advisory evidence and must never turn an executable role
    # action into a non-dispatchable control record (or vice versa).
    contract_role = ROLE_ACTION_CONTRACT.get(name)
    return contract_role in _CONTROL_ACTION_ROLES and role == contract_role


def _is_preview_action(action: PlannerAction | Mapping[str, Any]) -> bool:
    """Return whether an action is the isolated incremental preview path."""

    return _action(action).action.strip().lower() == "refresh_product_preview"


def _is_fresh_role(action: PlannerAction | Mapping[str, Any]) -> bool:
    """Return whether an action must always use a fresh/independent role."""

    current = _action(action)
    role = current.role.strip().lower()
    return (
        role in _FRESH_ROLE_NAMES
        or current.metadata.get("fresh_role") is True
    )


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
        # Persisted specs are parsed before the active transport is
        # configured.  Defer local/active skill-byte validation to
        # CodexRoleAdapter so release rotation can be classified as the
        # dedicated production-binding mismatch rather than a generic parse
        # failure, while malformed nested fields still fail here.
        CodexExecConfig.from_dict(codex_exec, validate_binding=False)
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
    # The following fields are privacy-safe continuity metadata.  They contain
    # only the exact Codex root id and registry state; prompts, model replies,
    # and command strings never enter the role-session registry.
    session_id: str | None = None
    session_key: str | None = None
    session_status: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        value = {
            "exit_code": self.exit_code,
            "output": _safe_text(self.output),
            "error": _safe_text(self.error),
            "timed_out": bool(self.timed_out),
        }
        if self.session_id is not None:
            value["session_id"] = self.session_id
        if self.session_key is not None:
            value["session_key"] = self.session_key
        if self.session_status is not None:
            value["session_status"] = self.session_status
        return value


class RoleSessionRegistry:
    """Atomic run-local registry for resumable logical role sessions.

    The registry is intentionally separate from Planner and role payloads.  A
    row contains only ownership/lineage ids, a root Codex session id, and a
    short privacy-safe audit trail.  Writes use the coordinator lock and the
    existing fsync + replace helper, so a crash can leave either the prior
    complete document or the next complete document, never a partial JSON
    record.
    """

    _ENTRY_FIELDS = frozenset(
        {
            "logical_owner",
            "role",
            "subject_id",
            "run_id",
            "generation_id",
            "session_id",
            "status",
            "replacement_required",
            "stale_reason",
            "created_at",
            "updated_at",
            "last_action",
            "last_idempotency_key",
            "replacement_of",
            "reservation_token",
            "reservation_status",
            "reservation_action",
            "reservation_owner_id",
            "reservation_pid",
            "reservation_process_start",
            "action_lineage",
            "audit",
        }
    )
    _AUDIT_FIELDS = frozenset(
        {"event", "at", "action", "subject_id", "idempotency_key", "reason", "session_id"}
    )
    _STATUSES = frozenset({"active", "reserved", "replacement_required"})
    _RESERVATION_STATUSES = frozenset({"reserved"})
    _REASONS = frozenset(
        {
            "thread_started_missing",
            "resume_session_unavailable",
            "resume_session_mismatch",
            "lineage_mismatch",
            "invocation_failed",
            "invalid_registry_entry",
            "orphaned_reservation",
            "reservation_owner_mismatch",
            "reservation_owner_unknown",
            "reservation_conflict",
        }
    )

    def __init__(self, context: RunContext) -> None:
        if not isinstance(context, RunContext):
            raise TypeError("RoleSessionRegistry requires a RunContext")
        self.context = context
        self.control_plane = context.resolve_run_path(CONTROL_PLANE_DIRNAME)
        self.path = self.control_plane / COORDINATOR_ROLE_SESSIONS_FILENAME
        self.lock_path = self.control_plane / COORDINATOR_LOCK_FILENAME

    @property
    def registry_path(self) -> Path:
        """Compatibility/readability alias for the durable registry path."""

        return self.path

    @staticmethod
    def _empty(run_id: str) -> dict[str, Any]:
        return {
            "schema_version": ROLE_SESSION_SCHEMA_VERSION,
            "kind": "role_session_registry",
            "run_id": run_id,
            "sessions": {},
        }

    @staticmethod
    def _safe_identifier(value: Any, name: str, *, allow_none: bool = False) -> str | None:
        if value is None and allow_none:
            return None
        if not isinstance(value, str):
            raise CoordinatorIntegrityError(f"role session {name} is invalid")
        value = value.strip()
        if not value or len(value) > 256 or "\x00" in value:
            raise CoordinatorIntegrityError(f"role session {name} is invalid")
        return value

    @classmethod
    def _validate_entry(cls, key: str, raw: Any, *, run_id: str) -> dict[str, Any]:
        if not isinstance(key, str) or not key:
            raise CoordinatorIntegrityError("role session logical owner key is invalid")
        if not isinstance(raw, Mapping) or set(raw) != cls._ENTRY_FIELDS:
            raise CoordinatorIntegrityError("role session entry fields are invalid")
        value = dict(_canonical(raw))
        if value.get("logical_owner") != key or value.get("run_id") != run_id:
            raise CoordinatorIntegrityError("role session entry ownership is invalid")
        for name in ("logical_owner", "role", "subject_id", "run_id", "generation_id", "created_at", "updated_at"):
            cls._safe_identifier(value.get(name), name)
        session_id = cls._safe_identifier(value.get("session_id"), "session_id", allow_none=True)
        if session_id is not None and _ROLE_SESSION_ID_RE.fullmatch(session_id) is None:
            raise CoordinatorIntegrityError("role session session_id is invalid")
        replacement_of = cls._safe_identifier(value.get("replacement_of"), "replacement_of", allow_none=True)
        if replacement_of is not None and _ROLE_SESSION_ID_RE.fullmatch(replacement_of) is None:
            raise CoordinatorIntegrityError("role session replacement_of is invalid")
        status = value.get("status")
        if status not in cls._STATUSES:
            raise CoordinatorIntegrityError("role session status is invalid")
        if not isinstance(value.get("replacement_required"), bool):
            raise CoordinatorIntegrityError("role session replacement state is invalid")
        if value["replacement_required"] != (status == "replacement_required"):
            raise CoordinatorIntegrityError("role session replacement state does not match status")
        reservation_status = value.get("reservation_status")
        reservation_token = cls._safe_identifier(value.get("reservation_token"), "reservation_token", allow_none=True)
        reservation_action = cls._safe_identifier(value.get("reservation_action"), "reservation_action", allow_none=True)
        reservation_owner_id = cls._safe_identifier(
            value.get("reservation_owner_id"), "reservation_owner_id", allow_none=True
        )
        reservation_pid = value.get("reservation_pid")
        reservation_process_start = cls._safe_identifier(
            value.get("reservation_process_start"), "reservation_process_start", allow_none=True
        )
        if status == "reserved":
            if (
                reservation_status not in cls._RESERVATION_STATUSES
                or reservation_token is None
                or reservation_action is None
                or reservation_owner_id is None
            ):
                raise CoordinatorIntegrityError("role session reservation is incomplete")
            if isinstance(reservation_pid, bool) or not isinstance(reservation_pid, int) or reservation_pid <= 0:
                raise CoordinatorIntegrityError("role session reservation owner is invalid")
        elif (
            reservation_status is not None
            or reservation_token is not None
            or reservation_action is not None
            or reservation_owner_id is not None
            or reservation_pid is not None
            or reservation_process_start is not None
        ):
            raise CoordinatorIntegrityError("role session reservation state is unexpected")
        reason = value.get("stale_reason")
        if reason is not None and reason not in cls._REASONS:
            raise CoordinatorIntegrityError("role session stale reason is invalid")
        cls._safe_identifier(value.get("last_action"), "last_action", allow_none=True)
        cls._safe_identifier(value.get("last_idempotency_key"), "last_idempotency_key", allow_none=True)
        lineage = value.get("action_lineage")
        if not isinstance(lineage, list) or not lineage:
            raise CoordinatorIntegrityError("role session action lineage is invalid")
        for item in lineage:
            if not isinstance(item, Mapping) or set(item) != {"action", "subject_id", "idempotency_key", "at"}:
                raise CoordinatorIntegrityError("role session action lineage entry is invalid")
            cls._safe_identifier(item.get("action"), "lineage action")
            cls._safe_identifier(item.get("subject_id"), "lineage subject_id")
            cls._safe_identifier(item.get("idempotency_key"), "lineage idempotency_key")
            cls._safe_identifier(item.get("at"), "lineage at")
        audit = value.get("audit")
        if not isinstance(audit, list) or not audit:
            raise CoordinatorIntegrityError("role session audit is invalid")
        for item in audit:
            if not isinstance(item, Mapping) or set(item) != cls._AUDIT_FIELDS:
                raise CoordinatorIntegrityError("role session audit entry is invalid")
            cls._safe_identifier(item.get("event"), "audit event")
            cls._safe_identifier(item.get("at"), "audit at")
            cls._safe_identifier(item.get("action"), "audit action")
            cls._safe_identifier(item.get("subject_id"), "audit subject_id")
            cls._safe_identifier(item.get("idempotency_key"), "audit idempotency_key")
            cls._safe_identifier(item.get("reason"), "audit reason", allow_none=True)
            cls._safe_identifier(item.get("session_id"), "audit session_id", allow_none=True)
        return value

    @classmethod
    def _validate_document(cls, raw: Any, *, run_id: str) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise CoordinatorIntegrityError("role session registry must be an object")
        expected = {"schema_version", "kind", "run_id", "sessions"}
        if set(raw) != expected:
            raise CoordinatorIntegrityError("role session registry fields are invalid")
        if raw.get("schema_version") != ROLE_SESSION_SCHEMA_VERSION or raw.get("kind") != "role_session_registry":
            raise CoordinatorIntegrityError("role session registry version or kind is invalid")
        if raw.get("run_id") != run_id:
            raise CoordinatorIntegrityError("role session registry run_id does not match context")
        sessions = raw.get("sessions")
        if not isinstance(sessions, Mapping):
            raise CoordinatorIntegrityError("role session registry sessions are invalid")
        validated = cls._empty(run_id)
        validated["sessions"] = {}
        for key, value in sessions.items():
            if not isinstance(key, str):
                raise CoordinatorIntegrityError("role session logical owner key is invalid")
            validated["sessions"][key] = cls._validate_entry(key, value, run_id=run_id)
        return validated

    @contextmanager
    def _locked(self) -> Iterable[None]:
        if self.control_plane.is_symlink():
            raise CoordinatorIntegrityError("role session control plane cannot be a symlink")
        self.control_plane.mkdir(parents=True, exist_ok=True)
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

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty(self.context.run_id)
        if self.path.is_symlink() or not self.path.is_file():
            raise CoordinatorIntegrityError("role session registry is not a regular file")
        value = _load_json(self.path)
        return self._validate_document(value, run_id=self.context.run_id)

    def _write_unlocked(self, document: Mapping[str, Any]) -> None:
        validated = self._validate_document(document, run_id=self.context.run_id)
        _atomic_json(self.path, validated)

    def read(self) -> dict[str, Any]:
        """Read and validate the complete registry without mutating it."""

        if not self.control_plane.exists() and not self.path.exists():
            return self._empty(self.context.run_id)
        with self._locked():
            return self._read_unlocked()

    load = read

    def get(self, logical_owner: str) -> Mapping[str, Any] | None:
        key = self._safe_identifier(logical_owner, "logical_owner")
        assert key is not None
        value = self.read()["sessions"].get(key)
        return dict(value) if isinstance(value, Mapping) else None

    @staticmethod
    def _lineage_entry(action: PlannerAction, idempotency_key: str, timestamp: str) -> dict[str, str]:
        return {
            "action": action.action,
            "subject_id": action.subject_id,
            "idempotency_key": str(idempotency_key),
            "at": timestamp,
        }

    @staticmethod
    def _audit_entry(
        event: str,
        action: PlannerAction,
        idempotency_key: str,
        timestamp: str,
        *,
        reason: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "event": event,
            "at": timestamp,
            "action": action.action,
            "subject_id": action.subject_id,
            "idempotency_key": str(idempotency_key),
            "reason": reason,
            "session_id": session_id,
        }

    @staticmethod
    def _token(value: Any, name: str = "idempotency_key") -> str:
        token = RoleSessionRegistry._safe_identifier(value, name)
        assert token is not None
        return token

    def _reservation_scope(self, logical_owner: str) -> tuple[str, str]:
        return (str(self.path), logical_owner)

    @staticmethod
    def _local_reservation(scope: tuple[str, str]) -> str | None:
        with _ROLE_RESERVATION_LOCK:
            return _LOCAL_ROLE_RESERVATIONS.get(scope)

    @staticmethod
    def _claim_local_reservation(scope: tuple[str, str], token: str) -> bool:
        with _ROLE_RESERVATION_LOCK:
            existing = _LOCAL_ROLE_RESERVATIONS.get(scope)
            if existing is not None and existing != token:
                return False
            _LOCAL_ROLE_RESERVATIONS[scope] = token
            return True

    def release_reservation(self, logical_owner: str, reservation_token: str) -> None:
        """Release the process-local prepare/spawn guard for one owner.

        The durable reservation is deliberately retained.  A process crash
        therefore leaves an auditable orphan rather than silently reopening a
        new root session; this method only removes the in-memory contention
        marker after the dispatch has reached a terminal transport path.
        """

        scope = self._reservation_scope(logical_owner)
        token = self._token(reservation_token, "reservation_token")
        with _ROLE_RESERVATION_LOCK:
            if _LOCAL_ROLE_RESERVATIONS.get(scope) == token:
                _LOCAL_ROLE_RESERVATIONS.pop(scope, None)

    @classmethod
    def _lineage_with_action(
        cls,
        lineage: Iterable[Mapping[str, Any]],
        action: PlannerAction,
        idempotency_key: str,
        timestamp: str,
    ) -> list[dict[str, Any]]:
        values = [dict(item) for item in lineage if isinstance(item, Mapping)]
        if not any(
            item.get("action") == action.action
            and item.get("subject_id") == action.subject_id
            and item.get("idempotency_key") == str(idempotency_key)
            for item in values
        ):
            values.append(cls._lineage_entry(action, idempotency_key, timestamp))
        return values[-32:]

    @classmethod
    def _audit_with_entry(
        cls,
        audit: Iterable[Mapping[str, Any]],
        entry: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        values = [dict(item) for item in audit if isinstance(item, Mapping)]
        values.append(dict(entry))
        return values[-32:]

    def _reserved_entry(
        self,
        action: PlannerAction,
        *,
        generation_id: str,
        reservation_token: str,
        reservation_owner_id: str | None = None,
        reservation_process_start: str | None = None,
        timestamp: str,
        reservation_pid: int | None = None,
        session_id: str | None = None,
        replacement_of: str | None = None,
        lineage: Iterable[Mapping[str, Any]] = (),
        audit: Iterable[Mapping[str, Any]] = (),
        event: str = "role_session_reserved",
        reason: str | None = None,
    ) -> dict[str, Any]:
        owner_id = self._token(
            reservation_owner_id or f"registry-{uuid.uuid4().hex}",
            "reservation_owner_id",
        )
        process_start = self._safe_identifier(
            reservation_process_start,
            "reservation_process_start",
            allow_none=True,
        )
        if process_start is None:
            raise CoordinatorIntegrityError("exact process start identity is unavailable")
        value = {
            "logical_owner": _role_session_identity(
                action,
                run_id=self.context.run_id,
                generation_id=generation_id,
            )[0],
            "role": action.role,
            "subject_id": action.subject_id,
            "run_id": self.context.run_id,
            "generation_id": generation_id,
            "session_id": session_id,
            "status": "reserved",
            "replacement_required": False,
            "stale_reason": None,
            "created_at": timestamp,
            "updated_at": timestamp,
            "last_action": None,
            "last_idempotency_key": None,
            "replacement_of": replacement_of,
            "reservation_token": reservation_token,
            "reservation_status": "reserved",
            "reservation_action": action.action,
            "reservation_owner_id": owner_id,
            "reservation_pid": int(reservation_pid if reservation_pid is not None else os.getpid()),
            "reservation_process_start": process_start,
            "action_lineage": self._lineage_with_action(lineage, action, reservation_token, timestamp),
            "audit": self._audit_with_entry(
                audit,
                self._audit_entry(
                    event,
                    action,
                    reservation_token,
                    timestamp,
                    reason=reason,
                    session_id=session_id,
                ),
            ),
        }
        return value

    @staticmethod
    def _response(
        mode: str,
        *,
        key: str,
        role: str,
        existing: Mapping[str, Any] | None = None,
        reservation_token: str | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "mode": mode,
            "logical_owner": key,
            "role": role,
        }
        if existing is not None:
            value["session_id"] = existing.get("session_id")
            value["status"] = existing.get("status")
            value["reservation_token"] = existing.get("reservation_token")
            value["reservation_status"] = existing.get("reservation_status")
            value["reservation_owner_id"] = existing.get("reservation_owner_id")
            value["reservation_process_start"] = existing.get("reservation_process_start")
        if reservation_token is not None:
            value["reservation_token"] = reservation_token
            value["reservation_status"] = "reserved"
        return value

    def prepare(
        self,
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        allow_replacement: bool = False,
        active_dispatch_tokens: Iterable[str] = (),
        active_dispatch_owner: Mapping[str, Any] | None = None,
        reservation_owner_id: str | None = None,
        reservation_pid: int | None = None,
        reservation_process_start: str | None = None,
        active_dispatch_owner_pid: int | None = None,
    ) -> dict[str, Any]:
        """Reserve one logical owner before any model process is spawned.

        The reservation is an atomic compare-and-set boundary.  It is written
        while holding the registry lock and contains the dispatch token, so a
        second caller cannot observe an unclaimed owner between ``prepare``
        and process creation.  A reservation without a bound session is an
        orphan after a crash and requires explicit replacement; a reservation
        with an exact bound session may resume that same session.
        """

        identity = _role_session_identity(
            action,
            run_id=self.context.run_id,
            generation_id=generation_id,
        )
        if identity is None:
            return {"mode": "fresh", "logical_owner": None, "role": action.role}
        key, role = identity
        token = self._token(idempotency_key)
        active_owner = active_dispatch_owner if isinstance(active_dispatch_owner, Mapping) else {}
        owner_id_value = (
            reservation_owner_id
            if reservation_owner_id is not None
            else active_owner.get("owner_id")
        )
        owner_id = (
            self._token(owner_id_value, "reservation_owner_id")
            if isinstance(owner_id_value, str) and owner_id_value.strip()
            else self._token(f"registry-{uuid.uuid4().hex}", "reservation_owner_id")
        )
        owner_pid = reservation_pid if reservation_pid is not None else active_owner.get("pid")
        if owner_pid is None:
            owner_pid = os.getpid()
        elif isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
            raise ValueError("reservation_pid must be a positive integer")
        if active_dispatch_owner_pid is not None:
            if (
                isinstance(active_dispatch_owner_pid, bool)
                or not isinstance(active_dispatch_owner_pid, int)
                or active_dispatch_owner_pid <= 0
            ):
                raise ValueError("active_dispatch_owner_pid must be a positive integer")
            owner_pid = active_dispatch_owner_pid
        process_start_value = (
            reservation_process_start
            if reservation_process_start is not None
            else active_owner.get("process_start")
        )
        if process_start_value is None:
            if owner_pid != os.getpid():
                raise CoordinatorIntegrityError("exact process start identity is unavailable for reservation owner")
            process_start = _required_process_start_token(owner_pid)
        else:
            process_start = self._safe_identifier(
                process_start_value,
                "reservation_process_start",
                allow_none=False,
            )
            assert process_start is not None
        active_tokens = {
            self._token(value, "active_dispatch_token")
            for value in active_dispatch_tokens
            if isinstance(value, str) and value.strip()
        }
        scope = self._reservation_scope(key)
        with _ROLE_RESERVATION_LOCK:
            local_token = _LOCAL_ROLE_RESERVATIONS.get(scope)
        if local_token is not None:
            with self._locked():
                existing = self._read_unlocked()["sessions"].get(key)
            return self._response("blocked", key=key, role=role, existing=existing, reservation_token=local_token)
        with self._locked():
            # Re-check after acquiring the durable lock. Another thread may
            # have observed an empty local guard immediately before the first
            # caller persisted and claimed this reservation; without this
            # second read it could mark the live reservation stale while the
            # winner is already spawning its root process.
            with _ROLE_RESERVATION_LOCK:
                locked_local_token = _LOCAL_ROLE_RESERVATIONS.get(scope)
            if locked_local_token is not None:
                existing = self._read_unlocked()["sessions"].get(key)
                return self._response(
                    "blocked",
                    key=key,
                    role=role,
                    existing=existing,
                    reservation_token=locked_local_token,
                )
            document = self._read_unlocked()
            existing = document["sessions"].get(key)
            if existing is None:
                value = self._reserved_entry(
                    action,
                    generation_id=generation_id,
                    reservation_token=token,
                    reservation_owner_id=owner_id,
                    reservation_process_start=process_start,
                    timestamp=_now(),
                    reservation_pid=owner_pid,
                )
                document["sessions"][key] = value
                self._write_unlocked(document)
                if not self._claim_local_reservation(scope, token):
                    # Another in-process caller claimed the owner between the
                    # registry write and local guard.  Leave the durable
                    # reservation intact and let that caller remain the sole
                    # transport owner.
                    return self._response("blocked", key=key, role=role, existing=value, reservation_token=token)
                return self._response("new", key=key, role=role, existing=value, reservation_token=token)
            if existing.get("run_id") != self.context.run_id or existing.get("generation_id") != generation_id:
                stale = self._mark_stale_unlocked(
                    document,
                    key,
                    action,
                    generation_id=generation_id,
                    idempotency_key=token,
                    reason="lineage_mismatch",
                    reservation_token=(existing.get("reservation_token") if existing.get("status") == "reserved" else None),
                )
                if allow_replacement:
                    value = self._reserve_replacement_unlocked(
                        document,
                        key,
                        action,
                        generation_id=generation_id,
                        reservation_token=token,
                        reservation_owner_id=owner_id,
                        reservation_process_start=process_start,
                        reservation_pid=owner_pid,
                        reason="lineage_mismatch",
                    )
                    if self._claim_local_reservation(scope, token):
                        return self._response("replace", key=key, role=role, existing=value, reservation_token=token)
                return stale
            status = existing.get("status")
            if status == "replacement_required":
                if allow_replacement:
                    value = self._reserve_replacement_unlocked(
                        document,
                        key,
                        action,
                        generation_id=generation_id,
                        reservation_token=token,
                        reservation_owner_id=owner_id,
                        reservation_process_start=process_start,
                        reservation_pid=owner_pid,
                        reason=existing.get("stale_reason") or "lineage_mismatch",
                    )
                    if self._claim_local_reservation(scope, token):
                        return self._response("replace", key=key, role=role, existing=value, reservation_token=token)
                return self._response("blocked", key=key, role=role, existing=existing)
            if status == "reserved":
                # Only an exact active-dispatch identity proves that this
                # reservation is still in flight.  PID-only checks are not
                # sufficient: a reused PID or an abandoned adapter thread
                # must be recoverable rather than permanently blocking.
                existing_token = existing.get("reservation_token")
                existing_start = existing.get("reservation_process_start")
                if not isinstance(existing_start, str) or not existing_start.strip():
                    # Rows written by an older/partially upgraded runtime do
                    # not carry an exact process-start token. They cannot be
                    # resumed safely, even when their PID matches this
                    # process; surface an explicit replacement boundary.
                    stale = self._mark_stale_unlocked(
                        document,
                        key,
                        action,
                        generation_id=generation_id,
                        idempotency_key=token,
                        reason="reservation_owner_unknown",
                        reservation_token=existing_token,
                    )
                    if allow_replacement:
                        value = self._reserve_replacement_unlocked(
                            document,
                            key,
                            action,
                            generation_id=generation_id,
                            reservation_token=token,
                            reservation_owner_id=owner_id,
                            reservation_process_start=process_start,
                            reservation_pid=owner_pid,
                            reason="reservation_owner_unknown",
                        )
                        if self._claim_local_reservation(scope, token):
                            return self._response("replace", key=key, role=role, existing=value, reservation_token=token)
                    return stale
                active_owner_id = active_owner.get("owner_id")
                active_owner_pid = active_owner.get("pid")
                active_owner_start = active_owner.get("process_start")
                active_owner_identity = _process_identity_matches(active_owner_pid, active_owner_start)
                exact_active_dispatch = (
                    existing_token in active_tokens
                    and isinstance(active_owner_id, str)
                    and active_owner_id == existing.get("reservation_owner_id")
                    and isinstance(active_owner_pid, int)
                    and not isinstance(active_owner_pid, bool)
                    and active_owner_pid == existing.get("reservation_pid")
                    and isinstance(active_owner_start, str)
                    and active_owner_start == existing.get("reservation_process_start")
                    and active_owner_identity is True
                )
                if exact_active_dispatch or existing_token == local_token:
                    return self._response("blocked", key=key, role=role, existing=existing)
                owner_identity = _process_identity_matches(
                    existing.get("reservation_pid"),
                    existing.get("reservation_process_start"),
                )
                existing_pid = existing.get("reservation_pid")
                if owner_identity is True and existing_pid != os.getpid():
                    # A different live process still owns this reservation.
                    # Without an exact active-dispatch handoff, do not let a
                    # second process race it; the owner can later be
                    # reconciled once its start identity disappears.
                    return self._response("blocked", key=key, role=role, existing=existing)
                if (
                    owner_identity is None
                    and existing_pid != os.getpid()
                    and _pid_alive(existing_pid)
                    and not allow_replacement
                ):
                    # The platform could not provide a start token for a
                    # foreign PID.  Treat that uncertainty as in-flight
                    # evidence until an explicitly authorized replacement;
                    # never infer safety from the PID alone.
                    return self._response("blocked", key=key, role=role, existing=existing)
                if (
                    owner_identity is None
                    and existing_pid == os.getpid()
                    and _pid_alive(existing_pid)
                ):
                    # The current process is alive but its exact start token
                    # could not be proven.  Even same-process recovery is
                    # unsafe in this state: only an exact token match may
                    # resume a bound root.  Surface an explicit replacement
                    # boundary rather than treating the PID as sufficient.
                    stale = self._mark_stale_unlocked(
                        document,
                        key,
                        action,
                        generation_id=generation_id,
                        idempotency_key=token,
                        reason="reservation_owner_unknown",
                        reservation_token=existing_token,
                    )
                    if allow_replacement:
                        value = self._reserve_replacement_unlocked(
                            document,
                            key,
                            action,
                            generation_id=generation_id,
                            reservation_token=token,
                            reservation_owner_id=owner_id,
                            reservation_process_start=process_start,
                            reservation_pid=owner_pid,
                            reason="reservation_owner_unknown",
                        )
                        if self._claim_local_reservation(scope, token):
                            return self._response("replace", key=key, role=role, existing=value, reservation_token=token)
                    return stale
                if owner_identity is False:
                    # The PID currently names a different process instance
                    # (or an invalid/dead PID with a contradictory token), so
                    # this is a proven PID-reuse/owner-mismatch boundary. Do
                    # not resume even a previously bound root silently.
                    stale = self._mark_stale_unlocked(
                        document,
                        key,
                        action,
                        generation_id=generation_id,
                        idempotency_key=token,
                        reason="reservation_owner_mismatch",
                        reservation_token=existing_token,
                    )
                    if allow_replacement:
                        value = self._reserve_replacement_unlocked(
                            document,
                            key,
                            action,
                            generation_id=generation_id,
                            reservation_token=token,
                            reservation_owner_id=owner_id,
                            reservation_process_start=process_start,
                            reservation_pid=owner_pid,
                            reason="reservation_owner_mismatch",
                        )
                        if self._claim_local_reservation(scope, token):
                            return self._response("replace", key=key, role=role, existing=value, reservation_token=token)
                    return stale
                # A missing/mismatched start token is an orphan boundary.  A
                # matching token for this same process without exact
                # active-dispatch evidence is also treated as abandoned, per
                # same-process recovery semantics; no PID-only liveness claim
                # is made here.
                orphan_reason = (
                    "reservation_owner_mismatch"
                    if owner_identity is False
                    else "orphaned_reservation"
                )
                if allow_replacement:
                    self._mark_stale_unlocked(
                        document,
                        key,
                        action,
                        generation_id=generation_id,
                        idempotency_key=token,
                        reason=orphan_reason,
                        reservation_token=existing_token,
                    )
                    value = self._reserve_replacement_unlocked(
                        document,
                        key,
                        action,
                        generation_id=generation_id,
                        reservation_token=token,
                        reservation_owner_id=owner_id,
                        reservation_process_start=process_start,
                        reservation_pid=owner_pid,
                        reason=orphan_reason,
                    )
                    if self._claim_local_reservation(scope, token):
                        return self._response("replace", key=key, role=role, existing=value, reservation_token=token)
                    return self._response("blocked", key=key, role=role, existing=value)
                if existing.get("session_id"):
                    # The prior process died or abandoned its reservation
                    # after binding the exact root. A successor may reserve a
                    # continuation of that same root; this is not a
                    # replacement and preserves the exact session id.
                    value = self._reserve_continuation_unlocked(
                        document,
                        key,
                        action,
                        generation_id=generation_id,
                        reservation_token=token,
                        reservation_owner_id=owner_id,
                        reservation_process_start=process_start,
                        reservation_pid=owner_pid,
                        existing=existing,
                        orphaned=orphan_reason != "orphaned_reservation",
                    )
                    if self._claim_local_reservation(scope, token):
                        return self._response("resume", key=key, role=role, existing=value, reservation_token=token)
                    return self._response("blocked", key=key, role=role, existing=value)
                stale = self._mark_stale_unlocked(
                    document,
                    key,
                    action,
                    generation_id=generation_id,
                    idempotency_key=token,
                    reason=orphan_reason,
                    reservation_token=existing_token,
                )
                return stale
            if not existing.get("session_id"):
                stale = self._mark_stale_unlocked(
                    document,
                    key,
                    action,
                    generation_id=generation_id,
                    idempotency_key=token,
                    reason="invalid_registry_entry",
                )
                if allow_replacement:
                    value = self._reserve_replacement_unlocked(
                        document,
                        key,
                        action,
                        generation_id=generation_id,
                        reservation_token=token,
                        reservation_owner_id=owner_id,
                        reservation_process_start=process_start,
                        reservation_pid=owner_pid,
                        reason="invalid_registry_entry",
                    )
                    if self._claim_local_reservation(scope, token):
                        return self._response("replace", key=key, role=role, existing=value, reservation_token=token)
                return stale
            value = self._reserve_continuation_unlocked(
                document,
                key,
                action,
                generation_id=generation_id,
                reservation_token=token,
                reservation_owner_id=owner_id,
                reservation_process_start=process_start,
                reservation_pid=owner_pid,
                existing=existing,
            )
            if not self._claim_local_reservation(scope, token):
                return self._response("blocked", key=key, role=role, existing=value)
            return self._response("resume", key=key, role=role, existing=value, reservation_token=token)

    def _reserve_continuation_unlocked(
        self,
        document: dict[str, Any],
        key: str,
        action: PlannerAction,
        *,
        generation_id: str,
        reservation_token: str,
        reservation_owner_id: str,
        reservation_process_start: str | None,
        reservation_pid: int,
        existing: Mapping[str, Any],
        orphaned: bool = False,
    ) -> dict[str, Any]:
        timestamp = _now()
        if not isinstance(reservation_process_start, str) or not reservation_process_start.strip():
            raise CoordinatorIntegrityError("exact process start identity is unavailable")
        value = dict(existing)
        value.update(
            {
                "logical_owner": key,
                "role": action.role,
                "subject_id": action.subject_id,
                "run_id": self.context.run_id,
                "generation_id": generation_id,
                "status": "reserved",
                "replacement_required": False,
                "stale_reason": None,
                "updated_at": timestamp,
                "reservation_token": reservation_token,
                "reservation_status": "reserved",
                "reservation_action": action.action,
                "reservation_owner_id": reservation_owner_id,
                "reservation_pid": reservation_pid,
                "reservation_process_start": reservation_process_start,
            }
        )
        event = "role_session_reservation_recovered" if orphaned else "role_session_reserved"
        value["audit"] = self._audit_with_entry(
            value.get("audit") or (),
            self._audit_entry(event, action, reservation_token, timestamp, session_id=value.get("session_id")),
        )
        document["sessions"][key] = value
        self._write_unlocked(document)
        return value

    def _reserve_replacement_unlocked(
        self,
        document: dict[str, Any],
        key: str,
        action: PlannerAction,
        *,
        generation_id: str,
        reservation_token: str,
        reservation_owner_id: str,
        reservation_process_start: str | None,
        reservation_pid: int,
        reason: str,
    ) -> dict[str, Any]:
        existing = document["sessions"].get(key)
        timestamp = _now()
        prior_session = existing.get("session_id") if isinstance(existing, Mapping) else None
        value = self._reserved_entry(
            action,
            generation_id=generation_id,
            reservation_token=reservation_token,
            reservation_owner_id=reservation_owner_id,
            reservation_process_start=reservation_process_start,
            timestamp=timestamp,
            reservation_pid=reservation_pid,
            session_id=None,
            replacement_of=prior_session,
            lineage=(existing.get("action_lineage") if isinstance(existing, Mapping) else ()),
            audit=(existing.get("audit") if isinstance(existing, Mapping) else ()),
            event="role_session_replacement_reserved",
            reason=reason,
        )
        value["created_at"] = str(existing.get("created_at", timestamp)) if isinstance(existing, Mapping) else timestamp
        document["sessions"][key] = value
        self._write_unlocked(document)
        return value

    def replace_failed_resume(
        self,
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        reservation_token: str,
        expected_session_id: str,
        reason: str = "resume_session_unavailable",
    ) -> dict[str, Any]:
        """Atomically reserve one fresh root after a failed exact resume.

        This is intentionally narrower than :meth:`prepare` with
        ``allow_replacement``.  It can only replace the reservation owned by
        the current dispatch, while it is still reserved and still points at
        the exact session that failed to resume.  Keeping the same reservation
        token/owner and idempotency key lets the adapter perform the fresh root
        as one bounded continuation of the original logical action; the
        durable lock makes the stale-to-replacement transition a single CAS.
        """

        if reason != "resume_session_unavailable":
            raise ValueError("failed-resume replacement requires resume_session_unavailable")
        token = self._token(reservation_token, "reservation_token")
        dispatch_token = self._token(idempotency_key)
        if dispatch_token != token:
            raise CoordinatorConflictError("failed resume idempotency key does not match reservation")
        expected = self._safe_identifier(expected_session_id, "expected_session_id")
        assert expected is not None
        identity = _role_session_identity(action, run_id=self.context.run_id, generation_id=generation_id)
        if identity is None:
            raise ValueError("action is not resumable")
        key, role = identity
        with self._locked():
            document = self._read_unlocked()
            existing = document["sessions"].get(key)
            if not isinstance(existing, Mapping):
                raise CoordinatorConflictError("role session reservation is missing")
            if (
                existing.get("run_id") != self.context.run_id
                or existing.get("generation_id") != generation_id
                or existing.get("status") != "reserved"
                or existing.get("reservation_status") != "reserved"
                or existing.get("reservation_token") != token
                or existing.get("reservation_action") != action.action
                or existing.get("session_id") != expected
            ):
                raise CoordinatorConflictError("failed resume reservation compare-and-set failed")
            value = self._reserve_replacement_unlocked(
                document,
                key,
                action,
                generation_id=generation_id,
                # The dispatch remains the same logical action.  Reusing its
                # token preserves the coordinator's idempotency/CAS boundary;
                # ``replacement_of`` carries the old root lineage.
                reservation_token=token,
                reservation_owner_id=str(existing.get("reservation_owner_id")),
                reservation_process_start=existing.get("reservation_process_start"),
                reservation_pid=int(existing.get("reservation_pid")),
                reason=reason,
            )
            return self._response(
                "replace",
                key=key,
                role=role,
                existing=value,
                reservation_token=token,
            )

    def _mark_stale_unlocked(
        self,
        document: dict[str, Any],
        key: str,
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        reason: str,
        reservation_token: str | None = None,
    ) -> dict[str, Any]:
        existing = document["sessions"].get(key)
        timestamp = _now()
        token = self._token(idempotency_key)
        if isinstance(existing, Mapping) and existing.get("status") == "reserved":
            current_token = existing.get("reservation_token")
            if reservation_token is None or current_token != reservation_token:
                raise CoordinatorConflictError("role session reservation token changed")
        if existing is None:
            existing = {
                "logical_owner": key,
                "role": action.role,
                "subject_id": action.subject_id,
                "run_id": self.context.run_id,
                "generation_id": generation_id,
                "session_id": None,
                "status": "replacement_required",
                "replacement_required": True,
                "stale_reason": reason,
                "created_at": timestamp,
                "updated_at": timestamp,
                "last_action": action.action,
                "last_idempotency_key": token,
                "replacement_of": None,
                "reservation_token": None,
                "reservation_status": None,
                "reservation_action": None,
                "reservation_owner_id": None,
                "reservation_pid": None,
                "reservation_process_start": None,
                "action_lineage": [self._lineage_entry(action, token, timestamp)],
                "audit": [self._audit_entry("role_session_replacement_required", action, token, timestamp, reason=reason)],
            }
        else:
            existing = dict(existing)
            existing["status"] = "replacement_required"
            existing["replacement_required"] = True
            existing["stale_reason"] = reason
            existing["updated_at"] = timestamp
            existing["last_action"] = action.action
            existing["last_idempotency_key"] = token
            existing["reservation_token"] = None
            existing["reservation_status"] = None
            existing["reservation_action"] = None
            existing["reservation_owner_id"] = None
            existing["reservation_pid"] = None
            existing["reservation_process_start"] = None
            existing["action_lineage"] = self._lineage_with_action(existing.get("action_lineage") or (), action, token, timestamp)
            existing["audit"] = self._audit_with_entry(
                existing.get("audit") or (),
                self._audit_entry(
                    "role_session_replacement_required",
                    action,
                    token,
                    timestamp,
                    reason=reason,
                    session_id=existing.get("session_id"),
                ),
            )
        document["sessions"][key] = existing
        self._write_unlocked(document)
        return {
            "mode": "blocked",
            "logical_owner": key,
            "role": action.role,
            "session_id": existing.get("session_id"),
            "status": "replacement_required",
            "reservation_token": None,
            "reservation_status": None,
            "reservation_owner_id": None,
            "reservation_process_start": None,
        }

    def mark_stale(
        self,
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        reason: str,
        reservation_token: str | None = None,
    ) -> dict[str, Any]:
        if reason not in self._REASONS:
            raise ValueError("unsupported role session stale reason")
        identity = _role_session_identity(action, run_id=self.context.run_id, generation_id=generation_id)
        if identity is None:
            return {"mode": "fresh", "logical_owner": None, "status": None}
        key, _role = identity
        with self._locked():
            document = self._read_unlocked()
            return self._mark_stale_unlocked(
                document,
                key,
                action,
                generation_id=generation_id,
                idempotency_key=idempotency_key,
                reason=reason,
                reservation_token=reservation_token,
            )

    def _terminal_product_regeneration_matches(
        self,
        existing: Mapping[str, Any],
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        terminal_request: Mapping[str, Any] | None,
    ) -> bool:
        """Prove that an intentional Product request may refresh an owner.

        ``replacement_required`` is normally a hard stop: it can only be
        crossed by an explicit, typed replacement boundary.  Product
        regeneration is the one operation that may establish that boundary
        after a prior Product request reached a terminal outcome.  Keep this
        check pure so callers can run it before creating revision files.
        """

        if not isinstance(terminal_request, Mapping):
            return False
        # A replacement-required owner can only be refreshed after a failed
        # Product regeneration.  An accepted terminal request already has a
        # valid current Product pointer; treating it as replacement authority
        # would permit an unbounded supersession path and must fail closed.
        if terminal_request.get("status") != "failed":
            return False
        previous_token = terminal_request.get("request_id")
        if not isinstance(previous_token, str) or not previous_token.strip():
            return False
        token = self._token(idempotency_key)
        if previous_token == token:
            return False
        if terminal_request.get("authorization_origin") != PRODUCT_REGENERATION_ORIGIN:
            return False
        if terminal_request.get("run_id") != self.context.run_id:
            return False
        if terminal_request.get("generation_id") != generation_id:
            return False
        if (
            existing.get("logical_owner")
            != f"product_agent:{self.context.run_id}:{generation_id}"
            or existing.get("role") != "product_agent"
            or existing.get("subject_id") != action.subject_id
            or existing.get("run_id") != self.context.run_id
            or existing.get("generation_id") != generation_id
            or existing.get("status") != "replacement_required"
            or existing.get("replacement_required") is not True
            or not isinstance(existing.get("session_id"), str)
            or not existing.get("session_id")
        ):
            return False
        # A replacement-required row cannot carry any active reservation.  A
        # malformed row is rejected here rather than being refreshed into a
        # fresh Product dispatch.
        if any(
            existing.get(field_name) is not None
            for field_name in (
                "reservation_token",
                "reservation_status",
                "reservation_action",
                "reservation_owner_id",
                "reservation_pid",
                "reservation_process_start",
            )
        ):
            return False
        lineage = existing.get("action_lineage")
        if not isinstance(lineage, list):
            return False
        return True

    @staticmethod
    def _latest_product_regeneration_audit(
        existing: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Return the latest typed Product-regeneration audit entry."""

        audit = existing.get("audit")
        if not isinstance(audit, list):
            return None
        latest: Mapping[str, Any] | None = None
        for entry in audit:
            if isinstance(entry, Mapping) and entry.get("event") == "product_regeneration_requested":
                latest = entry
        return latest

    @classmethod
    def _product_regeneration_audit_matches(
        cls,
        existing: Mapping[str, Any],
        action: PlannerAction,
        token: str,
    ) -> bool:
        """Match one request against the latest Product-regeneration audit."""

        latest = cls._latest_product_regeneration_audit(existing)
        session_id = existing.get("session_id")
        return bool(
            isinstance(latest, Mapping)
            and isinstance(session_id, str)
            and session_id
            and latest.get("idempotency_key") == token
            and latest.get("action") == action.action
            and latest.get("subject_id") == action.subject_id
            and latest.get("session_id") == session_id
            and latest.get("reason") == PRODUCT_REGENERATION_ORIGIN
        )

    def _classify_product_regeneration_unlocked(
        self,
        document: Mapping[str, Any],
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        terminal_request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Purely classify Product owner admission for read and write paths.

        Both preflight and commit consume this one classifier.  Keeping the
        token/history and replacement checks here prevents a commit path from
        accidentally accepting a state that its preceding read-only preflight
        would reject.
        """

        if action.role.strip().lower() != "product_agent" or action.action.strip().lower() != "build_product_candidate":
            raise ValueError("product regeneration requires a Product Agent candidate action")
        identity = _role_session_identity(action, run_id=self.context.run_id, generation_id=generation_id)
        if identity is None:
            raise ValueError("product regeneration action is not resumable")
        key, role = identity
        token = self._token(idempotency_key)
        sessions = document.get("sessions") if isinstance(document, Mapping) else None
        existing = sessions.get(key) if isinstance(sessions, Mapping) else None
        if existing is None:
            return {"mode": "fresh", "logical_owner": key, "role": role, "existing": None}
        if not isinstance(existing, Mapping):
            raise CoordinatorIntegrityError("product session entry is invalid")
        if existing.get("run_id") != self.context.run_id or existing.get("generation_id") != generation_id:
            raise CoordinatorConflictError("product session lineage does not match regeneration request")

        # An active reservation is an unconditional conflict, including when
        # the caller repeats its idempotency token.  It is the sole owner of
        # the in-flight transport and must never be refreshed by this path.
        status = existing.get("status")
        if status == "reserved":
            raise CoordinatorConflictError("Product Agent session reservation is active")

        audit = existing.get("audit") if isinstance(existing.get("audit"), list) else []
        historical_tokens = {
            entry.get("idempotency_key")
            for entry in audit
            if isinstance(entry, Mapping)
            and entry.get("event") == "product_regeneration_requested"
            and isinstance(entry.get("idempotency_key"), str)
        }
        same_token = (
            status == "replacement_required"
            and existing.get("last_idempotency_key") == token
            and self._product_regeneration_audit_matches(existing, action, token)
        )
        if same_token:
            return {"mode": "already_requested", "logical_owner": key, "role": role, "existing": existing}
        # A token seen anywhere in the typed regeneration history is an old or
        # malformed replay unless it satisfied the exact same-token state
        # above.  Reject it before ProductReviewStore can allocate/reconcile a
        # revision namespace.
        if token in historical_tokens or existing.get("last_idempotency_key") == token:
            raise CoordinatorConflictError("historical Product regeneration request conflicts with owner state")

        if status == "replacement_required":
            if self._terminal_product_regeneration_matches(
                existing,
                action,
                generation_id=generation_id,
                idempotency_key=token,
                terminal_request=terminal_request,
            ):
                return {
                    "mode": "eligible_terminal_replacement",
                    "logical_owner": key,
                    "role": role,
                    "existing": existing,
                }
            raise CoordinatorConflictError("Product Agent session already requires an explicit replacement")
        if status != "active" or not existing.get("session_id"):
            raise CoordinatorConflictError("Product Agent session is not a completed active owner")
        return {"mode": "eligible", "logical_owner": key, "role": role, "existing": existing}

    def _preflight_product_regeneration_unlocked(
        self,
        document: Mapping[str, Any],
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        terminal_request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read-only Product owner admission before revision creation."""
        classification = self._classify_product_regeneration_unlocked(
            document,
            action,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
            terminal_request=terminal_request,
        )
        existing = classification.get("existing")
        return self._response(
            str(classification["mode"]),
            key=str(classification["logical_owner"]),
            role=str(classification["role"]),
            existing=existing if isinstance(existing, Mapping) else None,
        )

    def preflight_product_regeneration(
        self,
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        terminal_request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Public read-only Product owner admission boundary."""

        with self._locked():
            document = self._read_unlocked()
            return self._preflight_product_regeneration_unlocked(
                document,
                action,
                generation_id=generation_id,
                idempotency_key=idempotency_key,
                terminal_request=terminal_request,
            )

    def _request_product_regeneration_unlocked(
        self,
        document: dict[str, Any],
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        terminal_request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an intentional Product revision replacement.

        This is deliberately distinct from transport-stale recovery.  The
        completed Product owner is retained as immutable lineage while its
        logical slot waits for the revision-bound action.  ``stale_reason``
        remains untouched; the audit event and coordinator authorization carry
        the truthful regeneration origin.
        """

        classification = self._classify_product_regeneration_unlocked(
            document,
            action,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
            terminal_request=terminal_request,
        )
        mode = str(classification["mode"])
        key = str(classification["logical_owner"])
        role = str(classification["role"])
        token = self._token(idempotency_key)
        existing = classification.get("existing")
        if mode in {"fresh", "already_requested"}:
            return self._response(
                mode,
                key=key,
                role=role,
                existing=existing if isinstance(existing, Mapping) else None,
            )
        if not isinstance(existing, Mapping):
            raise CoordinatorIntegrityError("product session entry is unavailable after admission")
        timestamp = _now()
        value = dict(existing)
        value.update(
            {
                "status": "replacement_required",
                "replacement_required": True,
                # This is an intentional supersession boundary, not a stale
                # transport diagnosis. Keep the legacy field null.
                "stale_reason": None,
                "updated_at": timestamp,
                "last_action": action.action,
                "last_idempotency_key": token,
                "reservation_token": None,
                "reservation_status": None,
                "reservation_action": None,
                "reservation_owner_id": None,
                "reservation_pid": None,
                "reservation_process_start": None,
            }
        )
        value["action_lineage"] = self._lineage_with_action(value.get("action_lineage") or (), action, token, timestamp)
        value["audit"] = self._audit_with_entry(
            value.get("audit") or (),
            self._audit_entry(
                "product_regeneration_requested",
                action,
                token,
                timestamp,
                reason=PRODUCT_REGENERATION_ORIGIN,
                session_id=value.get("session_id"),
            ),
        )
        document["sessions"][key] = value
        self._write_unlocked(document)
        return self._response("requested", key=key, role=role, existing=value)

    def request_product_regeneration(
        self,
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Public registry boundary for intentional Product supersession."""

        token = self._token(idempotency_key)
        identity = _role_session_identity(action, run_id=self.context.run_id, generation_id=generation_id)
        if identity is None or identity[1] != "product_agent" or action.action.strip().lower() != "build_product_candidate":
            raise ValueError("product regeneration requires a Product Agent candidate action")
        with self._locked():
            document = self._read_unlocked()
            return self._request_product_regeneration_unlocked(
                document,
                action,
                generation_id=generation_id,
                idempotency_key=token,
            )

    def bind_reservation(
        self,
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        reservation_token: str,
        session_id: str,
        replacement: bool = False,
    ) -> dict[str, Any]:
        """CAS-bind the exact root session from a live ``thread.started``.

        This method is intentionally separate from completion.  The reader
        thread calls it as soon as the first valid root event arrives, while
        the Codex process is still running.  A stale token, generation, or
        different session id cannot overwrite an active reservation.
        """

        token = self._token(reservation_token, "reservation_token")
        if not isinstance(session_id, str) or _ROLE_SESSION_ID_RE.fullmatch(session_id.strip()) is None:
            raise ValueError("role session id is invalid")
        identity = _role_session_identity(action, run_id=self.context.run_id, generation_id=generation_id)
        if identity is None:
            raise ValueError("action is not resumable")
        key, _role = identity
        timestamp = _now()
        with self._locked():
            document = self._read_unlocked()
            existing = document["sessions"].get(key)
            if not isinstance(existing, Mapping):
                raise CoordinatorConflictError("role session reservation is missing")
            if (
                existing.get("run_id") != self.context.run_id
                or existing.get("generation_id") != generation_id
                or existing.get("status") != "reserved"
                or existing.get("reservation_status") != "reserved"
                or existing.get("reservation_token") != token
            ):
                raise CoordinatorConflictError("role session reservation compare-and-set failed")
            prior_session = existing.get("session_id")
            candidate = session_id.strip()
            if prior_session is not None and prior_session != candidate and not replacement:
                raise CoordinatorConflictError("role session id compare-and-set failed")
            value = dict(existing)
            value["session_id"] = candidate
            value["stale_reason"] = None
            value["updated_at"] = timestamp
            # A replacement reservation already carries ``replacement_of``
            # from the stale row while its bound session is intentionally
            # empty.  Do not overwrite that lineage with ``None`` when the
            # fresh root's first ``thread.started`` event is CAS-bound.
            value["replacement_of"] = (
                prior_session
                if replacement and prior_session is not None and prior_session != candidate
                else value.get("replacement_of")
            )
            event = "role_session_replaced" if replacement and prior_session != candidate else "role_session_bound"
            value["audit"] = self._audit_with_entry(
                value.get("audit") or (),
                self._audit_entry(event, action, token, timestamp, session_id=candidate),
            )
            document["sessions"][key] = value
            self._write_unlocked(document)
            return dict(value)

    def complete_reservation(
        self,
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        reservation_token: str,
        session_id: str | None = None,
        replacement: bool = False,
    ) -> dict[str, Any]:
        """Finalize a reserved dispatch without permitting an ID overwrite."""

        token = self._token(reservation_token, "reservation_token")
        identity = _role_session_identity(action, run_id=self.context.run_id, generation_id=generation_id)
        if identity is None:
            raise ValueError("action is not resumable")
        key, role = identity
        timestamp = _now()
        if session_id is not None and (
            not isinstance(session_id, str) or _ROLE_SESSION_ID_RE.fullmatch(session_id.strip()) is None
        ):
            raise ValueError("role session id is invalid")
        with self._locked():
            document = self._read_unlocked()
            existing = document["sessions"].get(key)
            if not isinstance(existing, Mapping):
                raise CoordinatorConflictError("role session reservation is missing")
            if (
                existing.get("run_id") != self.context.run_id
                or existing.get("generation_id") != generation_id
                or existing.get("status") != "reserved"
                or existing.get("reservation_status") != "reserved"
                or existing.get("reservation_token") != token
            ):
                raise CoordinatorConflictError("role session reservation compare-and-set failed")
            bound = existing.get("session_id")
            if session_id is not None:
                candidate = session_id.strip()
                if bound is not None and bound != candidate and not replacement:
                    raise CoordinatorConflictError("role session id compare-and-set failed")
                if bound is None or replacement:
                    bound = candidate
            if bound is None:
                raise CoordinatorConflictError("role session cannot complete without an exact session id")
            value = dict(existing)
            value.update(
                {
                    "role": role,
                    "subject_id": action.subject_id,
                    "session_id": bound,
                    "status": "active",
                    "replacement_required": False,
                    "stale_reason": None,
                    "updated_at": timestamp,
                    "last_action": action.action,
                    "last_idempotency_key": str(idempotency_key),
                    "reservation_token": None,
                    "reservation_status": None,
                    "reservation_action": None,
                    "reservation_owner_id": None,
                    "reservation_pid": None,
                    "reservation_process_start": None,
                }
            )
            value["action_lineage"] = self._lineage_with_action(
                value.get("action_lineage") or (), action, str(idempotency_key), timestamp
            )
            value["audit"] = self._audit_with_entry(
                value.get("audit") or (),
                self._audit_entry("role_session_completed", action, str(idempotency_key), timestamp, session_id=bound),
            )
            document["sessions"][key] = value
            self._write_unlocked(document)
            return dict(value)

    def record_session(
        self,
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        session_id: str,
        replacement: bool = False,
        reservation_token: str | None = None,
    ) -> dict[str, Any]:
        """Complete one previously prepared reservation using a CAS token."""

        token = reservation_token
        if token is None:
            identity = _role_session_identity(action, run_id=self.context.run_id, generation_id=generation_id)
            if identity is None:
                raise ValueError("action is not resumable")
            current = self.get(identity[0])
            token = current.get("reservation_token") if isinstance(current, Mapping) else None
        if token is None:
            raise CoordinatorConflictError("role session reservation token is required")
        return self.complete_reservation(
            action,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
            reservation_token=str(token),
            session_id=session_id,
            replacement=replacement,
        )

    bind = record_session

    def replace(
        self,
        action: PlannerAction,
        *,
        generation_id: str,
        idempotency_key: str,
        session_id: str,
        reservation_token: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly bind a replacement after a stale-session audit."""

        return self.record_session(
            action,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
            session_id=session_id,
            replacement=True,
            reservation_token=reservation_token,
        )


class PlannerActionProvider(Protocol):
    def next_actions(self, context: RunContext, state: Mapping[str, Any]) -> Sequence[PlannerAction]: ...


class RequirementPlannerProvider:
    """Read the active public requirement Planner without doing work."""

    def next_actions(self, context: RunContext, state: Mapping[str, Any]) -> Sequence[PlannerAction]:
        from .requirement_planning import RequirementSupervisorWorkspace

        # Pass the replay-validated coordinator state into Planner so product
        # routing uses the exact publication policy that the coordinator has
        # admitted, without a second model/provider call or a weaker label.
        return RequirementSupervisorWorkspace(context).next_actions(
            coordinator_state=state,
        )


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
            session_id=(str(value["session_id"]) if value.get("session_id") is not None else None),
            session_key=(str(value["session_key"]) if value.get("session_key") is not None else None),
            session_status=(str(value["session_status"]) if value.get("session_status") is not None else None),
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
        additive = (
            "revision" in action.metadata
            and "published_revision" in action.metadata
            and action.metadata.get("revision", 1) > action.metadata.get("published_revision", 0)
        )
        revision_note = (
            " This is an additive immutable revision: preserve the prior published rows, reuse existing stable IDs as no-ops, and submit only genuinely new facts or objects; do not reauthor unchanged rows."
            if additive
            else ""
        )
        return (
            "Entity Resolution Owner sequence: load the public current_scope for the assigned domain; "
            "physical inputs are already bound by the program to the active generation's immutable data "
            "revision, so invoke the program-owned workspace/context entry points and never choose a data "
            "revision ID, archive path, or filename yourself. "
            "whenever concrete new source hints or representation item IDs are found, call the public "
            "record_scope_discovery(domain_id, owner_ref, source_hints=..., representation_item_ids=...) "
            "operation while holding the active resolution-owner lease. Treat an already_present result "
            "as success, refresh current_scope immediately before submission, and call submit_result with "
            "expected_scope_hash=current_scope.scope_hash. If submit_result reports a stale scope, treat it "
            "as ordinary resolver continuation: refresh current_scope, incorporate the expanded scope, and "
            "retry; do not consume repair or claim independent-review failure. Use only these public APIs and "
            "do not expose internal paths or state details."
            + revision_note
        )
    if name in {"analyze_requirement", "resume_requirement_analysis", "repair_requirement"} or role == "analytical_owner":
        # A first business-repair dispatch must activate the durable
        # reviewer authorization before the owner performs *any* readiness,
        # semantic search, selection, or attempt mutation.  Those public
        # searches append item-local semantic selections and therefore cannot
        # precede ``use_business_repair`` without invalidating the exact
        # reviewer baseline.  A resumed repair already carries
        # ``repair_active=true`` and must reuse that authorization instead of
        # opening a second one.
        if action.metadata.get("repair_active") is True:
            return (
                "Analytical Owner active-repair continuation: Planner metadata repair_active=true means a durable "
                "business-repair authorization is already active, including for resume_requirement_analysis. "
                "Reuse that exact authorization without reactivating it. Do not call BusinessReviewAdapter.begin_repair "
                "or ItemWorkspace.use_business_repair. Bind/load ItemWorkspace and BoundAnalysisContext; physical "
                "inputs are automatically bound to the active generation's immutable data revision. Invoke those "
                "program-owned entry points and do not choose a data revision ID, archive path, or filename. Construct "
                "AnalystWorkspace. Use the analytics toolkit first for supported profile_data, compute_kpi_table, "
                "segment_customers (k-means with optional agglomerative comparison), and score_segments; use custom "
                "owner-authored code only for unsupported methods through ControlledScriptRunner. Request specialists "
                "only for genuinely independent uncertainty, using the smallest useful set bounded by actual host "
                "capacity; do not create one specialist per method or checklist item. Persist any bounded "
                "SpecialistTask through AnalystWorkspace.assign_specialist; the Coordinator will offer exactly "
                "one fresh specialist action for each unresolved task. Do not spawn or delegate specialists. "
                "Then perform the "
                "readiness check and public search_ontology, "
                "search_identity_mappings, search_identity_decisions, and search_prepared_assets/select APIs. Call "
                "AnalystWorkspace.plan_requirement before begin_analysis or material analysis, continue the same-owner "
                "repair attempt, bind semantic-scope/source/evidence/specialist APIs, run_analysis and prepare_data as "
                "needed, submit_answer or conclude_data_insufficiency, and finish_attempt only after the repaired "
                "draft/conclusion is persisted."
            )
        if name == "repair_requirement" and action.metadata.get("repair_active") is not True:
            return (
                "Analytical Owner business-repair sequence: bind/load ItemWorkspace and "
                "BoundAnalysisContext (the program automatically binds the active generation's immutable data "
                "revision; do not choose a data revision ID, archive path, or filename), construct AnalystWorkspace, "
                "and first call the public "
                "BusinessReviewAdapter.begin_repair (which invokes ItemWorkspace.use_business_repair) "
                "for the same owner authorization before any readiness check, semantic search, "
                "source/ontology/identity/prepared selection, begin_attempt, or other mutation. "
                "After begin_repair succeeds, use the analytics toolkit first for supported profile_data, "
                "compute_kpi_table, segment_customers (k-means with optional agglomerative comparison), and "
                "score_segments; use custom owner-authored code only for unsupported methods through "
                "ControlledScriptRunner. Request specialists only for genuinely independent uncertainty, using the "
                "smallest useful set bounded by actual host capacity; do not create one specialist per method or "
                "checklist item. Persist any bounded SpecialistTask through AnalystWorkspace.assign_specialist; "
                "the Coordinator will offer exactly one fresh specialist action for each unresolved task. Do not "
                "spawn or delegate specialists. Then perform the readiness check and public "
                "search_ontology, search_identity_mappings, search_identity_decisions, and "
                "search_prepared_assets/select APIs, then begin or continue the same-owner repair "
                "attempt, call AnalystWorkspace.plan_requirement before begin_analysis or material "
                "analysis, bind the semantic-scope/source/evidence/specialist APIs, run_analysis and "
                "prepare_data as needed, submit_answer or conclude_data_insufficiency, and finish_attempt "
                "only after the repaired draft/conclusion is persisted. If the repair authorization is "
                "already active, reuse it and never call use_business_repair again."
            )
        return (
            "Analytical Owner sequence: bind/load ItemWorkspace and BoundAnalysisContext; the program automatically "
            "binds physical inputs to the active generation's immutable data revision, so do not choose a data "
            "revision ID, archive path, or filename. "
            "Use the analytics toolkit first for supported profile_data, compute_kpi_table, segment_customers "
            "(k-means with optional agglomerative comparison), and score_segments work; choose and preserve the "
            "exact method and parameters. Use custom owner-authored code only for methods the toolkit does not "
            "support, through ControlledScriptRunner. For genuinely independent uncertainty, persist the smallest "
            "useful set of bounded SpecialistTask records through AnalystWorkspace.assign_specialist, bounded by actual host "
            "capacity; the Coordinator will offer "
            "exactly one fresh specialist action for each unresolved task. Do not create one specialist per method, "
            "spawn, or delegate specialists, "
            "and never create a task solely because suggested_specialists appears in shared advisory context. "
            "construct AnalystWorkspace, perform a readiness check and call public "
            "search_ontology, search_identity_mappings, search_identity_decisions, and "
            "search_prepared_assets/select APIs before analysis, then call begin_attempt when an owner attempt is "
            "needed and call AnalystWorkspace.plan_requirement before begin_analysis or any material analysis; "
            "bind the semantic-scope/source/evidence/specialist APIs, run_analysis and "
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
    if name == "specialist" or role == "specialist":
        return (
            "Coordinator-owned Specialist sequence: load the exact task payload and item_id from Planner metadata; "
            "bind the supplied RunContext and the existing Analytical Owner owner_ref, then use the public "
            "AnalystWorkspace APIs to inspect the assigned SpecialistTask and its source-bound evidence. Record "
            "exactly one typed SpecialistMemo for that task through AnalystWorkspace.record_specialist_memo, with "
            "evidence_refs and limitations. Do not create new tasks, spawn/delegate subagents, edit item state, or "
            "write specialist JSONL directly. A memo for any other task is invalid; after the public write returns, "
            "exit so the Planner can re-offer the same item-local AO continuation."
        )
    if name == "review_requirement" or role == "business_reviewer":
        return (
            "Business reviewer sequence: load the item and its public AnalystWorkspace/"
            "BusinessReviewAdapter, inspect the persisted draft and evidence, and call "
            "record (or confirm_data_insufficiency) with an independent reviewer identity."
        )
    if name in {"integrate_requirement", "repair_integration_fidelity", "commit_integration_requirement"} or role == "integration_agent":
        return (
            "Integration sequence: use IntegrationSession.create/load; this path auto-stages all sealed "
            "business-accepted typed AnalyticalArtifact refs from sealed item-local state for the mandatory "
            "handoff. Do not manually re-submit or re-declare accepted analytical artifacts, and do not invent "
            "a new integration method. The accepted Analytical Owner answer bytes and accepted_content_hash are "
            "immutable; integration records are only a derived pre-commit projection. A literal difference "
            "between normalized typed fields and the accepted prose/artifact is expected projection work, not a "
            "semantic conflict or refusal. After fidelity authorization, use the same Integration Agent's public "
            "correct_record for every authorized affected record (or remove_record when removal is authorized), "
            "preserving the accepted hash and business meaning; rebuild the fidelity packet and submit the "
            "targeted recheck before commit. Never edit accepted answer bytes or redo analysis. Stage typed "
            "ontology and dashboard records through public APIs; ontology writes are canonical ensure operations, "
            "so existing IDs are reused automatically and agents should add only genuinely new objects or "
            "relationships. Call validate before build_fidelity_packet, "
            "and use public correct_record to repair mechanically invalid pre-fidelity records before "
            "revalidating. Mechanical/internal failures remain open/pending: record an incident and "
            "retry/repair; never terminalize accepted integration as a technical failure. After an accepted "
            "fidelity review call session.commit. Do not write integration artifacts directly."
        )
    if name == "review_integration_fidelity" or role == "integration_fidelity_reviewer":
        return "Use IntegrationSession.load and record_fidelity_review only; an independent reviewer must not commit."
    if role == "product_agent" and name != "publish_final_product":
        return (
            "Product sequence: read PRODUCT_AGENT_ASSEMBLER_CONTRACT.md once. Construct "
            "product_workspace.ProductWorkspace(context, action) from the supplied action metadata. "
            "Call workspace.feedback() first; correct the exact predecessor review when present. "
            "Page through workspace.inventory(offset=...) until next_offset is null; use "
            "workspace.detail(widget_id) only for selected candidates requiring exact source context. "
            "Inspect every accepted answer visual and committed candidate visual fact, including "
            "accepted evidence tables/fact sheets. Integration success is not a presentation prerequisite "
            "for accepted previews; a semantic defect must go back to the Analytical Owner, never be "
            "hidden by presentation. The Product Agent itself chooses only decision-useful business information. "
            "Choose one semantic representative per requirement/business metric/scope; prefer substantive "
            "accepted-evidence surfaces over unavailable or unbound placeholders. Use a variety of eligible "
            "charts only where the exact evidence supports them (line, area, bar, pie, donut, scatter, "
            "funnel, histogram, table or callout). Every accepted requirement needs a decision surface or "
            "explicit limitation, including an explicit reviewed empty state when warranted. Preserve the "
            "selected business meaning and exact rows/columns/values, units, period, population, denominator "
            "and proxy status. Do not invent metrics or run new analytics. Keep execution traces, "
            "files/inventory, pipeline/source-process diagnostics and technical join counts on the audit "
            "surface, not as business KPIs. Select complete ordered choices with widget_id and explicit "
            "recipe_id, layout, and renderer_type from the eligible inventory. Populate the executive "
            "overview from the explicit manager selection; optional presentation.overview_widget_ids "
            "selects a compact subset. Call workspace.build(choices, presentation={...}) once with concise "
            "title, subtitle, section_titles and widget_titles as appropriate. It owns preflight, plan CAS, "
            "generation routes, immutable revision paths, all hashes, rendering, output validation, preview "
            "manifest and candidate registration. Never calculate these by hand. The same call is safe for "
            "idempotent re-entry after a process interruption. No assembler or renderer code may reinterpret "
            "the plan. Do not edit fixture, chart, or manifest bytes directly. Do not call index_repository "
            "or search code, inspect git status or env, inventory/list the run root, or inspect Control Center "
            "or launch manifests. Do not search control_plane/coordinator_events.jsonl, role_sessions.json, "
            "~/.codex/sessions/history or perform recursive run-root searches. Use public metadata and the "
            "contract/API definitions only. Return the workspace result; never accept your own product, "
            "alter lifecycle, authorize publication, or repair repository source."
        )
    if name == "publish_final_product":
        return (
            "Publication is a mechanical policy-bound transition, not a new build. Use only the existing "
            "independently reviewed ProductReviewStore candidate and explicitly supplied publication policy. "
            "Never rebuild, self-review, change the policy or publish when authorization is absent."
        )
    if name == "review_final_product" or role == "product_reviewer":
        return (
            "Load ProductReviewStore and call record_review as an independent reviewer (pass revision_id from the "
            "action metadata when present); on an accepted regeneration review call activate_revision for that exact "
            "revision, while a repair/block outcome remains auditable and never replaces the prior accepted pointer. "
            "Do not authorize publication. "
            "Check that every accepted requirement has a meaningful decision surface or an explicit business limitation, "
            "including accepted visuals whose integration projection is terminally failed; integration success is not a "
            "presentation prerequisite. Inspect the assembled candidate's receipt, product manifest, chart map, index, "
            "and domain pages—not only inventory metadata—and act as the semantic quality control: verify each "
            "manager visual is decision-useful for its requirement, accepted business outputs and ontology context "
            "are represented faithfully, exact selected rows/columns/values are preserved, and execution traces, "
            "files/inventory, pipeline/source-process diagnostics, implementation details, and evidence IDs remain "
            "on the separate audit surface unless the Product Agent explicitly selected a business projection. Verify "
            "the explicit plan membership is honored without semantic re-evaluation, and verify a meaningful decision "
            "surface or limitation for every accepted requirement, including accepted visuals whose integration "
            "projection failed. Do not enforce fixed counts, ratios, or requirement-specific chart choices; chart "
            "count/type/layout remain Product Agent design decisions. Confirm manager HTML preserves the selected "
            "business content and contains no raw failure reasons or internal absolute paths."
        )
    if name == "finalize_final_report":
        return (
            "Reporting finalization sequence: call RunReportFinalizer.recover() first to converge any "
            "intent-bound transaction, then call RunReportInputGatherer.gather_from_run(context, persist=True) "
            "for authoritative preflight/recovery, load the persisted preflight, and call "
            "RunReportFinalizer.finalize. An unbound transaction_pending residue is an explicit operator-repair "
            "boundary; do not retry a model action. Do not author timings, incidents, reviews, or implementation "
            "hashes; these values must come from persisted public report inputs, never agent-authored values."
        )
    if role == "reporting_agent" or name in {"recover_final_report", "preflight_final_report"}:
        return (
            "Reporting preflight/recovery sequence: call RunReportFinalizer.recover() before gathering. This "
            "deterministically converges intent-bound staging/backup paths; an unbound transaction_pending "
            "residue is explicit operator repair and must not be retried as a model action. For preflight, call "
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
    if custom_guidance:
        # Persisted role prompts provide context, but cannot replace the
        # action-specific safety contract. Keep the mandatory sequence last
        # for every role/action so generic reviewer, integration, product,
        # and future prompts cannot bypass safety guidance.
        guidance = (
            custom_guidance
            if mandatory_guidance in custom_guidance
            else f"{custom_guidance}\n\n{mandatory_guidance}"
        )
    else:
        guidance = mandatory_guidance
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
        "Any shared_analysis_context or MissionContext refs/hashes in the action metadata are immutable, "
        "advisory evidence-reuse hints only; keep item-local analytical conclusions owned by the Analytical "
        "Owner and never infer a conclusion from shared context alone.\n"
        f"{binding_text}"
        f"Role-specific contract: {guidance}\n"
    )


@dataclass(frozen=True)
class CodexExecConfig:
    """Configuration for one plain-text ``codex exec`` role process."""

    binary: str = "codex"
    model: str | None = None
    profile: str | None = None
    reasoning_effort: str | None = None
    sandbox: str = "workspace-write"
    timeout_seconds: float | None = None
    ephemeral: bool = True
    role_prompts: Mapping[str, str] = field(default_factory=dict)
    role_models: Mapping[str, str] = field(default_factory=dict)
    role_profiles: Mapping[str, str] = field(default_factory=dict)
    role_reasoning_efforts: Mapping[str, str] = field(default_factory=dict)
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
    _validate_binding: bool = field(default=True, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "binary", _text(self.binary, "codex binary"))
        if self.model is not None:
            object.__setattr__(self, "model", _text(self.model, "model"))
        if self.profile is not None:
            object.__setattr__(self, "profile", _text(self.profile, "profile"))
        if self.reasoning_effort is not None:
            object.__setattr__(self, "reasoning_effort", _text(self.reasoning_effort, "reasoning_effort").lower())
        sandbox = _text(self.sandbox, "sandbox").lower()
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("sandbox must be read-only, workspace-write, or danger-full-access")
        object.__setattr__(self, "sandbox", sandbox)
        if self.timeout_seconds is not None and (isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or self.timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(self.ephemeral, bool):
            raise TypeError("ephemeral must be a boolean")
        for name in (
            "role_prompts",
            "role_models",
            "role_profiles",
            "role_reasoning_efforts",
            "role_sandboxes",
            "role_timeouts",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping")
        object.__setattr__(self, "role_prompts", {str(k): _text(v, "role prompt") for k, v in self.role_prompts.items()})
        object.__setattr__(self, "role_models", {str(k): _text(v, "role model") for k, v in self.role_models.items()})
        object.__setattr__(self, "role_profiles", {str(k): _text(v, "role profile") for k, v in self.role_profiles.items()})
        object.__setattr__(
            self,
            "role_reasoning_efforts",
            {str(k): _text(v, "role reasoning effort").lower() for k, v in self.role_reasoning_efforts.items()},
        )
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
        if self._validate_binding and self.has_skill_binding:
            self.validate_skill_binding(required=True, verify_active=False)

    def _role_setting(self, mapping: Mapping[str, Any], role: str, default: Any = None) -> Any:
        return mapping.get(role, default)

    def for_role(self, role: str) -> "CodexExecConfig":
        normalized_role = _text(role, "role").lower()
        contract = ROLE_MODEL_CONTRACT.get(normalized_role)
        if contract is None:
            raise CoordinatorIntegrityError(
                f"role {normalized_role!r} is a deterministic control and has no model route"
            )
        # An explicit per-role or scalar setting remains authoritative for
        # callers that intentionally configure a test/alternate transport.
        # Production configs with no setting now receive the reviewed
        # Sol/Luna route instead of falling through to an ambient model.
        model_default = self.model if self.model is not None else contract.get("model")
        reasoning_default = (
            self.reasoning_effort
            if self.reasoning_effort is not None
            else contract.get("reasoning_effort")
        )
        return CodexExecConfig(
            binary=self.binary,
            model=self._role_setting(self.role_models, normalized_role, model_default),
            profile=self._role_setting(self.role_profiles, normalized_role, self.profile),
            reasoning_effort=self._role_setting(self.role_reasoning_efforts, normalized_role, reasoning_default),
            sandbox=self._role_setting(self.role_sandboxes, normalized_role, self.sandbox),
            timeout_seconds=self._role_setting(self.role_timeouts, normalized_role, self.timeout_seconds),
            ephemeral=self.ephemeral,
            role_prompts=self.role_prompts,
            role_models=self.role_models,
            role_profiles=self.role_profiles,
            role_reasoning_efforts=self.role_reasoning_efforts,
            role_sandboxes=self.role_sandboxes,
            role_timeouts=self.role_timeouts,
            skill_path=self.skill_path,
            skill_version=self.skill_version,
            core_version=self.core_version,
            skill_sha256=self.skill_sha256,
            _validate_binding=False,
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
        if verify_active:
            installed = resolve_production_skill_binding(
                repo_root=repo_root or Path(__file__).resolve().parents[2],
                role_cwd=role_cwd,
            )
            if any(
                getattr(self, field_name) != installed[field_name]
                for field_name in ("skill_path", "skill_version", "core_version", "skill_sha256")
            ):
                raise CoordinatorProductionBindingMismatch(
                    "Codex skill binding does not match the single active installed skill"
                )
        actual = _sha256_bytes(_skill_release_bytes(resolved))
        if actual != self.skill_sha256:
            raise CoordinatorIntegrityError("Codex skill bytes do not match the persisted release hash")

    def to_dict(self) -> dict[str, Any]:
        return {
            "binary": self.binary,
            "model": self.model,
            "profile": self.profile,
            "reasoning_effort": self.reasoning_effort,
            "sandbox": self.sandbox,
            "timeout_seconds": self.timeout_seconds,
            "ephemeral": self.ephemeral,
            "role_prompts": dict(self.role_prompts),
            "role_models": dict(self.role_models),
            "role_profiles": dict(self.role_profiles),
            "role_reasoning_efforts": dict(self.role_reasoning_efforts),
            "role_sandboxes": dict(self.role_sandboxes),
            "role_timeouts": dict(self.role_timeouts),
            "skill_path": self.skill_path,
            "skill_version": self.skill_version,
            "core_version": self.core_version,
            "skill_sha256": self.skill_sha256,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any] | None,
        *,
        validate_binding: bool = True,
    ) -> "CodexExecConfig":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise TypeError("codex_exec must be a mapping")
        allowed = {
            "binary", "model", "profile", "reasoning_effort", "sandbox", "timeout_seconds", "ephemeral",
            "role_prompts", "role_models", "role_profiles", "role_reasoning_efforts", "role_sandboxes", "role_timeouts",
            "skill_path", "skill_version", "core_version", "skill_sha256",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError("codex_exec contains unknown/deprecated fields: " + ", ".join(unknown))
        return cls(
            **{key: value[key] for key in allowed if key in value},
            _validate_binding=validate_binding,
        )


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
        self.config = (
            config
            if isinstance(config, CodexExecConfig)
            else CodexExecConfig.from_dict(config, validate_binding=False)
        )
        self.require_skill_binding = bool(require_skill_binding)
        # A stable nonce identifies this adapter/coordinator instance when a
        # direct transport call has no persisted active-dispatch owner yet.
        # It is deliberately not persisted outside the reservation row.
        self._owner_nonce = f"adapter-{uuid.uuid4().hex}"
        if self.require_skill_binding:
            self.config.validate_skill_binding(
                required=True,
                verify_active=True,
                repo_root=Path(__file__).resolve().parents[2],
                role_cwd=context.run_root,
            )

    @staticmethod
    def _replacement_requested(action: PlannerAction) -> bool:
        metadata = action.metadata if isinstance(action.metadata, Mapping) else {}
        return (
            metadata.get("allow_session_replacement") is True
            or metadata.get("session_replacement") in {"allow", "authorized"}
        )

    @staticmethod
    def _generation_id(context: RunContext, action: PlannerAction) -> str:
        """Read the coordinator generation without trusting action metadata."""

        state_path = context.resolve_run_path(f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_STATE_FILENAME}")
        if state_path.is_file() and not state_path.is_symlink():
            try:
                state = _load_json(state_path)
            except CoordinatorIntegrityError:
                state = None
            if isinstance(state, Mapping) and isinstance(state.get("generation_id"), str) and state.get("generation_id"):
                return str(state["generation_id"])
        metadata_generation = action.metadata.get("generation_id") if isinstance(action.metadata, Mapping) else None
        if isinstance(metadata_generation, str) and metadata_generation.strip():
            return metadata_generation.strip()
        # Direct adapter tests may intentionally run without a coordinator
        # checkpoint.  Keep their transport usable while persisted runs always
        # resolve the generation from the authoritative state above.
        return "unbound"

    @staticmethod
    def _registry_enabled(context: RunContext) -> bool:
        control_plane = context.resolve_run_path(CONTROL_PLANE_DIRNAME)
        return control_plane.is_dir() and not control_plane.is_symlink()

    @staticmethod
    def _active_dispatch_tokens(context: RunContext) -> frozenset[str]:
        """Read coordinator active-dispatch evidence without mutating it."""

        path = context.resolve_run_path(f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_STATE_FILENAME}")
        if not path.is_file() or path.is_symlink():
            return frozenset()
        try:
            value = _load_json(path)
        except CoordinatorIntegrityError:
            # The coordinator's own replay reader remains the integrity
            # authority.  A malformed snapshot cannot authorize a second
            # transport, so fail closed by returning no positive evidence.
            return frozenset()
        active = value.get("active_dispatches") if isinstance(value, Mapping) else None
        if not isinstance(active, list):
            return frozenset()
        tokens: set[str] = set()
        for entry in active:
            if not isinstance(entry, Mapping):
                continue
            token = entry.get("idempotency_key")
            if isinstance(token, str) and token.strip():
                tokens.add(token.strip())
        return frozenset(tokens)

    @staticmethod
    def _active_dispatch_owner(context: RunContext, idempotency_key: str) -> dict[str, Any] | None:
        """Return the exact owner identity for one active dispatch.

        A restarted coordinator claims a persisted dispatch by writing its
        owner nonce, PID, and process-start token before invoking the adapter.
        The adapter uses all three values for reservation reconciliation; a
        matching PID without the start token is never treated as proof of
        active ownership.
        """

        path = context.resolve_run_path(f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_STATE_FILENAME}")
        if not path.is_file() or path.is_symlink():
            return None
        try:
            value = _load_json(path)
        except CoordinatorIntegrityError:
            return None
        active = value.get("active_dispatches") if isinstance(value, Mapping) else None
        if not isinstance(active, list):
            return None
        token = str(idempotency_key).strip()
        for entry in active:
            if not isinstance(entry, Mapping) or entry.get("idempotency_key") != token:
                continue
            owner_id = entry.get("runner_id")
            runner_pid = entry.get("runner_pid")
            process_start = entry.get("runner_process_start")
            if not isinstance(owner_id, str) or not owner_id.strip():
                return None
            if isinstance(runner_pid, bool) or not isinstance(runner_pid, int) or runner_pid <= 0:
                return None
            if not isinstance(process_start, str) or not process_start.strip():
                return None
            return {
                "owner_id": owner_id.strip(),
                "pid": runner_pid,
                "process_start": process_start.strip(),
            }
        return None

    @classmethod
    def _active_dispatch_owner_pid(cls, context: RunContext, idempotency_key: str) -> int | None:
        """Backward-compatible PID projection for internal diagnostics."""

        owner = cls._active_dispatch_owner(context, idempotency_key)
        return int(owner["pid"]) if owner is not None else None

    def __call__(
        self,
        action: PlannerAction,
        *,
        idempotency_key: str,
        context: RunContext,
        _prepared_session_info: Mapping[str, Any] | None = None,
    ) -> RoleExecution:
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
        generation_id = self._generation_id(context, action)
        resumable_identity = _role_session_identity(
            action,
            run_id=context.run_id,
            generation_id=generation_id,
        )
        registry: RoleSessionRegistry | None = None
        session_info: dict[str, Any] = {"mode": "fresh", "logical_owner": None, "role": action.role}
        if resumable_identity is not None and self._registry_enabled(context):
            registry = RoleSessionRegistry(context)
            if _prepared_session_info is not None:
                if not isinstance(_prepared_session_info, Mapping):
                    return RoleExecution(
                        exit_code=1,
                        error="prepared role session is invalid; explicit replacement is required",
                        session_key=resumable_identity[0],
                        session_status="replacement_required",
                    )
                session_info = dict(_prepared_session_info)
                if (
                    session_info.get("mode") != "replace"
                    or session_info.get("logical_owner") != resumable_identity[0]
                    or session_info.get("reservation_token") != str(idempotency_key).strip()
                    or session_info.get("session_id") is not None
                ):
                    return RoleExecution(
                        exit_code=1,
                        error="prepared role session does not match the failed resume; explicit replacement is required",
                        session_key=resumable_identity[0],
                        session_status="replacement_required",
                    )
            else:
                try:
                    active_dispatch_tokens = self._active_dispatch_tokens(context)
                    active_dispatch_owner = self._active_dispatch_owner(context, idempotency_key)
                    if active_dispatch_owner is None:
                        active_dispatch_owner = {
                            "owner_id": self._owner_nonce,
                            "pid": os.getpid(),
                            "process_start": _required_process_start_token(os.getpid()),
                        }
                    session_info = registry.prepare(
                        action,
                        generation_id=generation_id,
                        idempotency_key=idempotency_key,
                        allow_replacement=self._replacement_requested(action),
                        active_dispatch_tokens=active_dispatch_tokens,
                        active_dispatch_owner=active_dispatch_owner,
                    )
                except (CoordinatorError, OSError, ValueError, TypeError) as exc:
                    # A corrupt registry cannot safely authorize a transport or
                    # overwrite an existing root.  Surface a retryable technical
                    # failure and require an explicit registry repair/replacement
                    # instead of falling back to a new session.
                    return RoleExecution(
                        exit_code=1,
                        error="role session registry is unavailable; explicit replacement is required",
                        session_key=resumable_identity[0],
                        session_status="replacement_required",
                    )
            if session_info.get("mode") == "blocked":
                # Never silently fall back to a new ephemeral session when an
                # exact logical-owner session is reserved, stale, or
                # unavailable.  A blocked in-flight reservation may be
                # retried only after its owning dispatch has reconciled.
                return RoleExecution(
                    exit_code=1,
                    error="role session is unavailable; explicit replacement is required",
                    session_id=session_info.get("session_id"),
                    session_key=session_info.get("logical_owner"),
                    session_status=(
                        "replacement_required"
                        if session_info.get("status") == "replacement_required"
                        else "reservation_in_flight"
                    ),
                )
        reservation_token = session_info.get("reservation_token")
        reservation_key = session_info.get("logical_owner")

        def release_reservation() -> None:
            if (
                registry is not None
                and isinstance(reservation_key, str)
                and isinstance(reservation_token, str)
            ):
                registry.release_reservation(reservation_key, reservation_token)
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
            resume_session_id = session_info.get("session_id") if session_info.get("mode") == "resume" else None
            if session_info.get("mode") == "resume" and isinstance(resume_session_id, str):
                # ``codex exec resume --help`` exposes a narrower option set
                # than the initial ``exec`` command.  Keep options in its
                # documented order, put the exact id before the stdin prompt,
                # and rely on the original session's model/profile/sandbox.
                argv = [config.binary, "exec", "resume", "--skip-git-repo-check"]
                if config.model:
                    argv.extend(["--model", config.model])
                if config.reasoning_effort:
                    argv.extend(["-c", f"model_reasoning_effort={config.reasoning_effort}"])
                argv.extend(["--json", "--output-last-message", str(output_path), resume_session_id, "-"])
            else:
                # Initial resumable invocations must persist a Codex session;
                # fresh reviewers/specialists are ephemeral even when a
                # caller's broad config sets ephemeral=false.
                argv = [config.binary, "exec", "--skip-git-repo-check"]
                if _is_fresh_role(action) or (config.ephemeral and resumable_identity is None):
                    argv.append("--ephemeral")
                argv.extend(["--sandbox", config.sandbox])
                if config.model:
                    argv.extend(["--model", config.model])
                if config.profile:
                    argv.extend(["--profile", config.profile])
                if config.reasoning_effort:
                    argv.extend(["-c", f"model_reasoning_effort={config.reasoning_effort}"])
                argv.extend(["--json", "--output-last-message", str(output_path), "-"])
            # JSONL is transport telemetry only.  It is normalized into the
            # Control Center lifecycle allowlist and never treated as phase
            # success authority.  Deliberately no --output-schema.
            process: subprocess.Popen[bytes] | None = None
            stderr_bytes = bytearray()
            lifecycle_errors: list[str] = []
            writer = LifecycleEventWriter(context.run_root)
            root_thread: str | None = None
            bound_thread: str | None = None
            binding_error: str | None = None
            expected_resume_session = (
                session_info.get("session_id")
                if session_info.get("mode") == "resume"
                else None
            )

            def read_stdout() -> None:
                nonlocal root_thread, bound_thread, binding_error
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
                        prior_root = root_thread
                        root_thread, rows = normalize_codex_json_line(
                            line,
                            root_thread=root_thread,
                            root_invocation_id=idempotency_key,
                        )
                        if (
                            root_thread is not None
                            and root_thread != prior_root
                            and registry is not None
                            and isinstance(reservation_token, str)
                        ):
                            try:
                                bound = registry.bind_reservation(
                                    action,
                                    generation_id=generation_id,
                                    idempotency_key=idempotency_key,
                                    reservation_token=reservation_token,
                                    session_id=root_thread,
                                    replacement=(
                                        session_info.get("mode") == "replace"
                                        and bound_thread is None
                                    ),
                                )
                                bound_thread = str(bound.get("session_id") or root_thread)
                            except Exception as exc:
                                # A different root or stale reservation token
                                # is a CAS failure, not permission to continue
                                # on a fresh session.  Keep the process alive
                                # long enough to drain telemetry, then return
                                # a retryable replacement-required result.
                                binding_error = "role session binding compare-and-set failed"
                                lifecycle_errors.append(binding_error)
                                try:
                                    registry.mark_stale(
                                        action,
                                        generation_id=generation_id,
                                        idempotency_key=idempotency_key,
                                        reason=(
                                            "resume_session_mismatch"
                                            if expected_resume_session is not None
                                            else "reservation_conflict"
                                        ),
                                        reservation_token=reservation_token,
                                    )
                                except Exception:
                                    pass
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
                if registry is not None and resumable_identity is not None:
                    try:
                        registry.mark_stale(
                            action,
                            generation_id=generation_id,
                            idempotency_key=idempotency_key,
                            reason="invocation_failed",
                            reservation_token=(reservation_token if isinstance(reservation_token, str) else None),
                        )
                    except Exception:
                        pass
                    release_reservation()
                    return RoleExecution(
                        exit_code=1,
                        error="codex exec invocation failed; replacement is required",
                        session_id=session_info.get("session_id"),
                        session_key=session_info.get("logical_owner"),
                        session_status="replacement_required",
                    )
                return RoleExecution(exit_code=1, error=f"codex exec invocation failed: {exc}")

            stdout_thread = Thread(target=read_stdout, name="codex-jsonl-reader", daemon=True)
            stderr_thread = Thread(target=read_stderr, name="codex-stderr-reader", daemon=True)
            stdin_thread = Thread(target=feed_stdin, name="codex-stdin-writer", daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            stdin_thread.start()
            timed_out = False
            process_error: str | None = None
            returncode: int | None = None
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
            except Exception as exc:
                # A transport crash is still reconciled through the durable
                # reservation.  If ``thread.started`` was already consumed,
                # the exact root remains resumable; otherwise the reservation
                # is marked orphaned below and requires explicit replacement.
                process_error = f"codex exec process failed: {exc}"
                returncode = 1
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
            if process_error and not error:
                error = process_error
            if lifecycle_errors and not error:
                error = lifecycle_errors[0]
            exit_code: int | None = None if timed_out else returncode
            session_id: str | None = root_thread
            session_status: str | None = None
            session_key: str | None = session_info.get("logical_owner")
            if registry is not None and resumable_identity is not None:
                expected_session = session_info.get("session_id") if session_info.get("mode") == "resume" else None
                replacement = session_info.get("mode") == "replace"
                stale_reason: str | None = None
                if binding_error is not None:
                    stale_reason = (
                        "resume_session_mismatch"
                        if expected_session is not None
                        else "reservation_conflict"
                    )
                elif expected_session is not None and root_thread is not None and root_thread != expected_session:
                    stale_reason = "resume_session_mismatch"
                elif expected_session is not None and root_thread is None and (timed_out or exit_code not in (0, None)):
                    # A failed resume with no normalized thread.started record
                    # is indistinguishable from an unavailable/corrupt exact
                    # session.  Keep the durable replacement boundary explicit.
                    stale_reason = "resume_session_unavailable"
                elif expected_session is None and root_thread is None:
                    # A new resumable invocation cannot be resumed without the
                    # exact root id.  Do not silently launch another session on
                    # the next retry.
                    stale_reason = "thread_started_missing"
                if stale_reason is not None:
                    if (
                        stale_reason == "resume_session_unavailable"
                        and expected_session is not None
                        and session_info.get("mode") == "resume"
                        and _prepared_session_info is None
                    ):
                        # A resume that never produced a root event is a
                        # transport/app-server availability failure, not a
                        # Planner-level retry.  Atomically replace this exact
                        # reservation while retaining its owner/token, then
                        # invoke one fresh configured root through this same
                        # adapter.  The prepared-session override prevents a
                        # second registry prepare/replacement and therefore
                        # bounds recovery to one fresh attempt.
                        try:
                            replacement_info = registry.replace_failed_resume(
                                action,
                                generation_id=generation_id,
                                idempotency_key=idempotency_key,
                                reservation_token=str(reservation_token),
                                expected_session_id=str(expected_session),
                            )
                        except Exception:
                            replacement_info = None
                        if replacement_info is not None:
                            replacement_result = self(
                                action,
                                idempotency_key=idempotency_key,
                                context=context,
                                _prepared_session_info=replacement_info,
                            )
                            release_reservation()
                            return replacement_result
                    try:
                        stale = registry.mark_stale(
                            action,
                            generation_id=generation_id,
                            idempotency_key=idempotency_key,
                            reason=stale_reason,
                            reservation_token=(reservation_token if isinstance(reservation_token, str) else None),
                        )
                    except Exception:
                        stale = {"status": "replacement_required", "logical_owner": session_key}
                    session_status = "replacement_required"
                    session_key = stale.get("logical_owner", session_key)
                    if session_id is None:
                        session_id = stale.get("session_id")
                    if exit_code == 0:
                        exit_code = 1
                    if not error:
                        error = "role session is unavailable; explicit replacement is required"
                elif root_thread is not None:
                    try:
                        bound = registry.record_session(
                            action,
                            generation_id=generation_id,
                            idempotency_key=idempotency_key,
                            session_id=root_thread,
                            replacement=replacement,
                            reservation_token=(reservation_token if isinstance(reservation_token, str) else None),
                        )
                        session_id = str(bound.get("session_id"))
                        session_status = str(bound.get("status"))
                        session_key = str(bound.get("logical_owner"))
                    except Exception:
                        # A successful Codex process with an unwriteable
                        # registry cannot claim continuity.  Surface a
                        # retryable replacement-required result rather than
                        # pretending the phase is resumable.
                        session_status = "replacement_required"
                        if exit_code == 0:
                            exit_code = 1
                        if not error:
                            error = "role session registry update failed; explicit replacement is required"
                elif expected_session is not None:
                    # Some Codex resume builds omit a repeated
                    # thread.started event on a successful continuation.  The
                    # exact id was still targeted, so retain the existing
                    # binding and update only the safe action lineage.
                    try:
                        bound = registry.record_session(
                            action,
                            generation_id=generation_id,
                            idempotency_key=idempotency_key,
                            session_id=str(expected_session),
                            reservation_token=(reservation_token if isinstance(reservation_token, str) else None),
                        )
                        session_id = str(bound.get("session_id"))
                        session_status = str(bound.get("status"))
                        session_key = str(bound.get("logical_owner"))
                    except Exception:
                        session_status = "replacement_required"
                        if exit_code == 0:
                            exit_code = 1
                        if not error:
                            error = "role session registry update failed; explicit replacement is required"
            result = RoleExecution(
                exit_code=exit_code,
                output=output,
                error=error,
                timed_out=timed_out,
                session_id=session_id,
                session_key=session_key,
                session_status=session_status,
            )
            release_reservation()
            return result


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

    @property
    def transport_rebind_intent_path(self) -> Path:
        return self.control_plane / COORDINATOR_TRANSPORT_REBIND_INTENT_FILENAME

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

    def _load_transport_spec_for_target(
        self,
        target: CoordinatorRunSpec,
    ) -> tuple[CoordinatorRunSpec, str]:
        """Read a persisted spec while replacing only its Codex transport.

        A transport release can be rotated while a run is quiescent.  The
        previous spec still binds the old skill bytes, which may no longer be
        present at the same installed path.  Parsing that document directly
        would therefore ask ``CodexExecConfig`` to validate bytes that are no
        longer available.  Project the requested transport into the raw
        document instead: ``CoordinatorRunSpec`` then validates every outer
        field and the requested transport, while the raw transport remains
        represented only by its canonical hash.

        This helper is intentionally private to the transport transaction and
        never becomes a general persisted-spec fallback.  Callers must compare
        the returned raw hash with the durable state/intent before mutating.
        """

        raw = _load_json(self.spec_path)
        if not isinstance(raw, Mapping):
            raise CoordinatorIntegrityError("coordinator_spec.json is missing or invalid")
        if self._looks_like_legacy_spec(raw):
            raise CoordinatorIntegrityError("legacy G5 coordinator specification requires reopen import")
        raw_codex = raw.get("codex_exec", {})
        if not isinstance(raw_codex, Mapping):
            raise CoordinatorIntegrityError("persisted coordinator codex_exec is invalid")

        projected_raw = dict(raw)
        projected_raw["codex_exec"] = target.codex_exec
        try:
            projected = CoordinatorRunSpec.from_dict(projected_raw)
        except (TypeError, ValueError) as exc:
            raise CoordinatorIntegrityError("persisted coordinator specification is malformed") from exc
        if projected.run_id != self.context.run_id:
            raise CoordinatorIntegrityError("persisted coordinator spec has a different run_id")

        # ``projected`` supplies the exact CoordinatorRunSpec normalization
        # (defaults, command tuples, publication policy, and lease float).
        # Reinsert the untouched raw transport to recover the historical
        # state hash without touching the old skill path or bytes.
        raw_payload = projected.to_dict()
        raw_payload["codex_exec"] = dict(_canonical(raw_codex))
        return projected, self._spec_hash_from_payload(raw_payload)

    @staticmethod
    def _spec_hash_from_payload(payload: Mapping[str, Any]) -> str:
        """Hash one already-normalized coordinator spec payload."""

        return _sha256_value(payload)

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
            self._recover_transport_rebind_locked()
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
                # Persisted specifications are parsed before the adapter's
                # explicit active-release check so a stale production binding
                # can be classified as the narrow, read-only preview boundary
                # rather than being collapsed into a generic parse failure.
                CodexExecConfig.from_dict(spec.codex_exec, validate_binding=False),
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
            "role_sessions_ref": f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_ROLE_SESSIONS_FILENAME}",
            "status": "ready",
            "phase": "queued",
            "active_dispatches": [],
            "last_action": None,
            "attempt": 0,
            "no_progress_count": 0,
            "last_no_progress_action": None,
            # Retry evidence is part of the coordinator state/event chain,
            # rather than process-local bookkeeping.  A restart therefore
            # cannot silently reset a bounded action budget.
            "retry_counts": {},
            "retry_blocked": {},
            # State fingerprints bind retry evidence to the program-derived
            # durable phase snapshot that was observed when the action was
            # offered.  They are private coordinator bookkeeping; public
            # status projections never expose these hashes.
            "retry_state_fingerprints": {},
            "dispatch_state_fingerprints": {},
            # A reopen authorizes only the exact stale logical owners that
            # existed at that durable boundary.  The mapping is consumed when
            # a matching Planner offer is admitted; it is deliberately kept
            # separate from retry evidence so reopening never resets history.
            "replacement_authorizations": {},
            # Intentional Product regeneration is a distinct, durable
            # one-shot operator request. Keep its terminal dispatch marker so
            # repeated requests with the same idempotency key remain no-ops.
            "product_regeneration": None,
            "last_control_fingerprint": None,
            "diagnostics": [],
            "publication_policy": dict(spec.publication_policy),
            "publication_ready": False,
            "pending_plan_rebind": None,
            "pending_binding_upgrade": None,
            "pending_transport_rebind": None,
            "last_event_seq": 0,
            "last_event_hash": "",
        }

    @classmethod
    def _normalize_replayed_state(cls, state: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize the persisted active-dispatch list.

        Older coordinator events may contain raw role transport under a
        diagnostic's ``transport`` field.  The event bytes remain immutable
        and are validated by :meth:`_read_replay` first, but the state
        projection used by a resumed coordinator must not carry that text
        forward into a new checkpoint.
        """

        active = state.get("active_dispatches")
        if not isinstance(active, list):
            active = []
        diagnostics = cls._sanitize_diagnostics(state.get("diagnostics"))
        raw_retry_counts = state.get("retry_counts")
        retry_counts: dict[str, int] = {}
        if isinstance(raw_retry_counts, Mapping):
            for key, value in raw_retry_counts.items():
                try:
                    count = int(value)
                except (TypeError, ValueError):
                    continue
                if count >= 0:
                    retry_counts[str(key)] = count
        raw_retry_blocked = state.get("retry_blocked")
        retry_blocked = (
            {str(key): dict(_canonical(value)) for key, value in raw_retry_blocked.items() if isinstance(value, Mapping)}
            if isinstance(raw_retry_blocked, Mapping)
            else {}
        )
        raw_retry_state_fingerprints = state.get("retry_state_fingerprints")
        retry_state_fingerprints = (
            {
                str(key): str(value)
                for key, value in raw_retry_state_fingerprints.items()
                if isinstance(key, str) and _is_sha256(value)
            }
            if isinstance(raw_retry_state_fingerprints, Mapping)
            else {}
        )
        raw_dispatch_state_fingerprints = state.get("dispatch_state_fingerprints")
        dispatch_state_fingerprints = (
            {
                str(key): str(value)
                for key, value in raw_dispatch_state_fingerprints.items()
                if isinstance(key, str) and _is_sha256(value)
            }
            if isinstance(raw_dispatch_state_fingerprints, Mapping)
            else {}
        )
        raw_replacement_authorizations = state.get("replacement_authorizations")
        replacement_authorizations: dict[str, dict[str, str]] = {}
        if isinstance(raw_replacement_authorizations, Mapping):
            for key, value in raw_replacement_authorizations.items():
                if not isinstance(key, str) or not key or not isinstance(value, Mapping):
                    continue
                role = value.get("role")
                subject_id = value.get("subject_id")
                generation_id = value.get("generation_id")
                if not all(
                    isinstance(candidate, str) and candidate.strip()
                    for candidate in (role, subject_id, generation_id)
                ):
                    continue
                binding = {
                    "role": role.strip().lower(),
                    "subject_id": subject_id.strip(),
                    "generation_id": generation_id.strip(),
                }
                # Reopen authorizations may carry the exact action/input and
                # implementation identity that was admitted.  Preserve only
                # the typed, hash-bound fields; malformed optional bindings
                # invalidate this authorization rather than being silently
                # ignored on replay.
                malformed_binding = False
                for field_name in (
                    "run_id",
                    "input_fingerprint",
                    "implementation_identity",
                    "action_fingerprint",
                    "state_fingerprint",
                    "prior_candidate_hash",
                    "prior_review_hash",
                    "predecessor_product_review_hash",
                ):
                    candidate = value.get(field_name)
                    if candidate is None:
                        continue
                    if not isinstance(candidate, str) or not candidate.strip():
                        malformed_binding = True
                        break
                    if field_name != "run_id" and not _is_sha256(candidate.strip()):
                        malformed_binding = True
                        break
                    binding[field_name] = candidate.strip()
                for field_name in ("revision_id", "prior_revision_id"):
                    candidate = value.get(field_name)
                    if candidate is None:
                        continue
                    if (
                        not isinstance(candidate, str)
                        or re.fullmatch(r"rev-\d{4,}", candidate.strip()) is None
                    ):
                        malformed_binding = True
                        break
                    binding[field_name] = candidate.strip()
                candidate = value.get("output_root_ref")
                if candidate is not None:
                    if not isinstance(candidate, str) or not candidate.strip():
                        malformed_binding = True
                    else:
                        binding["output_root_ref"] = candidate.strip()
                for field_name in ("authorization_origin", "request_id", "predecessor_product_review_ref"):
                    candidate = value.get(field_name)
                    if candidate is None:
                        continue
                    if not isinstance(candidate, str) or not candidate.strip():
                        malformed_binding = True
                        break
                    binding[field_name] = candidate.strip()
                if not malformed_binding:
                    replacement_authorizations[key] = binding
        raw_product_regeneration = state.get("product_regeneration")
        product_regeneration: dict[str, Any] | None = None
        if isinstance(raw_product_regeneration, Mapping):
            candidate = dict(_canonical(raw_product_regeneration))
            origin = candidate.get("authorization_origin")
            status = candidate.get("status")
            request_id = candidate.get("request_id")
            request_run_id = candidate.get("run_id")
            request_generation_id = candidate.get("generation_id")
            if (
                origin == PRODUCT_REGENERATION_ORIGIN
                # Keep terminal request markers in the replay projection.
                # They are the durable idempotency/audit boundary for an
                # operator request; dropping them on reload would make a
                # failed/accepted regeneration look unrequested and permit
                # an accidental duplicate request with the same key.
                and status in {"requested", "dispatched", "accepted", "failed"}
                and all(
                    isinstance(value, str) and value.strip()
                    for value in (request_id, request_run_id, request_generation_id)
                )
                and _is_sha256(candidate.get("input_fingerprint"))
                and _is_sha256(candidate.get("implementation_identity"))
                and _is_sha256(candidate.get("action_fingerprint"))
                and _is_sha256(candidate.get("state_fingerprint"))
                and request_run_id == state.get("run_id")
                and request_generation_id == state.get("generation_id")
                and (
                    candidate.get("prior_candidate_hash") is None
                    or _is_sha256(candidate.get("prior_candidate_hash"))
                )
                and (
                    candidate.get("prior_review_hash") is None
                    or _is_sha256(candidate.get("prior_review_hash"))
                )
                and (
                    candidate.get("predecessor_product_review_ref") is None
                    or (
                        isinstance(candidate.get("predecessor_product_review_ref"), str)
                        and candidate.get("predecessor_product_review_ref", "").strip()
                    )
                )
                and (
                    candidate.get("predecessor_product_review_hash") is None
                    or _is_sha256(candidate.get("predecessor_product_review_hash"))
                )
                and (
                    (candidate.get("predecessor_product_review_ref") is None)
                    == (candidate.get("predecessor_product_review_hash") is None)
                )
                and (
                    candidate.get("revision_id") is None
                    or (
                        isinstance(candidate.get("revision_id"), str)
                        and re.fullmatch(r"rev-\d{4,}", candidate.get("revision_id")) is not None
                    )
                )
                and (
                    candidate.get("prior_revision_id") is None
                    or (
                        isinstance(candidate.get("prior_revision_id"), str)
                        and re.fullmatch(r"rev-\d{4,}", candidate.get("prior_revision_id")) is not None
                    )
                )
                and (
                    candidate.get("revision_id") is None
                    or (
                        isinstance(candidate.get("output_root_ref"), str)
                        and candidate.get("output_root_ref", "").strip()
                    )
                )
            ):
                product_regeneration = candidate
        return {
            "schema_version": COORDINATOR_SCHEMA_VERSION,
            "kind": "run_coordinator_state",
            "run_id": state.get("run_id"),
            "generation_id": state.get("generation_id"),
            "planner_ref": state.get("planner_ref"),
            "planner_hash": state.get("planner_hash"),
            "spec_hash": state.get("spec_hash"),
            "spec_ref": state.get("spec_ref", f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_SPEC_FILENAME}"),
            "role_sessions_ref": state.get(
                "role_sessions_ref",
                f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_ROLE_SESSIONS_FILENAME}",
            ),
            "status": state.get("status", "ready"),
            "phase": state.get("phase", "queued"),
            "active_dispatches": [dict(_canonical(value)) for value in active if isinstance(value, Mapping)],
            "last_action": state.get("last_action") or state.get("last_completed_action"),
            "attempt": int(state.get("attempt", 0) or 0),
            "no_progress_count": int(state.get("no_progress_count", state.get("consecutive_no_progress", 0)) or 0),
            "last_no_progress_action": state.get("last_no_progress_action"),
            "retry_counts": retry_counts,
            "retry_blocked": retry_blocked,
            "retry_state_fingerprints": retry_state_fingerprints,
            "dispatch_state_fingerprints": dispatch_state_fingerprints,
            "replacement_authorizations": replacement_authorizations,
            "product_regeneration": product_regeneration,
            "last_control_fingerprint": state.get("last_control_fingerprint"),
            "diagnostics": diagnostics,
            "publication_policy": dict(state.get("publication_policy") or {}) if isinstance(state.get("publication_policy") or {}, Mapping) else {},
            "publication_ready": bool(state.get("publication_ready", False)),
            "pending_plan_rebind": dict(_canonical(state.get("pending_plan_rebind"))) if isinstance(state.get("pending_plan_rebind"), Mapping) else None,
            "pending_binding_upgrade": dict(_canonical(state.get("pending_binding_upgrade"))) if isinstance(state.get("pending_binding_upgrade"), Mapping) else None,
            "pending_transport_rebind": dict(_canonical(state.get("pending_transport_rebind"))) if isinstance(state.get("pending_transport_rebind"), Mapping) else None,
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
    def _product_agent_owner_key(
        action: PlannerAction,
        *,
        run_id: str,
        generation_id: str,
    ) -> str | None:
        """Return the shared Product Agent logical-owner admission key.

        Product preview and terminal composition deliberately retain distinct
        action/slot keys for telemetry and retry accounting, but they all use
        one run/generation Product Agent session.  Admission must therefore
        serialize those action slots by that owner while leaving unrelated
        role actions free to run concurrently.
        """

        if action.role.strip().lower() != "product_agent":
            return None
        identity = _role_session_identity(
            action,
            run_id=run_id,
            generation_id=generation_id,
        )
        return identity[0] if identity is not None else None

    @classmethod
    def _active_product_agent_owners(
        cls,
        entries: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        generation_id: str,
    ) -> set[str]:
        """Project active Product Agent dispatches onto logical owners."""

        owners: set[str] = set()
        for entry in entries:
            raw_action = entry.get("action") if isinstance(entry, Mapping) else None
            if not isinstance(raw_action, Mapping):
                continue
            owner = cls._product_agent_owner_key(
                _action(raw_action),
                run_id=run_id,
                generation_id=generation_id,
            )
            if owner is not None:
                owners.add(owner)
        return owners

    def _reserved_product_agent_owner_locked(
        self,
        action: PlannerAction,
        *,
        run_id: str,
        generation_id: str,
    ) -> str | None:
        """Return a normal in-flight registry owner reservation, if any.

        This is a read-only race guard for the narrow case where another
        coordinator/adapter has claimed the shared Product Agent session but
        its dispatch projection has not reached this coordinator yet.  A
        malformed or unavailable registry is not interpreted as contention;
        the normal role transport remains responsible for surfacing that
        integrity failure.
        """

        owner = self._product_agent_owner_key(
            action,
            run_id=run_id,
            generation_id=generation_id,
        )
        if owner is None:
            return None
        try:
            document = RoleSessionRegistry(self.context)._read_unlocked()  # noqa: SLF001 - caller owns coordinator lock
        except (CoordinatorError, OSError, TypeError, ValueError):
            return None
        sessions = document.get("sessions")
        entry = sessions.get(owner) if isinstance(sessions, Mapping) else None
        if not isinstance(entry, Mapping):
            return None
        if entry.get("status") == "reserved" and entry.get("reservation_status") == "reserved":
            return owner
        return None

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
    def _is_control_action(action: PlannerAction | Mapping[str, Any]) -> bool:
        return _is_control_action(action)

    @staticmethod
    def _capacity_scope(action: PlannerAction) -> str | None:
        """Map one physical role action to its persisted capacity subcap."""

        role = action.role.strip().lower()
        name = action.action.strip().lower()
        if role in {"entity_resolution_owner", "entity_resolution"} or name in {
            "resolve_identity",
            "resume_identity_resolution",
            "repair_identity_result",
        }:
            return "entity_resolution"
        if role == "analytical_owner" or name in _ANALYTICAL_OWNER_ACTIONS:
            return "analytical_owner"
        if role == "specialist":
            return "specialist"
        return None

    @classmethod
    def _capacity_counts(cls, entries: Sequence[Mapping[str, Any]]) -> tuple[int, dict[str, int]]:
        """Count active physical dispatches by total and role subcap."""

        counts = {"entity_resolution": 0, "analytical_owner": 0, "specialist": 0}
        total = 0
        for entry in entries:
            raw_action = entry.get("action") if isinstance(entry, Mapping) else None
            if not isinstance(raw_action, Mapping):
                continue
            action = _action(raw_action)
            # Coordinator control actions never enter ``active_dispatches``;
            # retain this guard for imported/recovered state from older runs.
            if cls._is_control_action(action):
                continue
            total += 1
            scope = cls._capacity_scope(action)
            if scope is not None:
                counts[scope] += 1
        return total, counts

    def _resolution_capacity(self) -> Any:
        """Read the persisted run capacity without reconciling or mutating it.

        Most unit-level coordinator fixtures intentionally omit the entity
        workspace; those retain the current default capacity.  A production
        run has a signed/hash-bound ``entity_resolution/state.json`` and its
        complete ``ResolutionCapacity`` is authoritative for every
        coordinator-dispatched physical action.
        """

        from .entity_resolution import ResolutionCapacity

        path = self.context.resolve_run_path("entity_resolution/state.json")
        if not path.exists():
            return ResolutionCapacity()
        if path.is_symlink() or not path.is_file():
            raise CoordinatorIntegrityError("persisted resolution capacity is not a regular file")
        raw = _load_json(path)
        if not isinstance(raw, Mapping) or raw.get("run_id") != self.context.run_id:
            raise CoordinatorIntegrityError("persisted resolution capacity run identity is invalid")
        try:
            capacity = ResolutionCapacity.from_dict(raw["capacity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CoordinatorIntegrityError("persisted resolution capacity is invalid") from exc
        # ``EntityResolutionWorkspace`` hashes canonical JSON *with* a
        # trailing newline; mirror that exact digest here without loading the
        # workspace (whose load path may reconcile and write recovery state).
        state_hash = raw.get("state_hash")
        if isinstance(state_hash, str):
            unsigned = {key: value for key, value in raw.items() if key != "state_hash"}
            if _sha256_bytes(_json_bytes(unsigned)) != state_hash:
                raise CoordinatorIntegrityError("persisted resolution capacity state hash is invalid")
        return capacity

    @classmethod
    def _capacity_admits(
        cls,
        action: PlannerAction,
        *,
        total: int,
        subcounts: Mapping[str, int],
        capacity: Any,
    ) -> tuple[bool, str | None]:
        """Return ``(admissible, reason)`` for a new physical dispatch."""

        if cls._is_control_action(action):
            return True, None
        if total >= int(capacity.total_active):
            return False, "total_active_capacity"
        scope = cls._capacity_scope(action)
        if scope is not None and int(subcounts.get(scope, 0)) >= int(getattr(capacity, scope)):
            return False, f"{scope}_capacity"
        return True, None

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
            if value.get("runner_process_start") is not None:
                entry["runner_process_start"] = str(value.get("runner_process_start"))
            result.append(entry)
        return result

    def _append_event_locked(
        self,
        state: dict[str, Any],
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._sanitize_state_in_place(state)
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
            # Role transport fields in event payloads are projected at their
            # call sites.  Keep arbitrary Planner/action metadata canonical;
            # legacy scrubbing is intentionally limited to diagnostics.
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
        _atomic_json(self.state_path, self._state_for_persistence(state))

    @staticmethod
    def _durable_transport(execution: RoleExecution | Mapping[str, Any] | None) -> dict[str, Any]:
        """Project role transport metadata without persisting model text.

        Role output and stderr may contain prompts, business data, or other
        sensitive text.  Coordinator events/state retain only execution status
        and privacy-safe session continuity metadata; detailed transport text
        remains an ephemeral diagnostic at the caller boundary.
        """

        if isinstance(execution, RoleExecution):
            raw = execution.to_dict()
        elif isinstance(execution, Mapping):
            raw = execution
        else:
            raw = {}
        value: dict[str, Any] = {
            "exit_code": raw.get("exit_code"),
            "timed_out": bool(raw.get("timed_out", False)),
        }
        value["ok"] = value["exit_code"] == 0 and not value["timed_out"]
        for field in ("session_id", "session_key", "session_status"):
            field_value = raw.get(field)
            if field_value is not None:
                value[field] = _safe_text(field_value)
        return value

    @classmethod
    def _sanitize_durable_value(cls, value: Any) -> Any:
        """Remove raw role transport from one diagnostic projection.

        A paused run can have been written by an older coordinator that
        persisted ``RoleExecution.to_dict()`` under a diagnostic.  Callers
        deliberately apply this only to the diagnostics channel; arbitrary
        Planner/action metadata in coordinator state remains untouched.  Only
        known top-level diagnostic transport fields are projected; ordinary
        diagnostic text and nested action metadata remain available to
        operators.
        """

        normalized = _canonical(value)
        if not isinstance(normalized, Mapping):
            return normalized
        result = dict(normalized)
        for field in ("transport", "execution", "role_execution"):
            candidate = result.get(field)
            if isinstance(candidate, Mapping):
                result[field] = cls._durable_transport(candidate)
        return result

    @classmethod
    def _diagnostic_value(cls, value: Mapping[str, Any]) -> Any:
        return cls._sanitize_durable_value(value)

    @classmethod
    def _sanitize_diagnostics(cls, diagnostics: Any) -> list[Any]:
        if not isinstance(diagnostics, list):
            return []
        values = [cls._diagnostic_value(value) for value in diagnostics]
        return values[-32:]

    @classmethod
    def _state_for_persistence(cls, state: Mapping[str, Any]) -> dict[str, Any]:
        # Preserve every state field exactly as its existing canonical JSON
        # projection; only the diagnostic transport channel has a legacy raw
        # text migration rule.
        projected = _canonical(state)
        if not isinstance(projected, Mapping):
            raise CoordinatorIntegrityError("coordinator state must be an object")
        result = dict(projected)
        result["diagnostics"] = cls._sanitize_diagnostics(result.get("diagnostics"))
        return result

    @classmethod
    def _sanitize_state_in_place(cls, state: dict[str, Any]) -> None:
        state["diagnostics"] = cls._sanitize_diagnostics(state.get("diagnostics"))

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
        elif state.get("status") in {"waiting", "blocked_rethink"} and isinstance(
            state.get("last_no_progress_action"), Mapping
        ):
            next_action = dict(state["last_no_progress_action"])
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

    def _data_revision_recovery_status(
        self,
        state: Mapping[str, Any],
        *,
        pending_revision_id: str,
        current_revision_id: str | None,
    ) -> CoordinatorStatus:
        """Project an append/admit handoff without treating it as a failure."""

        status = self._status_from_state(state)
        diagnostic = {
            "kind": "data_revision_recovery",
            "reason": "pending_revision_not_current",
            "pending_revision_id": pending_revision_id,
            "current_revision_id": current_revision_id,
        }
        return CoordinatorStatus(
            run_id=status.run_id,
            generation_id=status.generation_id,
            status="waiting",
            phase="data_revision_recovery",
            next_action=status.next_action,
            owner=status.owner,
            lease_expires_at=status.lease_expires_at,
            diagnostics=(*status.diagnostics, diagnostic),
            last_event_seq=status.last_event_seq,
            last_event_hash=status.last_event_hash,
            publication_ready=False,
            publication_enabled=status.publication_enabled,
            no_progress_count=status.no_progress_count,
            next_actions=status.next_actions,
            active_dispatches=status.active_dispatches,
        )

    def _data_refresh_waiting_status(
        self,
        state: Mapping[str, Any],
        *,
        phase: str,
        reason: str,
    ) -> CoordinatorStatus:
        status = self._status_from_state(state)
        diagnostic = {"kind": "data_refresh_pending", "reason": reason}
        return CoordinatorStatus(
            run_id=status.run_id,
            generation_id=status.generation_id,
            status="waiting",
            phase=phase,
            next_action=status.next_action,
            owner=status.owner,
            lease_expires_at=status.lease_expires_at,
            diagnostics=(*status.diagnostics, diagnostic),
            last_event_seq=status.last_event_seq,
            last_event_hash=status.last_event_hash,
            publication_ready=False,
            publication_enabled=status.publication_enabled,
            no_progress_count=status.no_progress_count,
            next_actions=status.next_actions,
            active_dispatches=status.active_dispatches,
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

    def _transport_rebind_status(self, state: Mapping[str, Any]) -> CoordinatorStatus:
        """Project a quiescent same-lineage transport rebind intent."""

        status = self._status_from_state(state)
        return CoordinatorStatus(
            run_id=status.run_id,
            generation_id=status.generation_id,
            status="waiting",
            phase="transport_rebind_pending",
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

    def _validate_transport_target(self, target: CoordinatorRunSpec) -> CodexExecConfig:
        config = CodexExecConfig.from_dict(target.codex_exec)
        if target.codex_exec and not self._custom_role_runner:
            config.validate_skill_binding(
                required=True,
                verify_active=True,
                repo_root=Path(__file__).resolve().parents[2],
                role_cwd=self.context.run_root,
            )
        return config

    def _transport_rebind_intent_ref(self) -> str:
        return f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_TRANSPORT_REBIND_INTENT_FILENAME}"

    def _transport_rebind_intent_document(
        self,
        target: CoordinatorRunSpec,
        *,
        old_spec_hash: str,
        new_spec_hash: str,
        prior_state: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str]:
        prior = {
            "status": str(prior_state.get("status") or "ready"),
            "phase": str(prior_state.get("phase") or "queued"),
            "publication_ready": bool(prior_state.get("publication_ready", False)),
        }
        body: dict[str, Any] = {
            "schema_version": COORDINATOR_SCHEMA_VERSION,
            "kind": "coordinator_transport_rebind_intent",
            "run_id": target.run_id,
            "old_spec_hash": old_spec_hash,
            "new_spec_hash": new_spec_hash,
            "transport_fields": ["codex_exec"],
            "from": {
                "generation_id": target.generation_id,
                "planner_ref": target.planner_ref,
                "planner_hash": target.planner_hash,
            },
            "to": {
                "generation_id": target.generation_id,
                "planner_ref": target.planner_ref,
                "planner_hash": target.planner_hash,
            },
            "target_spec": target.to_dict(),
            "prior_state": prior,
        }
        return body, _sha256_value(body)

    def _read_transport_rebind_intent_locked(self) -> tuple[dict[str, Any], CoordinatorRunSpec]:
        path = self.transport_rebind_intent_path
        if path.is_symlink() or not path.is_file():
            raise CoordinatorIntegrityError("transport rebind intent is missing or not a regular file")
        value = _load_json(path)
        if not isinstance(value, Mapping):
            raise CoordinatorIntegrityError("transport rebind intent must be an object")
        required = {
            "schema_version",
            "kind",
            "run_id",
            "old_spec_hash",
            "new_spec_hash",
            "transport_fields",
            "from",
            "to",
            "target_spec",
            "prior_state",
            "intent_hash",
        }
        if set(value) != required:
            raise CoordinatorIntegrityError("transport rebind intent shape is invalid")
        if value.get("schema_version") != COORDINATOR_SCHEMA_VERSION or value.get("kind") != "coordinator_transport_rebind_intent":
            raise CoordinatorIntegrityError("transport rebind intent version or kind is invalid")
        intent_hash = value.get("intent_hash")
        if not _is_sha256(intent_hash):
            raise CoordinatorIntegrityError("transport rebind intent hash is invalid")
        body = dict(value)
        body.pop("intent_hash", None)
        if _sha256_value(body) != intent_hash:
            raise CoordinatorIntegrityError("transport rebind intent hash mismatch")
        if value.get("run_id") != self.context.run_id:
            raise CoordinatorIntegrityError("transport rebind intent run_id does not match context")
        old_hash = value.get("old_spec_hash")
        new_hash = value.get("new_spec_hash")
        if not _is_sha256(old_hash) or not _is_sha256(new_hash):
            raise CoordinatorIntegrityError("transport rebind intent specification hash is invalid")
        if tuple(value.get("transport_fields") or ()) != ("codex_exec",):
            raise CoordinatorIntegrityError("transport rebind intent fields are invalid")
        target_raw = value.get("target_spec")
        if not isinstance(target_raw, Mapping):
            raise CoordinatorIntegrityError("transport rebind intent target is invalid")
        try:
            target = CoordinatorRunSpec.from_dict(target_raw)
        except (TypeError, ValueError) as exc:
            raise CoordinatorIntegrityError("transport rebind intent target is malformed") from exc
        if target.run_id != self.context.run_id or self._spec_hash(target) != new_hash:
            raise CoordinatorIntegrityError("transport rebind intent target hash is invalid")
        for lineage_name in ("generation_id", "planner_ref", "planner_hash"):
            target_lineage = target.to_dict().get(lineage_name)
            for side in ("from", "to"):
                lineage = value.get(side)
                if (
                    not isinstance(lineage, Mapping)
                    or set(lineage) != {"generation_id", "planner_ref", "planner_hash"}
                    or lineage.get(lineage_name) != target_lineage
                ):
                    raise CoordinatorIntegrityError("transport rebind intent planner lineage is invalid")
        prior = value.get("prior_state")
        if not isinstance(prior, Mapping) or set(prior) != {"status", "phase", "publication_ready"}:
            raise CoordinatorIntegrityError("transport rebind intent prior state is invalid")
        if not isinstance(prior.get("status"), str) or not isinstance(prior.get("phase"), str) or not isinstance(prior.get("publication_ready"), bool):
            raise CoordinatorIntegrityError("transport rebind intent prior state values are invalid")
        return dict(value), target

    def _write_transport_rebind_intent_locked(
        self,
        document: Mapping[str, Any],
        intent_hash: str,
    ) -> None:
        path = self.transport_rebind_intent_path
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise CoordinatorIntegrityError("transport rebind intent target is not a regular file")
        if path.exists():
            existing, _ = self._read_transport_rebind_intent_locked()
            if existing.get("intent_hash") != intent_hash:
                raise CoordinatorConflictError("a different transport rebind intent already exists")
            return
        _atomic_json(path, {**dict(document), "intent_hash": intent_hash})

    def _remove_transport_rebind_intent_locked(self) -> None:
        path = self.transport_rebind_intent_path
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise CoordinatorIntegrityError("transport rebind intent target is not a regular file")
        if path.exists():
            path.unlink()

    def validate_read_only_resume_evidence(self, *, reject_transport_rebind: bool = False) -> None:
        """Validate persisted recovery evidence without recovering or writing.

        Product-regeneration status may run while the installed skill has
        rotated, but it must not project through a malformed or half-written
        coordinator recovery transaction.  This read-only guard reuses the
        canonical replay, transport-intent, and target-spec validators; it
        intentionally never removes an orphan intent or advances a state
        checkpoint as :meth:`_recover_transport_rebind_locked` does.
        """

        if self._legacy_pending:
            raise CoordinatorIntegrityError("legacy coordinator recovery cannot be previewed")
        with self._locked(create=False):
            if self.spec_path.is_symlink() or not self.spec_path.is_file():
                raise CoordinatorIntegrityError("coordinator specification is unavailable for preview")
            if self.state_path.is_symlink() or not self.state_path.is_file():
                raise CoordinatorIntegrityError("coordinator state is unavailable for preview")
            raw_spec = _load_json(self.spec_path)
            if not isinstance(raw_spec, Mapping) or self._looks_like_legacy_spec(raw_spec):
                raise CoordinatorIntegrityError("legacy coordinator specification cannot be previewed")
            state, _ = self._read_replay()
            assert state is not None
            # A pending planner/binding transaction changes more than the
            # Codex transport.  Let the normal recovery path handle it rather
            # than exposing a key derived from a mixed persisted boundary.
            if isinstance(state.get("pending_plan_rebind"), Mapping):
                raise CoordinatorIntegrityError("coordinator plan rebind is pending")
            if isinstance(state.get("pending_binding_upgrade"), Mapping):
                raise CoordinatorIntegrityError("coordinator binding upgrade is pending")

            intent_path = self.transport_rebind_intent_path
            if intent_path.is_symlink() or (intent_path.exists() and not intent_path.is_file()):
                raise CoordinatorIntegrityError("transport rebind intent target is not a regular file")
            pending = state.get("pending_transport_rebind")
            if pending is None:
                if not intent_path.exists():
                    return
                # A process may have stopped after writing the private intent
                # but before its started event.  Validate ownership and the
                # exact old source hash; do not silently remove it on status.
                intent, target = self._read_transport_rebind_intent_locked()
                persisted = self._load_spec_document()
                persisted_hash = self._spec_hash(persisted)
                if persisted_hash != intent.get("old_spec_hash"):
                    raise CoordinatorIntegrityError("orphan transport rebind intent source changed")
                if any(
                    getattr(persisted, field_name) != getattr(target, field_name)
                    for field_name in ("run_id", "generation_id", "planner_ref", "planner_hash")
                ):
                    raise CoordinatorIntegrityError("orphan transport rebind intent lineage changed")
                if reject_transport_rebind:
                    raise CoordinatorIntegrityError("transport rebind recovery is pending")
                return
            if not isinstance(pending, Mapping):
                raise CoordinatorIntegrityError("pending transport rebind state is invalid")
            intent, target = self._read_transport_rebind_intent_locked()
            expected_pending = {
                "run_id",
                "old_spec_hash",
                "new_spec_hash",
                "transport_fields",
                "from",
                "to",
                "intent_ref",
                "intent_sha256",
                "prior_state",
            }
            if set(pending) != expected_pending:
                raise CoordinatorIntegrityError("pending transport rebind summary is invalid")
            if (
                pending.get("run_id") != intent.get("run_id")
                or pending.get("old_spec_hash") != intent.get("old_spec_hash")
                or pending.get("new_spec_hash") != intent.get("new_spec_hash")
                or tuple(pending.get("transport_fields") or ()) != ("codex_exec",)
                or pending.get("intent_ref") != self._transport_rebind_intent_ref()
                or pending.get("intent_sha256") != intent.get("intent_hash")
                or _canonical(pending.get("from")) != _canonical(intent.get("from"))
                or _canonical(pending.get("to")) != _canonical(intent.get("to"))
                or _canonical(pending.get("prior_state")) != _canonical(intent.get("prior_state"))
            ):
                raise CoordinatorIntegrityError("pending transport rebind summary does not match intent")
            old_hash = intent.get("old_spec_hash")
            new_hash = intent.get("new_spec_hash")
            if not _is_sha256(old_hash) or not _is_sha256(new_hash):
                raise CoordinatorIntegrityError("pending transport rebind specification hash is invalid")
            self._validate_transport_target(target)
            persisted, persisted_hash = self._load_transport_spec_for_target(target)
            if persisted_hash not in {old_hash, new_hash}:
                raise CoordinatorConflictError("pending transport rebind source specification changed")
            if (
                persisted.run_id != target.run_id
                or persisted.generation_id != target.generation_id
                or persisted.planner_ref != target.planner_ref
                or persisted.planner_hash != target.planner_hash
                or persisted.to_dict() != target.to_dict()
            ):
                raise CoordinatorConflictError("pending transport rebind specification is not transport-only")
            if self._active_entries(state):
                raise CoordinatorConflictError("coordinator cannot preview transport rebind while dispatches are active")
            if reject_transport_rebind:
                raise CoordinatorIntegrityError("transport rebind recovery is pending")

    def _recover_transport_rebind_locked(self) -> CoordinatorStatus | None:
        """Finish an interrupted same-lineage transport rebind transaction."""

        if self._legacy_pending:
            return None
        intent_path = self.transport_rebind_intent_path
        if intent_path.is_symlink() or (intent_path.exists() and not intent_path.is_file()):
            raise CoordinatorIntegrityError("transport rebind intent target is not a regular file")
        intent_exists = intent_path.exists()
        if not self.spec_path.is_file() or self.spec_path.is_symlink():
            if intent_exists:
                self._read_transport_rebind_intent_locked()
                self._remove_transport_rebind_intent_locked()
            return None
        if not self.state_path.is_file() or self.state_path.is_symlink():
            if intent_exists:
                self._read_transport_rebind_intent_locked()
                self._remove_transport_rebind_intent_locked()
            return None
        raw_spec = _load_json(self.spec_path)
        if not isinstance(raw_spec, Mapping) or self._looks_like_legacy_spec(raw_spec):
            return None
        state, _ = self._read_replay()
        assert state is not None
        pending = state.get("pending_transport_rebind")
        if pending is None:
            if intent_exists:
                # A process may have died after writing the private intent but
                # before publishing the started event.  Validate ownership
                # before removing this orphan; never delete arbitrary files.
                self._read_transport_rebind_intent_locked()
                self._remove_transport_rebind_intent_locked()
            return None
        if not isinstance(pending, Mapping):
            raise CoordinatorIntegrityError("pending transport rebind state is invalid")
        intent, target = self._read_transport_rebind_intent_locked()
        expected_pending = {
            "run_id",
            "old_spec_hash",
            "new_spec_hash",
            "transport_fields",
            "from",
            "to",
            "intent_ref",
            "intent_sha256",
            "prior_state",
        }
        if set(pending) != expected_pending:
            raise CoordinatorIntegrityError("pending transport rebind summary is invalid")
        if (
            pending.get("run_id") != intent.get("run_id")
            or pending.get("old_spec_hash") != intent.get("old_spec_hash")
            or pending.get("new_spec_hash") != intent.get("new_spec_hash")
            or tuple(pending.get("transport_fields") or ()) != ("codex_exec",)
            or pending.get("intent_ref") != self._transport_rebind_intent_ref()
            or pending.get("intent_sha256") != intent.get("intent_hash")
            or _canonical(pending.get("from")) != _canonical(intent.get("from"))
            or _canonical(pending.get("to")) != _canonical(intent.get("to"))
            or _canonical(pending.get("prior_state")) != _canonical(intent.get("prior_state"))
        ):
            raise CoordinatorIntegrityError("pending transport rebind summary does not match intent")
        old_hash = str(intent["old_spec_hash"])
        new_hash = str(intent["new_spec_hash"])
        prior_state = intent["prior_state"]
        self._validate_transport_target(target)
        persisted, persisted_hash = self._load_transport_spec_for_target(target)
        if persisted_hash not in {old_hash, new_hash}:
            raise CoordinatorConflictError("pending transport rebind source specification changed")
        if (
            persisted.run_id != target.run_id
            or persisted.generation_id != target.generation_id
            or persisted.planner_ref != target.planner_ref
            or persisted.planner_hash != target.planner_hash
        ):
            raise CoordinatorConflictError("pending transport rebind planner lineage changed")
        if persisted.to_dict() != target.to_dict():
            raise CoordinatorConflictError("pending transport rebind would change non-transport specification fields")
        if self._active_entries(state):
            raise CoordinatorConflictError("coordinator cannot recover transport rebind while dispatches are active")
        if persisted_hash == old_hash:
            self._write_spec(target)
        state.update(
            {
                "spec_hash": new_hash,
                "spec_ref": f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_SPEC_FILENAME}",
                "status": str(prior_state.get("status") or "ready"),
                "phase": str(prior_state.get("phase") or "queued"),
                "pending_transport_rebind": None,
                "publication_ready": bool(prior_state.get("publication_ready", False)),
            }
        )
        self._append_event_locked(state, "coordinator_transport_rebound", pending)
        self._remove_transport_rebind_intent_locked()
        self._spec = target
        self._configure_from_spec(target)
        return self._status_from_state(state)

    def _pending_rebind_status(self) -> CoordinatorStatus | None:
        if self._legacy_pending or not self.spec_path.exists():
            return None
        with self._locked(create=False):
            recovered = self._recover_transport_rebind_locked()
            if recovered is not None:
                return recovered
            state, _ = self._read_replay()
            assert state is not None
            if isinstance(state.get("pending_binding_upgrade"), Mapping):
                return self._binding_upgrade_status(state)
            if isinstance(state.get("pending_transport_rebind"), Mapping):
                return self._transport_rebind_status(state)
            if not isinstance(state.get("pending_plan_rebind"), Mapping):
                return None
            return self._status_from_state(state)

    def _consume_pending_data_refresh_locked(self, state: dict[str, Any]) -> tuple[str, CoordinatorStatus]:
        """Consume the canonical D admission at the one safe scheduler boundary.

        The caller holds the coordinator lock.  No Planner action is queried or
        dispatched until this method has either applied the admission or
        durably left it pending for a later boundary.
        """

        from .data_revisions import DataRevisionStore
        from .lifecycle import RunLifecycle
        from .requirement_planning import (
            RequirementExecutionPlan,
            RequirementSupervisorWorkspace,
            inspect_product_manifest,
        )
        from .run_extension import (
            DataRefreshNotSafeError,
            DataRefreshSupersededError,
            RequirementRunExtension,
        )

        store = DataRevisionStore(self.context)
        pending = store.pending_data_refresh(allow_stale=True)
        if pending is None:
            return "none", self._status_from_state(state)
        current_revision = store.current()
        if (
            current_revision is None
            or current_revision.revision_id != pending.data_revision_id
            or current_revision.manifest_hash != pending.data_revision_manifest_hash
            or current_revision.archive_sha256 != pending.data_revision_archive_sha256
        ):
            # A crash after a successor D pointer swap but before its pending
            # admission leaves the older immutable admission on disk.  Keep
            # that audit bytes untouched and wait for the exact successor
            # admission to coalesce; this is recoverable state, not a planner
            # or technical failure.
            return "revision_recovery", self._data_revision_recovery_status(
                state,
                pending_revision_id=pending.data_revision_id,
                current_revision_id=None if current_revision is None else current_revision.revision_id,
            )
        if self._active_entries(state) or self._futures:
            return "active", self._status_from_state(state)

        # A data refresh is a successor generation.  Keep the current
        # generation authoritative until its existing final product manifest
        # is valid and published; this preserves the parent product required
        # by generation-aware assembly.  The planner remains runnable while
        # this admission stays pending, so product publication can complete
        # at its ordinary safe boundary without introducing a second gate.
        active_lifecycle = RunLifecycle.load(self.context)
        active_metadata = active_lifecycle.generation_metadata
        active_revision_id = None
        if active_metadata is not None:
            active_ref = active_metadata.data_revision_ref
            if isinstance(active_ref, str):
                parts = Path(active_ref).parts
                if len(parts) == 4 and parts[:2] == ("data_room", "revisions"):
                    active_revision_id = parts[2]
        already_bound_to_pending = bool(
            active_metadata is not None
            and active_metadata.data_revision_hash == pending.data_revision_manifest_hash
            and (
                active_metadata.data_revision_ref == pending.data_revision_ref
                or active_revision_id == pending.data_revision_id
            )
        )
        if not already_bound_to_pending:
            product_view = inspect_product_manifest(
                self.context,
                active_lifecycle.generation_id,
                active_lifecycle.product_manifest_ref,
                metadata=active_metadata,
            )
            if not product_view.get("valid"):
                return "waiting_product", self._data_refresh_waiting_status(
                    state,
                    phase="waiting_product",
                    reason="parent_product_manifest_incomplete",
                )

        # Product reconciliation may durably advance the authoritative state or
        # plan after this admission captured its parent CAS.  Rebase the
        # pending bytes at the verified safe boundary, retaining the original
        # parent hashes as immutable provenance and structurally coalescing any
        # newer current-plan records before constructing the successor target.
        parent_plan = RequirementSupervisorWorkspace(self.context).load()
        try:
            parent_plan_hash = _sha256_bytes(Path(active_lifecycle.plan_path).read_bytes())
        except (AttributeError, OSError, ValueError):
            parent_plan_hash = _sha256_bytes(_json_bytes(parent_plan.to_dict()))
        parent_needs_rebase = (
            not already_bound_to_pending
            and (
                pending.expected_parent_generation_id != active_lifecycle.generation_id
                or pending.expected_parent_state_hash != active_lifecycle.snapshot.manifest_hash
                or pending.expected_parent_plan_hash != parent_plan_hash
            )
        )
        if parent_needs_rebase:
            pending = store.rebase_pending_data_refresh(
                pending.intent_hash,
                expected_parent_generation_id=active_lifecycle.generation_id,
                expected_parent_state_hash=active_lifecycle.snapshot.manifest_hash,
                expected_parent_plan_hash=parent_plan_hash,
                plan=parent_plan.to_dict(),
            )

        revision = store.load(pending.data_revision_id)
        requested_plan = RequirementExecutionPlan.from_dict(dict(pending.plan))
        # refresh_data advances a stale candidate revision to the current
        # parent revision + 1. Derive the same immutable candidate before
        # constructing the coordinator target so its hash is known before
        # the publisher runs.
        plan = requested_plan
        if requested_plan != parent_plan and requested_plan.revision <= parent_plan.revision:
            plan = RequirementExecutionPlan(
                input_records=requested_plan.input_records,
                groups=requested_plan.groups,
                planner_ref=requested_plan.planner_ref,
                portfolio_strategy=requested_plan.portfolio_strategy,
                revision=parent_plan.revision + 1,
            )
        parent_ordinal = int(pending.expected_parent_generation_id[2:])
        target_generation_id = f"G-{parent_ordinal + 1:04d}"
        base = self._load_spec_document()
        target = CoordinatorRunSpec(
            run_id=base.run_id,
            generation_id=target_generation_id,
            planner_ref=plan.planner_ref,
            planner_hash=_sha256_bytes(_json_bytes(plan.to_dict())),
            role_dispatch_command=base.role_dispatch_command,
            publication_policy=base.publication_policy,
            codex_exec=base.codex_exec,
            lease_ttl_seconds=base.lease_ttl_seconds,
        )
        if isinstance(state.get("pending_binding_upgrade"), Mapping) or isinstance(
            state.get("pending_transport_rebind"), Mapping
        ):
            return "blocked", self._status_from_state(state)
        existing_rebind = state.get("pending_plan_rebind")
        if isinstance(existing_rebind, Mapping) and existing_rebind.get("new_spec_hash") != self._spec_hash(target):
            return "blocked", self._status_from_state(state)
        published: list[Any] = []

        def publisher(_target: CoordinatorRunSpec) -> None:
            refresh_kwargs: dict[str, Any] = {
                "plan": plan,
                "data_revision": revision,
                "reopened_item_ids": pending.reopened_item_ids,
                "expected_parent_state_hash": pending.expected_parent_state_hash,
                "expected_parent_plan_hash": pending.expected_parent_plan_hash,
                "generation_id": target_generation_id,
            }
            if pending.expected_parent_generation_id != "G-0001":
                refresh_kwargs["expected_parent_generation_id"] = pending.expected_parent_generation_id

            # A crash after refresh_data publishes G-N but before the
            # coordinator spec/state commit leaves the parent CAS stale. If
            # the active generation is already the exact requested refresh,
            # omit only the old-parent CAS fields and let refresh_data's own
            # immutable idempotence checks recover the staged generation.
            try:
                lifecycle = RunLifecycle.load(self.context)
                metadata = lifecycle.generation_metadata
                active_plan_hash = _sha256_bytes(lifecycle.plan_path.read_bytes())
                if (
                    metadata is not None
                    and metadata.generation_id == target_generation_id
                    and metadata.data_revision_hash == pending.data_revision_manifest_hash
                    and tuple(metadata.reopened_item_ids) == tuple(pending.reopened_item_ids)
                    and active_plan_hash == target.planner_hash
                ):
                    refresh_kwargs.pop("expected_parent_generation_id", None)
                    refresh_kwargs.pop("expected_parent_state_hash", None)
                    refresh_kwargs.pop("expected_parent_plan_hash", None)
            except (OSError, KeyError, TypeError, ValueError):
                pass
            published.append(
                RequirementRunExtension.refresh_data(
                    self.context,
                    **refresh_kwargs,
                )
            )

        status = self._publish_rebind_locked(
            target,
            publisher,
            state=state,
            failpoint_prefix="data_refresh",
            retry_exception=(DataRefreshNotSafeError, DataRefreshSupersededError),
            retry_reasons={
                DataRefreshNotSafeError: "active_attempt",
                DataRefreshSupersededError: "data_revision_superseded",
            },
        )
        if status.phase in {"plan_rebind_pending", "data_refresh_pending"}:
            return "blocked", status
        generation_id = published[-1].generation_id if published else status.generation_id
        self._legacy_failpoint("data_refresh_before_applied")
        applied = store.mark_pending_data_refresh_applied(
            pending.intent_hash,
            generation_id=generation_id,
        )
        # If a newer D admission replaced the canonical pointer while this
        # generation was being rebound, preserve it for the next safe
        # boundary.  The older G must return to ordinary Planner/Product work
        # before another generation can supersede it.
        if applied is not None and applied.intent_hash != pending.intent_hash and applied.state == "pending":
            # The successor may have been admitted against the old parent or
            # against this newly authoritative G.  Rebind only when its
            # parent hashes still name the old generation; its D/plan/reopened
            # bytes remain unchanged and the canonical pending file remains
            # the scheduler source of truth.
            successor_lifecycle = RunLifecycle.load(self.context)
            successor_plan_hash = _sha256_bytes(successor_lifecycle.plan_path.read_bytes())
            if (
                applied.expected_parent_generation_id != successor_lifecycle.generation_id
                or applied.expected_parent_state_hash != successor_lifecycle.snapshot.manifest_hash
                or applied.expected_parent_plan_hash != successor_plan_hash
            ):
                store.rebase_pending_data_refresh(
                    applied.intent_hash,
                    expected_parent_generation_id=successor_lifecycle.generation_id,
                    expected_parent_state_hash=successor_lifecycle.snapshot.manifest_hash,
                    expected_parent_plan_hash=successor_plan_hash,
                )
            return "applied", self._status_from_state(state)
        return "applied", self._status_from_state(state)

    def consume_pending_data_refresh(self) -> CoordinatorStatus:
        """Synchronously consume a canonical admission when no action is active."""

        self._ensure_persisted_configuration()
        if self._legacy_pending:
            return self._legacy_pending_status()
        from .data_revisions import DataRevisionStore

        canonical_pending = DataRevisionStore(self.context).pending_data_refresh(allow_stale=True)
        pending_rebind = self._pending_rebind_status()
        # A refresh may have published G-N before the coordinator spec/state
        # commit. In that crash window the normal pending-rebind projection is
        # expected; let the canonical admission drive the exact retry instead
        # of returning the waiting status forever.
        if pending_rebind is not None and canonical_pending is None:
            return pending_rebind
        with self._locked(create=False):
            state, _ = self._read_replay()
            assert state is not None
            _outcome, status = self._consume_pending_data_refresh_locked(state)
            return status

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
        # Validate ownership before deduplication so a malformed duplicate
        # cannot hide a known action/role mismatch.  Unknown action names are
        # allowed only when their role still has an explicit production route.
        validated = tuple(
            self._strip_untrusted_replacement_authorization(validate_role_action_contract(item))
            for item in supplied
        )
        # An intentional Product regeneration is an exclusive workflow.  The
        # Requirement Planner already projects this boundary, but enforce it
        # again at the coordinator seam so a stale/mixed provider snapshot
        # cannot dispatch AO, integration, ontology, reporting, or other
        # non-Product work while the target revision is pending.  The filter
        # is removed automatically once the request reaches accepted/failed.
        regeneration = state.get("product_regeneration")
        if (
            isinstance(regeneration, Mapping)
            and regeneration.get("authorization_origin") == PRODUCT_REGENERATION_ORIGIN
            and regeneration.get("status") in {"requested", "dispatched"}
        ):
            validated = tuple(
                action
                for action in validated
                if action.role.strip().lower() in {"product_agent", "product_reviewer"}
                and action.action.strip().lower() in {"build_product_candidate", "review_final_product"}
            )
        return self._dedupe(validated)

    @staticmethod
    def _strip_untrusted_replacement_authorization(action: PlannerAction) -> PlannerAction:
        """Remove transport authority supplied by an untrusted Planner.

        ``allow_session_replacement`` is coordinator-issued state, not a
        Planner semantic. Keeping the field on a raw Planner offer would let
        an ordinary retry bypass the role-session replacement boundary without
        an explicit ``reopen``. Existing action metadata remains untouched.
        """

        metadata = action.metadata if isinstance(action.metadata, Mapping) else {}
        if "allow_session_replacement" not in metadata and "session_replacement" not in metadata:
            return action
        clean = dict(metadata)
        clean.pop("allow_session_replacement", None)
        clean.pop("session_replacement", None)
        return PlannerAction(
            action=action.action,
            role=action.role,
            subject_id=action.subject_id,
            reason=action.reason,
            priority=action.priority,
            metadata=clean,
        )

    @staticmethod
    def _replacement_authorization_for_action(
        state: Mapping[str, Any],
        action: PlannerAction,
    ) -> tuple[str, Mapping[str, Any]] | None:
        """Return one durable reopen authorization matching *action*.

        Matching uses the same logical-owner key as the role-session registry,
        plus role/subject identity. The generation is retained as lineage
        evidence in the authorization record; role-session admission performs
        the final generation compare-and-set.
        """

        generation_id = state.get("generation_id")
        run_id = state.get("run_id")
        if not isinstance(generation_id, str) or not generation_id.strip():
            return None
        if not isinstance(run_id, str) or not run_id.strip():
            return None
        identity = _role_session_identity(
            action,
            run_id=run_id,
            generation_id=generation_id,
        )
        if identity is None:
            return None
        authorizations = state.get("replacement_authorizations")
        if not isinstance(authorizations, Mapping):
            return None
        value = authorizations.get(identity[0])
        if not isinstance(value, Mapping):
            return None
        if (
            value.get("role") != identity[1]
            or value.get("subject_id") != action.subject_id
            or value.get("generation_id") != generation_id
            or (
                "run_id" in value
                and value.get("run_id") != run_id
            )
        ):
            return None
        return identity[0], value

    @staticmethod
    def _replacement_authorizations_from_registry_locked(
        context: RunContext,
    ) -> dict[str, dict[str, str]]:
        """Project currently stale role sessions into reopen authorizations.

        The caller already holds the coordinator lock. Role-session writes use
        that same lock, so reading the validated registry document through its
        unlocked path is atomic without reacquiring the file lock (which would
        deadlock on POSIX). Only explicit ``replacement_required`` rows are
        eligible; no PID or action-text inference is used.
        """

        registry = RoleSessionRegistry(context)
        document = registry._read_unlocked()  # noqa: SLF001 - shared lock boundary
        sessions = document.get("sessions")
        if not isinstance(sessions, Mapping):
            return {}
        result: dict[str, dict[str, str]] = {}
        for key, entry in sessions.items():
            if not isinstance(key, str) or not isinstance(entry, Mapping):
                continue
            if entry.get("status") != "replacement_required":
                continue
            role = entry.get("role")
            subject_id = entry.get("subject_id")
            generation_id = entry.get("generation_id")
            if not all(
                isinstance(candidate, str) and candidate.strip()
                for candidate in (role, subject_id, generation_id)
            ):
                continue
            result[key] = {
                "role": role.strip().lower(),
                "subject_id": subject_id.strip(),
                "generation_id": generation_id.strip(),
                "run_id": context.run_id,
            }
        return result

    def _product_replacement_binding_locked(
        self,
        state: Mapping[str, Any],
        action: PlannerAction,
    ) -> dict[str, str] | None:
        """Return the current product input/implementation binding.

        Explicit reopen is a one-shot transport authorization, not a generic
        retry reset.  Bind the stale final candidate offer to the current
        accepted-input fingerprint, coordinator specification (which carries
        the production skill identity), and the exact Planner action/state
        projection before allowing it to consume that authorization.
        """

        metadata = action.metadata if isinstance(action.metadata, Mapping) else {}
        input_fingerprint = metadata.get("input_fingerprint")
        if not _is_sha256(input_fingerprint):
            return None
        implementation_identity = state.get("spec_hash")
        if not _is_sha256(implementation_identity):
            return None
        try:
            phase = self._phase_snapshot()
        except Exception:
            return None
        product = phase.get("product") if isinstance(phase, Mapping) else None
        expected_input = product.get("preview_input_fingerprint") if isinstance(product, Mapping) else None
        if not _is_sha256(expected_input) or expected_input != input_fingerprint:
            return None
        action_fingerprint = _action_key(action)
        if not _is_sha256(action_fingerprint):
            return None
        state_fingerprint = self._product_regeneration_state_fingerprint(state)
        if not _is_sha256(state_fingerprint):
            return None
        return {
            "run_id": str(state["run_id"]),
            "generation_id": str(state["generation_id"]),
            "input_fingerprint": input_fingerprint,
            "implementation_identity": implementation_identity,
            "action_fingerprint": action_fingerprint,
            "state_fingerprint": state_fingerprint,
        }

    def _product_regeneration_binding_locked(
        self,
        state: Mapping[str, Any],
        action: PlannerAction,
    ) -> dict[str, Any] | None:
        """Return the exact binding for an operator Product regeneration.

        The accepted-input and implementation checks are shared with the
        reviewed replacement boundary.  Prior candidate/review identities
        are added here so an operator request cannot silently drift to a
        different product while it waits for resume.
        """

        metadata = action.metadata if isinstance(action.metadata, Mapping) else {}
        if metadata.get("authorization_origin") != PRODUCT_REGENERATION_ORIGIN:
            return None
        request_id = metadata.get("product_regeneration_request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            return None
        revision_id = metadata.get("product_revision_id")
        if revision_id is None:
            request = state.get("product_regeneration")
            if isinstance(request, Mapping) and request.get("request_id") == request_id:
                revision_id = request.get("revision_id")
        if not isinstance(revision_id, str) or re.fullmatch(r"rev-\d{4,}", revision_id.strip()) is None:
            return None
        binding = self._product_replacement_binding_locked(state, action)
        if binding is None:
            return None
        try:
            from .product_review import ProductReviewStore

            product_store = ProductReviewStore(self.context, str(state.get("generation_id")))
            revision = product_store.load_revision(revision_id.strip())
        except Exception:
            return None
        if (
            revision.request_id != request_id.strip()
            or revision.status not in {"pending", "candidate", "reviewed"}
            or revision.input_fingerprint != binding.get("input_fingerprint")
            or revision.implementation_identity != binding.get("implementation_identity")
        ):
            return None
        expected_output_root = revision.output_root_ref
        supplied_output_root = metadata.get("output_root_ref")
        if not isinstance(expected_output_root, str) or not expected_output_root.strip():
            return None
        if supplied_output_root != expected_output_root:
            return None
        request = state.get("product_regeneration")
        if not isinstance(request, Mapping):
            return None
        if metadata.get("product_revision_id") is not None and metadata.get("product_revision_id") != request.get("revision_id"):
            return None
        predecessor_product_review_ref = request.get("predecessor_product_review_ref")
        predecessor_product_review_hash = request.get("predecessor_product_review_hash")
        if (predecessor_product_review_ref is None) != (predecessor_product_review_hash is None):
            return None
        if predecessor_product_review_ref is not None:
            if (
                not isinstance(predecessor_product_review_ref, str)
                or not predecessor_product_review_ref.strip()
                or metadata.get("predecessor_product_review_ref") != predecessor_product_review_ref
            ):
                return None
            if not _is_sha256(predecessor_product_review_hash):
                return None
            if metadata.get("predecessor_product_review_hash") != predecessor_product_review_hash:
                return None
        durable_action_fingerprint = request.get("action_fingerprint")
        if not _is_sha256(durable_action_fingerprint):
            return None
        try:
            phase = self._phase_snapshot()
        except Exception:
            return None
        product = phase.get("product") if isinstance(phase, Mapping) else None
        if not isinstance(product, Mapping):
            return None
        candidate = product.get("candidate")
        review = product.get("review")
        prior_candidate_hash = candidate.get("candidate_hash") if isinstance(candidate, Mapping) else None
        prior_review_hash = review.get("review_hash") if isinstance(review, Mapping) else None
        for value in (prior_candidate_hash, prior_review_hash):
            if value is not None and not _is_sha256(value):
                return None
        # The request boundary, not a later Planner projection, owns the
        # action digest.  Planner decorations include this digest as
        # ``regeneration_action_fingerprint`` and are therefore inherently
        # self-referential; never hash that decorated projection or trust it
        # as a new authority.
        binding["action_fingerprint"] = str(durable_action_fingerprint)
        binding.update(
            {
                "authorization_origin": PRODUCT_REGENERATION_ORIGIN,
                "request_id": request_id.strip(),
                "revision_id": revision_id.strip(),
                "prior_revision_id": revision.prior_revision_id,
                "output_root_ref": expected_output_root,
                "prior_candidate_hash": prior_candidate_hash,
                "prior_review_hash": prior_review_hash,
            }
        )
        if predecessor_product_review_ref is not None:
            binding.update(
                {
                    "predecessor_product_review_ref": predecessor_product_review_ref,
                    "predecessor_product_review_hash": predecessor_product_review_hash,
                }
            )
        return binding

    def _product_regeneration_review_binding_locked(
        self,
        state: Mapping[str, Any],
        action: PlannerAction,
    ) -> bool:
        """Validate a Product Reviewer offer against the target revision.

        Reviewer metadata is an untrusted Planner projection.  The reviewer
        may inspect only the candidate produced for the currently pending
        operator regeneration request, with the exact source/input/spec and
        output namespace recorded by the Coordinator.  A malformed or stale
        offer is skipped before retry accounting, role-session admission, or
        any one-shot authorization can be consumed.
        """

        if action.action.strip().lower() != "review_final_product" or action.role.strip().lower() != "product_reviewer":
            return True
        metadata = action.metadata if isinstance(action.metadata, Mapping) else {}
        request = state.get("product_regeneration")
        # A Product Reviewer offer is ordinary advisory work when no
        # regeneration is active.  Once an intentional regeneration request
        # is pending/in-flight, however, *every* reviewer offer must carry the
        # exact request/revision/output binding.  Do not let a generic
        # ``review_final_product`` offer bypass the target revision by simply
        # omitting ``authorization_origin``.
        if not isinstance(request, Mapping) or request.get("status") not in {"requested", "dispatched"}:
            return True
        if metadata.get("authorization_origin") != PRODUCT_REGENERATION_ORIGIN:
            return False
        run_id = state.get("run_id")
        generation_id = state.get("generation_id")
        request_id = request.get("request_id")
        revision_id = request.get("revision_id")
        if (
            not isinstance(run_id, str)
            or not isinstance(generation_id, str)
            or not isinstance(request_id, str)
            or not isinstance(revision_id, str)
            or metadata.get("product_regeneration_request_id") != request_id
            or metadata.get("product_revision_id") != revision_id
            or metadata.get("generation_id") != generation_id
        ):
            return False
        if not re.fullmatch(r"rev-\d{4,}", revision_id):
            return False
        implementation_identity = state.get("spec_hash")
        if not _is_sha256(implementation_identity):
            return False
        if request.get("implementation_identity") != implementation_identity:
            return False
        for key in ("input_fingerprint", "implementation_identity", "regeneration_action_fingerprint", "regeneration_state_fingerprint"):
            value = metadata.get(key)
            if value is not None and not _is_sha256(value):
                return False
        if metadata.get("input_fingerprint") != request.get("input_fingerprint"):
            return False
        if metadata.get("implementation_identity") != implementation_identity:
            return False
        if metadata.get("regeneration_action_fingerprint") != request.get("action_fingerprint"):
            return False
        if metadata.get("regeneration_state_fingerprint") != request.get("state_fingerprint"):
            return False
        try:
            phase = self._phase_snapshot()
            product = phase.get("product") if isinstance(phase, Mapping) else None
            if not isinstance(product, Mapping) or product.get("preview_input_fingerprint") != request.get("input_fingerprint"):
                return False
            from .product_review import ProductReviewStore

            store = ProductReviewStore(self.context, generation_id)
            revision = store.load_revision(revision_id)
            candidate = store.load_revision_candidate(revision_id)
        except Exception:
            return False
        if (
            revision.request_id != request_id
            or revision.status != "candidate"
            or revision.input_fingerprint != request.get("input_fingerprint")
            or revision.implementation_identity != implementation_identity
            or not isinstance(revision.output_root_ref, str)
            or metadata.get("output_root_ref") != revision.output_root_ref
            or metadata.get("candidate_ref") != revision.candidate_ref
            or metadata.get("candidate_hash") != revision.candidate_hash
            or metadata.get("review_ref") != f"products/generations/{generation_id}/product_revisions/{revision_id}/product_review.json"
        ):
            return False
        if not _is_sha256(revision.candidate_hash) or candidate.computed_hash != revision.candidate_hash:
            return False
        # The request's initial action/state fingerprints are the authoritative
        # operator binding.  Keep them visible in the reviewer metadata but do
        # not allow an agent-authored replacement hash to become authority.
        return True

    def _authorize_preview_replacement_for_final_locked(
        self,
        state: dict[str, Any],
        action: PlannerAction,
    ) -> bool:
        """Authorize one Product Agent replacement at a reviewed product boundary.

        Incremental preview and final candidate construction intentionally
        share one run/generation Product Agent owner.  A preview transport
        failure may therefore leave that owner ``replacement_required`` while
        the requirement facts continue to advance.  Authorize exactly one
        fresh root when the current action is either an all-terminal final
        candidate after a preview-only stale row, or a Product Reviewer
        ``repair_once``/``blocked_rethink`` candidate rebuild after an
        orphaned prior build.  The ordinary one-shot
        ``replacement_authorizations`` consumption below carries the
        coordinator-issued flag to the adapter and preserves the existing
        explicit-reopen boundary for every other role/action.
        """

        if action.action.strip().lower() != "build_product_candidate":
            return False
        if action.role.strip().lower() != "product_agent":
            return False
        run_id = state.get("run_id")
        generation_id = state.get("generation_id")
        if not isinstance(run_id, str) or not run_id.strip() or not isinstance(generation_id, str) or not generation_id.strip():
            return False
        identity = _role_session_identity(
            action,
            run_id=run_id,
            generation_id=generation_id,
        )
        if identity is None:
            return False
        metadata = action.metadata if isinstance(action.metadata, Mapping) else {}
        operator_regeneration = (
            metadata.get("authorization_origin") == PRODUCT_REGENERATION_ORIGIN
            and isinstance(metadata.get("product_regeneration_request_id"), str)
            and bool(str(metadata.get("product_regeneration_request_id")).strip())
        )

        authorizations = state.get("replacement_authorizations")
        if not isinstance(authorizations, dict):
            authorizations = {}
            state["replacement_authorizations"] = authorizations
        existing_authorization = authorizations.get(identity[0])
        existing_authorization_matches = (
            isinstance(existing_authorization, Mapping)
            and existing_authorization.get("role") == identity[1]
            and existing_authorization.get("subject_id") == action.subject_id
            and existing_authorization.get("generation_id") == generation_id
        )
        if isinstance(existing_authorization, Mapping) and not existing_authorization_matches:
            return False
        reconstructed_authorization = False

        try:
            registry = RoleSessionRegistry(self.context)
            # The caller already owns the coordinator lock.  Role-session
            # writes use this same lock, so the unlocked read is atomic here
            # and avoids a POSIX lock reacquisition deadlock.
            document = registry._read_unlocked()  # noqa: SLF001 - shared lock boundary
        except (CoordinatorError, OSError, TypeError, ValueError):
            return False
        sessions = document.get("sessions")
        entry = sessions.get(identity[0]) if isinstance(sessions, Mapping) else None
        if not isinstance(entry, Mapping):
            # An explicit regeneration may bootstrap the first Product Agent
            # owner.  No stale registry row is fabricated for that case; the
            # bound coordinator authorization is still consumed exactly once.
            if operator_regeneration and isinstance(existing_authorization, Mapping):
                current_binding = self._product_regeneration_binding_locked(state, action)
                if current_binding is None:
                    return False
                for field_name, expected in current_binding.items():
                    if existing_authorization.get(field_name) != expected:
                        return False
                return True
            return False
        if (
            entry.get("logical_owner") != identity[0]
            or entry.get("role") != identity[1]
            or entry.get("subject_id") != action.subject_id
            or entry.get("run_id") != run_id
            or entry.get("generation_id") != generation_id
            or entry.get("status") != "replacement_required"
            or entry.get("replacement_required") is not True
        ):
            return False
        lineage = entry.get("action_lineage")
        if not isinstance(lineage, list):
            return False
        if operator_regeneration:
            if not isinstance(existing_authorization, Mapping):
                # The initial Product regeneration dispatch consumes its
                # one-shot authorization before transport runs.  If that
                # transport reports ``replacement_required``, the exact same
                # request is still eligible for one fresh root on its first
                # bounded retry.  Reconstruct the existing authorization
                # shape from the durable request/binding only; the retry
                # namespace and dispatched status keep this from becoming a
                # generic retry reset or a second replacement loop.
                request = state.get("product_regeneration")
                retry_counts = state.get("retry_counts")
                retry_fingerprint = self._retry_fingerprint(state, action)
                retry_count = (
                    retry_counts.get(retry_fingerprint)
                    if isinstance(retry_counts, Mapping)
                    else None
                )
                if (
                    not isinstance(request, Mapping)
                    or request.get("status") != "dispatched"
                    or isinstance(retry_count, bool)
                    or not isinstance(retry_count, int)
                    or retry_count != 1
                ):
                    return False
                current_binding = self._product_regeneration_binding_locked(state, action)
                if current_binding is None:
                    return False
                existing_authorization = {
                    "role": identity[1],
                    "subject_id": action.subject_id,
                    **current_binding,
                }
                authorizations[identity[0]] = dict(existing_authorization)
                reconstructed_authorization = True
            regeneration_audit = any(
                isinstance(row, Mapping)
                and row.get("event") == "product_regeneration_requested"
                and row.get("idempotency_key") == metadata.get("product_regeneration_request_id")
                for row in lineage if isinstance(row, Mapping)
            )
            # The intentional registry boundary records its origin in the
            # audit channel and leaves stale_reason null.  Older explicit
            # regeneration rows may still carry the truthful origin there;
            # accept either representation while never inferring a stale
            # transport diagnosis.
            audit_entries = entry.get("audit") if isinstance(entry.get("audit"), list) else []
            regeneration_audit = regeneration_audit or any(
                isinstance(row, Mapping)
                and row.get("event") == "product_regeneration_requested"
                and row.get("idempotency_key") == metadata.get("product_regeneration_request_id")
                for row in audit_entries
            )
            if (
                existing_authorization.get("authorization_origin") != PRODUCT_REGENERATION_ORIGIN
                or existing_authorization.get("request_id")
                != metadata.get("product_regeneration_request_id")
                or (
                    not reconstructed_authorization
                    and entry.get("last_idempotency_key")
                    != metadata.get("product_regeneration_request_id")
                )
                or (not reconstructed_authorization and not regeneration_audit)
            ):
                return False
            current_binding = self._product_regeneration_binding_locked(state, action)
            if current_binding is None:
                return False
            for field_name, expected in current_binding.items():
                if existing_authorization.get(field_name) != expected:
                    return False
            return True
        last_action = entry.get("last_action")
        reviewed_metadata = metadata.get("review_verdict") in {"repair_once", "blocked_rethink"}
        preview_only = (
            last_action == "refresh_product_preview"
            and any(
                isinstance(row, Mapping) and row.get("action") == "refresh_product_preview"
                for row in lineage
            )
            and not any(
                isinstance(row, Mapping)
                and row.get("action") in {"build_product_candidate", "build_final_product", "publish_final_product"}
                for row in lineage
            )
        )
        reviewed_repair = (
            last_action == "build_product_candidate"
            and reviewed_metadata
            and any(
                isinstance(row, Mapping) and row.get("action") == "build_product_candidate"
                for row in lineage
            )
        )
        # A public reopen also authorizes one fresh Product Agent attempt when
        # the previous final candidate itself is stale/invalid.  In that
        # boundary Planner metadata intentionally has no review verdict: the
        # product review cannot be trusted until the stale artifact binding is
        # rebuilt.  Require an already-persisted owner authorization and bind
        # it to the current accepted input, implementation/spec, action, and
        # state identity before admitting the replacement.
        explicit_final_reopen = (
            existing_authorization_matches
            and last_action == "build_product_candidate"
            and any(
                isinstance(row, Mapping) and row.get("action") == "build_product_candidate"
                for row in lineage
            )
            and not reviewed_metadata
        )
        if not preview_only and not reviewed_repair and not explicit_final_reopen:
            return False
        if explicit_final_reopen:
            current_binding = self._product_replacement_binding_locked(state, action)
            if current_binding is None:
                return False
            # If this authorization was already enriched before a crash or a
            # competing admission, every bound field must match exactly.  A
            # missing field is filled once from the current program-owned
            # projection; no Planner-supplied replacement authority is used.
            if isinstance(existing_authorization, dict):
                for field_name, expected in current_binding.items():
                    supplied = existing_authorization.get(field_name)
                    if supplied is not None and supplied != expected:
                        return False
                    existing_authorization[field_name] = expected
        if reviewed_metadata:
            # The action metadata is advisory.  Re-read the hash-bound review
            # and candidate before issuing coordinator replacement authority;
            # an arbitrary Planner payload cannot self-authorize a new Product
            # Agent root.
            try:
                from .product_review import ProductReviewStore

                store = ProductReviewStore(self.context, generation_id)
                candidate = store.load_candidate()
                review = store.load_review()
            except Exception:
                return False
            if review.verdict != metadata.get("review_verdict"):
                return False
            candidate_hash = metadata.get("candidate_hash")
            if not _is_sha256(candidate_hash) or candidate_hash != candidate.computed_hash:
                return False
        if existing_authorization_matches:
            return True
        authorizations[identity[0]] = {
            "role": identity[1],
            "subject_id": action.subject_id,
            "generation_id": generation_id,
        }
        return True

    @staticmethod
    def _consume_replacement_authorization(
        state: dict[str, Any],
        action: PlannerAction,
    ) -> tuple[PlannerAction, Mapping[str, Any]] | None:
        """Consume and mark a matching reopen authorization exactly once."""

        matched = RunCoordinator._replacement_authorization_for_action(state, action)
        if matched is None:
            return None
        logical_owner, binding = matched
        authorizations = state.get("replacement_authorizations")
        if not isinstance(authorizations, dict):
            return None
        clean_metadata = dict(action.metadata) if isinstance(action.metadata, Mapping) else {}
        clean_metadata["allow_session_replacement"] = True
        if binding.get("authorization_origin") == PRODUCT_REGENERATION_ORIGIN:
            # The durable authorization stores the typed revision as
            # ``revision_id``; Planner/role transport metadata uses the
            # explicit ``product_revision_id`` name.  Preserve the binding
            # without exposing or inventing any other revision authority.
            if "revision_id" in binding:
                clean_metadata["product_revision_id"] = binding.get("revision_id")
            if "prior_revision_id" in binding:
                clean_metadata["prior_revision_id"] = binding.get("prior_revision_id")
            if "output_root_ref" in binding:
                clean_metadata["output_root_ref"] = binding.get("output_root_ref")
        authorized = PlannerAction(
            action=action.action,
            role=action.role,
            subject_id=action.subject_id,
            reason=action.reason,
            priority=action.priority,
            metadata=clean_metadata,
        )
        consumed = dict(binding)
        consumed["logical_owner"] = logical_owner
        # Remove the one-shot authorization from the in-memory state only
        # after the bound transport action has been constructed.  The caller
        # persists this mutation together with the authoritative
        # ``dispatch_started`` event; if anything fails before that event is
        # appended, the next reconciliation rereads the checkpoint and the
        # still-pending authorization remains available for safe replay.
        authorizations.pop(logical_owner, None)
        return authorized, consumed

    def _phase_snapshot(self) -> Mapping[str, Any]:
        from .requirement_planning import RequirementSupervisorWorkspace

        return RequirementSupervisorWorkspace(self.context).phase_snapshot()

    def _reconcile_product_regeneration_locked(self, state: dict[str, Any]) -> None:
        """Converge a target revision after Product Agent/Reviewer exit.

        Revision files are the ProductReviewStore authority.  This small
        coordinator projection only advances the request marker after an
        accepted review (activating the immutable pointer) or records a
        failed target while leaving the prior pointer untouched.
        """

        request = state.get("product_regeneration")
        if not isinstance(request, Mapping) or request.get("status") not in {"requested", "dispatched"}:
            return
        revision_id = request.get("revision_id")
        generation_id = request.get("generation_id")
        if not isinstance(revision_id, str) or not isinstance(generation_id, str):
            return
        try:
            from .product_review import ProductReviewStore

            store = ProductReviewStore(self.context, generation_id)
            revision = store.load_revision(revision_id)
            if revision.status == "reviewed":
                review = store.load_revision_review(revision_id)
                if review.verdict in {"accept", "accept_with_limits"}:
                    store.activate_revision(revision_id)
                    revision = store.load_revision(revision_id)
                else:
                    revision = store.fail_revision(revision_id)
            elif revision.status in {"activation_pending", "accepted"}:
                # Activation is a two-file transaction: a crash can leave a
                # target pointer written while the target state still says
                # ``activation_pending`` (or leave an older implementation's
                # accepted state just before its pointer CAS).  Reconcile the
                # exact revision under the ProductReviewStore lock before
                # projecting the coordinator request terminal.  Never mark
                # the regeneration accepted merely because the target JSON
                # says accepted; the authoritative pointer must be current.
                pointer = store.reconcile_revision_activation(revision_id)
                if pointer.revision_id != revision_id or pointer.status != "accepted":
                    return
                revision = store.load_revision(revision_id)
            if revision.status not in {"accepted", "failed"}:
                return
            try:
                pointer = store.read_active_revision()
            except Exception:
                return
            if revision.status == "accepted" and (
                pointer is None
                or pointer.revision_id != revision_id
                or pointer.status != "accepted"
                or pointer.revision_hash != revision.computed_hash
            ):
                # A target accepted state without an exact current pointer is
                # not a completed Product regeneration.  Leave the request
                # pending for the next replay/reconcile pass.
                return
        except Exception:
            # Product action admission/validation remains fail-closed.  A
            # malformed target is left pending for an explicit operator
            # diagnostic rather than silently advancing the request marker.
            return
        terminal_status = "accepted" if revision.status == "accepted" else "failed"
        if request.get("status") == terminal_status:
            return
        state["product_regeneration"] = {
            **dict(_canonical(request)),
            "status": terminal_status,
            "revision_id": revision.revision_id,
        }
        state["status"] = "ready" if terminal_status == "accepted" else "waiting"
        state["phase"] = "product_regeneration_complete" if terminal_status == "accepted" else "product_regeneration_failed"
        self._append_event_locked(
            state,
            "product_regeneration_completed" if terminal_status == "accepted" else "product_regeneration_failed",
            {
                "authorization_origin": PRODUCT_REGENERATION_ORIGIN,
                "request_id": request.get("request_id"),
                "revision_id": revision.revision_id,
                "status": terminal_status,
            },
        )

    def _authoritative_state_fingerprint(
        self,
        state: Mapping[str, Any],
        action: PlannerAction | None = None,
    ) -> str:
        """Hash the program-derived durable state used for Planner admission.

        Retry exhaustion is meaningful only for the same action against the
        same durable world.  The pure Planner phase snapshot is the strongest
        current projection of item/lifecycle/integration/product/report state;
        when a minimal coordinator fixture has no lifecycle yet, fall back to
        the persisted coordinator state after removing transport bookkeeping.
        Every scoped projection is additionally bound to the persisted
        coordinator specification hash.  A same-lineage transport rebind
        therefore invalidates retry evidence for the affected action without
        resetting unrelated action histories; an idempotent rebind keeps the
        same hash and preserves the existing budget.
        Requirement-local actions use only their bound item projection so
        unrelated requirement progress cannot reset their retry budget.  This
        is an evidence hash, not a new semantic acceptance rule.
        """

        # ``spec_hash`` is the event-chain's exact persisted CoordinatorRunSpec
        # identity (including the Codex transport/bound skill fields). Keep
        # the raw value in the binding so malformed state remains fail-closed
        # elsewhere while any persisted identity change still invalidates the
        # prior retry evidence deterministically.
        spec_identity = _canonical(state.get("spec_hash"))
        try:
            phase = self._phase_snapshot()
        except Exception:
            phase = None
        phase_validation = phase.get("lifecycle_validation") if isinstance(phase, Mapping) else None
        if isinstance(phase, Mapping) and not (
            isinstance(phase_validation, Mapping) and phase_validation.get("valid") is False
        ):
            if action is not None:
                identity_fingerprint = self._identity_domain_fingerprint(phase, action)
                if identity_fingerprint is not None:
                    return _sha256_value(
                        {
                            "spec_identity": spec_identity,
                            "identity_domain": identity_fingerprint,
                        }
                    )
                try:
                    item_id = self._requirement_item_id(action)
                except Exception:
                    # A missing/malformed lifecycle is handled by the
                    # existing coordinator-state fallback below; fixtures
                    # without lifecycle retain global retry semantics.
                    item_id = None
                items = phase.get("items")
                item = items.get(item_id) if isinstance(items, Mapping) and item_id is not None else None
                if isinstance(item, Mapping):
                    return _sha256_value(
                        {
                            "spec_identity": spec_identity,
                            "phase_snapshot_item": {
                                "item_id": item_id,
                                "phase": _canonical(item),
                            }
                        }
                    )
            return _sha256_value(
                {
                    "spec_identity": spec_identity,
                    "phase_snapshot": _canonical(phase),
                }
            )
        volatile = {
            "status",
            "phase",
            "attempt",
            "last_action",
            "last_no_progress_action",
            "no_progress_count",
            "retry_counts",
            "retry_blocked",
            "retry_state_fingerprints",
            "dispatch_state_fingerprints",
            "replacement_authorizations",
            "product_regeneration",
            "active_dispatches",
            "last_control_fingerprint",
            "last_event_seq",
            "last_event_hash",
            "diagnostics",
            "spec_hash",
        }
        durable = {
            str(key): _canonical(value)
            for key, value in state.items()
            if key not in volatile
        }
        return _sha256_value(
            {
                "spec_identity": spec_identity,
                "coordinator_durable_state": durable,
            }
        )

    def _product_regeneration_state_fingerprint(
        self,
        state: Mapping[str, Any],
    ) -> str:
        """Hash the stable context for one Product regeneration request.

        A regeneration request records its authorization before the request
        and replacement-authority fields exist in the coordinator state.  A
        later dispatch therefore must not hash those self-referential fields
        back into the binding.  Likewise, the active Product revision
        projection can change from legacy root paths to the adopted rev-0001
        namespace while the accepted business inputs remain identical.  Keep
        the context bound to all unrelated durable coordinator/lifecycle
        state and accepted item projections, while excluding only transport
        bookkeeping and Product request/authorization projections that this
        operation itself creates.
        """

        spec_identity = _canonical(state.get("spec_hash"))
        volatile = {
            "status",
            "phase",
            "attempt",
            "last_action",
            "last_no_progress_action",
            "no_progress_count",
            "retry_counts",
            "retry_blocked",
            "retry_state_fingerprints",
            "dispatch_state_fingerprints",
            "replacement_authorizations",
            "product_regeneration",
            "active_dispatches",
            "last_control_fingerprint",
            "last_event_seq",
            "last_event_hash",
            "diagnostics",
            # ``regenerate_product`` clears this publication projection while
            # it records the pending request.  It is derived from the active
            # Product revision and therefore must not make the request's
            # pre-write context differ from the dispatch-time context.
            "publication_ready",
            "spec_hash",
        }
        durable = {
            str(key): _canonical(value)
            for key, value in state.items()
            if key not in volatile
        }

        try:
            phase = self._phase_snapshot()
        except Exception:
            phase = None
        if isinstance(phase, Mapping):
            # Requirement/lifecycle projections are the authoritative
            # accepted-input context.  Product output/revision paths are
            # intentionally omitted because revision adoption and request
            # creation may change those derived pointers without changing
            # the accepted source boundary (which is bound separately by
            # input_fingerprint and prior candidate/review hashes).
            phase_projection: dict[str, Any] = {
                str(key): _canonical(value)
                for key, value in phase.items()
                # Lifecycle scheduling state may advance from ``paused`` to
                # ``integration_complete`` while this request waits for its
                # Product Agent dispatch.  That operational transition does
                # not change the accepted input boundary; keep the durable
                # validation/items projections below bound while excluding
                # only this volatile top-level label.
                if key not in {"product", "lifecycle_state"}
            }
            product = phase.get("product")
            if isinstance(product, Mapping):
                phase_projection["product"] = {
                    str(key): _canonical(product.get(key))
                    for key in (
                        "preview_input_fingerprint",
                        "preview_item_ids",
                        "preview_item_bindings",
                        "preview_failed_items",
                        "preview_limitations",
                        "presentation_inventory_ref",
                        "presentation_plan_ref",
                    )
                    if key in product
                }
        else:
            phase_projection = None
        return _sha256_value(
            {
                "spec_identity": spec_identity,
                "coordinator_durable_state": durable,
                "phase_snapshot": phase_projection,
            }
        )

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
            # ``ResolutionCapacity`` is the persisted admission authority;
            # the user-provided worker bound is only an upper bound.  Keep a
            # minimum of one for the executor implementation, while launch
            # admission prevents entries when total_active is zero.
            capacity = self._resolution_capacity()
            worker_limit = min(self.max_workers, max(1, int(capacity.total_active)))
            self._executor = ThreadPoolExecutor(
                max_workers=worker_limit,
                thread_name_prefix="auto-foundry-role",
            )
        return self._executor

    @staticmethod
    def _integration_identity(item_workspace: Any) -> tuple[str, str]:
        """Use IntegrationSession's validated snapshot/recovery authority."""
        from .integration import IntegrationSession
        try:
            return IntegrationSession.persisted_identity(item_workspace)
        except (OSError, ValueError, TypeError) as exc:
            raise CoordinatorIntegrityError(str(exc)) from exc

    def _requirement_item_id(self, action: PlannerAction) -> str | None:
        """Resolve an action's bound requirement without trusting free text."""

        name = action.action.strip().lower()
        metadata = action.metadata if isinstance(action.metadata, Mapping) else {}
        candidate = metadata.get("item_id")
        if not isinstance(candidate, str) or not candidate.strip():
            candidate = action.subject_id
        # A lifecycle read is intentionally the ownership boundary.  A
        # planner/control subject such as a run id or identity domain must not
        # be mistaken for a requirement and terminalized accidentally.
        try:
            from .lifecycle import RunLifecycle

            lifecycle = RunLifecycle.load(self.context)
        except FileNotFoundError:
            return None
        item_ids = set(lifecycle.item_ids)
        if not isinstance(candidate, str) or candidate not in item_ids:
            return None
        requirement_actions = _REQUIREMENT_MUTATING_ACTIONS | {"specialist"}
        requirement_roles = _REQUIREMENT_MUTATING_ROLES | {"specialist"}
        if name not in requirement_actions and action.role.strip().lower() not in requirement_roles:
            return None
        return candidate

    def _identity_domain_fingerprint(
        self,
        phase: Mapping[str, Any],
        action: PlannerAction,
    ) -> str | None:
        """Hash one identity domain and its local requester projections.

        Identity actions target a shared run-level domain rather than carrying
        a requirement ``item_id``.  Bind retry evidence to the validated
        domain state plus every requester that belongs to this lifecycle.  A
        missing, foreign-only, or malformed binding returns ``None`` so the
        existing global/fail-closed fallback remains authoritative.
        """

        if action.action.strip().lower() not in _IDENTITY_EXECUTABLE_ACTIONS:
            return None
        try:
            from .entity_resolution import EntityResolutionWorkspace

            domain = EntityResolutionWorkspace.load(self.context).get_domain(action.subject_id)
        except Exception:
            # An unavailable or malformed identity registry cannot establish a
            # safe item-local boundary.  Exhaustion routing will still fail
            # closed through the existing domain terminalization path.
            return None
        domain_value = domain.to_dict()
        raw_requesters: list[str] = []
        for value in domain_value.get("requested_by", ()):
            if isinstance(value, str) and value.strip():
                raw_requesters.append(value)
        discovered = domain_value.get("discovered_by_item_id")
        if isinstance(discovered, str) and discovered.strip():
            raw_requesters.append(discovered)
        for request in domain_value.get("requests", ()):
            if isinstance(request, Mapping):
                item_id = request.get("item_id")
                if isinstance(item_id, str) and item_id.strip():
                    raw_requesters.append(item_id)
        requester_ids = tuple(dict.fromkeys(raw_requesters))
        if not requester_ids:
            return None
        items = phase.get("items") if isinstance(phase, Mapping) else None
        if not isinstance(items, Mapping):
            return None
        # ``phase`` is the lifecycle authority used by ordinary requirement
        # fingerprints.  Every bound requester must have a validated item
        # projection; foreign IDs are retained only as domain identity and do
        # not grant access to another run's item data.
        bound_requesters = tuple(item_id for item_id in requester_ids if item_id in items)
        if not bound_requesters:
            return None
        requester_projection: list[dict[str, Any]] = []
        for item_id in bound_requesters:
            item = items.get(item_id)
            if not isinstance(item, Mapping):
                return None
            requester_projection.append(
                {
                    "item_id": item_id,
                    "phase": _canonical(item),
                }
            )
        return _sha256_value(
            {
                "identity_domain": _canonical(domain_value),
                "requesters": requester_projection,
            }
        )

    def _terminalize_exhausted_requirement_locked(
        self,
        state: Mapping[str, Any],
        action: PlannerAction,
        execution: RoleExecution | None = None,
    ) -> bool:
        """Publish one item-local terminal failure at retry exhaustion.

        This helper runs under the coordinator lock but uses each durable
        item's own transition/session lock.  It deliberately never mutates a
        different requirement or the run lifecycle directly; Planner's next
        fresh read performs dependency release and run-level advancement.
        """

        item_id = self._requirement_item_id(action)
        if item_id is None:
            return False
        from .durable import ItemWorkspace

        workspace = ItemWorkspace.load(self.context, item_id, mode="requirement")
        item_state = workspace.state
        # A concurrent completion may have terminalized the item while the
        # transport result was in flight.  Treat that as settled and do not
        # rewrite accepted bytes or integration state.
        terminal = item_state.get("terminal_outcome")
        if isinstance(terminal, Mapping):
            integration_state = item_state.get("integration_state")
            if integration_state == "pending" and terminal.get("outcome") in {"accepted", "accepted_with_limits"}:
                # Integration actions retain the accepted business output and
                # settle only the downstream boundary after exhaustion.
                pass
            else:
                return False
        name = action.action.strip().lower()
        reason = f"{name} recovery exhausted"
        integration_actions = {
            "integrate_requirement",
            "repair_integration_fidelity",
            "review_integration_fidelity",
            "commit_integration_requirement",
        }
        if name in integration_actions and isinstance(terminal, Mapping):
            # Accepted integration failures use the explicit exhausted
            # boundary.  A valid open session is expected for normal action
            # retries; a missing/foreign session is a run-integrity defect and
            # therefore propagates fail-closed.
            from .integration import IntegrationSession
            from .prepared import PreparedAssetRegistry

            integration_root = workspace.item_root / "integration"
            identity_paths = tuple(integration_root / relative for relative in (
                "staging/session.json", "staging/snapshot.json", "committed/manifest.json"))
            never_started = not any(path.exists() or path.is_symlink() for path in identity_paths)
            if never_started and name == "integrate_requirement":
                # A failed transport may not have created a session. Create an
                # empty mechanical failure boundary, not synthetic integration.
                session = IntegrationSession.create(
                    self.context, workspace, PreparedAssetRegistry(self.context),
                    "coordinator-recovery", invocation_id=f"exhausted-{item_id}",
                )
            else:
                owner_id, invocation_id = self._integration_identity(workspace)
                session = IntegrationSession.load(
                    self.context, workspace, PreparedAssetRegistry(self.context), owner_id, invocation_id,
                )
            try:
                session.finalize_technical_failure(reason)
            finally:
                session.release()
        else:
            # Pre-acceptance failures are terminal item outcomes.  The durable
            # transition closes any active attempt and records an idempotent,
            # privacy-safe run incident.
            workspace.technical_failure(reason, recovery_exhausted=True)
        return True

    def _terminalize_identity_requesters_locked(
        self,
        action: PlannerAction,
    ) -> bool:
        """Terminalize only active requirements bound to a failed domain."""

        from .entity_resolution import EntityResolutionWorkspace
        from .durable import ItemWorkspace
        from .lifecycle import RunLifecycle

        resolution = EntityResolutionWorkspace.load(self.context)
        domain = resolution.get_domain(action.subject_id)
        lifecycle = RunLifecycle.load(self.context)
        item_ids = set(lifecycle.item_ids)
        requester_ids = tuple(
            dict.fromkeys(
                (
                    *getattr(domain, "requested_by", ()),
                    getattr(domain, "discovered_by_item_id", None),
                    *(
                        request.item_id
                        for request in getattr(domain, "requests", ())
                        if getattr(request, "item_id", None) is not None
                    ),
                )
            )
        )
        terminalized = False
        for item_id in requester_ids:
            if item_id not in item_ids:
                continue
            workspace = ItemWorkspace.load(self.context, item_id, mode="requirement")
            state = workspace.state
            if isinstance(state.get("terminal_outcome"), Mapping):
                # Accepted business output is immutable.  Integration
                # failures have their own explicit boundary and are handled by
                # the integration action, not by identity control routing.
                continue
            workspace.technical_failure(
                "identity resolution recovery exhausted",
                recovery_exhausted=True,
            )
            terminalized = True
        return terminalized

    def _terminalize_exhausted_product_regeneration_locked(
        self,
        state: Mapping[str, Any],
        action: PlannerAction,
    ) -> bool:
        """Fail one Product regeneration target after bounded retry exhaustion.

        Product regeneration is not a requirement item and therefore cannot
        be settled by the ordinary item terminalization helper.  Once the
        same bound Product Agent/Reviewer offer has reached the coordinator's
        retry budget, mark only its revision ``failed`` and keep the prior
        accepted pointer untouched.  A malformed or already-active target is
        left pending so the normal integrity boundary can diagnose it rather
        than silently advancing the request marker.
        """

        name = action.action.strip().lower()
        role = action.role.strip().lower()
        if name not in {"build_product_candidate", "review_final_product"} or role not in {
            "product_agent",
            "product_reviewer",
        }:
            return False
        if not isinstance(state, dict):
            return False
        metadata = action.metadata if isinstance(action.metadata, Mapping) else {}
        if metadata.get("authorization_origin") != PRODUCT_REGENERATION_ORIGIN:
            return False
        request = state.get("product_regeneration")
        if not isinstance(request, Mapping) or request.get("status") not in {"requested", "dispatched"}:
            return False
        request_id = metadata.get("product_regeneration_request_id")
        if not isinstance(request_id, str) or request_id.strip() != str(request.get("request_id") or "").strip():
            return False
        revision_id = request.get("revision_id")
        if not isinstance(revision_id, str) or re.fullmatch(r"rev-\d{4,}", revision_id.strip()) is None:
            return False
        try:
            from .product_review import ProductReviewStore

            store = ProductReviewStore(self.context, str(state.get("generation_id") or ""))
            revision = store.load_revision(revision_id.strip())
            if revision.status == "accepted":
                # An accepted target should have been reconciled before retry
                # accounting. Never demote an active accepted pointer here.
                return False
            if revision.status != "failed":
                revision = store.fail_revision(revision.revision_id)
            if revision.status != "failed":
                return False
        except Exception:
            # Keep the request and target auditable when the revision evidence
            # cannot be loaded or failed safely; callers will surface the
            # existing retry/exhaustion diagnostic for explicit repair.
            return False
        updated = {
            **dict(_canonical(request)),
            "status": "failed",
            "revision_id": revision.revision_id,
        }
        state["product_regeneration"] = updated
        state["status"] = "waiting"
        state["phase"] = "product_regeneration_failed"
        self._append_event_locked(
            state,
            "product_regeneration_failed",
            {
                "authorization_origin": PRODUCT_REGENERATION_ORIGIN,
                "request_id": request_id.strip(),
                "revision_id": revision.revision_id,
                "status": "failed",
                "reason": "recovery_exhausted",
                "action": action.to_dict(),
            },
        )
        return True

    def _terminalize_exhausted_action_locked(
        self,
        state: Mapping[str, Any],
        action: PlannerAction,
        execution: RoleExecution | None = None,
    ) -> bool:
        """Settle the requirement boundary for any exhausted action.

        Ordinary requirement actions carry an ``item_id`` and are handled by
        :meth:`_terminalize_exhausted_requirement_locked`.  Entity-resolution
        actions intentionally target a shared identity domain instead; their
        item-local boundary is the set of active requester requirements.  A
        foreign or unbound domain returns ``False`` so the existing
        fail-closed retry diagnostic remains authoritative.
        """

        if self._terminalize_exhausted_product_regeneration_locked(state, action):
            return True
        if self._terminalize_exhausted_requirement_locked(state, action, execution):
            return True
        if action.action.strip().lower() in _IDENTITY_EXECUTABLE_ACTIONS:
            return self._terminalize_identity_requesters_locked(action)
        return False

    def _publication_policy(self) -> dict[str, Any]:
        """Return the replay-bound publication policy for deterministic gates.

        The run specification is the immutable policy input and the
        coordinator state is its event-chain projection.  Require both to
        agree before a publication authorization can be written; a missing or
        malformed enabled flag is treated as a denied policy, never as an
        ambient/default route.
        """

        spec = self._spec
        if spec is None:
            spec = self._load_spec_document()
            self._spec = spec
        policy = dict(_canonical(spec.publication_policy))
        state_value = _load_json(self.state_path)
        if isinstance(state_value, Mapping) and "publication_policy" in state_value:
            state_policy = state_value.get("publication_policy")
            if not isinstance(state_policy, Mapping):
                raise CoordinatorIntegrityError("coordinator publication policy projection is invalid")
            if _json_bytes(state_policy) != _json_bytes(policy):
                raise CoordinatorIntegrityError("coordinator publication policy projection is stale")
        enabled = policy.get("enabled")
        if not isinstance(enabled, bool):
            raise CoordinatorIntegrityError("publication policy enabled flag must be boolean")
        return policy

    def _dispatch_deterministic(self, action: PlannerAction) -> RoleExecution | None:
        """Apply transitions whose semantic authorization is already durable.

        Returning a :class:`RoleExecution` for these action names is
        intentional even when a role runner is configured: no model transport
        is allowed to own the finalization or accepted integration commit.
        """

        name = action.action.strip().lower()
        role = action.role.strip().lower()
        if name in {"recover_final_report", "preflight_final_report", "finalize_final_report"}:
            if role != "reporting_agent":
                return RoleExecution(
                    exit_code=1,
                    error=f"{name} requires reporting_agent",
                )
            try:
                from .reporting import RunReportFinalizer, RunReportInputGatherer

                finalizer = RunReportFinalizer(self.context)
                # Report transaction recovery is a deterministic public
                # boundary.  It must happen before any gather/load operation
                # so a backup_moved process death cannot be mistaken for a
                # missing preflight and retried through a model role.
                recovered = finalizer.recover()
                if name == "recover_final_report":
                    return RoleExecution(
                        output=f"deterministic report recovery applied ({recovered.get('stage', 'unknown')})"
                    )
                if name == "preflight_final_report":
                    preflight = RunReportInputGatherer.gather_from_run(
                        self.context,
                        persist=True,
                    )
                    return RoleExecution(
                        output=f"deterministic report preflight persisted ({preflight.preflight_hash})"
                    )
                preflight = RunReportInputGatherer(self.context).load()
                receipt = finalizer.finalize(preflight)
                return RoleExecution(
                    output=f"deterministic report finalization applied ({receipt.get('receipt_hash', '')})"
                )
            except Exception as exc:
                return RoleExecution(exit_code=1, error=f"deterministic report transition failed: {exc}")

        if name == "publish_final_product":
            if role != "product_agent":
                return RoleExecution(
                    exit_code=1,
                    error="publish_final_product requires product_agent",
                )
            try:
                from .product_review import ProductReviewStore, canonical_hash

                policy = self._publication_policy()
                if policy.get("enabled") is not True:
                    return RoleExecution(
                        exit_code=1,
                        error="publication is disabled; awaiting explicit publication authorization",
                    )
                policy_hash = canonical_hash(policy)
                metadata = action.metadata if isinstance(action.metadata, Mapping) else {}
                supplied_policy_hash = metadata.get("publication_policy_hash")
                if supplied_policy_hash != policy_hash:
                    raise CoordinatorPublicationError(
                        "publication policy hash is missing or does not match coordinator policy"
                    )
                generation_id = metadata.get("generation_id")
                if not isinstance(generation_id, str) or not generation_id.strip():
                    raise CoordinatorPublicationError("publication action generation_id is missing")
                store = ProductReviewStore(self.context, generation_id)
                candidate = store.load_candidate()
                review = store.load_review()
                if metadata.get("candidate_hash") != candidate.computed_hash:
                    raise CoordinatorPublicationError("publication candidate hash is stale")
                if metadata.get("review_hash") != review.computed_hash:
                    raise CoordinatorPublicationError("publication review hash is stale")
                authorization = store.authorize_publish(
                    publisher_ref="coordinator",
                    publication_policy=policy,
                    publication_policy_hash=policy_hash,
                )
                return RoleExecution(
                    output=f"deterministic publication authorization applied ({authorization.authorization_hash})"
                )
            except Exception as exc:
                return RoleExecution(exit_code=1, error=f"deterministic publication authorization failed: {exc}")

        if name == "finalize_requirement_review":
            if role != "business_reviewer":
                return RoleExecution(
                    exit_code=1,
                    error="finalize_requirement_review requires business_reviewer",
                )
            try:
                from .durable import ItemWorkspace

                workspace = ItemWorkspace.load(self.context, action.subject_id, mode="requirement")
                state = workspace.state
                terminal = state.get("terminal_outcome")
                if isinstance(terminal, Mapping):
                    terminal_status = terminal.get("status", terminal.get("outcome"))
                    if terminal_status in {"accepted", "accepted_with_limits", "blocked_by_evidence"}:
                        return RoleExecution(output="deterministic requirement finalization already durable")
                    raise ValueError("requirement terminal outcome is not business-accepted")
                review = state.get("review")
                if not isinstance(review, Mapping):
                    raise ValueError("requirement review metadata is missing")
                review_status = review.get("status")
                verdict = review.get("verdict")
                if review_status == "reviewed" and verdict == "confirm_data_insufficiency":
                    workspace.finalize_blocked_by_evidence()
                elif (
                    review_status == "reviewed"
                    and verdict in {"accept", "accept_with_limits"}
                ) or (
                    review_status == "unavailable"
                    and verdict == "not_reviewed"
                ):
                    workspace.accept()
                else:
                    raise ValueError("requirement review is not terminally accepted")
                return RoleExecution(output="deterministic requirement finalization applied")
            except Exception as exc:
                return RoleExecution(exit_code=1, error=f"deterministic requirement finalization failed: {exc}")

        if name == "commit_integration_requirement":
            if role != "integration_agent":
                return RoleExecution(
                    exit_code=1,
                    error="commit_integration_requirement requires integration_agent",
                )
            session = None
            try:
                from .durable import ItemWorkspace
                from .integration import IntegrationSession
                from .prepared import PreparedAssetRegistry

                workspace = ItemWorkspace.load(self.context, action.subject_id, mode="requirement")
                owner_id, invocation_id = self._integration_identity(workspace)
                session = IntegrationSession.load(
                    self.context,
                    workspace,
                    PreparedAssetRegistry(self.context),
                    owner_id,
                    invocation_id,
                )
                session.commit()
                return RoleExecution(output="deterministic integration commit applied")
            except Exception as exc:
                return RoleExecution(exit_code=1, error=f"deterministic integration commit failed: {exc}")
            finally:
                if session is not None:
                    try:
                        session.release()
                    except Exception:
                        pass

        if name == "commit_identity_result":
            if role != "identity_reviewer":
                return RoleExecution(
                    exit_code=1,
                    error="commit_identity_result requires identity_reviewer",
                )
            try:
                # Identity review acceptance is already durable.  Publishing
                # the validated resolution commit is a mechanical transition
                # and must not consume a second ambient model dispatch.
                from .entity_resolution import EntityResolutionWorkspace

                EntityResolutionWorkspace.load(self.context).commit(action.subject_id)
                return RoleExecution(output="deterministic identity commit applied")
            except Exception as exc:
                return RoleExecution(exit_code=1, error=f"deterministic identity commit failed: {exc}")

        return None

    def _dispatch_role(self, action: PlannerAction, key: str) -> RoleExecution:
        action = validate_role_action_contract(action)
        deterministic = self._dispatch_deterministic(action)
        if deterministic is not None:
            return deterministic
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
        prior_start = entry.get("runner_process_start")
        prior_identity = _process_identity_matches(prior_pid, prior_start)
        if prior_identity is True:
            # A claim from this exact coordinator instance is reusable. Any
            # other exact process identity remains authoritative until it
            # exits.  A matching PID without a matching start token is never
            # enough to retain a claim.
            return (
                prior_pid == os.getpid()
                and prior_runner == self.owner_id
                and prior_start == _process_start_token(os.getpid())
            )
        if prior_identity is None and prior_pid is not None and _pid_alive(prior_pid):
            # A live claim without a verifiable process-start token is
            # ambiguous (including PID reuse).  Do not silently take it over;
            # an explicit reconciliation/replacement must establish a fresh
            # exact owner identity first.
            return False
        if prior_identity is False and prior_pid is not None and _pid_alive(prior_pid):
            # A live PID with a different start token proves that this claim
            # belongs to another process instance.  It cannot be adopted as a
            # normal restart claim.
            return False
        entry["runner_id"] = self.owner_id
        entry["runner_pid"] = os.getpid()
        entry["runner_process_start"] = _required_process_start_token(os.getpid())
        return True

    @staticmethod
    def _claim_is_definitively_orphaned(entry: Mapping[str, Any]) -> bool:
        """Return whether an active claim can be cleared without takeover.

        A missing/dead PID is an orphan.  A live PID whose process-start
        identity is missing or mismatched remains ambiguous and is retained
        until an explicit reconciliation establishes ownership; otherwise a
        restart could silently replace a still-running dispatch.
        """

        pid = entry.get("runner_pid")
        if pid is None:
            return True
        identity = _process_identity_matches(pid, entry.get("runner_process_start"))
        if identity is True:
            return False
        return not _pid_alive(pid)

    def _reconcile_orphaned_product_session_reservations_locked(
        self,
        removed: Sequence[Mapping[str, Any]],
        *,
        state: Mapping[str, Any],
    ) -> tuple[str, ...]:
        """Mark matching dead Product Agent reservations for replacement.

        Transport rebind may safely remove a dispatch claim only after its
        process identity is proven dead.  The corresponding role-session row
        is a separate durable ownership boundary; clear neither it nor its
        lineage.  Instead, compare the exact dispatch token/owner identity
        and transition only that reservation to the registry's existing
        ``replacement_required`` state.  A live or mismatched reservation is
        left untouched and therefore remains a blocking contention boundary.
        """

        if not removed:
            return ()
        try:
            registry = RoleSessionRegistry(self.context)
            document = registry._read_unlocked()  # noqa: SLF001 - coordinator lock is already held
        except (CoordinatorError, OSError, TypeError, ValueError):
            # A missing registry is normal for non-resumable test fixtures.
            # A malformed/unavailable registry cannot authorize replacement;
            # retaining the durable role evidence is the safe outcome.
            return ()
        sessions = document.get("sessions")
        if not isinstance(sessions, Mapping):
            return ()
        run_id = state.get("run_id") or self.context.run_id
        generation_id = state.get("generation_id")
        if not isinstance(run_id, str) or not run_id.strip() or not isinstance(generation_id, str) or not generation_id.strip():
            return ()
        reconciled: list[str] = []
        for dispatch in removed:
            raw_action = dispatch.get("action") if isinstance(dispatch, Mapping) else None
            if not isinstance(raw_action, Mapping):
                continue
            try:
                action = _action(raw_action)
                if action.role.strip().lower() != "product_agent":
                    continue
                identity = _role_session_identity(
                    action,
                    run_id=run_id,
                    generation_id=generation_id,
                )
                token = dispatch.get("idempotency_key")
                if identity is None or not isinstance(token, str) or not token.strip():
                    continue
                entry = sessions.get(identity[0])
                if not isinstance(entry, Mapping):
                    continue
                if (
                    entry.get("status") != "reserved"
                    or entry.get("reservation_status") != "reserved"
                    or entry.get("reservation_token") != token
                    or entry.get("reservation_action") != action.action
                    or entry.get("reservation_owner_id") != dispatch.get("runner_id")
                    or entry.get("reservation_pid") != dispatch.get("runner_pid")
                    or entry.get("reservation_process_start") != dispatch.get("runner_process_start")
                ):
                    continue
                registry._mark_stale_unlocked(  # noqa: SLF001 - shared coordinator/registry lock boundary
                    document,
                    identity[0],
                    action,
                    generation_id=generation_id,
                    idempotency_key=token,
                    reason="orphaned_reservation",
                    reservation_token=token,
                )
                # The durable transition above is authoritative.  Clearing
                # the matching process-local guard only prevents this
                # interpreter from re-reporting its own proven-dead token;
                # it does not erase the registry lineage or grant a fresh
                # reservation.
                registry.release_reservation(identity[0], token)
                reconciled.append(identity[0])
            except (CoordinatorError, OSError, TypeError, ValueError):
                # Never infer ownership after a compare-and-set mismatch;
                # leave the reservation blocked for explicit reconciliation.
                continue
        return tuple(sorted(set(reconciled)))

    def _clear_orphaned_dispatches_for_transport_rebind_locked(
        self,
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Clear only proven-dead transport claims before a rebind.

        A transport release can be rotated after its supervisor has exited,
        leaving stale dispatch claims in the persisted coordinator snapshot.
        The transport rebind is a quiescent transaction, so it may remove
        those claims, but it must not adopt or rewrite a claim whose process
        is live (or whose identity is ambiguous). Action/retry/session
        evidence remains in the event chain and role-session registry; only
        the stale active-slot projection is removed.

        The caller owns ``_locked``. When the last orphan is removed, mark
        the coordinator waiting so an explicit ``reopen`` can establish the
        one-shot replacement authorization for any stale logical owner.
        """

        active = self._active_entries(state)
        if not active:
            return []
        removed = [
            dict(entry)
            for entry in active
            if self._claim_is_definitively_orphaned(entry)
        ]
        if not removed:
            return []
        remaining = [entry for entry in active if entry not in removed]
        state["active_dispatches"] = remaining
        prior_status = state.get("status")
        prior_phase = state.get("phase")
        if not remaining:
            state["status"] = "waiting"
            state["phase"] = "waiting"
        reconciled_sessions = self._reconcile_orphaned_product_session_reservations_locked(
            removed,
            state=state,
        )
        diagnostic = {
            "kind": "dispatch_claims_cleared",
            "reason": "transport_rebind_orphan_cleanup",
            "removed_dispatches": removed,
            "reconciled_product_session_owners": list(reconciled_sessions),
            "prior_status": prior_status,
            "prior_phase": prior_phase,
        }
        self._append_diagnostic(state, diagnostic)
        self._append_event_locked(state, "dispatch_claims_cleared", diagnostic)
        return removed

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
            and self._claim_is_definitively_orphaned(entry)
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
        product_agent_owners: set[str] = set()
        physical_total = 0
        physical_subcounts = {"entity_resolution": 0, "analytical_owner": 0, "specialist": 0}
        capacity = self._resolution_capacity()
        for entry in entries:
            action = validate_role_action_contract(entry["action"])
            product_agent_owner = self._product_agent_owner_key(
                action,
                run_id=str(state.get("run_id") or self.context.run_id),
                generation_id=str(state.get("generation_id") or ""),
            )
            if product_agent_owner is not None and product_agent_owner in product_agent_owners:
                # Older/recovered state may contain multiple Product Agent
                # actions for one owner. Keep the first as the durable queue
                # head and wait for its completion before claiming the next.
                continue
            analytical_owner, workflow_key = self._admission_scope(action)
            if analytical_owner and analytical_owner_count >= 1:
                continue
            if workflow_key is not None and workflow_key in workflow_keys:
                continue
            admitted, _reason = self._capacity_admits(
                action,
                total=physical_total,
                subcounts=physical_subcounts,
                capacity=capacity,
            )
            if not admitted:
                continue
            if analytical_owner:
                analytical_owner_count += 1
            if workflow_key is not None:
                workflow_keys.add(workflow_key)
            if product_agent_owner is not None:
                product_agent_owners.add(product_agent_owner)
            if not self._is_control_action(action):
                physical_total += 1
                scope = self._capacity_scope(action)
                if scope is not None:
                    physical_subcounts[scope] += 1
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

    def _retry_fingerprint(self, state: Mapping[str, Any], action: PlannerAction) -> str:
        """Return a retry key bound to the durable Product request, if any."""

        request = state.get("product_regeneration")
        request_id = request.get("request_id") if isinstance(request, Mapping) else None
        return logical_action_fingerprint(
            action,
            run_id=str(state.get("run_id") or self.context.run_id),
            generation_id=str(state.get("generation_id") or ""),
            product_regeneration_request_id=(
                request_id.strip()
                if isinstance(request_id, str) and request_id.strip()
                else None
            ),
        )

    def _record_retry_locked(
        self,
        state: dict[str, Any],
        action: PlannerAction,
        execution: RoleExecution,
        state_fingerprint: str,
    ) -> tuple[str, int]:
        """Persist one unchanged-offer retry against action *and* state."""

        fingerprint = self._retry_fingerprint(state, action)
        counts = state.setdefault("retry_counts", {})
        if not isinstance(counts, dict):
            counts = {}
            state["retry_counts"] = counts
        count = int(counts.get(fingerprint, 0) or 0) + 1
        counts[fingerprint] = count
        fingerprints = state.setdefault("retry_state_fingerprints", {})
        if not isinstance(fingerprints, dict):
            fingerprints = {}
            state["retry_state_fingerprints"] = fingerprints
        fingerprints[fingerprint] = state_fingerprint
        blocked = state.setdefault("retry_blocked", {})
        if not isinstance(blocked, dict):
            blocked = {}
            state["retry_blocked"] = blocked
        if count >= MAX_RUN_RETRIES_PER_ACTION:
            blocked[fingerprint] = {
                "action": action.to_dict(),
                "count": count,
                "state_fingerprint": state_fingerprint,
                "recoverable": True,
                # A preview transport failure is presentation-local.  It is
                # retried/suppressed by its input fingerprint and must never
                # create a run-level rethink/lifecycle repair boundary.
                "requires_rethink": not _is_preview_action(action),
                "isolated_preview": _is_preview_action(action),
            }
        return fingerprint, count

    def _clear_retry_locked(self, state: dict[str, Any], action: PlannerAction) -> None:
        """Forget retry evidence once the Planner has advanced past an action."""

        fingerprint = self._retry_fingerprint(state, action)
        counts = state.get("retry_counts")
        if isinstance(counts, dict):
            counts.pop(fingerprint, None)
        blocked = state.get("retry_blocked")
        if isinstance(blocked, dict):
            blocked.pop(fingerprint, None)
        fingerprints = state.get("retry_state_fingerprints")
        if isinstance(fingerprints, dict):
            fingerprints.pop(fingerprint, None)

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
        data_refresh_blocked = False
        data_revision_recovery = False
        refresh_applied = False
        refresh_status: CoordinatorStatus | None = None
        planner_read = False
        control_status: str | None = None
        retry_exhausted_actions: list[dict[str, Any]] = []
        deferred_slots: set[str] = set()
        current_state_fingerprint: str | None = None
        completed_offer_state_fingerprint: str | None = None
        terminalized_requirement = False
        needs_fresh_planner = False
        try:
            # Keep the Planner read and control-plane reconciliation in one
            # short critical section. Planner owns its public state and never
            # calls back into the coordinator; role execution starts only
            # after this lock is released.
            with self._locked(create=False):
                state, _ = self._read_replay()
                assert state is not None
                self._verify_planner_binding(state)
                self._reconcile_product_regeneration_locked(state)
                active = self._active_entries(state)
                if completed is not None:
                    completed_action, execution = completed
                    completed_slot = self._slot_key(completed_action)
                    dispatch_fingerprints = state.get("dispatch_state_fingerprints")
                    if isinstance(dispatch_fingerprints, Mapping):
                        raw_fingerprint = dispatch_fingerprints.get(completed_slot)
                        if _is_sha256(raw_fingerprint):
                            completed_offer_state_fingerprint = str(raw_fingerprint)
                        if isinstance(dispatch_fingerprints, dict):
                            dispatch_fingerprints.pop(completed_slot, None)
                    state["last_action"] = completed_action.to_dict()
                    state["active_dispatches"] = [
                        entry for entry in active if str(entry["slot_key"]) != completed_slot
                    ]
                    active = self._active_entries(state)
                    # Remove and checkpoint the completed slot before any
                    # Planner read.  This prevents runtime_snapshot's
                    # lifecycle reconciliation from invalidating a pending
                    # D parent CAS before its safe-boundary admission.
                    self._append_event_locked(
                        state,
                        "role_exit",
                        {
                            "action": completed_action.to_dict(),
                            "idempotency_key": self._idempotency_key(state, completed_action),
                            "transport": self._durable_transport(execution),
                        },
                    )
                    if execution.session_status == "replacement_required":
                        # Keep the lifecycle event privacy-safe: exact
                        # prompts, model output, and command lines are never
                        # copied into the continuity audit.  The registry is
                        # the authority for the detailed stale reason.
                        self._append_event_locked(
                            state,
                            "role_session_replacement_required",
                            {
                                "action": completed_action.to_dict(),
                                "logical_owner": execution.session_key,
                                "session_status": "replacement_required",
                            },
                        )

                # A canonical data admission is consumed only at a no-active
                # boundary and before the fresh Planner snapshot.  If another
                # action is still active, Planner reconciliation remains the
                # ordinary path and the admission waits.
                if not active:
                    refresh_outcome, refresh_status = self._consume_pending_data_refresh_locked(state)
                    if refresh_outcome == "applied":
                        refresh_applied = True
                        pending = []
                    elif refresh_outcome == "blocked":
                        data_refresh_blocked = True
                    elif refresh_outcome == "revision_recovery":
                        # A valid successor journal suppresses only the stale
                        # D admission.  The authoritative current generation
                        # must keep its ordinary Planner/Product work; expose
                        # recovery once that work has no offer to dispatch.
                        data_revision_recovery = True
                        self._append_diagnostic(
                            state,
                            {
                                "kind": "data_revision_recovery",
                                "reason": "pending_revision_not_current",
                            },
                        )
                planner_read = True
                actions = () if data_refresh_blocked else self._query_planner(state)
                planner_read = False
                current_state_fingerprint = self._authoritative_state_fingerprint(state)

                # A restart may have left a second action durable in the same
                # scope but intentionally unsent by the admission queue. Once
                # the fresh Planner snapshot is available, claim the next
                # persisted offer in this reconciliation.
                active = self._active_entries(state)
                if active:
                    pending.extend(self._resume_active_locked(state, actions))
                    active = self._active_entries(state)

                active_slots = {str(entry["slot_key"]) for entry in active}
                analytical_owner_count, workflow_keys = self._admission_counts(active)
                product_agent_owners = self._active_product_agent_owners(
                    active,
                    run_id=str(state.get("run_id") or self.context.run_id),
                    generation_id=str(state.get("generation_id") or ""),
                )
                physical_total, physical_subcounts = self._capacity_counts(active)
                capacity = self._resolution_capacity()
                deferred_actions: list[dict[str, Any]] = []

                if completed is not None:
                    completed_action, execution = completed
                    completed_slot = self._slot_key(completed_action)
                    if refresh_applied:
                        # The completed action belongs to the parent
                        # generation and cannot be compared with a Planner
                        # offer after an authoritative generation refresh.
                        self._last_completion_same = False
                        self._clear_retry_locked(state, completed_action)
                        state["no_progress_count"] = 0
                        state["last_no_progress_action"] = None
                        self._append_event_locked(
                            state,
                            "planner_advanced",
                            {
                                "reason": "data_refresh_applied",
                                "before_action": completed_action.to_dict(),
                                "after_actions": [action.to_dict() for action in actions],
                                "transport": self._durable_transport(execution),
                            },
                        )
                    else:
                        reservation_contention = (
                            execution.session_status == "reservation_in_flight"
                            and self._product_agent_owner_key(
                                completed_action,
                                run_id=str(state.get("run_id") or self.context.run_id),
                                generation_id=str(state.get("generation_id") or ""),
                            )
                            is not None
                        )
                        self._last_completion_same = any(
                            self._slot_key(action) == completed_slot for action in actions
                        )
                        if reservation_contention:
                            # A second transport that loses the shared
                            # Product Agent registry reservation is ordinary
                            # in-flight contention, not a role failure. Keep
                            # it out of retry/rethink accounting and defer the
                            # same offer until the owning reservation exits.
                            self._last_completion_same = False
                            deferred_slots.add(completed_slot)
                        if self._last_completion_same:
                            completed_state_fingerprint = self._authoritative_state_fingerprint(
                                state,
                                completed_action,
                            )
                            completed_fingerprint_changed = (
                                completed_offer_state_fingerprint is not None
                                and completed_offer_state_fingerprint != completed_state_fingerprint
                            )
                            retry_fingerprint = self._retry_fingerprint(state, completed_action)
                            retry_state = state.get("retry_state_fingerprints")
                            prior_retry_fingerprint = (
                                retry_state.get(retry_fingerprint)
                                if isinstance(retry_state, Mapping)
                                else None
                            )
                            completed_fingerprint_changed = completed_fingerprint_changed or (
                                isinstance(prior_retry_fingerprint, str)
                                and prior_retry_fingerprint != completed_state_fingerprint
                            )
                            if completed_fingerprint_changed:
                                # The same logical action is still offered,
                                # but durable state advanced while the role
                                # ran.  It is progress, not another unchanged
                                # retry, so preserve the action's future
                                # continuation opportunity.
                                self._last_completion_same = False
                                state["no_progress_count"] = 0
                                state["last_no_progress_action"] = None
                                self._clear_retry_locked(state, completed_action)
                                self._append_event_locked(
                                    state,
                                    "planner_advanced",
                                    {
                                        "reason": "durable_state_advanced",
                                        "before_action": completed_action.to_dict(),
                                        "after_actions": [action.to_dict() for action in actions],
                                        "transport": self._durable_transport(execution),
                                        "state_fingerprint": completed_state_fingerprint,
                                    },
                                )
                                # Normal action admission continues below;
                                # the local run-loop guard prevents immediate
                                # re-entry of the just-completed slot.
                            else:
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
                                retry_fingerprint, retry_count = self._record_retry_locked(
                                    state,
                                    completed_action,
                                    execution,
                                    completed_state_fingerprint,
                                )
                                self._append_diagnostic(
                                    state,
                                    {
                                        "kind": kind,
                                        "action": completed_action.to_dict(),
                                        "transport": self._durable_transport(execution),
                                        "count": state["no_progress_count"],
                                        "retry_count": retry_count,
                                        "retry_fingerprint": retry_fingerprint,
                                        "retry_exhausted": retry_count >= MAX_RUN_RETRIES_PER_ACTION,
                                        "state_fingerprint": completed_state_fingerprint,
                                    },
                                )
                                # Checkpoint retry/no-progress bookkeeping
                                # even when an unrelated sibling dispatch keeps
                                # this reconciliation in the active branch.
                                # Without an event here, the in-memory retry
                                # count is lost when the next loop rereads the
                                # durable coordinator state.  Keep this event
                                # privacy-safe: transport output/error is
                                # already excluded from the checkpoint.
                                self._append_event_locked(
                                    state,
                                    "retry_recorded",
                                    {
                                        "action": completed_action.to_dict(),
                                        "kind": kind,
                                        "retry_fingerprint": retry_fingerprint,
                                        "retry_count": retry_count,
                                        "retry_exhausted": retry_count >= MAX_RUN_RETRIES_PER_ACTION,
                                        "state_fingerprint": completed_state_fingerprint,
                                    },
                                )
                                if retry_count >= MAX_RUN_RETRIES_PER_ACTION:
                                    if self._terminalize_exhausted_action_locked(
                                        state,
                                        completed_action,
                                        execution,
                                    ):
                                        terminalized_requirement = True
                                        needs_fresh_planner = True
                                        self._append_event_locked(
                                            state,
                                            "requirement_terminalized",
                                            {
                                                "action": completed_action.to_dict(),
                                                "retry_count": retry_count,
                                                "reason": "recovery_exhausted",
                                            },
                                        )
                        else:
                            self._clear_retry_locked(state, completed_action)
                            state["no_progress_count"] = 0
                            state["last_no_progress_action"] = None
                            if not execution.ok and not reservation_contention:
                                self._append_diagnostic(
                                    state,
                                    {
                                        "kind": "role_transport_failure",
                                        "action": completed_action.to_dict(),
                                        "transport": self._durable_transport(execution),
                                    },
                                )
                            if reservation_contention:
                                self._append_diagnostic(
                                    state,
                                    {
                                        "kind": "dispatch_deferred",
                                        "reason": "role_session_reservation_in_flight",
                                        "action": completed_action.to_dict(),
                                        "logical_owner": self._product_agent_owner_key(
                                            completed_action,
                                            run_id=str(state.get("run_id") or self.context.run_id),
                                            generation_id=str(state.get("generation_id") or ""),
                                        ),
                                        "transport": self._durable_transport(execution),
                                    },
                                )
                            self._append_event_locked(
                                state,
                                "planner_advanced",
                                {
                                    "reason": (
                                        "role_session_reservation_in_flight"
                                        if reservation_contention
                                        else "completed_offer_not_repeated"
                                    ),
                                    "before_action": completed_action.to_dict(),
                                    "after_actions": [action.to_dict() for action in actions],
                                    "transport": self._durable_transport(execution),
                                },
                            )
                for action in actions:
                    if data_refresh_blocked:
                        break
                    if terminalized_requirement:
                        # The just-failed item has released its claim.  Do
                        # not admit stale offers from the pre-terminal
                        # Planner snapshot; a fresh pass runs after this lock.
                        continue
                    action = validate_role_action_contract(action)
                    if (
                        action.action.strip().lower() == "review_final_product"
                        and action.role.strip().lower() == "product_reviewer"
                        and not self._product_regeneration_review_binding_locked(state, action)
                    ):
                        # A Product Reviewer offer is advisory until it is
                        # checked against the exact pending revision/request
                        # binding.  Reject malformed, cross-revision, or
                        # stale candidates before retry accounting,
                        # reservation admission, and transport.
                        self._append_diagnostic(
                            state,
                            {
                                "kind": "product_reviewer_binding_rejected",
                                "action": action.to_dict(),
                                "reason": "product_regeneration_binding_invalid",
                            },
                        )
                        continue
                    if self._is_control_action(action):
                        # Planner/control offers are durable state decisions,
                        # not model work.  Never place them in the active
                        # dispatch queue or pass them to an ambient adapter.
                        control_fingerprint = self._retry_fingerprint(state, action)
                        metadata = action.metadata if isinstance(action.metadata, Mapping) else {}
                        requires_rethink = (
                            metadata.get("requires_rethink") is True
                            or action.action.strip().lower() in {"requires_rethink", "rethink", "escalate_identity_failure", "repair_identity_request"}
                        )
                        control_status = "blocked_rethink" if requires_rethink else "waiting"
                        state["status"] = control_status
                        state["phase"] = "rethink" if requires_rethink else "control"
                        state["last_no_progress_action"] = action.to_dict()
                        prior_control = state.get("last_control_fingerprint")
                        if prior_control != control_fingerprint:
                            state["last_control_fingerprint"] = control_fingerprint
                            control_diagnostic = {
                                "kind": "coordinator_control",
                                "action": action.to_dict(),
                                "control_fingerprint": control_fingerprint,
                                "requires_rethink": requires_rethink,
                                "recoverable": True,
                            }
                            self._append_diagnostic(state, control_diagnostic)
                            self._append_event_locked(state, "control", control_diagnostic)
                        # Identity-domain controls are item-local evidence.
                        # Keep them visible, but do not let a malformed or
                        # failed domain suppress unrelated runnable items in
                        # this same Planner snapshot.  Global lifecycle,
                        # product, and run-integrity controls remain
                        # fail-closed and stop admission here.
                        if action.action.strip().lower() not in {
                            "escalate_identity_failure",
                            "repair_identity_request",
                        }:
                            break
                        if action.action.strip().lower() == "escalate_identity_failure":
                            metadata = action.metadata if isinstance(action.metadata, Mapping) else {}
                            if metadata.get("binding_status") == "active_unresolved":
                                if self._terminalize_identity_requesters_locked(action):
                                    terminalized_requirement = True
                                    needs_fresh_planner = True
                                    self._append_event_locked(
                                        state,
                                        "requirement_terminalized",
                                        {
                                            "action": action.to_dict(),
                                            "reason": "identity_recovery_exhausted",
                                        },
                                    )
                        continue
                    slot = self._slot_key(action)
                    # Product preview and terminal composition have distinct
                    # slots for telemetry, but share one run/generation
                    # Product Agent session. Keep a second action deferred
                    # while that owner is active or reserved; this check is
                    # deliberately before retry/replacement accounting so
                    # contention cannot consume a retry or trigger rethink.
                    if slot in active_slots:
                        continue
                    if slot in deferred_slots:
                        deferred_actions.append(
                            {
                                "action": action.to_dict(),
                                "reason": "role_session_reservation_in_flight",
                            }
                        )
                        continue
                    product_agent_owner = self._product_agent_owner_key(
                        action,
                        run_id=str(state.get("run_id") or self.context.run_id),
                        generation_id=str(state.get("generation_id") or ""),
                    )
                    if product_agent_owner is not None:
                        if product_agent_owner in product_agent_owners:
                            deferred_actions.append(
                                {
                                    "action": action.to_dict(),
                                    "reason": "product_agent_owner_busy",
                                    "logical_owner": product_agent_owner,
                                }
                            )
                            continue
                        if (
                            self._reserved_product_agent_owner_locked(
                                action,
                                run_id=str(state.get("run_id") or self.context.run_id),
                                generation_id=str(state.get("generation_id") or ""),
                            )
                            is not None
                        ):
                            deferred_actions.append(
                                {
                                    "action": action.to_dict(),
                                    "reason": "role_session_reservation_in_flight",
                                    "logical_owner": product_agent_owner,
                                }
                            )
                            continue
                    retry_fingerprint = self._retry_fingerprint(state, action)
                    retry_counts = state.get("retry_counts") if isinstance(state.get("retry_counts"), Mapping) else {}
                    retry_records = state.get("retry_blocked") if isinstance(state.get("retry_blocked"), Mapping) else {}
                    retry_state_records = (
                        state.get("retry_state_fingerprints")
                        if isinstance(state.get("retry_state_fingerprints"), Mapping)
                        else {}
                    )
                    prior_retry_state = retry_state_records.get(retry_fingerprint)
                    action_state_fingerprint = self._authoritative_state_fingerprint(state, action)
                    if (
                        isinstance(prior_retry_state, str)
                        and prior_retry_state != action_state_fingerprint
                    ):
                        # A durable state advance makes prior exhaustion stale
                        # for this logical action.  Clear only this action's
                        # evidence; unrelated retry histories stay bounded.
                        self._clear_retry_locked(state, action)
                        retry_counts = {}
                        retry_records = {}
                    # ``retry_blocked`` is a one-pass run-loop guard that
                    # prevents immediate resubmission of the just-completed
                    # slot.  Only the persisted count/record constitutes
                    # durable exhaustion across coordinator restarts.
                    if slot in retry_blocked:
                        continue
                    # A preview-only stale Product Agent owner may be reused
                    # once for the final candidate boundary.  This is a
                    # coordinator-issued, owner-scoped authorization; all
                    # other resumable actions retain the explicit reopen
                    # requirement for replacement.
                    replacement_boundary_valid = True
                    if action.action.strip().lower() == "build_product_candidate":
                        replacement_boundary_valid = self._authorize_preview_replacement_for_final_locked(
                            state,
                            action,
                        )
                    replacement_authorized = (
                        replacement_boundary_valid
                        and self._replacement_authorization_for_action(state, action) is not None
                    )
                    retry_exhausted = (
                        retry_fingerprint in retry_records
                        or int(retry_counts.get(retry_fingerprint, 0) or 0) >= MAX_RUN_RETRIES_PER_ACTION
                    )
                    if retry_exhausted and not replacement_authorized:
                        if self._terminalize_exhausted_action_locked(state, action):
                            terminalized_requirement = True
                            needs_fresh_planner = True
                            product_regeneration_terminalized = (
                                action.role.strip().lower() in {"product_agent", "product_reviewer"}
                                and action.action.strip().lower() in {"build_product_candidate", "review_final_product"}
                                and isinstance(action.metadata, Mapping)
                                and action.metadata.get("authorization_origin") == PRODUCT_REGENERATION_ORIGIN
                            )
                            if not product_regeneration_terminalized:
                                self._append_event_locked(
                                    state,
                                    "requirement_terminalized",
                                    {
                                        "action": action.to_dict(),
                                        "retry_count": int(retry_counts.get(retry_fingerprint, 0) or 0),
                                        "reason": "recovery_exhausted",
                                    },
                                )
                        else:
                            retry_exhausted_actions.append(
                                {
                                    "action": action.to_dict(),
                                    "retry_fingerprint": retry_fingerprint,
                                    "retry_count": int(retry_counts.get(retry_fingerprint, 0) or 0),
                                    "recoverable": True,
                                    "requires_rethink": not _is_preview_action(action),
                                    "isolated_preview": _is_preview_action(action),
                                }
                            )
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
                    admitted, capacity_reason = self._capacity_admits(
                        action,
                        total=physical_total,
                        subcounts=physical_subcounts,
                        capacity=capacity,
                    )
                    if not admitted:
                        deferred_actions.append(
                            {
                                "action": action.to_dict(),
                                "reason": capacity_reason or "capacity",
                            }
                        )
                        continue
                    replacement_consumed = (
                        self._consume_replacement_authorization(state, action)
                        if replacement_authorized
                        else None
                    )
                    replacement_binding: Mapping[str, Any] | None = None
                    if replacement_consumed is not None:
                        action, replacement_binding = replacement_consumed
                    key = self._idempotency_key(state, action)
                    entry = {
                        "action": action.to_dict(),
                        "idempotency_key": key,
                        "slot_key": slot,
                        "runner_id": self.owner_id,
                        "runner_pid": os.getpid(),
                        "runner_process_start": _required_process_start_token(os.getpid()),
                    }
                    dispatch_state_fingerprints = state.setdefault("dispatch_state_fingerprints", {})
                    if not isinstance(dispatch_state_fingerprints, dict):
                        dispatch_state_fingerprints = {}
                        state["dispatch_state_fingerprints"] = dispatch_state_fingerprints
                    if current_state_fingerprint is not None:
                        dispatch_state_fingerprints[slot] = self._authoritative_state_fingerprint(
                            state,
                            action,
                        )
                    state.setdefault("active_dispatches", []).append(entry)
                    state["status"] = "dispatching"
                    state["phase"] = action.action
                    state["attempt"] = int(state.get("attempt", 0) or 0) + 1
                    dispatch_payload: dict[str, Any] = {
                        "action": action.to_dict(),
                        "idempotency_key": key,
                    }
                    if replacement_binding is not None:
                        # Consume replacement authorization and advance the
                        # Product regeneration state in the same authoritative
                        # dispatch_started checkpoint.  A crash before this
                        # event leaves both pending; replay after the event
                        # observes both consumed and active.
                        dispatch_payload["replacement_authorization"] = dict(replacement_binding)
                        if replacement_binding.get("authorization_origin") == PRODUCT_REGENERATION_ORIGIN:
                            request = state.get("product_regeneration")
                            request_id = replacement_binding.get("request_id")
                            if (
                                isinstance(request, Mapping)
                                and request.get("status") == "requested"
                                and request.get("request_id") == request_id
                            ):
                                state["product_regeneration"] = {
                                    **dict(_canonical(request)),
                                    "status": "dispatched",
                                }
                                dispatch_payload["product_regeneration"] = {
                                    "authorization_origin": PRODUCT_REGENERATION_ORIGIN,
                                    "request_id": request_id,
                                    "revision_id": replacement_binding.get("revision_id"),
                                }
                    self._append_event_locked(
                        state,
                        "dispatch_started",
                        dispatch_payload,
                    )
                    active_slots.add(slot)
                    if analytical_owner:
                        analytical_owner_count += 1
                    if workflow_key is not None:
                        workflow_keys.add(workflow_key)
                    if product_agent_owner is not None:
                        product_agent_owners.add(product_agent_owner)
                    if not self._is_control_action(action):
                        physical_total += 1
                        scope = self._capacity_scope(action)
                        if scope is not None:
                            physical_subcounts[scope] += 1
                    new_entries.append(entry)
                if deferred_actions and not terminalized_requirement:
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
                if retry_exhausted_actions and not terminalized_requirement:
                    # Exhaustion is recoverable only through explicit
                    # inspection/repair and ``reopen``; never spin a hidden
                    # ambient retry loop after a restart.
                    requires_rethink = any(
                        entry.get("requires_rethink") is True
                        for entry in retry_exhausted_actions
                    )
                    self._append_diagnostic(
                        state,
                        {
                            "kind": "retry_budget_exhausted",
                            "actions": retry_exhausted_actions,
                            "recoverable": True,
                            "requires_rethink": requires_rethink,
                        },
                    )
                active = self._active_entries(state)
                product_request = state.get("product_regeneration")
                product_terminalized = (
                    terminalized_requirement
                    and isinstance(product_request, Mapping)
                    and product_request.get("status") in {"accepted", "failed"}
                    and state.get("phase") in {"product_regeneration_complete", "product_regeneration_failed"}
                )
                if product_terminalized and not active:
                    status = self._status_from_state(state)
                elif control_status is not None and not active:
                    status = self._status_from_state(state)
                elif data_refresh_blocked:
                    status = refresh_status
                elif data_revision_recovery and not active and not actions:
                    status = refresh_status
                elif not active and not actions:
                    # A Product regeneration request has its own terminal
                    # accepted/failed phase under the analytical generation.
                    # Preserve that durable outcome instead of replacing it
                    # with the generic Planner-empty ``waiting`` projection;
                    # this keeps the accepted pointer/fallback visible to a
                    # restart while the ordinary run may continue through
                    # its unrelated lifecycle boundary.
                    product_request = state.get("product_regeneration")
                    product_terminal = (
                        isinstance(product_request, Mapping)
                        and product_request.get("status") in {"accepted", "failed"}
                        and state.get("phase") in {
                            "product_regeneration_complete",
                            "product_regeneration_failed",
                        }
                    )
                    if product_terminal:
                        status = self._status_from_state(state)
                    else:
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
                    state["last_no_progress_action"] = actions[0].to_dict()
                    requires_rethink = any(
                        entry.get("requires_rethink") is True
                        for entry in retry_exhausted_actions
                    )
                    self._append_event_locked(
                        state,
                        "wait",
                        {
                            "reason": "retry_budget",
                            "actions": [action.to_dict() for action in actions],
                            "retry_exhausted": retry_exhausted_actions,
                            "recoverable": True,
                            "requires_rethink": requires_rethink,
                        },
                    )
                preserve_refresh_status = data_refresh_blocked or (data_revision_recovery and not active and not actions)
                if not preserve_refresh_status or refresh_status is None:
                    status = self._status_from_state(state)
        except Exception as exc:
            if not planner_read:
                raise
            return self._record_planner_error(exc)
        self._submit_entries(pending)
        self._submit_entries(new_entries)
        if needs_fresh_planner:
            # Re-read only after the terminalization transaction and any
            # already-active sibling entries have been checkpointed.  The
            # recursive call is outside the coordinator lock, so the next
            # requirement can be admitted in this same ``run`` turn without
            # an explicit reopen.
            return self._refresh_and_launch(retry_blocked)
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

    def _current_product_regeneration_inputs(
        self,
    ) -> tuple[
        Mapping[str, Any],
        Mapping[str, Any],
        Mapping[str, Mapping[str, Any]],
        tuple[str, ...],
        Mapping[str, Mapping[str, Any]],
        str,
    ]:
        """Return the current validated accepted-input projection.

        Both the operator request and its read-only UI projection must derive
        their binding from the same phase snapshot.  In particular, never
        reuse a terminal request's old fingerprint after accepted answers or
        their source bindings have changed.
        """

        from .requirement_planning import (
            _accepted_preview_bindings_complete,
            _preview_item_views,
            preview_input_fingerprint,
        )

        phase = self._phase_snapshot()
        product = phase.get("product") if isinstance(phase, Mapping) else None
        items = phase.get("items") if isinstance(phase, Mapping) else None
        if not isinstance(product, Mapping) or not isinstance(items, Mapping):
            raise ValueError("Product presentation projection is unavailable")
        accepted_ids = tuple(item_id for item_id, _phase in _preview_item_views(items))
        item_ids_value = product.get("preview_item_ids")
        item_bindings = product.get("preview_item_bindings")
        if not isinstance(item_ids_value, (list, tuple)):
            raise ValueError("Product presentation item projection is unavailable")
        item_ids = tuple(item_ids_value)
        if item_ids != accepted_ids:
            raise ValueError("Product presentation item projection is stale")
        if not isinstance(item_bindings, Mapping):
            raise ValueError("Product presentation binding projection is unavailable")
        input_fingerprint = preview_input_fingerprint(items)
        if product.get("preview_input_fingerprint") != input_fingerprint:
            raise ValueError("Product presentation input fingerprint is stale")
        if not _accepted_preview_bindings_complete(items, item_ids, item_bindings):
            raise ValueError("Product presentation accepted bindings are incomplete")
        return phase, product, items, item_ids, item_bindings, input_fingerprint

    def product_regeneration_projection(
        self,
        *,
        implementation_identity: str | None = None,
        persisted_spec_hash: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Return the durable Product-regeneration request projection.

        Control Center status uses this narrow read-only boundary for pending
        and idempotency state.  It intentionally exposes the request marker
        only; action bindings, artifact paths, and transport internals remain
        Coordinator admission data.

        ``implementation_identity`` and ``persisted_spec_hash`` are supplied
        only by :meth:`product_regeneration_projection_for_spec` after it has
        validated a desired post-rebind specification against the raw
        persisted document.  Supplying that pair keeps the read-only preview
        from constructing an adapter bound to an older installed release.
        """

        if implementation_identity is None:
            self._ensure_persisted_configuration()
        elif not _is_sha256(implementation_identity):
            raise CoordinatorIntegrityError("Product regeneration implementation identity is invalid")
        if self._legacy_pending:
            return None
        with self._locked(create=False):
            state, _ = self._read_replay()
            assert state is not None
            if persisted_spec_hash is not None:
                if not _is_sha256(persisted_spec_hash) or state.get("spec_hash") != persisted_spec_hash:
                    raise CoordinatorIntegrityError("persisted coordinator state/spec binding is invalid")
            request = state.get("product_regeneration")
            if request is not None and not isinstance(request, Mapping):
                raise CoordinatorIntegrityError("product regeneration request is invalid")
            if isinstance(request, Mapping):
                if request.get("run_id") != self.context.run_id or request.get("generation_id") != state.get("generation_id"):
                    raise CoordinatorIntegrityError("product regeneration request lineage is invalid")
                projection = dict(_canonical(request))
            else:
                projection = {}

            # ``request_id`` is the durable one-shot key only while a
            # regeneration is in flight.  Once a request reaches a terminal
            # outcome, retaining that old key in the UI would make the next
            # click an intentional no-op forever.  Derive a fresh, stable
            # intent from the *current* accepted projection/spec/revision
            # boundary plus the terminal outcome (or the no-request state),
            # so stale terminal request bindings can never leak into a new
            # intent and status polling remains read-only.
            pending = projection.get("status") in {"requested", "dispatched", "running"}
            projection["pending"] = pending
            if pending:
                request_id = projection.get("request_id")
                if not isinstance(request_id, str) or not request_id.strip():
                    raise CoordinatorIntegrityError("pending product regeneration request id is invalid")
                projection["idempotency_key"] = request_id.strip()
            else:
                try:
                    from .product_review import ProductReviewStore

                    generation_id = state.get("generation_id")
                    run_id = state.get("run_id")
                    implementation_identity = implementation_identity or state.get("spec_hash")
                    if (
                        not isinstance(run_id, str)
                        or not run_id.strip()
                        or not isinstance(generation_id, str)
                        or not generation_id.strip()
                        or not _is_sha256(implementation_identity)
                    ):
                        raise CoordinatorIntegrityError("Product regeneration coordinator binding is invalid")
                    if self._spec is not None and self._spec_hash(self._spec) != implementation_identity:
                        raise CoordinatorIntegrityError("Product regeneration specification binding is stale")
                    _phase, product, _items, _item_ids, _item_bindings, input_fingerprint = (
                        self._current_product_regeneration_inputs()
                    )
                    product_store = ProductReviewStore(self.context, generation_id)
                    pointer = product_store.read_active_revision()
                    if pointer is not None:
                        active_revision: Mapping[str, Any] | None = {
                            "revision_id": pointer.revision_id,
                            "revision_hash": pointer.revision_hash,
                            "candidate_hash": pointer.candidate_hash,
                            "review_hash": pointer.review_hash,
                        }
                    else:
                        # A revision namespace without its authoritative
                        # pointer is not a legacy run: it is evidence of a
                        # lost/tampered pointer and must never fall back to
                        # phase-projected root hashes.  ``read_active_revision``
                        # is intentionally read-only; inspect the namespace
                        # without creating it so status polling remains a
                        # non-mutating admission check.
                        revisions_root = product_store.revisions_root
                        revision_evidence = False
                        try:
                            if revisions_root.is_symlink():
                                revision_evidence = True
                            elif revisions_root.exists():
                                if not revisions_root.is_dir():
                                    revision_evidence = True
                                else:
                                    revision_evidence = any(revisions_root.iterdir())
                        except OSError as exc:
                            raise CoordinatorIntegrityError("Product revision evidence is unreadable") from exc
                        if revision_evidence:
                            raise CoordinatorIntegrityError(
                                "Product revision evidence exists without an active pointer"
                            )

                        # Before one-time revision adoption, validate the
                        # complete legacy root bundle through ProductReviewStore
                        # itself.  Do not trust hashes projected in phase state:
                        # the persisted candidate/review and every artifact
                        # binding must be present, accepted, and hash-valid.
                        candidate_record = product_store.load_candidate()
                        review_record = product_store.load_review()
                        candidate_hash = candidate_record.candidate_hash
                        review_hash = review_record.review_hash
                        if not _is_sha256(candidate_hash) or not _is_sha256(review_hash):
                            raise CoordinatorIntegrityError(
                                "legacy Product candidate/review hashes are incomplete"
                            )
                        if review_record.verdict not in {"accept", "accept_with_limits"}:
                            raise CoordinatorIntegrityError(
                                "legacy Product review is not accepted"
                            )
                        if review_record.candidate_hash != candidate_record.computed_hash:
                            raise CoordinatorIntegrityError(
                                "legacy Product review is stale against the candidate"
                            )
                        active_revision = {
                            "revision_id": None,
                            "revision_hash": None,
                            "candidate_hash": candidate_hash,
                            "review_hash": review_hash,
                        }
                except (CoordinatorError, OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                    # Current accepted-input or ProductRevision evidence is a
                    # hard admission boundary.  A malformed/tampered binding
                    # must not expose a clickable regeneration intent.
                    projection["eligible"] = False
                    projection["idempotency_key"] = None
                    return projection
                seed = {
                    "run_id": run_id,
                    "generation_id": generation_id,
                    "active_revision": active_revision,
                    "input_fingerprint": input_fingerprint,
                    "implementation_identity": implementation_identity,
                    "terminal_request_id": (
                        projection.get("request_id")
                        if projection.get("status") in {"accepted", "failed"}
                        else None
                    ),
                    "terminal_revision_id": (
                        projection.get("revision_id")
                        if projection.get("status") in {"accepted", "failed"}
                        else None
                    ),
                    "terminal_status": (
                        projection.get("status")
                        if projection.get("status") in {"accepted", "failed"}
                        else None
                    ),
                }
                projection["eligible"] = True
                projection["idempotency_key"] = "product-regeneration-" + _sha256_value(seed)
            return projection if projection else None

    def product_regeneration_projection_for_spec(
        self,
        desired_spec: CoordinatorRunSpec | Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Project Product regeneration against a desired post-rebind spec.

        A status read may observe a run whose persisted Codex skill binding is
        an older release than the currently installed one.  Constructing that
        persisted coordinator would correctly fail closed, but it would also
        prevent Control Center from showing the operator the deterministic
        intent that POST will use after its normal rebind transaction.  This
        method validates the old raw specification and event checkpoint using
        the existing transport-projection helper, then evaluates the same
        Product projection with the desired spec hash.  It performs no
        recovery, adapter construction, event, state, or registry write.
        """

        if isinstance(desired_spec, Mapping):
            try:
                target = CoordinatorRunSpec.from_dict(desired_spec)
            except (KeyError, TypeError, ValueError) as exc:
                raise CoordinatorIntegrityError("desired coordinator specification is invalid") from exc
        elif isinstance(desired_spec, CoordinatorRunSpec):
            target = desired_spec
        else:
            raise TypeError("desired_spec must be a CoordinatorRunSpec or mapping")
        if target.run_id != self.context.run_id:
            raise CoordinatorConflictError("desired coordinator spec run_id does not match context")
        target_hash = self._spec_hash(target)
        if not _is_sha256(target_hash):
            raise CoordinatorIntegrityError("desired coordinator specification hash is invalid")
        # Keep this core entrypoint fail-closed even when called directly
        # rather than through LaunchManager.preview_resume_coordinator.  The
        # guard is read-only and validates any transport/recovery intent before
        # projecting the post-rebind Product request.
        self.validate_read_only_resume_evidence()
        if self._legacy_pending:
            return None
        with self._locked(create=False):
            state, _ = self._read_replay()
            assert state is not None
            # Parse the persisted outer spec while replacing only its Codex
            # transport with the desired binding.  This validates every
            # persisted field and returns the exact old hash without loading
            # or touching an adapter tied to the stale skill path.
            _projected, persisted_hash = self._load_transport_spec_for_target(target)
            if state.get("spec_hash") != persisted_hash:
                raise CoordinatorIntegrityError("persisted coordinator state/spec binding is invalid")
        return self.product_regeneration_projection(
            implementation_identity=target_hash,
            persisted_spec_hash=persisted_hash,
        )

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
                if self._claim_is_definitively_orphaned(entry):
                    entry.pop("runner_pid", None)
                    entry.pop("runner_id", None)
                    entry.pop("runner_process_start", None)
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
            # Reopening is a fresh Planner admission boundary, not a global
            # retry-history reset.  Durable retry evidence remains available
            # for unchanged offers; only an action-specific durable state
            # advance clears its evidence through ``_clear_retry_locked``.
            state["last_control_fingerprint"] = None
            state["publication_ready"] = False
            # An explicit reopen is the only source of replacement authority.
            # Snapshot the exact stale logical owners while this shared lock is
            # held; the next matching Planner admission consumes each entry
            # one-shot.  Retry evidence and accepted/integrated item state are
            # intentionally untouched.
            replacement_authorizations = self._replacement_authorizations_from_registry_locked(self.context)
            state["replacement_authorizations"] = replacement_authorizations
            self._append_event_locked(
                state,
                "reopen",
                {
                    "reason": reason,
                    "replacement_logical_owners": sorted(replacement_authorizations),
                },
            )
            return self._status_from_state(state)

    def regenerate_product(
        self,
        *,
        reason: str = "operator requested Product dashboard regeneration",
        idempotency_key: str | None = None,
    ) -> CoordinatorStatus:
        """Request one intentional Product Agent candidate regeneration.

        This operation is a narrow operator control-plane boundary. It does
        not mark a transport stale, clear retry evidence, reopen unrelated
        roles, or publish a product. The accepted-answer projection is
        validated before any state/registry write, then one bound
        ``build_product_candidate`` offer is emitted on the next normal
        Coordinator step/resume.
        """

        reason = _text(reason, "reason")
        requested_token = _text(idempotency_key, "idempotency_key") if idempotency_key is not None else None
        self._ensure_persisted_configuration()
        if self._legacy_pending:
            raise CoordinatorConflictError("product regeneration requires the canonical coordinator state")
        with self._locked(create=False):
            pending = self._recover_legacy_import_locked()
            if pending is not None:
                raise CoordinatorConflictError("product regeneration requires the canonical coordinator state")
            state, _ = self._read_replay()
            assert state is not None
            self._verify_planner_binding(state)

            existing_request = state.get("product_regeneration")
            if isinstance(existing_request, Mapping) and requested_token is not None:
                if existing_request.get("request_id") == requested_token:
                    return self._status_from_state(state)
                if existing_request.get("status") in {"requested", "dispatched"}:
                    raise CoordinatorConflictError("a different Product regeneration request is already recorded")

            # No new operator request may race any model transport. A normal
            # paused/ready/waiting boundary has no active Coordinator entry;
            # registry reservations are checked separately below.
            if self._active_entries(state):
                raise CoordinatorConflictError("product regeneration requires an idle Coordinator boundary")

            try:
                phase, product, items, item_ids, item_bindings, input_fingerprint = (
                    self._current_product_regeneration_inputs()
                )
                if not item_ids or not _is_sha256(input_fingerprint):
                    raise ValueError("source-bound accepted business inputs are unavailable")
            except (CoordinatorError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise CoordinatorConflictError(
                    "Product regeneration requires complete source-bound accepted business inputs"
                ) from exc

            run_id = state.get("run_id")
            generation_id = state.get("generation_id")
            implementation_identity = state.get("spec_hash")
            if (
                not isinstance(run_id, str)
                or not run_id.strip()
                or not isinstance(generation_id, str)
                or not generation_id.strip()
                or not _is_sha256(implementation_identity)
            ):
                raise CoordinatorIntegrityError("Product regeneration coordinator binding is invalid")

            candidate = product.get("candidate")
            review = product.get("review")
            prior_candidate_hash = candidate.get("candidate_hash") if isinstance(candidate, Mapping) else None
            prior_review_hash = review.get("review_hash") if isinstance(review, Mapping) else None
            if prior_candidate_hash is not None and not _is_sha256(prior_candidate_hash):
                raise CoordinatorConflictError("existing Product candidate binding is invalid")
            if prior_review_hash is not None and not _is_sha256(prior_review_hash):
                raise CoordinatorConflictError("existing Product review binding is invalid")

            # Resolve the active revision before deriving an omitted request
            # key.  A terminal failed target leaves the prior pointer in
            # place, so the pointer alone cannot distinguish the next retry;
            # the previous terminal request/revision identity below supplies
            # that distinction while keeping the key stable across reloads.
            try:
                from .product_review import ProductReviewStore

                product_store = ProductReviewStore(self.context, generation_id)
                # ``read_active_revision`` is deliberately pure.  Legacy
                # adoption (which may create rev-0001) is deferred until the
                # Product owner registry has passed admission below, so an
                # active reservation conflict cannot leave a new revision
                # namespace behind.
                active_pointer = product_store.read_active_revision()
                if active_pointer is not None:
                    prior_candidate_hash = active_pointer.candidate_hash
                    prior_review_hash = active_pointer.review_hash
                    active_revision_identity = {
                        "revision_id": active_pointer.revision_id,
                        "revision_hash": active_pointer.revision_hash,
                        "candidate_hash": active_pointer.candidate_hash,
                        "review_hash": active_pointer.review_hash,
                    }
                else:
                    active_revision_identity = None
            except (CoordinatorError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise CoordinatorConflictError("Product regeneration revision transaction is unavailable") from exc

            if requested_token is None:
                requested_token = _sha256_value(
                    {
                        "run_id": run_id,
                        "generation_id": generation_id,
                        "input_fingerprint": input_fingerprint,
                        "implementation_identity": implementation_identity,
                        "prior_candidate_hash": prior_candidate_hash,
                        "prior_review_hash": prior_review_hash,
                        "prior_revision_id": active_pointer.revision_id if active_pointer is not None else None,
                        "active_revision": active_revision_identity,
                        "terminal_request_id": (
                            existing_request.get("request_id")
                            if isinstance(existing_request, Mapping)
                            and existing_request.get("status") in {"accepted", "failed"}
                            else None
                        ),
                        "terminal_revision_id": (
                            existing_request.get("revision_id")
                            if isinstance(existing_request, Mapping)
                            and existing_request.get("status") in {"accepted", "failed"}
                            else None
                        ),
                        "terminal_status": (
                            existing_request.get("status")
                            if isinstance(existing_request, Mapping)
                            and existing_request.get("status") in {"accepted", "failed"}
                            else None
                        ),
                        "reason": reason,
                    }
                )
            # The default idempotency key is derived only after the current
            # input/prior bindings are known.  Re-check it here so a caller
            # that omits an explicit key cannot reopen the same terminal
            # request after a restart.  A different key is intentionally
            # allowed only after the prior request reached a terminal
            # outcome; pending requests remain a conflict.
            if isinstance(existing_request, Mapping):
                if existing_request.get("request_id") == requested_token:
                    return self._status_from_state(state)
                if existing_request.get("status") in {"requested", "dispatched"}:
                    raise CoordinatorConflictError("a different Product regeneration request is already recorded")

            terminal_request = (
                existing_request
                if isinstance(existing_request, Mapping)
                and existing_request.get("status") in {"accepted", "failed"}
                else None
            )
            predecessor_product_review_ref: str | None = None
            predecessor_product_review_hash: str | None = None
            if terminal_request is not None:
                # A replacement-required registry row is refreshable only
                # when the predecessor is a replayed, hash-valid Product
                # revision bound to that exact terminal request.  A state
                # marker alone is insufficient authority (and must not allow
                # a new revision namespace to be created).
                try:
                    terminal_revision_id = terminal_request.get("revision_id")
                    terminal_request_id = terminal_request.get("request_id")
                    terminal_revision = (
                        product_store.load_revision(terminal_revision_id)
                        if isinstance(terminal_revision_id, str)
                        else None
                    )
                    if (
                        terminal_revision is None
                        or terminal_revision.status != terminal_request.get("status")
                        or terminal_revision.request_id != terminal_request_id
                        or terminal_revision.input_fingerprint != terminal_request.get("input_fingerprint")
                        or terminal_revision.implementation_identity
                        != terminal_request.get("implementation_identity")
                        or terminal_revision.output_root_ref != terminal_request.get("output_root_ref")
                        or not _is_sha256(terminal_revision.revision_hash)
                        or terminal_revision.revision_hash != terminal_revision.computed_hash
                    ):
                        raise ValueError("terminal Product revision binding is invalid")
                    for field_name in ("prior_revision_id", "prior_candidate_hash", "prior_review_hash"):
                        if terminal_request.get(field_name) != getattr(terminal_revision, field_name):
                            raise ValueError("terminal Product revision predecessor binding is invalid")
                    if terminal_request.get("status") == "accepted":
                        if (
                            active_pointer is None
                            or active_pointer.revision_id != terminal_revision.revision_id
                            or active_pointer.status != "accepted"
                            or active_pointer.revision_hash != terminal_revision.revision_hash
                        ):
                            raise ValueError("accepted Product revision is not the active pointer")
                except (CoordinatorError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise CoordinatorConflictError("terminal Product regeneration evidence is unavailable") from exc
                if terminal_request.get("status") == "failed" and terminal_revision is not None:
                    # A failed revision may still carry a valid Product Review
                    # (for example, a blocked/rethink outcome).  Preserve
                    # that exact immutable evidence for the next Product
                    # Agent, but keep the active accepted revision's review
                    # out of this repair channel.
                    expected_review_ref = (
                        f"products/generations/{generation_id}/product_revisions/"
                        f"{terminal_revision.revision_id}/product_review.json"
                    )
                    if (
                        terminal_revision.review_ref == expected_review_ref
                        and _is_sha256(terminal_revision.review_hash)
                    ):
                        try:
                            predecessor_review = product_store.load_revision_review(terminal_revision.revision_id)
                        except (
                            CoordinatorError,
                            OSError,
                            UnicodeDecodeError,
                            json.JSONDecodeError,
                            TypeError,
                            ValueError,
                        ):
                            predecessor_review = None
                        if (
                            predecessor_review is not None
                            and predecessor_review.computed_hash == terminal_revision.review_hash
                        ):
                            predecessor_product_review_ref = expected_review_ref
                            predecessor_product_review_hash = predecessor_review.computed_hash

            # Run the pure role-session admission before touching the Product
            # revision store.  A completed Product owner may be refreshed only
            # when the prior Product regeneration request is durably failed and
            # the exact Product owner has no active reservation; unrelated or
            # live rows fail closed without any revision files being created.
            registry = RoleSessionRegistry(self.context)
            document = registry._read_unlocked()  # noqa: SLF001 - coordinator lock is shared
            preflight_action = PlannerAction(
                "build_product_candidate",
                "product_agent",
                run_id,
                f"operator requested Product regeneration: {reason}",
                priority=59,
                metadata={"generation_id": generation_id},
            )
            registry._preflight_product_regeneration_unlocked(  # noqa: SLF001
                document,
                preflight_action,
                generation_id=generation_id,
                idempotency_key=requested_token,
                terminal_request=terminal_request,
            )

            # Only after the owner boundary is clear may a legacy root be
            # adopted or a pending target be created.  If a pointer appeared
            # between the pure read and this load, ProductReviewStore's own
            # compare-and-set validation remains authoritative.
            try:
                active_pointer = product_store.load_active_revision()
                if active_pointer is not None:
                    prior_candidate_hash = active_pointer.candidate_hash
                    prior_review_hash = active_pointer.review_hash
                    active_revision_identity = {
                        "revision_id": active_pointer.revision_id,
                        "revision_hash": active_pointer.revision_hash,
                        "candidate_hash": active_pointer.candidate_hash,
                        "review_hash": active_pointer.review_hash,
                    }
                else:
                    active_revision_identity = None
            except (CoordinatorError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise CoordinatorConflictError("Product regeneration revision transaction is unavailable") from exc

            if requested_token is None:
                # This branch is normally unreachable because the omitted-key
                # derivation above uses the pure pointer.  Keep the guard
                # explicit so a future ProductReviewStore implementation
                # cannot proceed with an unbound token.
                raise CoordinatorIntegrityError("Product regeneration idempotency key is unavailable")
            # Adopt any existing root candidate/review and create the target
            # Product revision before writing coordinator authorization.  The
            # store owns this transaction and keeps the prior evidence
            # immutable while the new revision is pending.
            try:
                target_revision = product_store.begin_revision(
                    request_id=requested_token,
                    input_fingerprint=input_fingerprint,
                    implementation_identity=implementation_identity,
                    prior_revision_id=(active_pointer.revision_id if active_pointer is not None else None),
                    prior_candidate_hash=prior_candidate_hash,
                    prior_review_hash=prior_review_hash,
                )
            except (CoordinatorError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise CoordinatorConflictError("Product regeneration revision transaction is unavailable") from exc
            action_reason = f"operator requested Product regeneration: {reason}"
            target_output_root_ref = target_revision.output_root_ref
            if not isinstance(target_output_root_ref, str) or not target_output_root_ref.strip():
                raise CoordinatorIntegrityError("Product regeneration target output namespace is invalid")
            metadata: dict[str, Any] = {
                "generation_id": generation_id,
                # A regeneration candidate owns a revision-scoped immutable
                # bundle.  Never point its manifest back at the mutable
                # generation root; the Product Agent/assembler admission
                # revalidates this exact namespace before writing bytes.
                "product_manifest_ref": f"{target_output_root_ref}/product_manifest.json",
                "output_root_ref": target_output_root_ref,
                "candidate_ref": f"products/generations/{generation_id}/product_revisions/{target_revision.revision_id}/product_candidate.json",
                "review_ref": f"products/generations/{generation_id}/product_revisions/{target_revision.revision_id}/product_review.json",
                "authorization_ref": f"products/generations/{generation_id}/product_revisions/{target_revision.revision_id}/publish_authorization.json",
                "presentation_inventory_ref": product.get("presentation_inventory_ref"),
                "presentation_plan_ref": product.get("presentation_plan_ref"),
                "input_fingerprint": input_fingerprint,
                "item_ids": list(item_ids),
                "item_bindings": dict(item_bindings),
                "failed_items": list(product.get("preview_failed_items") or ()),
                "limitations": list(product.get("preview_limitations") or ()),
                "authorization_origin": PRODUCT_REGENERATION_ORIGIN,
                "product_regeneration_request_id": requested_token,
                "product_revision_id": target_revision.revision_id,
                "product_revision_ref": f"products/generations/{generation_id}/product_revisions/{target_revision.revision_id}/revision.json",
                "prior_revision_id": target_revision.prior_revision_id,
            }
            if predecessor_product_review_ref is not None and predecessor_product_review_hash is not None:
                metadata.update(
                    {
                        "predecessor_product_review_ref": predecessor_product_review_ref,
                        "predecessor_product_review_hash": predecessor_product_review_hash,
                    }
                )
            if prior_candidate_hash is not None:
                metadata["candidate_hash"] = prior_candidate_hash
            if prior_review_hash is not None:
                metadata["review_hash"] = prior_review_hash
            action = PlannerAction(
                "build_product_candidate",
                "product_agent",
                run_id,
                action_reason,
                priority=59,
                metadata=metadata,
            )
            action_fingerprint = _action_key(action)
            if not _is_sha256(action_fingerprint):
                raise CoordinatorIntegrityError("Product regeneration action fingerprint is invalid")
            state_fingerprint = self._product_regeneration_state_fingerprint(state)
            if not _is_sha256(state_fingerprint):
                raise CoordinatorIntegrityError("Product regeneration state fingerprint is invalid")

            logical_owner = _role_session_identity(
                action,
                run_id=run_id,
                generation_id=generation_id,
            )
            if logical_owner is None:
                raise CoordinatorIntegrityError("Product regeneration logical owner is invalid")
            owner_key = logical_owner[0]
            # A prior requested/dispatched record is a one-shot boundary. A
            # different idempotency key cannot overwrite it while it remains
            # pending or in-flight, even if a caller supplied a new reason.
            if isinstance(existing_request, Mapping) and existing_request.get("status") in {"requested", "dispatched"}:
                if existing_request.get("request_id") == requested_token:
                    return self._status_from_state(state)
                raise CoordinatorConflictError("a different Product regeneration request is already recorded")

            registry_result = registry._request_product_regeneration_unlocked(  # noqa: SLF001
                document,
                action,
                generation_id=generation_id,
                idempotency_key=requested_token,
                terminal_request=terminal_request,
            )
            if registry_result.get("mode") == "blocked":
                raise CoordinatorConflictError("Product Agent session reservation is active")
            # The registry write is its own durable admission boundary.  A
            # process may be interrupted before the coordinator request event
            # is appended; replay of the same token must then reconcile the
            # pending Product revision and emit exactly one request event.
            self._legacy_failpoint("product_regeneration_after_registry")

            auth: dict[str, Any] = {
                "role": logical_owner[1],
                "subject_id": action.subject_id,
                "generation_id": generation_id,
                "run_id": run_id,
                "authorization_origin": PRODUCT_REGENERATION_ORIGIN,
                "request_id": requested_token,
                "input_fingerprint": input_fingerprint,
                "implementation_identity": implementation_identity,
                "action_fingerprint": action_fingerprint,
                "state_fingerprint": state_fingerprint,
                "prior_candidate_hash": prior_candidate_hash,
                "prior_review_hash": prior_review_hash,
                "revision_id": target_revision.revision_id,
                "prior_revision_id": target_revision.prior_revision_id,
                "output_root_ref": target_output_root_ref,
            }
            if predecessor_product_review_ref is not None and predecessor_product_review_hash is not None:
                auth.update(
                    {
                        "predecessor_product_review_ref": predecessor_product_review_ref,
                        "predecessor_product_review_hash": predecessor_product_review_hash,
                    }
                )
            authorizations = state.setdefault("replacement_authorizations", {})
            if not isinstance(authorizations, dict):
                authorizations = {}
                state["replacement_authorizations"] = authorizations
            authorizations[owner_key] = auth
            regeneration_request = {
                "status": "requested",
                "authorization_origin": PRODUCT_REGENERATION_ORIGIN,
                "request_id": requested_token,
                "run_id": run_id,
                "generation_id": generation_id,
                "input_fingerprint": input_fingerprint,
                "implementation_identity": implementation_identity,
                "action_fingerprint": action_fingerprint,
                "state_fingerprint": state_fingerprint,
                "prior_candidate_hash": prior_candidate_hash,
                "prior_review_hash": prior_review_hash,
                "revision_id": target_revision.revision_id,
                "prior_revision_id": target_revision.prior_revision_id,
                "output_root_ref": target_output_root_ref,
                "reason": reason,
            }
            if predecessor_product_review_ref is not None and predecessor_product_review_hash is not None:
                regeneration_request.update(
                    {
                        "predecessor_product_review_ref": predecessor_product_review_ref,
                        "predecessor_product_review_hash": predecessor_product_review_hash,
                    }
                )
            state["product_regeneration"] = regeneration_request
            state["status"] = "ready"
            state["phase"] = "product_regeneration_requested"
            state["publication_ready"] = False
            request_event_payload = {
                "reason": reason,
                "authorization_origin": PRODUCT_REGENERATION_ORIGIN,
                "request_id": requested_token,
                "logical_owner": owner_key,
                "generation_id": generation_id,
                "input_fingerprint": input_fingerprint,
                "implementation_identity": implementation_identity,
                "action_fingerprint": action_fingerprint,
                "state_fingerprint": state_fingerprint,
                "prior_candidate_hash": prior_candidate_hash,
                "prior_review_hash": prior_review_hash,
                "revision_id": target_revision.revision_id,
                "output_root_ref": target_output_root_ref,
            }
            if predecessor_product_review_ref is not None and predecessor_product_review_hash is not None:
                request_event_payload.update(
                    {
                        "predecessor_product_review_ref": predecessor_product_review_ref,
                        "predecessor_product_review_hash": predecessor_product_review_hash,
                    }
                )
            self._append_event_locked(
                state,
                "product_regeneration_requested",
                request_event_payload,
            )
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

    def _publish_rebind_locked(
        self,
        target: CoordinatorRunSpec,
        publisher: Callable[[CoordinatorRunSpec], Any],
        *,
        state: dict[str, Any] | None = None,
        failpoint_prefix: str | None = None,
        retry_reason: str | None = None,
        retry_exception: type[BaseException] | tuple[type[BaseException], ...] | None = None,
        retry_reasons: Mapping[type[BaseException], str] | None = None,
    ) -> CoordinatorStatus:
        """Run the durable generation/spec/state rebind transaction.

        The caller owns ``_locked``.  Both ordinary plan rebinds and canonical
        data-refresh admissions use this one transaction so a crash can leave
        only the existing ``pending_plan_rebind`` intent, never a new spec
        with an old coordinator state.
        """

        if not callable(publisher):
            raise TypeError("publisher must be callable")
        if target.run_id != self.context.run_id:
            raise CoordinatorConflictError("run spec run_id does not match context")
        # A complete production binding may point at a skill tree whose bytes
        # were rotated in place while the coordinator was quiescent.  Loading
        # that raw document through ``CoordinatorRunSpec.from_dict`` would
        # validate the removed bytes before this publication transaction can
        # rotate the transport.  Project only the active target transport and
        # retain the exact raw-spec hash for the pending-intent proof below.
        raw_old_codex: dict[str, Any] | None = None
        raw_value = _load_json(self.spec_path)
        if isinstance(raw_value, Mapping) and not self._looks_like_legacy_spec(raw_value):
            raw_codex = raw_value.get("codex_exec")
            binding_fields = self._binding_fields()
            if (
                isinstance(raw_codex, Mapping)
                and all(raw_codex.get(field_name) is not None for field_name in binding_fields)
                and _canonical(raw_codex) != _canonical(target.codex_exec)
            ):
                self._validate_transport_target(target)
                persisted, persisted_hash = self._load_transport_spec_for_target(target)
                raw_old_codex = dict(_canonical(raw_codex))
            else:
                persisted = self._load_spec_document()
                persisted_hash = self._spec_hash(persisted)
        else:
            persisted = self._load_spec_document()
            persisted_hash = self._spec_hash(persisted)
        if state is None:
            state, _ = self._read_replay()
            assert state is not None
        self._spec = persisted
        self._configure_from_spec(persisted)
        pending = state.get("pending_plan_rebind")
        if pending is not None and not isinstance(pending, Mapping):
            raise CoordinatorIntegrityError("pending plan rebind state is invalid")
        target_hash = self._spec_hash(target)
        if isinstance(pending, Mapping):
            # Keep the prior target identity explicit across both the normal
            # retry (already-current intent) and the skill-rotation retarget
            # path.  In particular, a retry after a crash must not depend on
            # the mismatch branch having run in this process.
            previous_target_hash = pending.get("new_spec_hash")
            if pending.get("new_spec_hash") != target_hash:
                # A plan publication can be interrupted before the active
                # transport release is rotated.  Accept exactly one
                # structured retarget: the pending intent must still name the
                # same run, old persisted spec hash, and from/to planner
                # lineage, while its original target hash must be reproducible
                # by replacing only the target codex transport with the raw
                # persisted binding.  No old skill bytes are loaded here.
                persisted_dict = persisted.to_dict()
                target_dict = target.to_dict()
                from_lineage = pending.get("from")
                to_lineage = pending.get("to")
                old_target_payload = dict(target_dict)
                old_target_payload["codex_exec"] = dict(raw_old_codex or {})
                lineage_fields = ("generation_id", "planner_ref", "planner_hash")
                state_lineage = {
                    field_name: state.get(field_name)
                    for field_name in lineage_fields
                }
                persisted_lineage = {
                    field_name: persisted_dict.get(field_name)
                    for field_name in lineage_fields
                }
                target_lineage = {
                    field_name: target_dict.get(field_name)
                    for field_name in lineage_fields
                }
                # The publisher runs between two durable writes.  A crash
                # before it starts leaves the raw spec on the source
                # generation; a crash after ``_write_spec`` leaves the raw
                # spec on the target generation while state and the pending
                # intent still describe the source.  Both forms are valid,
                # but each must prove its own hash/lineage relationship.
                source_raw_shape = (
                    pending.get("old_spec_hash") == persisted_hash
                    and state.get("spec_hash") == persisted_hash
                    and state_lineage == persisted_lineage
                    and _canonical(from_lineage) == _canonical(persisted_lineage)
                )
                target_raw_shape = (
                    pending.get("new_spec_hash") == persisted_hash
                    and state.get("spec_hash") == pending.get("old_spec_hash")
                    and _canonical(from_lineage) == _canonical(state_lineage)
                    and _canonical(to_lineage) == _canonical(persisted_lineage)
                )
                proven = (
                    raw_old_codex is not None
                    and pending.get("run_id") == target.run_id
                    and state.get("run_id") == target.run_id
                    and isinstance(from_lineage, Mapping)
                    and isinstance(to_lineage, Mapping)
                    and _canonical(to_lineage) == _canonical(target_lineage)
                    and _canonical(from_lineage) != _canonical(to_lineage)
                    # During a pending plan rebind the coordinator state is
                    # still bound to the source spec.  Requiring that exact
                    # hash and source lineage (including in the after-spec
                    # crash window) prevents a hand-edited or cross-run
                    # pending record from being retargeted merely because
                    # its target hash happens to match.
                    and (source_raw_shape or target_raw_shape)
                    and self._spec_hash_from_payload(old_target_payload) == pending.get("new_spec_hash")
                )
                if not proven:
                    raise CoordinatorConflictError("a different plan rebind is already pending")
                pending = dict(_canonical(pending))
                previous_target_hash = pending.get("new_spec_hash")
                pending["new_spec_hash"] = target_hash
            if self._active_entries(state):
                raise CoordinatorConflictError("coordinator cannot rebind while dispatches are active")
            if pending.get("new_spec_hash") == target_hash and previous_target_hash != target_hash:
                state["pending_plan_rebind"] = pending
                self._append_event_locked(
                    state,
                    "plan_rebind_transport_retargeted",
                    {
                        "old_new_spec_hash": previous_target_hash,
                        "new_spec_hash": target_hash,
                    },
                )
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
            # ``persisted`` may be a projected view whose codex transport is
            # already the requested active binding while the raw persisted
            # spec still carries a previous release.  The raw normalized hash
            # is the no-op authority; comparing the projection alone would
            # skip the required rebind and leave state/spec hashes split.
            if persisted_hash == target_hash:
                return self._status_from_state(state)
            lineage = {"generation_id", "planner_ref", "planner_hash"}
            if not any(persisted.to_dict().get(name) != target.to_dict().get(name) for name in lineage):
                raise CoordinatorConflictError("plan rebind requires a changed generation or planner lineage")
            if self._active_entries(state):
                raise CoordinatorConflictError("coordinator cannot rebind while dispatches are active")
            old = persisted
            # ``old`` can be the target-transport projection of a raw spec
            # whose skill bytes were rotated in place.  Preserve that raw
            # spec hash in the durable pending intent so restart recovery can
            # prove the exact source without loading removed bytes.
            payload = self._plan_rebound_payload(old, target, old_spec_hash=persisted_hash)
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
            if failpoint_prefix:
                self._legacy_failpoint(f"{failpoint_prefix}_after_started")

        try:
            publisher(target)
        except Exception as exc:
            if retry_exception is not None and isinstance(exc, retry_exception):
                # An active attempt is a safe-boundary deferral, not a
                # partially-published rebind. Clear the coordinator's
                # temporary rebind intent so later D admissions can coalesce
                # against this same parent; the canonical D admission remains
                # the durable source of the deferred operation.
                reason = retry_reason or "deferred"
                if retry_reasons:
                    for exception_type, exception_reason in retry_reasons.items():
                        if isinstance(exc, exception_type):
                            reason = exception_reason
                            break
                state["pending_plan_rebind"] = None
                state["status"] = "waiting"
                state["phase"] = "data_refresh_pending"
                self._append_diagnostic(
                    state,
                    {
                        "kind": "data_refresh_pending",
                        "reason": reason,
                        "target_spec_hash": target_hash,
                    },
                )
                self._append_event_locked(
                    state,
                    "data_refresh_deferred",
                    {
                        "reason": reason,
                        "target_spec_hash": target_hash,
                    },
                )
                return self._status_from_state(state)
            self._append_diagnostic(
                state,
                {
                    "kind": "plan_rebind_pending",
                    "reason": "publisher_error",
                    "target_spec_hash": target_hash,
                },
            )
            state["status"] = "waiting"
            state["phase"] = "plan_rebind_pending"
            self._append_event_locked(
                state,
                "wait",
                {"reason": "plan_rebind_pending", "pending_reason": "publisher_error", "target_spec_hash": target_hash},
            )
            return self._status_from_state(state)

        payload = self._plan_rebound_payload(old, target, old_spec_hash=str(pending.get("old_spec_hash")))
        self._spec = target
        self._configure_from_spec(target)
        self._write_spec(target)
        self._legacy_failpoint("plan_rebind_after_spec")
        if failpoint_prefix:
            self._legacy_failpoint(f"{failpoint_prefix}_after_spec")
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
                "retry_counts": {},
                "retry_blocked": {},
                "last_control_fingerprint": None,
                "publication_ready": False,
                "attempt": 0,
            }
        )
        self._append_event_locked(state, "plan_rebound", payload)
        self._legacy_failpoint("plan_rebind_after_event")
        if failpoint_prefix:
            self._legacy_failpoint(f"{failpoint_prefix}_after_event")
        return self._status_from_state(state)

    def publish_and_rebind(
        self,
        spec: CoordinatorRunSpec | Mapping[str, Any],
        publisher: Callable[[CoordinatorRunSpec], Any],
    ) -> CoordinatorStatus:
        """Publish a new public generation and atomically bind the coordinator."""

        target = spec if isinstance(spec, CoordinatorRunSpec) else CoordinatorRunSpec.from_dict(spec)
        if target.run_id != self.context.run_id:
            raise CoordinatorConflictError("run spec run_id does not match context")
        with self._locked(create=False):
            recovered = self._recover_legacy_import_locked()
            if recovered is not None:
                raise CoordinatorConflictError("cannot publish while legacy import is pending")
            return self._publish_rebind_locked(target, publisher)

    def rebind_transport(
        self,
        spec: CoordinatorRunSpec | Mapping[str, Any],
    ) -> CoordinatorStatus:
        """Quiescently update ``codex_exec`` without changing run progress.

        This public transaction is intentionally narrower than
        ``publish_and_rebind``: planner lineage and every non-transport spec
        field must remain identical, no dispatch may be active, and the
        append-only coordinator chain records the old/new specification
        hashes.  Retries of the same target are idempotent.
        """

        target = spec if isinstance(spec, CoordinatorRunSpec) else CoordinatorRunSpec.from_dict(spec)
        if target.run_id != self.context.run_id:
            raise CoordinatorConflictError("run spec run_id does not match context")
        self._validate_transport_target(target)
        with self._locked(create=False):
            self._recover_legacy_import_locked()
            self._recover_binding_upgrade_locked()
            state, _ = self._read_replay()
            assert state is not None
            pending = state.get("pending_transport_rebind")
            target_hash = self._spec_hash(target)
            if isinstance(pending, Mapping) and pending.get("new_spec_hash") != target_hash:
                raise CoordinatorConflictError("a different transport rebind is already pending")
            if pending is not None and not isinstance(pending, Mapping):
                raise CoordinatorIntegrityError("pending transport rebind state is invalid")
            self._recover_transport_rebind_locked()
            state, _ = self._read_replay()
            assert state is not None
            persisted, persisted_hash = self._load_transport_spec_for_target(target)
            state, _ = self._read_replay()
            assert state is not None
            self._verify_planner_binding(state)
            if isinstance(state.get("pending_binding_upgrade"), Mapping):
                raise CoordinatorConflictError("coordinator has a pending binding upgrade")
            if isinstance(state.get("pending_plan_rebind"), Mapping):
                raise CoordinatorConflictError("coordinator has a pending plan rebind")
            # A supervisor that was interrupted can leave an active dispatch
            # claim behind even though its process is gone. Clear only claims
            # that the exact process-identity helper proves orphaned; live or
            # ambiguous claims remain authoritative and keep this transaction
            # quiescent. The cleanup event is committed under this same lock
            # before the active-dispatch admission check below.
            self._clear_orphaned_dispatches_for_transport_rebind_locked(state)
            if self._active_entries(state):
                raise CoordinatorConflictError("coordinator cannot rebind transport while dispatches are active")
            if state.get("spec_hash") != persisted_hash:
                raise CoordinatorConflictError("coordinator specification hash is stale")
            persisted_dict = persisted.to_dict()
            target_dict = target.to_dict()
            lineage = ("run_id", "generation_id", "planner_ref", "planner_hash")
            if any(persisted_dict.get(name) != target_dict.get(name) for name in lineage):
                raise CoordinatorConflictError("transport rebind requires unchanged planner lineage")
            for field_name in persisted_dict:
                if field_name != "codex_exec" and persisted_dict.get(field_name) != target_dict.get(field_name):
                    raise CoordinatorConflictError("transport rebind would change non-transport specification fields")
            # ``persisted`` is projected with the requested transport so its
            # outer fields can be validated without touching removed old skill
            # bytes.  The raw hash is the source-of-truth distinction between
            # an already-applied target and an old transport release.
            if persisted_hash == target_hash:
                self._spec = persisted
                self._configure_from_spec(persisted)
                return self._status_from_state(state)
            intent, intent_hash = self._transport_rebind_intent_document(
                target,
                old_spec_hash=persisted_hash,
                new_spec_hash=target_hash,
                prior_state=state,
            )
            self._write_transport_rebind_intent_locked(intent, intent_hash)
            self._legacy_failpoint("transport_rebind_after_intent")
            payload = {
                "run_id": target.run_id,
                "old_spec_hash": persisted_hash,
                "new_spec_hash": target_hash,
                "transport_fields": ["codex_exec"],
                "from": dict(intent["from"]),
                "to": dict(intent["to"]),
                "intent_ref": self._transport_rebind_intent_ref(),
                "intent_sha256": intent_hash,
                "prior_state": dict(intent["prior_state"]),
            }
            state["pending_transport_rebind"] = payload
            state["status"] = "waiting"
            state["phase"] = "transport_rebind_pending"
            self._append_event_locked(state, "coordinator_transport_rebind_started", payload)
            self._legacy_failpoint("transport_rebind_after_started")
            self._write_spec(target)
            self._legacy_failpoint("transport_rebind_after_spec")
            state.update(
                {
                    "spec_hash": target_hash,
                    "spec_ref": f"{CONTROL_PLANE_DIRNAME}/{COORDINATOR_SPEC_FILENAME}",
                    "status": str((payload.get("prior_state") or {}).get("status") or "ready"),
                    "phase": str((payload.get("prior_state") or {}).get("phase") or "queued"),
                    "pending_transport_rebind": None,
                    "publication_ready": bool((payload.get("prior_state") or {}).get("publication_ready", False)),
                }
            )
            self._append_event_locked(state, "coordinator_transport_rebound", payload)
            self._legacy_failpoint("transport_rebind_after_event")
            self._remove_transport_rebind_intent_locked()
            self._spec = target
            self._configure_from_spec(target)
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
            self._recover_transport_rebind_locked()
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
                pending_transport = state.get("pending_transport_rebind")
                if isinstance(pending_transport, Mapping):
                    if pending_transport.get("new_spec_hash") != self._spec_hash(spec):
                        raise CoordinatorConflictError("a different transport rebind is already pending")
                    return self._transport_rebind_status(state)
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
                coordinator._recover_transport_rebind_locked()
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
    "COORDINATOR_ROLE_SESSIONS_FILENAME",
    "COORDINATOR_SPEC_FILENAME",
    "COORDINATOR_STATE_FILENAME",
    "ROLE_SESSION_SCHEMA_VERSION",
    "PRODUCTION_SKILL_VERSION",
    "PRODUCTION_CORE_VERSION",
    "PRODUCTION_SKILL_SHA256",
    "PRODUCTION_SKILL_FILE_COUNT",
    "PRODUCTION_SKILL_NAME",
    "PRODUCTION_RELEASE",
    "PRODUCT_REGENERATION_ORIGIN",
    "ROLE_MODEL_CONTRACT",
    "ROLE_ACTION_CONTRACT",
    "ACTION_ROLE_CONTRACT",
    "PRODUCTION_ROLE_MODEL_CONTRACT",
    "PRODUCTION_ROLE_ACTION_CONTRACT",
    "production_role_routing",
    "role_model_route",
    "role_route_for_action",
    "validate_role_action_contract",
    "logical_action_fingerprint",
    "resolve_production_skill_binding",
    "CoordinatorConflictError",
    "CoordinatorError",
    "CoordinatorIntegrityError",
    "CoordinatorProductionBindingMismatch",
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
    "RoleSessionRegistry",
    "RoleRunner",
    "RunCoordinator",
    "build_role_prompt",
    "start_coordinator",
]
