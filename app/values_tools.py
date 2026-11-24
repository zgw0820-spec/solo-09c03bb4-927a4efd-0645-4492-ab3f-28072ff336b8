"""Values YAML loading, leaf-path enumeration and key removal.

A "mapping key path" addresses a key inside a nested dict, e.g.
``image.pullPolicy`` or ``autoscaling.minReplicas``. Only leaf keys (whose
value is not a dict) are returned as candidates, and no candidate path is a
prefix of another.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_values(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"values file {path} does not contain a YAML mapping")
    return data


def dump_values(data: dict[str, Any]) -> str:
    """Stable, readable YAML output (sorted mapping keys, block style)."""
    return yaml.safe_dump(
        data,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
    )


def leaf_paths(data: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Return paths of leaf mapping keys.

    Dicts are recursed into; lists/scalars terminate the walk. Every returned
    path is therefore non-contained: none is a strict prefix of another.
    """
    paths: list[tuple[str, ...]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            here = prefix + (str(key),)
            if isinstance(value, dict):
                paths.extend(leaf_paths(value, here))
            else:
                paths.append(here)
    return paths


def render_path(path: tuple[str, ...]) -> str:
    """Dotted notation. Keys containing '.' are quoted so the path round-trips."""
    parts: list[str] = []
    for part in path:
        if "." in part or '"' in part:
            parts.append('"' + part.replace('"', '\\"') + '"')
        else:
            parts.append(part)
    return ".".join(parts)


def parse_path(text: str) -> tuple[str, ...]:
    """Parse dotted notation, honoring double-quoted segments."""
    parts: list[str] = []
    buf = ""
    quoted = False
    i = 0
    while i < len(text):
        ch = text[i]
        if quoted:
            if ch == '"':
                quoted = False
            elif ch == "\\" and i + 1 < len(text) and text[i + 1] == '"':
                buf += '"'
                i += 1
            else:
                buf += ch
        elif ch == '"':
            quoted = True
        elif ch == ".":
            parts.append(buf)
            buf = ""
        else:
            buf += ch
        i += 1
    if quoted:
        raise ValueError(f"unterminated quoted segment in path: {text!r}")
    parts.append(buf)
    if any(p == "" for p in parts):
        raise ValueError(f"empty segment in path: {text!r}")
    return tuple(parts)


def remove_paths(data: dict[str, Any], paths: list[tuple[str, ...]]) -> dict[str, Any]:
    """Deep-copy ``data`` and drop the given leaf paths.

    Empty parent mappings left behind are pruned so removing every child of a
    mapping removes the mapping itself. Unknown paths raise KeyError.
    """
    import copy

    result = copy.deepcopy(data)
    for path in paths:
        node: Any = result
        for key in path[:-1]:
            if not isinstance(node, dict) or key not in node:
                raise KeyError(render_path(path))
            node = node[key]
        if not isinstance(node, dict) or path[-1] not in node:
            raise KeyError(render_path(path))
        del node[path[-1]]
    _prune_empty(result)
    return result


def _prune_empty(node: Any) -> None:
    if not isinstance(node, dict):
        return
    for key in list(node.keys()):
        child = node[key]
        if isinstance(child, dict):
            _prune_empty(child)
            if not child:
                del node[key]
