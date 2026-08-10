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


RUN_STATE_FILENAME = "run_state.json"
_RUN_LOCK_FILENAME = ".run_state.lock"
INVOCATION_LEDGER_FILENAME = "invocation_receipts.jsonl"
RUN_STATES = (
    "initialized",
    "running",
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
        for name in ("invocation_id", "item_id", "role", "route"):
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
        self._receipts: dict[str, AgentInvocationReceipt] = {}
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
    def receipts(self) -> tuple[AgentInvocationReceipt, ...]:
        return tuple(self._receipts.values())

    def append(self, receipt: AgentInvocationReceipt | Mapping[str, Any]) -> AgentInvocationReceipt:
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
        return receipt

    record = append

    def reload(self) -> tuple[AgentInvocationReceipt, ...]:
        self._receipts = {}
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
        return self.receipts

    def get(self, invocation_id: str) -> AgentInvocationReceipt:
        key = str(invocation_id).strip()
        try:
            return self._receipts[key]
        except KeyError as exc:
            raise KeyError(f"unknown invocation_id: {key}") from exc


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


class RunLifecycle:
    """The only writer of top-level ``run_state.json``."""

    def __init__(self, context: RunContext, state: Mapping[str, Any]) -> None:
        self.context = context
        self._state = dict(state)
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
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("item_ids must be non-empty and unique")
        with cls._run_lock(context):
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
        path = cls._state_path(context)
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("run_state.json is invalid") from exc
        if not isinstance(value, Mapping):
            raise ValueError("run_state.json must contain an object")
        return cls(context, value)

    @classmethod
    @contextmanager
    def _run_lock(cls, context: RunContext):
        """Serialize run-state creation, reconciliation, and publication."""

        root = context.run_root
        root.mkdir(parents=True, exist_ok=True)
        lock_path = context.resolve_run_path(_RUN_LOCK_FILENAME)
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
        path = context.resolve_run_path(RUN_STATE_FILENAME)
        if path.is_symlink():
            raise AllowedRootError("run_state.json cannot be a symlink")
        return path

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
        if set(state) != expected:
            raise ValueError("run_state.json fields are invalid")
        if state.get("run_id") != context.run_id:
            raise ValueError("run_state.json run_id does not match context")
        if Path(str(state.get("run_root"))).expanduser().resolve(strict=False) != context.run_root:
            raise ValueError("run_state.json run_root does not match context")
        mode = state.get("mode")
        if mode not in _VALID_MODES:
            raise ValueError("run_state.json mode is invalid")
        item_ids = state.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids or len(set(item_ids)) != len(item_ids):
            raise ValueError("run_state.json item_ids are invalid")
        for item_id in item_ids:
            if not isinstance(item_id, str):
                raise ValueError("run_state.json item_ids are invalid")
            _simple_component(item_id, "item_id")
        if state.get("status") not in _RUN_STATE_SET:
            raise ValueError("run_state.json state is invalid")
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
        _atomic_write_json(self._state_path(self.context), candidate)
        self._state = candidate

    def _write_state(self, state: Mapping[str, Any]) -> None:
        with self._run_lock(self.context):
            self._reload_authoritative_unlocked()
            self._write_state_unlocked(state)

    def _reload_authoritative_unlocked(self) -> None:
        authoritative = self._load_unlocked(self.context)
        self._state = dict(authoritative._state)

    def _advance_unlocked(self, target: str) -> None:
        if target not in _RUN_STATE_SET:
            raise ValueError("unknown run state")
        current = self.state
        if current == target:
            return
        reachable = _TRANSITIONS.get(current, ())
        if target not in reachable:
            raise ValueError(f"invalid run lifecycle transition: {current} -> {target}")
        state = dict(self._state)
        state["status"] = target
        self._write_state_unlocked(state)

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
        return lifecycle if lifecycle in {"accepted", "accepted_with_limits", "technical_failure"} else None

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

        if product_terminal_status is None and product_status is not None:
            product_terminal_status = product_status
        if optimizer_terminal is None and optimizer_status is not None:
            optimizer_terminal = optimizer_status
        item_states = [self._coerce_item_state(item) for item in items]
        if tuple(sorted(str(item.get("item_id")) for item in item_states)) != tuple(sorted(self.item_ids)):
            raise ValueError("reconcile item IDs do not match run lifecycle")
        outcomes = [self._outcome(item) for item in item_states]
        all_business_terminal = all(outcome in {"accepted", "accepted_with_limits", "technical_failure"} for outcome in outcomes)
        accepted_items = [item for item, outcome in zip(item_states, outcomes) if outcome in {"accepted", "accepted_with_limits"}]
        all_integrated = all(
            item.get("integration_state") in {"integrated", "technical_failure"} for item in accepted_items
        )
        products_terminal = self._is_product_terminal(product_terminal_status)
        limited = any(
            outcome in {"accepted_with_limits", "technical_failure"} for outcome in outcomes
        ) or any(
            item.get("integration_state") == "technical_failure" for item in accepted_items
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
    "InvocationReceiptLedger",
    "classify_invocation_terminal_reason",
    "RUN_STATE_FILENAME",
    "RUN_STATES",
    "RunLifecycle",
    "RunLifecycleSnapshot",
    "classify_terminal_reason",
    "recovery_classification",
]
