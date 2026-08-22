from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import pytest

from scripts import install_skill_release as installer


def _synthetic_release(root: Path) -> tuple[Path, str]:
    skill = root / "source" / installer.SKILL_NAME
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {installer.SKILL_NAME}\n"
        f'version: "{installer.SKILL_VERSION}"\n'
        "core_name: auto_foundry_core\n"
        f'core_version: "{installer.CORE_VERSION}"\n'
        f"release: {installer.RELEASE_SLUG}\n"
        "---\n\n"
        f"skill_name: {installer.SKILL_NAME}\n"
        f"skill_version: {installer.SKILL_VERSION}\n"
        "core_name: auto_foundry_core\n"
        f"core_version: {installer.CORE_VERSION}\n",
        encoding="utf-8",
    )
    (skill / "README.md").write_text("synthetic release\n", encoding="utf-8")
    archive = root / "release.zip"
    payload = installer._deterministic_zip_bytes(skill)
    archive.write_bytes(payload)
    return archive, hashlib.sha256(payload).hexdigest()


def test_synthetic_release_stages_and_moves_backups_outside_discovery() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        archive, digest = _synthetic_release(root)
        skills = root / "skills"
        active = skills / installer.SKILL_NAME
        active.mkdir(parents=True)
        (active / "old.txt").write_text("old", encoding="utf-8")
        backup = skills / f"{installer.SKILL_NAME}.backup"
        backup.mkdir()
        (backup / "SKILL.md").write_text("old backup", encoding="utf-8")

        result = installer.install_skill_release(
            archive,
            skills,
            expected_sha256=digest,
            expected_file_count=2,
        )

        assert result["installed"] is True
        assert (active / "SKILL.md").is_file()
        assert not (active / "old.txt").exists()
        assert not backup.exists()
        archives = list((root / "skill-archives").iterdir())
        assert len(archives) == 2
        assert all(not path.is_relative_to(skills) for path in archives)


def test_synthetic_release_swap_rolls_back_after_activation() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        archive, digest = _synthetic_release(root)
        skills = root / "skills"
        active = skills / installer.SKILL_NAME
        active.mkdir(parents=True)
        (active / "old.txt").write_text("old", encoding="utf-8")

        def failpoint(name: str) -> None:
            if name == "after_activate":
                raise RuntimeError("simulated crash")

        with pytest.raises(installer.ReleaseInstallError, match="rolled back"):
            installer.install_skill_release(
                archive,
                skills,
                expected_sha256=digest,
                expected_file_count=2,
                failpoint=failpoint,
            )
        assert (active / "old.txt").read_text(encoding="utf-8") == "old"
        assert not (active / "SKILL.md").exists()


def test_process_death_after_archive_recovers_from_intent_journal() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        archive, digest = _synthetic_release(root)
        skills = root / "skills"
        active = skills / installer.SKILL_NAME
        active.mkdir(parents=True)
        (active / "old.txt").write_text("old", encoding="utf-8")
        script = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "from scripts import install_skill_release as i\n"
            "i.install_skill_release(Path(sys.argv[1]), Path(sys.argv[2]), "
            "expected_sha256=sys.argv[3], expected_file_count=2, "
            "failpoint=lambda name: os._exit(23) if name == 'after_archive' else None)\n"
        )
        environment = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        }
        child = subprocess.run(
            [sys.executable, "-c", script, str(archive), str(skills), digest],
            env=environment,
            check=False,
        )
        assert child.returncode == 23
        transaction = root / f".{installer.SKILL_NAME}-installer"
        assert (transaction / "swap.intent.json").is_file()
        result = installer.install_skill_release(
            archive,
            skills,
            expected_sha256=digest,
            expected_file_count=2,
        )
        assert result["installed"] is True
        assert (active / "SKILL.md").is_file()
        assert not (active / "old.txt").exists()
        assert not (transaction / "swap.intent.json").exists()


def test_two_process_install_is_locked_and_idempotent() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        archive, digest = _synthetic_release(root)
        skills = root / "skills"
        skills.mkdir()
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from scripts import install_skill_release as i\n"
            "print(json.dumps(i.install_skill_release(Path(sys.argv[1]), Path(sys.argv[2]), "
            "expected_sha256=sys.argv[3], expected_file_count=2), default=list))\n"
        )
        environment = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        }
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(archive), str(skills), digest],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        outputs = [process.communicate(timeout=30) for process in processes]
        assert all(process.returncode == 0 for process in processes), outputs
        results = [json.loads(stdout) for stdout, _ in outputs]
        assert sum(bool(result["already_current"]) for result in results) == 1
        assert (skills / installer.SKILL_NAME / "SKILL.md").is_file()
        assert len(list((root / "skill-archives").iterdir())) == 0


def test_symlinked_zip_root_and_archive_paths_fail_closed() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        archive, digest = _synthetic_release(root)
        skills = root / "skills"
        skills.mkdir()
        zip_alias = root / "release-alias.zip"
        zip_alias.symlink_to(archive)
        with pytest.raises(installer.ReleaseInstallError, match="symlink"):
            installer.inspect_release(zip_alias, expected_sha256=digest, expected_file_count=2)

        skills_alias = root / "skills-alias"
        skills_alias.symlink_to(skills, target_is_directory=True)
        with pytest.raises(installer.ReleaseInstallError, match="symlink"):
            installer.install_skill_release(
                archive,
                skills_alias,
                expected_sha256=digest,
                expected_file_count=2,
            )

        archive_root = root / "archives"
        archive_root.mkdir()
        archive_alias = root / "archives-alias"
        archive_alias.symlink_to(archive_root, target_is_directory=True)
        with pytest.raises(installer.ReleaseInstallError, match="symlink"):
            installer.install_skill_release(
                archive,
                skills,
                archive_root=archive_alias,
                expected_sha256=digest,
                expected_file_count=2,
            )
