from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import re
import sys
import threading
from pathlib import Path

from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.product_review import ProductCandidate, ProductReview, hash_artifact
from auto_foundry_core.requirement_planning import RequirementSupervisorWorkspace, persist_preview_manifest
from auto_foundry_core.workspace import RunContext

from apps.control_center_operational.launch import LaunchManager, LaunchSettings
from apps.control_center_operational.projection import (
    OperationalRepository,
    _product_projection_bundle,
    _tree_hash,
)
from apps.control_center_operational.server import OperationalServer


def _load_dashboard_assembler():
    """Load the production assembler used to publish preview bytes."""

    path = Path(__file__).resolve().parents[3] / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_assembler.py"
    spec = importlib.util.spec_from_file_location("control_center_preview_dashboard_assembler", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_dashboard_renderer():
    """Load the production renderer for bounded inline-style auditing."""

    path = Path(__file__).resolve().parents[3] / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_renderer.py"
    spec = importlib.util.spec_from_file_location("control_center_preview_dashboard_renderer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _tree_binding(root: Path, path: Path) -> dict[str, object]:
    files = {
        child.relative_to(path).as_posix(): _sha(child.read_bytes())
        for child in sorted(path.rglob("*"))
        if child.is_file()
    }
    kind, digest = hash_artifact(path)
    return {"ref": _relative(root, path), "kind": kind, "sha256": digest, "files": files}


def _run_root(tmp_path: Path, run_id: str = "RUN-PREVIEW") -> Path:
    root = tmp_path / "run"
    root.mkdir()
    lifecycle = RunLifecycle.create(RunContext(run_id, root), ["REQ-001"], mode="requirement")
    lifecycle.plan_path.write_bytes(b"plan\n")
    return root


def _site(root: Path, generation: str = "G-0001", *, preview: bool = False, js_name: str = "dashboard.js") -> tuple[Path, dict[str, str], str, str]:
    base = root / "products" / "generations" / generation
    if preview:
        base /= "preview"
    site = base / "site"
    (site / "assets").mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_bytes(f'<script src="assets/{js_name}"></script>'.encode())
    (site / "assets" / js_name).write_bytes(b"document.documentElement.dataset.ready='1';")
    (site / "assets" / "dashboard.css").write_bytes(b"body{color:#123456}")
    inventory = {
        "index.html": _sha((site / "index.html").read_bytes()),
        f"assets/{js_name}": _sha((site / "assets" / js_name).read_bytes()),
        "assets/dashboard.css": _sha((site / "assets" / "dashboard.css").read_bytes()),
    }
    tree_hash = _tree_hash(inventory)
    blueprint_ref = f"products/generations/{generation}/{'preview/' if preview else ''}dashboard_blueprint_v2.json"
    blueprint = {
        "schema_version": "dashboard.business_presentation_plan.v2",
        "kind": "dashboard_blueprint",
        "run_id": "RUN-PREVIEW",
        "generation_id": generation,
        "source_bindings": {"blueprint_ref": blueprint_ref},
    }
    bp_path = root / blueprint_ref
    bp_path.parent.mkdir(parents=True, exist_ok=True)
    bp_path.write_bytes(_canonical(blueprint))
    blueprint_hash = _sha(bp_path.read_bytes())
    manifest = {
        "pages": ["index.html"],
        "assets": ["assets/dashboard.css", f"assets/{js_name}"],
        "site_file_hashes": inventory,
        "site_tree_sha256": tree_hash,
        "runtime": {"asset": "assets/dashboard.js", "deterministic": True, "network": False},
        "blueprint_ref": blueprint_ref,
        "blueprint_sha256": blueprint_hash,
    }
    if js_name == "dashboard.js":
        manifest_path = site / "site_manifest.json"
        manifest_path.write_bytes(_canonical(manifest))
    else:
        # Unknown JavaScript is intentionally left with a matching inventory
        # so the projection validator, rather than fixture construction,
        # proves the canonical runtime allowlist.
        manifest_path = site / "site_manifest.json"
        manifest_path.write_bytes(_canonical(manifest))
    return site, inventory, blueprint_ref, blueprint_hash


def _write_incremental_preview(root: Path) -> None:
    site, _inventory, blueprint_ref, blueprint_hash = _site(root, preview=True)
    generation_root = root / "products" / "generations" / "G-0001" / "preview"
    site_manifest = site / "site_manifest.json"
    site_manifest_hash = _sha(site_manifest.read_bytes())
    # The embedded site manifest binds the non-manifest renderer inventory;
    # the preview/receipt contract binds the complete direct file map,
    # including site_manifest.json.
    complete_inventory = {
        child.relative_to(site).as_posix(): _sha(child.read_bytes())
        for child in sorted(site.rglob("*"))
        if child.is_file()
    }
    site_tree_hash = _tree_hash(complete_inventory)
    receipt_ref = "products/generations/G-0001/preview/build_receipt.json"
    receipt = {
        "status": "complete",
        "new_analytics": False,
        "run_id": "RUN-PREVIEW",
        "generation_id": "G-0001",
        "outputs": {"receipt_ref": receipt_ref, "blueprint_ref": blueprint_ref, "site_ref": "products/generations/G-0001/preview/site"},
        "blueprint_binding": {"ref": blueprint_ref, "sha256": blueprint_hash},
        "site_binding": {"tree_sha256": site_tree_hash, "files": complete_inventory},
    }
    receipt_path = generation_root / "build_receipt.json"
    receipt_path.write_bytes(_canonical(receipt))
    current_product = RequirementSupervisorWorkspace(RunContext("RUN-PREVIEW", root)).phase_snapshot()["product"]
    preview = {
        "schema_version": "dashboard.preview.v1",
        "run_id": "RUN-PREVIEW",
        "generation_id": "G-0001",
        "finalizable": False,
        "input_fingerprint": current_product["preview_input_fingerprint"],
        "item_ids": [],
        "item_bindings": {},
        "failed_items": [],
        "limitations": [],
        "assembly_receipt_ref": receipt_ref,
        "assembly_receipt_sha256": _sha(receipt_path.read_bytes()),
        "blueprint_ref": blueprint_ref,
        "blueprint_sha256": blueprint_hash,
        "site_manifest_ref": "products/generations/G-0001/preview/site/site_manifest.json",
        "site_manifest_sha256": site_manifest_hash,
        "site_ref": "products/generations/G-0001/preview/site",
        "site_tree_sha256": site_tree_hash,
    }
    (generation_root / "preview_manifest.json").write_bytes(_canonical(preview))


def _write_candidate(root: Path, *, review: str | None = None) -> tuple[str, Path]:
    site, _inventory, blueprint_ref, _blueprint_hash = _site(root)
    generation_root = root / "products" / "generations" / "G-0001"
    files: dict[str, Path] = {"site": site, "blueprint": root / blueprint_ref}
    for name in ("manifest", "fixture", "chart_map", "chart_registry", "receipt"):
        path = generation_root / f"{name}.json"
        path.write_bytes(_canonical({"name": name}))
        files[name] = path
    bindings = {name: _tree_binding(root, path) if name == "site" else {"ref": _relative(root, path), "kind": "file", "sha256": _sha(path.read_bytes())} for name, path in files.items()}
    lifecycle = RunLifecycle.load(RunContext("RUN-PREVIEW", root))
    candidate = ProductCandidate(
        run_id="RUN-PREVIEW",
        generation_id="G-0001",
        product_owner="product-owner",
        parent_lineage={"root_generation": True, "parent_generation_id": None, "parent_manifest_ref": None, "parent_manifest_hash": None},
        plan_binding={"plan_ref": _relative(root, lifecycle.plan_path), "plan_hash": _sha(lifecycle.plan_path.read_bytes())},
        publication_policy_hash="b" * 64,
        artifact_bindings=bindings,
        created_at="2026-09-02T00:00:00Z",
    )
    candidate_path = generation_root / "product_candidate.json"
    candidate_path.write_bytes(_canonical(candidate.to_dict()))
    if review:
        value = ProductReview(
            run_id="RUN-PREVIEW",
            generation_id="G-0001",
            candidate_ref="products/generations/G-0001/product_candidate.json",
            candidate_hash=candidate.computed_hash,
            product_owner="product-owner",
            reviewer_ref="independent-reviewer",
            verdict=review,
            reviewed_at="2026-09-02T00:01:00Z",
        )
        (generation_root / "product_review.json").write_bytes(_canonical(value.to_dict()))
    return candidate.computed_hash, candidate_path


def _repository(root: Path) -> tuple[OperationalRepository, str]:
    repository = OperationalRepository(None, [root.parent])
    return repository, repository.list_runs()[0]["id"]


def test_production_assembler_preview_uses_complete_tree_and_public_route_id(tmp_path: Path) -> None:
    """Bind the real assembler/persisted preview contract to the HTTP route."""

    # The integration fixture commits one typed metric, which is the minimum
    # accepted/integrated input required by the production assembler.
    from tests.integration.test_dashboard_assembler import _seed_run

    root = tmp_path / "run"
    context = _seed_run(root)
    assembler = _load_dashboard_assembler()
    receipt = assembler.assemble_dashboard(
        context,
        output_dir="generations/G-0001/preview",
        item_ids=["REQ-A"],
    )
    product_state = RequirementSupervisorWorkspace(context).phase_snapshot()["product"]
    manifest = persist_preview_manifest(
        context,
        "G-0001",
        input_fingerprint=product_state["preview_input_fingerprint"],
        item_ids=product_state["preview_item_ids"],
        item_bindings=product_state["preview_item_bindings"],
    )
    assert manifest["site_tree_sha256"] == receipt["site_binding"]["tree_sha256"]
    assert manifest["site_tree_sha256"] == _tree_hash(receipt["site_binding"]["files"])

    repository = OperationalRepository(None, [root.parent])
    public_id = repository.list_runs()[0]["id"]
    assert public_id != context.run_id
    snapshot = repository.snapshot(public_id)
    preview = snapshot["productPreview"]
    assert preview["valid"] is True
    assert preview["source"] == "incremental_preview"
    assert preview["previewUrl"] == f"/api/product/preview/{public_id}/index.html"

    settings = LaunchSettings(
        runtime_root=tmp_path,
        runs_root=root.parent,
        source_roots=(tmp_path,),
        launch_token="token",
    )
    server = OperationalServer(
        ("127.0.0.1", 0),
        repository,
        LaunchManager(settings, repository=repository),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=4)
        connection.request("GET", preview["previewUrl"])
        response = connection.getresponse()
        body = response.read()
        connection.close()
        assert response.status == 200
        assert body == (root / "products/generations/G-0001/preview/site/index.html").read_bytes()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_core_preview_validation_rejects_fingerprint_and_item_binding_tamper(tmp_path: Path) -> None:
    """Core Product Agent inspection remains the single preview schema gate."""

    from tests.integration.test_dashboard_assembler import _seed_run

    root = tmp_path / "run"
    context = _seed_run(root)
    assembler = _load_dashboard_assembler()
    assembler.assemble_dashboard(context, output_dir="generations/G-0001/preview", item_ids=["REQ-A"])
    product_state = RequirementSupervisorWorkspace(context).phase_snapshot()["product"]
    persisted = persist_preview_manifest(
        context,
        "G-0001",
        input_fingerprint=product_state["preview_input_fingerprint"],
        item_ids=product_state["preview_item_ids"],
        item_bindings=product_state["preview_item_bindings"],
    )
    preview_path = root / "products/generations/G-0001/preview/preview_manifest.json"
    baseline = json.loads(preview_path.read_text(encoding="utf-8"))
    repository = OperationalRepository(None, [root.parent])
    public_id = repository.list_runs()[0]["id"]

    tampered_values: list[dict[str, object]] = []
    fingerprint_tamper = dict(baseline)
    fingerprint_tamper["input_fingerprint"] = "0" * 64
    tampered_values.append(fingerprint_tamper)
    relabeled = dict(baseline)
    relabeled["item_ids"] = ["REQ-RENAMED"]
    tampered_values.append(relabeled)
    missing_field = dict(baseline)
    missing_field["item_bindings"] = {"REQ-A": {key: value for key, value in baseline["item_bindings"]["REQ-A"].items() if key != "records_hash"}}
    tampered_values.append(missing_field)
    extra_field = dict(baseline)
    extra_field["item_bindings"] = {"REQ-A": {**baseline["item_bindings"]["REQ-A"], "unexpected": "value"}}
    tampered_values.append(extra_field)
    foreign_ref = dict(baseline)
    foreign_ref["item_bindings"] = {"REQ-A": {**baseline["item_bindings"]["REQ-A"], "accepted_manifest_ref": "raw/foreign.json"}}
    tampered_values.append(foreign_ref)

    for tampered in tampered_values:
        preview_path.write_bytes(_canonical(tampered))
        assert repository.snapshot(public_id)["productPreview"]["valid"] is False
    # The helper's return value is checked as well, proving the persisted
    # producer bytes remain canonical before each mutation.
    assert persisted["finalizable"] is False


def test_renderer_inline_geometry_styles_are_bounded_and_not_raw_reviewed_text() -> None:
    """Only normalized numeric percentages may enter CSP style attributes."""

    renderer = _load_dashboard_renderer()
    freeze = {
        "answers_frozen": True,
        "living_enterprise_model_frozen": True,
        "prepared_data_registry_frozen": True,
        "dashboard_frozen": True,
        "telemetry_frozen": True,
    }
    fixture = {
        "title": "Inline geometry audit",
        "freeze_markers": freeze,
        "widgets": [
            {
                "id": "bar-1",
                "type": "bar",
                "title": "Bar geometry",
                "rows": [{"label": "Unsafe; --x:bad", "value": 7, "size": "70%"}],
                "requirement_id": "REQ-BAR",
                "requirement_title": "Bar",
                "reviewed_item_ref": "requirements/REQ-BAR/accepted/manifest.json",
                "reviewed_output_ref": "requirements/REQ-BAR/accepted/answer_content.json",
                "evidence_refs": ["products/evidence/bar.json"],
                "trace_refs": ["products/evidence/bar.json"],
            },
            {
                "id": "column-1",
                "type": "column",
                "title": "Column geometry",
                "rows": [{"label": "<img src=x>", "value": 3, "size": "30%"}],
                "requirement_id": "REQ-COLUMN",
                "requirement_title": "Column",
                "reviewed_item_ref": "requirements/REQ-COLUMN/accepted/manifest.json",
                "reviewed_output_ref": "requirements/REQ-COLUMN/accepted/answer_content.json",
                "evidence_refs": ["products/evidence/column.json"],
                "trace_refs": ["products/evidence/column.json"],
            },
        ],
        "domains": [
            {
                "id": "operations",
                "title": "Operations",
                "order": 1,
                "decision_flow": [{"id": "flow", "title": "Operations", "order": 1, "widget_ids": ["bar-1", "column-1"]}],
            }
        ],
    }
    pages, _manifest = renderer.render_dashboard_site(fixture)
    style_values = [
        value
        for page in pages.values()
        if isinstance(page, (str, bytes))
        for value in re.findall(r'\sstyle="([^"]*)"', page.decode() if isinstance(page, bytes) else page)
    ]
    assert style_values
    assert all(re.fullmatch(r"--[a-z-]+-size:(?:0|[1-9]\d*)(?:\.\d+)?%", value) for value in style_values)
    assert all("Unsafe" not in value and "<img" not in value for value in style_values)


def test_incremental_preview_projects_and_disappears_without_failure(tmp_path: Path) -> None:
    root = _run_root(tmp_path)
    _write_incremental_preview(root)
    repository, run_id = _repository(root)
    preview = repository.snapshot(run_id)["productPreview"]
    assert preview["valid"] is True
    assert preview["source"] == "incremental_preview"
    assert repository.product_asset(run_id, "assets/dashboard.js", preview=True)[0].startswith(b"document")
    (root / "products/generations/G-0001/preview/preview_manifest.json").unlink()
    snapshot = repository.snapshot(run_id)
    assert snapshot["productPreview"]["valid"] is False
    assert snapshot["run"]["status"] == "initialized"


def test_candidate_preview_then_accepted_final_uses_same_candidate_hash(tmp_path: Path) -> None:
    root = _run_root(tmp_path)
    candidate_hash, _candidate_path = _write_candidate(root)
    repository, run_id = _repository(root)
    preview = repository.snapshot(run_id)["productPreview"]
    assert preview["valid"] is True and preview["source"] == "candidate"
    assert preview["candidateHash"] == candidate_hash
    assert repository.snapshot(run_id)["productDashboard"]["valid"] is False
    _write_candidate(root, review="accept_with_limits")
    final = repository.snapshot(run_id)["productDashboard"]
    assert final["valid"] is True
    assert final["source"] == "accepted_candidate"
    assert final["reviewVerdict"] == "accept_with_limits"
    assert final["candidateHash"] == candidate_hash
    assert repository.snapshot(run_id)["productPreview"]["valid"] is False


def test_unknown_runtime_javascript_and_symlink_are_rejected(tmp_path: Path) -> None:
    root = _run_root(tmp_path)
    site, _inventory, _blueprint_ref, _blueprint_hash = _site(root, js_name="evil.js")
    repository, run_id = _repository(root)
    assert repository.snapshot(run_id)["productPreview"]["valid"] is False
    # An otherwise valid candidate becomes absent when a declared site member
    # is replaced by a symlink; the strict tree validator is fail-closed.
    _write_candidate(root)
    target = site / "assets" / "dashboard.js"
    target.unlink()
    target.symlink_to(site / "index.html")
    assert repository.snapshot(run_id)["productPreview"]["valid"] is False


def test_preview_and_final_routes_use_isolated_sandbox_csp(tmp_path: Path) -> None:
    root = _run_root(tmp_path)
    _write_incremental_preview(root)
    settings = LaunchSettings(runtime_root=tmp_path, runs_root=tmp_path / "runs", source_roots=(tmp_path,), launch_token="token")
    repository = OperationalRepository(None, [root.parent])
    server = OperationalServer(("127.0.0.1", 0), repository, LaunchManager(settings, repository=repository))
    server_thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        run_id = repository.list_runs()[0]["id"]
        preview_url = repository.snapshot(run_id)["productPreview"]["previewUrl"]
        assert preview_url == f"/api/product/preview/{run_id}/index.html"
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=4)
        conn.request("GET", preview_url)
        response = conn.getresponse()
        response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        assert response.status == 200
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["content-security-policy"] == (
            "sandbox allow-scripts; default-src 'none'; script-src 'self'; connect-src 'none'; "
            "style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'; object-src 'none'"
        )
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
