"""Dotted-path helpers for nested dicts and Pydantic models."""

from __future__ import annotations

import typing
from typing import Any

from pydantic import BaseModel

_MISSING = object()


def flatten(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Dotted path -> leaf value. Dicts recurse; lists are leaves."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(flatten(value, f"{path}."))
        else:
            out[path] = value
    return out


def get_path(record: dict[str, Any], path: str) -> Any:
    node: Any = record
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(path)
        node = node[part]
    return node


def has_path(record: dict[str, Any], path: str) -> bool:
    try:
        get_path(record, path)
    except KeyError:
        return False
    return True


def set_path(record: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = record
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def pop_path(record: dict[str, Any], path: str) -> Any:
    """Remove and return the value at path; prune parents left empty."""
    parts = path.split(".")
    chain: list[dict[str, Any]] = [record]
    node: Any = record
    for part in parts[:-1]:
        if not isinstance(node, dict) or not isinstance(node.get(part), dict):
            raise KeyError(path)
        node = node[part]
        chain.append(node)
    if parts[-1] not in node:
        raise KeyError(path)
    value = node.pop(parts[-1])
    for parent, part in zip(reversed(chain[:-1]), reversed(parts[:-1]), strict=True):
        if parent[part] == {}:
            del parent[part]
        else:
            break
    return value


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """Return the BaseModel inside `X` or `X | None`, else None."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if len(args) == 1 and isinstance(args[0], type) and issubclass(args[0], BaseModel):
        return args[0]
    return None


def _type_name(annotation: Any) -> str:
    name = getattr(annotation, "__name__", None)
    return name if name else str(annotation).replace("typing.", "")


def model_paths(model: type[BaseModel], prefix: str = "") -> dict[str, str]:
    """Dotted path (using input aliases) -> type name, recursing into nested models."""
    out: dict[str, str] = {}
    for name, field in model.model_fields.items():
        key = field.validation_alias if isinstance(field.validation_alias, str) else None
        key = key or field.alias or name
        path = f"{prefix}{key}"
        nested = _nested_model(field.annotation)
        if nested is not None:
            out.update(model_paths(nested, f"{path}."))
        else:
            out[path] = _type_name(field.annotation)
    return out


def model_key(model: type[BaseModel]) -> str:
    return f"{model.__module__}.{model.__qualname__}"
