"""Generated Capability Catalog accessors."""

from __future__ import annotations

import re
from typing import Iterable

from .capabilities import DESCRIPTORS
from .contracts import CapabilityDescriptor


def capability_catalog() -> list[CapabilityDescriptor]:
    """Return descriptors generated from the executable capability metadata."""

    return [DESCRIPTORS[key] for key in sorted(DESCRIPTORS)]


def get_capability(capability_id: str) -> CapabilityDescriptor:
    try:
        return DESCRIPTORS[capability_id]
    except KeyError as exc:
        raise KeyError(f"unknown capability: {capability_id}") from exc


def search_capabilities(text: str) -> list[CapabilityDescriptor]:
    needle = str(text).strip().lower()
    if not needle:
        return capability_catalog()
    tokens = tuple(re.findall(r"[a-z0-9_]+", needle))
    matches: list[CapabilityDescriptor] = []
    for descriptor in capability_catalog():
        metadata = descriptor.metadata
        search_terms = metadata.get("search_terms", ()) if hasattr(metadata, "get") else ()
        searchable = descriptor.to_json().lower() + " " + " ".join(str(term).lower() for term in search_terms)
        if needle in searchable or all(token in searchable for token in tokens):
            matches.append(descriptor)
    return matches


def catalog_json() -> list[dict]:
    return [descriptor.to_dict() for descriptor in capability_catalog()]


list_capabilities = capability_catalog
describe = get_capability
search = search_capabilities

__all__ = ["capability_catalog", "catalog_json", "describe", "get_capability", "list_capabilities", "search", "search_capabilities"]
