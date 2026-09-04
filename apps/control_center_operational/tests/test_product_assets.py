from __future__ import annotations

import http.client
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from apps.control_center_operational.launch import LaunchManager, LaunchSettings
from apps.control_center_operational.projection import _product_signature_identity
from apps.control_center_operational.server import OperationalRepository, OperationalServer
from apps.control_center_operational.tests.test_projection_sidecars import _run, _write_valid_product
from auto_foundry_core.product_review import ProductCandidate, ProductReviewStore, canonical_hash
from auto_foundry_core.workspace import RunContext


def _materialize_product_revision(
    store: ProductReviewStore,
    source: ProductCandidate,
    *,
    request_id: str,
    marker: bytes,
) -> tuple[object, ProductCandidate]:
    """Build one fully self-contained revision bundle for projection tests."""

    revision = store.begin_revision(
        request_id=request_id,
        input_fingerprint=hashlib.sha256(request_id.encode("utf-8")).hexdigest(),
        implementation_identity=hashlib.sha256((request_id + ":implementation").encode("utf-8")).hexdigest(),
    )
    artifact_root = store.revision_artifacts_root(revision.revision_id)
    filenames = {
        "manifest": "product_manifest.json",
        "fixture": "dashboard_fixture_v4.json",
        "chart_map": "dashboard_chart_map_v4.json",
        "chart_registry": "dashboard_chart_registry_v4.json",
        "blueprint": "dashboard_blueprint_v2.json",
        "receipt": "build_receipt.json",
    }
    bindings: dict[str, dict[str, str]] = {}
    for name, filename in filenames.items():
        source_path = store.context.resolve_run_path(source.artifact_bindings[name]["ref"])
        target = artifact_root / filename
        target.write_bytes(source_path.read_bytes())
        bindings[name] = {"ref": str(target.relative_to(store.context.run_root))}
    source_site = store.context.resolve_run_path(source.artifact_bindings["site"]["ref"])
    target_site = artifact_root / "site"
    shutil.copytree(source_site, target_site)
    bindings["site"] = {"ref": str(target_site.relative_to(store.context.run_root))}

    # Keep every nested receipt/manifest/Blueprint reference inside this
    # revision namespace so the active pointer swap exercises real serving,
    # rather than merely pointing a new candidate at legacy root bytes.
    relative = {
        name: str((artifact_root / filename).relative_to(store.context.run_root))
        for name, filename in filenames.items()
    }
    blueprint_path = artifact_root / filenames["blueprint"]
    blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
    source_bindings = blueprint.get("source_bindings")
    if isinstance(source_bindings, dict):
        source_bindings["blueprint_ref"] = relative["blueprint"]
    blueprint_path.write_bytes(
        (json.dumps(blueprint, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )
    blueprint_hash = hashlib.sha256(blueprint_path.read_bytes()).hexdigest()

    site_manifest_path = target_site / "site_manifest.json"
    site_manifest = json.loads(site_manifest_path.read_text(encoding="utf-8"))
    index_path = target_site / "index.html"
    index_path.write_bytes(marker)
    site_file_hashes = dict(site_manifest["site_file_hashes"])
    site_file_hashes["index.html"] = hashlib.sha256(marker).hexdigest()
    site_manifest["site_file_hashes"] = site_file_hashes
    site_manifest["blueprint_ref"] = relative["blueprint"]
    site_manifest["blueprint_sha256"] = blueprint_hash
    if "site_tree_sha256" in site_manifest:
        site_manifest["site_tree_sha256"] = canonical_hash(site_file_hashes)
    site_manifest_path.write_bytes(
        (json.dumps(site_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )

    receipt_path = artifact_root / filenames["receipt"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["outputs"] = {
        "receipt_ref": relative["receipt"],
        "fixture_ref": relative["fixture"],
        "chart_map_ref": relative["chart_map"],
        "chart_registry_ref": relative["chart_registry"],
        "blueprint_ref": relative["blueprint"],
        "site_ref": str(target_site.relative_to(store.context.run_root)),
    }
    receipt["output_hashes"] = {
        "fixture_sha256": hashlib.sha256((artifact_root / filenames["fixture"]).read_bytes()).hexdigest(),
        "chart_map_sha256": hashlib.sha256((artifact_root / filenames["chart_map"]).read_bytes()).hexdigest(),
        "chart_registry_sha256": hashlib.sha256((artifact_root / filenames["chart_registry"]).read_bytes()).hexdigest(),
        "blueprint_sha256": blueprint_hash,
        "site_manifest_sha256": hashlib.sha256(site_manifest_path.read_bytes()).hexdigest(),
    }
    if isinstance(receipt.get("blueprint_binding"), dict):
        receipt["blueprint_binding"]["ref"] = relative["blueprint"]
        receipt["blueprint_binding"]["sha256"] = blueprint_hash
    receipt_path.write_bytes(
        (json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )

    manifest_path = artifact_root / filenames["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dashboard"] = {
        "receipt_ref": relative["receipt"],
        "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
    }
    manifest_path.write_bytes(
        (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )

    candidate = replace(source, artifact_bindings=bindings, candidate_hash=None)
    persisted = store.record_candidate(candidate, revision_id=revision.revision_id)
    return revision, persisted


class ProductAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        runs_root = root / "runs"
        run_root = _run(runs_root / "RUN-SIDECAR")
        _write_valid_product(run_root)
        settings = LaunchSettings(
            runtime_root=root,
            runs_root=runs_root,
            source_roots=(root,),
            launch_token="server-token",
        )
        repository = OperationalRepository(None, [runs_root], launch_state_root=settings.state_root)
        self.server = OperationalServer(
            ("127.0.0.1", 0),
            repository,
            LaunchManager(settings, repository=repository),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.run_id = repository.list_runs()[0]["id"]
        self.index = run_root / "products" / "site" / "index.html"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path: str) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=4)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, body, headers

    def test_validated_generated_index_and_css_are_served_same_origin(self) -> None:
        base = f"/api/product/dashboard/{self.run_id}"
        status, body, headers = self.request(f"{base}/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.index.read_bytes())
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox allow-scripts", headers["content-security-policy"])
        self.assertIn("script-src 'self'", headers["content-security-policy"])
        self.assertIn("connect-src 'none'", headers["content-security-policy"])
        self.assertEqual(
            headers["content-security-policy"],
            "sandbox allow-scripts; default-src 'none'; script-src 'self'; connect-src 'none'; "
            "style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'",
        )
        # A CSP-sandboxed document has an opaque origin; same-origin CORP
        # would block its own hash-bound subresources.
        self.assertEqual(headers["cross-origin-resource-policy"], "cross-origin")

        status, body, headers = self.request(f"{base}/assets/dashboard.css")
        self.assertEqual(status, 200)
        self.assertIn(b"color", body)
        self.assertTrue(headers["content-type"].startswith("text/css"))

        snapshot = self.server.repository.snapshot(self.run_id)
        self.assertEqual(
            snapshot["productDashboard"]["dashboardUrl"],
            f"{base}/index.html",
        )

    def test_product_route_rejects_traversal_unlisted_and_tampered_assets(self) -> None:
        base = f"/api/product/dashboard/{self.run_id}"
        for relative in ("../products/fixture.json", "%2e%2e/products/fixture.json", "secret.txt", "site_manifest.json"):
            status, _body, _headers = self.request(f"{base}/{relative}")
            self.assertEqual(status, 404, relative)

        original = self.index.read_bytes()
        replacement = bytes([original[0] ^ 1]) + original[1:]
        self.index.write_bytes(replacement)
        status, _body, _headers = self.request(f"{base}/index.html")
        self.assertEqual(status, 404)

    def test_product_cache_digest_detects_same_size_same_mtime_tamper(self) -> None:
        snapshot = self.server.repository.snapshot(self.run_id)
        self.assertTrue(snapshot["productDashboard"]["valid"])
        original = self.index.read_bytes()
        stat = self.index.stat()
        self.index.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        os.utime(self.index, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        tampered = self.server.repository.snapshot(self.run_id)
        self.assertFalse(tampered["productDashboard"]["valid"])

    def test_product_cache_digest_detects_same_size_same_mtime_receipt_tamper(self) -> None:
        snapshot = self.server.repository.snapshot(self.run_id)
        self.assertTrue(snapshot["productDashboard"]["valid"])
        receipt = self.index.parents[1] / "receipt.json"
        original = receipt.read_bytes()
        stat = receipt.stat()
        self.assertGreater(len(original), 0)
        receipt.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        os.utime(receipt, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        tampered = self.server.repository.snapshot(self.run_id)
        self.assertFalse(tampered["productDashboard"]["valid"])

    def test_active_revision_pointer_swap_serves_new_assets_and_failed_target_keeps_prior(self) -> None:
        """Projection follows the store pointer, never generation-root sidecars."""

        run_root = self.index.parents[2]
        # The HTTP route uses a path-derived public id, while durable Product
        # sidecars bind the run's canonical ``run_state.run_id``.
        context = RunContext("RUN-SIDECAR", run_root)
        store = ProductReviewStore(context, "G-0001")
        pointer_one = store.load_active_revision()
        self.assertIsNotNone(pointer_one)
        self.assertEqual(pointer_one.revision_id, "rev-0001")
        source = store.load_candidate()
        baseline = self.server.repository.snapshot(self.run_id)["productDashboard"]
        self.assertTrue(baseline["valid"])
        self.assertIn("product_revisions/rev-0001", baseline["candidateRef"])
        status, body, _headers = self.request(f"/api/product/dashboard/{self.run_id}/index.html")
        self.assertEqual(status, 200)
        prior_bytes = body

        revision_two, candidate_two = _materialize_product_revision(
            store,
            source,
            request_id="projection-revision-two",
            marker=b"<!doctype html><main>revision-two</main>\n",
        )
        store.record_review(
            reviewer_ref="projection-reviewer-two",
            verdict="accept",
            candidate_hash=candidate_two.computed_hash,
            revision_id=revision_two.revision_id,
            reviewed_at="2026-09-03T00:00:01Z",
        )
        # A reviewed target is not current yet; projection and serving remain
        # bound to the prior accepted pointer.
        before_accept = self.server.repository.snapshot(self.run_id)["productDashboard"]
        self.assertIn("product_revisions/rev-0001", before_accept["candidateRef"])
        status, body, _headers = self.request(f"/api/product/dashboard/{self.run_id}/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(body, prior_bytes)

        store.activate_revision(revision_two.revision_id)
        after_accept = self.server.repository.snapshot(self.run_id)["productDashboard"]
        self.assertTrue(after_accept["valid"])
        self.assertIn("product_revisions/rev-0002", after_accept["candidateRef"])
        self.assertIn("product_revisions/rev-0002", after_accept["reviewRef"])
        status, body, _headers = self.request(f"/api/product/dashboard/{self.run_id}/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b"revision-two", body)

        revision_three, candidate_three = _materialize_product_revision(
            store,
            store.load_candidate(),
            request_id="projection-revision-three",
            marker=b"<!doctype html><main>revision-three-failed</main>\n",
        )
        store.record_review(
            reviewer_ref="projection-reviewer-three",
            verdict="block",
            candidate_hash=candidate_three.computed_hash,
            revision_id=revision_three.revision_id,
            reviewed_at="2026-09-03T00:00:02Z",
        )
        store.fail_revision(revision_three.revision_id)
        after_failure = self.server.repository.snapshot(self.run_id)["productDashboard"]
        self.assertIn("product_revisions/rev-0002", after_failure["candidateRef"])
        status, body, _headers = self.request(f"/api/product/dashboard/{self.run_id}/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b"revision-two", body)
        self.assertNotIn(b"revision-three-failed", body)

    def test_g0001_active_generation_manifest_is_included_in_cache_identity(self) -> None:
        run_root = self.index.parents[2]
        root_manifest = run_root / "products" / "product_manifest.json"
        generation_manifest = run_root / "products" / "generations" / "G-0001" / "product_manifest.json"
        generation_manifest.parent.mkdir(parents=True, exist_ok=True)
        generation_manifest.write_bytes(root_manifest.read_bytes())
        (run_root / "active_generation.json").write_text(
            json.dumps({"generation_id": "G-0001", "manifest_ref": "products/product_manifest.json"}),
            encoding="utf-8",
        )
        summary = self.server.repository.get(self.run_id).summary
        identity = _product_signature_identity(run_root, summary)
        self.assertIsNotNone(identity)
        self.assertTrue(any(item[1] == "products/generations/G-0001/product_manifest.json" for item in identity if isinstance(item, tuple) and len(item) > 1))
        original = generation_manifest.read_bytes()
        stat = generation_manifest.stat()
        generation_manifest.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        os.utime(generation_manifest, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        tampered = _product_signature_identity(run_root, summary)
        self.assertNotEqual(identity, tampered)


if __name__ == "__main__":
    unittest.main()
