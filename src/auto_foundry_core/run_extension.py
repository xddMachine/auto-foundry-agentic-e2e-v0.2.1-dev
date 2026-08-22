"""Append-only cumulative Requirement Mode generations.

The first Requirement Mode generation remains the legacy run root.  A later
generation is staged below ``extensions/G-XXXX`` and becomes authoritative only
after a strict, hash-bound pointer is atomically published.  This keeps the
same logical run identity while making admission crash-safe and retryable.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping, Sequence

from .contracts import RequirementRecord
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
                    "cumulative_item_ids": list(cumulative_ids),
                    "state_ref": state_path.relative_to(context.run_root).as_posix(),
                    "plan_ref": plan_path.relative_to(context.run_root).as_posix(),
                    "state_manifest_hash": initial_state["manifest_hash"],
                    "plan_hash": _sha256_file(plan_path),
                    "request_hash": request_hash,
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
                "cumulative_item_ids": list(current_ids),
                "state_ref": state_path.relative_to(context.run_root).as_posix(),
                "plan_ref": plan_path.relative_to(context.run_root).as_posix(),
                "state_manifest_hash": initial_state["manifest_hash"],
                "plan_hash": _sha256_file(plan_path),
                "request_hash": request_hash,
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


__all__ = ["GenerationManifest", "RequirementRunGeneration", "RequirementRunExtension", "RunExtension"]
