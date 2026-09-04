"""Program-owned run lifecycle and invocation receipts.

This module is deliberately offline and host-observational.  It persists only
small, hash-bound metadata; it never launches a model, chooses a provider, or
claims telemetry that the host did not supply.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from contextlib import contextmanager
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence

try:  # pragma: no cover - POSIX hosts provide fcntl
    import fcntl
except ImportError:  # pragma: no cover - defensive fallback
    fcntl = None  # type: ignore[assignment]

from .workspace import AllowedRootError, RunContext
from .contracts import ImplementationTransition


RUN_STATE_FILENAME = "run_state.json"
_RUN_LOCK_FILENAME = ".run_state.lock"
INVOCATION_LEDGER_FILENAME = "invocation_receipts.jsonl"
IMPLEMENTATION_TRANSITIONS_FILENAME = "implementation_transitions.jsonl"
_INVOCATION_LEDGER_LOCK_FILENAME = ".invocation_receipts.lock"
ACTIVE_GENERATION_POINTER_FILENAME = "active_generation.json"
GENERATION_DIRECTORY = "extensions"
GENERATION_STATE_FILENAME = RUN_STATE_FILENAME
GENERATION_PLAN_FILENAME = "requirement_supervisor_plan.json"
GENERATION_MANIFEST_FILENAME = "generation_manifest.json"
RUN_STATES = (
    "initialized",
    "running",
    "paused",
    "analytical_complete",
    "integration_complete",
    "products_complete",
    "complete",
    "complete_with_limits",
)
_RUN_STATE_SET = frozenset(RUN_STATES)
_VALID_MODES = frozenset({"question", "requirement"})
_TRANSITIONS = {
    "initialized": ("running",),
    "running": ("analytical_complete",),
    "analytical_complete": ("integration_complete",),
    "integration_complete": ("products_complete",),
    "products_complete": ("complete", "complete_with_limits"),
}
_RECEIPT_FIELDS = (
    "invocation_id",
    "item_id",
    "attempt_id",
    "lane_id",
    "role",
    "route",
    "provider",
    "model",
    "start",
    "first_activity",
    "finish",
    "terminal_reason",
    "provider_error",
    "interrupt_reason",
    "artifact_delta",
    "tool_calls",
)
_RECEIPT_FIELD_SET = frozenset(_RECEIPT_FIELDS)
_LEDGER_RECORD_FIELDS = frozenset({*_RECEIPT_FIELDS, "record_hash"})
_CODING_ERRORS = frozenset({"syntax_error", "name_error", "type_error", "dependency_error"})
_BUSINESS_REVIEW_ERRORS = frozenset(
    {
        "business_review_error",
        "business_review",
        "review_error",
        "review_failed",
        "review_failure",
        "business_validation_error",
        "business_error",
        "review_contract_error",
        "review_repair",
    }
)
_EXECUTION_RECOVERY_REASONS = frozenset(
    {"lane_unavailable", "provider_failure", "host_interruption", "process_lost"}
)
_CORE_DEFECT_REASONS = frozenset(
    {
        "core_defect",
        "core_runtime_defect",
        "runtime_defect",
        "runtime_bug",
        "integrity_failure",
        "contract_violation",
        "program_error",
        "abort_and_new_clean_run",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in sorted(value, key=str)]
    if isinstance(value, Path):
        return str(value)
    return value


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _manifest_hash(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "manifest_hash"}
    return _sha256_bytes(_json_bytes(unsigned))


_GENERATION_POINTER_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "run_id",
        "run_root",
        "generation_id",
        "generation_ordinal",
        "parent_generation_id",
        "state_ref",
        "plan_ref",
        "manifest_ref",
        "generation_manifest_hash",
        "manifest_hash",
    }
)
_GENERATION_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "run_id",
        "run_root",
        "generation_id",
        "generation_ordinal",
        "parent_generation_id",
        "parent_state_hash",
        "parent_plan_hash",
        "added_item_ids",
        "reopened_item_ids",
        "cumulative_item_ids",
        "state_ref",
        "plan_ref",
        "state_manifest_hash",
        "plan_hash",
        "request_hash",
        "data_revision_ref",
        "data_revision_hash",
        "product_manifest_ref",
        "created_at",
        "manifest_hash",
    }
)


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _assert_no_symlink_components(path: Path, *, root: Path) -> None:
    """Reject aliases in a run-local path before trusting its contents."""

    resolved_root = root.expanduser().resolve(strict=False)
    raw_path = path.expanduser()
    try:
        relative = raw_path.relative_to(resolved_root)
    except ValueError as exc:
        raise AllowedRootError("generation path escapes run root") from exc
    current = resolved_root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise AllowedRootError("generation path cannot contain symlinks")


def _resolve_run_path_lexical(
    context: RunContext,
    relative_or_path: str | os.PathLike[str],
    *,
    label: str = "run path",
) -> Path:
    """Validate lexical run-local components before resolving a path.

    ``RunContext.resolve_run_path`` intentionally resolves symlinks for its
    containment check.  Writers that need alias rejection must inspect the
    lexical path first; otherwise a symlink to another run-local file can be
    dereferenced and receive bytes before the caller notices the alias.
    """

    raw = Path(relative_or_path).expanduser()
    candidate = raw if raw.is_absolute() else context.run_root / raw
    try:
        _assert_no_symlink_components(candidate, root=context.run_root)
    except AllowedRootError as exc:
        raise AllowedRootError(f"{label} cannot use a symlink or escape the run root") from exc
    return context.resolve_run_path(relative_or_path)


def current_implementation_identity(context: RunContext) -> tuple[str, str]:
    """Derive the current loaded core's deterministic source/tree identity."""

    package_root = Path(__file__).resolve().parent
    entries: list[dict[str, str]] = []
    for source_path in sorted(package_root.glob("*.py"), key=lambda path: path.name):
        if not source_path.is_file() or source_path.is_symlink():
            continue
        entries.append({"path": source_path.name, "content_hash": hashlib.sha256(source_path.read_bytes()).hexdigest()})
    if not entries:
        raise ValueError("current implementation source manifest is empty")
    encoded_entries = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tree = hashlib.sha1(encoded_entries).hexdigest()
    encoded_identity = json.dumps(
        {"core_version": str(context.core_version), "skill_version": context.skill_version, "tree": tree},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha1(encoded_identity).hexdigest(), tree


def _implementation_pair(skill: Any, core: Any) -> tuple[str, str]:
    skill_value = str(skill)
    core_value = str(core)
    return (
        skill_value if skill_value.startswith("skill") else f"skill{skill_value}",
        core_value if core_value.startswith("core") else f"core{core_value}",
    )


def _transition_version_pair(value: Any) -> tuple[str, str]:
    raw = str(value)
    if "/" not in raw:
        raise ValueError("implementation transition version must be skill/core")
    skill, core = raw.split("/", 1)
    return _implementation_pair(skill, core)


def _implementation_transition_path(context: RunContext) -> Path:
    path = _resolve_run_path_lexical(context, IMPLEMENTATION_TRANSITIONS_FILENAME, label="implementation transition ledger")
    if path.is_symlink():
        raise AllowedRootError("implementation transition ledger cannot be a symlink")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_preserved_transition_admission(
    lifecycle: "RunLifecycle",
    candidate: ImplementationTransition,
    *,
    context: RunContext | None,
) -> None:
    """Validate preserved accepted snapshots before publishing a ledger row.

    A transition ledger entry is an authorization to reuse immutable accepted
    work across an implementation change.  Validate that authorization while
    the run lock is held, before either the ledger or run-state checkpoint can
    be written.  The lifecycle's bound context is authoritative when callers
    do not provide one explicitly; a supplied context must still identify the
    same run so validation cannot read a different workspace root.
    """

    preserved = dict(candidate.preserved_accepted_hashes)
    if not preserved:
        return
    validation_context = context if context is not None else lifecycle.context
    if not isinstance(validation_context, RunContext):
        raise ValueError("implementation transition preservation requires a run context")
    if (
        validation_context.run_id != lifecycle.context.run_id
        or validation_context.run_root != lifecycle.context.run_root
    ):
        raise ValueError("implementation transition preservation context does not match lifecycle")
    item_ids = tuple(str(value) for value in lifecycle.item_ids)
    earliest_item = str(candidate.earliest_affected_item)
    if earliest_item not in item_ids:
        raise ValueError("implementation transition earliest item is not in the lifecycle")
    earliest_position = item_ids.index(earliest_item)
    mode = str(lifecycle.snapshot.mode)
    from .durable import ItemWorkspace
    from .integration import AcceptedAnalysisBundle

    for item_id, expected_hash in preserved.items():
        item_id = str(item_id)
        if item_id not in item_ids:
            raise ValueError("implementation transition preserved accepted item is not in the lifecycle")
        if item_ids.index(item_id) >= earliest_position:
            raise ValueError(
                "implementation transition preserved accepted item must precede the earliest affected item"
            )
        workspace = ItemWorkspace.load(validation_context, item_id, mode=mode)
        if workspace.state.get("lifecycle_state") != "accepted":
            raise ValueError("implementation transition preserved item is not accepted")
        AcceptedAnalysisBundle.load(workspace)
        accepted_manifest = workspace.accepted_root / "manifest.json"
        if accepted_manifest.is_symlink() or not accepted_manifest.is_file():
            raise ValueError("implementation transition accepted manifest is not a regular file")
        if _sha256_file(accepted_manifest) != str(expected_hash):
            raise ValueError("implementation transition preserved accepted hash does not match disk")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(directory)
            except OSError:
                pass
        finally:
            os.close(directory)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, _json_bytes(value))


