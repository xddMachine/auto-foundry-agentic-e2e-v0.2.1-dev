"""Clean-room path validation and run workspace helpers."""

from __future__ import annotations

from dataclasses import dataclass
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


def require_allowed_roots(
    allowed_roots: Iterable[str | os.PathLike[str]] | None,
    *,
    context: str = "filesystem operation",
) -> tuple[str, ...]:
    """Require one declared execution root at an execution boundary.

    Low-level readers may intentionally remain general; catalog execution,
    manifests, reproduction, and CLI output call this helper before touching a
    filesystem path.
    """

    if allowed_roots is None:
        raise AllowedRootError(f"{context} requires nonempty allowed_roots")
    roots = tuple(str(root) for root in allowed_roots)
    if not roots:
        raise AllowedRootError(f"{context} requires nonempty allowed_roots")
    return roots


@dataclass(frozen=True)
class RunContext:
    """The small immutable boundary for one local analytical run.

    ``RunContext`` is deliberately a workspace convenience, not an
    authorization or host-sandbox mechanism.  It resolves symlinks before
    checking containment and never creates directories while validating a
    path.  A caller with unrestricted shell access can still bypass this
    package; true isolation requires a separate workspace/container or host
    allowlist.

    Relative input paths are looked up in the declared ``input_roots`` first
    and in ``run_root`` second.  Absolute paths must already be under one of
    those roots.  Run, product, and optimizer paths are always system-owned
    paths under this run's root.
    """

    run_id: str
    run_root: Path | str
    input_roots: tuple[Path | str, ...] = ()
    core_version: str = "0.3.0"
    skill_version: str | None = None

    def __post_init__(self) -> None:
        run_id = str(self.run_id).strip()
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ValueError("run_id must be a simple path component")
        root = _resolved(self.run_root)
        roots = tuple(_resolved(value) for value in (self.input_roots or ()))
        # The input roots may intentionally be outside the run root, but a
        # duplicate root is harmless and preserving declaration order makes
        # relative source lookup deterministic.
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "run_root", root)
        object.__setattr__(self, "input_roots", roots)
        object.__setattr__(self, "core_version", str(self.core_version))
        if self.skill_version is not None:
            object.__setattr__(self, "skill_version", str(self.skill_version))

    @property
    def product_root(self) -> Path:
        return self.run_root / "products"

    @property
    def optimizer_root(self) -> Path:
        return self.run_root / "optimizer"

    @property
    def cache_root(self) -> Path:
        return self.run_root / "cache"

    @property
    def telemetry_root(self) -> Path:
        return self.run_root / "telemetry"

    @property
    def read_roots(self) -> tuple[Path, ...]:
        """Roots accepted for source reads, in deterministic lookup order."""

        return self.input_roots + (self.run_root,)

    def _under(self, candidate: Path, roots: Iterable[Path], *, label: str) -> Path:
        resolved = _resolved(candidate)
        normalized = tuple(_resolved(root) for root in roots)
        if not any(resolved == root or root in resolved.parents for root in normalized):
            raise AllowedRootError(f"{label} escapes run context: {resolved}")
        return resolved

    def resolve_input(self, path: str | os.PathLike[str]) -> Path:
        """Resolve a source read under an input root or this run root.

        Relative paths are searched without probing outside the allowed roots.
        If no candidate exists yet, the first declared input root is used as
        the deterministic destination for the eventual read (or ``run_root``
        when no input root was declared).
        """

        raw = Path(path).expanduser()
        if raw.is_absolute():
            return self._under(raw, self.read_roots, label="input path")

        candidates: list[Path] = []
        for root in self.read_roots:
            candidate = _resolved(root / raw)
            # A symlinked candidate that resolves outside any allowed root is
            # an escape even if a later root happens to contain a valid file.
            if (root / raw).exists() or (root / raw).is_symlink():
                self._under(candidate, self.read_roots, label="input path")
            candidates.append(candidate)
        for candidate in candidates:
            if candidate.exists():
                return self._under(candidate, self.read_roots, label="input path")
        return self._under(candidates[0], self.read_roots, label="input path")

    def resolve_run_path(self, relative_or_path: str | os.PathLike[str]) -> Path:
        """Resolve a system-owned path under ``run_root`` without creating it."""

        raw = Path(relative_or_path).expanduser()
        candidate = raw if raw.is_absolute() else self.run_root / raw
        return self._under(candidate, (self.run_root,), label="run path")

    def resolve_product_path(self, relative_or_path: str | os.PathLike[str]) -> Path:
        """Resolve a product path under ``run_root/products``."""

        # Validate the designated subtree itself before resolving a child.  A
        # pre-existing ``products`` symlink to a sibling run must not redefine
        # the run boundary.
        product_root = self._under(self.run_root / "products", (self.run_root,), label="product root")
        raw = Path(relative_or_path).expanduser()
        candidate = raw if raw.is_absolute() else product_root / raw
        return self._under(candidate, (product_root,), label="product path")

    def resolve_optimizer_path(self, relative_or_path: str | os.PathLike[str]) -> Path:
        """Resolve an optimizer path under ``run_root/optimizer``."""

        optimizer_root = self._under(self.run_root / "optimizer", (self.run_root,), label="optimizer root")
        raw = Path(relative_or_path).expanduser()
        candidate = raw if raw.is_absolute() else optimizer_root / raw
        return self._under(candidate, (optimizer_root,), label="optimizer path")

    def ensure_run_dir(self, relative: str | os.PathLike[str] = "") -> Path:
        """Validate a run-relative directory, then create it."""

        destination = self.resolve_run_path(relative)
        destination.mkdir(parents=True, exist_ok=True)
        return destination


__all__ = ["AllowedRootError", "RunContext", "require_allowed_roots", "validate_allowed_path"]
