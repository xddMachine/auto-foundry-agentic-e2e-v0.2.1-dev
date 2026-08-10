from __future__ import annotations

import hashlib
import json
from pathlib import Path
import textwrap
import zipfile

import pytest

from auto_foundry_core.analysis import (
    ANALYSIS_CONTEXT_ENV,
    BoundAnalysisContext,
    load_bound_analysis_context,
)
from auto_foundry_core.contracts import DataAssetRef
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.workspace import AllowedRootError, RunContext


def _fixture(tmp_path: Path) -> tuple[RunContext, Path, ItemWorkspace, BoundAnalysisContext]:
    inputs = tmp_path / "inputs"
    run = tmp_path / "run"
    inputs.mkdir()
    run.mkdir()
    archive = inputs / "room.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", b"order_id,region\nA-1,DE\nA-2,FR\n")
    context = RunContext("RUN-ANALYSIS-RUNTIME", run, (inputs,), core_version="0.3.0-test")
    item = ItemWorkspace.create(context, "Q-001", original_text="Summarize the fixture.")
    bound = BoundAnalysisContext.create(
        context,
        DataAssetRef.from_path(archive),
        item,
        ontology_bundle={"relevant": ("orders",)},
    )
    return context, archive, item, bound


def _write_script(item: ItemWorkspace, name: str, source: str) -> Path:
    path = item.work_root / "calculations" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def test_bound_context_manifest_and_environment_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context, archive, item, bound = _fixture(tmp_path)
    assert bound.source_identity.content_hash == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert bound.source_catalog.entries
    assert bound.prepared_assets.search() == ()
    assert bound.ontology_bundle["relevant"] == ("orders",)

    monkeypatch.setenv(ANALYSIS_CONTEXT_ENV, str(bound.manifest_path))
    loaded = load_bound_analysis_context(context)
    assert loaded.item_workspace.item_id == item.item_id
    assert loaded.source_catalog.content_hash == bound.source_catalog.content_hash
    assert loaded.manifest_hash == bound.manifest_hash

    payload = json.loads(bound.manifest_path.read_text(encoding="utf-8"))
    payload["source_identity"]["uri"] = str(tmp_path / "wrong.zip")
    bound.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest hash"):
        load_bound_analysis_context(context, path=bound.manifest_path)


def test_context_source_mutation_fails_closed_before_script(tmp_path: Path) -> None:
    context, archive, item, bound = _fixture(tmp_path)
    script = _write_script(
        item,
        "ok.py",
        """
        from pathlib import Path
        Path('ran.txt').write_text('ran', encoding='utf-8')
        """,
    )
    archive.write_bytes(archive.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="source changed"):
        bound.script_runner.run_pipeline(script)
    assert not (item.work_root / "ran.txt").exists()


@pytest.mark.parametrize("target", ("manifest", "source", "catalog"))
def test_child_context_mutation_fails_closed_and_does_not_publish(tmp_path: Path, target: str) -> None:
    _, _, item, bound = _fixture(tmp_path)
    output = item.work_root / "tampered-output.json"
    output.write_text("accepted-before-child", encoding="utf-8")
    script = _write_script(
        item,
        f"tamper_{target}.py",
        f"""
        from pathlib import Path
        from auto_foundry_core.analysis import load_bound_analysis_context
        ctx = load_bound_analysis_context()
        Path('tampered-output.json').write_text('must-not-publish', encoding='utf-8')
        target = {{'manifest': ctx.manifest_path, 'source': ctx.source_identity.uri, 'catalog': ctx.source_catalog.path}}['{target}']
        Path(target).write_text('tampered', encoding='utf-8')
        """,
    )
    report = bound.script_runner.run_pipeline(script, allowed_outputs=(output,))
    assert report.status == "failed"
    assert report.same_attempt_feedback
    assert report.receipts[0].error_category == "context_integrity_failure"
    assert output.read_text(encoding="utf-8") == "accepted-before-child"


def test_script_pipeline_smoke_then_full_and_context_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, item, bound = _fixture(tmp_path)
    monkeypatch.setenv("AUTO_FOUNDRY_TEST_SECRET", "must-not-cross-boundary")
    output = item.work_root / "result.json"
    script = _write_script(
        item,
        "analysis.py",
        """
        import json
        import os
        from pathlib import Path
        from auto_foundry_core.analysis import load_bound_analysis_context
        # The normal path is environment-bound; no path or source hash is in this script.
        ctx = load_bound_analysis_context()
        Path('result.json').write_text(json.dumps({'phase': os.environ['AUTO_FOUNDRY_ANALYSIS_PHASE'], 'rows': ctx.source_catalog.counts.catalog_entries, 'secret': os.environ.get('AUTO_FOUNDRY_TEST_SECRET')}), encoding='utf-8')
        print(os.environ['AUTO_FOUNDRY_ANALYSIS_PHASE'])
        """,
    )
    report = bound.script_runner.run_pipeline(script, allowed_outputs=(output,), sample_limit=2)
    assert report.succeeded
    assert [receipt.phase for receipt in report.receipts] == ["smoke", "full"]
    assert report.receipts[0].stdout.strip() == "smoke"
    assert report.receipts[1].stdout.strip() == "full"
    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8"))["secret"] is None


