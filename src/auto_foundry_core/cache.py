"""Run-local immutable content-addressed cache for deterministic operations."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any, Callable, Iterable, Mapping

from .contracts import OperationSpec


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    return value


def _restore(value: Any) -> Any:
    if isinstance(value, Mapping) and set(value) == {"__bytes__"}:
        return base64.b64decode(value["__bytes__"])
    if isinstance(value, Mapping):
        return {k: _restore(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_restore(v) for v in value]
    return value


@dataclass(frozen=True)
class CacheEntry:
    key: str
    value: Any
    status: str
    path: str


class RunCache:
    """An immutable cache whose root must be unique to one run."""

    def __init__(self, root: str | Path, *, core_version: str = "0.1.0", telemetry=None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.core_version = core_version
        self.telemetry = telemetry

    def key(self, spec: OperationSpec | Mapping[str, Any] | str, source_hashes: Iterable[str] = ()) -> str:
        if isinstance(spec, OperationSpec):
            normalized = spec.normalized
        elif isinstance(spec, Mapping):
            normalized = OperationSpec.from_dict(spec).normalized
        else:
            normalized = str(spec)
        payload = {"core_version": self.core_version, "spec": normalized, "source_hashes": sorted(str(v) for v in source_hashes)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("cache key must be a SHA-256 hex digest")
        return self.root / f"{key}.json"

    def get(self, spec: OperationSpec | Mapping[str, Any] | str, source_hashes: Iterable[str] = ()) -> CacheEntry | None:
        key = self.key(spec, source_hashes)
        path = self._path(key)
        if not path.exists():
            self._emit("cache_miss", key, spec, source_hashes)
            return None
        with path.open("r", encoding="utf-8") as stream:
            record = json.load(stream)
        self._emit("cache_hit", key, spec, source_hashes)
        return CacheEntry(key=key, value=_restore(record["value"]), status="hit", path=str(path))

    def put(
        self,
        spec: OperationSpec | Mapping[str, Any] | str,
        source_hashes: Iterable[str],
        value: Any,
        *,
        kind: str = "deterministic",
        metadata: Mapping[str, Any] | None = None,
    ) -> CacheEntry:
        if kind.lower() in {"judgement", "judgment", "agent_judgement", "agent_judgment"}:
            raise ValueError("agent judgement cannot be cached")
        key = self.key(spec, source_hashes)
        path = self._path(key)
        record = {"key": key, "core_version": self.core_version, "kind": kind, "value": _jsonable(value), "metadata": dict(metadata or {})}
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        if path.exists():
            with path.open("r", encoding="utf-8") as stream:
                existing = stream.read()
            if existing != encoded:
                raise RuntimeError(f"immutable cache key already contains a different result: {key}")
        else:
            # Atomic create/rename prevents a partial cache object from being
            # observed by another operation in the same run.
            fd, temporary = tempfile.mkstemp(prefix=f".{key}.", dir=self.root)
            try:
                with open(fd, "w", encoding="utf-8", closefd=True) as stream:
                    stream.write(encoded)
                Path(temporary).replace(path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        self._emit("cache_store", key, spec, source_hashes)
        return CacheEntry(key=key, value=value, status="stored", path=str(path))

    def get_or_compute(
        self,
        spec: OperationSpec | Mapping[str, Any] | str,
        source_hashes: Iterable[str],
        compute: Callable[[], Any],
        *,
        kind: str = "deterministic",
    ) -> CacheEntry:
        source_hashes = tuple(source_hashes)
        hit = self.get(spec, source_hashes)
        if hit is not None:
            return hit
        return self.put(spec, source_hashes, compute(), kind=kind)

    def _emit(self, event_type: str, key: str, spec: Any, source_hashes: Iterable[str]) -> None:
        if self.telemetry is not None:
            try:
                self.telemetry.record(event_type, capability_id=getattr(spec, "capability_id", None), facts={"cache_key": key, "source_hashes": list(source_hashes)})
            except Exception:
                # Telemetry is intentionally passive and must never block a
                # deterministic operation.
                pass


ContentAddressedCache = RunCache

__all__ = ["CacheEntry", "ContentAddressedCache", "RunCache"]