def _simple_component(value: Any, label: str) -> str:
    result = str(value).strip()
    if (
        not result
        or result in {".", ".."}
        or Path(result).name != result
        or "\\" in result
        or "\x00" in result
    ):
        raise ValueError(f"{label} must be a simple path component")
    return result


def _safe_telemetry_path(context: RunContext, filename: str) -> Path:
    root = context.resolve_run_path("telemetry")
    # Resolve lexical components before creation; a symlinked telemetry root or
    # file is never accepted as a run-local ledger.
    lexical = context.run_root / "telemetry"
    try:
        relative = lexical.relative_to(context.run_root)
    except ValueError as exc:
        raise AllowedRootError("telemetry path escapes run context") from exc
    current = context.run_root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise AllowedRootError(f"telemetry path cannot use symlink: {current}")
    if lexical.exists() and not lexical.is_dir():
        raise ValueError("telemetry root is not a directory")
    path = context.resolve_run_path(Path("telemetry") / filename)
    if path.is_symlink():
        raise AllowedRootError(f"telemetry ledger cannot be a symlink: {path}")
    return path


def classify_terminal_reason(reason: Any) -> str | None:
    """Classify a supplied terminal reason without inventing observability."""

    if not isinstance(reason, str) or not reason.strip():
        return None
    normalized = reason.strip().lower()
    if normalized in _CODING_ERRORS:
        return "same_attempt_feedback"
    if normalized in _BUSINESS_REVIEW_ERRORS:
        return "business_repair"
    if normalized in _EXECUTION_RECOVERY_REASONS:
        return "execution_recovery"
    if normalized in _CORE_DEFECT_REASONS:
        return "abort_and_new_clean_run"
    return None


def recovery_classification(receipt: "AgentInvocationReceipt") -> str | None:
    if not isinstance(receipt, AgentInvocationReceipt):
        raise TypeError("receipt must be AgentInvocationReceipt")
    return classify_terminal_reason(receipt.terminal_reason)


classify_invocation_terminal_reason = classify_terminal_reason


