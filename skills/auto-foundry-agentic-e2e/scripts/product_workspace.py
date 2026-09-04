"""Agent-facing presentation transaction over the existing product APIs.

Agents choose business views. This workspace owns source binding, CAS, paths,
revision routing and candidate registration. It never reviews its own result,
changes accepted facts, invokes a model, or authorizes publication.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from auto_foundry_core import RunContext, RunLifecycle, PlannerAction, RequirementSupervisorWorkspace
from auto_foundry_core.product_review import ProductCandidate, ProductReviewStore, canonical_hash
from auto_foundry_core.requirement_planning import persist_preview_manifest
import dashboard_assembler as assembler
import dashboard_delta_assembler as generation


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular file: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object: {path.name}")
    return value


class ProductWorkspace:
    """One bounded product action. Recreate after any accepted input changes."""

    def __init__(self, context: RunContext, action: PlannerAction | Mapping[str, Any]) -> None:
        self.context = context
        self.action = action if isinstance(action, PlannerAction) else PlannerAction.from_dict(action)
        if self.action.role != "product_agent":
            raise ValueError("ProductWorkspace requires the product_agent role")
        if self.action.action not in {"build_product_candidate", "build_product_preview", "build_incremental_preview", "refresh_product_preview", "build_final_product"}:
            raise ValueError("ProductWorkspace only builds preview or candidate; it never publishes")
        self.metadata = dict(self.action.metadata or {})
        self.lifecycle = RunLifecycle.load(context)
        self.generation_id = getattr(self.lifecycle.generation_metadata, "generation_id", None) or "G-0001"
        if self.metadata.get("generation_id", self.generation_id) != self.generation_id:
            raise ValueError("product action belongs to a different generation")
        self.preview = "preview" in self.action.action
        self.phase = RequirementSupervisorWorkspace(context).phase_snapshot()
        product = self.phase["product"]
        self.input_fingerprint = product["preview_input_fingerprint"]
        supplied = self.metadata.get("input_fingerprint")
        if supplied is not None and supplied != self.input_fingerprint:
            raise ValueError("product action accepted inputs are stale; request a fresh action")
        self.item_ids = list(product["preview_item_ids"] if self.preview else self.lifecycle.item_ids)
        supplied_ids = self.metadata.get("item_ids")
        if supplied_ids is not None and set(supplied_ids) != set(product["preview_item_ids"]):
            raise ValueError("product action item selection is stale")
        if not self.preview and not self.phase.get("all_items_integrated"):
            raise ValueError("final product requires terminal item boundaries; build a partial preview instead")
        if not self.item_ids:
            raise ValueError("no accepted business inputs are ready for presentation")
        self.plan_ref = f"extensions/{self.generation_id}/business_presentation_plan.json"
        if self.metadata.get("presentation_plan_ref", self.plan_ref) != self.plan_ref:
            raise ValueError("product presentation plan must use its generation namespace")
        self.preflight = assembler.business_presentation_preflight(context, item_ids=self.item_ids, generation_id=self.generation_id)
        self._candidates = self.preflight["inventory"]["candidates"]
        self._by_id = {c["widget_id"]: c for c in self._candidates}
        visuals = assembler.business_presentation_visual_inventory(context, fixture_ref=self.preflight["fixture_ref"], chart_map_ref=self.preflight["chart_map_ref"])
        self._visual_ids = set(visuals["all_visual_widget_ids"])

    def inventory(self, *, offset: int = 0, limit: int = 24) -> dict[str, Any]:
        """Bounded design metadata, not repeated multi-megabyte raw evidence."""
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        entries = []
        fields = ("widget_id", "requirement_id", "title", "type", "value", "unit", "denominator", "period", "grain", "limitations", "accepted_visual", "accepted_evidence", "technical_surface", "technical_surface_reason", "no_geometry_fallback_duplicate", "recipe_id", "layout", "renderer_type", "recipes")
        for candidate in self._candidates[offset:offset + limit]:
            entry = {key: copy.deepcopy(candidate[key]) for key in fields if key in candidate}
            entry["visual"] = candidate["widget_id"] in self._visual_ids
            entries.append(entry)
        end = offset + len(entries)
        return {"generation_id": self.generation_id, "preview": self.preview, "candidates": entries,
                "total": len(self._candidates), "next_offset": end if end < len(self._candidates) else None,
                "item_ids": list(self.item_ids)}

    def detail(self, widget_id: str) -> dict[str, Any]:
        """Inspect exact accepted data/provenance only for a chosen candidate."""
        if widget_id not in self._by_id:
            raise ValueError("unknown widget_id")
        return copy.deepcopy(self._by_id[widget_id])

    def feedback(self) -> dict[str, Any] | None:
        store = ProductReviewStore(self.context, self.generation_id)
        predecessor_ref = self.metadata.get("predecessor_product_review_ref")
        if predecessor_ref:
            from auto_foundry_core.product_review import ProductReview
            review = ProductReview.from_dict(_json(self.context.resolve_run_path(predecessor_ref)))
            if (review.run_id != self.context.run_id or review.generation_id != self.generation_id
                    or review.computed_hash != self.metadata.get("predecessor_product_review_hash")):
                raise ValueError("predecessor product review is stale or unbound")
            return review.to_dict()
        try:
            revision = self.metadata.get("product_revision_id")
            review = store.load_revision_review(revision) if revision else store.load_review()
        except FileNotFoundError:
            return None
        return review.to_dict()

    def _assert_current(self) -> None:
        lifecycle = RunLifecycle.load(self.context)
        generation_id = getattr(lifecycle.generation_metadata, "generation_id", None) or "G-0001"
        phase = RequirementSupervisorWorkspace(self.context).phase_snapshot()
        if generation_id != self.generation_id or phase["product"]["preview_input_fingerprint"] != self.input_fingerprint:
            raise ValueError("product inputs changed during design; recreate ProductWorkspace")

    def _routes(self) -> dict[str, Any] | None:
        """Derive technical delta routes from typed plan membership, not prose."""
        metadata = self.lifecycle.generation_metadata
        if metadata is None or self.generation_id == "G-0001":
            return None
        if self.metadata.get("route") is not None:
            return dict(self.metadata["route"])
        parent = self.preflight["inventory"].get("parent")
        if not parent:
            raise ValueError("successor generation is missing parent lineage")
        receipt = _json(self.context.resolve_run_path(parent["receipt_ref"]))
        fixture = _json(self.context.resolve_run_path(receipt["outputs"]["fixture_ref"]))
        old_ids = {w.get("requirement_id") for w in fixture.get("widgets", [])}
        added_ids = set(getattr(metadata, "added_item_ids", ()))
        plan = _json(Path(self.lifecycle.plan_path))
        routes = {}
        used_groups = {d["id"] for d in fixture.get("domains", [])}
        for index, group in enumerate(plan["groups"], 1):
            ids = set(group["requirement_ids"])
            new = ids & added_ids
            if not new:
                continue
            prior = ids & old_ids
            if prior:
                domains = {generation._parent_domain_for_item(fixture, item) for item in prior}
                domains.discard(None)
                if len(domains) != 1:
                    raise ValueError("typed plan merges incompatible parent domains")
                route = {"kind": "existing", "group_id": domains.pop()}
            else:
                group_id = f"group-{index:02d}"
                if group_id in used_groups:
                    group_id = f"{self.generation_id}-group-{index:02d}"
                route = {"kind": "new", "group_id": group_id, "title": group.get("rationale") or f"Decision group {index}", "order": index}
            for item in new:
                routes[item] = route
        return {"routes": routes}

    def build(self, choices: Sequence[Mapping[str, Any]], *, presentation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Materialize one complete selected design; repeated identical calls are safe.

        A changed, already reviewed product requires a new authorized product
        revision. Review and activation remain independent coordinator actions.
        """
        self._assert_current()
        if isinstance(choices, (str, bytes)) or not isinstance(choices, Sequence):
            raise ValueError("choices must be a list of design choices")
        entries = []
        allowed = {"widget_id", "recipe_id", "layout", "renderer_type"}
        for choice in choices:
            if not isinstance(choice, Mapping) or set(choice) - allowed:
                raise ValueError("choices allow only widget_id, recipe_id, layout, renderer_type; facts are immutable")
            widget_id = choice.get("widget_id")
            if widget_id not in self._by_id:
                raise ValueError("unknown widget_id")
            entry = copy.deepcopy(self._by_id[widget_id])
            entry.update(choice)
            entries.append(entry)
        if not entries:
            raise ValueError("select at least one accepted result or explicit limitation")
        selected_ids = [entry["widget_id"] for entry in entries]
        if len(set(selected_ids)) != len(selected_ids):
            raise ValueError("duplicate widget_id")
        # Every accepted requirement needs a selected surface. A missing chart
        # may be represented by the source-bound answer table, never invention.
        accepted_ids = set(self.phase["product"]["preview_item_ids"])
        covered = {entry.get("requirement_id") for entry in entries}
        missing = accepted_ids - covered
        if missing:
            raise ValueError(f"requirements without a decision surface: {sorted(missing)}")
        plan_path = self.context.resolve_run_path(self.plan_ref)
        old = _json(plan_path) if plan_path.exists() or plan_path.is_symlink() else None
        predecessor_hash = _hash(plan_path) if old is not None else None
        store = ProductReviewStore(self.context, self.generation_id)
        revision = self.metadata.get("product_revision_id")
        copy_value = presentation if presentation is not None else (old or {}).get("presentation")
        # Compare presentation choice only; all machine bindings are revalidated
        # by the writers and assembly. Do not write new lineage for an exact retry.
        projected = lambda seq: [{k: row.get(k) for k in ("widget_id", "recipe_id", "layout", "renderer_type")} for row in seq]
        same = bool(old and projected(old["manager_entries"]) == projected(entries) and old.get("presentation") == copy_value)
        changed_source = bool(old and any(old["source_bindings"].get(k) != self.preflight[k] for k in ("fixture_sha256", "chart_map_sha256")))
        if not self.preview and not revision and old and (not same or changed_source):
            # A repair verdict authorizes exactly a repair, not bypassing an
            # accepted review. This operation archives predecessor evidence.
            had_candidate = store.candidate_path.exists() or store.candidate_path.is_symlink()
            retired = store.discard_stale_candidate_for_rebuild()
            if had_candidate and not retired:
                raise ValueError("cannot discard a valid or accepted product candidate; request a product revision")
        writer = {"manager_entries": entries, "reviewer_ref": "product-agent-design",
                  "fixture_ref": self.preflight["fixture_ref"], "chart_map_ref": self.preflight["chart_map_ref"],
                  "item_ids": self.item_ids, "presentation_plan_ref": self.plan_ref, "presentation": copy_value}
        if old is None or changed_source:
            assembler.write_business_presentation_plan(self.context, generation_id=self.generation_id, **writer)
        elif not same:
            successor = assembler.write_business_presentation_plan_v2(self.context, previous_plan_ref=self.plan_ref, **writer)
            assembler.revise_business_presentation_plan_v2(self.context, successor_plan=successor,
                expected_current_plan_sha256=predecessor_hash,
                expected_successor_plan_sha256=hashlib.sha256(assembler._canonical_bytes(successor)).hexdigest(),
                presentation_plan_ref=self.plan_ref)
        self._assert_current()
        if self.preview:
            receipt = generation.assemble_generation_preview(self.context, item_ids=self.item_ids, presentation_plan_ref=self.plan_ref)
            product = self.phase["product"]
            manifest = persist_preview_manifest(self.context, self.generation_id,
                input_fingerprint=self.input_fingerprint, item_ids=self.item_ids, item_bindings=product["preview_item_bindings"],
                failed_items=product["preview_failed_items"], limitations=product["preview_limitations"])
            return {"status": "preview", "finalizable": False, "site_ref": receipt["outputs"]["site_ref"], "manifest": manifest}
        kwargs = {"presentation_plan_ref": self.plan_ref}
        if revision:
            kwargs.update(revision_id=revision, output_root_ref=self.metadata["output_root_ref"])
        else:
            kwargs["route"] = self._routes()
        receipt = generation.assemble_generation_product(self.context, **kwargs)
        validated = generation.validate_generation_product(self.context, receipt,
            product_manifest_ref=self.metadata.get("product_manifest_ref") or (str(Path(self.metadata["output_root_ref"]) / "product_manifest.json") if revision else self.lifecycle.product_manifest_ref), revision_id=revision,
            output_root_ref=self.metadata.get("output_root_ref"))
        metadata = self.lifecycle.generation_metadata
        if metadata is None:
            parent = {"root_generation": True, "parent_generation_id": None, "parent_manifest_ref": None, "parent_manifest_hash": None}
        else:
            parent_id = metadata.parent_generation_id
            parent_ref = "run_state.json" if parent_id == "G-0001" else f"extensions/{parent_id}/generation_manifest.json"
            parent = {"root_generation": False, "parent_generation_id": parent_id, "parent_manifest_ref": parent_ref,
                      "parent_manifest_hash": _hash(self.context.resolve_run_path(parent_ref))}
        spec = _json(self.context.resolve_run_path("control_plane/coordinator_spec.json"))
        candidate = ProductCandidate(run_id=self.context.run_id, generation_id=self.generation_id,
            product_owner="product-agent", parent_lineage=parent,
            plan_binding={"plan_ref": Path(self.lifecycle.plan_path).relative_to(self.context.run_root).as_posix(),
                          "plan_hash": _hash(Path(self.lifecycle.plan_path))},
            publication_policy_hash=canonical_hash(spec["publication_policy"]), artifact_bindings=validated["artifact_bindings"])
        candidate = store.record_candidate(candidate, revision_id=revision)
        return {"status": "candidate", "candidate_hash": candidate.computed_hash,
                "site_ref": receipt["outputs"]["site_ref"], "receipt_ref": receipt["outputs"]["receipt_ref"],
                "generation_id": self.generation_id, "revision_id": revision, "published": False}
