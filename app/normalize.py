"""Manifest normalization and semantic comparison.

Comparison rules (per spec):
* YAML comments are ignored (we parse into Python objects, comments never survive).
* Document order is ignored (documents are sorted by a stable identity key).
* Mapping key order is ignored (dicts compare by content).
* List order is significant (lists compare element by element, in order).
"""
from __future__ import annotations

from typing import Any

import yaml


def parse_documents(rendered: str) -> list[Any]:
    """Parse a multi-document helm template output, dropping empty documents."""
    docs: list[Any] = []
    for doc in yaml.safe_load_all(rendered):
        if doc is None:
            continue
        docs.append(doc)
    return docs


def _doc_identity(doc: Any) -> str:
    if isinstance(doc, dict):
        kind = doc.get("kind", "")
        meta = doc.get("metadata") or {}
        name = meta.get("name", "")
        namespace = meta.get("namespace", "")
        return f"{kind}/{namespace}/{name}"
    # Non-mapping documents are rare; fall back to content hash.
    return "raw/" + repr(doc)


def normalize_manifests(rendered: str) -> list[Any]:
    """Canonical form: documents sorted by identity; list order preserved."""
    docs = parse_documents(rendered)
    return sorted(docs, key=_doc_identity)


def manifests_equal(left: str, right: str) -> bool:
    """Semantic equality under the rules in the module docstring."""
    return normalize_manifests(left) == normalize_manifests(right)
