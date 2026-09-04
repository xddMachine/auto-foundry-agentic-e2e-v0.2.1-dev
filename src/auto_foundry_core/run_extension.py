"""Append-only cumulative Requirement Mode generations.

The first Requirement Mode generation remains the legacy run root.  A later
generation is staged below ``extensions/G-XXXX`` and becomes authoritative only
after a strict, hash-bound pointer is atomically published.  This keeps the
same logical run identity while making admission crash-safe and retryable.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping, Sequence

from .contracts import RequirementRecord
from .data_revisions import DataRevision, DataRevisionError, DataRevisionStore
from .durable import ItemWorkspace, _fsync_directory
from .lifecycle import (
    ACTIVE_GENERATION_POINTER_FILENAME,
    GENERATION_DIRECTORY,
    GENERATION_MANIFEST_FILENAME,
    GENERATION_PLAN_FILENAME,
    GENERATION_STATE_FILENAME,
    RUN_STATE_FILENAME,
    RunGenerationSnapshot,
    RunLifecycle,
    _GENERATION_MANIFEST_FIELDS,
    _assert_no_symlink_components,
    _atomic_write_json,
    _json_bytes,
    _manifest_hash,
    _resolve_run_path_lexical,
    _sha256_file,
    _simple_component,
)
from .requirement_planning import RequirementExecutionGroup, RequirementExecutionPlan
from .workspace import AllowedRootError, RunContext


class DataRefreshNotSafeError(RuntimeError):
    """The current requirement heads are active and cannot be refreshed yet."""


class DataRefreshSupersededError(DataRevisionError):
    """The requested D revision was superseded before refresh publication."""


def _records(
    values: RequirementRecord
    | Mapping[str, Any]
    | Iterable[RequirementRecord | Mapping[str, Any]],
) -> tuple[RequirementRecord, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("records must be an iterable of RequirementRecord values")
    if isinstance(values, RequirementRecord) or isinstance(values, Mapping):
        values = (values,)
    result_values: list[RequirementRecord] = []
    for value in values:
        if isinstance(value, RequirementRecord):
            result_values.append(value)
        elif isinstance(value, Mapping):
            result_values.append(RequirementRecord.from_dict(value))
        else:
            raise TypeError("records must contain RequirementRecord values")
    result = tuple(result_values)
    if not result:
        raise ValueError("at least one new RequirementRecord is required")
    ids = tuple(value.requirement_id for value in result)
    if len(ids) != len(set(ids)):
        raise ValueError("new requirement IDs must be unique")
    return result


def _sha256_payload(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _generation_id(value: Any) -> str:
    result = _simple_component(value, "generation_id")
    suffix = result[2:] if result.startswith("G-") else ""
    if not result.startswith("G-") or len(suffix) != 4 or not suffix.isdigit() or int(suffix) < 2:
        raise ValueError("generation_id must use the G-XXXX form with ordinal >= 2")
    return result


_REFRESH_INTENT_FILENAME = "generation_intent.json"
_REFRESH_INTENT_FIELDS = frozenset(
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
        "plan_hash",
        "request_hash",
        "cumulative_item_ids",
        "added_item_ids",
        "removed_item_ids",
        "updated_item_ids",
        "reopened_item_ids",
        "data_revision_ref",
        "data_revision_hash",
        "item_phases",
        "created_at",
        "intent_hash",
    }
)


def _group_shape(group: RequirementExecutionGroup) -> tuple[Any, ...]:
    return (
        group.rationale,
        group.shared_analysis_intent,
        group.suggested_specialists,
    )


def _validate_group_route(
    parent_groups: Sequence[RequirementExecutionGroup],
    candidate_groups: Sequence[RequirementExecutionGroup],
    new_ids: set[str],
) -> None:
    """Preserve all old groups while allowing explicit new routes.

    Existing groups may gain new IDs only as a suffix.  New groups can be
    inserted, but cannot contain an old ID or alter old metadata/order.
    """

    matched: set[int] = set()
    cursor = 0
    for parent_group in parent_groups:
        found: int | None = None
        for index in range(cursor, len(candidate_groups)):
            candidate = candidate_groups[index]
            old_ids = parent_group.requirement_ids
            if tuple(candidate.requirement_ids[: len(old_ids)]) != old_ids:
                continue
            if _group_shape(candidate) != _group_shape(parent_group):
                continue
            if any(item_id not in new_ids for item_id in candidate.requirement_ids[len(old_ids) :]):
                continue
            found = index
            break
        if found is None:
            raise ValueError("generation route must preserve every prior execution group")
        matched.add(found)
        cursor = found + 1

    for index, group in enumerate(candidate_groups):
        old_members = [item_id for item_id in group.requirement_ids if item_id not in new_ids]
        if old_members and index not in matched:
            raise ValueError("generation route cannot reorder or split prior execution groups")


@dataclass(frozen=True)
class RequirementRunGeneration:
    """Public immutable view of one generation admission."""

    context: RunContext
    metadata: RunGenerationSnapshot
    lifecycle: RunLifecycle

    @property
    def generation_id(self) -> str:
        return self.metadata.generation_id

    @property
    def generation_ordinal(self) -> int:
        return self.metadata.generation_ordinal

    @property
    def added_item_ids(self) -> tuple[str, ...]:
        return self.metadata.added_item_ids

    @property
    def reopened_item_ids(self) -> tuple[str, ...]:
        return self.metadata.reopened_item_ids

    @property
    def data_revision_ref(self) -> str | None:
        return self.metadata.data_revision_ref

    @property
    def data_revision_hash(self) -> str | None:
        return self.metadata.data_revision_hash

    @property
    def cumulative_item_ids(self) -> tuple[str, ...]:
        return self.metadata.cumulative_item_ids

    @property
    def state_path(self) -> Path:
        return Path(self.metadata.state_path)

    @property
    def plan_path(self) -> Path:
        return Path(self.metadata.plan_path)

    @property
    def manifest_path(self) -> Path:
        return Path(self.metadata.manifest_path)


class RequirementRunExtension:
    """Admission API for a new cumulative Requirement Mode generation."""

    def __init__(self, context: RunContext, metadata: RunGenerationSnapshot, lifecycle: RunLifecycle) -> None:
        self.context = context
        self.metadata = metadata
        self.lifecycle = lifecycle

    @property
    def generation_id(self) -> str:
        return self.metadata.generation_id

    @property
    def generation_ordinal(self) -> int:
        return self.metadata.generation_ordinal

    @property
    def added_item_ids(self) -> tuple[str, ...]:
        return self.metadata.added_item_ids

    @property
    def reopened_item_ids(self) -> tuple[str, ...]:
        return self.metadata.reopened_item_ids

    @property
    def data_revision_ref(self) -> str | None:
        return self.metadata.data_revision_ref

    @property
    def data_revision_hash(self) -> str | None:
        return self.metadata.data_revision_hash

    @property
    def cumulative_item_ids(self) -> tuple[str, ...]:
        return self.metadata.cumulative_item_ids

    @property
    def state_path(self) -> Path:
        return Path(self.metadata.state_path)

    @property
    def plan_path(self) -> Path:
        return Path(self.metadata.plan_path)

    @property
    def manifest_path(self) -> Path:
        return Path(self.metadata.manifest_path)

    @classmethod
    def append(
        cls,
        context: RunContext,
        records: RequirementRecord
        | Mapping[str, Any]
        | Iterable[RequirementRecord | Mapping[str, Any]],
        *,
        groups: Iterable[RequirementExecutionGroup | Mapping[str, Any]] | None = None,
        plan: RequirementExecutionPlan | Mapping[str, Any] | None = None,
        planner_ref: str | None = None,
        portfolio_strategy: str | None = None,
        expected_parent_state_hash: str | None = None,
        expected_parent_plan_hash: str | None = None,
        generation_id: str | None = None,
    ) -> "RequirementRunExtension":
        if not isinstance(context, RunContext):
            raise TypeError("RequirementRunExtension.append requires a RunContext")
        new_records = _records(records)
        if plan is not None and groups is not None:
            raise TypeError("plan and groups cannot both be supplied")
        # Reject all aliases that the admission transaction could otherwise
        # touch before acquiring the lock.  The lexical preflight is
        # deliberately separate from RunContext's resolved containment check.
        for relative, label in (
            (RUN_STATE_FILENAME, "run_state.json"),
            (GENERATION_PLAN_FILENAME, "requirement supervisor plan"),
            (ACTIVE_GENERATION_POINTER_FILENAME, "active generation pointer"),
            (GENERATION_DIRECTORY, "generation directory"),
        ):
            _resolve_run_path_lexical(context, relative, label=label)
        with RunLifecycle._run_lock(context):
            parent = RunLifecycle._load_unlocked(context)
            if parent.snapshot.mode != "requirement":
                raise ValueError("RequirementRunExtension requires Requirement Mode")
            active_meta = parent.generation_metadata
            # An exact retry of an already exposed generation is harmless even
            # while that generation is running.  A conflicting retry is not.
            if active_meta is not None and set(value.requirement_id for value in new_records) == set(active_meta.added_item_ids):
                active_plan = cls._load_plan(parent.plan_path)
                if cls._retry_matches(active_plan, new_records, groups, plan, planner_ref, portfolio_strategy):
                    if parent.state == "initialized":
                        items = tuple(
                            ItemWorkspace.load(context, item_id, mode="requirement")
                            for item_id in parent.item_ids
                        )
                        parent._reconcile_unlocked(items)  # noqa: SLF001 - lock already held
                    return cls(context, active_meta, parent)
                raise ValueError("active generation retry conflicts with its admitted route")
            parent_plan_path = parent.plan_path
            parent_plan = cls._load_plan(parent_plan_path)
            parent_state_hash = parent.snapshot.manifest_hash
            parent_plan_hash = _sha256_file(parent_plan_path)
            if expected_parent_state_hash is not None and expected_parent_state_hash != parent_state_hash:
                raise ValueError("expected parent state hash is stale")
            if expected_parent_plan_hash is not None and expected_parent_plan_hash != parent_plan_hash:
                raise ValueError("expected parent plan hash is stale")
            old_ids = tuple(record.requirement_id for record in parent_plan.input_records)
            if tuple(parent.item_ids) != old_ids:
                raise ValueError("active lifecycle and supervisor plan item universes differ")
            duplicate_ids = set(old_ids).intersection(value.requirement_id for value in new_records)
            if duplicate_ids:
                raise ValueError("new requirement IDs already exist in the active generation")
            if plan is None and groups is None:
                groups = parent_plan.groups + tuple(
                    RequirementExecutionGroup(
                        (record.requirement_id,),
                        "Added to the active requirement portfolio.",
                    )
                    for record in new_records
                )
            candidate = cls._candidate_plan(parent_plan, new_records, groups, plan, planner_ref, portfolio_strategy)
            if tuple(candidate.input_records[: len(parent_plan.input_records)]) != parent_plan.input_records:
                raise ValueError("generation plan must preserve prior RequirementRecords byte-for-byte")
            if tuple(candidate.input_records[len(parent_plan.input_records) :]) != new_records:
                raise ValueError("generation plan must append exactly the supplied new RequirementRecords")
            try:
                persisted_parent = json.loads(parent_plan_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("parent requirement supervisor plan is unreadable") from exc
            persisted_records = persisted_parent.get("input_records") if isinstance(persisted_parent, Mapping) else None
            candidate_records = candidate.to_dict()["input_records"]
            if not isinstance(persisted_records, list) or _json_bytes(persisted_records) != _json_bytes(candidate_records[: len(persisted_records)]):
                raise ValueError("generation plan must preserve prior RequirementRecords byte-for-byte")
            if candidate.revision <= parent_plan.revision:
                raise ValueError("generation plan revision must be higher than the parent plan")
            _validate_group_route(parent_plan.groups, candidate.groups, {value.requirement_id for value in new_records})

            ordinal = (active_meta.generation_ordinal + 1) if active_meta is not None else 2
            target_id = _generation_id(generation_id) if generation_id is not None else f"G-{ordinal:04d}"
            if int(target_id[2:]) != ordinal:
                raise ValueError("generation_id ordinal does not match the active parent generation")
            request_hash = _sha256_payload(
                {
                    "parent_state_hash": parent_state_hash,
                    "parent_plan_hash": parent_plan_hash,
                    "added_records": [record.to_dict() for record in new_records],
                    "plan": candidate.to_dict(),
                    "generation_id": target_id,
                }
            )
            generation_root = _resolve_run_path_lexical(
                context,
                Path(GENERATION_DIRECTORY) / target_id,
                label="generation root",
            )
            state_path = generation_root / GENERATION_STATE_FILENAME
            plan_path = generation_root / GENERATION_PLAN_FILENAME
            manifest_path = generation_root / GENERATION_MANIFEST_FILENAME
            cls._validate_generation_root(context, generation_root)
            cls._validate_generation_files(context, state_path, plan_path, manifest_path)

            existing_metadata = cls._existing_staged_metadata(
                context,
                target_id,
                parent_state_hash=parent_state_hash,
                parent_plan_hash=parent_plan_hash,
                request_hash=request_hash,
            )
            if existing_metadata is None:
                generation_root.mkdir(parents=True, exist_ok=True)
                # ``mkdir(parents=True)`` creates both the generation entry
                # and, on a first extension, its parent directory entry.  A
                # later durable pointer must not become visible before those
                # directory entries have reached stable storage.
                for directory in (
                    generation_root,
                    generation_root.parent,
                    generation_root.parent.parent,
                ):
                    _fsync_directory(directory)
                # Stage all item state before pointer publication.  ItemWorkspace
                # is idempotent and validates mode/original text on retries.
                for record in new_records:
                    item = ItemWorkspace.create(
                        context,
                        record.requirement_id,
                        mode="requirement",
                        original_text=record.original_text,
                    )
                    # ItemWorkspace atomically persists its state, but its
                    # namespace/item mkdir boundaries are owned by that
                    # lower-level helper.  Force the containing entries before
                    # exposing this generation through the root pointer.
                    for directory in (
                        item.item_root,
                        item.item_root.parent,
                        item.item_root.parent.parent,
                    ):
                        _fsync_directory(directory)

                cumulative_ids = old_ids + tuple(record.requirement_id for record in new_records)
                initial_state = cls._initial_state(context, cumulative_ids, parent=parent)
                if state_path.exists() or state_path.is_symlink():
                    state = RunLifecycle._read_state_mapping(state_path, context)
                    if (
                        tuple(state["item_ids"]) != cumulative_ids
                        or state.get("status") != initial_state["status"]
                        or state.get("resume_status") != initial_state.get("resume_status")
                        or state.get("pause_reason") != initial_state.get("pause_reason")
                    ):
                        raise ValueError("staged generation state conflicts with the requested append")
                    initial_state = state
                else:
                    _atomic_write_json(state_path, initial_state)
                if plan_path.exists() or plan_path.is_symlink():
                    if plan_path.is_symlink() or _sha256_file(plan_path) != _sha256_payload(candidate.to_dict()):
                        raise ValueError("staged generation plan conflicts with the requested append")
                else:
                    _atomic_write_json(plan_path, candidate.to_dict())

                manifest = {
                    "schema_version": 1,
                    "kind": "run_generation",
                    "run_id": context.run_id,
                    "run_root": str(context.run_root),
                    "generation_id": target_id,
                    "generation_ordinal": ordinal,
                    "parent_generation_id": parent.generation_id,
                    "parent_state_hash": parent_state_hash,
                    "parent_plan_hash": parent_plan_hash,
                    "added_item_ids": [record.requirement_id for record in new_records],
                    "reopened_item_ids": [],
                    "cumulative_item_ids": list(cumulative_ids),
                    "state_ref": state_path.relative_to(context.run_root).as_posix(),
                    "plan_ref": plan_path.relative_to(context.run_root).as_posix(),
                    "state_manifest_hash": initial_state["manifest_hash"],
                    "plan_hash": _sha256_file(plan_path),
                    "request_hash": request_hash,
                    "data_revision_ref": None,
                    "data_revision_hash": None,
                    "product_manifest_ref": f"products/generations/{target_id}/product_manifest.json",
                    "created_at": initial_state["created_at"],
                }
                manifest["manifest_hash"] = _manifest_hash(manifest)
                _atomic_write_json(manifest_path, manifest)
                metadata = cls._metadata_from_staged(context, manifest)
            else:
                metadata = existing_metadata
            latest = RunLifecycle._load_unlocked(context)
            if latest.snapshot.manifest_hash != parent_state_hash or _sha256_file(latest.plan_path) != parent_plan_hash:
                raise ValueError("parent lifecycle or plan changed during generation admission")
            cls._publish_pointer(context, metadata)
        return cls._reconcile_and_load(context, target_id)

    @classmethod
    def revise(
        cls,
        context: RunContext,
        *,
        plan: RequirementExecutionPlan | Mapping[str, Any],
        generation_id: str | None = None,
    ) -> "RequirementRunExtension":
        """Publish an exact mutable portfolio revision.

        Requirements may be added, removed, reordered, or replaced while a
        run is active or complete.  A changed/removed item's prior workspace
        is moved to ``history/requirements`` before a fresh head is exposed;
        unchanged accepted work is reused directly.  The previous generation
        and its product remain readable and no implementation version is part
        of admission.
        """

        if not isinstance(context, RunContext):
            raise TypeError("RequirementRunExtension.revise requires a RunContext")
        candidate = RequirementExecutionPlan.from_dict(plan) if isinstance(plan, Mapping) else plan
        if not isinstance(candidate, RequirementExecutionPlan):
            raise TypeError("plan must be a RequirementExecutionPlan")

        with RunLifecycle._run_lock(context):
            parent = RunLifecycle._load_unlocked(context)
            if parent.snapshot.mode != "requirement":
                raise ValueError("RequirementRunExtension requires Requirement Mode")
            parent_plan = cls._load_plan(parent.plan_path)
            if candidate == parent_plan:
                metadata = parent.generation_metadata
                if metadata is None:
                    raise ValueError("the initial generation has no extension metadata")
                return cls(context, metadata, parent)
            if candidate.revision <= parent_plan.revision:
                candidate = RequirementExecutionPlan(
                    input_records=candidate.input_records,
                    groups=candidate.groups,
                    planner_ref=candidate.planner_ref,
                    portfolio_strategy=candidate.portfolio_strategy,
                    revision=parent_plan.revision + 1,
                )

            parent_by_id = {record.requirement_id: record for record in parent_plan.input_records}
            candidate_by_id = {record.requirement_id: record for record in candidate.input_records}
            old_ids = tuple(record.requirement_id for record in parent_plan.input_records)
            current_ids = tuple(record.requirement_id for record in candidate.input_records)
            added_ids = tuple(item_id for item_id in current_ids if item_id not in parent_by_id)
            removed_ids = tuple(item_id for item_id in old_ids if item_id not in candidate_by_id)
            updated_ids = tuple(
                item_id
                for item_id in current_ids
                if item_id in parent_by_id and candidate_by_id[item_id] != parent_by_id[item_id]
            )

            active_meta = parent.generation_metadata
            ordinal = (active_meta.generation_ordinal + 1) if active_meta is not None else 2
            target_id = _generation_id(generation_id) if generation_id is not None else f"G-{ordinal:04d}"
            if int(target_id[2:]) != ordinal:
                raise ValueError("generation_id ordinal does not match the active parent generation")
            parent_state_hash = parent.snapshot.manifest_hash
            parent_plan_hash = _sha256_file(parent.plan_path)
            request_hash = _sha256_payload(
                {
                    "parent_state_hash": parent_state_hash,
                    "parent_plan_hash": parent_plan_hash,
                    "plan": candidate.to_dict(),
                    "generation_id": target_id,
                }
            )
            generation_root = _resolve_run_path_lexical(
                context,
                Path(GENERATION_DIRECTORY) / target_id,
                label="generation root",
            )
            if generation_root.exists() or generation_root.is_symlink():
                raise ValueError("generation revision already exists with different content")
            state_path = generation_root / GENERATION_STATE_FILENAME
            plan_path = generation_root / GENERATION_PLAN_FILENAME
            manifest_path = generation_root / GENERATION_MANIFEST_FILENAME
            cls._validate_generation_root(context, generation_root)
            cls._validate_generation_files(context, state_path, plan_path, manifest_path)

            generation_root.mkdir(parents=True, exist_ok=False)
            initial_state = cls._initial_state(context, current_ids, parent=parent)
            _atomic_write_json(state_path, initial_state)
            _atomic_write_json(plan_path, candidate.to_dict())
            manifest = {
                "schema_version": 1,
                "kind": "run_generation",
                "run_id": context.run_id,
                "run_root": str(context.run_root),
                "generation_id": target_id,
                "generation_ordinal": ordinal,
                "parent_generation_id": parent.generation_id,
                "parent_state_hash": parent_state_hash,
                "parent_plan_hash": parent_plan_hash,
                "added_item_ids": list(added_ids),
                "reopened_item_ids": [],
                "cumulative_item_ids": list(current_ids),
                "state_ref": state_path.relative_to(context.run_root).as_posix(),
                "plan_ref": plan_path.relative_to(context.run_root).as_posix(),
                "state_manifest_hash": initial_state["manifest_hash"],
                "plan_hash": _sha256_file(plan_path),
                "request_hash": request_hash,
                "data_revision_ref": None,
                "data_revision_hash": None,
                "product_manifest_ref": f"products/generations/{target_id}/product_manifest.json",
                "created_at": initial_state["created_at"],
            }
            manifest["manifest_hash"] = _manifest_hash(manifest)
            _atomic_write_json(manifest_path, manifest)
            metadata = cls._metadata_from_staged(context, manifest)

            archived: list[tuple[Path, Path]] = []
            created: list[Path] = []
            published = False
            try:
                for item_id in (*removed_ids, *updated_ids):
                    source = context.resolve_run_path(Path("requirements") / item_id)
                    if not source.exists():
                        continue
                    history = context.resolve_run_path(
                        Path("history") / "requirements" / item_id / target_id
                    )
                    if history.exists() or history.is_symlink():
                        raise ValueError(f"requirement history revision already exists: {item_id}")
                    history.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, history)
                    archived.append((source, history))

                for item_id in (*added_ids, *updated_ids):
                    record = candidate_by_id[item_id]
                    item = ItemWorkspace.create(
                        context,
                        item_id,
                        mode="requirement",
                        original_text=record.original_text,
                    )
                    created.append(item.item_root)

                latest = RunLifecycle._load_unlocked(context)
                if (
                    latest.snapshot.manifest_hash != parent_state_hash
                    or _sha256_file(latest.plan_path) != parent_plan_hash
                ):
                    raise ValueError("parent lifecycle or plan changed during generation revision")
                cls._publish_pointer(context, metadata)
                published = True
            except Exception:
                if not published:
                    for path in reversed(created):
                        if path.exists() and path.is_dir() and not path.is_symlink():
                            shutil.rmtree(path)
                    for source, history in reversed(archived):
                        if history.exists() and not source.exists():
                            source.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(history, source)
                    if generation_root.exists() and generation_root.is_dir() and not generation_root.is_symlink():
                        shutil.rmtree(generation_root)
                raise

        return cls._reconcile_and_load(context, target_id)

    @staticmethod
    def _initial_state(
        context: RunContext,
        cumulative_ids: Sequence[str],
        *,
        parent: RunLifecycle,
    ) -> dict[str, Any]:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        state = {
            "run_id": context.run_id,
            "run_root": str(context.run_root),
            "item_ids": list(cumulative_ids),
            "mode": "requirement",
            "status": "paused" if parent.paused else "initialized",
            "generation": 0,
            "created_at": now,
            "updated_at": now,
        }
        if parent.paused:
            state["resume_status"] = "running"
            state["pause_reason"] = parent._state.get("pause_reason")  # noqa: SLF001 - generation transfer
        state["manifest_hash"] = _manifest_hash(state)
        return state

    @staticmethod
    def _load_plan(path: Path) -> RequirementExecutionPlan:
        if path.is_symlink() or not path.is_file():
            raise ValueError("requirement supervisor plan is missing")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("requirement supervisor plan is invalid") from exc
        return RequirementExecutionPlan.from_dict(value)

    @staticmethod
    def _candidate_plan(
        parent: RequirementExecutionPlan,
        records: tuple[RequirementRecord, ...],
        groups: Iterable[RequirementExecutionGroup | Mapping[str, Any]] | None,
        plan: RequirementExecutionPlan | Mapping[str, Any] | None,
        planner_ref: str | None,
        portfolio_strategy: str | None,
    ) -> RequirementExecutionPlan:
        if plan is not None:
            candidate = RequirementExecutionPlan.from_dict(plan) if isinstance(plan, Mapping) else plan
            if not isinstance(candidate, RequirementExecutionPlan):
                raise TypeError("plan must be a RequirementExecutionPlan")
            return candidate
        if groups is None:
            raise ValueError("explicit generation groups or a full cumulative plan are required")
        parsed_groups = tuple(
            RequirementExecutionGroup.from_dict(value) if isinstance(value, Mapping) else value
            for value in groups
        )
        return RequirementExecutionPlan(
            input_records=parent.input_records + records,
            groups=parsed_groups,
            planner_ref=planner_ref or parent.planner_ref,
            portfolio_strategy=portfolio_strategy or parent.portfolio_strategy,
            revision=parent.revision + 1,
        )

    @staticmethod
    def _retry_matches(
        active_plan: RequirementExecutionPlan,
        records: tuple[RequirementRecord, ...],
        groups: Iterable[RequirementExecutionGroup | Mapping[str, Any]] | None,
        plan: RequirementExecutionPlan | Mapping[str, Any] | None,
        planner_ref: str | None,
        portfolio_strategy: str | None,
    ) -> bool:
        if not records or tuple(record.requirement_id for record in active_plan.input_records[-len(records) :]) != tuple(
            record.requirement_id for record in records
        ):
            return False
        active_records = {record.requirement_id: record for record in active_plan.input_records}
        if any(active_records.get(record.requirement_id) != record for record in records):
            return False
        if plan is not None:
            candidate = RequirementExecutionPlan.from_dict(plan) if isinstance(plan, Mapping) else plan
            return candidate == active_plan
        if groups is not None:
            parsed = tuple(
                RequirementExecutionGroup.from_dict(value) if isinstance(value, Mapping) else value
                for value in groups
            )
            if parsed != active_plan.groups:
                return False
        return (planner_ref is None or planner_ref == active_plan.planner_ref) and (
            portfolio_strategy is None or portfolio_strategy == active_plan.portfolio_strategy
        )

    @staticmethod
    def _validate_generation_root(context: RunContext, generation_root: Path) -> None:
        try:
            relative = generation_root.relative_to(context.run_root)
        except ValueError as exc:
            raise AllowedRootError("generation root escapes run context") from exc
        current = context.run_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise AllowedRootError("generation root cannot contain symlinks")
        if generation_root.is_symlink():
            raise AllowedRootError("generation root cannot be a symlink")

    @staticmethod
    def _validate_generation_files(context: RunContext, *paths: Path) -> None:
        """Reject generation file aliases before any staging write occurs."""

        for path in paths:
            _assert_no_symlink_components(path, root=context.run_root)
            if path.is_symlink():
                raise AllowedRootError(f"generation file cannot be a symlink: {path.name}")

    @classmethod
    def _existing_staged_metadata(
        cls,
        context: RunContext,
        generation_id: str,
        *,
        parent_state_hash: str,
        parent_plan_hash: str,
        request_hash: str,
    ) -> RunGenerationSnapshot | None:
        root = _resolve_run_path_lexical(
            context,
            Path(GENERATION_DIRECTORY) / generation_id,
            label="staged generation root",
        )
        manifest_path = root / GENERATION_MANIFEST_FILENAME
        if manifest_path.is_symlink():
            raise AllowedRootError("staged generation manifest cannot be a symlink")
        if not manifest_path.exists():
            return None
        if not manifest_path.is_file():
            raise ValueError("staged generation manifest is not a regular file")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("staged generation manifest is invalid") from exc
        if not isinstance(manifest, Mapping) or set(manifest) != _GENERATION_MANIFEST_FIELDS:
            raise ValueError("staged generation manifest fields are invalid")
        manifest = dict(manifest)
        if manifest.get("request_hash") != request_hash or manifest.get("parent_state_hash") != parent_state_hash or manifest.get("parent_plan_hash") != parent_plan_hash:
            raise ValueError("staged generation conflicts with the requested append")
        if manifest.get("manifest_hash") != _manifest_hash(manifest):
            raise ValueError("staged generation manifest hash does not match content")
        pointer = {
            "schema_version": 1,
            "kind": "active_generation",
            "run_id": context.run_id,
            "run_root": str(context.run_root),
            "generation_id": generation_id,
            "generation_ordinal": manifest["generation_ordinal"],
            "parent_generation_id": manifest["parent_generation_id"],
            "state_ref": manifest["state_ref"],
            "plan_ref": manifest["plan_ref"],
            "manifest_ref": manifest["manifest_ref"] if "manifest_ref" in manifest else manifest["state_ref"].replace(GENERATION_STATE_FILENAME, GENERATION_MANIFEST_FILENAME),
            "generation_manifest_hash": _sha256_file(manifest_path),
        }
        pointer["manifest_hash"] = _manifest_hash(pointer)
        return RunLifecycle._load_generation_unlocked(context, pointer)

    @classmethod
    def _metadata_from_staged(cls, context: RunContext, manifest: Mapping[str, Any]) -> RunGenerationSnapshot:
        manifest_path = _resolve_run_path_lexical(
            context,
            manifest["state_ref"].replace(GENERATION_STATE_FILENAME, GENERATION_MANIFEST_FILENAME),
            label="staged generation manifest",
        )
        pointer = {
            "schema_version": 1,
            "kind": "active_generation",
            "run_id": context.run_id,
            "run_root": str(context.run_root),
            "generation_id": manifest["generation_id"],
            "generation_ordinal": manifest["generation_ordinal"],
            "parent_generation_id": manifest["parent_generation_id"],
            "state_ref": manifest["state_ref"],
            "plan_ref": manifest["plan_ref"],
            "manifest_ref": manifest_path.relative_to(context.run_root).as_posix(),
            "generation_manifest_hash": _sha256_file(manifest_path),
        }
        pointer["manifest_hash"] = _manifest_hash(pointer)
        return RunLifecycle._load_generation_unlocked(context, pointer)

    @staticmethod
    def _publish_pointer(context: RunContext, metadata: RunGenerationSnapshot) -> None:
        manifest_path = Path(metadata.manifest_path)
        pointer = {
            "schema_version": 1,
            "kind": "active_generation",
            "run_id": context.run_id,
            "run_root": str(context.run_root),
            "generation_id": metadata.generation_id,
            "generation_ordinal": metadata.generation_ordinal,
            "parent_generation_id": metadata.parent_generation_id,
            "state_ref": Path(metadata.state_path).relative_to(context.run_root).as_posix(),
            "plan_ref": Path(metadata.plan_path).relative_to(context.run_root).as_posix(),
            "manifest_ref": manifest_path.relative_to(context.run_root).as_posix(),
            "generation_manifest_hash": _sha256_file(manifest_path),
        }
        pointer["manifest_hash"] = _manifest_hash(pointer)
        pointer_path = _resolve_run_path_lexical(
            context,
            ACTIVE_GENERATION_POINTER_FILENAME,
            label="active generation pointer",
        )
        if (context.run_root / ACTIVE_GENERATION_POINTER_FILENAME).is_symlink() or pointer_path.is_symlink():
            raise AllowedRootError("active generation pointer cannot be a symlink")
        _atomic_write_json(pointer_path, pointer)

    @classmethod
    def _reconcile_and_load(cls, context: RunContext, generation_id: str) -> "RequirementRunExtension":
        lifecycle = RunLifecycle.load(context)
        if lifecycle.generation_id != generation_id:
            raise ValueError("active generation pointer did not publish requested generation")
        lifecycle.reconcile_from_run()
        refreshed = RunLifecycle.load(context)
        metadata = refreshed.generation_metadata
        if metadata is None or metadata.generation_id != generation_id:
            raise ValueError("active generation metadata is missing after publication")
        return cls(context, metadata, refreshed)

    @staticmethod
    def _refresh_failpoint(name: str) -> None:
        """Private crash-test hook; production has no refresh configuration."""

        del name

    @classmethod
    def _refresh_tripwire(cls, name: str, item_id: str | None = None) -> None:
        cls._refresh_failpoint(name)
        if item_id is not None:
            cls._refresh_failpoint(f"{name}:{item_id}")

    @staticmethod
    def _refresh_tree_hash(root: Path) -> str:
        """Hash every byte in an item root using deterministic relative paths."""

        if root.is_symlink() or not root.is_dir():
            raise ValueError(f"refresh item root is not a regular directory: {root}")
        records: list[dict[str, str]] = []
        for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
            if path.is_symlink():
                raise AllowedRootError(f"refresh item root cannot contain symlinks: {path}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError(f"refresh item root contains a non-regular path: {path}")
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        return hashlib.sha256(_json_bytes(records)).hexdigest()

    @classmethod
    def _validated_data_revision(
        cls,
        context: RunContext,
        data_revision: DataRevision,
    ) -> tuple[DataRevision, str]:
        if not isinstance(data_revision, DataRevision):
            raise TypeError("data_revision must be a validated DataRevision")
        if data_revision.run_id != context.run_id:
            raise DataRevisionError("data revision run identity does not match context")
        store = DataRevisionStore(context)
        loaded = store.load(data_revision.revision_id)
        if loaded != data_revision:
            raise DataRevisionError("data revision metadata is not the validated store value")
        try:
            relative = data_revision.manifest_path.relative_to(context.run_root)
        except ValueError as exc:
            raise DataRevisionError("data revision manifest escapes the run root") from exc
        reference = relative.as_posix()
        try:
            manifest_value = json.loads(data_revision.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataRevisionError("data revision manifest is unreadable") from exc
        if (
            reference != f"data_room/revisions/{data_revision.revision_id}/revision_manifest.json"
            or data_revision.manifest_path.is_symlink()
            or not data_revision.manifest_path.is_file()
            or not isinstance(manifest_value, Mapping)
            or manifest_value.get("manifest_hash") != data_revision.manifest_hash
            or _manifest_hash(manifest_value) != data_revision.manifest_hash
        ):
            raise DataRevisionError("data revision manifest reference is not canonical")
        current = store.current()
        if current is None:
            raise DataRevisionError("data revision is not the current validated revision")
        if current.revision_id != loaded.revision_id or current.manifest_hash != loaded.manifest_hash:
            raise DataRefreshSupersededError("data revision was superseded before refresh admission")
        return loaded, reference

    @staticmethod
    def _normalise_reopened(reopened_item_ids: Iterable[str]) -> tuple[str, ...]:
        if isinstance(reopened_item_ids, (str, bytes)):
            raise TypeError("reopened_item_ids must be an iterable of item IDs")
        values = tuple(_simple_component(item_id, "reopened_item_id") for item_id in reopened_item_ids)
        if len(values) != len(set(values)):
            raise ValueError("reopened_item_ids must be unique")
        return values

    @staticmethod
    def _refresh_state(context: RunContext, item_id: str) -> tuple[Path, dict[str, Any]]:
        root = _resolve_run_path_lexical(context, Path("requirements") / item_id, label="refresh requirement head")
        _assert_no_symlink_components(root, root=context.run_root)
        if root.is_symlink() or not root.is_dir():
            raise ValueError(f"refresh requirement head is not a regular directory: {item_id}")
        state_path = root / "item_state.json"
        _assert_no_symlink_components(state_path, root=context.run_root)
        if state_path.is_symlink() or not state_path.is_file():
            raise ValueError(f"refresh requirement state is missing: {item_id}")
        try:
            value = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"refresh requirement state is invalid: {item_id}") from exc
        if not isinstance(value, Mapping):
            raise ValueError(f"refresh requirement state is invalid: {item_id}")
        ItemWorkspace._validate_state_shape(value)  # noqa: SLF001 - preflight without reconciliation writes
        return root, dict(value)

    @classmethod
    def _preflight_refresh_active(
        cls,
        context: RunContext,
        candidate: RequirementExecutionPlan,
        reopened: tuple[str, ...],
    ) -> None:
        """Reject active mutable heads before any data-revision scan/telemetry."""

        parent = RunLifecycle.load(context)
        if parent.snapshot.mode != "requirement":
            raise ValueError("RequirementRunExtension requires Requirement Mode")
        parent_plan = cls._load_plan(parent.plan_path)
        parent_by_id = {record.requirement_id: record for record in parent_plan.input_records}
        candidate_by_id = {record.requirement_id: record for record in candidate.input_records}
        if tuple(parent.item_ids) != tuple(parent_by_id):
            raise ValueError("active lifecycle and supervisor plan item universes differ")
        if any(item_id not in parent_by_id or item_id not in candidate_by_id for item_id in reopened):
            raise ValueError("reopened_item_ids must select existing current requirements")
        added = {item_id for item_id in candidate_by_id if item_id not in parent_by_id}
        removed = {item_id for item_id in parent_by_id if item_id not in candidate_by_id}
        updated = {
            item_id
            for item_id in candidate_by_id
            if item_id in parent_by_id and candidate_by_id[item_id] != parent_by_id[item_id]
        }
        for item_id in sorted(added | removed | updated | set(reopened)):
            root = _resolve_run_path_lexical(context, Path("requirements") / item_id, label="refresh requirement head")
            if not root.exists():
                continue
            _root, state = cls._refresh_state(context, item_id)
            if state.get("active_attempt_id") is not None or state.get("lifecycle_state") in {
                "review",
                "recovering",
                "recovery_ready",
            }:
                raise DataRefreshNotSafeError(f"refresh requires a stable current requirement: {item_id}")

    @classmethod
    def _write_refresh_intent(cls, path: Path, intent: Mapping[str, Any]) -> None:
        value = dict(intent)
        value["intent_hash"] = _manifest_hash({key: item for key, item in value.items() if key != "intent_hash"})
        _atomic_write_json(path, value)
        _fsync_directory(path.parent)

    @classmethod
    def _load_refresh_intent(cls, context: RunContext, path: Path) -> dict[str, Any]:
        _assert_no_symlink_components(path, root=context.run_root)
        if path.is_symlink() or not path.is_file():
            raise ValueError("refresh generation intent is missing")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("refresh generation intent is invalid") from exc
        if not isinstance(value, Mapping) or set(value) != _REFRESH_INTENT_FIELDS:
            raise ValueError("refresh generation intent fields are invalid")
        intent = dict(value)
        expected = _manifest_hash({key: item for key, item in intent.items() if key != "intent_hash"})
        if intent.get("intent_hash") != expected:
            raise ValueError("refresh generation intent hash does not match content")
        if intent.get("run_id") != context.run_id or Path(str(intent.get("run_root"))).expanduser().resolve(strict=False) != context.run_root:
            raise ValueError("refresh generation intent run identity is invalid")
        for field_name in ("parent_state_hash", "parent_plan_hash", "plan_hash", "request_hash", "data_revision_hash"):
            if field_name == "data_revision_hash" and intent.get(field_name) is None:
                continue
            value_hash = intent.get(field_name)
            if not isinstance(value_hash, str) or len(value_hash) != 64 or value_hash != value_hash.lower() or any(char not in "0123456789abcdef" for char in value_hash):
                raise ValueError(f"refresh generation intent {field_name} is invalid")
        if intent.get("data_revision_ref") is not None:
            ref = intent.get("data_revision_ref")
            if not isinstance(ref, str) or Path(ref).is_absolute() or ref != Path(ref).as_posix():
                raise ValueError("refresh generation intent data revision reference is invalid")
            parts = Path(ref).parts
            revision_id = parts[2] if len(parts) == 4 else ""
            if (
                len(parts) != 4
                or parts[:2] != ("data_room", "revisions")
                or not revision_id.startswith("D-")
                or not revision_id[2:].isdigit()
                or len(revision_id[2:]) < 4
                or parts[3] != "revision_manifest.json"
            ):
                raise ValueError("refresh generation intent data revision reference is invalid")
            data_path = _resolve_run_path_lexical(context, ref, label="refresh data revision manifest")
            _assert_no_symlink_components(data_path, root=context.run_root)
            try:
                data_value = json.loads(data_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("refresh generation intent data revision manifest is invalid") from exc
            if data_path.is_symlink() or not data_path.is_file() or not isinstance(data_value, Mapping) or data_value.get("manifest_hash") != intent.get("data_revision_hash") or _manifest_hash(data_value) != intent.get("data_revision_hash"):
                raise ValueError("refresh generation intent data revision binding is invalid")
        phases = intent.get("item_phases")
        if not isinstance(phases, list):
            raise ValueError("refresh generation intent item phases are invalid")
        ids: list[str] = []
        for phase in phases:
            if not isinstance(phase, Mapping) or set(phase) != {"item_id", "phase", "archive", "create", "original_text", "source_tree_hash"}:
                raise ValueError("refresh generation intent item phase fields are invalid")
            item_id = _simple_component(phase.get("item_id"), "refresh item_id")
            if item_id in ids or phase.get("phase") not in {"pending", "archived", "created"}:
                raise ValueError("refresh generation intent item phase is invalid")
            if not isinstance(phase.get("archive"), bool) or not isinstance(phase.get("create"), bool):
                raise ValueError("refresh generation intent item phase flags are invalid")
            source_hash = phase.get("source_tree_hash")
            if source_hash is not None and (not isinstance(source_hash, str) or len(source_hash) != 64 or source_hash != source_hash.lower() or any(char not in "0123456789abcdef" for char in source_hash)):
                raise ValueError("refresh generation intent source hash is invalid")
            if phase.get("create") and not isinstance(phase.get("original_text"), str):
                raise ValueError("refresh generation intent original text is invalid")
            ids.append(item_id)
        if tuple(ids) != tuple(sorted(ids)):
            raise ValueError("refresh generation intent item phases are not lexically ordered")
        return intent

    @classmethod
    def _ensure_refresh_head(cls, context: RunContext, item_id: str, original_text: str) -> Path:
        root = _resolve_run_path_lexical(context, Path("requirements") / item_id, label="refresh requirement head")
        _assert_no_symlink_components(root, root=context.run_root)
        if root.exists() or root.is_symlink():
            if root.is_symlink() or not root.is_dir():
                raise AllowedRootError("refresh requirement head must be a regular directory")
            _root, state = cls._refresh_state(context, item_id)
            if (
                state.get("item_id") != item_id
                or state.get("mode") != "requirement"
                or state.get("original_text") != original_text
                or state.get("lifecycle_state") != "work"
                or state.get("active_attempt_id") is not None
                or state.get("attempts", []) not in ([], None)
            ):
                raise ValueError("refresh requirement head conflicts with the requested fresh workspace")
            return root
        item = ItemWorkspace.create(context, item_id, mode="requirement", original_text=original_text)
        for directory in (item.item_root, item.item_root.parent, item.item_root.parent.parent):
            _fsync_directory(directory)
        return item.item_root

    @classmethod
    def _archive_refresh_head(
        cls,
        context: RunContext,
        item_id: str,
        target_id: str,
        expected_hash: str,
    ) -> Path:
        source = _resolve_run_path_lexical(context, Path("requirements") / item_id, label="refresh current requirement")
        history = _resolve_run_path_lexical(
            context,
            Path("history") / "requirements" / item_id / target_id,
            label="refresh requirement history",
        )
        _assert_no_symlink_components(source, root=context.run_root)
        _assert_no_symlink_components(history, root=context.run_root)
        if source.is_symlink() or history.is_symlink():
            raise AllowedRootError("refresh requirement paths cannot be symlinks")
        if history.exists():
            if source.exists():
                raise ValueError(f"refresh archive and current head both exist for {item_id}")
            if cls._refresh_tree_hash(history) != expected_hash:
                raise ValueError(f"refresh archive bytes conflict for {item_id}")
            return history
        if not source.exists():
            raise ValueError(f"refresh current requirement head is missing: {item_id}")
        if cls._refresh_tree_hash(source) != expected_hash:
            raise ValueError(f"refresh current requirement head changed for {item_id}")
        history.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_components(history.parent, root=context.run_root)
        os.replace(source, history)
        _fsync_directory(history.parent)
        _fsync_directory(history.parent.parent)
        _fsync_directory(source.parent)
        return history

    @classmethod
    def refresh_data(
        cls,
        context: RunContext,
        plan: RequirementExecutionPlan | Mapping[str, Any],
        *,
        data_revision: DataRevision,
        reopened_item_ids: Iterable[str] = (),
        generation_id: str | None = None,
        expected_parent_generation_id: str | None = None,
        expected_parent_state_hash: str | None = None,
        expected_parent_plan_hash: str | None = None,
    ) -> "RequirementRunExtension":
        """Publish a crash-recoverable generation bound to a data revision.

        The data revision and candidate plan are validated before the run lock.
        Once the lock is held, item transition locks are acquired in lexical
        item-ID order.  The generation-local intent is fsync-backed and is
        advanced after each archive/fresh-head phase, allowing a later retry to
        converge after any process interruption.
        """

        if not isinstance(context, RunContext):
            raise TypeError("RequirementRunExtension.refresh_data requires a RunContext")
        candidate = RequirementExecutionPlan.from_dict(plan) if isinstance(plan, Mapping) else plan
        if not isinstance(candidate, RequirementExecutionPlan):
            raise TypeError("plan must be a RequirementExecutionPlan")
        requested_candidate = candidate
        reopened = cls._normalise_reopened(reopened_item_ids)
        cls._preflight_refresh_active(context, candidate, reopened)
        for relative, label in (
            (RUN_STATE_FILENAME, "run_state.json"),
            (GENERATION_PLAN_FILENAME, "requirement supervisor plan"),
            (ACTIVE_GENERATION_POINTER_FILENAME, "active generation pointer"),
            (GENERATION_DIRECTORY, "generation directory"),
        ):
            _resolve_run_path_lexical(context, relative, label=label)
        # Validate immutable data bytes and the current pointer before acquiring
        # any run/item transition lock.  The run lock serializes a concurrent
        # data-revision pointer swap after this read.
        validated_revision, data_ref = cls._validated_data_revision(context, data_revision)
        data_hash = validated_revision.manifest_hash

        with RunLifecycle._run_lock(context):
            parent = RunLifecycle._load_unlocked(context)
            if parent.snapshot.mode != "requirement":
                raise ValueError("RequirementRunExtension requires Requirement Mode")
            if expected_parent_generation_id is not None and parent.generation_id != _generation_id(expected_parent_generation_id):
                raise ValueError("expected parent generation is stale")
            if expected_parent_state_hash is not None and parent.snapshot.manifest_hash != expected_parent_state_hash:
                raise ValueError("expected parent state hash is stale")
            if expected_parent_plan_hash is not None and _sha256_file(parent.plan_path) != expected_parent_plan_hash:
                raise ValueError("expected parent plan hash is stale")
            parent_plan = cls._load_plan(parent.plan_path)
            if candidate != parent_plan and candidate.revision <= parent_plan.revision:
                candidate = RequirementExecutionPlan(
                    input_records=candidate.input_records,
                    groups=candidate.groups,
                    planner_ref=candidate.planner_ref,
                    portfolio_strategy=candidate.portfolio_strategy,
                    revision=parent_plan.revision + 1,
                )
            parent_by_id = {record.requirement_id: record for record in parent_plan.input_records}
            candidate_by_id = {record.requirement_id: record for record in candidate.input_records}
            old_ids = tuple(record.requirement_id for record in parent_plan.input_records)
            current_ids = tuple(record.requirement_id for record in candidate.input_records)
            if tuple(parent.item_ids) != old_ids:
                raise ValueError("active lifecycle and supervisor plan item universes differ")
            if any(item_id not in parent_by_id or item_id not in candidate_by_id for item_id in reopened):
                raise ValueError("reopened_item_ids must select existing current requirements")
            added_ids = tuple(item_id for item_id in current_ids if item_id not in parent_by_id)
            removed_ids = tuple(item_id for item_id in old_ids if item_id not in candidate_by_id)
            updated_ids = tuple(
                item_id
                for item_id in current_ids
                if item_id in parent_by_id and candidate_by_id[item_id] != parent_by_id[item_id]
            )
            affected_ids = tuple(sorted(set((*added_ids, *removed_ids, *updated_ids, *reopened))))

            active_meta = parent.generation_metadata
            candidate_plan_hash = _sha256_payload(candidate.to_dict())
            if active_meta is not None and active_meta.data_revision_hash == data_hash:
                active_plan = cls._load_plan(parent.plan_path)
                if (
                    (
                        active_plan == candidate
                        or (
                            active_plan.input_records == requested_candidate.input_records
                            and active_plan.groups == requested_candidate.groups
                            and active_plan.planner_ref == requested_candidate.planner_ref
                            and active_plan.portfolio_strategy == requested_candidate.portfolio_strategy
                        )
                    )
                    and active_meta.data_revision_ref == data_ref
                    and active_meta.reopened_item_ids == reopened
                    and (generation_id is None or _generation_id(generation_id) == active_meta.generation_id)
                ):
                    return cls(context, active_meta, parent)
                raise ValueError("data revision is already active for a conflicting refresh")

            current_pointer = DataRevisionStore(context)._read_pointer()  # noqa: SLF001 - metadata-only CAS under run lock
            if current_pointer is None:
                raise DataRevisionError("data revision pointer changed during refresh admission")
            if current_pointer != (validated_revision.revision_id, data_hash):
                raise DataRefreshSupersededError("data revision was superseded during refresh admission")

            ordinal = (active_meta.generation_ordinal + 1) if active_meta is not None else 2
            target_id = _generation_id(generation_id) if generation_id is not None else f"G-{ordinal:04d}"
            if int(target_id[2:]) != ordinal:
                raise ValueError("generation_id ordinal does not match the active parent generation")
            parent_state_hash = parent.snapshot.manifest_hash
            parent_plan_hash = _sha256_file(parent.plan_path)
            request_hash = _sha256_payload(
                {
                    "parent_state_hash": parent_state_hash,
                    "parent_plan_hash": parent_plan_hash,
                    "plan": candidate.to_dict(),
                    "reopened_item_ids": list(reopened),
                    "data_revision_ref": data_ref,
                    "data_revision_hash": data_hash,
                    "generation_id": target_id,
                }
            )
            generation_root = _resolve_run_path_lexical(
                context,
                Path(GENERATION_DIRECTORY) / target_id,
                label="refresh generation root",
            )
            state_path = generation_root / GENERATION_STATE_FILENAME
            plan_path = generation_root / GENERATION_PLAN_FILENAME
            manifest_path = generation_root / GENERATION_MANIFEST_FILENAME
            intent_path = generation_root / _REFRESH_INTENT_FILENAME
            cls._validate_generation_root(context, generation_root)
            cls._validate_generation_files(context, state_path, plan_path, manifest_path, intent_path)

            # Lock every affected mode/item pair in lexical order.  The
            # ItemWorkspace transition lock is run-local and remains stable
            # while a current head is archived and a fresh head is created.
            # Missing current heads (added items or a retry after archive) are
            # therefore locked too, preventing a concurrent create/transition
            # from bypassing the refresh lock identity.
            lock_ids = tuple(sorted(affected_ids))
            # Reject active/non-stable heads before opening a transition lock.
            for item_id in lock_ids:
                head = _resolve_run_path_lexical(
                    context,
                    Path("requirements") / item_id,
                    label="refresh requirement head",
                )
                _assert_no_symlink_components(head, root=context.run_root)
                if not head.exists():
                    continue
                _root, state = cls._refresh_state(context, item_id)
                if state.get("active_attempt_id") is not None or state.get("lifecycle_state") in {
                    "review",
                    "recovering",
                    "recovery_ready",
                }:
                    raise DataRefreshNotSafeError(f"refresh requires a stable current requirement: {item_id}")
            with ExitStack() as lock_stack:
                lockers: list[ItemWorkspace] = []
                for item_id in lock_ids:
                    locker = ItemWorkspace(
                        context,
                        item_id,
                        mode="requirement",
                        original_text="",
                        state={},
                    )
                    lock_stack.enter_context(locker._state_transition_lock())  # noqa: SLF001 - deterministic lock order
                    lockers.append(locker)
                for item_id in lock_ids:
                    head = _resolve_run_path_lexical(
                        context,
                        Path("requirements") / item_id,
                        label="refresh requirement head",
                    )
                    if not head.exists():
                        continue
                    _root, state = cls._refresh_state(context, item_id)
                    if state.get("active_attempt_id") is not None or state.get("lifecycle_state") in {
                        "review",
                        "recovering",
                        "recovery_ready",
                    }:
                        raise DataRefreshNotSafeError(f"refresh requires a stable current requirement: {item_id}")

                generation_root.mkdir(parents=True, exist_ok=True)
                for directory in (generation_root, generation_root.parent, generation_root.parent.parent):
                    _fsync_directory(directory)

                existing_metadata = cls._existing_staged_metadata(
                    context,
                    target_id,
                    parent_state_hash=parent_state_hash,
                    parent_plan_hash=parent_plan_hash,
                    request_hash=request_hash,
                )
                if existing_metadata is None:
                    initial_state = cls._initial_state(context, current_ids, parent=parent)
                    if state_path.exists() or state_path.is_symlink():
                        state = RunLifecycle._read_state_mapping(state_path, context)
                        if tuple(state.get("item_ids", ())) != current_ids:
                            raise ValueError("staged refresh state conflicts with the requested portfolio")
                        initial_state = state
                    else:
                        _atomic_write_json(state_path, initial_state)
                    if plan_path.exists() or plan_path.is_symlink():
                        if plan_path.is_symlink() or _sha256_file(plan_path) != candidate_plan_hash:
                            raise ValueError("staged refresh plan conflicts with the requested portfolio")
                    else:
                        _atomic_write_json(plan_path, candidate.to_dict())
                    manifest = {
                        "schema_version": 1,
                        "kind": "run_generation",
                        "run_id": context.run_id,
                        "run_root": str(context.run_root),
                        "generation_id": target_id,
                        "generation_ordinal": ordinal,
                        "parent_generation_id": parent.generation_id,
                        "parent_state_hash": parent_state_hash,
                        "parent_plan_hash": parent_plan_hash,
                        "added_item_ids": list(added_ids),
                        "reopened_item_ids": list(reopened),
                        "cumulative_item_ids": list(current_ids),
                        "state_ref": state_path.relative_to(context.run_root).as_posix(),
                        "plan_ref": plan_path.relative_to(context.run_root).as_posix(),
                        "state_manifest_hash": initial_state["manifest_hash"],
                        "plan_hash": _sha256_file(plan_path),
                        "request_hash": request_hash,
                        "data_revision_ref": data_ref,
                        "data_revision_hash": data_hash,
                        "product_manifest_ref": f"products/generations/{target_id}/product_manifest.json",
                        "created_at": initial_state["created_at"],
                    }
                    manifest["manifest_hash"] = _manifest_hash(manifest)
                    _atomic_write_json(manifest_path, manifest)
                    metadata = cls._metadata_from_staged(context, manifest)
                else:
                    metadata = existing_metadata
                    if (
                        metadata.reopened_item_ids != reopened
                        or metadata.data_revision_ref != data_ref
                        or metadata.data_revision_hash != data_hash
                        or metadata.cumulative_item_ids != current_ids
                    ):
                        raise ValueError("staged refresh metadata conflicts with the requested operation")

                if intent_path.exists() or intent_path.is_symlink():
                    intent = cls._load_refresh_intent(context, intent_path)
                    expected_bindings = {
                        "generation_id": target_id,
                        "generation_ordinal": ordinal,
                        "parent_generation_id": parent.generation_id,
                        "parent_state_hash": parent_state_hash,
                        "parent_plan_hash": parent_plan_hash,
                        "plan_hash": metadata.plan_hash,
                        "request_hash": request_hash,
                        "cumulative_item_ids": list(current_ids),
                        "added_item_ids": list(added_ids),
                        "removed_item_ids": list(removed_ids),
                        "updated_item_ids": list(updated_ids),
                        "reopened_item_ids": list(reopened),
                        "data_revision_ref": data_ref,
                        "data_revision_hash": data_hash,
                    }
                    if any(intent.get(key) != value for key, value in expected_bindings.items()):
                        raise ValueError("refresh generation intent conflicts with the requested operation")
                else:
                    phase_values: list[dict[str, Any]] = []
                    for item_id in affected_ids:
                        source = _resolve_run_path_lexical(context, Path("requirements") / item_id, label="refresh requirement head")
                        history = _resolve_run_path_lexical(
                            context,
                            Path("history") / "requirements" / item_id / target_id,
                            label="refresh requirement history",
                        )
                        source_hash: str | None = None
                        if source.exists() and not source.is_symlink():
                            source_hash = cls._refresh_tree_hash(source)
                        elif history.exists() and not history.is_symlink():
                            source_hash = cls._refresh_tree_hash(history)
                        if item_id in (*removed_ids, *updated_ids, *reopened) and source_hash is None:
                            raise ValueError(f"refresh source head is missing: {item_id}")
                        phase_values.append(
                            {
                                "item_id": item_id,
                                "phase": "pending",
                                "archive": item_id in (*removed_ids, *updated_ids, *reopened),
                                "create": item_id in (*added_ids, *updated_ids, *reopened),
                                "original_text": candidate_by_id[item_id].original_text if item_id in candidate_by_id and item_id not in removed_ids else None,
                                "source_tree_hash": source_hash,
                            }
                        )
                    intent = {
                        "schema_version": 1,
                        "kind": "requirement_generation_refresh_intent",
                        "run_id": context.run_id,
                        "run_root": str(context.run_root),
                        "generation_id": target_id,
                        "generation_ordinal": ordinal,
                        "parent_generation_id": parent.generation_id,
                        "parent_state_hash": parent_state_hash,
                        "parent_plan_hash": parent_plan_hash,
                        "plan_hash": metadata.plan_hash,
                        "request_hash": request_hash,
                        "cumulative_item_ids": list(current_ids),
                        "added_item_ids": list(added_ids),
                        "removed_item_ids": list(removed_ids),
                        "updated_item_ids": list(updated_ids),
                        "reopened_item_ids": list(reopened),
                        "data_revision_ref": data_ref,
                        "data_revision_hash": data_hash,
                        "item_phases": phase_values,
                        "created_at": metadata.created_at,
                    }
                    cls._write_refresh_intent(intent_path, intent)
                    cls._refresh_tripwire("after_intent")

                phases = [dict(value) for value in intent["item_phases"]]
                for phase in phases:
                    item_id = str(phase["item_id"])
                    if phase["archive"] and phase["phase"] == "pending":
                        cls._archive_refresh_head(context, item_id, target_id, str(phase["source_tree_hash"]))
                        cls._refresh_tripwire("after_archive", item_id)
                        phase["phase"] = "archived"
                        intent["item_phases"] = phases
                        cls._write_refresh_intent(intent_path, intent)
                    elif phase["archive"]:
                        history = _resolve_run_path_lexical(
                            context,
                            Path("history") / "requirements" / item_id / target_id,
                            label="refresh requirement history",
                        )
                        if history.is_symlink() or not history.is_dir() or cls._refresh_tree_hash(history) != str(phase["source_tree_hash"]):
                            raise ValueError(f"refresh archive bytes conflict for {item_id}")
                    if phase["create"]:
                        cls._ensure_refresh_head(context, item_id, str(phase["original_text"]))
                        if phase["phase"] != "created":
                            cls._refresh_tripwire("after_create", item_id)
                            phase["phase"] = "created"
                            intent["item_phases"] = phases
                            cls._write_refresh_intent(intent_path, intent)
                    elif phase["phase"] != "archived":
                        phase["phase"] = "archived"
                        intent["item_phases"] = phases
                        cls._write_refresh_intent(intent_path, intent)

                if any(
                    phase["phase"] != ("created" if phase["create"] else "archived")
                    for phase in phases
                ):
                    raise ValueError("refresh generation intent did not reach a terminal item phase")
                latest = RunLifecycle._load_unlocked(context)
                if latest.snapshot.manifest_hash != parent_state_hash or _sha256_file(latest.plan_path) != parent_plan_hash:
                    raise ValueError("parent lifecycle or plan changed during data refresh")
                cls._refresh_tripwire("before_pointer")
                cls._publish_pointer(context, metadata)
                cls._refresh_tripwire("after_pointer")

        return cls._reconcile_and_load(context, target_id)

    @classmethod
    def load(cls, context: RunContext, generation_id: str | None = None) -> "RequirementRunExtension":
        lifecycle = RunLifecycle.load(context)
        metadata = lifecycle.generation_metadata
        if metadata is None:
            raise FileNotFoundError("no active requirement generation")
        if generation_id is not None and _generation_id(generation_id) != metadata.generation_id:
            raise ValueError("requested generation is not active")
        return cls(context, metadata, lifecycle)

    @classmethod
    def list(cls, context: RunContext) -> tuple[RunGenerationSnapshot, ...]:
        raw_root = context.run_root / GENERATION_DIRECTORY
        if raw_root.is_symlink():
            raise AllowedRootError("generation directory cannot be a symlink")
        root = _resolve_run_path_lexical(context, GENERATION_DIRECTORY, label="generation directory")
        if not root.exists():
            return ()
        if root.is_symlink() or not root.is_dir():
            raise AllowedRootError("generation directory must be a regular directory")
        values: list[RunGenerationSnapshot] = []
        for child in sorted(root.iterdir(), key=lambda path: path.name):
            if not child.is_dir() or child.is_symlink():
                continue
            manifest_path = child / GENERATION_MANIFEST_FILENAME
            if manifest_path.is_symlink():
                raise AllowedRootError("generation manifest cannot be a symlink")
            if not manifest_path.exists():
                continue
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                raise ValueError("generation manifest is invalid")
            pointer = {
                "schema_version": 1,
                "kind": "active_generation",
                "run_id": context.run_id,
                "run_root": str(context.run_root),
                "generation_id": value.get("generation_id"),
                "generation_ordinal": value.get("generation_ordinal"),
                "parent_generation_id": value.get("parent_generation_id"),
                "state_ref": value.get("state_ref"),
                "plan_ref": value.get("plan_ref"),
                "manifest_ref": manifest_path.relative_to(context.run_root).as_posix(),
                "generation_manifest_hash": _sha256_file(manifest_path),
            }
            pointer["manifest_hash"] = _manifest_hash(pointer)
            values.append(RunLifecycle._load_generation_unlocked(context, pointer))
        return tuple(values)


RunExtension = RequirementRunExtension
GenerationManifest = RunGenerationSnapshot


__all__ = [
    "DataRefreshNotSafeError",
    "DataRefreshSupersededError",
    "GenerationManifest",
    "RequirementRunGeneration",
    "RequirementRunExtension",
    "RunExtension",
]
