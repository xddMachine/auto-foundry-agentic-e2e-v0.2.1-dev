"""Clean-room path validation and run workspace helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
from typing import Iterable


class AllowedRootError(ValueError):
    """Raised when a source or output escapes the configured local roots."""


def _resolved(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def validate_allowed_path(path: str | os.PathLike[str], allowed_roots: Iterable[str | os.PathLike[str]]) -> Path:
    """Return a resolved path when it is inside one of ``allowed_roots``.

    Validation uses ``Path.is_relative_to`` semantics and therefore rejects
    sibling prefixes (``/tmp/data2`` is not inside ``/tmp/data``) as well as
    traversal through symlinks after resolution.
    """

    candidate = _resolved(path)
    roots = tuple(_resolved(root) for root in allowed_roots)
    if not roots:
        raise AllowedRootError("at least one allowed root is required")
    if not any(candidate == root or root in candidate.parents for root in roots):
        raise AllowedRootError(f"path is outside allowed roots: {candidate}")
    return candidate


@dataclass
class Workspace:
    """A bounded, local run workspace.

    The workspace never mutates a source.  It creates only explicitly named
    run-local directories on demand.
    """

    root: Path | str
    allowed_roots: tuple[Path | str, ...] | None = None
    run_id: str = "run"
    _created: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = _resolved(self.root)
        roots = self.allowed_roots or (self.root,)
        self.allowed_roots = tuple(_resolved(r) for r in roots)
        validate_allowed_path(self.root, self.allowed_roots)
        if not self.run_id or Path(self.run_id).name != self.run_id:
            raise ValueError("run_id must be a simple path component")

    @property
    def run_dir(self) -> Path:
        value = self.root / self.run_id
        validate_allowed_path(value, self.allowed_roots)
        return value

    def source_path(self, path: str | os.PathLike[str]) -> Path:
        return validate_allowed_path(path, self.allowed_roots)

    def output_path(self, path: str | os.PathLike[str]) -> Path:
        candidate = _resolved(path)
        validate_allowed_path(candidate, (self.run_dir,))
        return candidate

    def ensure(self, relative: str = "") -> Path:
        candidate = self.run_dir / relative
        candidate = self.output_path(candidate)
        candidate.mkdir(parents=True, exist_ok=True)
        self._created.add(str(candidate))
        return candidate

    def cache_dir(self) -> Path:
        return self.ensure("cache")

    def telemetry_dir(self) -> Path:
        return self.ensure("telemetry")

    def manifest_dir(self) -> Path:
        return self.ensure("manifests")


__all__ = ["AllowedRootError", "Workspace", "validate_allowed_path"]