def test_compile_name_import_errors_are_same_attempt_feedback(tmp_path: Path) -> None:
    _, _, item, bound = _fixture(tmp_path)
    syntax = _write_script(item, "syntax.py", "if True print('bad')")
    syntax_report = bound.script_runner.run_pipeline(syntax)
    assert syntax_report.same_attempt_feedback
    assert syntax_report.receipts[0].phase == "compile"
    assert syntax_report.receipts[0].error_type == "SyntaxError"

    runtime = _write_script(item, "runtime.py", "raise NameError('repair me')")
    runtime_report = bound.script_runner.run_pipeline(runtime)
    assert runtime_report.same_attempt_feedback
    assert runtime_report.receipts[0].phase == "smoke"
    assert runtime_report.receipts[0].error_type == "NameError"
    assert len(runtime_report.receipts) == 1

    dependency = _write_script(item, "dependency.py", "import package_that_is_not_installed")
    dependency_report = bound.script_runner.run_pipeline(dependency)
    assert dependency_report.same_attempt_feedback
    assert dependency_report.receipts[0].phase == "dependency_check"


def test_output_escape_timeout_and_output_cap_are_rejected_or_bounded(tmp_path: Path) -> None:
    _, _, item, bound = _fixture(tmp_path)
    script = _write_script(item, "escape.py", "from pathlib import Path\nPath('../escape.txt').write_text('x')")
    outside = item.work_root.parent / "escape.txt"
    with pytest.raises(AllowedRootError):
        bound.script_runner.run_pipeline(script, allowed_outputs=(outside,))
    with pytest.raises(AllowedRootError):
        bound.script_runner.run_pipeline(script, allowed_outputs=(Path("../escape.txt"),))
    assert not outside.exists()

    slow = _write_script(item, "slow.py", "import time\ntime.sleep(2)")
    timeout = bound.script_runner.run_pipeline(slow, timeout_seconds=0.05)
    assert timeout.status == "failed"
    assert timeout.receipts[0].timed_out
    assert timeout.receipts[0].error_category == "runtime_timeout"
    assert timeout.same_attempt_feedback

    noisy = _write_script(item, "noisy.py", "print('x' * 1000000)")
    capped = bound.script_runner.run_pipeline(noisy, output_bytes=128)
    assert capped.status == "failed"
    assert capped.receipts[0].output_limited or capped.receipts[0].stdout_truncated
    assert len(capped.receipts[0].stdout) <= 128
    assert capped.same_attempt_feedback


def test_runner_manifest_timeout_and_output_integrity_guards(tmp_path: Path) -> None:
    context, _, item, bound = _fixture(tmp_path)
    assert bound.runner_config["default_timeout_seconds"] == 3600.0
    manifest = json.loads(bound.manifest_path.read_text(encoding="utf-8"))
    assert manifest["runner_config"]["default_timeout_seconds"] == 3600.0

    undeclared = _write_script(
        item,
        "undeclared.py",
        "from pathlib import Path\nPath('undeclared.txt').write_text('no', encoding='utf-8')\n",
    )
    report = bound.script_runner.run_pipeline(undeclared)
    assert report.status == "failed"
    assert report.receipts[0].error_type == "UndeclaredOutput"
    assert not (item.work_root / "undeclared.txt").exists()

    bytecode = _write_script(
        item,
        "bytecode.py",
        "from pathlib import Path\nPath('manual.pyc').write_bytes(b'bad')\n",
    )
    report = bound.script_runner.run_pipeline(bytecode)
    assert report.status == "failed"
    assert report.receipts[0].error_type == "BytecodeArtifact"
    assert not any(path.suffix == ".pyc" for path in context.run_root.rglob("*"))

    preexisting = item.work_root / "preexisting.txt"
    preexisting.write_text("before", encoding="utf-8")
    mutate = _write_script(
        item,
        "mutate_preexisting.py",
        "from pathlib import Path\nPath('preexisting.txt').write_text('mutated', encoding='utf-8')\n",
    )
    report = bound.script_runner.run_pipeline(mutate)
    assert report.status == "failed"
    assert report.receipts[0].error_type == "UndeclaredOutput"
    assert preexisting.read_text(encoding="utf-8") == "before"


def test_runner_subprocess_oserror_rolls_back_child_declared_and_undeclared_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, item, bound = _fixture(tmp_path)
    output = item.work_root / "child-output.json"
    output.write_text("before", encoding="utf-8")
    undeclared = item.work_root / "child-undeclared.txt"
    script = _write_script(item, "child_oserror.py", "print('child')")

    import auto_foundry_core.analysis as analysis_module

    def child_writes_then_process_lookup_error(*args: object, **kwargs: object) -> object:
        cwd = Path(str(kwargs["cwd"]))
        (cwd / output.name).write_text("mutated", encoding="utf-8")
        (cwd / undeclared.name).write_text("must-roll-back", encoding="utf-8")
        raise ProcessLookupError("child disappeared")

    monkeypatch.setattr(analysis_module.subprocess, "Popen", child_writes_then_process_lookup_error)
    report = bound.script_runner.run_pipeline(script, allowed_outputs=(output,))

    assert report.status == "failed"
    assert report.receipts[0].error_type == "UndeclaredOutput"
    assert output.read_text(encoding="utf-8") == "before"
    assert not undeclared.exists()


