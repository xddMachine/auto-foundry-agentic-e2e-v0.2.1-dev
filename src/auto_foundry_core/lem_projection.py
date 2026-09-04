"""Deterministic cumulative LEM projection from committed integration records.

Committed item records are the durable authority.  The cumulative Living
Enterprise Model is a read-only materialized view rebuilt in lifecycle order;
it has no independent checkpoint or transaction log that can drift from the
accepted item commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .durable import ItemWorkspace
from .enterprise_model import LivingEnterpriseModel
from .lifecycle import RunLifecycle
from .workspace import AllowedRootError, RunContext


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class LEMCommittedBinding:
    item_id: str
    session_id: str
    manifest_hash: str
    records_hash: str
    accepted_content_hash: str
    accepted_manifest_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "session_id": self.session_id,
            "manifest_hash": self.manifest_hash,
            "records_hash": self.records_hash,
            "accepted_content_hash": self.accepted_content_hash,
            "accepted_manifest_hash": self.accepted_manifest_hash,
        }


@dataclass(frozen=True)
class LEMProjection:
    run_id: str
    item_order: tuple[str, ...]
    bindings: tuple[LEMCommittedBinding, ...]
    model: LivingEnterpriseModel
    projection_hash: str
    resolution_bindings: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> Mapping[str, Any]:
        return MappingProxyType({
            "run_id": self.run_id,
            "item_order": list(self.item_order),
            "bindings": [binding.to_dict() for binding in self.bindings],
            "resolution_bindings": [dict(binding) for binding in self.resolution_bindings],
            "lem_export": self.model.export(),
            "projection_hash": self.projection_hash,
        })


_HISTORY_GENERATION_RE = re.compile(r"^G-(\d{4})$")


class _HistoricalItemWorkspace(ItemWorkspace):
    """Read-only ItemWorkspace view rooted at one immutable history entry."""

    def __init__(
        self,
        context: RunContext,
        item_root: Path,
        item_id: str,
        mode: str,
        state: Mapping[str, Any],
    ) -> None:
        self.context = context
        self.item_id = str(item_id)
        self.mode = str(mode)
        self.original_text = str(state.get("original_text", ""))
        self.telemetry = None
        self._state = dict(state)
        self._historical_root = item_root
        ItemWorkspace._validate_state(
            self._state,
            item_id=self.item_id,
            mode=self.mode,
            original_text=self.original_text,
        )
        self._validate_recovery_authorizations()

    def _resolve_item_subpath(self, relative: str | Path = "") -> Path:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise AllowedRootError("historical item path escapes its archived root")
        candidate = self._historical_root
        if candidate.is_symlink():
            raise AllowedRootError("historical item root cannot be a symlink")
        for component in relative_path.parts:
            candidate = candidate / component
            if candidate.is_symlink():
                raise AllowedRootError(f"historical item path cannot use symlink: {candidate}")
        return candidate

    @property
    def integration_state(self) -> str:
        value = self._state.get("integration_state")
        if value not in {"pending", "integrated", "technical_failure"}:
            raise ValueError("historical item integration_state is invalid")
        return str(value)

    @property
    def integration_manifest_hash(self) -> str | None:
        value = self._state.get("integration_manifest_hash")
        return str(value) if value is not None else None

    @property
    def integration_manifest_ref(self) -> str | None:
        value = self._state.get("integration_manifest_ref")
        return str(value) if value is not None else None


@dataclass(frozen=True)
class _CommittedVersion:
    item_id: str
    generation_id: str | None
    generation_ordinal: int
    lifecycle_order: int
    current_head: bool
    committed_at: datetime
    manifest: Mapping[str, Any]
    bundle: Any
    records: tuple[Any, ...]
    workspace: Any

    @property
    def manifest_hash(self) -> str:
        return str(self.manifest["manifest_hash"])

    @property
    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.committed_at,
            self.generation_ordinal,
            1 if self.current_head else 0,
            self.lifecycle_order,
            self.manifest_hash,
        )


def _history_path(context: RunContext, *parts: str) -> Path:
    current = context.run_root
    for component in parts:
        current = current / component
        if current.is_symlink():
            raise AllowedRootError(f"historical path cannot use symlink: {current}")
    return context.resolve_run_path(Path(*parts))


def _history_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError(f"historical {label} is invalid")
    if "\\" in value or "\x00" in value:
        raise ValueError(f"historical {label} is invalid")
    return value


def _read_history_json(path: Path, *, label: str) -> Mapping[str, Any]:
    if path.is_symlink():
        raise AllowedRootError(f"historical {label} cannot be a symlink: {path}")
    if not path.is_file():
        raise ValueError(f"historical {label} is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"historical {label} is invalid") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"historical {label} is invalid")
    return value


def _discover_history_roots(context: RunContext) -> tuple[tuple[str, str, Path], ...]:
    """Discover only path-confined ``history/requirements/<item>/G-XXXX`` roots."""

    history_root = _history_path(context, "history")
    requirements_root = _history_path(context, "history", "requirements")
    if not history_root.exists() and not history_root.is_symlink():
        return ()
    if history_root.is_symlink() or not history_root.is_dir():
        raise ValueError("historical root must be a regular directory")
    if not requirements_root.exists() and not requirements_root.is_symlink():
        return ()
    if requirements_root.is_symlink() or not requirements_root.is_dir():
        raise ValueError("historical requirements root must be a regular directory")

    discovered: list[tuple[str, str, Path]] = []
    for item_entry in sorted(requirements_root.iterdir(), key=lambda path: path.name):
        if item_entry.is_symlink():
            raise AllowedRootError(f"historical item directory cannot be a symlink: {item_entry}")
        item_id = _history_component(item_entry.name, "item_id")
        if not item_entry.is_dir():
            raise ValueError(f"historical item path is not a directory: {item_entry}")
        for generation_entry in sorted(item_entry.iterdir(), key=lambda path: path.name):
            if generation_entry.is_symlink():
                raise AllowedRootError(f"historical generation cannot be a symlink: {generation_entry}")
            match = _HISTORY_GENERATION_RE.fullmatch(generation_entry.name)
            if match is None or int(match.group(1)) < 2:
                raise ValueError(f"historical generation path is unexpected: {generation_entry}")
            if not generation_entry.is_dir():
                raise ValueError(f"historical generation is not a directory: {generation_entry}")
            discovered.append((item_id, generation_entry.name, generation_entry))
    return tuple(discovered)


def _parse_committed_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("committed integration committed_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("committed integration committed_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("committed integration committed_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _load_archived_version(
    context: RunContext,
    *,
    item_id: str,
    generation_id: str,
    item_root: Path,
    lifecycle_order: int,
    accepted_bundle_cls: Any,
    integration_session_cls: Any,
) -> _CommittedVersion | None:
    """Validate one immutable archived ItemWorkspace and load its records."""

    state = _read_history_json(item_root / "item_state.json", label="item_state.json")
    workspace = _HistoricalItemWorkspace(context, item_root, item_id, "requirement", state)
    committed = integration_session_cls._committed_manifest(workspace)
    lifecycle_state = str(state.get("lifecycle_state", ""))
    if lifecycle_state in {"blocked_by_evidence", "technical_failure"}:
        if committed is not None:
            raise ValueError(f"historical terminal item has committed integration: {item_id}/{generation_id}")
        return None
    if lifecycle_state != "accepted":
        if committed is not None:
            raise ValueError(f"historical non-accepted item has committed integration: {item_id}/{generation_id}")
        return None

    bundle = accepted_bundle_cls.load(workspace)
    if committed is None:
        # An accepted historical root without integration records contributes
        # no LEM bytes, but its terminal binding was still validated above.
        return None
    if committed.get("item_id") != item_id:
        raise ValueError(f"historical committed manifest item is invalid: {item_id}/{generation_id}")
    if committed.get("accepted_content_hash") != bundle.content_hash:
        raise ValueError("historical committed integration accepted content binding is stale")
    if committed.get("accepted_manifest_hash") != bundle.manifest_hash:
        raise ValueError("historical committed integration accepted manifest binding is stale")
    integration_state = workspace.integration_state
    if integration_state not in {"pending", "integrated"}:
        raise ValueError(f"historical committed integration state is invalid: {item_id}/{generation_id}")
    if integration_state == "integrated":
        if workspace.integration_manifest_ref != "integration/committed/manifest.json":
            raise ValueError("historical integrated item manifest ref is invalid")
        if workspace.integration_manifest_hash != committed.get("manifest_hash"):
            raise ValueError("historical integrated item manifest hash is stale")
    records = integration_session_cls._read_records(
        item_root / "integration" / "committed" / "records.jsonl",
        committed,
        bundle,
    )
    binding = LEMCommittedBinding(
        item_id=item_id,
        session_id=str(committed["session_id"]),
        manifest_hash=str(committed["manifest_hash"]),
        records_hash=str(committed["records_hash"]),
        accepted_content_hash=bundle.content_hash,
        accepted_manifest_hash=bundle.manifest_hash,
    )
    return _CommittedVersion(
        item_id=item_id,
        generation_id=generation_id,
        generation_ordinal=int(generation_id[2:]),
        lifecycle_order=lifecycle_order,
        current_head=False,
        committed_at=_parse_committed_at(committed.get("committed_at")),
        manifest=committed,
        bundle=bundle,
        records=tuple(records),
        workspace=workspace,
    )


class LivingEnterpriseModelProjector:
    """Rebuild the cumulative model from validated committed item records."""

    @classmethod
    def project(
        cls,
        context: RunContext,
        *,
        before_item_id: str | None = None,
        include_item_id: str | None = None,
        item_ids: Iterable[str] | None = None,
        _lifecycle: RunLifecycle | None = None,
    ) -> LEMProjection:
        if not isinstance(context, RunContext):
            raise TypeError("LEM projection requires a RunContext")
        if before_item_id is not None and include_item_id is not None:
            raise ValueError("LEM projection cannot combine before_item_id and include_item_id")

        # Context transition publication already owns the run lifecycle lock.
        # Reuse its authoritative snapshot at that boundary instead of
        # reacquiring the advisory lock (which can deadlock on POSIX).
        lifecycle = _lifecycle if _lifecycle is not None else RunLifecycle.load(context)
        if not isinstance(lifecycle, RunLifecycle):
            raise TypeError("LEM projection lifecycle must be a RunLifecycle")
        lifecycle_snapshot = lifecycle.snapshot
        if (
            lifecycle.context.run_id != context.run_id
            or lifecycle.context.run_root != context.run_root
            or lifecycle_snapshot.run_id != context.run_id
            or lifecycle_snapshot.run_root != str(context.run_root)
            or lifecycle_snapshot.mode not in {"question", "requirement"}
            or tuple(lifecycle_snapshot.item_ids) != tuple(lifecycle.item_ids)
        ):
            raise ValueError("LEM projection lifecycle is bound to a different run")
        order = tuple(lifecycle.item_ids)
        target = before_item_id if before_item_id is not None else include_item_id
        if target is not None and target not in order:
            raise ValueError("LEM projection target is outside lifecycle item order")
        selected_ids: frozenset[str] | None = None
        if item_ids is not None:
            selected_ids = frozenset(str(item_id) for item_id in item_ids)
            unknown = selected_ids.difference(order)
            if unknown:
                raise ValueError("LEM projection item selection is outside lifecycle item order")

        # Imported lazily because integration imports this projector.
        from .integration import AcceptedAnalysisBundle, IntegrationSession

        model = LivingEnterpriseModel(run_id=context.run_id)
        bindings: list[LEMCommittedBinding] = []
        # Relationship records are applied in a second phase.  A resolution-
        # only commit can provide a canonical endpoint without an item-local
        # ontology record, while an earlier item relationship may itself be
        # the first durable reference to that endpoint.  Apply every other
        # item-local semantic record in lifecycle/record order first, replay
        # the run-level resolution authority once, then apply relationships in
        # their original order.  The included candidate item is held until
        # this complete prior frontier is materialized.
        deferred_relationships: list[tuple[Any, str]] = []
        deferred_records: tuple[Any, ...] | None = None
        deferred_binding: LEMCommittedBinding | None = None
        deferred_applied_at: str | None = None
        mode = lifecycle.snapshot.mode

        lifecycle_order = {item_id: index for index, item_id in enumerate(order)}
        generation_metadata = lifecycle.generation_metadata
        current_generation_ordinal = (
            int(generation_metadata.generation_ordinal)
            if generation_metadata is not None
            else 1
        )
        history_versions: list[_CommittedVersion] = []
        history_roots: tuple[tuple[str, str, Path], ...] = ()
        if mode == "requirement":
            history_roots = _discover_history_roots(context)
            for item_id, generation_id, item_root in history_roots:
                version = _load_archived_version(
                    context,
                    item_id=item_id,
                    generation_id=generation_id,
                    item_root=item_root,
                    # Removed requirements remain replayable from their
                    # immutable history even after the current lifecycle
                    # frontier no longer lists their item ID.  Unknown
                    # historical items sort deterministically after current
                    # lifecycle IDs when commit timestamps tie.
                    lifecycle_order=lifecycle_order.get(item_id, len(order)),
                    accepted_bundle_cls=AcceptedAnalysisBundle,
                    integration_session_cls=IntegrationSession,
                )
                if version is not None:
                    history_versions.append(version)
        history_item_ids = {item_id for item_id, _generation_id, _item_root in history_roots}

        current_versions: list[_CommittedVersion] = []
        current_heads: dict[str, _CommittedVersion] = {}
        target_index = lifecycle_order.get(target) if target is not None else None
        # Requirement refreshes may leave a later lexical item as the durable
        # predecessor of the included current candidate.  Inspect all current
        # heads in Requirement Mode so chronology, rather than filename order,
        # decides whether they belong to that candidate's prior frontier.
        scan_all_current = mode == "requirement" and target is not None
        for item_position, item_id in enumerate(order):
            if before_item_id is not None and not scan_all_current and item_id == before_item_id:
                break
            if selected_ids is not None and item_id not in selected_ids:
                continue
            try:
                workspace = ItemWorkspace.load(context, item_id, mode=mode)
            except FileNotFoundError as exc:
                if scan_all_current and target_index is not None and item_position > target_index:
                    continue
                # A refresh archives the prior accepted root before creating
                # the next head.  If that head is absent (for example, an
                # interrupted refresh), the archived version remains the sole
                # durable authority; do not turn an otherwise valid history
                # replay into a synthetic lifecycle gap.
                if mode == "requirement" and item_id in history_item_ids:
                    continue
                raise ValueError(f"LEM projection has a missing lifecycle item before the frontier: {item_id}") from exc

            state = workspace.state
            lifecycle_state = str(state.get("lifecycle_state", ""))
            integration_state = str(state.get("integration_state", ""))
            if lifecycle_state in {"blocked_by_evidence", "technical_failure"}:
                if IntegrationSession._committed_manifest(workspace) is not None:
                    raise ValueError(f"terminal non-accepted item cannot have committed integration: {item_id}")
                if integration_state == "integrated":
                    raise ValueError(f"terminal non-accepted item cannot be integrated: {item_id}")
                continue

            if lifecycle_state == "accepted" and integration_state == "technical_failure":
                if IntegrationSession._committed_manifest(workspace) is not None:
                    raise ValueError(f"integration-failed item cannot have committed records: {item_id}")
                bundle = AcceptedAnalysisBundle.load(workspace)
                failure_manifest = IntegrationSession._technical_failure_manifest(workspace, bundle)
                if failure_manifest is None:
                    raise ValueError(f"integration technical failure manifest is missing: {item_id}")
                if workspace.integration_manifest_ref != "integration/technical_failure/manifest.json":
                    raise ValueError(f"integration technical failure manifest ref is invalid: {item_id}")
                if workspace.integration_manifest_hash != failure_manifest["manifest_hash"]:
                    raise ValueError(f"integration technical failure manifest hash is stale: {item_id}")
                continue

            manifest = IntegrationSession._committed_manifest(workspace)
            if manifest is None:
                if include_item_id == item_id and (selected_ids is None or item_id in selected_ids):
                    raise ValueError(f"included LEM item has no committed integration: {item_id}")
                if mode == "requirement":
                    # Requirement items form an independent universe rather
                    # than a serialized execution prefix.  A merely pending
                    # item is therefore a skipped candidate.  Terminal
                    # integration states remain strict: their missing
                    # manifests are corruption, not absence of evidence.
                    if integration_state == "integrated":
                        raise ValueError(f"committed integration manifest is missing: {item_id}")
                    if integration_state == "technical_failure":
                        raise ValueError(f"integration technical failure manifest is missing: {item_id}")
                    continue
                raise ValueError(f"LEM projection encountered an uncommitted lifecycle gap: {item_id}")
            if lifecycle_state != "accepted":
                raise ValueError(f"committed integration item is not analytically accepted: {item_id}")
            if integration_state not in {"pending", "integrated"}:
                raise ValueError(f"committed integration item state is invalid: {item_id}")

            bundle = AcceptedAnalysisBundle.load(workspace)
            if manifest.get("accepted_content_hash") != bundle.content_hash:
                raise ValueError("committed integration accepted content binding is stale")
            if manifest.get("accepted_manifest_hash") != bundle.manifest_hash:
                raise ValueError("committed integration accepted manifest binding is stale")
            records = IntegrationSession._read_records(
                workspace.item_root / "integration" / "committed" / "records.jsonl",
                manifest,
                bundle,
            )
            if manifest.get("item_id") != item_id:
                raise ValueError("committed integration manifest item is out of order")
            if integration_state == "integrated":
                if workspace.integration_manifest_ref != "integration/committed/manifest.json":
                    raise ValueError(f"integrated item manifest ref is invalid: {item_id}")
                if workspace.integration_manifest_hash != manifest["manifest_hash"]:
                    raise ValueError(f"integrated item manifest hash is stale: {item_id}")
            version = _CommittedVersion(
                item_id=item_id,
                generation_id=None,
                generation_ordinal=current_generation_ordinal,
                lifecycle_order=lifecycle_order[item_id],
                current_head=True,
                committed_at=_parse_committed_at(manifest.get("committed_at")),
                manifest=manifest,
                bundle=bundle,
                records=tuple(records),
                workspace=workspace,
            )
            current_versions.append(version)
            current_heads[item_id] = version

            if include_item_id == item_id and not scan_all_current:
                break

        all_versions = history_versions + current_versions
        versions_by_key: dict[tuple[str, str], _CommittedVersion] = {}
        versions_by_manifest: dict[str, _CommittedVersion] = {}
        for version in all_versions:
            version_key = (version.item_id, version.generation_id or "current-head")
            prior_version = versions_by_key.get(version_key)
            if prior_version is not None and prior_version.manifest_hash != version.manifest_hash:
                raise ValueError("LEM projection contains conflicting duplicate item versions")
            if prior_version is None:
                versions_by_key[version_key] = version
            prior_manifest = versions_by_manifest.get(version.manifest_hash)
            if prior_manifest is None:
                versions_by_manifest[version.manifest_hash] = version
            elif prior_manifest.current_head and not version.current_head:
                # Keep a current-head marker when an exact archive copy is
                # present, so include_item_id cannot defer it twice.
                continue
            elif version.current_head and not prior_manifest.current_head:
                versions_by_manifest[version.manifest_hash] = version
            elif prior_manifest.item_id != version.item_id:
                raise ValueError("LEM projection contains a conflicting duplicate manifest")

        versions = sorted(versions_by_manifest.values(), key=lambda version: version.sort_key)
        target_selected = target is not None and (selected_ids is None or target in selected_ids)
        target_current = current_heads.get(target) if target_selected else None
        if include_item_id is not None and target_selected and target_current is None:
            raise ValueError(f"included LEM item has no committed integration: {include_item_id}")

        if before_item_id is not None or include_item_id is not None:
            if target_current is not None:
                frontier_key = target_current.sort_key
                if before_item_id is not None:
                    selected_versions = [
                        version
                        for version in versions
                        if (
                            (version.item_id == target and not version.current_head)
                            or version.sort_key < frontier_key
                        )
                    ]
                else:
                    selected_versions = [
                        version
                        for version in versions
                        if version.item_id == target or version.sort_key < frontier_key
                    ]
            elif target_index is not None:
                if mode == "requirement":
                    # A fresh Requirement head has no committed_at yet.  All
                    # durable versions discovered at this boundary therefore
                    # precede the candidate, including a later lexical item
                    # whose commit is needed by a successor delta.
                    selected_versions = list(versions)
                else:
                    selected_versions = [
                        version
                        for version in versions
                        if version.item_id == target
                        or (
                            version.item_id in lifecycle_order
                            and lifecycle_order[version.item_id] < target_index
                        )
                    ]
            else:  # pragma: no cover - target was validated above
                selected_versions = []
        else:
            selected_versions = versions

        if selected_ids is not None:
            selected_versions = [version for version in selected_versions if version.item_id in selected_ids]

        for version in selected_versions:
            is_deferred_current = (
                include_item_id is not None
                and target_selected
                and version.current_head
                and version.item_id == target
                and target_current is version
            )
            if is_deferred_current:
                # Keep the current candidate's accepted records intact while
                # replaying the run-level resolution commits below.  Archived
                # predecessors of the same item were selected above and are
                # applied exactly once before this deferred head.
                deferred_records = version.records
                deferred_binding = LEMCommittedBinding(
                    item_id=version.item_id,
                    session_id=str(version.manifest["session_id"]),
                    manifest_hash=version.manifest_hash,
                    records_hash=str(version.manifest["records_hash"]),
                    accepted_content_hash=version.bundle.content_hash,
                    accepted_manifest_hash=version.bundle.manifest_hash,
                )
                deferred_applied_at = str(version.manifest["committed_at"])
                continue
            binding = LEMCommittedBinding(
                item_id=version.item_id,
                session_id=str(version.manifest["session_id"]),
                manifest_hash=version.manifest_hash,
                records_hash=str(version.manifest["records_hash"]),
                accepted_content_hash=version.bundle.content_hash,
                accepted_manifest_hash=version.bundle.manifest_hash,
            )
            for record in version.records:
                delta_payload = record.payload if record.kind == "knowledge_delta" else {}
                delta_operation = delta_payload.get("operation") if isinstance(delta_payload, Mapping) else None
                delta_targets = delta_payload.get("supersedes", ()) if isinstance(delta_payload, Mapping) else ()
                delta_relationship = (
                    record.kind == "knowledge_delta"
                    and (
                        delta_operation == "add_relationship"
                        or any(
                            isinstance(target, Mapping) and target.get("namespace") == "relationship"
                            for target in delta_targets
                        )
                    )
                )
                if record.kind == "relationship" or delta_relationship:
                    deferred_relationships.append((record, str(version.manifest["committed_at"])))
                elif record.kind in {
                    "ontology_item",
                    "limitation",
                    "prepared_asset",
                    "identity_decision",
                    "canonical_mapping",
                    "knowledge_delta",
                }:
                    IntegrationSession._apply_lem_record_to_model(
                        model,
                        record,
                        applied_at=str(version.manifest["committed_at"]),
                    )
            bindings.append(binding)

        resolution_bindings: tuple[Mapping[str, Any], ...] = ()
        try:
            from .entity_resolution import _replay_commits_with_bindings

            resolution_bindings = _replay_commits_with_bindings(
                context,
                model,
            )
        except FileNotFoundError:
            # No run-level resolution workspace is a valid empty authority.
            resolution_bindings = ()

        for record, applied_at in deferred_relationships:
            IntegrationSession._apply_lem_record_to_model(
                model,
                record,
                applied_at=applied_at,
            )

        if deferred_records is not None:
            if deferred_binding is None:  # pragma: no cover - internal invariant
                raise ValueError("LEM projection deferred binding is missing")
            if deferred_applied_at is None:  # pragma: no cover - internal invariant
                raise ValueError("LEM projection deferred commit time is missing")
            for record in deferred_records:
                if record.kind in {
                    "ontology_item",
                    "relationship",
                    "limitation",
                    "prepared_asset",
                    "identity_decision",
                    "canonical_mapping",
                    "knowledge_delta",
                }:
                    IntegrationSession._apply_lem_record_to_model(
                        model,
                        record,
                        applied_at=deferred_applied_at,
                    )
            bindings.append(deferred_binding)

        unsigned = {
            "run_id": context.run_id,
            "item_order": list(order),
            "bindings": [binding.to_dict() for binding in bindings],
            "resolution_bindings": [dict(binding) for binding in resolution_bindings],
            "lem_export": model.export(),
        }
        return LEMProjection(
            run_id=context.run_id,
            item_order=order,
            bindings=tuple(bindings),
            resolution_bindings=tuple(dict(binding) for binding in resolution_bindings),
            model=model,
            projection_hash=_digest(unsigned),
        )


__all__ = ["LEMCommittedBinding", "LEMProjection", "LivingEnterpriseModelProjector"]
