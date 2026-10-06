"""Edit Kubernetes object dicts by path.

Two notations reach this module: RFC 6902 JSON patches (the catalog's injections, and some
agent fixes) and the dotted YAML fragments the agent is prompted to write
(`spec.template.spec.containers[0].env: [...]`). Both are applied to a deep copy, so a caller
can diff old against new - which is what the remediation policy needs - before anything is
sent to the cluster.
"""

import copy
import re
from typing import Any

_INDEXED = re.compile(r"^(?P<name>[^\[\]]+)(?:\[(?P<index>\d+)\])?$")


class PathError(ValueError):
    pass


# --- RFC 6902 -----------------------------------------------------------------------------


def _pointer(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise PathError(f"JSON pointer must start with '/': {path!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _container(doc: Any, part: str) -> Any:
    if isinstance(doc, list):
        try:
            return doc[int(part)]
        except (ValueError, IndexError) as exc:
            raise PathError(f"no list index {part!r}") from exc
    if isinstance(doc, dict):
        if part not in doc:
            raise PathError(f"no key {part!r}")
        return doc[part]
    raise PathError(f"cannot descend into {type(doc).__name__} at {part!r}")


def apply_json_patch(obj: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    doc = copy.deepcopy(obj)
    for op in ops:
        kind, parts = op.get("op"), _pointer(str(op.get("path", "")))
        if not parts:
            raise PathError("patching the document root is not allowed")
        parent = doc
        for part in parts[:-1]:
            parent = _container(parent, part)
        last = parts[-1]
        if kind in ("add", "replace"):
            value = copy.deepcopy(op.get("value"))
            if isinstance(parent, list):
                if last == "-":
                    parent.append(value)
                    continue
                index = int(last)
                if kind == "add":
                    parent.insert(index, value)
                else:
                    if index >= len(parent):
                        raise PathError(f"replace past end of list at {last!r}")
                    parent[index] = value
            elif isinstance(parent, dict):
                if kind == "replace" and last not in parent:
                    raise PathError(f"replace of missing key {last!r}")
                parent[last] = value
            else:
                raise PathError(f"cannot set {last!r} on {type(parent).__name__}")
        elif kind == "remove":
            if isinstance(parent, list):
                del parent[int(last)]
            elif isinstance(parent, dict) and last in parent:
                del parent[last]
            else:
                raise PathError(f"remove of missing {last!r}")
        else:
            raise PathError(f"unsupported op {kind!r}")
    return doc


# --- dotted YAML fragments ----------------------------------------------------------------


def split_path(path: str) -> list[str | int]:
    """`spec.template.spec.containers[0].env` -> ['spec', ..., 'containers', 0, 'env']."""
    if path.startswith("/"):
        return [int(p) if p.isdigit() else p for p in _pointer(path)]
    segments: list[str | int] = []
    for part in path.split("."):
        match = _INDEXED.match(part.strip())
        if not match:
            raise PathError(f"unreadable path segment {part!r} in {path!r}")
        segments.append(match.group("name"))
        if match.group("index") is not None:
            segments.append(int(match.group("index")))
    return segments


def _named(items: Any) -> bool:
    return isinstance(items, list) and bool(items) and all(
        isinstance(i, dict) and "name" in i for i in items
    )


def merge_value(existing: Any, value: Any) -> Any:
    """Merge `value` into `existing` the way a reader of a fix means it.

    Mappings merge key by key. Lists of named items (containers, env, volumes, ports) merge by
    name, as Kubernetes' strategic merge does - so "add DATABASE_URL" does not silently delete
    every other variable. An empty mapping or list means "make it empty": merging nothing in
    would leave `nodeSelector: {}` a silent no-op. Anything else is replaced.
    """
    if value == {} or value == []:
        return copy.deepcopy(value)
    if isinstance(existing, dict) and isinstance(value, dict):
        merged = dict(existing)
        for key, item in value.items():
            merged[key] = merge_value(existing.get(key), item)
        return merged
    if _named(existing) and _named(value):
        merged_list = [dict(item) for item in existing]
        positions = {item["name"]: i for i, item in enumerate(merged_list)}
        for item in value:
            if item["name"] in positions:
                index = positions[item["name"]]
                merged_list[index] = merge_value(merged_list[index], item)
            else:
                merged_list.append(copy.deepcopy(item))
        return merged_list
    return copy.deepcopy(value)


def assign(obj: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """Merge `value` in at `path`, creating mappings as needed, on a copy of `obj`.

    A path that walks into a list without an index (`containers.env`) means its only element;
    with more than one element that is ambiguous and refused rather than guessed.
    """
    doc = copy.deepcopy(obj)
    segments = split_path(path)
    if not segments:
        raise PathError("empty path")
    parent: Any = doc
    for position, segment in enumerate(segments[:-1]):
        nxt = segments[position + 1]
        if isinstance(parent, list) and isinstance(segment, str):
            if len(parent) != 1:
                raise PathError(f"{path!r} walks into a list of {len(parent)} without an index")
            parent = parent[0]
        if isinstance(segment, int):
            if not isinstance(parent, list) or segment >= len(parent):
                raise PathError(f"no element {segment} at {path!r}")
            parent = parent[segment]
            continue
        if not isinstance(parent, dict):
            raise PathError(f"cannot descend into {type(parent).__name__} at {segment!r}")
        if segment not in parent or parent[segment] is None:
            parent[segment] = [] if isinstance(nxt, int) else {}
        parent = parent[segment]

    last = segments[-1]
    if isinstance(parent, list) and isinstance(last, str):
        if len(parent) != 1:
            raise PathError(f"{path!r} walks into a list of {len(parent)} without an index")
        parent = parent[0]
    if isinstance(last, int):
        if not isinstance(parent, list) or last >= len(parent):
            raise PathError(f"no element {last} at {path!r}")
        parent[last] = merge_value(parent[last], value)
    elif isinstance(parent, dict):
        parent[last] = merge_value(parent.get(last), value)
    else:
        raise PathError(f"cannot set {last!r} on {type(parent).__name__}")
    return doc


def apply_fragment(obj: dict[str, Any], fragment: dict[str, Any]) -> dict[str, Any]:
    """Apply a YAML fix fragment whose keys are paths (dotted, nested, or a mix)."""
    doc = obj
    for key, value in fragment.items():
        doc = assign(doc, str(key), value)
    return doc