def test_runner_subprocess_oserror_rolls_back_declared_write_without_early_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, item, bound = _fixture(tmp_path)
    output = item.work_root / "child-output.json"
    output.write_text("before", encoding="utf-8")
    script = _write_script(item, "child_oserror_declared.py", "print('child')")

    import auto_foundry_core.analysis as analysis_module

    def child_writes_then_oserror(*args: object, **kwargs: object) -> object:
        cwd = Path(str(kwargs["cwd"]))
        (cwd / output.name).write_text("mutated", encoding="utf-8")
        raise OSError("transport failed")

    monkeypatch.setattr(analysis_module.subprocess, "Popen", child_writes_then_oserror)
    report = bound.script_runner.run_pipeline(script, allowed_outputs=(output,))

    assert report.status == "failed"
    assert report.receipts[0].error_type == "OSError"
    assert report.receipts[0].error_category == "same_attempt_feedback"
    assert output.read_text(encoding="utf-8") == "before"


def test_deterministic_rerun_match_and_mismatch_do_not_publish_mismatch(tmp_path: Path) -> None:
    _, _, item, bound = _fixture(tmp_path)
    output = item.work_root / "deterministic.json"
    stable = _write_script(
        item,
        "stable.py",
        "from pathlib import Path\nPath('deterministic.json').write_text('{\"value\": 1}', encoding='utf-8')",
    )
    passed = bound.script_runner.run_pipeline(stable, allowed_outputs=(output,), deterministic_outputs=(output,))
    assert passed.succeeded and passed.deterministic_match is True
    assert [receipt.phase for receipt in passed.receipts] == ["smoke", "full", "full"]
    assert output.read_text(encoding="utf-8") == '{"value": 1}'

    changing = _write_script(
        item,
        "changing.py",
        "import time\nfrom pathlib import Path\nPath('deterministic.json').write_text(str(time.time_ns()), encoding='utf-8')",
    )
    before = output.read_bytes()
    failed = bound.script_runner.run_pipeline(changing, allowed_outputs=(output,), deterministic_outputs=(output,))
    assert failed.status == "failed"
    assert failed.deterministic_match is False
    assert output.read_bytes() == before


def test_deterministic_materialization_requires_every_declared_scratch_output(
    tmp_path: Path,
) -> None:
    _, _, item, bound = _fixture(tmp_path)
    deterministic = item.work_root / "deterministic.json"
    non_deterministic = item.work_root / "optional.json"
    deterministic.write_text("accepted-deterministic", encoding="utf-8")
    non_deterministic.write_text("accepted-optional", encoding="utf-8")
    script = _write_script(
        item,
        "missing_optional.py",
        "from pathlib import Path\nPath('deterministic.json').write_text('{\"value\": 1}', encoding='utf-8')",
    )

    report = bound.script_runner.run_pipeline(
        script,
        allowed_outputs=(deterministic, non_deterministic),
        deterministic_outputs=(deterministic,),
    )
    assert report.status == "failed"
    assert report.same_attempt_feedback
    assert report.error_category == "deterministic_output_missing"
    assert deterministic.read_text(encoding="utf-8") == "accepted-deterministic"
    assert non_deterministic.read_text(encoding="utf-8") == "accepted-optional"


def test_deterministic_materialization_rolls_back_all_targets_on_late_write_failure(
    tmp_path: Path,
) -> None:
    _, _, item, bound = _fixture(tmp_path)
    first_target = item.work_root / "first.json"
    second_target = item.work_root / "second.json"
    first_target.write_text("sentinel-first", encoding="utf-8")
    second_target.write_text("sentinel-second", encoding="utf-8")
    script = _write_script(
        item,
        "two_outputs.py",
        "from pathlib import Path\n"
        "Path('first.json').write_text('{\"value\": 1}', encoding='utf-8')\n"
        "Path('second.json').write_text('{\"value\": 2}', encoding='utf-8')\n",
    )
    runner = bound.script_runner
    original_write = runner._atomic_write_bytes

    def fail_second(path: Path, content: bytes) -> None:
        if path.name == "second.json" and path.parent == item.work_root:
            raise OSError("injected second-target write failure")
        original_write(path, content)

    runner._atomic_write_bytes = fail_second  # type: ignore[method-assign]
    try:
        report = runner.run_pipeline(
            script,
            allowed_outputs=(first_target, second_target),
            deterministic_outputs=(first_target, second_target),
        )
    finally:
        runner._atomic_write_bytes = original_write  # type: ignore[method-assign]
    assert report.status == "failed"
    assert report.same_attempt_feedback
    assert report.error_category == "same_attempt_feedback"
    assert report.error_type == "OSError"
    assert first_target.read_text(encoding="utf-8") == "sentinel-first"
    assert second_target.read_text(encoding="utf-8") == "sentinel-second"
