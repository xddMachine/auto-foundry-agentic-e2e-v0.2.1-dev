"""Durable registry for *accepted* prepared candidates.

Prepared data is first written by the analysis/workbench owner below an
accepted item's ``work/prepared`` directory.  The candidate descriptor and
payload are deliberately not registry state: :class:`PreparedAssetRegistry`
only records a candidate after an accepted ``IntegrationSession`` has
validated the exact bytes, row count, byte count, scope, and provenance.  The
registry references that immutable, hash-verified candidate in place; it does
not contain a compatibility materializer or a parser/semantic compiler.

The checks here are mechanical integrity checks.  They cannot prove semantic
completeness, so a live Integration Agent and an external test-only fidelity
audit remain required for that judgment.
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

try:  # pragma: no cover - fcntl is present on supported macOS/POSIX hosts
    import fcntl
except ImportError:  # pragma: no cover - defensive for non-POSIX packaging
    fcntl = None  # type: ignore[assignment]

from .contracts import DataAssetRef, PreparedAssetDescriptor
from .workspace import AllowedRootError, RunContext


REGISTRY_SCHEMA_VERSION = "1"
INDEX_SCHEMA_VERSION = "1"
ALLOWED_SCOPES = frozenset({"requirement_scoped", "reusable", "exploratory", "superseded"})
_REGISTRY_RELATIVE = Path("lem") / "prepared_data_registry.jsonl"
_INDEX_RELATIVE = Path("indexes") / "prepared_index.json"
_LOCK_RELATIVE = Path("lem") / ".prepared_data_registry.lock"


class _RegistryCommitAuthority:
    """Opaque capability minted only after an Integration commit intent."""

    __slots__ = ("item_workspace", "session_id", "owner_id", "intent_path", "intent_hash")

    def __init__(
        self,
        item_workspace: Any,
        *,
        session_id: str,
        owner_id: str,
        intent_path: Path,
        intent_hash: str,
    ) -> None:
        self.item_workspace = item_workspace
        self.session_id = str(session_id)
        self.owner_id = str(owner_id)
        self.intent_path = intent_path
        self.intent_hash = str(intent_hash)


def _new_registry_commit_authority(
    item_workspace: Any,
    *,
    session_id: str,
    owner_id: str,
    intent_path: Path,
    intent_hash: str,
) -> _RegistryCommitAuthority:
    """Create the private registry publication capability for one intent."""

    return _RegistryCommitAuthority(
        item_workspace,
        session_id=session_id,
        owner_id=owner_id,
        intent_path=intent_path,
        intent_hash=intent_hash,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    """Write one registry/index file atomically and durably."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(descriptor)
            except OSError:
                pass
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _registry_lock(path: Path):
    """Serialize registry/index publication across concurrent run callers."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _regular_file(context: RunContext, value: str | Path, *, label: str) -> Path:
    """Resolve one run-local regular file without following a symlink.

    ``RunContext.resolve_run_path`` returns a resolved path.  Inspecting the
    lexical path first is therefore necessary to reject a symlink that points
    back inside the run (a symlink target can otherwise look safe after
    resolution).
    """

    raw = Path(value).expanduser()
    lexical = raw if raw.is_absolute() else context.run_root / raw
    try:
        relative = lexical.relative_to(context.run_root)
    except ValueError as exc:
        raise AllowedRootError(f"{label} escapes current run") from exc
    current = context.run_root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise AllowedRootError(f"{label} cannot use a symlink: {current}")
    try:
        resolved = context.resolve_run_path(value)
    except Exception as exc:
        raise AllowedRootError(f"{label} escapes current run") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError(f"{label} must be a regular run-local file: {resolved.name}")
    return resolved


def _decode_rows_bytes(
    encoded: bytes,
    descriptor: PreparedAssetDescriptor,
    *,
    fallback_format: str = "jsonl",
) -> list[dict[str, Any]]:
    fmt = str(descriptor.metadata.get("format", fallback_format)).lower()
    if fmt == "jsonl":
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(encoded.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"prepared JSONL row {line_number} is not an object")
            rows.append(dict(value))
        return rows
    if fmt == "csv":
        return [dict(row) for row in csv.DictReader(io.StringIO(encoded.decode("utf-8")))]
    raise ValueError(f"unsupported prepared asset format: {fmt}")


def _decode_rows(path: Path, descriptor: PreparedAssetDescriptor) -> list[dict[str, Any]]:
    return _decode_rows_bytes(path.read_bytes(), descriptor, fallback_format=path.suffix.lstrip("."))


def _verify_descriptor(context: RunContext, descriptor: PreparedAssetDescriptor, *, item_workspace: Any = None) -> Path:
    if descriptor.scope not in ALLOWED_SCOPES:
        raise ValueError(f"prepared asset scope is invalid: {descriptor.scope!r}")
    raw = Path(descriptor.location).expanduser()
    if not raw.is_absolute() and item_workspace is not None:
        if raw.parts and raw.parts[0] == "work":
            raw = item_workspace.item_root / raw
        elif not (raw.parts and raw.parts[0] in {"questions", "requirements"}):
            raw = item_workspace.work_root / "prepared" / raw
    path = _regular_file(context, raw, label="prepared asset location")
    actual_hash = _sha256(path)
    if descriptor.prepared_content_hash and actual_hash != descriptor.prepared_content_hash:
        raise ValueError(f"prepared asset content changed: {descriptor.prepared_asset_id}")
    if descriptor.byte_count is not None and descriptor.byte_count != path.stat().st_size:
        raise ValueError(f"prepared asset byte count changed: {descriptor.prepared_asset_id}")
    rows = _decode_rows(path, descriptor)
    if descriptor.row_count is not None and descriptor.row_count != len(rows):
        raise ValueError(f"prepared asset row count changed: {descriptor.prepared_asset_id}")
    return path


def _read_descriptor_sidecar(path: Path) -> PreparedAssetDescriptor | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("prepared descriptor sidecar must be an object")
        return PreparedAssetDescriptor.from_dict(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"prepared descriptor sidecar is invalid: {path.name}") from exc


class PreparedAssetRegistry:
    """Durable registry of accepted, run-local prepared descriptors."""

    allowed_scopes = ALLOWED_SCOPES

    def __init__(self, context: RunContext, *, telemetry: Any = None) -> None:
        if not isinstance(context, RunContext):
            raise TypeError("PreparedAssetRegistry requires one RunContext")
        self.context = context
        self.telemetry = telemetry
        self.registry_path = context.resolve_run_path(_REGISTRY_RELATIVE)
        self.index_path = context.resolve_run_path(_INDEX_RELATIVE)
        self.lock_path = context.resolve_run_path(_LOCK_RELATIVE)

    def _read_records(self) -> list[PreparedAssetDescriptor]:
        if not self.registry_path.is_file():
            return []
        records: list[PreparedAssetDescriptor] = []
        seen: set[str] = set()
        try:
            lines = self.registry_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("prepared registry is unreadable") from exc
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                descriptor = PreparedAssetDescriptor.from_dict(payload)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError(f"prepared registry line {line_number} is invalid") from exc
            if descriptor.prepared_asset_id in seen:
                raise ValueError(f"prepared registry contains duplicate asset: {descriptor.prepared_asset_id}")
            seen.add(descriptor.prepared_asset_id)
            records.append(descriptor)
        return records

    @staticmethod
    def _index_entry(descriptor: PreparedAssetDescriptor) -> dict[str, Any]:
        return {
            "prepared_asset_id": descriptor.prepared_asset_id,
            "location": descriptor.location,
            "source_refs": [
                ref.to_dict() if isinstance(ref, DataAssetRef) else str(ref)
                for ref in descriptor.source_refs
            ],
            "prepared_content_hash": descriptor.prepared_content_hash,
            "operation_manifest_hash": descriptor.operation_manifest_hash,
            "source_hashes": list(descriptor.source_hashes),
            "scope": descriptor.scope,
            "status": descriptor.status,
            "core_version": descriptor.core_version,
            "row_count": descriptor.row_count,
            "byte_count": descriptor.byte_count,
            "effective_period": descriptor.effective_period,
        }

    def _write_records(self, records: Iterable[PreparedAssetDescriptor]) -> None:
        ordered = sorted(records, key=lambda descriptor: descriptor.prepared_asset_id)
        content = "".join(
            json.dumps(descriptor.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for descriptor in ordered
        )
        _atomic_write(self.registry_path, content)
        index = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "registry_schema_version": REGISTRY_SCHEMA_VERSION,
            "run_id": self.context.run_id,
            "entries": [self._index_entry(descriptor) for descriptor in ordered],
        }
        _atomic_write(self.index_path, json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    @staticmethod
    def _coerce_descriptor(descriptor: PreparedAssetDescriptor | Mapping[str, Any]) -> PreparedAssetDescriptor:
        return descriptor if isinstance(descriptor, PreparedAssetDescriptor) else PreparedAssetDescriptor.from_dict(descriptor)

    def _validated_descriptor(
        self,
        descriptor: PreparedAssetDescriptor | Mapping[str, Any],
        *,
        require_accepted_item: bool = True,
        item_workspace: Any = None,
    ) -> PreparedAssetDescriptor:
        value = self._coerce_descriptor(descriptor)
        # Registry entries are accepted candidates, never open-ended
        # descriptors.  These three fields are the mechanical content binding
        # used by integration retries and registry loads.
        if value.prepared_content_hash is None:
            raise ValueError("accepted prepared asset requires prepared_content_hash")
        if value.byte_count is None:
            raise ValueError("accepted prepared asset requires byte_count")
        if value.row_count is None:
            raise ValueError("accepted prepared asset requires row_count")
        _verify_descriptor(self.context, value, item_workspace=item_workspace)
        self._validate_candidate_sidecar(value, item_workspace=item_workspace)
        if item_workspace is not None:
            self._validate_candidate_location(value, item_workspace)
        if require_accepted_item:
            self._validate_accepted_item(value, item_workspace=item_workspace)
        return value

    def _validate_candidate_sidecar(self, descriptor: PreparedAssetDescriptor, *, item_workspace: Any = None) -> None:
        """Validate an optional workbench sidecar without trusting it.

        The descriptor supplied to the registry remains authoritative.  A
        malformed or conflicting sidecar is treated as a tamper signal, while
        a missing sidecar is allowed so manually-created test candidates can
        exercise the integration boundary.
        """

        raw = Path(descriptor.location).expanduser()
        if not raw.is_absolute() and item_workspace is not None:
            if raw.parts and raw.parts[0] == "work":
                raw = item_workspace.item_root / raw
            elif not (raw.parts and raw.parts[0] in {"questions", "requirements"}):
                raw = item_workspace.work_root / "prepared" / raw
        payload_path = _regular_file(self.context, raw, label="prepared asset location")
        sidecar_path = payload_path.parent / f"{descriptor.prepared_asset_id}.descriptor.json"
        if not sidecar_path.exists() and not sidecar_path.is_symlink():
            return
        sidecar = _regular_file(self.context, sidecar_path, label="prepared descriptor sidecar")
        loaded = _read_descriptor_sidecar(sidecar)
        if loaded is None or loaded != descriptor:
            raise ValueError(f"prepared descriptor sidecar does not match: {descriptor.prepared_asset_id}")

    def _validate_candidate_location(self, descriptor: PreparedAssetDescriptor, item_workspace: Any) -> Path:
        """Require a descriptor location below one item's work/prepared root."""

        if not hasattr(item_workspace, "item_root") or not hasattr(item_workspace, "work_root"):
            raise TypeError("item_workspace must expose item_root and work_root")
        root = item_workspace.work_root / "prepared"
        raw = Path(descriptor.location).expanduser()
        if raw.is_absolute():
            lexical = raw
        elif raw.parts and raw.parts[0] == "work":
            lexical = item_workspace.item_root / raw
        elif raw.parts and raw.parts[0] in {"questions", "requirements"}:
            lexical = self.context.run_root / raw
        else:
            lexical = root / raw
        _regular_file(self.context, lexical, label="prepared asset location")
        candidate = self.context.resolve_run_path(lexical)
        resolved_root = self.context.resolve_run_path(root)
        if resolved_root != candidate and resolved_root not in candidate.parents:
            raise AllowedRootError("prepared candidate must be under item work/prepared")
        return candidate

    def _validate_accepted_item(self, descriptor: PreparedAssetDescriptor, *, item_workspace: Any = None) -> None:
        """Require the candidate to belong to an accepted item workspace."""

        path = _regular_file(self.context, descriptor.location, label="prepared asset location")
        if item_workspace is not None:
            self._validate_candidate_location(descriptor, item_workspace)
            state = getattr(item_workspace, "state", {})
            lifecycle_state = state.get("lifecycle_state") if isinstance(state, Mapping) else None
            integration_state = getattr(item_workspace, "integration_state", None)
            if lifecycle_state != "accepted" or integration_state not in {"pending", "integrated"}:
                raise ValueError("prepared asset requires an accepted item")
            accepted_root = getattr(item_workspace, "accepted_root", None)
            if accepted_root is None or accepted_root.is_symlink() or not accepted_root.is_dir():
                raise ValueError("prepared asset item acceptance state is missing")
            return
        try:
            relative = path.relative_to(self.context.run_root)
        except ValueError as exc:
            raise AllowedRootError("prepared asset location escapes current run") from exc
        parts = relative.parts
        if len(parts) < 5 or parts[0] not in {"questions", "requirements"} or parts[2:4] != ("work", "prepared"):
            raise ValueError("accepted prepared asset must be under an item work/prepared directory")
        item_root = self.context.run_root / parts[0] / parts[1]
        state_path = item_root / "item_state.json"
        accepted_root = item_root / "accepted"
        if state_path.is_symlink() or accepted_root.is_symlink() or not state_path.is_file() or not accepted_root.is_dir():
            raise ValueError("prepared asset item acceptance state is missing")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("prepared asset item acceptance state is invalid") from exc
        if not isinstance(state, Mapping) or state.get("lifecycle_state") != "accepted":
            raise ValueError("prepared asset requires an accepted item")
        if state.get("integration_state") not in {"pending", "integrated"}:
            raise ValueError("prepared asset item integration is not accepted")

    @staticmethod
    def _validate_commit_authority(
        authority: Any,
        item_workspace: Any,
    ) -> _RegistryCommitAuthority:
        """Require the exact persisted IntegrationSession commit intent."""

        if not isinstance(authority, _RegistryCommitAuthority):
            raise ValueError("accepted registry publication requires IntegrationSession commit authority")
        if authority.item_workspace is not item_workspace:
            raise ValueError("accepted registry publication authority is bound to another item")
        path = authority.intent_path
        if path.is_symlink() or not path.is_file():
            raise ValueError("accepted registry publication requires a persisted commit intent")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("accepted registry publication commit intent is invalid") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("accepted registry publication commit intent is invalid")
        if payload.get("session_id") != authority.session_id or payload.get("owner_id") != authority.owner_id:
            raise ValueError("accepted registry publication commit intent identity is invalid")
        unsigned = {key: value for key, value in payload.items() if key != "intent_hash"}
        expected_hash = hashlib.sha256(
            (
                json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                + "\n"
            ).encode("utf-8")
        ).hexdigest()
        if payload.get("intent_hash") != authority.intent_hash or payload.get("intent_hash") != expected_hash:
            raise ValueError("accepted registry publication commit intent hash is invalid")
        return authority

    def preflight_candidate(
        self,
        descriptor: PreparedAssetDescriptor | Mapping[str, Any],
        item_workspace: Any,
    ) -> PreparedAssetDescriptor:
        """Validate an item-local candidate without requiring acceptance state.

        Candidate publication is owned by the workbench/analysis layer.  This
        method only performs exact descriptor/content checks and the same-ID
        collision check; it never writes registry state and deliberately does
        not infer whether the item is accepted.  The commit path supplies the
        accepted item workspace to :meth:`register_accepted`.
        """

        value = self._validated_descriptor(
            descriptor,
            require_accepted_item=False,
            item_workspace=item_workspace,
        )
        with _registry_lock(self.lock_path):
            records = self._read_records()
            existing = next((record for record in records if record.prepared_asset_id == value.prepared_asset_id), None)
            if existing is not None and existing != value:
                raise ValueError(f"prepared asset already exists with different descriptor: {value.prepared_asset_id}")
            return existing or value

    def preflight_register(
        self,
        descriptor: PreparedAssetDescriptor | Mapping[str, Any],
        *,
        item_workspace: Any = None,
    ) -> PreparedAssetDescriptor:
        """Validate an accepted descriptor without publishing registry state.

        The same-ID decision is made while holding the registry lock so an
        integration owner can preflight a complete bundle before its first
        commit and receive the exact descriptor that would be accepted.
        """

        value = self._validated_descriptor(descriptor, item_workspace=item_workspace)
        with _registry_lock(self.lock_path):
            records = self._read_records()
            existing = next((record for record in records if record.prepared_asset_id == value.prepared_asset_id), None)
            if existing is not None and existing != value:
                raise ValueError(f"prepared asset already exists with different descriptor: {value.prepared_asset_id}")
            return existing or value

    def register_accepted(
        self,
        descriptor: PreparedAssetDescriptor | Mapping[str, Any],
        item_workspace: Any = None,
        *,
        _commit_authority: Any = None,
    ) -> PreparedAssetDescriptor:
        """Register one verified descriptor through an Integration commit only."""

        self._validate_commit_authority(_commit_authority, item_workspace)
        descriptor = self._validated_descriptor(descriptor, item_workspace=item_workspace)
        with _registry_lock(self.lock_path):
            records = self._read_records()
            existing = next((record for record in records if record.prepared_asset_id == descriptor.prepared_asset_id), None)
            if existing is not None:
                if existing != descriptor:
                    raise ValueError(f"prepared asset already exists with different descriptor: {descriptor.prepared_asset_id}")
            else:
                records.append(descriptor)
            # Always rewrite both durable projections, including an exact
            # duplicate.  A crash after the registry file is replaced but
            # before the derived index is written is repaired by the retry.
            self._write_records(records)
        if existing is None:
            self._emit("prepared_registry_registered", descriptor)
            return descriptor
        return existing

    def search(
        self,
        query: str | None = None,
        *,
        prepared_asset_id: str | None = None,
        scope: str | None = None,
        reusable_only: bool = False,
        source_hashes: Iterable[str] | None = None,
        include_superseded: bool = False,
    ) -> tuple[PreparedAssetDescriptor, ...]:
        """Search descriptors without reading prepared payloads."""

        if scope is not None and scope not in ALLOWED_SCOPES:
            raise ValueError(f"prepared asset scope is invalid: {scope!r}")
        wanted_hashes = set(str(value) for value in (source_hashes or ()))
        normalized_query = str(query).strip().lower() if query is not None else ""
        records = self._read_records()
        result: list[PreparedAssetDescriptor] = []
        for descriptor in records:
            if prepared_asset_id is not None and descriptor.prepared_asset_id != str(prepared_asset_id):
                continue
            if scope is not None and descriptor.scope != scope:
                continue
            if reusable_only and (descriptor.scope != "reusable" or descriptor.status == "superseded"):
                continue
            if not include_superseded and (descriptor.scope == "superseded" or descriptor.status == "superseded"):
                continue
            if wanted_hashes and not wanted_hashes.issubset(set(descriptor.source_hashes)):
                continue
            if normalized_query:
                haystack = " ".join(
                    (
                        descriptor.prepared_asset_id,
                        descriptor.location,
                        descriptor.scope,
                        descriptor.status,
                        *(str(value) for value in descriptor.source_hashes),
                        *(str(value) for value in descriptor.source_refs),
                        *(str(value) for value in descriptor.ontology_refs),
                    )
                ).lower()
                if normalized_query not in haystack:
                    continue
            result.append(descriptor)
        return tuple(sorted(result, key=lambda item: item.prepared_asset_id))

    def load(self, prepared_asset_id: str) -> Any:
        """Load and re-verify one explicitly selected prepared asset."""

        matches = self.search(prepared_asset_id=prepared_asset_id, include_superseded=True)
        if not matches:
            raise FileNotFoundError(f"prepared asset is not registered: {prepared_asset_id}")
        descriptor = self._validated_descriptor(matches[0])
        path = _verify_descriptor(self.context, descriptor)
        rows = tuple(_decode_rows(path, descriptor))
        # Import lazily to avoid the workbench -> registry import cycle.
        from .workbench import PreparedAsset

        return PreparedAsset(descriptor=descriptor, rows=rows)

    def _emit(self, event_type: str, descriptor: PreparedAssetDescriptor) -> None:
        if self.telemetry is None:
            return
        try:
            self.telemetry.record(
                event_type,
                rows=descriptor.row_count,
                bytes_processed=descriptor.byte_count,
                output_hashes=(descriptor.prepared_content_hash,) if descriptor.prepared_content_hash else (),
                facts={"prepared_asset_id": descriptor.prepared_asset_id, "scope": descriptor.scope},
            )
        except Exception:
            pass


__all__ = ["ALLOWED_SCOPES", "PreparedAssetRegistry"]