@dataclass(frozen=True)
class AgentInvocationReceipt:
    """Facts for one invocation; provider/model remain unavailable if unknown."""

    invocation_id: str
    item_id: str
    attempt_id: str
    lane_id: str
    role: str
    route: str
    provider: str = "unavailable"
    model: str = "unavailable"
    start: str | None = None
    first_activity: str | None = None
    finish: str | None = None
    terminal_reason: str | None = None
    provider_error: str | None = None
    interrupt_reason: str | None = None
    artifact_delta: Mapping[str, Any] = ()
    tool_calls: Any = ()

    def __post_init__(self) -> None:
        for name in ("invocation_id", "item_id", "attempt_id", "lane_id", "role", "route"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must be non-empty")
            if name == "item_id":
                _simple_component(value, name)
            object.__setattr__(self, name, value)
        for name in ("provider", "model"):
            value = str(getattr(self, name) or "unavailable").strip() or "unavailable"
            object.__setattr__(self, name, value)
        for name in ("start", "first_activity", "finish", "terminal_reason", "provider_error", "interrupt_reason"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string or None")
            if isinstance(value, str):
                object.__setattr__(self, name, value.strip() or None)
        if self.finish is not None and not self.terminal_reason:
            raise ValueError("terminal_reason is required when invocation has finished")
        if isinstance(self.artifact_delta, Mapping):
            object.__setattr__(self, "artifact_delta", copy.deepcopy(dict(self.artifact_delta)))
        elif self.artifact_delta in (None, ()):
            object.__setattr__(self, "artifact_delta", {})
        else:
            raise TypeError("artifact_delta must be a mapping")
        if isinstance(self.tool_calls, (tuple, list)):
            object.__setattr__(self, "tool_calls", tuple(copy.deepcopy(list(self.tool_calls))))
        elif self.tool_calls is None:
            object.__setattr__(self, "tool_calls", ())
        elif isinstance(self.tool_calls, int) and not isinstance(self.tool_calls, bool):
            if self.tool_calls < 0:
                raise ValueError("tool_calls must be nonnegative")
        else:
            raise TypeError("tool_calls must be a sequence or count")

    @property
    def completed(self) -> bool:
        return self.finish is not None and bool(self.terminal_reason)

    @property
    def classification(self) -> str | None:
        return classify_terminal_reason(self.terminal_reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "item_id": self.item_id,
            "attempt_id": self.attempt_id,
            "lane_id": self.lane_id,
            "role": self.role,
            "route": self.route,
            "provider": self.provider,
            "model": self.model,
            "start": self.start,
            "first_activity": self.first_activity,
            "finish": self.finish,
            "terminal_reason": self.terminal_reason,
            "provider_error": self.provider_error,
            "interrupt_reason": self.interrupt_reason,
            "artifact_delta": _jsonable(self.artifact_delta),
            "tool_calls": _jsonable(self.tool_calls),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AgentInvocationReceipt":
        if not isinstance(value, Mapping) or set(value) != _RECEIPT_FIELD_SET:
            raise ValueError("invocation receipt fields are invalid")
        return cls(**dict(value))


class InvocationReceiptLedger:
    """Append-only, fsync-backed invocation receipts under one run telemetry root."""

    def __init__(self, context: RunContext, *, path: str | Path | None = None) -> None:
        if not isinstance(context, RunContext):
            raise TypeError("InvocationReceiptLedger requires a RunContext")
        self.context = context
        self.path = _safe_telemetry_path(context, INVOCATION_LEDGER_FILENAME) if path is None else self._resolve_path(path)
        self._lock_path = (
            _safe_telemetry_path(context, _INVOCATION_LEDGER_LOCK_FILENAME)
            if path is None
            else self.path.with_name(f".{self.path.name}.lock")
        )
        if self._lock_path.is_symlink():
            raise AllowedRootError("invocation ledger lock cannot be a symlink")
        self._receipts: dict[str, AgentInvocationReceipt] = {}
        self._record_hashes: dict[str, str] = {}
        self.reload()

    @classmethod
    def load(cls, context: RunContext, *, path: str | Path | None = None) -> "InvocationReceiptLedger":
        """Load and validate the existing run-local ledger."""

        return cls(context, path=path)

    def _resolve_path(self, path: str | Path) -> Path:
        raw = Path(path)
        resolved = self.context.resolve_run_path(raw)
        try:
            relative = resolved.relative_to(self.context.run_root)
        except ValueError as exc:
            raise AllowedRootError("invocation ledger escapes run context") from exc
        if relative.parts[:1] != ("telemetry",):
            raise AllowedRootError("invocation ledger must live under telemetry")
        if resolved.is_symlink():
            raise AllowedRootError("invocation ledger cannot be a symlink")
        return resolved

    @property
    def _reference_prefix(self) -> str:
        """Return the run-relative ledger path used in stable references."""

        try:
            relative = self.path.relative_to(self.context.run_root)
        except ValueError as exc:  # pragma: no cover - guarded by _resolve_path
            raise AllowedRootError("invocation ledger escapes run context") from exc
        return relative.as_posix()

    def _stable_ref(self, invocation_id: str) -> str:
        return f"{self._reference_prefix}#{invocation_id}"

    @contextmanager
    def _ledger_lock(self):
        """Serialize ledger readers and writers across threads/processes."""

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        if self._lock_path.is_symlink():
            raise AllowedRootError("invocation ledger lock cannot be a symlink")
        with self._lock_path.open("a+b") as stream:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @property
    def receipts(self) -> tuple[AgentInvocationReceipt, ...]:
        """Return an authoritative, lock-protected ledger snapshot."""

        with self._ledger_lock():
            return self._reload_unlocked()

    def _parse_stable_ref(self, receipt_ref: Any) -> str:
        """Validate and extract an invocation ID from one canonical ref.

        Recovery deliberately accepts no invocation-ID shorthand and no path
        aliases.  The reference must point at this exact run-local ledger.
        """

        if not isinstance(receipt_ref, str) or not receipt_ref.strip():
            raise ValueError("receipt_ref must be a stable ledger reference")
        value = receipt_ref.strip()
        prefix = f"{self._reference_prefix}#"
        if not value.startswith(prefix):
            raise ValueError("receipt_ref must use the canonical ledger path")
        invocation_id = value[len(prefix) :]
        if not invocation_id or "#" in invocation_id or "\\" in invocation_id or "/" in invocation_id:
            raise ValueError("receipt_ref invocation_id is invalid")
        return invocation_id

    def append(self, receipt: AgentInvocationReceipt | Mapping[str, Any]) -> str:
        """Persist one completed receipt and return its stable ledger reference.

        The payload hash is computed before the fsync-backed append.  Callers
        must retain the returned reference and use it for any later recovery;
        the receipt object itself is never an authorization token.
        """

        with self._ledger_lock():
            # Refresh before every append while holding the same advisory lock
            # used by readers and other writers.  A ledger object held across
            # concurrent writers therefore cannot append through a duplicate
            # ID or a tampered ledger snapshot.
            self._reload_unlocked()
            if isinstance(receipt, Mapping):
                receipt = AgentInvocationReceipt.from_dict(receipt)
            if not isinstance(receipt, AgentInvocationReceipt):
                raise TypeError("receipt must be AgentInvocationReceipt")
            if not receipt.completed:
                raise ValueError("only completed invocation receipts may be appended")
            if receipt.invocation_id in self._receipts:
                raise ValueError("invocation_id is already recorded")
            payload = receipt.to_dict()
            record = {**payload, "record_hash": _sha256_bytes(_json_bytes(payload))}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise AllowedRootError("invocation ledger cannot be a symlink")
            with self.path.open("ab") as stream:
                stream.write(_json_bytes(record))
                stream.flush()
                os.fsync(stream.fileno())
            self._receipts[receipt.invocation_id] = receipt
            self._record_hashes[receipt.invocation_id] = record["record_hash"]
            return self._stable_ref(receipt.invocation_id)

    record = append

    def reload(self) -> tuple[AgentInvocationReceipt, ...]:
        with self._ledger_lock():
            return self._reload_unlocked()

    def _reload_unlocked(self) -> tuple[AgentInvocationReceipt, ...]:
        self._receipts = {}
        self._record_hashes = {}
        if not self.path.exists():
            return ()
        if self.path.is_symlink() or not self.path.is_file():
            raise AllowedRootError("invocation ledger is not a regular file")
        try:
            lines = self.path.read_bytes().splitlines()
        except OSError as exc:
            raise ValueError("invocation ledger cannot be read") from exc
        for line_number, line in enumerate(lines, 1):
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invocation ledger line {line_number} is invalid") from exc
            if not isinstance(value, Mapping) or set(value) != _LEDGER_RECORD_FIELDS:
                raise ValueError(f"invocation ledger line {line_number} fields are invalid")
            payload = {key: value[key] for key in _RECEIPT_FIELDS}
            expected = _sha256_bytes(_json_bytes(payload))
            if value.get("record_hash") != expected:
                raise ValueError(f"invocation ledger line {line_number} hash does not match content")
            receipt = AgentInvocationReceipt.from_dict(payload)
            if not receipt.completed:
                raise ValueError("invocation ledger contains an incomplete receipt")
            if receipt.invocation_id in self._receipts:
                raise ValueError("invocation ledger contains duplicate invocation_id")
            self._receipts[receipt.invocation_id] = receipt
            self._record_hashes[receipt.invocation_id] = expected
        return tuple(self._receipts.values())

    def resolve(self, receipt_ref: str) -> tuple[AgentInvocationReceipt, str]:
        """Resolve one exact, persisted reference and return receipt + hash."""

        with self._ledger_lock():
            # Re-read the ledger at the authorization boundary while holding
            # the reader lock.  This closes the gap where a previously-created
            # ledger object would otherwise trust a receipt after its JSONL
            # line had been edited or replaced, and prevents a partial append
            # from being observed.
            self._reload_unlocked()
            key = self._parse_stable_ref(receipt_ref)
            try:
                receipt = self._receipts[key]
                return receipt, self._record_hashes[key]
            except KeyError as exc:
                raise KeyError(f"unknown receipt_ref: {receipt_ref}") from exc

    def get(self, receipt_ref: str) -> AgentInvocationReceipt:
        """Resolve a canonical stable reference to its persisted receipt."""

        receipt, _record_hash = self.resolve(receipt_ref)
        return receipt

    def record_hash(self, receipt_ref: str) -> str:
        """Return the hash bound to one canonical stable reference."""

        _receipt, record_hash = self.resolve(receipt_ref)
        return record_hash


@dataclass(frozen=True)
class RunLifecycleSnapshot:
    run_id: str
    mode: str
    item_ids: tuple[str, ...]
    state: str
    run_root: str
    generation: int
    manifest_hash: str
    created_at: str
    updated_at: str

    @property
    def status(self) -> str:
        """Alias matching the persisted top-level status field."""

        return self.state


@dataclass(frozen=True)
class RunGenerationSnapshot:
    """Hash-bound admission metadata for one cumulative run generation."""

    generation_id: str
    generation_ordinal: int
    parent_generation_id: str
    parent_state_hash: str
    parent_plan_hash: str
    added_item_ids: tuple[str, ...]
    reopened_item_ids: tuple[str, ...]
    cumulative_item_ids: tuple[str, ...]
    state_path: str
    plan_path: str
    manifest_path: str
    state_manifest_hash: str
    plan_hash: str
    request_hash: str
    data_revision_ref: str | None
    data_revision_hash: str | None
    product_manifest_ref: str
    created_at: str
    manifest_hash: str


class RunLifecycle:
    """The only writer of top-level ``run_state.json``."""

    def __init__(
        self,
        context: RunContext,
        state: Mapping[str, Any],
        *,
        state_path: Path | None = None,
        generation: RunGenerationSnapshot | None = None,
    ) -> None:
        self.context = context
        self._state = dict(state)
        self._state_path_override = state_path
        self._generation = generation
        self._validate_state(self._state, context)

    @classmethod
    def create(
        cls,
        context: RunContext,
        item_ids: Iterable[str],
        *,
        mode: str = "question",
    ) -> "RunLifecycle":
        mode = str(mode).strip()
        if mode not in _VALID_MODES:
            raise ValueError("mode must be 'question' or 'requirement'")
        ids = tuple(_simple_component(item_id, "item_id") for item_id in item_ids)
        if len(set(ids)) != len(ids) or (mode == "question" and not ids):
            raise ValueError("item_ids must be unique; Question Mode also requires at least one item")
        with cls._run_lock(context):
            existing_pointer = cls._read_generation_pointer_unlocked(context)
            if existing_pointer is not None:
                existing = cls._load_unlocked(context)
                if tuple(existing.item_ids) != ids or existing.snapshot.mode != mode:
                    raise ValueError("existing run_state identity does not match requested item_ids/mode")
                return existing
            path = cls._state_path(context)
            if path.exists() or path.is_symlink():
                existing = cls._load_unlocked(context)
                if tuple(existing.item_ids) != ids or existing.snapshot.mode != mode:
                    raise ValueError("existing run_state identity does not match requested item_ids/mode")
                return existing
            now = _now()
            state = {
                "run_id": context.run_id,
                "run_root": str(context.run_root),
                "item_ids": list(ids),
                "mode": mode,
                "status": "initialized",
                "generation": 0,
                "created_at": now,
                "updated_at": now,
            }
            state["manifest_hash"] = _manifest_hash(state)
            _atomic_write_json(path, state)
            return cls(context, state)

    @classmethod
    def load(cls, context: RunContext) -> "RunLifecycle":
        with cls._run_lock(context):
            return cls._load_unlocked(context)

    @classmethod
    def _load_unlocked(cls, context: RunContext) -> "RunLifecycle":
        pointer = cls._read_generation_pointer_unlocked(context)
        if pointer is not None:
            generation = cls._load_generation_unlocked(context, pointer)
            return cls._load_state_path_unlocked(context, generation.state_path, generation)
        path = cls._state_path(context)
        return cls._load_state_path_unlocked(context, path, None)

    @classmethod
    def _load_state_path_unlocked(
        cls,
        context: RunContext,
        path: Path,
        generation: RunGenerationSnapshot | None,
    ) -> "RunLifecycle":
        path = Path(path)
        _assert_no_symlink_components(path, root=context.run_root)
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("run_state.json is invalid") from exc
        if not isinstance(value, Mapping):
            raise ValueError("run_state.json must contain an object")
        return cls(context, value, state_path=path, generation=generation)

    @classmethod
    def _pointer_path(cls, context: RunContext) -> Path:
        raw = context.run_root / ACTIVE_GENERATION_POINTER_FILENAME
        _assert_no_symlink_components(raw, root=context.run_root)
        path = _resolve_run_path_lexical(context, ACTIVE_GENERATION_POINTER_FILENAME, label="active generation pointer")
        if path.is_symlink():
            raise AllowedRootError("active generation pointer cannot be a symlink")
        return path

    @classmethod
    def _generation_root(cls, context: RunContext, generation_id: str) -> Path:
        generation_id = _simple_component(generation_id, "generation_id")
        if not generation_id.startswith("G-") or not generation_id[2:].isdigit():
            raise ValueError("generation_id must use the G-XXXX form")
        raw = context.run_root / GENERATION_DIRECTORY / generation_id
        _assert_no_symlink_components(raw, root=context.run_root)
        root = _resolve_run_path_lexical(
            context,
            Path(GENERATION_DIRECTORY) / generation_id,
            label="generation root",
        )
        _assert_no_symlink_components(root, root=context.run_root)
        return root

    @classmethod
    def _validate_generation_ref(
        cls,
        context: RunContext,
        value: Any,
        *,
        expected: Path,
        label: str,
    ) -> Path:
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            raise ValueError(f"generation {label} reference is invalid")
        _assert_no_symlink_components(context.run_root / value, root=context.run_root)
        path = _resolve_run_path_lexical(context, value, label=f"generation {label} reference")
        _assert_no_symlink_components(path, root=context.run_root)
        if path != expected:
            raise ValueError(f"generation {label} reference is invalid")
        return path

    @classmethod
    def _read_generation_pointer_unlocked(cls, context: RunContext) -> dict[str, Any] | None:
        path = cls._pointer_path(context)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ValueError("active generation pointer must be a regular file")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("active generation pointer is invalid") from exc
        if not isinstance(value, Mapping) or set(value) != _GENERATION_POINTER_FIELDS:
            raise ValueError("active generation pointer fields are invalid")
        pointer = dict(value)
        if pointer.get("schema_version") != 1 or pointer.get("kind") != "active_generation":
            raise ValueError("active generation pointer kind is invalid")
        if pointer.get("run_id") != context.run_id:
            raise ValueError("active generation pointer run_id does not match context")
        if Path(str(pointer.get("run_root"))).expanduser().resolve(strict=False) != context.run_root:
            raise ValueError("active generation pointer run_root does not match context")
        generation_id = _simple_component(pointer.get("generation_id"), "generation_id")
        suffix = generation_id[2:] if generation_id.startswith("G-") else ""
        if not suffix.isdigit() or int(suffix) < 2 or len(suffix) != 4:
            raise ValueError("active generation pointer generation_id is invalid")
        ordinal = pointer.get("generation_ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal != int(suffix) or ordinal < 2:
            raise ValueError("active generation pointer generation_ordinal is invalid")
        parent_id = _simple_component(pointer.get("parent_generation_id"), "parent_generation_id")
        if not parent_id.startswith("G-") or not parent_id[2:].isdigit():
            raise ValueError("active generation pointer parent_generation_id is invalid")
        generation_root = cls._generation_root(context, generation_id)
        cls._validate_generation_ref(
            context,
            pointer.get("state_ref"),
            expected=generation_root / GENERATION_STATE_FILENAME,
            label="state",
        )
        cls._validate_generation_ref(
            context,
            pointer.get("plan_ref"),
            expected=generation_root / GENERATION_PLAN_FILENAME,
            label="plan",
        )
        manifest_path = cls._validate_generation_ref(
            context,
            pointer.get("manifest_ref"),
            expected=generation_root / GENERATION_MANIFEST_FILENAME,
            label="manifest",
        )
        if not _is_sha256(pointer.get("generation_manifest_hash")):
            raise ValueError("active generation pointer generation_manifest_hash is invalid")
        if not _is_sha256(pointer.get("manifest_hash")) or pointer.get("manifest_hash") != _manifest_hash(pointer):
            raise ValueError("active generation pointer manifest hash does not match content")
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ValueError("active generation pointer manifest is missing")
        if _sha256_file(manifest_path) != pointer["generation_manifest_hash"]:
            raise ValueError("active generation pointer manifest hash does not match disk")
        return pointer

    @classmethod
    def _load_generation_unlocked(
        cls,
        context: RunContext,
        pointer: Mapping[str, Any],
    ) -> RunGenerationSnapshot:
        generation_id = str(pointer["generation_id"])
        generation_root = cls._generation_root(context, generation_id)
        manifest_path = cls._validate_generation_ref(
            context,
            pointer["manifest_ref"],
            expected=generation_root / GENERATION_MANIFEST_FILENAME,
            label="manifest",
        )
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("generation manifest is invalid") from exc
        if not isinstance(value, Mapping) or set(value) != _GENERATION_MANIFEST_FIELDS:
            raise ValueError("generation manifest fields are invalid")
        manifest = dict(value)
        if manifest.get("schema_version") != 1 or manifest.get("kind") != "run_generation":
            raise ValueError("generation manifest kind is invalid")
        if manifest.get("run_id") != context.run_id:
            raise ValueError("generation manifest run_id does not match context")
        if Path(str(manifest.get("run_root"))).expanduser().resolve(strict=False) != context.run_root:
            raise ValueError("generation manifest run_root does not match context")
        if manifest.get("generation_id") != generation_id or manifest.get("generation_ordinal") != pointer["generation_ordinal"]:
            raise ValueError("generation manifest identity does not match pointer")
        if manifest.get("parent_generation_id") != pointer["parent_generation_id"]:
            raise ValueError("generation manifest parent does not match pointer")
        for field_name in ("parent_state_hash", "parent_plan_hash", "state_manifest_hash", "plan_hash", "request_hash"):
            if not _is_sha256(manifest.get(field_name)):
                raise ValueError(f"generation manifest {field_name} is invalid")
        added = manifest.get("added_item_ids")
        reopened = manifest.get("reopened_item_ids")
        cumulative = manifest.get("cumulative_item_ids")
        if (
            not isinstance(added, list)
            or len(set(added)) != len(added)
            or any(not isinstance(item_id, str) for item_id in added)
            or not isinstance(reopened, list)
            or len(set(reopened)) != len(reopened)
            or any(not isinstance(item_id, str) for item_id in reopened)
            or not isinstance(cumulative, list)
            or len(set(cumulative)) != len(cumulative)
            or any(not isinstance(item_id, str) for item_id in cumulative)
        ):
            raise ValueError("generation manifest item IDs are invalid")
        for item_id in (*added, *reopened, *cumulative):
            try:
                _simple_component(item_id, "generation manifest item_id")
            except (TypeError, ValueError) as exc:
                raise ValueError("generation manifest item IDs are invalid") from exc
        if any(item_id not in cumulative for item_id in added):
            raise ValueError("generation manifest added IDs must be present in the current portfolio")
        if any(item_id not in cumulative for item_id in reopened):
            raise ValueError("generation manifest reopened IDs must be present in the current portfolio")
        data_ref = manifest.get("data_revision_ref")
        data_hash = manifest.get("data_revision_hash")
        if data_ref is None:
            if data_hash is not None:
                raise ValueError("generation manifest data revision hash requires a manifest reference")
        else:
            if (
                not isinstance(data_ref, str)
                or not data_ref
                or Path(data_ref).is_absolute()
                or data_ref != Path(data_ref).as_posix()
                or not data_ref.startswith("data_room/revisions/")
                or not data_ref.endswith("/revision_manifest.json")
            ):
                raise ValueError("generation manifest data revision reference is invalid")
            data_parts = Path(data_ref).parts
            revision_id = data_parts[2] if len(data_parts) == 4 else ""
            if (
                len(data_parts) != 4
                or data_parts[:2] != ("data_room", "revisions")
                or not revision_id.startswith("D-")
                or not revision_id[2:].isdigit()
                or len(revision_id[2:]) < 4
                or data_parts[3] != "revision_manifest.json"
            ):
                raise ValueError("generation manifest data revision reference is invalid")
            data_path = _resolve_run_path_lexical(context, data_ref, label="generation data revision manifest")
            _assert_no_symlink_components(data_path, root=context.run_root)
            if data_path.is_symlink() or not data_path.is_file():
                raise ValueError("generation manifest data revision manifest is missing")
            if not _is_sha256(data_hash):
                raise ValueError("generation manifest data revision hash is invalid")
            try:
                data_value = json.loads(data_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("generation manifest data revision manifest is invalid") from exc
            if not isinstance(data_value, Mapping) or data_value.get("manifest_hash") != data_hash or _manifest_hash(data_value) != data_hash:
                raise ValueError("generation manifest data revision hash does not match disk")
        parent_id = str(manifest["parent_generation_id"])
        parent_suffix = parent_id[2:] if parent_id.startswith("G-") else ""
        if not parent_suffix.isdigit() or len(parent_suffix) != 4 or int(parent_suffix) != int(manifest["generation_ordinal"]) - 1:
            raise ValueError("generation manifest parent ordinal is invalid")
        state_path = cls._validate_generation_ref(
            context,
            manifest.get("state_ref"),
            expected=generation_root / GENERATION_STATE_FILENAME,
            label="state",
        )
        plan_path = cls._validate_generation_ref(
            context,
            manifest.get("plan_ref"),
            expected=generation_root / GENERATION_PLAN_FILENAME,
            label="plan",
        )
        expected_product_ref = f"products/generations/{generation_id}/product_manifest.json"
        if manifest.get("product_manifest_ref") != expected_product_ref:
            raise ValueError("generation manifest product_manifest_ref is invalid")
        if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"]:
            raise ValueError("generation manifest created_at is invalid")
        if manifest.get("manifest_hash") != _manifest_hash(manifest):
            raise ValueError("generation manifest hash does not match content")
        if _sha256_file(manifest_path) != pointer["generation_manifest_hash"]:
            raise ValueError("generation manifest hash does not match pointer")
        if not state_path.is_file() or state_path.is_symlink() or not plan_path.is_file() or plan_path.is_symlink():
            raise ValueError("generation state or plan is missing")
        state = cls._read_state_mapping(state_path, context)
        if state.get("run_id") != context.run_id or tuple(state.get("item_ids", ())) != tuple(cumulative) or state.get("mode") != "requirement":
            raise ValueError("generation state identity does not match manifest")
        return RunGenerationSnapshot(
            generation_id=generation_id,
            generation_ordinal=int(manifest["generation_ordinal"]),
            parent_generation_id=str(manifest["parent_generation_id"]),
            parent_state_hash=str(manifest["parent_state_hash"]),
            parent_plan_hash=str(manifest["parent_plan_hash"]),
            added_item_ids=tuple(added),
            reopened_item_ids=tuple(reopened),
            cumulative_item_ids=tuple(cumulative),
            state_path=str(state_path),
            plan_path=str(plan_path),
            manifest_path=str(manifest_path),
            state_manifest_hash=str(manifest["state_manifest_hash"]),
            plan_hash=str(manifest["plan_hash"]),
            request_hash=str(manifest["request_hash"]),
            data_revision_ref=None if data_ref is None else str(data_ref),
            data_revision_hash=None if data_hash is None else str(data_hash),
            product_manifest_ref=str(manifest["product_manifest_ref"]),
            created_at=str(manifest["created_at"]),
            manifest_hash=str(manifest["manifest_hash"]),
        )

    @staticmethod
    def _read_state_mapping(path: Path, context: RunContext) -> dict[str, Any]:
        _assert_no_symlink_components(path, root=context.run_root)
        if path.is_symlink() or not path.is_file():
            raise ValueError("run_state.json must be a regular file")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("run_state.json is invalid") from exc
        if not isinstance(value, Mapping):
            raise ValueError("run_state.json must contain an object")
        RunLifecycle._validate_state(value, context)
        return dict(value)

    @classmethod
    @contextmanager
    def _run_lock(cls, context: RunContext):
        """Serialize run-state creation, reconciliation, and publication."""

        root = context.run_root
        lock_path = _resolve_run_path_lexical(context, _RUN_LOCK_FILENAME, label="run lifecycle lock")
        root.mkdir(parents=True, exist_ok=True)
        if lock_path.is_symlink():
            raise AllowedRootError("run lifecycle lock cannot be a symlink")
        with lock_path.open("a+b") as stream:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _state_path(context: RunContext) -> Path:
        path = _resolve_run_path_lexical(context, RUN_STATE_FILENAME, label="run_state.json")
        if path.is_symlink():
            raise AllowedRootError("run_state.json cannot be a symlink")
        return path

    @classmethod
    def active_generation_metadata(cls, context: RunContext) -> RunGenerationSnapshot | None:
        """Return validated active-generation metadata, if this run has one."""

        with cls._run_lock(context):
            pointer = cls._read_generation_pointer_unlocked(context)
            if pointer is None:
                return None
            return cls._load_generation_unlocked(context, pointer)

    @classmethod
    def active_state_path(cls, context: RunContext) -> Path:
        """Resolve the authoritative state path selected by the pointer."""

        metadata = cls.active_generation_metadata(context)
        return cls._state_path(context) if metadata is None else Path(metadata.state_path)

    @classmethod
    def active_plan_path(cls, context: RunContext) -> Path:
        """Resolve the authoritative requirement plan path for this run."""

        with cls._run_lock(context):
            return cls._active_plan_path_unlocked(context)

    @classmethod
    def _active_plan_path_unlocked(cls, context: RunContext) -> Path:
        """Resolve the authoritative plan while the caller owns ``_run_lock``."""

        pointer = cls._read_generation_pointer_unlocked(context)
        if pointer is None:
            return _resolve_run_path_lexical(context, GENERATION_PLAN_FILENAME, label="requirement supervisor plan")
        metadata = cls._load_generation_unlocked(context, pointer)
        return Path(metadata.plan_path)

    @classmethod
    def active_product_manifest_ref(cls, context: RunContext) -> str:
        """Return the generation-scoped product manifest reference."""

        metadata = cls.active_generation_metadata(context)
        return "products/product_manifest.json" if metadata is None else metadata.product_manifest_ref

    @classmethod
    def active_product_manifest_path(cls, context: RunContext) -> Path:
        reference = cls.active_product_manifest_ref(context)
        return context.resolve_product_path(reference.removeprefix("products/"))

    @classmethod
    def active_generation_id(cls, context: RunContext) -> str:
        metadata = cls.active_generation_metadata(context)
        return "G-0001" if metadata is None else metadata.generation_id

    @classmethod
    def active_generation_ordinal(cls, context: RunContext) -> int:
        metadata = cls.active_generation_metadata(context)
        return 1 if metadata is None else metadata.generation_ordinal

    @staticmethod
    def _validate_state(state: Mapping[str, Any], context: RunContext) -> None:
        expected = {
            "run_id",
            "run_root",
            "item_ids",
            "mode",
            "status",
            "generation",
            "manifest_hash",
            "created_at",
            "updated_at",
        }
        fields = set(state)
        allowed_optional: set[str] = {"resume_status", "pause_reason"}
        if not fields.issubset(expected | allowed_optional) or not expected.issubset(fields):
            raise ValueError("run_state.json fields are invalid")
        if state.get("run_id") != context.run_id:
            raise ValueError("run_state.json run_id does not match context")
        if Path(str(state.get("run_root"))).expanduser().resolve(strict=False) != context.run_root:
            raise ValueError("run_state.json run_root does not match context")
        mode = state.get("mode")
        if mode not in _VALID_MODES:
            raise ValueError("run_state.json mode is invalid")
        item_ids = state.get("item_ids")
        if (
            not isinstance(item_ids, list)
            or len(set(item_ids)) != len(item_ids)
            or (mode == "question" and not item_ids)
        ):
            raise ValueError("run_state.json item_ids are invalid")
        for item_id in item_ids:
            if not isinstance(item_id, str):
                raise ValueError("run_state.json item_ids are invalid")
            _simple_component(item_id, "item_id")
        if state.get("status") not in _RUN_STATE_SET:
            raise ValueError("run_state.json state is invalid")
        resume_status = state.get("resume_status")
        pause_reason = state.get("pause_reason")
        if state.get("status") == "paused":
            if resume_status not in _RUN_STATE_SET - {"paused"}:
                raise ValueError("paused run_state.json resume_status is invalid")
            if pause_reason is not None and not isinstance(pause_reason, str):
                raise ValueError("paused run_state.json pause_reason is invalid")
        elif resume_status is not None or pause_reason is not None:
            raise ValueError("non-paused run_state.json cannot retain pause metadata")
        generation = state.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("run_state.json generation is invalid")
        if state.get("manifest_hash") != _manifest_hash(state):
            raise ValueError("run_state.json manifest hash does not match content")
        for field_name in ("created_at", "updated_at"):
            if not isinstance(state.get(field_name), str) or not state[field_name]:
                raise ValueError(f"run_state.json {field_name} is invalid")

    @property
    def state(self) -> str:
        return str(self._state["status"])

    @property
    def status(self) -> str:
        return self.state

    @property
    def run_state(self) -> str:
        return self.state

    @property
    def item_ids(self) -> tuple[str, ...]:
        return tuple(self._state["item_ids"])

    @property
    def snapshot(self) -> RunLifecycleSnapshot:
        return RunLifecycleSnapshot(
            run_id=self._state["run_id"],
            mode=self._state["mode"],
            item_ids=self.item_ids,
            state=self.state,
            run_root=self._state["run_root"],
            generation=self._state["generation"],
            manifest_hash=self._state["manifest_hash"],
            created_at=self._state["created_at"],
            updated_at=self._state["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    @property
    def generation_id(self) -> str:
        return "G-0001" if self._generation is None else self._generation.generation_id

    @property
    def generation_ordinal(self) -> int:
        return 1 if self._generation is None else self._generation.generation_ordinal

    @property
    def generation_metadata(self) -> RunGenerationSnapshot | None:
        return self._generation

    @property
    def parent_generation_id(self) -> str | None:
        return None if self._generation is None else self._generation.parent_generation_id

    @property
    def parent_state_hash(self) -> str | None:
        return None if self._generation is None else self._generation.parent_state_hash

    @property
    def parent_plan_hash(self) -> str | None:
        return None if self._generation is None else self._generation.parent_plan_hash

    @property
    def added_item_ids(self) -> tuple[str, ...]:
        return () if self._generation is None else self._generation.added_item_ids

    @property
    def reopened_item_ids(self) -> tuple[str, ...]:
        return () if self._generation is None else self._generation.reopened_item_ids

    @property
    def data_revision_ref(self) -> str | None:
        return None if self._generation is None else self._generation.data_revision_ref

    @property
    def data_revision_hash(self) -> str | None:
        return None if self._generation is None else self._generation.data_revision_hash

    @property
    def cumulative_item_ids(self) -> tuple[str, ...]:
        return self.item_ids

    @property
    def state_path(self) -> Path:
        return self._state_path_override or self._state_path(self.context)

    @property
    def plan_path(self) -> Path:
        return self._state_path(self.context).parent / GENERATION_PLAN_FILENAME if self._generation is None else Path(self._generation.plan_path)

    @property
    def product_manifest_ref(self) -> str:
        return "products/product_manifest.json" if self._generation is None else self._generation.product_manifest_ref

    @property
    def product_manifest_path(self) -> Path:
        return self.context.resolve_product_path(self.product_manifest_ref.removeprefix("products/"))

    @property
    def implementation_transitions(self) -> tuple[ImplementationTransition, ...]:
        """Load the append-only implementation/resume checkpoint ledger."""

        path = _implementation_transition_path(self.context)
        if not path.exists():
            return ()
        if not path.is_file() or path.is_symlink():
            raise AllowedRootError("implementation transition ledger is not a regular file")
        records: list[ImplementationTransition] = []
        seen: set[str] = set()
        for line_number, line in enumerate(path.read_bytes().splitlines(), 1):
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"implementation transition line {line_number} is invalid") from exc
            if not isinstance(payload, Mapping) or set(payload) != {
                "transition_id",
                "old_sha",
                "new_sha",
                "old_tree",
                "new_tree",
                "old_version",
                "new_version",
                "earliest_affected_item",
                "preserved_accepted_hashes",
                "unaffected_reason",
                "resume_point",
                "record_hash",
            }:
                raise ValueError(f"implementation transition line {line_number} fields are invalid")
            unsigned = {key: payload[key] for key in payload if key != "record_hash"}
            expected = _sha256_bytes(_json_bytes(unsigned))
            if payload.get("record_hash") != expected:
                raise ValueError(f"implementation transition line {line_number} hash does not match content")
            transition = ImplementationTransition(**dict(unsigned))
            transition_id = transition.transition_id
            if not transition_id or transition_id in seen:
                raise ValueError("implementation transition IDs must be unique")
            seen.add(transition_id)
            records.append(transition)
        return tuple(records)

    def record_implementation_transition(
        self,
        transition: ImplementationTransition | Mapping[str, Any] | None = None,
        *,
        context: RunContext | None = None,
        **fields: Any,
    ) -> ImplementationTransition:
        """Persist an explicit program patch and safe resumable checkpoint.

        The transition is independent of ``run_state.json`` so adding a
        checkpoint cannot silently alter lifecycle status or reinterpret old
        item state.  Re-recording an identical transition is idempotent;
        conflicting reuse of its identity fails closed.
        """

        if transition is None:
            transition = fields
        elif fields:
            raise TypeError("transition mapping and keyword fields cannot both be supplied")
        if isinstance(transition, ImplementationTransition):
            candidate = transition
        elif isinstance(transition, Mapping):
            raw = dict(transition)
            unsigned = {key: value for key, value in raw.items() if key != "transition_id"}
            transition_id = raw.get("transition_id")
            if transition_id is None:
                transition_id = "T-" + _sha256_bytes(_json_bytes(unsigned))[:16]
            candidate = ImplementationTransition(transition_id=str(transition_id), **unsigned)
        else:
            raise TypeError("transition must be ImplementationTransition or mapping")
        if candidate.transition_id is None:
            unsigned = {key: value for key, value in candidate.to_dict().items() if key != "transition_id"}
            candidate = ImplementationTransition(
                transition_id="T-" + _sha256_bytes(_json_bytes(unsigned))[:16],
                **unsigned,
            )
        payload = candidate.to_dict()
        payload["record_hash"] = _sha256_bytes(_json_bytes(payload))
        path = _implementation_transition_path(self.context)
        with self._run_lock(self.context):
            # Re-read under the lifecycle lock to close duplicate-writer races.
            self._reload_authoritative_unlocked()
            _validate_preserved_transition_admission(
                self,
                candidate,
                context=context,
            )
            existing = {value.transition_id: value for value in self.implementation_transitions}
            identity = self._state.get("implementation_identity")
            enforce_origin = self._state.get("portfolio_authority") is not None
            requirement_identity = self._state.get("mode") == "requirement" and isinstance(identity, Mapping)
            if enforce_origin:
                if not isinstance(context, RunContext):
                    raise ValueError("authority-backed implementation transition requires current context")
                if context.run_id != self.context.run_id or context.run_root != self.context.run_root:
                    raise ValueError("implementation transition current context does not match lifecycle")
                expected_new_pair = _implementation_pair(context.skill_version, context.core_version)
                candidate_new_pair = _transition_version_pair(candidate.new_version)
                if candidate_new_pair != expected_new_pair:
                    raise ValueError("implementation transition new version does not match current context")
                expected_new_sha, expected_new_tree = current_implementation_identity(context)
                if candidate.new_sha != expected_new_sha or candidate.new_tree != expected_new_tree:
                    raise ValueError("implementation transition new identity does not match current implementation")
            if candidate.transition_id in existing:
                if existing[candidate.transition_id] != candidate:
                    raise ValueError("implementation transition ID is already recorded with different facts")
                if requirement_identity:
                    old_identity = {
                        "core_version": str(identity["core_version"]),
                        "skill_version": identity.get("skill_version"),
                        "implementation_sha": str(identity["implementation_sha"]),
                        "implementation_tree": str(identity["implementation_tree"]),
                    }
                    candidate_old_skill, candidate_old_core = _transition_version_pair(candidate.old_version)
                    expected_old = {
                        "core_version": candidate_old_core.removeprefix("core"),
                        "skill_version": candidate_old_skill.removeprefix("skill"),
                        "implementation_sha": candidate.old_sha,
                        "implementation_tree": candidate.old_tree,
                    }
                    candidate_new_skill, candidate_new_core = _transition_version_pair(candidate.new_version)
                    new_identity = {
                        "core_version": candidate_new_core.removeprefix("core"),
                        "skill_version": candidate_new_skill.removeprefix("skill"),
                        "implementation_sha": candidate.new_sha,
                        "implementation_tree": candidate.new_tree,
                    }
                    if old_identity == new_identity:
                        # The append already committed and the state advance
                        # was also observed by a retrying caller.  Do not
                        # append a duplicate or rewrite the checkpoint.
                        return existing[candidate.transition_id]
                    if old_identity == expected_old:
                        state = dict(self._state)
                        state["implementation_identity"] = new_identity
                        self._write_state_unlocked(state)
                        return existing[candidate.transition_id]
                    raise ValueError("implementation transition retry does not match run identity")
                return existing[candidate.transition_id]
            if isinstance(identity, Mapping) and enforce_origin:
                old_skill, old_core = _transition_version_pair(candidate.old_version)
                expected_old = _implementation_pair(identity["skill_version"], identity["core_version"])
                if (old_skill, old_core) != expected_old:
                    raise ValueError("implementation transition old version does not match run identity")
                if candidate.old_sha != identity["implementation_sha"] or candidate.old_tree != identity["implementation_tree"]:
                    raise ValueError("implementation transition old identity does not match run identity")
                if candidate.earliest_affected_item not in self.item_ids:
                    raise ValueError("implementation transition item is not in the lifecycle")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise AllowedRootError("implementation transition ledger cannot be a symlink")
            with path.open("ab") as stream:
                stream.write(_json_bytes(payload))
                stream.flush()
                os.fsync(stream.fileno())
            if isinstance(identity, Mapping):
                new_skill, new_core = _transition_version_pair(candidate.new_version)
                state = dict(self._state)
                state["implementation_identity"] = {
                    "core_version": new_core.removeprefix("core"),
                    "skill_version": new_skill.removeprefix("skill"),
                    "implementation_sha": candidate.new_sha,
                    "implementation_tree": candidate.new_tree,
                }
                self._write_state_unlocked(state)
        return candidate

    def _write_state_unlocked(self, state: Mapping[str, Any]) -> None:
        # Compare the authoritative on-disk generation/hash immediately before
        # publication.  A stale in-memory lifecycle can never overwrite a
        # newer run_state snapshot, even if another writer bypassed the lock.
        authoritative = self._load_unlocked(self.context)
        if (
            authoritative._state["generation"] != self._state["generation"]
            or authoritative._state["manifest_hash"] != self._state["manifest_hash"]
        ):
            raise ValueError("run_state CAS failed: authoritative generation changed")
        candidate = copy.deepcopy(dict(state))
        candidate["generation"] = int(self._state["generation"]) + 1
        candidate["updated_at"] = _now()
        candidate["manifest_hash"] = _manifest_hash(candidate)
        self._validate_state(candidate, self.context)
        _atomic_write_json(self.state_path, candidate)
        self._state = candidate

    def _write_state(self, state: Mapping[str, Any]) -> None:
        with self._run_lock(self.context):
            self._reload_authoritative_unlocked()
            self._write_state_unlocked(state)

    def _reload_authoritative_unlocked(self) -> None:
        authoritative = self._load_unlocked(self.context)
        self._state = dict(authoritative._state)
        self._state_path_override = authoritative._state_path_override
        self._generation = authoritative._generation

    def _advance_unlocked(self, target: str) -> None:
        if target not in _RUN_STATE_SET:
            raise ValueError("unknown run state")
        current = self.state
        if current == target:
            return
        if target not in _TRANSITIONS.get(current, ()):
            raise ValueError(f"illegal lifecycle transition: {current} -> {target}")
        state = dict(self._state)
        state["status"] = target
        state.pop("resume_status", None)
        state.pop("pause_reason", None)
        self._write_state_unlocked(state)

    @property
    def paused(self) -> bool:
        return self.state == "paused"

    def pause(self, reason: str | None = None) -> RunLifecycleSnapshot:
        """Pause scheduling without changing item state or derived artifacts."""

        with self._run_lock(self.context):
            self._reload_authoritative_unlocked()
            if self.state == "paused":
                return self.snapshot
            state = dict(self._state)
            state["resume_status"] = self.state
            state["pause_reason"] = None if reason is None else str(reason)
            state["status"] = "paused"
            self._write_state_unlocked(state)
            return self.snapshot

    def resume(self) -> RunLifecycleSnapshot:
        """Resume from the exact status that was active before :meth:`pause`."""

        with self._run_lock(self.context):
            self._reload_authoritative_unlocked()
            if self.state != "paused":
                return self.snapshot
            state = dict(self._state)
            state["status"] = str(state.pop("resume_status", "running"))
            state.pop("pause_reason", None)
            self._write_state_unlocked(state)
            return self.snapshot

    def reopen(self) -> RunLifecycleSnapshot:
        """Make a completed run schedulable again without rewriting its history."""

        with self._run_lock(self.context):
            self._reload_authoritative_unlocked()
            state = dict(self._state)
            state["status"] = "running"
            state.pop("resume_status", None)
            state.pop("pause_reason", None)
            self._write_state_unlocked(state)
            return self.snapshot

    def _advance(self, target: str) -> None:
        with self._run_lock(self.context):
            self._reload_authoritative_unlocked()
            self._advance_unlocked(target)

    @staticmethod
    def _coerce_item_state(value: Any) -> Mapping[str, Any]:
        if hasattr(value, "state"):
            value = value.state
        if not isinstance(value, Mapping):
            raise TypeError("item state must be a mapping or ItemWorkspace")
        return value

    @staticmethod
    def _outcome(item: Mapping[str, Any]) -> str | None:
        terminal = item.get("terminal_outcome")
        if isinstance(terminal, Mapping):
            outcome = terminal.get("outcome")
            if isinstance(outcome, str):
                return outcome
        lifecycle = item.get("lifecycle_state")
        return lifecycle if lifecycle in {"accepted", "accepted_with_limits", "technical_failure", "blocked_by_evidence"} else None

    @staticmethod
    def _is_product_terminal(value: Any) -> bool:
        if isinstance(value, Mapping):
            status = value.get("status", value.get("state"))
            if value.get("terminal") is True:
                return True
            return status in {"complete", "complete_with_limits", "terminal", "accepted", "reviewed", "accepted_with_limits"}
        if isinstance(value, str):
            return value in {"complete", "complete_with_limits", "terminal", "accepted", "reviewed", "accepted_with_limits"}
        return value is True

    def _validated_integration_boundary(
        self,
        item: Any,
        state: Mapping[str, Any],
        *,
        blocked: bool = False,
    ) -> bool:
        """Require the same immutable integration boundary used by Planner.

        Item labels are projections and are not sufficient evidence for a
        lifecycle transition.  When an ``ItemWorkspace`` (or a run-local
        item path) is available, use the pure committed-manifest inspector
        so accepted bundle, records, artifact publication, and item-state
        bindings are checked together.  Mapping-only callers may provide the
        inspector's typed result explicitly; an unvalidated ``integrated``
        label is intentionally not trusted.
        """

        item_id = str(state.get("item_id", ""))
        if not item_id:
            return False
        expected_stage = "not_committed" if blocked else "committed"
        expected_state = "pending" if blocked else "integrated"
        terminal = state.get("terminal_outcome")
        terminal_status = terminal.get("status") if isinstance(terminal, Mapping) else None
        terminal_outcome = terminal.get("outcome") if isinstance(terminal, Mapping) else None
        preacceptance_failure = not blocked and (
            state.get("lifecycle_state") == "technical_failure"
            or terminal_status == "technical_failure"
            or terminal_outcome == "technical_failure"
        )
        if preacceptance_failure:
            # A pre-acceptance failure is a no-integration terminal boundary.
            # Mapping callers must carry the same typed proof that Planner
            # projected; an item label or generic technical-failure state is
            # never sufficient to advance the run lifecycle.
            if (
                state.get("lifecycle_state") != "technical_failure"
                or terminal_status != "technical_failure"
                or terminal_outcome != "technical_failure"
                or state.get("integration_state") != "pending"
            ):
                return False
            expected_stage = "technical_failure"
            expected_state = "pending"
        technical_boundary = (
            not blocked
            and not preacceptance_failure
            and state.get("integration_state") == "technical_failure"
        )
        if technical_boundary:
            expected_stage = "technical_failure"
            expected_state = "technical_failure"
        if state.get("integration_state", "pending") != expected_state:
            return False

        # Prefer the pure on-disk boundary whenever the caller supplies a
        # workspace/context.  It is read-only and performs no projection or
        # telemetry side effects.
        context = getattr(item, "context", None)
        item_root = getattr(item, "item_root", None)
        if isinstance(context, RunContext):
            try:
                from .requirement_planning import inspect_committed_integration

                view = inspect_committed_integration(context, item_id)
            except Exception:
                return False
            return (
                view.get("valid") is True
                and view.get("stage") == expected_stage
                and (
                    not (technical_boundary or preacceptance_failure)
                    or view.get("recovery_exhausted") is True
                )
                and (not preacceptance_failure or view.get("pre_acceptance") is True)
            )
        if isinstance(item_root, (str, Path)):
            try:
                from .requirement_planning import inspect_committed_integration

                view = inspect_committed_integration(Path(item_root), item_id)
            except Exception:
                return False
            return (
                view.get("valid") is True
                and view.get("stage") == expected_stage
                and (
                    not (technical_boundary or preacceptance_failure)
                    or view.get("recovery_exhausted") is True
                )
                and (not preacceptance_failure or view.get("pre_acceptance") is True)
            )

        # A serialized reconciliation caller can carry the pure inspector
        # view alongside its item state.  This remains a typed boundary (the
        # view must explicitly prove validity and the expected stage), unlike
        # trusting the weaker ``integration_state`` label by itself.
        validation_key = "blocked_integration_validation" if blocked else "committed_integration_validation"
        validation = state.get(validation_key)
        return (
            isinstance(validation, Mapping)
            and validation.get("valid") is True
            and validation.get("stage") == expected_stage
            and (
                not (technical_boundary or preacceptance_failure)
                or validation.get("recovery_exhausted") is True
            )
            and (not preacceptance_failure or validation.get("pre_acceptance") is True)
            and (not preacceptance_failure or state.get("integration_stage") == "technical_failure")
        )

    @staticmethod
    def _optimizer_limit(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, Mapping):
            status = value.get("status", value.get("state"))
            return (
                value.get("nonblocking") is True
                or status in {"technical_failure", "failed", "complete_with_limits", "nonblocking_limit"}
                or value.get("limited") is True
            )
        return str(value) in {"technical_failure", "failed", "complete_with_limits", "nonblocking_limit"}

    def reconcile(
        self,
        items: Iterable[Any],
        *,
        product_terminal_status: Any = None,
        optimizer_terminal: Any = None,
        product_status: Any = None,
        optimizer_status: Any = None,
    ) -> RunLifecycleSnapshot:
        with self._run_lock(self.context):
            self._reload_authoritative_unlocked()
            return self._reconcile_unlocked(
                items,
                product_terminal_status=product_terminal_status,
                optimizer_terminal=optimizer_terminal,
                product_status=product_status,
                optimizer_status=optimizer_status,
            )

    def reconcile_from_run(
        self,
        *,
        product_terminal_status: Any = None,
        optimizer_terminal: Any = None,
    ) -> RunLifecycleSnapshot:
        """Refresh top-level lifecycle state from the run's item workspaces.

        This is the ordinary event-loop entry point.  Callers should not have
        to remember to assemble every ``ItemWorkspace`` merely to move a run
        from ``initialized`` to ``running`` or to publish later aggregate
        states.
        """

        from .durable import ItemWorkspace

        items = tuple(
            ItemWorkspace.load(self.context, item_id, mode=self.snapshot.mode)
            for item_id in self.item_ids
        )
        return self.reconcile(
            items,
            product_terminal_status=product_terminal_status,
            optimizer_terminal=optimizer_terminal,
        )

    def _reconcile_unlocked(
        self,
        items: Iterable[Any],
        *,
        product_terminal_status: Any = None,
        optimizer_terminal: Any = None,
        product_status: Any = None,
        optimizer_status: Any = None,
    ) -> RunLifecycleSnapshot:
        """Derive the next valid state from objective item/product facts."""

        if self.state == "paused":
            return self.snapshot

        if product_terminal_status is None and product_status is not None:
            product_terminal_status = product_status
        if optimizer_terminal is None and optimizer_status is not None:
            optimizer_terminal = optimizer_status
        raw_items = tuple(items)
        item_states = [self._coerce_item_state(item) for item in raw_items]
        if tuple(sorted(str(item.get("item_id")) for item in item_states)) != tuple(sorted(self.item_ids)):
            raise ValueError("reconcile item IDs do not match run lifecycle")
        outcomes = [self._outcome(item) for item in item_states]
        all_business_terminal = all(
            outcome in {"accepted", "accepted_with_limits", "technical_failure", "blocked_by_evidence"}
            for outcome in outcomes
        )
        accepted_items = [
            (item, raw)
            for item, raw, outcome in zip(item_states, raw_items, outcomes)
            if outcome in {"accepted", "accepted_with_limits"}
        ]
        blocked_items = [
            (item, raw)
            for item, raw, outcome in zip(item_states, raw_items, outcomes)
            if outcome == "blocked_by_evidence"
        ]
        technical_failure_items = [
            (item, raw)
            for item, raw, outcome in zip(item_states, raw_items, outcomes)
            if outcome == "technical_failure"
        ]
        # Accepted business evidence is never discarded or reclassified as an
        # integration success.  An explicit exhausted technical-failure
        # manifest is a settled limited boundary; historical/recoverable
        # failure evidence remains actionable.
        all_integrated = all(
            self._validated_integration_boundary(raw, item, blocked=False)
            for item, raw in accepted_items
        ) and all(
            self._validated_integration_boundary(raw, item, blocked=True)
            for item, raw in blocked_items
        ) and all(
            self._validated_integration_boundary(raw, item, blocked=False)
            for item, raw in technical_failure_items
        )
        products_terminal = self._is_product_terminal(product_terminal_status)
        limited = any(
            outcome in {"accepted_with_limits", "technical_failure", "blocked_by_evidence"} for outcome in outcomes
        ) or any(
            item.get("integration_state") == "technical_failure" for item, _raw in accepted_items
        ) or self._optimizer_limit(optimizer_terminal)

        if self.state == "initialized":
            self._advance_unlocked("running")
        if self.state == "running" and all_business_terminal:
            self._advance_unlocked("analytical_complete")
        if self.state == "analytical_complete" and all_integrated:
            self._advance_unlocked("integration_complete")
        if self.state == "integration_complete" and products_terminal:
            self._advance_unlocked("products_complete")
        if self.state == "products_complete":
            self._advance_unlocked("complete_with_limits" if limited else "complete")
        return self.snapshot


__all__ = [
    "AgentInvocationReceipt",
    "INVOCATION_LEDGER_FILENAME",
    "IMPLEMENTATION_TRANSITIONS_FILENAME",
    "ImplementationTransition",
    "InvocationReceiptLedger",
    "classify_invocation_terminal_reason",
    "RUN_STATE_FILENAME",
    "RUN_STATES",
    "ACTIVE_GENERATION_POINTER_FILENAME",
    "GENERATION_DIRECTORY",
    "GENERATION_MANIFEST_FILENAME",
    "GENERATION_PLAN_FILENAME",
    "GENERATION_STATE_FILENAME",
    "RunLifecycle",
    "RunGenerationSnapshot",
    "RunLifecycleSnapshot",
    "classify_terminal_reason",
    "recovery_classification",
]
