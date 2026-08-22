"""Deterministic cumulative LEM projection from committed integration records.

Committed item records are the durable authority.  The cumulative Living
Enterprise Model is a read-only materialized view rebuilt in lifecycle order;
it has no independent checkpoint or transaction log that can drift from the
accepted item commits.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .durable import ItemWorkspace
from .enterprise_model import LivingEnterpriseModel
from .lifecycle import RunLifecycle
from .workspace import RunContext


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
        seen_manifests: set[str] = set()
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

        for item_id in order:
            if before_item_id is not None and item_id == before_item_id:
                break
            if selected_ids is not None and item_id not in selected_ids:
                continue
            try:
                workspace = ItemWorkspace.load(context, item_id, mode=mode)
            except FileNotFoundError as exc:
                raise ValueError(f"LEM projection has a missing lifecycle item before the frontier: {item_id}") from exc

            state = workspace.state
            lifecycle_state = str(state.get("lifecycle_state", ""))
            integration_state = str(state.get("integration_state", ""))
            if lifecycle_state in {"blocked_by_evidence", "technical_failure"}:
                if IntegrationSession._committed_manifest(workspace) is not None:
                    raise ValueError(f"terminal non-accepted item cannot have committed integration: {item_id}")
                if integration_state == "integrated":
                    raise ValueError(f"terminal non-accepted item cannot be integrated: {item_id}")
                if include_item_id == item_id:
                    break
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
                if include_item_id == item_id:
                    break
                continue

            manifest = IntegrationSession._committed_manifest(workspace)
            if manifest is None:
                if include_item_id == item_id:
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
            if manifest["manifest_hash"] in seen_manifests:
                raise ValueError("LEM projection contains a duplicate committed manifest")
            seen_manifests.add(str(manifest["manifest_hash"]))
            binding = LEMCommittedBinding(
                item_id=item_id,
                session_id=str(manifest["session_id"]),
                manifest_hash=str(manifest["manifest_hash"]),
                records_hash=str(manifest["records_hash"]),
                accepted_content_hash=bundle.content_hash,
                accepted_manifest_hash=bundle.manifest_hash,
            )
            if include_item_id == item_id:
                # Keep the current item's accepted records intact while
                # replaying the run-level resolution commits below.  This is
                # deliberately a single deferred application, not endpoint
                # seeding or a compatibility fallback.
                deferred_records = tuple(records)
                deferred_binding = binding
                deferred_applied_at = str(manifest["committed_at"])
            else:
                for record in records:
                    if record.kind == "relationship":
                        deferred_relationships.append((record, str(manifest["committed_at"])))
                    elif record.kind in {
                        "ontology_item",
                        "limitation",
                        "prepared_asset",
                        "identity_decision",
                        "canonical_mapping",
                    }:
                        IntegrationSession._apply_lem_record_to_model(
                            model,
                            record,
                            applied_at=str(manifest["committed_at"]),
                        )
                bindings.append(binding)
            if include_item_id == item_id:
                break

        resolution_bindings: tuple[Mapping[str, Any], ...] = ()
        try:
            from .entity_resolution import _replay_commits_with_bindings

            resolution_bindings = _replay_commits_with_bindings(context, model)
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
